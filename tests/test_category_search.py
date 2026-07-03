import pytest
from unittest.mock import patch

from app.graph import chat_graph
from app.graph.chat_graph import (
    SESSIONS,
    _extract_search_queries,
    _extract_search_terms,
    _search_display_label,
    run_chat,
)

CATALOG = {
    "PLZ-LSD-045": {
        "code": "PLZ-LSD-045",
        "name": "LSD Líquido 100µg/ml",
        "price": 220_000,
        "stock": 600,
        "saleUnit": "milliliter",
    },
    "PLZ-MJ-012": {
        "code": "PLZ-MJ-012",
        "name": "Aceite CBD 10%",
        "price": 180_000,
        "stock": 38,
        "saleUnit": "unit",
    },
    "PLZ-MJ-001": {
        "code": "PLZ-MJ-001",
        "name": "Marihuana Premium",
        "price": 45_000,
        "stock": 100,
        "saleUnit": "gram",
    },
    "PLZ-LSD-001": {
        "code": "PLZ-LSD-001",
        "name": "LSD Ácido",
        "price": 5_000,
        "stock": 100,
        "saleUnit": "unit",
    },
}


@pytest.fixture(autouse=True)
def mocks():
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

    async def search_paged(q, page_size=50, page=1):
        items = await search(q, page_size=page_size)
        if not q.strip():
            return list(CATALOG.values())[:page_size], len(CATALOG)
        return items, len(items)

    with (
        patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),
        patch.object(chat_graph.dotnet_tools, "search_products", side_effect=search),
        patch.object(chat_graph.dotnet_tools, "search_products_paged", side_effect=search_paged),
    ):
        SESSIONS.clear()
        yield
        SESSIONS.clear()


def test_extract_liquid_category_queries():
    queries = _extract_search_queries("tienes algo liquido")
    assert "líquido" in queries
    assert "aceite" in queries


def test_extract_marijuana_category_queries():
    queries = _extract_search_queries("hay marihuana")
    assert queries == ["marihuana"]


def test_extract_search_terms_strips_conversational_noise():
    assert _extract_search_terms("tienes algo liquido") == "líquido"
    assert _extract_search_terms("lsd") == "lsd"
    assert _extract_search_terms("PLZ-MJ-001") == "PLZ-MJ-001"


def test_search_display_label_for_liquid_category():
    assert _search_display_label("tienes algo liquido") == "productos líquidos"


@pytest.mark.asyncio
async def test_tienes_algo_liquido_lists_liquid_products():
    result = await run_chat("liquid-1", "tienes algo liquido")
    assert "No encontré productos" not in result.response
    assert "PLZ-LSD-045" in result.response or "LSD Líquido" in result.response
    assert "Aceite CBD" in result.response or "PLZ-MJ-012" in result.response


@pytest.mark.asyncio
async def test_hay_marihuana_finds_marijuana():
    result = await run_chat("mj-1", "hay marihuana")
    assert "No encontré productos" not in result.response
    assert "Marihuana" in result.response or "PLZ-MJ-001" in result.response


@pytest.mark.asyncio
async def test_lsd_sku_search_still_works():
    result = await run_chat("lsd-1", "lsd")
    assert "No encontré productos" not in result.response
    assert "LSD" in result.response


@pytest.mark.asyncio
async def test_sku_code_search_still_works():
    result = await run_chat("sku-1", "PLZ-MJ-001")
    assert "No encontré productos" not in result.response
    assert "PLZ-MJ-001" in result.response
