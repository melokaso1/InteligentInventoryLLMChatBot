import pytest

from unittest.mock import patch



from app.graph import chat_graph

from app.graph.chat_graph import (

    SESSIONS,

    _parse_multi_item_order,

    run_chat,

)



CATALOG = {

    "PLZ-LSD-001": {

        "code": "PLZ-LSD-001",

        "name": "LSD Blotter",

        "price": 25000,

        "stock": 10,

        "saleUnit": "unit",

        "allowsFractional": False,

    },

    "PLZ-MJ-001": {

        "code": "PLZ-MJ-001",

        "name": "Marihuana Sativa Indoor Premium",

        "price": 45000,

        "stock": 10,

        "saleUnit": "gram",

        "allowsFractional": True,

    },

    "PLZ-MJ-002": {

        "code": "PLZ-MJ-002",

        "name": "Marihuana Índica",

        "price": 28000,

        "stock": 10,

        "saleUnit": "gram",

        "allowsFractional": True,

    },

    "PLZ-MJ-003": {

        "code": "PLZ-MJ-003",

        "name": "Marihuana Híbrida Blue Dream",

        "price": 42000,

        "stock": 10,

        "saleUnit": "gram",

        "allowsFractional": True,

    },

    "PLZ-MJ-011": {

        "code": "PLZ-MJ-011",

        "name": "Pre-rolled Sativa x6",

        "price": 18000,

        "stock": 10,

        "saleUnit": "unit",

        "allowsFractional": False,

    },

    "PLZ-COC-099": {

        "code": "PLZ-COC-099",

        "name": "Cocaína Perlada — Polvo",

        "price": 180000,

        "stock": 10,

        "saleUnit": "gram",

        "allowsFractional": True,

    },

    "PLZ-COC-100": {

        "code": "PLZ-COC-100",

        "name": "Crack (Cocaína Base)",

        "price": 95000,

        "stock": 10,

        "saleUnit": "gram",

        "allowsFractional": True,

    },

    "PLZ-KET-021": {

        "code": "PLZ-KET-021",

        "name": "Ketamina Líquida 50ml",

        "price": 180000,

        "stock": 10,

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



        s = _strip_accents(q.strip().lower())

        matches = []

        for product in CATALOG.values():

            name = _strip_accents(product["name"].lower())

            code = product["code"].lower()

            if not s or s in name or s in code or any(word in name for word in s.split()):

                matches.append(product)

        return matches[:page_size]



    async def search_paged(q, page_size=5, page=1):

        items = await search(q, page_size=page_size)

        return items, len(items)



    async def stock(code):

        product = await get_code(code)

        if not product:

            raise ValueError(code)

        return {**product, "status": "ok"}



    captured_sales: list[dict] = []



    async def sale(customer_name, customer_email, line_items, session_id=None, delivery_address=None, delivery_city=None, save_delivery_address=False):

        captured_sales.append(

            {

                "customer_name": customer_name,

                "customer_email": customer_email,

                "line_items": line_items,

                "session_id": session_id,

                "delivery_address": delivery_address,

                "delivery_city": delivery_city,

                "save_delivery_address": save_delivery_address,

            }

        )

        return {"orderNumber": "ORD-MULTI", "invoiceNumber": "INV-MULTI"}



    async def saved_address(_email):
        return None



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

        yield {"sales": captured_sales}

        SESSIONS.clear()





@pytest.mark.asyncio

async def test_add_lsd_then_marihuana_builds_two_item_cart(mock_catalog):

    session_id = "cart-multi"

    await run_chat(session_id, "quiero comprar lsd")

    assert SESSIONS[session_id]["phase"] == "awaiting_quantity"



    r_qty = await run_chat(session_id, "8 unidades")

    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"

    assert len(SESSIONS[session_id]["cart"]) == 1

    assert SESSIONS[session_id]["cart"][0]["productCode"] == "PLZ-LSD-001"

    assert SESSIONS[session_id]["cart"][0]["quantity"] == 8



    r_add = await run_chat(session_id, "agrega marihuana premium")

    assert SESSIONS[session_id]["phase"] == "awaiting_quantity"

    assert "Agregando" in r_add.response or "marihuana" in r_add.response.lower()



    r_mj_qty = await run_chat(session_id, "10 gramos")

    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"

    cart = SESSIONS[session_id]["cart"]

    assert len(cart) == 2

    codes = {item["productCode"] for item in cart}

    assert codes == {"PLZ-LSD-001", "PLZ-MJ-001"}



    summary = SESSIONS[session_id]["operation_summary"]

    assert summary is not None

    assert len(summary["lineItems"]) == 2

    assert summary["total"] > summary["subtotal"]

    assert "Resumen del carrito" in r_mj_qty.response

    assert "Agregar otro producto" in (r_mj_qty.chips or [])





@pytest.mark.asyncio

async def test_single_confirm_creates_one_order_with_two_lines(mock_catalog):

    session_id = "cart-confirm"

    await run_chat(session_id, "quiero comprar lsd")

    await run_chat(session_id, "8 unidades")

    await run_chat(session_id, "agrega marihuana premium")

    await run_chat(session_id, "10 gramos")



    result = await run_chat(session_id, "Confirmar compra")

    assert result.state == "awaiting_delivery_address"

    result = await run_chat(session_id, "Calle 45 #12-30, Bogotá")

    assert result.state == "awaiting_save_address"

    result = await run_chat(session_id, "no")

    assert result.state == "sale_completed"

    assert result.invoice_number == "INV-MULTI"

    assert len(mock_catalog["sales"]) == 1

    sale_payload = mock_catalog["sales"][0]

    assert len(sale_payload["line_items"]) == 2

    product_codes = {item["product_code"] for item in sale_payload["line_items"]}

    assert product_codes == {"PLZ-LSD-001", "PLZ-MJ-001"}

    assert sale_payload["delivery_address"] == "Calle 45 #12-30"

    assert sale_payload["delivery_city"] == "Bogotá"





def test_parse_multi_item_order_user_example():

    message = (

        "quiero 3 unidades de cocaina y 5 unidades de marihuana blue "

        "y 3 unidades de ketamina liquida"

    )

    items = _parse_multi_item_order(message)

    assert len(items) == 3

    assert items[0] == {"quantity": 3.0, "unit": "unit", "product_query": "cocaina"}

    assert items[1] == {

        "quantity": 5.0,

        "unit": "unit",

        "product_query": "marihuana blue dream",

    }

    assert items[2] == {

        "quantity": 3.0,

        "unit": "unit",

        "product_query": "ketamina liquida",

    }





def test_parse_multi_item_order_comma_and_grams():

    items = _parse_multi_item_order("dame 2 gramos de cocaina, 1 unidad de lsd")

    assert len(items) == 2

    assert items[0]["quantity"] == 2.0

    assert items[0]["unit"] == "gram"

    assert items[0]["product_query"] == "cocaina"

    assert items[1]["quantity"] == 1.0

    assert items[1]["unit"] == "unit"

    assert items[1]["product_query"] == "lsd"





@pytest.mark.asyncio

async def test_multi_item_order_in_one_message_builds_cart(mock_catalog):

    session_id = "multi-order"

    message = (

        "quiero 3 unidades de cocaina y 5 unidades de marihuana blue "

        "y 3 unidades de ketamina liquida"

    )

    result = await run_chat(session_id, message)



    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"

    cart = SESSIONS[session_id]["cart"]

    assert len(cart) == 3

    by_code = {item["productCode"]: item for item in cart}

    assert by_code["PLZ-COC-099"]["quantity"] == 3

    assert by_code["PLZ-MJ-003"]["quantity"] == 5

    assert by_code["PLZ-KET-021"]["quantity"] == 3

    assert "Resumen del carrito" in result.response

    assert "Confirmar compra" in (result.chips or [])

    assert "varios productos" not in result.response.lower()





@pytest.mark.asyncio

async def test_multi_item_order_from_awaiting_product_search_phase(mock_catalog):

    """Multi-item message must work when session was left in product-search phase."""

    session_id = "multi-from-search"

    SESSIONS[session_id] = chat_graph._session(session_id)

    SESSIONS[session_id]["phase"] = "awaiting_product_search"

    SESSIONS[session_id]["awaiting_product_search"] = True

    message = (

        "quiero 3 unidades de cocaina y 5 unidades de marihuana blue "

        "y 3 unidades de ketamina liquida"

    )

    result = await run_chat(session_id, message)

    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"

    cart = SESSIONS[session_id]["cart"]

    assert len(cart) == 3

    by_code = {item["productCode"]: item for item in cart}

    assert by_code["PLZ-COC-099"]["quantity"] == 3

    assert by_code["PLZ-MJ-003"]["quantity"] == 5

    assert by_code["PLZ-KET-021"]["quantity"] == 3

    assert "Resumen del carrito" in result.response

    assert "Confirmar compra" in (result.chips or [])





@pytest.mark.asyncio

async def test_multi_item_ambiguous_disambiguates_one_preserves_queue(mock_catalog):

    CATALOG["PLZ-LSD-002"] = {

        "code": "PLZ-LSD-002",

        "name": "LSD Blotter Art Edition",

        "price": 28000,

        "stock": 10,

        "saleUnit": "unit",

        "allowsFractional": False,

    }

    session_id = "multi-ambig"

    message = "quiero 2 unidades de lsd y 3 unidades de marihuana blue"

    result = await run_chat(session_id, message)



    assert SESSIONS[session_id]["phase"] == "awaiting_product_search"

    assert len(SESSIONS[session_id]["pending_order_queue"]) == 2

    assert SESSIONS[session_id]["pending_order_queue"][0]["product_query"] == "lsd"

    assert SESSIONS[session_id]["pending_order_queue"][0]["quantity"] == 2

    assert SESSIONS[session_id]["pending_order_queue"][1]["product_query"] == "marihuana blue dream"

    assert "varios productos" in result.response.lower() or "PLZ-LSD" in result.response



    result2 = await run_chat(session_id, "PLZ-LSD-001")

    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"

    cart = SESSIONS[session_id]["cart"]

    assert len(cart) == 2

    codes = {item["productCode"] for item in cart}

    assert codes == {"PLZ-LSD-001", "PLZ-MJ-003"}





@pytest.mark.asyncio

async def test_multi_item_out_of_stock_warns_and_continues(mock_catalog):

    CATALOG["PLZ-LSD-042"] = {

        "code": "PLZ-LSD-042",

        "name": "LSD Microdosis",

        "price": 18000,

        "stock": 0,

        "saleUnit": "unit",

        "allowsFractional": False,

    }

    session_id = "multi-oos"

    message = "quiero 2 unidades de lsd y 3 unidades de marihuana blue"

    result = await run_chat(session_id, message)



    assert SESSIONS[session_id]["phase"] == "awaiting_product_search"

    assert "varios productos" in result.response.lower() or "PLZ-LSD" in result.response



    result2 = await run_chat(session_id, "PLZ-LSD-042")

    assert "no tiene stock" in result2.response.lower()

    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"

    assert len(SESSIONS[session_id]["cart"]) == 1

    assert SESSIONS[session_id]["cart"][0]["productCode"] == "PLZ-MJ-003"

    assert SESSIONS[session_id]["cart"][0]["quantity"] == 3


@pytest.mark.asyncio
async def test_cocaina_alias_picks_perlada_not_crack(mock_catalog):
    """Bare «cocaina» must resolve to PLZ-COC-099, not crack PLZ-COC-100."""
    session_id = "cocaine-alias"
    result = await run_chat(session_id, "quiero cocaina")

    assert SESSIONS[session_id]["phase"] == "awaiting_quantity"
    assert SESSIONS[session_id]["product_code"] == "PLZ-COC-099"
    assert "Crack" not in result.response


@pytest.mark.asyncio
async def test_crack_alias_picks_crack_sku(mock_catalog):
    session_id = "crack-alias"
    result = await run_chat(session_id, "quiero 2 gramos de crack")

    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"
    assert SESSIONS[session_id]["cart"][0]["productCode"] == "PLZ-COC-100"
    assert "Crack" in result.response or "PLZ-COC-100" in result.response


@pytest.mark.asyncio
async def test_cocaine_grams_then_ketamine_summary_has_two_line_items(mock_catalog):
    """4g cocaine + 3 ketamine must expose both lines in operation_summary."""
    session_id = "coc-ket-summary"
    await run_chat(session_id, "quiero 4 gramos de cocaina")
    result = await run_chat(session_id, "y agrega 3 unidades de ketamina")

    summary = SESSIONS[session_id]["operation_summary"]
    assert summary is not None
    assert len(summary["lineItems"]) == 2

    codes = {item["productCode"] for item in summary["lineItems"]}
    assert codes == {"PLZ-COC-099", "PLZ-KET-021"}

    by_code = {item["productCode"]: item for item in summary["lineItems"]}
    assert by_code["PLZ-COC-099"]["quantity"] == 4
    assert by_code["PLZ-COC-099"]["subtotal"] == 720000
    assert by_code["PLZ-KET-021"]["quantity"] == 3
    assert by_code["PLZ-KET-021"]["subtotal"] == 540000

    assert result.operation_summary is not None
    assert len(result.operation_summary.line_items) == 2


@pytest.mark.asyncio
async def test_add_ketamina_during_confirmation_with_quantity(mock_catalog):
    """«y agrega 3 unidades de ketamina» must append to cart and re-show summary."""
    session_id = "confirm-add-keta"
    await run_chat(
        session_id,
        "quiero 3 unidades de cocaina y 5 unidades de marihuana blue",
    )

    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"
    assert len(SESSIONS[session_id]["cart"]) == 2

    result = await run_chat(session_id, "y agrega 3 unidades de ketamina")

    assert SESSIONS[session_id]["phase"] == "awaiting_confirmation"
    cart = SESSIONS[session_id]["cart"]
    assert len(cart) == 3
    by_code = {item["productCode"]: item for item in cart}
    assert by_code["PLZ-KET-021"]["quantity"] == 3
    assert "Resumen del carrito" in result.response
    assert "Confirmar compra" in (result.chips or [])

