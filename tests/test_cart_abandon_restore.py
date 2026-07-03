import pytest
from unittest.mock import patch

from app.graph import chat_graph
from app.graph.chat_graph import SESSIONS, run_chat

CATALOG = {
    "PLZ-MJ-003": {
        "code": "PLZ-MJ-003",
        "name": "Marihuana Premium",
        "price": 45000,
        "stock": 50,
        "saleUnit": "unit",
        "allowsFractional": False,
        "unitContentLabel": "1 gramo por unidad",
    },
    "PLZ-COC-099": {
        "code": "PLZ-COC-099",
        "name": "Cocaína Perlada — Polvo",
        "price": 180000,
        "stock": 40,
        "saleUnit": "gram",
        "allowsFractional": True,
    },
    "PLZ-COC-100": {
        "code": "PLZ-COC-100",
        "name": "Crack Perlado",
        "price": 85000,
        "stock": 40,
        "saleUnit": "gram",
        "allowsFractional": True,
    },
    "PLZ-LSD-001": {
        "code": "PLZ-LSD-001",
        "name": "LSD Blotter",
        "price": 25000,
        "stock": 100,
        "saleUnit": "unit",
        "allowsFractional": False,
    },
    "PLZ-LSD-042": {
        "code": "PLZ-LSD-042",
        "name": "LSD Microdosis",
        "price": 18000,
        "stock": 0,
        "saleUnit": "unit",
        "allowsFractional": False,
    },
    "PLZ-KET-010": {
        "code": "PLZ-KET-010",
        "name": "Ketamina Líquida",
        "price": 120000,
        "stock": 20,
        "saleUnit": "milliliter",
        "allowsFractional": True,
    },
    "PLZ-MJ-001": {
        "code": "PLZ-MJ-001",
        "name": "Marihuana Premium",
        "price": 45000,
        "stock": 50,
        "saleUnit": "gram",
        "allowsFractional": True,
    },
}


@pytest.fixture(autouse=True)
def mock_catalog():
    async def get_code(code):
        return CATALOG.get(code.strip().upper())

    async def search(q, page_size=20):
        from app.graph.chat_graph import _strip_accents

        s = _strip_accents(q.strip().lower())
        matches = []
        for product in CATALOG.values():
            name = _strip_accents(product["name"].lower())
            code = product["code"].lower()
            if not s or s in name or s in code or any(word in name for word in s.split() if len(word) >= 2):
                matches.append(product)
        return matches[:page_size]

    async def search_paged(q, page_size=20, page=1):
        items = await search(q, page_size=page_size)
        return items, len(items)

    async def stock(code):
        product = await get_code(code)
        if not product:
            raise ValueError(code)
        return {**product, "status": "ok"}

    captured_sales: list[dict] = []

    async def sale(customer_name, customer_email, line_items, session_id=None):
        captured_sales.append(
            {
                "customer_name": customer_name,
                "customer_email": customer_email,
                "line_items": line_items,
                "session_id": session_id,
            }
        )
        return {"orderNumber": "ORD-FLOW", "invoiceNumber": "INV-FLOW"}

    with (
        patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),
        patch.object(chat_graph.dotnet_tools, "search_products", side_effect=search),
        patch.object(chat_graph.dotnet_tools, "search_products_paged", side_effect=search_paged),
        patch.object(chat_graph.dotnet_tools, "check_stock", side_effect=stock),
        patch.object(chat_graph.dotnet_tools, "create_sale", side_effect=sale),
    ):
        SESSIONS.clear()
        yield {"sales": captured_sales}
        SESSIONS.clear()


def _cart_codes(session_id: str) -> set[str]:
    return {item["productCode"] for item in SESSIONS[session_id]["cart"]}


async def _build_two_item_cart(session_id: str) -> None:
    await run_chat(session_id, "PLZ-MJ-003")
    await run_chat(session_id, "2 unidades")
    await run_chat(session_id, "agrega cocaina")
    await run_chat(session_id, "3 gramos")


@pytest.mark.asyncio
async def test_out_of_stock_sku_does_not_prompt_quantity(mock_catalog):
    session_id = "oos-lsd"
    await _build_two_item_cart(session_id)

    await run_chat(session_id, "y dame lsd y ketamina liquida")
    r = await run_chat(session_id, "PLZ-LSD-042")

    assert SESSIONS[session_id]["phase"] != "awaiting_quantity"
    assert "no tiene stock" in r.response.lower()
    assert len(SESSIONS[session_id]["cart"]) == 2
    assert _cart_codes(session_id) == {"PLZ-MJ-003", "PLZ-COC-099"}


@pytest.mark.asyncio
async def test_abandon_add_restores_multi_item_cart(mock_catalog):
    """Reproduce user flow: MJ + crack cart, try LSD/ketamina, then keep original cart."""
    session_id = "abandon-flow"
    await _build_two_item_cart(session_id)

    assert len(SESSIONS[session_id]["cart"]) == 2
    assert _cart_codes(session_id) == {"PLZ-MJ-003", "PLZ-COC-099"}
    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"

    r_lsd = await run_chat(session_id, "y dame lsd y ketamina liquida")
    assert "varios productos" in r_lsd.response.lower() or "PLZ-LSD" in r_lsd.response
    assert len(SESSIONS[session_id]["cart"]) == 2

    r_oos = await run_chat(session_id, "PLZ-LSD-042")
    assert "no tiene stock" in r_oos.response.lower()
    assert SESSIONS[session_id]["phase"] != "awaiting_quantity"
    assert len(SESSIONS[session_id]["cart"]) == 2

    r_restore = await run_chat(session_id, "entonces solo dame la marihuana y la coca")
    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"
    assert len(SESSIONS[session_id]["cart"]) == 2
    assert _cart_codes(session_id) == {"PLZ-MJ-003", "PLZ-COC-099"}
    assert "Resumen del carrito" in r_restore.response

    mj_line = next(
        item for item in SESSIONS[session_id]["cart"] if item["productCode"] == "PLZ-MJ-003"
    )
    assert mj_line["quantity"] == 2

    r_qty = await run_chat(session_id, "y 3 gramos de cocaina")
    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"
    crack_line = next(
        item for item in SESSIONS[session_id]["cart"] if item["productCode"] == "PLZ-COC-099"
    )
    assert crack_line["quantity"] == 3
    assert "Resumen del carrito" in r_qty.response


@pytest.mark.asyncio
async def test_cancel_during_add_restores_cart(mock_catalog):
    session_id = "cancel-add"
    await run_chat(session_id, "quiero comprar lsd")
    await run_chat(session_id, "PLZ-LSD-001")
    await run_chat(session_id, "2 unidades")
    await run_chat(session_id, "agrega marihuana")
    await run_chat(session_id, "PLZ-MJ-001")

    assert SESSIONS[session_id]["phase"] == "awaiting_quantity"
    assert len(SESSIONS[session_id]["cart"]) == 1

    r = await run_chat(session_id, "cancelar")
    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"
    assert len(SESSIONS[session_id]["cart"]) == 1
    assert SESSIONS[session_id]["cart"][0]["productCode"] == "PLZ-LSD-001"
    assert "Resumen" in r.response


@pytest.mark.asyncio
async def test_multi_product_add_queues_second_product(mock_catalog):
    session_id = "multi-add"
    await run_chat(session_id, "quiero comprar lsd")
    await run_chat(session_id, "PLZ-LSD-001")
    await run_chat(session_id, "2 unidades")

    await run_chat(session_id, "y dame lsd y ketamina liquida")
    assert SESSIONS[session_id].get("pending_add_queue") == ["ketamina liquida"]
