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
        "unitContentLabel": "300 mg por unidad",
    },
    "PLZ-LSD-012": {
        "code": "PLZ-LSD-012",
        "name": "LSD Blotter",
        "price": 35000,
        "stock": 80,
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


def _awaiting_quantity_state(product_key: str) -> dict:
    product = CATALOG[product_key]
    fields = chat_graph._product_fields(product)
    return {
        "phase": "awaiting_quantity",
        "product_code": fields["code"],
        "product_name": fields["name"],
        "unit_price": fields["price"],
        "stock": fields["stock"],
        "quantity": 0,
        "measure_unit": fields["saleUnit"],
        "selected_product": fields,
        "customer_name": "Cliente",
        "customer_email": "cliente@elplonsazo.com",
        "pending_sale": True,
        "awaiting_quantity": True,
        "awaiting_stock_sku": False,
        "awaiting_product_search": False,
        "chat_history": [],
    }


def _awaiting_confirmation_state(product_key: str, quantity: float) -> dict:
    state = _awaiting_quantity_state(product_key)
    state["phase"] = "awaiting_confirmation"
    state["awaiting_quantity"] = False
    state["quantity"] = quantity
    state["operation_summary"] = chat_graph._build_summary(state)
    return state


@pytest.mark.asyncio
async def test_awaiting_quantity_unit_content_with_label():
    SESSIONS["ext-unit"] = _awaiting_quantity_state("PLZ-EXT-056")

    result = await run_chat("ext-unit", "y cuanto trae cada unidad?")

    assert SESSIONS["ext-unit"]["phase"] == "awaiting_quantity"
    assert "300 mg" in result.response
    assert "Cada unidad" in result.response
    assert "Indica la cantidad en números" not in result.response
    assert any("unidades" in chip.lower() for chip in result.chips or [])
    assert "Cancelar" in (result.chips or [])


@pytest.mark.asyncio
async def test_awaiting_quantity_unit_content_without_label():
    SESSIONS["lsd-unit"] = _awaiting_quantity_state("PLZ-LSD-012")

    result = await run_chat("lsd-unit", "que trae cada unidad")

    assert SESSIONS["lsd-unit"]["phase"] == "awaiting_quantity"
    assert "no tengo el detalle de contenido por unidad" in result.response.lower()
    assert "Indica la cantidad en números" not in result.response
    assert any("unidades" in chip.lower() for chip in result.chips or [])


@pytest.mark.asyncio
async def test_awaiting_quantity_gram_product_sold_by_weight():
    SESSIONS["mj-gram"] = _awaiting_quantity_state("PLZ-MJ-001")

    result = await run_chat("mj-gram", "cuantos gramos trae")

    assert SESSIONS["mj-gram"]["phase"] == "awaiting_quantity"
    assert "se vende por **gramos**" in result.response
    assert "$45,000 COP" in result.response
    assert "por gramo" in result.response.lower()
    assert any("gramos" in chip.lower() for chip in result.chips or [])


@pytest.mark.asyncio
async def test_awaiting_quantity_still_accepts_numeric_quantity():
    SESSIONS["ext-qty"] = _awaiting_quantity_state("PLZ-EXT-056")

    result = await run_chat("ext-qty", "18 unidades")

    assert SESSIONS["ext-qty"]["phase"] == "awaiting_confirmation"
    assert "Confirmar compra" in (result.chips or [])


@pytest.mark.asyncio
async def test_awaiting_confirmation_unit_content_question():
    SESSIONS["ext-confirm"] = _awaiting_confirmation_state("PLZ-EXT-056", 10)

    result = await run_chat("ext-confirm", "cuanto trae cada unidad")

    assert SESSIONS["ext-confirm"]["phase"] == "awaiting_confirmation"
    assert "300 mg" in result.response
    assert "Confirmar compra" in (result.chips or [])
    assert "10 unidades" in result.response.lower()
