"""Request policies of the client, exercised offline with a scripted fake site."""
import json
from pathlib import Path

import pytest

from aliexpress_ru_mcp import antibot, client as client_mod
from aliexpress_ru_mcp.client import AliexpressClient, Blocked, City, NotFound, RateLimited

EKB = City("Екатеринбург", "Свердловская область", "917483860000000000", "917483866975000000")


class FakeResponse:
    def __init__(self, status=200, body=None, url="https://aliexpress.ru/x", text=None):
        self.status_code = status
        self.url = url
        self.text = text if text is not None else json.dumps(body if body is not None else {})

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A client whose browser and network are scripted."""
    monkeypatch.setenv("AE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("AE_MIN_INTERVAL", "0")
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    mints = []

    def fake_mint(**kwargs):
        mints.append(kwargs)
        return antibot.BrowserSession(user_agent="UA", brands="", platform="Linux", cookies={"cna": "x", "isg": "y"})

    monkeypatch.setattr(antibot, "mint", fake_mint)
    c = AliexpressClient()
    script, sent = [], []

    def fake_request(self, method, url, **kw):
        sent.append({"method": method, "url": url, "cookies": [(k.name, k.value) for k in self.cookies], **kw})
        return script.pop(0)

    monkeypatch.setattr(client_mod.requests.Session, "request", fake_request)
    return c, script, sent, mints


PUNISH = FakeResponse(200, url="https://aliexpress.ru//item/1.html/_____tmd_____/punish?x5secdata=abc", text="<html>captcha</html>")


def test_block_mints_a_new_session_once_and_retries(site):
    c, script, sent, mints = site
    # a session saved by an earlier run, now revoked by the anti-bot
    c._browser = antibot.BrowserSession(user_agent="UA", brands="", platform="Linux", cookies={"old": "1"})
    script += [PUNISH, FakeResponse(200, {"ok": True})]
    status, data = c._request("GET", "https://aliexpress.ru/api")
    assert data == {"ok": True}
    assert len(mints) == 1 and len(sent) == 2
    assert ("old", "1") in sent[0]["cookies"] and ("old", "1") not in sent[1]["cookies"]


def test_block_after_a_fresh_mint_is_an_error_not_a_loop(site):
    c, script, sent, mints = site
    script += [PUNISH, PUNISH, PUNISH]
    with pytest.raises(Blocked):
        c._request("GET", "https://aliexpress.ru/api")
    assert len(mints) == 1 and len(sent) == 1  # minted just now → no second browser launch


def test_soft_throttle_waits_and_retries_once(site):
    c, script, sent, _ = site
    throttled = FakeResponse(200, text='{"ret":["RGV587_ERROR::SM::busy"]}')
    script += [throttled, FakeResponse(200, {"ok": 1})]
    assert c._request("GET", "https://aliexpress.ru/api")[1] == {"ok": 1}
    script += [throttled, throttled]
    with pytest.raises(RateLimited):
        c._request("GET", "https://aliexpress.ru/api")


def test_region_cookie_is_the_only_one_and_names_the_city(site):
    c, script, sent, _ = site
    script += [FakeResponse(200, {}), FakeResponse(200, {})]
    c._request("GET", "https://aliexpress.ru/api", city=EKB)
    c._session().cookies.set("aep_usuc_f", "province=moscow", domain="aliexpress.ru", path="/")
    c._request("GET", "https://aliexpress.ru/api", city=EKB)
    region = [v for k, v in sent[-1]["cookies"] if k == "aep_usuc_f"]
    assert region == [EKB.cookie()]
    assert "city=917483866975000000" in region[0] and "c_tp=RUB" in region[0]


def test_wrong_source_id_is_corrected(site):
    c, script, sent, _ = site
    script += [
        FakeResponse(400, {"error": {"code": "PRODUCT_NOT_FOUND", "message": "..."}}),
        FakeResponse(200, {"data": {"id": "438997055", "name": "x"}}),
    ]
    data, used = c.product_data(438997055, 0, EKB)
    assert used == 1 and data["name"] == "x"
    assert sent[1]["params"]["sourceId"] == "1"


def test_missing_product(site):
    c, script, _, _ = site
    script += [FakeResponse(400, {"error": {"code": "PRODUCT_NOT_FOUND"}})] * 2
    with pytest.raises(NotFound):
        c.product_data(1005099999999999, 0, EKB)


def test_answer_for_another_product_is_rejected(site):
    c, script, _, _ = site
    script += [FakeResponse(200, {"data": {"id": "1", "name": "decoy"}})]
    with pytest.raises(client_mod.AliexpressError):
        c.product_data(2, 0, EKB)


def _page(info):
    return FakeResponse(200, {"data": {"productsFeed": {"productsV2": []}, "searchInfo": info,
                                       "pagination": {"totalPages": 9, "currentPage": 1}}})


def test_deep_page_is_chained_to_the_previous_ones(site):
    c, script, sent, _ = site
    script += [_page("T1"), _page("T2"), _page("T3")]
    c.search("фонарь", 1, "price_asc", None, None, EKB)
    _, continuity = c.search("фонарь", 3, "price_asc", None, None, EKB)
    assert continuity.startswith("continued")
    bodies = [s["json"] for s in sent]
    assert bodies[1]["page"] == 2 and bodies[1]["searchInfo"] == "T1"
    assert bodies[2]["page"] == 3 and bodies[2]["searchInfo"] == "T2"
    assert bodies[2]["sortType"] == "price_asc"


def test_session_file_is_private(site, tmp_path):
    c, script, _, _ = site
    script += [FakeResponse(200, {})]
    c._request("GET", "https://aliexpress.ru/api")
    session = tmp_path / "session.json"
    assert session.exists() and oct(session.stat().st_mode & 0o777) == "0o600"


def test_freight_is_asked_like_the_product_page(site):
    """Legacy defects: sendGoodsCountry forced to "RU" (no methods for goods
    from China) and a hard-coded payload sourceId "0" (no methods for
    /item/1_… items). The page sends the SKU's own (empty) code and the real
    sourceId; only an empty answer triggers one retry with the likely origin."""
    c, script, sent, _ = site
    pd = json.loads((Path(__file__).parent / "fixtures" / "product_1_438997055.json").read_text())["data"]
    sku = pd["skuInfo"]["priceList"][0]
    script += [FakeResponse(200, {"methods": []}), FakeResponse(200, {"methods": [{"groupName": "Почтой"}]})]
    resp, assumed = c.freight(438997055, 1, pd, sku, EKB)
    first, second = sent[0]["json"], sent[1]["json"]
    assert first["sendGoodsCountry"] == "" and first["sourceId"] == "1"
    assert first["provinceCode"] == EKB.province_code and first["cityCode"] == EKB.city_code
    assert second["sendGoodsCountry"] == "RU"  # local seller
    assert assumed == "RU" and resp["methods"]
