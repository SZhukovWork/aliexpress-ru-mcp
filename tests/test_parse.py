import json
from pathlib import Path

import pytest

from aliexpress_ru_mcp import parse

FIXTURES = Path(__file__).parent / "fixtures"
HEADLAMP = 1005005416845229   # Choice item, 7 variants, store coupon + order-total tiers
POWERBANK_RU = 438997055       # local seller, lives at /item/1_438997055.html
BAIT = 1005011877521176        # 67 ₽ variant with 564 ₽ delivery


def load(name):
    return json.loads((FIXTURES / name).read_text())


def product(name):
    return load(name)["data"]


# ---- helpers ------------------------------------------------------------------

@pytest.mark.parametrize("text, value", [
    ("1 619 ₽", 1619),
    ("298 081,78 ₽", 298081.78),
    ("3 200 ₽ с купоном", 3200),
    (771, 771),
    ("", None),
    (None, None),
])
def test_rub_parses_site_strings(text, value):
    assert parse.rub(text) == value


@pytest.mark.parametrize("text, value", [
    ("7 709 купили", 7709), ("4 510", 4510), ("184 goods", 184), ("4K подписчиков", None), ("", None),
])
def test_count_never_guesses_abbreviations(text, value):
    assert parse.count(text) == value


def test_russian_dates():
    assert parse.ru_date("14 апреля 2026") == "2026-04-14"
    assert parse.ru_date("Дополнен 29 июня 2026") == "2026-06-29"
    assert parse.ru_date("вчера") is None


def test_item_refs_and_urls():
    assert parse.parse_item_ref("https://aliexpress.ru/item/1_438997055.html?sku_id=5") == (POWERBANK_RU, 1)
    assert parse.parse_item_ref(str(HEADLAMP)) == (HEADLAMP, 0)
    assert parse.parse_item_ref("1_438997055") == (POWERBANK_RU, 1)
    assert parse.item_url(POWERBANK_RU, 1) == "https://aliexpress.ru/item/1_438997055.html"
    assert parse.item_url(HEADLAMP, 0, "reviews") == f"https://aliexpress.ru/item/{HEADLAMP}/reviews"
    with pytest.raises(ValueError):
        parse.parse_item_ref("not an item")


# ---- search ---------------------------------------------------------------------

def test_search_items_are_numbers_with_source_ids():
    data = load("search_headlamp_price_asc.json")["data"]
    items = parse.search_items(data)
    assert len(items) == 6
    for item in items:
        assert isinstance(item["item_id"], int)
        assert isinstance(item["price_rub"], (int, float)) and item["price_rub"] > 0
        assert item["url"].startswith("https://aliexpress.ru/item/")
    local = [i for i in items if i["source_id"] == 1]
    assert local and all(i["url"].endswith(f"/1_{i['item_id']}.html") for i in local)
    assert all(i["listing_sku_id"].isdigit() for i in items)
    first = items[0]
    assert first["price_before_discount_rub"] == 1002 and first["discount_percent"] == 50
    assert first["orders"] == 600 and first["rating"] == 3.8
    assert first["delivery_estimate"] == {"text": "до 5 дней, бесплатно", "cost_rub_from": 0, "days": "0-5"}


def test_search_meta():
    meta = parse.search_meta(load("search_headlamp_price_asc.json")["data"])
    assert meta["total_found"] == 18319
    assert meta["total_pages"] == 916 and meta["current_page"] == 1
    assert meta["search_info"] and meta["echo_query"] == "налобный фонарь"


# ---- product card ----------------------------------------------------------------

def test_card_price_is_the_active_variant_not_the_range_minimum():
    pd = product("product_1005005416845229.json")
    offer = parse.sku_offer(pd, parse.active_sku(pd))
    assert offer["price_rub"] == 1619          # the card shows 1 619 ₽
    assert pd["price"]["minActivityAmount"]["value"] == 771  # the range minimum is a different variant
    assert offer["options"] == {"Испускаемый цвет": "HL23-S-storage bag"}
    assert offer["stock"] == 6


def test_every_variant_has_options_and_price():
    pd = product("product_1005005416845229.json")
    offers = [parse.sku_offer(pd, s) for s in parse.sku_list(pd)]
    assert len(offers) == 7
    assert {o["price_rub"] for o in offers} == {771, 1319, 1419, 1619, 1009, 1259, 1369}
    assert all(o["options"] and "Испускаемый цвет" in o["options"] for o in offers)


def test_requested_variant_and_unknown_variant():
    pd = product("product_1005005416845229.json")
    assert parse.sku_offer(pd, parse.active_sku(pd, "12000032977363643"))["price_rub"] == 771
    assert parse.active_sku(pd, "123") == {}


def test_two_property_variants():
    pd = product("product_1005011877521176.json")
    offer = parse.sku_offer(pd, parse.active_sku(pd))
    assert set(offer["options"]) == {"Цвет", "Ёмкость аккумулятора"}
    assert offer["price_rub"] == 67 and offer["price_before_discount_rub"] == 133


def test_freight_country_is_what_the_page_sends_never_a_guessed_ru():
    """Legacy defect: an empty sendGoodsCountryCode was replaced with "RU",
    and the freight API answered no methods for goods shipped from China."""
    pd = product("product_1005005416845229.json")
    sku = parse.active_sku(pd)
    assert sku["sendGoodsCountryCode"] == ""
    assert parse.freight_country(pd, sku) == ""
    assert load("freight_1005005416845229_legacy_RU.json")["methods"] == []
    assert len(load("freight_1005005416845229.json")["methods"]) == 2


def test_lot_is_read_from_the_unit_text():
    """The payload's `lot` flag stays false on lot cards; the unit text does not."""
    assert parse.lot({"price": {"lot": False, "lotSizeText": "Цена за 1 лот (100 штук)"}})["pieces_per_lot"] == 100
    assert parse.lot({"price": {"lot": False, "lotSizeText": "Цена за 1 штуку"}}) is None
    assert parse.lot(product("product_1005005416845229.json")) is None


def test_rubles_are_enforced():
    with pytest.raises(ValueError):
        parse.money({"value": 10, "currency": "USD"})


# ---- coupons ---------------------------------------------------------------------

def test_coupon_list_with_thresholds():
    coupons = parse.coupons(product("product_1005005416845229.json")["promotion"])
    store = [c for c in coupons if c["type"] == "store"]
    tiers = [c for c in coupons if c["type"] == "order_total"]
    assert store == [{"type": "store", "discount_rub": 77, "min_order_rub": 908, "needs_claim": True,
                      "valid_until": "2026-11-01"}]
    assert [(c["discount_rub"], c["min_order_rub"]) for c in tiers] == [(116, 1802), (194, 3098), (285, 4523)]
    assert {c["min_pieces"] for c in coupons if c["type"] == "multi_piece"} == {3, 4, 5}


def test_one_unit_gets_only_the_coupons_it_unlocks():
    coupons = parse.coupons(product("product_1005005416845229.json")["promotion"])
    one = parse.coupon_summary(1619, 1, coupons)
    assert one["applied"] == [{"type": "store", "discount_rub": 77, "min_order_rub": 908}]
    assert one["per_item_rub"] == 1542           # the card shows "1 542 ₽ взять купон на 77 ₽"
    assert one["next_threshold"] == {"order_from_rub": 1802, "discount_rub": 116, "type": "order_total",
                                     "add_to_order_rub": 183}
    cheap = parse.coupon_summary(771, 1, coupons)
    assert "applied" not in cheap and cheap["total_rub"] == 771   # 771 < 908: no coupon at all


def test_store_coupon_stacks_with_one_order_total_tier():
    coupons = parse.coupons(product("product_1005005416845229.json")["promotion"])
    two = parse.coupon_summary(1619, 2, coupons)   # 3 238 ₽ clears 908 and 3 098
    assert {c["type"]: c["discount_rub"] for c in two["applied"]} == {"store": 77, "order_total": 194}
    assert two["total_rub"] == 3238 - 77 - 194


def test_real_cart_from_the_legacy_check():
    """2 × 2 109 ₽ = 4 218 ₽ → the 240 ₽ coupon (from 3 602 ₽) applies, the
    400 ₽ one (from 6 403 ₽) does not: 3 978 ₽ at checkout."""
    coupons = [{"type": "store", "discount_rub": 240, "min_order_rub": 3602},
               {"type": "store", "discount_rub": 400, "min_order_rub": 6403}]
    assert parse.coupon_summary(2109, 2, coupons)["total_rub"] == 3978
    assert parse.coupon_summary(2109, 1, coupons)["next_threshold"]["order_from_rub"] == 3602


# ---- store and widgets --------------------------------------------------------------

def page_blob():
    return parse.aer_data((FIXTURES / "item_page_1005005416845229.html").read_text())


def test_store_card():
    seller = parse.store(parse.widget_props(page_blob(), "SnowStoreContext"))
    assert seller["name"] == "SUPERFIRE FACTORY Store"
    assert seller["positive_feedback_percent"] == 93.66
    assert seller["followers"] == 3556
    assert seller["badges"] == ["brand", "top"]
    assert seller["orders_shipped"] == 4510 and seller["orders_shipped_period"] == "4 года"
    assert seller["products_rating"] == 4.7


def test_characteristics_and_description_from_async_widgets():
    uuids = parse.widget_uuids(page_blob(), ["HazeProductCharacteristics", "SnowProductContent"])
    assert set(uuids) == {"HazeProductCharacteristics", "SnowProductContent"}
    states = parse.widgets_by_name(load("widgets_1005005416845229.json"))
    chars = parse.characteristics(states["HazeProductCharacteristics"])
    assert {"name": "Номер модели", "value": "HL23 HL23-A HL23-S"} in chars
    assert {"name": "Waterproof Level", "value": "IP44"} in chars
    desc = parse.description(states["SnowProductContent"], 200)
    assert desc["images"] > 0
    assert len(desc.get("text") or "") <= 200


def test_no_characteristics_widget_is_not_an_empty_list():
    assert parse.characteristics(None) is None
    assert parse.characteristics({"groups": []}) == []


# ---- delivery ------------------------------------------------------------------------

def test_delivery_methods():
    resp = load("freight_1005005416845229.json")
    methods = parse.delivery_methods(resp)
    assert [m["method"] for m in methods] == ["В пункт выдачи", "Почтой"]
    assert methods[0]["date_text"] == "7–9 октября"
    assert methods[0]["eta_from"] == "2026-10-07" and methods[0]["eta_to"] == "2026-10-09"
    assert all(m["cost_rub"] == 0 for m in methods)
    assert parse.delivery_destination(resp)["city"] == "Екатеринбург"
    assert parse.buyer_protection_days(resp) == 75


# ---- reviews -----------------------------------------------------------------------------

def test_star_split():
    split = parse.star_split(load("rating_1005005416845229.json"))
    assert split["rating"] == 4.8
    assert split["stars"]["1"] == 22 and split["ratings_counted"] == 1109


def test_reviews_are_parsed_without_buyer_names():
    reviews = parse.reviews(load("reviews_1005005416845229_lowest.json"))
    assert len(reviews) == 10
    assert all(r["stars"] == 1 for r in reviews)
    assert all(r["date"][:2] == "20" for r in reviews)
    assert not any("Покупатель" in json.dumps(r, ensure_ascii=False) for r in reviews)
    assert any(r.get("seller_reply") for r in reviews)
    assert any(r.get("original_text") for r in reviews)
    assert all(isinstance(r["photos"], int) for r in reviews)


def test_aspect_tags_have_numeric_mentions():
    tags = parse.aspect_tags(load("ml_tags_1005005416845229.json"))
    assert tags and all(isinstance(t["mentions"], int) for t in tags)


# ---- city lookup ---------------------------------------------------------------------------

def test_moscow_is_the_capital_not_a_village():
    best, others = parse.pick_city(load("cities_moskva.json")["data"]["cities"], "Москва")
    assert best["cityCode"] == "917477679070000000" and best["localityType"] == "город"
    assert others == []  # villages named "Москва" are neither used nor offered as alternatives


def test_region_hint_picks_among_namesakes():
    cities = load("cities_kirovsk.json")["data"]["cities"]
    best, _ = parse.pick_city(cities, "Кировск, Мурманская")
    assert "Мурманская" in best["provinceName"]
    best, others = parse.pick_city(cities, "Кировск")
    assert best["localityType"] == "город" and any("Мурманская" in (o["provinceName"]) for o in others)
    assert parse.pick_city(cities, "Нигдебург") == (None, [])
