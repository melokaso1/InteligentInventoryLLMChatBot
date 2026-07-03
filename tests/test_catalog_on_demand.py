import pytest
from unittest.mock import patch

from app.graph import chat_graph
from app.graph.chat_graph import CATALOG_LOAD_MORE_CHIP, MENU_CHIPS, SESSIONS, run_chat

CATALOG = {
    "PLZ-MJ-001": {
        "code": "PLZ-MJ-001",
        "name": "Marihuana Sativa",
        "price": 45000,
        "stock": 100,
        "saleUnit": "gram",
    },
    "PLZ-LSD-001": {
        "code": "PLZ-LSD-001",
        "name": "LSD Ácido",
        "price": 5000,
        "stock": 50,
        "saleUnit": "unit",
    },
}

LARGE_CATALOG = {
    f"PLZ-TEST-{index:03d}": {
        "code": f"PLZ-TEST-{index:03d}",
        "name": f"Producto {index}",
        "price": 1000 * index,
        "stock": 10,
        "saleUnit": "unit",
    }
    for index in range(1, 28)
}
LARGE_CATALOG["PLZ-DMT-012-COPY-TEST"] = {
    "code": "PLZ-DMT-012-COPY-TEST",
    "name": "Test Duplicate",
    "price": 999,
    "stock": 1,
    "saleUnit": "unit",
}


@pytest.fixture(autouse=True)
def mock_catalog():
    async def get_code(code):
        return CATALOG.get(code.strip().upper())

    async def search(q, page_size=5):
        return list(CATALOG.values())[:page_size]

    async def search_paged(q, page_size=5, page=1):
        items = list(CATALOG.values())[:page_size]
        return items, len(CATALOG)

    with (
        patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),
        patch.object(chat_graph.dotnet_tools, "search_products", side_effect=search),
        patch.object(chat_graph.dotnet_tools, "search_products_paged", side_effect=search_paged),
    ):
        SESSIONS.clear()
        yield
        SESSIONS.clear()


@pytest.fixture
def mock_large_catalog():
    async def get_code(code):
        return LARGE_CATALOG.get(code.strip().upper())

    async def search_paged(q, page_size=5, page=1):
        items = list(LARGE_CATALOG.values())
        start = (page - 1) * page_size
        end = start + page_size
        return items[start:end], len(LARGE_CATALOG)

    with (
        patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),
        patch.object(
            chat_graph.dotnet_tools,
            "search_products_paged",
            side_effect=search_paged,
        ),
    ):
        SESSIONS.clear()
        yield
        SESSIONS.clear()


@pytest.mark.asyncio
async def test_idle_greeting_does_not_include_offers():
    result = await run_chat("welcome", "hola")

    assert result.offers is None
    assert result.offers_total_count is None
    assert "catálogo" in result.response.lower() or "catalogo" in result.response.lower()
    assert "Ver catálogo" in (result.chips or [])
    assert "Ver factura" in (result.chips or [])


@pytest.mark.asyncio
async def test_menu_chips_include_catalog_and_invoice():
    assert "Ver catálogo" in MENU_CHIPS
    assert "Ver factura" in MENU_CHIPS
    assert MENU_CHIPS.index("Ver catálogo") < MENU_CHIPS.index("Ver factura")


@pytest.mark.asyncio
async def test_ver_factura_chip_returns_invoice_guidance():
    result = await run_chat("invoice-chip", "Ver factura")

    assert "factura" in result.response.lower()
    assert "Mis facturas" in result.response
    assert "Ver factura" in (result.chips or [])


@pytest.mark.asyncio
async def test_ver_factura_message_returns_invoice_guidance():
    result = await run_chat("invoice-msg", "mis facturas")

    assert "factura" in result.response.lower()
    assert result.offers is None


@pytest.mark.asyncio
async def test_ver_catalogo_returns_offers():
    result = await run_chat("catalog", "ver catálogo")

    assert result.offers is not None
    assert len(result.offers) >= 1
    assert result.offers_total_count is not None
    assert "catálogo" in result.response.lower() or "catalogo" in result.response.lower()


@pytest.mark.asyncio
async def test_ver_catalogo_chip_returns_offers():
    result = await run_chat("catalog-chip", "Ver catálogo")

    assert result.offers is not None
    assert len(result.offers) >= 1


@pytest.mark.asyncio
async def test_catalog_pagination_first_page(mock_large_catalog):
    result = await run_chat("catalog-page-1", "ver catálogo")

    assert result.offers is not None
    assert len(result.offers) == len(LARGE_CATALOG) - 1
    assert result.offers_total_count == len(LARGE_CATALOG)
    assert CATALOG_LOAD_MORE_CHIP not in (result.chips or [])


@pytest.mark.asyncio
async def test_catalog_load_more_appends_offers(mock_large_catalog):
    session_id = "catalog-page-2"
    first = await run_chat(session_id, "ver catálogo")
    second = await run_chat(session_id, CATALOG_LOAD_MORE_CHIP)

    assert first.offers is not None
    assert second.offers is not None
    assert len(first.offers) == len(LARGE_CATALOG) - 1
    assert len(second.offers) == len(first.offers)


@pytest.mark.asyncio
async def test_catalog_filters_copy_test_products(mock_large_catalog):
    result = await run_chat("catalog-filter", "ver todo el catálogo")

    assert result.offers is not None
    codes = [offer.product_code for offer in result.offers]
    assert "PLZ-DMT-012-COPY-TEST" not in codes
    assert len(result.offers) == len(LARGE_CATALOG) - 1
