import pytest
from unittest.mock import patch

from app.graph import chat_graph
from app.graph.chat_graph import SESSIONS, run_chat

CATALOG = {
    "PLZ-MJ-001": {
        "code": "PLZ-MJ-001",
        "name": "Marihuana Sativa Indoor Premium",
        "price": 45000,
        "stock": 132,
        "saleUnit": "gram",
        "allowsFractional": True,
    },
    "PLZ-EXT-056": {
        "code": "PLZ-EXT-056",
        "name": "Éxtasis Tesla 300mg",
        "price": 60000,
        "stock": 120,
        "saleUnit": "unit",
        "allowsFractional": False,
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
        return matches[:page_size]

    async def search_paged(q, page_size=5, page=1):
        items = await search(q, page_size=page_size)
        return items, len(items)

    async def sale(*_args, **_kwargs):
        return {"orderNumber": "ORD-1", "invoiceNumber": "INV-1"}

    with (
        patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),
        patch.object(chat_graph.dotnet_tools, "search_products", side_effect=search),
        patch.object(chat_graph.dotnet_tools, "search_products_paged", side_effect=search_paged),
        patch.object(chat_graph.dotnet_tools, "create_sale", side_effect=sale),
    ):
        SESSIONS.clear()
        yield
        SESSIONS.clear()


@pytest.mark.asyncio
async def test_sku_shows_gram_labels():
    result = await run_chat("mj-sku", "PLZ-MJ-001")

    assert "gramos" in result.response.lower()
    assert "unidades" not in result.response.lower()
    assert SESSIONS["mj-sku"]["phase"] == "awaiting_quantity"
    assert SESSIONS["mj-sku"]["measure_unit"] == "gram"
    assert any("gramos" in chip for chip in result.chips or [])


@pytest.mark.asyncio
async def test_awaiting_quantity_quiero_15_gramos_goes_to_confirmation():
    SESSIONS["mj-qty"] = {
        "phase": "awaiting_quantity",
        "product_code": "PLZ-MJ-001",
        "product_name": "Marihuana Sativa Indoor Premium",
        "unit_price": 45000,
        "stock": 132,
        "quantity": 0,
        "measure_unit": "gram",
        "customer_name": "Cliente",
        "customer_email": "cliente@elplonsazo.com",
        "pending_sale": True,
        "awaiting_quantity": True,
        "awaiting_stock_sku": False,
        "awaiting_product_search": False,
        "chat_history": [],
    }

    result = await run_chat("mj-qty", "quiero 15 gramos")

    assert SESSIONS["mj-qty"]["phase"] == "awaiting_confirmation"
    assert SESSIONS["mj-qty"]["quantity"] == 15
    assert SESSIONS["mj-qty"]["measure_unit"] == "gram"
    assert "15 gramos" in result.response.lower()
    assert "Confirmar compra" in (result.chips or [])
    assert "No encontré productos" not in result.response
    assert result.operation_summary is not None
    assert result.operation_summary.quantity == 15


@pytest.mark.asyncio
async def test_awaiting_quantity_2_kilos_converts_to_grams():
    SESSIONS["mj-kg"] = {
        "phase": "awaiting_quantity",
        "product_code": "PLZ-MJ-001",
        "product_name": "Marihuana Sativa Indoor Premium",
        "unit_price": 45000,
        "stock": 5000,
        "quantity": 0,
        "measure_unit": "gram",
        "customer_name": "Cliente",
        "customer_email": "cliente@elplonsazo.com",
        "pending_sale": True,
        "awaiting_quantity": True,
        "awaiting_stock_sku": False,
        "awaiting_product_search": False,
        "chat_history": [],
    }

    result = await run_chat("mj-kg", "2 kilos")

    assert SESSIONS["mj-kg"]["phase"] == "awaiting_confirmation"
    assert SESSIONS["mj-kg"]["quantity"] == 2000
    assert "2000 gramos" in result.response.lower()


@pytest.mark.asyncio
async def test_unit_product_rejects_15_gramos():
    SESSIONS["ext-qty"] = {
        "phase": "awaiting_quantity",
        "product_code": "PLZ-EXT-056",
        "product_name": "Éxtasis Tesla 300mg",
        "unit_price": 60000,
        "stock": 120,
        "quantity": 0,
        "measure_unit": "unit",
        "customer_name": "Cliente",
        "customer_email": "cliente@elplonsazo.com",
        "pending_sale": True,
        "awaiting_quantity": True,
        "awaiting_stock_sku": False,
        "awaiting_product_search": False,
        "chat_history": [],
    }

    result = await run_chat("ext-qty", "15 gramos")

    assert SESSIONS["ext-qty"]["phase"] == "awaiting_quantity"
    assert "unidades" in result.response.lower()
    assert "No encontré productos" not in result.response
