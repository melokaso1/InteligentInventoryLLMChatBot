import pytest
from unittest.mock import patch

from app.graph import chat_graph
from app.graph.chat_graph import SESSIONS, run_chat

CATALOG = {
    "PLZ-COC-050": {
        "code": "PLZ-COC-050",
        "name": "Cocaína Sobre Unitario",
        "price": 50000,
        "stock": 10,
        "saleUnit": "unit",
        "allowsFractional": False,
    },
    "PLZ-COC-099": {
        "code": "PLZ-COC-099",
        "name": "Cocaína Perlada — Polvo",
        "price": 85000,
        "stock": 5000,
        "saleUnit": "gram",
        "allowsFractional": True,
    },
}


@pytest.fixture(autouse=True)
def mock_catalog():
    async def get_code(code):
        return CATALOG.get(code.strip().upper())

    async def search(q, page_size=5):
        from app.graph.chat_graph import _strip_accents

        s = _strip_accents(q.strip().upper())
        matches = [
            p
            for p in CATALOG.values()
            if not s
            or s in _strip_accents(p["name"].upper())
            or s in p["code"].upper()
        ]
        # Simulate API returning unit product before gram product.
        matches.sort(key=lambda p: p["code"])
        return matches[:page_size]

    async def search_paged(q, page_size=5, page=1):
        items = await search(q, page_size=page_size)
        return items, len(items)

    with (
        patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),
        patch.object(chat_graph.dotnet_tools, "search_products", side_effect=search),
        patch.object(chat_graph.dotnet_tools, "search_products_paged", side_effect=search_paged),
    ):
        SESSIONS.clear()
        yield
        SESSIONS.clear()


@pytest.mark.asyncio
async def test_purchase_500g_cocaina_uses_gram_product():
    result = await run_chat("coc-500", "quiero comprar 500g de cocaina")

    session = SESSIONS["coc-500"]
    assert session["phase"] == "awaiting_confirmation"
    assert session["product_code"] == "PLZ-COC-099"
    assert session["measure_unit"] == "gram"
    assert session["quantity"] == 500
    assert "unidades" not in result.response.lower()
    assert "500" in result.response
    assert "gramos" in result.response.lower()
    assert result.operation_summary is not None
    assert result.operation_summary.quantity == 500
    assert result.operation_summary.measure_unit == "gram"


@pytest.mark.asyncio
async def test_purchase_2_kg_cocaina_converts_to_grams():
    result = await run_chat("coc-kg", "quiero comprar 2 kilos de cocaina")

    session = SESSIONS["coc-kg"]
    assert session["phase"] == "awaiting_confirmation"
    assert session["product_code"] == "PLZ-COC-099"
    assert session["quantity"] == 2000
    assert "unidades" not in result.response.lower()
    assert "2000 gramos" in result.response.lower()
