import pytest

from unittest.mock import patch

from app.graph import chat_graph

from app.graph.chat_graph import COMMUNICATION_GUIDE, MENU_CHIPS, SESSIONS, _normalize_user_message, run_chat



CATALOG = {

    "PLZ-COC-099": {"code": "PLZ-COC-099", "name": "Cocaína Perlada", "price": 85000, "stock": 40},

    "PLZ-LSD-001": {"code": "PLZ-LSD-001", "name": "LSD Ácido", "price": 5000, "stock": 100},

}



@pytest.fixture(autouse=True)

def mocks():

    async def get_code(c):

        return CATALOG.get(c.strip().upper())

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

    async def stock(c):

        p = await get_code(c)

        if not p: raise ValueError(c)

        return {**p, "status": "ok"}

    with (

        patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),

        patch.object(chat_graph.dotnet_tools, "search_products", side_effect=search),

        patch.object(chat_graph.dotnet_tools, "search_products_paged", side_effect=search_paged),

        patch.object(chat_graph.dotnet_tools, "check_stock", side_effect=stock),

    ):

        SESSIONS.clear(); yield; SESSIONS.clear()



def test_slash_ofertas_normalized_to_natural():

    assert _normalize_user_message("/ofertas") == "ver ofertas"



def test_slash_ayuda_normalized():

    assert _normalize_user_message("/ayuda") == "ayuda"



def test_slash_buscar_normalized():

    assert _normalize_user_message("/buscar cocaina") == "cocaina"



@pytest.mark.asyncio

async def test_hola_rules_welcome():

    r = await run_chat("x", "hola")

    assert "Drogui" in r.response

    assert "Consultar stock" in (r.chips or [])



@pytest.mark.asyncio

async def test_plain_comprar_rules():

    r = await run_chat("y", "quiero comprar cocaina")

    assert SESSIONS["y"]["product_code"] == "PLZ-COC-099"

    assert "No encontré productos" not in r.response



@pytest.mark.asyncio

async def test_legacy_slash_ayuda():

    r = await run_chat("ay1", "/ayuda")

    assert r.response == COMMUNICATION_GUIDE

    assert "Consultar stock" in (r.chips or [])



@pytest.mark.asyncio

async def test_legacy_slash_buscar_still_works():

    r = await run_chat("z", "/buscar cocaina")

    assert "Cocaína" in r.response or "PLZ-COC" in r.response



@pytest.mark.asyncio

async def test_ayuda_natural():

    r = await run_chat("w", "¿Cómo me comunico?")

    assert r.response == COMMUNICATION_GUIDE



@pytest.mark.asyncio

async def test_fallback_hola():

    r = await run_chat("v", "hola")

    assert "Drogui" in r.response

    assert "Consultar stock" in (r.chips or [])





@pytest.mark.asyncio

async def test_ver_ofertas_natural():

    r = await run_chat("of", "ver ofertas")

    assert "ofertas" in r.response.lower() or r.offers

    assert "asistente de El Plonsazo" not in r.response or "Drogui" in r.response

    assert "¿En qué te puedo ayudar?" not in r.response





@pytest.mark.asyncio

async def test_legacy_slash_ofertas():

    r = await run_chat("of2", "/ofertas")

    assert "ofertas" in r.response.lower() or r.offers





@pytest.mark.asyncio

async def test_consultar_stock_prompt():

    r = await run_chat("st", "consultar stock")

    assert "stock" in r.response.lower()

    assert "Consultar stock" in MENU_CHIPS or "PLZ-MJ-001" in (r.chips or [])





@pytest.mark.asyncio

async def test_chip_consultar_stock():

    r = await run_chat("cs", "Consultar stock")

    assert "nombre" in r.response.lower() or "SKU" in r.response

    assert SESSIONS["cs"]["phase"] == "awaiting_stock_sku"





@pytest.mark.asyncio

async def test_chip_ver_ofertas():

    r = await run_chat("vo", "Ver ofertas")

    assert "ofertas" in r.response.lower() or r.offers

    assert "¿En qué te puedo ayudar?" not in r.response





@pytest.mark.asyncio

async def test_ver_ofertas_returns_offers_array():

    r = await run_chat("offers", "ver ofertas")

    assert r.offers is not None

    assert len(r.offers) >= 1

    assert r.offers[0].product_code.startswith("PLZ-")

    assert r.offers_total_count is not None

    assert r.offers_total_count >= len(r.offers)





@pytest.mark.asyncio

async def test_ver_ofertas_catalog_unavailable():

    from app.tools.dotnet_tools import CatalogError, CATALOG_CONNECTION_ERROR

    async def fail_paged(*_args, **_kwargs):
        raise CatalogError(CATALOG_CONNECTION_ERROR)

    with patch.object(chat_graph.dotnet_tools, "search_products_paged", side_effect=fail_paged):
        r = await run_chat("down", "ver ofertas")

    assert "5151" in r.response

    assert r.offers is None





@pytest.mark.asyncio

async def test_lsd_natural_search():

    r = await run_chat("lsd", "lsd")

    assert "LSD" in r.response or "PLZ-LSD" in r.response

    assert "¿En qué te puedo ayudar?" not in r.response





@pytest.mark.asyncio

async def test_quiero_lsd_search():

    r = await run_chat("ql", "quiero lsd")

    assert "LSD" in r.response or "PLZ-LSD" in r.response





@pytest.mark.asyncio

async def test_quiero_comprar_algo_prompts_for_product():

    r = await run_chat("qa", "quiero comprar algo")

    assert "No encontré productos para «algo»" not in r.response

    assert "No encontré productos para «quiero comprar algo»" not in r.response

    assert "producto" in r.response.lower()

    assert SESSIONS["qa"]["phase"] == "awaiting_product_search"





@pytest.mark.asyncio

async def test_welcome_never_uses_legacy_text():

    r = await run_chat("legacy", "hola")

    assert "¿En qué te puedo ayudar?" not in r.response

    assert "realizar compras" not in r.response

    assert "Drogui" in r.response

