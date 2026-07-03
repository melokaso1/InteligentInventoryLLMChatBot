import pytest
from unittest.mock import AsyncMock, patch

from app.graph import chat_graph
from app.graph.chat_graph import (
    SESSIONS,
    _quantity_chip_values,
    _quantity_chips,
    run_chat,
)

CATALOG = {
    "PLZ-HNG-034": {
        "code": "PLZ-HNG-034",
        "name": "Trufas Mágicas Holandesas",
        "price": 120000,
        "stock": 17,
        "saleUnit": "unit",
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
            if s and s in _strip_accents(p["name"].upper())
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


def test_quantity_chips_never_exceed_stock():
    values = _quantity_chip_values(17)
    assert values == [17, 10, 5]
    assert all(value <= 17 for value in values)
    assert 50 not in values

    chips = _quantity_chips(17, "unit")
    assert "50 unidades" not in chips
    assert "17 unidades" in chips
    assert "10 unidades" in chips
    assert "5 unidades" in chips


@pytest.mark.asyncio
async def test_hola_after_sale_completed_returns_welcome():
    SESSIONS["sale-greet"] = {
        "phase": "sale_completed",
        "product_code": "PLZ-HNG-034",
        "product_name": "Trufas Mágicas Holandesas",
        "unit_price": 120000,
        "stock": 17,
        "quantity": 2,
        "measure_unit": "unit",
        "customer_name": "Cliente",
        "customer_email": "cliente@elplonsazo.com",
        "invoice_number": "INV-1",
        "operation_summary": {"status": "Completada"},
        "pending_sale": False,
        "awaiting_quantity": False,
        "awaiting_stock_sku": False,
        "awaiting_product_search": False,
        "chat_history": [],
    }

    result = await run_chat("sale-greet", "hola")

    assert "Drogui" in result.response
    assert "Trufas" not in result.response
    assert "Holandesas" not in result.response
    assert SESSIONS["sale-greet"]["phase"] == "idle"
    assert SESSIONS["sale-greet"]["product_code"] == ""
    assert SESSIONS["sale-greet"]["product_name"] == ""


@pytest.mark.asyncio
async def test_awaiting_quantity_chips_capped_at_stock():
    SESSIONS["qty-chips"] = {
        "phase": "awaiting_quantity",
        "product_code": "PLZ-HNG-034",
        "product_name": "Trufas Mágicas Holandesas",
        "unit_price": 120000,
        "stock": 17,
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

    result = await run_chat("qty-chips", "no sé")

    assert "50 unidades" not in " ".join(result.chips or [])
    assert any("17 unidades" in chip for chip in result.chips or [])


@pytest.mark.asyncio
async def test_greeting_in_flow_phase_resets_to_welcome():
    SESSIONS["flow-greet"] = {
        "phase": "awaiting_quantity",
        "product_code": "PLZ-HNG-034",
        "product_name": "Trufas Mágicas Holandesas",
        "unit_price": 120000,
        "stock": 17,
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

    result = await run_chat("flow-greet", "hola")

    assert "Drogui" in result.response
    assert SESSIONS["flow-greet"]["phase"] == "idle"
    assert SESSIONS["flow-greet"]["product_code"] == ""
