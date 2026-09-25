"""Pure translations of raw aliexpress.ru payloads into tool output.

Nothing here does I/O, so every function is exercised by tests on recorded
responses (tests/fixtures). Money leaves as numbers in rubles; the site's own
formatted strings ("1 619 ₽" with non-breaking spaces) are parsed, never
passed on as the only representation.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

BASE = "https://aliexpress.ru"

SORTS: dict[str, str | None] = {
    "relevance": None,             # "Лучшее совпадение" — the site's default
    "orders": "total_tranpro_desc",  # by number of orders (not offered in the site UI, but honoured)
    "newest": "create_desc",       # "Новинки"
    "price_asc": "price_asc",      # "Сначала дешёвые"
    "price_desc": "price_desc",    # "Сначала дорогие"
}

REVIEW_SORTS = {"helpful": 1, "newest": 2, "highest": 3, "lowest": 4}
REVIEW_FILTER_PHOTOS = 1
REVIEW_FILTER_FOLLOW_UP = 2
REVIEW_FILTER_FROM_RUSSIA = 3
REVIEW_PAGE_SIZE = 10  # what the site sends; the endpoint answers 400 to 20


def review_star_filter(stars: int) -> int:
    """1★ → 4 … 5★ → 8, as the site's star chips send them. Only one star
    filter takes effect per request (the site's chips are exclusive too)."""
    return stars + 3


# Store badge ids as the product page renders them next to the seller name.
# Seen live: [3, 1] renders "brand" + "в топе"; 4 = "official" is from an
# earlier version of this server and was not seen during the rewrite.
STORE_BADGES = {1: "top", 3: "brand", 4: "official"}

_RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


# ---- small helpers ----------------------------------------------------------

def num(value: float | int | None) -> float | int | None:
    """Rubles as an int when whole, else rounded to kopecks."""
    if value is None:
        return None
    value = float(value)
    return int(value) if value == int(value) else round(value, 2)


def rub(text: Any) -> float | int | None:
    """"1 619 ₽", "298 081,78 ₽", 771 → a number of rubles; None if absent."""
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)):
        return num(text)
    cleaned = re.sub(r"\s", "", str(text))  # \s covers NBSP and thin spaces
    match = re.search(r"\d+(?:[.,]\d+)?", cleaned)
    if not match:
        return None
    return num(float(match.group(0).replace(",", ".")))


def count(text: Any) -> int | None:
    """"7 709 купили" → 7709. Abbreviated counts ("4K") are not guessed."""
    if text is None or text == "":
        return None
    if isinstance(text, int):
        return text
    if re.match(r"\s*\d+(?:[.,]\d+)?\s?[KkКкMmМм](?:\s|$)", str(text)):
        return None
    cleaned = re.sub(r"\s", "", str(text))  # \s covers NBSP and thin spaces
    match = re.match(r"\d+", cleaned)
    return int(match.group(0)) if match else None


def ratio(text: Any) -> float | None:
    """"4,7" / "4.8" → 4.7 / 4.8."""
    if text in (None, ""):
        return None
    try:
        value = float(str(text).replace(",", "."))
    except ValueError:
        return None
    return value or None


def money(obj: dict | None) -> float | int | None:
    if not obj:
        return None
    currency = obj.get("currency")
    if currency and currency != "RUB":
        raise ValueError(f"price in {currency}, expected RUB")
    return num(obj.get("value"))


def discount_percent(before: float | None, now: float | None) -> int | None:
    if not before or now is None or now >= before:
        return None
    return round((before - now) / before * 100)


def iso_ms(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def ru_date(text: str | None) -> str | None:
    """"14 апреля 2026" / "Дополнен 29 июня 2026" → "2026-04-14"; None if unparseable."""
    if not text:
        return None
    match = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", text.lower())
    if not match or match.group(2) not in _RU_MONTHS:
        return None
    try:
        return datetime(int(match.group(3)), _RU_MONTHS[match.group(2)], int(match.group(1))).date().isoformat()
    except ValueError:
        return None


def item_url(item_id: int, source_id: int = 0, page: str | None = None) -> str:
    """Storefront URL. Items with a non-zero sourceId live under a
    ``<sourceId>_<id>`` slug; their plain /item/<id>.html is a 404."""
    slug = f"{source_id}_{item_id}" if source_id else str(item_id)
    return f"{BASE}/item/{slug}/{page}" if page else f"{BASE}/item/{slug}.html"


def parse_item_ref(ref: str | int) -> tuple[int, int]:
    """"1005005416845229", "1_438997055" or a product URL → (item_id, source_id)."""
    text = str(ref).strip()
    match = re.search(r"/item/(?:(\d+)_)?(\d+)", text) or re.fullmatch(r"(?:(\d+)_)?(\d+)", text)
    if not match:
        raise ValueError(f"not an aliexpress.ru item reference: {ref!r}")
    return int(match.group(2)), int(match.group(1) or 0)


def prune(item: dict) -> dict:
    """Drop empty fields; zeros and False stay (they are data)."""
    return {k: v for k, v in item.items() if v is not None and v != [] and v != {}}


# ---- search -------------------------------------------------------------------

def _labelled_leaves(node: Any, name: str | None = None, out: dict | None = None) -> dict:
    """Search snippets are a UI tree; collect text leaves keyed by their element name."""
    out = {} if out is None else out
    if isinstance(node, dict):
        ident = node.get("id")
        if isinstance(ident, dict) and ident.get("name"):
            name = ident["name"]
        text = node.get("text")
        if isinstance(text, str) and text.strip() and name:
            out.setdefault(name, text.strip())
        image = node.get("image")
        if isinstance(image, dict) and name and isinstance(image.get("url"), str):
            out.setdefault(name + "#image", image["url"])
        for key, value in node.items():
            if key != "id":
                _labelled_leaves(value, name, out)
    elif isinstance(node, list):
        for value in node:
            _labelled_leaves(value, name, out)
    return out


def _gokey(item_data: dict, kind: str) -> dict:
    return (((item_data.get("trackingInfo") or {}).get("webTrackInfo") or {}).get(kind) or {}).get("gokey") or {}


def snippet(product: dict) -> dict:
    """One search hit. Numbers come from the structured itemData; the rest
    from the labelled texts the card shows."""
    container = product.get("snippetContainer") or {}
    data = container.get("itemData") or {}
    props = data.get("properties") or {}
    pre = (data.get("pdpInfo") or {}).get("preloadedData") or {}
    texts = _labelled_leaves(container.get("presentations"))
    behaviour = _gokey(data, "click").get("ae_click_behavior") or {}
    if not isinstance(behaviour, dict):
        behaviour = {}

    item_id = int(props["id"]) if str(props.get("id") or "").isdigit() else None
    source_id = int(props.get("sourceId") or 0)
    price = rub((pre.get("price") or {}).get("value"))
    before = rub(texts.get("partWrap.text.secondaryPrice.amount"))
    delivery_text = next((v for k, v in texts.items() if k.startswith("partWrap.text.delivery")), None)
    delivery_cost = behaviour.get("delivery_price")
    eta = behaviour.get("snippet_delivery_eta") or None
    store = pre.get("store") or {}
    all_skus = props.get("allSkuIds") or []
    return prune({
        "item_id": item_id,
        "source_id": source_id,
        "name": pre.get("title") or texts.get("partWrap.text.title"),
        "price_rub": price,
        "price_before_discount_rub": before if before and price and before > price else None,
        "discount_percent": discount_percent(before, price),
        "listing_coupon_price_rub": rub(texts.get("partWrap.text.coupon")),
        "rating": ratio(pre.get("rating") or texts.get("partWrap.text.rating")),
        "orders": count(pre.get("salesCount") or texts.get("partWrap.text.orders")),
        "delivery_estimate": prune({
            "text": delivery_text,
            "cost_rub_from": num(delivery_cost) if isinstance(delivery_cost, (int, float)) else None,
            "days": eta,
        }) or None,
        "listing_sku_id": str(props["preselectSkuId"]) if props.get("preselectSkuId") else None,
        "variants": len(all_skus) if len(all_skus) > 1 else None,
        "choice": True if props.get("isCombo") or "partWrap.image.tags.choice#image" in texts else None,
        "sponsored": True if props.get("isP4p") else None,
        "seller": store.get("name") or None,
        "store_url": store.get("url") or None,
        "url": item_url(item_id, source_id) if item_id else None,
    })


def search_meta(data: dict) -> dict:
    products = (data.get("productsFeed") or {}).get("productsV2") or []
    total = None
    for p in products[:3]:
        gk = _gokey((p.get("snippetContainer") or {}).get("itemData") or {}, "exposure")
        if isinstance(gk.get("exp_result_cnt"), int):
            total = gk["exp_result_cnt"]
            break
    pagination = data.get("pagination") or {}
    return {
        "total_found": total,
        "total_pages": pagination.get("totalPages"),
        "current_page": pagination.get("currentPage"),
        "search_info": data.get("searchInfo") or "",
        "echo_query": (data.get("searchQuery") or {}).get("searchText"),
        "filtered_out": [e.get("message") for e in data.get("errors") or [] if e.get("message")],
    }


def search_items(data: dict) -> list[dict]:
    products = (data.get("productsFeed") or {}).get("productsV2") or []
    return [s for s in (snippet(p) for p in products) if s.get("item_id")]


# ---- product card (productData) -------------------------------------------------

def sku_list(pd: dict) -> list[dict]:
    return ((pd.get("skuInfo") or {}).get("priceList")) or []


def active_sku(pd: dict, sku_id: str | None = None) -> dict:
    """The SKU the card shows (or the requested one). productData.price is a
    min–max range over all SKUs; the card's price is the active SKU's."""
    skus = sku_list(pd)
    if not skus:
        return {}
    wanted = str(sku_id or pd.get("activeSkuId") or "")
    for sku in skus:
        if str(sku.get("skuId")) == wanted:
            return sku
    return {} if sku_id else skus[0]


def sku_options(pd: dict, sku: dict) -> dict[str, str]:
    """{"Цвет": "Белый", "Ёмкость": "20000 мАч"} — as the site labels the choice."""
    ids = {x for x in str(sku.get("skuPropIds") or "").split(",") if x}
    options: dict[str, str] = {}
    for prop in (pd.get("skuInfo") or {}).get("propertyList") or []:
        for value in prop.get("values") or []:
            if str(value.get("id")) in ids:
                options[prop.get("name") or "?"] = value.get("displayName") or value.get("name") or "?"
    return options


def sku_offer(pd: dict, sku: dict) -> dict:
    price = money(sku.get("activityAmount"))
    before = money(sku.get("amount"))
    return {
        "sku_id": str(sku.get("skuId")),
        "options": sku_options(pd, sku) or None,
        "price_rub": price,
        "price_before_discount_rub": before if before and price is not None and before > price else None,
        "discount_percent": discount_percent(before, price),
        "stock": sku.get("availQuantity"),
    }


def lot(pd: dict) -> dict | None:
    """Lot selling: "Цена за 1 лот (100 штук)". The ``lot`` flag in the payload
    stays false even then, so the site's own unit text is what decides."""
    price = pd.get("price") or {}
    text = price.get("lotSizeText") or ""
    if not (price.get("lot") or re.search(r"\bлот", text, re.I)):
        return None
    pieces = re.search(r"(\d+)\s*(?:шт|pieces|piece)", text)
    return {
        "pieces_per_lot": int(pieces.group(1)) if pieces else (price.get("numberPerLot") or None),
        "site_says": text or None,
        "note": "Prices on this card are per lot of several pieces, not per piece.",
    }


def _ship_from(pd: dict, sku: dict) -> str | None:
    code = sku.get("sendGoodsCountryCode") or None
    if code:
        return code
    ids = {x for x in str(sku.get("skuPropIds") or "").split(",") if x}
    for prop in (pd.get("skuInfo") or {}).get("propertyList") or []:
        for value in prop.get("values") or []:
            if str(value.get("id")) in ids and value.get("skuPropertySendGoodsCountryCode"):
                return value["skuPropertySendGoodsCountryCode"]
    return None


def freight_country(pd: dict, sku: dict) -> str:
    """The sendGoodsCountry the site itself sends to the freight API: the SKU's
    own code, which is empty for most items (then "" — never a guessed "RU",
    which makes the API return no methods for goods shipped from China)."""
    return _ship_from(pd, sku) or ""


def warehouse(sku: dict) -> str | None:
    try:
        ext = json.loads(sku.get("freightExt") or "{}")
        prefer = json.loads(ext.get("preferWarehouse") or "{}")
    except (TypeError, ValueError):
        return None
    return prefer.get("type") or None


# ---- coupons ------------------------------------------------------------------

def coupons(promo: dict | None) -> list[dict]:
    """Every coupon on the card with the order subtotal it needs."""
    promo = promo or {}
    out = []
    for c in promo.get("shopCouponList") or []:
        out.append(prune({
            "type": "store",
            "discount_rub": rub(c.get("value")),
            "min_order_rub": rub(c.get("orderValue")) or 0,
            "needs_claim": bool(c.get("acquirable")) or None,
            "valid_until": iso_ms(c.get("endTime")),
        }))
    for c in promo.get("fixedDiscountCouponList") or []:
        out.append({
            "type": "order_total",
            "discount_rub": rub(c.get("discountAmount")),
            "min_order_rub": rub(c.get("fixedAmount")) or 0,
        })
    for c in promo.get("pieceCouponList") or []:
        out.append(prune({
            "type": "multi_piece",
            "discount_percent": rub(c.get("discount")),
            "min_pieces": c.get("conditionPiece"),
        }))
    for key, label in (("shopPromoCodeList", "promo_code"), ("crossCouponList", "cross_store"),
                       ("newUserCouponList", "new_user")):
        for c in promo.get(key) or []:
            out.append(prune({
                "type": label,
                "discount_rub": rub(c.get("value") or c.get("discountAmount")),
                "min_order_rub": rub(c.get("orderValue") or c.get("fixedAmount")),
                "code": c.get("code") if isinstance(c.get("code"), str) else None,
            }))
    return [c for c in out if c.get("discount_rub") or c.get("discount_percent")]


def _tiers(coupon_list: list[dict]) -> list[tuple[str, float, float]]:
    """(kind, threshold, value) for coupons that gate on the order subtotal —
    one store coupon and one order-total tier can stack; nothing else."""
    return [
        (c["type"], float(c.get("min_order_rub") or 0), float(c["discount_rub"]))
        for c in coupon_list if c["type"] in ("store", "order_total") and c.get("discount_rub")
    ]


def applied_coupons(subtotal: float, coupon_list: list[dict]) -> list[dict]:
    """Best coupon of each stackable kind whose threshold ``subtotal`` clears."""
    applied = []
    for kind in ("store", "order_total"):
        fits = [(value, threshold) for k, threshold, value in _tiers(coupon_list) if k == kind and threshold <= subtotal]
        if fits:
            value, threshold = max(fits)
            applied.append({"type": kind, "discount_rub": num(value), "min_order_rub": num(threshold)})
    return applied


def coupon_hint(subtotal: float, coupon_list: list[dict]) -> dict | None:
    """The nearest unreached threshold that would give a bigger discount of its
    kind than what already applies — "add more to the cart and it gets cheaper"."""
    applied = {c["type"]: c["discount_rub"] for c in applied_coupons(subtotal, coupon_list)}
    better = [
        (threshold, value, kind) for kind, threshold, value in _tiers(coupon_list)
        if threshold > subtotal and value > applied.get(kind, 0)
    ]
    if not better:
        return None
    threshold, value, kind = min(better)
    return {"order_from_rub": num(threshold), "discount_rub": num(value), "type": kind,
            "add_to_order_rub": num(threshold - subtotal)}


def coupon_summary(price: float | None, quantity: int, coupon_list: list[dict]) -> dict | None:
    if price is None:
        return None
    subtotal = price * quantity
    applied = applied_coupons(subtotal, coupon_list)
    discount = sum(c["discount_rub"] for c in applied)
    return prune({
        "quantity": quantity,
        "subtotal_rub": num(subtotal),
        "applied": applied,
        "total_rub": num(subtotal - discount),
        "per_item_rub": num((subtotal - discount) / quantity),
        "next_threshold": coupon_hint(subtotal, coupon_list),
    })


# ---- store (SSR blob of the item page) ----------------------------------------------

_AER_DATA = re.compile(r'<script id="__AER_DATA__" type="application/json">(.*?)</script>', re.S)


def aer_data(page_html: str) -> dict | None:
    match = _AER_DATA.search(page_html or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def iter_widgets(widgets: Iterable[dict] | None) -> Iterable[dict]:
    for w in widgets or []:
        yield w
        yield from iter_widgets(w.get("children"))


def widget_props(blob: dict | None, name: str) -> dict | None:
    for w in iter_widgets((blob or {}).get("widgets")):
        if name in (w.get("widgetId") or ""):
            return w.get("props") or {}
    return None


def widget_uuids(blob: dict | None, names: Iterable[str]) -> dict[str, str]:
    """uuid of the first widget of each wanted kind; the /widget endpoint
    resolves async widgets (characteristics, description) by these ids."""
    wanted = list(names)
    found: dict[str, str] = {}
    for w in iter_widgets((blob or {}).get("widgets")):
        wid = w.get("widgetId") or ""
        for name in wanted:
            if name not in found and f"/{name}/" in wid and w.get("uuid"):
                found[name] = w["uuid"]
    return found


def store(props: dict | None) -> dict | None:
    if not props:
        return None
    stats = {s.get("type"): s for s in props.get("stats") or []}
    rated, shipped = stats.get(1) or {}, stats.get(0) or {}
    period = re.search(r"за\s+(.+)$", (shipped.get("description") or "").replace("\n", " ").strip())
    tags = props.get("tags") or []
    followers = props.get("subscribersCount")
    return prune({
        "name": props.get("name"),
        "seller_id": props.get("id"),
        "store_url": props.get("url"),
        "positive_feedback_percent": (props.get("positiveReviews") or {}).get("percentages"),
        "followers": int(followers) if str(followers or "").isdigit() else None,
        "badges": [STORE_BADGES[t] for t in tags if t in STORE_BADGES] or None,
        "badges_unrecognised": [t for t in tags if t not in STORE_BADGES] or None,
        "products_rating": ratio(rated.get("value")),
        "products_rating_basis": (rated.get("description") or "").strip() or None,
        "orders_shipped": count(shipped.get("value")),
        "orders_shipped_period": period.group(1).strip() if period else None,
        "items_listed": count(props.get("totalGoodsFormatted")),
        "highlight": props.get("description") or None,
        "legal_info_url": props.get("legalInfoUrl") or None,
    })


# ---- async widgets (/widget?uuid=...) --------------------------------------------

def widgets_by_name(resp: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for w in resp.get("widgets") or []:
        state = (w.get("state") or {}).get("data")
        if w.get("name") and isinstance(state, dict):
            out.setdefault(w["name"], state)
    return out


def characteristics(state: dict | None) -> list[dict] | None:
    if state is None:
        return None
    rows = []
    for group in state.get("groups") or []:
        for prop in group.get("properties") or []:
            if prop.get("name") and prop.get("value") not in (None, ""):
                row = {"name": prop["name"], "value": prop["value"]}
                if group.get("title"):
                    row["group"] = group["title"]
                rows.append(row)
    return rows


def description(state: dict | None, max_chars: int) -> dict | None:
    if state is None:
        return None
    raw = state.get("html") or ""
    images = len(re.findall(r"<img\b", raw, re.I))
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", text, flags=re.I)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    text = "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return prune({
        "text": text[:max_chars] or None,
        "truncated": True if len(text) > max_chars else None,
        "images": images,
    })


# ---- delivery -------------------------------------------------------------------

def delivery_methods(resp: dict) -> list[dict]:
    methods = []
    for m in resp.get("methods") or []:
        amount = m.get("amount") or {}
        methods.append(prune({
            "method": m.get("groupName"),
            "date_text": m.get("dateFormat"),
            "eta_from": m.get("etaStartDeliveryDate") or None,
            "eta_to": m.get("etaEndDeliveryDate") or m.get("dateDisplay") or None,
            "cost_rub": money(amount) if amount.get("value") is not None else None,
            "service": m.get("serviceName") or None,
            "type": m.get("serviceGroupType") or None,
            "passport_required": True if m.get("passportRequired") else None,
        }))
    return methods


def buyer_protection_days(resp: dict) -> int | None:
    return next((m["commitDay"] for m in resp.get("methods") or [] if m.get("commitDay")), None)


def delivery_destination(resp: dict) -> dict:
    to = resp.get("to") or {}
    return {"city": to.get("cityName") or None, "region": to.get("regionName") or None,
            "city_code": to.get("city") or None}


def delivery_origin(resp: dict) -> str | None:
    frm = resp.get("from") or {}
    return frm.get("countryName") or frm.get("countryCode") or None


# ---- ratings and reviews ------------------------------------------------------------

def star_split(resp: dict) -> dict | None:
    data = resp.get("data") or {}
    stats = data.get("statistics") or []
    if not stats and data.get("rating") is None:
        return None
    split = {str(s.get("star")): count(s.get("amount")) or 0 for s in stats if s.get("star")}
    return prune({
        "rating": data.get("rating") or None,
        "ratings_counted": sum(split.values()) if split else None,
        "stars": split or None,
    })


def _country(flag_url: str | None) -> str | None:
    match = re.search(r"/([a-z]{2})\.svg", flag_url or "")
    return match.group(1).upper() if match else None


def review(r: dict) -> dict:
    root = r.get("root") or {}
    extra = r.get("additional") or {}
    text, original = root.get("text"), root.get("originalText")
    replies = [c.get("text") for c in root.get("comments") or [] if c.get("text")]
    follow_up = None
    if extra.get("text") or extra.get("images"):
        follow_up = prune({
            "date": ru_date(extra.get("date")),
            "text": extra.get("text"),
            "original_text": extra.get("originalText") if extra.get("originalText") not in (None, extra.get("text")) else None,
            "photos": len(extra.get("images") or []) or None,
        })
    return prune({
        "review_id": root.get("id"),
        "date": ru_date(root.get("date")) or root.get("date"),
        "stars": root.get("grade"),
        "text": text,
        "original_text": original if original and original != text else None,
        "bought_variant": (r.get("product") or {}).get("skuProperties") or None,
        "photos": len(root.get("images") or []),
        "helpful_votes": (r.get("interaction") or {}).get("likesAmount") or None,
        "buyer_country": _country((r.get("reviewer") or {}).get("countryFlag")),
        "seller_reply": replies[0] if len(replies) == 1 else (replies or None),
        "follow_up": follow_up,
    })


def reviews(resp: dict) -> list[dict]:
    return [review(r) for r in (resp.get("data") or {}).get("reviews") or []]


def aspect_tags(resp: dict) -> list[dict]:
    return [
        prune({
            "tag": t.get("title"),
            "mentions": count(t.get("counter")),
            "positive_percent": (t.get("bar") or {}).get("positivePercent"),
        })
        for t in (resp.get("data") or {}).get("tags") or []
    ]


# ---- city lookup ------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def pick_city(cities: list[dict], query: str) -> tuple[dict | None, list[dict]]:
    """Best match for "Город" or "Город, регион" among the site's city suggests.

    Exact name matches only (a village called "Москва" never replaces the
    capital silently): towns first, then other localities. Namesakes of the
    same kind (two towns called Кировск) are returned as alternatives so the
    ambiguity is visible.
    """
    name, _, hint = query.partition(",")
    name, hint = _norm(name), _norm(hint)
    exact = [c for c in cities if _norm(c.get("cityName")) == name]
    if hint:
        exact = [c for c in exact if hint.split()[0] in _norm(f"{c.get('provinceName')} {c.get('extendedCityName')}")]
    exact.sort(key=lambda c: c.get("localityType") != "город")
    if not exact:
        return None, []
    best = exact[0]
    return best, [c for c in exact[1:] if c.get("localityType") == best.get("localityType")]


def city_record(c: dict) -> dict:
    return {
        "name": c.get("cityName"),
        "region": c.get("extendedCityName") or c.get("provinceName"),
        "province_code": c.get("provinceCode"),
        "city_code": c.get("cityCode"),
    }
