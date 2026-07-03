import pytest

from unittest.mock import patch

from app.graph import chat_graph

from app.graph.chat_graph import SESSIONS, run_chat



CATALOG = {

    "PLZ-HNG-034": {"code": "PLZ-HNG-034", "name": "Trufas", "price": 120000, "stock": 25},

    "PLZ-COC-099": {"code": "PLZ-COC-099", "name": "Cocaína Perlada", "price": 85000, "stock": 40},

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
            if not s or s in _strip_accents(p["name"].upper()) or s in p["code"]
        ]
        return matches[:page_size]

    async def search_paged(q, page_size=5, page=1):
        items = await search(q, page_size=page_size)
        return items, len(items)

    async def stock(code):

        p = await get_code(code)

        if not p: raise ValueError(code)

        return {**p, "status": "ok"}

    async def sale(*a, **k):

        return {"orderNumber": "O1", "invoiceNumber": "INV-1"}

    with (

        patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),

        patch.object(chat_graph.dotnet_tools, "search_products", side_effect=search),

        patch.object(chat_graph.dotnet_tools, "search_products_paged", side_effect=search_paged),

        patch.object(chat_graph.dotnet_tools, "check_stock", side_effect=stock),

        patch.object(chat_graph.dotnet_tools, "create_sale", side_effect=sale),

    ):

        SESSIONS.clear(); yield; SESSIONS.clear()



@pytest.mark.asyncio

async def test_stock_unknown_sku():

    r = await run_chat("a", "consultar stock de TEST-001")

    assert "TEST-001" in r.response



@pytest.mark.asyncio

async def test_stock_known_sku():

    r = await run_chat("b", "consultar stock de PLZ-HNG-034")

    assert "Stock de" in r.response



@pytest.mark.asyncio

async def test_comprar_with_sku_and_qty():

    r = await run_chat("c", "quiero comprar PLZ-COC-099 2")

    assert SESSIONS["c"]["product_code"] == "PLZ-COC-099"



@pytest.mark.asyncio

async def test_hola_welcome():

    r = await run_chat("d", "hola")

    assert "Drogui" in r.response

    assert "Consultar stock" in " ".join(r.chips or [])



@pytest.mark.asyncio

async def test_plain_comprar_rules():

    r = await run_chat("e", "quiero comprar cocaina")

    assert SESSIONS["e"]["product_code"] == "PLZ-COC-099"

