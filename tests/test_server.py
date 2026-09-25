import anyio

from aliexpress_ru_mcp import server

TOOLS = {"search_products", "get_product", "get_reviews", "compare_products"}


def tools():
    return {t.name: t for t in anyio.run(server.mcp.list_tools)}


def test_tools_are_registered_read_only():
    registered = tools()
    assert set(registered) == TOOLS
    for tool in registered.values():
        assert tool.annotations.read_only_hint is True
        assert tool.description


def test_enumerations():
    registered = tools()
    sort = registered["search_products"].input_schema["properties"]["sort"]
    assert set(sort["enum"]) == {"relevance", "orders", "price_asc", "price_desc", "newest"}
    review_sort = registered["get_reviews"].input_schema["properties"]["sort"]
    assert set(review_sort["enum"]) == {"helpful", "newest", "lowest", "highest"}


def test_product_takes_source_id_sku_quantity_and_city():
    props = tools()["get_product"].input_schema["properties"]
    assert {"item_id", "source_id", "sku_id", "quantity", "city", "include_description"} <= set(props)


def test_error_reason_reaches_the_agent(monkeypatch):
    import pytest
    from mcp.server.mcpserver.exceptions import ToolError
    from aliexpress_ru_mcp import server
    from aliexpress_ru_mcp.client import RateLimited

    def throttled(*args, **kwargs):
        raise RateLimited("aliexpress.ru is rate limiting this IP; wait a few minutes")

    monkeypatch.setattr(server, "client", lambda: type("C", (), {"__getattr__": lambda self, name: throttled})())
    with pytest.raises(ToolError, match="rate limiting"):
        server.search_products("фонарь")
