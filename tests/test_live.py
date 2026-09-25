"""End-to-end check over the real MCP stdio transport against live aliexpress.ru.

Deselected by default; run with:  pytest -m live
Needs a Russian IP. The server starts with an empty cache directory, so it
has to mint an anti-bot session in Chromium from inside a tool call (a worker
thread of the MCP server) — the path that re-mints a blocked session.
"""
import json
import os
import sys

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.live

HEADLAMP = 1005005416845229   # Choice item with 7 variants and coupons
POWERBANK_RU = 438997055       # local seller, /item/1_438997055.html
TOOLS = {"search_products", "get_product", "get_reviews", "compare_products"}


async def _call_all(cache_dir):
    env = {**os.environ, "AE_CACHE_DIR": str(cache_dir), "AE_CITY": "Екатеринбург"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "aliexpress_ru_mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            results = {}
            for key, name, args in [
                ("search1", "search_products", {"query": "налобный фонарь", "sort": "price_asc",
                                                "price_min": 500, "price_max": 1500}),
                ("search2", "search_products", {"query": "налобный фонарь", "sort": "price_asc",
                                                "price_min": 500, "price_max": 1500, "page": 2}),
                ("product", "get_product", {"item_id": HEADLAMP}),
                ("product_local", "get_product", {"item_id": POWERBANK_RU, "source_id": 1, "city": "Москва"}),
                ("reviews", "get_reviews", {"item_id": HEADLAMP, "sort": "lowest", "stars": 1, "limit": 12}),
                ("compare", "compare_products", {"items": [str(HEADLAMP), f"1_{POWERBANK_RU}"]}),
            ]:
                res = await session.call_tool(name, args)
                assert not res.is_error, (name, res.content)
                results[key] = res.structured_content or json.loads(res.content[0].text)
            return tools, results


def test_all_tools_over_stdio(tmp_path):
    tools, r = anyio.run(_call_all, tmp_path)
    assert tools == TOOLS
    assert (tmp_path / "session.json").exists(), "the server did not mint a session itself"

    s1, s2 = r["search1"], r["search2"]
    assert s1["total_found"] > 0 and len(s1["items"]) == 20 and s1["has_more"]
    assert s1["city"]["name"] == "Екатеринбург"
    # The window filters on AliExpress's own price field; a listing can show a
    # cheaper pre-selected variant, so only most prices must be inside it.
    prices = [i["price_rub"] for i in s1["items"] + s2["items"]]
    inside = [p for p in prices if 500 <= p <= 1500]
    assert len(inside) >= 0.8 * len(prices) and min(prices) >= 500 * 0.7
    assert s2["page_continuity"].startswith("continued")
    ids1 = {i["item_id"] for i in s1["items"]}
    assert len(ids1 & {i["item_id"] for i in s2["items"]}) <= 2

    p = r["product"]
    variants = {v["sku_id"]: v["price_rub"] for v in p["variants"]}
    assert p["selected_variant"]["price_rub"] == variants[p["selected_variant"]["sku_id"]]
    assert p["with_coupons"]["per_item_rub"] <= p["selected_variant"]["price_rub"]
    assert p["delivery"]["to"] == "Екатеринбург" and p["delivery"]["methods"]
    assert p["seller"]["name"] and p["seller"]["positive_feedback_percent"]
    assert p["characteristics"] and p["rating"]["stars"]

    local = r["product_local"]
    assert local["flags"]["local_seller"] is True and local["source_id"] == 1
    assert local["delivery"]["to"] == "Москва" and local["city"]["name"] == "Москва"

    rv = r["reviews"]
    assert rv["returned"] == 12 and all(x["stars"] == 1 for x in rv["reviews"])
    assert len({x["review_id"] for x in rv["reviews"]}) == 12

    rows = r["compare"]["items"]
    assert [row["item_id"] for row in rows] == [HEADLAMP, POWERBANK_RU]
    assert all(row["price_rub"] and row.get("delivery_fastest") for row in rows)
