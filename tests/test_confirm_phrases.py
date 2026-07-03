import pytest
from unittest.mock import patch

from app.graph import chat_graph
from app.graph.chat_graph import SESSIONS, _is_confirm, run_chat

CATALOG = {
    "PLZ-COC-099": {
        "code": "PLZ-COC-099",
        "name": "Cocaína Perlada",
        "price": 85000,
        "stock": 40,
        "saleUnit": "gram",
        "allowsFractional": True,
    },
}


def _awaiting_confirmation_state() -> dict:
    cart_line = {
        "productCode": "PLZ-COC-099",
        "productName": "Cocaína Perlada",
        "quantity": 2.0,
        "measureUnit": "gram",
        "unitPrice": 85000.0,
        "subtotal": 170000.0,
    }
    return {
        "phase": "awaiting_confirmation",
        "product_code": "PLZ-COC-099",
        "product_name": "Cocaína Perlada",
        "unit_price": 85000.0,
        "stock": 40,
        "quantity": 2.0,
        "measure_unit": "gram",
        "pending_sale": True,
        "cart": [cart_line],
        "customer_name": "Cliente",
        "customer_email": "cliente@elplonsazo.com",
        "operation_summary": {
            "transactionId": "TXN-PLZ-COC-099-1",
            "status": "Pendiente de confirmación",
            "lineItems": [cart_line],
            "productCode": "PLZ-COC-099",
            "productName": "Cocaína Perlada",
            "quantity": 2.0,
            "measureUnit": "gram",
            "unitPrice": 85000.0,
            "subtotal": 170000.0,
            "tax": 13600.0,
            "total": 183600.0,
        },
    }


@pytest.fixture(autouse=True)
def mock_catalog():
    async def get_code(code):
        return CATALOG.get(code.strip().upper())

    async def search(q, page_size=5):
        return list(CATALOG.values())[:page_size]

    async def search_paged(q, page_size=5, page=1):
        items = await search(q, page_size=page_size)
        return items, len(items)

    async def stock(code):
        product = await get_code(code)
        if not product:
            raise ValueError(code)
        return {**product, "status": "ok"}

    async def sale(*_args, **_kwargs):
        return {"orderNumber": "ORD-99", "invoiceNumber": "INV-99"}

    with (
        patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),
        patch.object(chat_graph.dotnet_tools, "search_products", side_effect=search),
        patch.object(chat_graph.dotnet_tools, "search_products_paged", side_effect=search_paged),
        patch.object(chat_graph.dotnet_tools, "check_stock", side_effect=stock),
        patch.object(chat_graph.dotnet_tools, "create_sale", side_effect=sale),
    ):
        SESSIONS.clear()
        yield
        SESSIONS.clear()


@pytest.mark.parametrize(
    "phrase",
    [
        "Confirmar compra",
        "Sí, confirmo la compra.",
        "si confirmo la compra",
        "confirmo el pedido",
    ],
)
def test_is_confirm_recognizes_phrases(phrase: str):
    assert _is_confirm(phrase) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["Confirmar compra", "Sí, confirmo la compra."],
)
async def test_awaiting_confirmation_completes_sale(message: str):
    session_id = f"confirm-{message[:8]}"
    SESSIONS[session_id] = _awaiting_confirmation_state()

    result = await run_chat(session_id, message, _awaiting_confirmation_state())

    assert result.state == "sale_completed"
    assert SESSIONS[session_id]["phase"] == "sale_completed"
    assert result.invoice_number == "INV-99"
    assert "Compra confirmada" in result.response


@pytest.mark.asyncio
async def test_create_sale_failure_stays_in_confirmation():
    session_id = "confirm-fail"
    SESSIONS[session_id] = _awaiting_confirmation_state()

    async def failing_sale(*_args, **_kwargs):
        raise RuntimeError("API down")

    with patch.object(chat_graph.dotnet_tools, "create_sale", side_effect=failing_sale):
        result = await run_chat(session_id, "Confirmar compra", _awaiting_confirmation_state())

    assert result.state == "awaiting_confirmation"
    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"
    assert "No pude completar la compra" in result.response
    assert "Confirmar compra" in (result.chips or [])
