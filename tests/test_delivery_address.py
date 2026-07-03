import pytest

from app.graph import chat_graph
from app.graph.chat_graph import SESSIONS, _parse_delivery_address, run_chat
from unittest.mock import patch


@pytest.mark.parametrize(
    ("message", "address", "city"),
    [
        ("Calle 45 #12-30, Bogotá", "Calle 45 #12-30", "Bogotá"),
        ("vivo en Carrera 7 con 80, Medellín", "Carrera 7 con 80", "Medellín"),
        ("Mi dirección es Calle 10 #20-30; Cali", "Calle 10 #20-30", "Cali"),
        ("entregar en Avenida 6 #15-20, Bucaramanga", "Avenida 6 #15-20", "Bucaramanga"),
        ("envío a Calle 5 en Pereira", "Calle 5", "Pereira"),
        (
            "carrera 2da este 87 a 63 sur bogota",
            "carrera 2da este 87 a 63 sur",
            "Bogotá",
        ),
        ("Carrera 15 #80-20 medellin", "Carrera 15 #80-20", "Medellín"),
    ],
)
def test_parse_delivery_address_spanish(message: str, address: str, city: str):
    parsed_address, parsed_city = _parse_delivery_address(message)
    assert parsed_address == address
    assert parsed_city == city


def test_parse_delivery_address_requires_both_parts():
    assert _parse_delivery_address("Bogotá") == (None, None)
    assert _parse_delivery_address("") == (None, None)


def test_parse_delivery_address_without_known_city_fails():
    assert _parse_delivery_address("Calle 123 #45-67 Apartamento 301") == (None, None)


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
            "tax": 32300.0,
            "total": 202300.0,
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

    async def saved_address(_email):
        return None

    sales: list[dict] = []

    async def sale(
        customer_name,
        customer_email,
        line_items,
        session_id=None,
        delivery_address=None,
        delivery_city=None,
        save_delivery_address=False,
    ):
        sales.append(
            {
                "customer_name": customer_name,
                "customer_email": customer_email,
                "line_items": line_items,
                "delivery_address": delivery_address,
                "delivery_city": delivery_city,
                "save_delivery_address": save_delivery_address,
            }
        )
        return {"orderNumber": "ORD-ADDR", "invoiceNumber": "INV-ADDR"}

    with (
        patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),
        patch.object(chat_graph.dotnet_tools, "search_products", side_effect=search),
        patch.object(chat_graph.dotnet_tools, "search_products_paged", side_effect=search_paged),
        patch.object(chat_graph.dotnet_tools, "check_stock", side_effect=stock),
        patch.object(
            chat_graph.dotnet_tools,
            "get_customer_saved_delivery_address",
            side_effect=saved_address,
        ),
        patch.object(chat_graph.dotnet_tools, "create_sale", side_effect=sale),
    ):
        SESSIONS.clear()
        yield {"sales": sales}
        SESSIONS.clear()


@pytest.mark.asyncio
async def test_save_address_flow_yes(mock_catalog):
    session_id = "save-yes"
    SESSIONS[session_id] = _awaiting_confirmation_state()

    await run_chat(session_id, "Confirmar compra", _awaiting_confirmation_state())
    result = await run_chat(session_id, "carrera 2da este 87 a 63 sur bogota")
    assert result.state == "awaiting_save_address"
    assert "guardar" in result.response.lower()

    result = await run_chat(session_id, "sí")
    assert result.state == "sale_completed"
    assert mock_catalog["sales"][0]["delivery_address"] == "carrera 2da este 87 a 63 sur"
    assert mock_catalog["sales"][0]["delivery_city"] == "Bogotá"
    assert mock_catalog["sales"][0]["save_delivery_address"] is True


@pytest.mark.asyncio
async def test_save_address_flow_no(mock_catalog):
    session_id = "save-no"
    SESSIONS[session_id] = _awaiting_confirmation_state()

    await run_chat(session_id, "Confirmar compra", _awaiting_confirmation_state())
    await run_chat(session_id, "Calle 45 #12-30, Bogotá")
    result = await run_chat(session_id, "no gracias")

    assert result.state == "sale_completed"
    assert mock_catalog["sales"][0]["save_delivery_address"] is False


@pytest.mark.asyncio
async def test_use_saved_address_when_available(mock_catalog):
    session_id = "saved-addr"

    async def saved_address(email):
        if email == "cliente@elplonsazo.com":
            return {
                "deliveryAddress": "Calle 99 #1-1",
                "deliveryCity": "Bogotá",
            }
        return None

    with patch.object(
        chat_graph.dotnet_tools,
        "get_customer_saved_delivery_address",
        side_effect=saved_address,
    ):
        SESSIONS[session_id] = _awaiting_confirmation_state()
        result = await run_chat(session_id, "Confirmar compra", _awaiting_confirmation_state())

    assert result.state == "awaiting_use_saved_address"
    assert "dirección guardada" in result.response.lower()

    result = await run_chat(session_id, "sí")
    assert result.state == "sale_completed"
    assert mock_catalog["sales"][0]["delivery_address"] == "Calle 99 #1-1"
    assert mock_catalog["sales"][0]["delivery_city"] == "Bogotá"
    assert mock_catalog["sales"][0]["save_delivery_address"] is False
