"""HTTP client for the aliexpress.ru storefront's internal endpoints.

All calls go through one ``requests`` session that carries the cookies a real
browser was given (see ``antibot``). Policies:

- Calls are serialised by a lock and spaced ``AE_MIN_INTERVAL`` seconds apart
  (the MCP SDK runs sync tools in worker threads, so several tool calls can
  arrive at once).
- An anti-bot block (``_____tmd_____/punish`` page, ``x5secdata``,
  ``rgv587_flag``) drops the session, mints a new one in the browser once and
  repeats the call; a second block in the same call becomes a clear error.
- ``RGV587_ERROR::SM`` is a soft per-IP throttle, not a broken session:
  wait and retry once, then report it — never re-mint for it.
- The delivery region is the ``aep_usuc_f`` cookie (the freight API ignores
  the city in its own payload). It is set on every request, and the city the
  site echoes back is compared with the one asked for.
- Answers are sanity-checked: the product id must match, prices must be in
  rubles, the delivery destination must be the requested city.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from . import antibot, parse

log = logging.getLogger(__name__)

SITE = "https://aliexpress.ru"
SEARCH_URL = f"{SITE}/aer-webapi/v1/search"
PRODUCT_URL = f"{SITE}/aer-jsonapi/v1/bx/pdp/web/productData"
FREIGHT_URL = f"{SITE}/aer-api/v1/pdp/web/freight/calculate"
WIDGET_URL = f"{SITE}/widget"
RATING_URL = f"{SITE}/aer-jsonapi/review-product/v1/product/rating/desktop"
REVIEWS_URL = f"{SITE}/aer-jsonapi/review/v5/desktop/product-reviews"
ML_TAGS_URL = f"{SITE}/aer-jsonapi/review/v1/desktop/product-ml-tags"
CITY_URL = f"{SITE}/aer-jsonapi/bl/maps/v1/address/get-city-suggests"

DEFAULT_CITY = "Москва"
SEARCH_CHAIN_TTL = 30 * 60
SEARCH_CHAIN_WALK = 4  # at most this many earlier pages are fetched to continue a result set

_BLOCK_MARKERS = ("_____tmd_____", "x5secdata", "rgv587_flag", "FAIL_SYS_USER_VALIDATE")
_THROTTLE_MARKERS = ("RGV587_ERROR::SM",)


class AliexpressError(RuntimeError):
    """Anything that stops a tool from returning trustworthy data."""


class Blocked(AliexpressError):
    pass


class RateLimited(AliexpressError):
    pass


class NotFound(AliexpressError):
    pass


@dataclass
class City:
    name: str
    region: str | None
    province_code: str
    city_code: str
    alternatives: list[str] = field(default_factory=list)

    def cookie(self) -> str:
        return (f"b_locale=ru_RU&c_tp=RUB&region=RU&site=rus"
                f"&province={self.province_code}&city={self.city_code}")

    def public(self, source: str) -> dict:
        out = {"name": self.name, "region": self.region, "source": source}
        if self.alternatives:
            out["other_places_with_this_name"] = self.alternatives
        return out


def cache_dir() -> Path:
    root = os.environ.get("AE_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "aliexpress-ru-mcp"
    )
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def default_city_name() -> tuple[str, str]:
    env = (os.environ.get("AE_CITY") or "").strip()
    return (env, "AE_CITY environment variable") if env else (DEFAULT_CITY, "default (set AE_CITY to change)")


class AliexpressClient:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proxy = os.environ.get("AE_PROXY") or None
        self._headless = os.environ.get("AE_HEADLESS", "1") != "0"
        self._min_interval = float(os.environ.get("AE_MIN_INTERVAL", "1.0"))
        self._dir = cache_dir()
        self._session_file = self._dir / "session.json"
        self._cities_file = self._dir / "cities.json"
        self._browser: antibot.BrowserSession | None = self._load_session()
        self._http: requests.Session | None = None
        self._last_call = 0.0
        self._last_mint = float("-inf")
        self._last_save = time.monotonic()
        self._cities: dict[str, dict] = self._load_json(self._cities_file) or {}
        self._chains: dict[tuple, dict] = {}

    # ---- persistence -------------------------------------------------------------

    @staticmethod
    def _load_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _write_private(self, path: Path, data: Any) -> None:
        try:
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never world-readable, not even briefly
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(data, ensure_ascii=False))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("Could not write %s: %s", path, e)

    def _load_session(self) -> antibot.BrowserSession | None:
        data = self._load_json(self._session_file)
        try:
            return antibot.BrowserSession.from_dict(data) if data else None
        except TypeError:
            return None

    def _save_session(self) -> None:
        if self._browser is None:
            return
        if self._http is not None:  # keep cookies the site refreshed along the way
            fresh = {c.name: c.value for c in self._http.cookies if c.name != "aep_usuc_f" and c.value}
            self._browser.cookies.update(fresh)
        self._write_private(self._session_file, self._browser.to_dict())
        self._last_save = time.monotonic()

    # ---- anti-bot session ----------------------------------------------------------

    def _mint(self) -> None:
        log.info("Minting an aliexpress.ru session in Chromium (headless=%s)", self._headless)
        try:
            self._browser = antibot.mint(proxy=self._proxy, headless=self._headless)
        except antibot.AntibotError as e:
            raise Blocked(str(e)) from e
        self._last_mint = time.monotonic()
        self._http = None
        self._save_session()

    def _session(self) -> requests.Session:
        if self._browser is None:
            self._mint()
        if self._http is None:
            b = self._browser
            s = requests.Session()
            s.headers.update({
                "User-Agent": b.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Origin": SITE,
                "Referer": SITE + "/",
            })
            if b.brands:
                s.headers.update({"sec-ch-ua": b.brands, "sec-ch-ua-mobile": "?0",
                                  "sec-ch-ua-platform": f'"{b.platform}"'})
            for name, value in b.cookies.items():
                s.cookies.set(name, value, domain=".aliexpress.ru", path="/")
            if self._proxy:
                s.proxies.update({"http": self._proxy, "https": self._proxy})
            self._http = s
        return self._http

    @staticmethod
    def _set_region(session: requests.Session, city: City) -> None:
        for c in [c for c in session.cookies if c.name == "aep_usuc_f"]:
            session.cookies.clear(c.domain, c.path, c.name)
        session.cookies.set("aep_usuc_f", city.cookie(), domain=".aliexpress.ru", path="/")

    def _pace(self) -> None:
        wait = self._min_interval + random.uniform(0, 0.4) - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    def _request(self, method: str, url: str, *, city: City | None = None, referer: str | None = None,
                 params: dict | None = None, body: Any = None, headers: dict | None = None,
                 html: bool = False) -> tuple[int, Any]:
        """Send one storefront request; return (status, parsed JSON or HTML text).

        Anti-bot and throttle answers never come back from here as data.
        """
        with self._lock:
            reminted = throttled = False
            while True:
                session = self._session()
                if city is not None:
                    self._set_region(session, city)
                extra = dict(headers or {})
                if referer:
                    extra["Referer"] = referer
                if html:
                    extra.update({"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
                    extra.pop("Origin", None)
                self._pace()
                try:
                    resp = session.request(method, url, params=params, json=body, headers=extra, timeout=30)
                except requests.RequestException as e:
                    raise AliexpressError(f"Network error talking to aliexpress.ru: {e}") from e
                finally:
                    self._last_call = time.monotonic()

                head = resp.text[:3000]
                if any(m in head for m in _THROTTLE_MARKERS) or (resp.status_code == 429 and not _is_block(resp, head)):
                    if throttled:
                        raise RateLimited(
                            "aliexpress.ru is throttling this IP (RGV587 / HTTP 429). Wait a few "
                            "minutes before the next call; retrying in a loop makes it worse."
                        )
                    throttled = True
                    log.warning("aliexpress.ru throttled %s, waiting 15s", url)
                    time.sleep(15)
                    continue
                if _is_block(resp, head):
                    recent = time.monotonic() - self._last_mint < 60
                    if reminted or recent:
                        raise Blocked(
                            "aliexpress.ru anti-bot rejects even a freshly minted session. Wait "
                            "10-30 minutes, or run once with AE_HEADLESS=0 to solve the captcha "
                            "in a browser window (AE_PROXY for another IP)."
                        )
                    log.warning("Anti-bot block on %s; minting a new session", url)
                    self._browser = None
                    self._http = None
                    self._mint()
                    reminted = True
                    continue

                if time.monotonic() - self._last_save > 600:
                    self._save_session()
                if html:
                    return resp.status_code, resp.text
                try:
                    return resp.status_code, resp.json()
                except ValueError:
                    raise AliexpressError(
                        f"aliexpress.ru answered HTTP {resp.status_code} with non-JSON for {url}"
                    ) from None

    # ---- city ---------------------------------------------------------------------------

    def city(self, name: str | None) -> tuple[City, str]:
        """Resolve a Russian city name to the site's region codes (cached on disk)."""
        if name and name.strip():
            query, source = name.strip(), "tool argument"
        else:
            query, source = default_city_name()
        key = parse._norm(query)
        with self._lock:
            cached = self._cities.get(key)
            if cached is None:
                status, data = self._request("POST", CITY_URL, body={"countryCode": "RU", "query": query.split(",")[0].strip()})
                if status != 200:
                    raise AliexpressError(f"City lookup failed (HTTP {status})")
                best, others = parse.pick_city((data.get("data") or {}).get("cities") or [], query)
                if best is None:
                    raise AliexpressError(
                        f"aliexpress.ru does not know a city named {query!r}. Use the name as on the "
                        "site, optionally with the region: 'Кировск, Мурманская'."
                    )
                cached = {**parse.city_record(best),
                          "alternatives": [f"{c.get('cityName')} ({c.get('extendedCityName')})" for c in others[:5]]}
                self._cities[key] = cached
                self._write_private(self._cities_file, self._cities)
        return City(**cached), source

    # ---- search -------------------------------------------------------------------------

    def search(self, query: str, page: int, sort: str, price_min: int | None, price_max: int | None,
               city: City) -> tuple[dict, str]:
        """One page of results and how it was chained to the previous pages.

        The site passes an opaque ``searchInfo`` from page N-1 to page N; without
        it deep pages repeat items. The token is cached per result set, and a
        few missing earlier pages are fetched to rebuild the chain.
        """
        key = (parse._norm(query), sort, price_min, price_max, city.city_code)
        with self._lock:
            chain = self._chains.get(key)
            if chain is None or time.monotonic() - chain["at"] > SEARCH_CHAIN_TTL:
                chain = self._chains[key] = {"at": time.monotonic(), "info": {}}
            continuity = "first page"
            if page > 1:
                known = [p for p in chain["info"] if p < page]
                start = max(known) if known else 0
                if page - 1 - start <= SEARCH_CHAIN_WALK:
                    for p in range(start + 1, page):
                        self._search_page(query, p, sort, price_min, price_max, city, chain)
                    continuity = "continued from the previous page"
                else:
                    continuity = ("jumped without the previous page's token: items may repeat those "
                                  "of earlier pages — request pages in order to avoid that")
            data = self._search_page(query, page, sort, price_min, price_max, city, chain)
            return data, continuity

    def _search_page(self, query: str, page: int, sort: str, price_min: int | None, price_max: int | None,
                     city: City, chain: dict) -> dict:
        body: dict[str, Any] = {
            "catId": "", "searchInfo": chain["info"].get(page - 1, "") if page > 1 else "",
            "searchText": query, "storeIds": [], "pgChildren": [], "aeBrainIds": [],
            "searchTrigger": "search_bar", "mainFilters": "", "source": "direct",
        }
        if parse.SORTS[sort]:
            body["sortType"] = parse.SORTS[sort]
        if price_min is not None:
            body["minPrice"] = price_min
        if price_max is not None:
            body["maxPrice"] = price_max
        if page > 1:
            body.update({"page": page, "g": "y"})
        status, data = self._request("POST", SEARCH_URL, body=body, city=city,
                                     referer=f"{SITE}/wholesale?SearchText={requests.utils.quote(query)}")
        payload = data.get("data") if isinstance(data, dict) else None
        if status != 200 or not isinstance(payload, dict) or "productsFeed" not in payload:
            raise AliexpressError(f"aliexpress.ru search failed (HTTP {status}): {_error_text(data)}")
        info = payload.get("searchInfo")
        if info:
            chain["info"][page] = info
        return payload

    # ---- product ------------------------------------------------------------------------

    def product_data(self, item_id: int, source_id: int, city: City, sku_id: str | None = None) -> tuple[dict, int]:
        """productData for the item; returns (data, source_id actually used).

        A wrong source_id answers PRODUCT_NOT_FOUND; the other value (0 ↔ 1) is
        tried once so a bare id copied from a URL still works.
        """
        tried = []
        for sid in (source_id, 1 - source_id if source_id in (0, 1) else None):
            if sid is None:
                continue
            tried.append(sid)
            status, data = self._request(
                "GET", PRODUCT_URL, city=city, referer=parse.item_url(item_id, sid),
                params={"productId": str(item_id), "sourceId": str(sid), "sku_id": str(sku_id or 0)},
            )
            payload = data.get("data") if isinstance(data, dict) else None
            if status == 200 and isinstance(payload, dict):
                if str(payload.get("id")) != str(item_id):
                    raise AliexpressError(
                        f"aliexpress.ru returned product {payload.get('id')} for {item_id}; not passing it on"
                    )
                return payload, sid
            if _error_code(data) != "PRODUCT_NOT_FOUND":
                raise AliexpressError(f"productData for {item_id} failed (HTTP {status}): {_error_text(data)}")
        raise NotFound(
            f"aliexpress.ru has no product {item_id} (tried source_id {', '.join(map(str, tried))}). "
            "It was removed, or the id is wrong."
        )

    def item_page(self, item_id: int, source_id: int, city: City) -> str | None:
        status, text = self._request("GET", parse.item_url(item_id, source_id), city=city, html=True)
        return text if status == 200 else None

    def widgets(self, item_id: int, source_id: int, uuids: list[str], city: City) -> dict[str, dict]:
        if not uuids:
            return {}
        page = parse.item_url(item_id, source_id)
        status, data = self._request(
            "GET", WIDGET_URL, city=city, referer=page,
            params=[("uuid", u) for u in uuids],  # type: ignore[arg-type]
            headers={"aer-url": page},
        )
        if status != 200 or not isinstance(data, dict):
            log.warning("Widget request for %s failed: HTTP %s", item_id, status)
            return {}
        return parse.widgets_by_name(data)

    def freight(self, item_id: int, source_id: int, pd: dict, sku: dict, city: City,
                quantity: int = 1) -> tuple[dict, str | None]:
        """Delivery quote for one SKU, exactly as the product page asks for it.

        Returns (response, ship_from_assumption). The page sends the SKU's
        ``sendGoodsCountryCode`` — usually empty. Should that ever yield no
        methods, the likely origin (RU for local sellers, CN otherwise) is
        tried once and the assumption is reported.
        """
        logistic = sku.get("logisticAmount") or {}
        try:
            ext = json.loads(sku.get("freightExt") or "{}")
        except ValueError:
            ext = {}
        local = bool(pd.get("isLocalSeller"))
        first = parse.freight_country(pd, sku)
        attempts = [(first, None)]
        if not first:
            attempts.append(("RU" if local else "CN", "RU" if local else "CN"))
        resp: dict = {}
        for country, assumption in attempts:
            payload = {
                "productId": int(item_id), "productIdV2": str(item_id), "sendGoodsCountry": country,
                "country": "RU", "provinceCode": city.province_code, "cityCode": city.city_code,
                "skuId": str(sku.get("skuId")), "count": quantity,
                "maxPrice": logistic.get("value"), "minPrice": logistic.get("value"),
                "tradeCurrency": logistic.get("currency") or "RUB", "displayMultipleFreight": False,
                "ext": ext, "sourceId": str(source_id), "sourceType": "",
                "buyerPrice": sku.get("buyerPriceForLogistic"), "freeDelivery": None,
                "isLocalSeller": local, "unitPriceInfo": {},
            }
            status, data = self._request(
                "POST", FREIGHT_URL, city=city, referer=parse.item_url(item_id, source_id),
                params={"product_id": str(item_id), "sourceId": str(source_id)}, body=payload,
            )
            if status != 200 or not isinstance(data, dict):
                raise AliexpressError(f"Delivery quote for {item_id} failed (HTTP {status}): {_error_text(data)}")
            resp = data
            if data.get("methods"):
                return data, assumption
        return resp, None

    # ---- reviews ------------------------------------------------------------------------

    def rating(self, item_id: int, source_id: int) -> dict:
        status, data = self._request("POST", RATING_URL, referer=parse.item_url(item_id, source_id),
                                     body={"productId": str(item_id), "productSource": source_id, "skuFilter": []})
        return data if status == 200 and isinstance(data, dict) else {}

    def reviews(self, item_id: int, source_id: int, page: int, sort: int, filters: list[int]) -> list[dict]:
        status, data = self._request(
            "POST", REVIEWS_URL, referer=parse.item_url(item_id, source_id, "reviews"),
            body={"productKey": {"id": str(item_id), "sourceId": source_id},
                  "pagination": {"pageNum": page, "pageSize": parse.REVIEW_PAGE_SIZE},
                  "sort": sort, "filters": filters, "skuFilter": []},
        )
        if status != 200 or not isinstance(data, dict) or "data" not in data:
            raise AliexpressError(f"Reviews for {item_id} failed (HTTP {status}): {_error_text(data)}")
        return parse.reviews(data)

    def aspect_tags(self, item_id: int, source_id: int) -> list[dict]:
        status, data = self._request("POST", ML_TAGS_URL, referer=parse.item_url(item_id, source_id),
                                     body={"productKey": {"id": str(item_id), "sourceId": source_id}})
        return parse.aspect_tags(data) if status == 200 and isinstance(data, dict) else []


def _is_block(resp: requests.Response, head: str) -> bool:
    if antibot.PUNISH_MARKER in resp.url:
        return True
    return any(m in head for m in _BLOCK_MARKERS)


def _error_code(data: Any) -> str | None:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("code")
    return None


def _error_text(data: Any) -> str:
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return f"{data['error'].get('code')}: {data['error'].get('message')}"
    return str(data)[:200]
