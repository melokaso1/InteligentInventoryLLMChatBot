import pytest
from unittest.mock import patch

from app.graph import chat_graph
from app.graph.chat_graph import (
    SESSIONS,
    _extract_purchase_details,
    _extract_search_queries,
    _extract_search_terms,
    _resolve_product_alias,
    run_chat,
)

CATALOG = {
    "PLZ-COC-050": {
        "code": "PLZ-COC-050",
        "name": "Cocaína Sobre Unitario",
        "price": 50000,
        "stock": 10,
        "saleUnit": "unit",
        "allowsFractional": False,
    },
    "PLZ-COC-099": {
        "code": "PLZ-COC-099",
        "name": "Cocaína Perlada — Polvo",
        "price": 85000,
        "stock": 5000,
        "saleUnit": "gram",
        "allowsFractional": True,
    },
    "PLZ-COC-100": {
        "code": "PLZ-COC-100",
        "name": "Crack (Cocaína Base)",
        "price": 340000,
        "stock": 25,
        "saleUnit": "gram",
        "allowsFractional": True,
    },
    "PLZ-MJ-001": {
        "code": "PLZ-MJ-001",
        "name": "Marihuana Sativa Indoor Premium",
        "price": 45000,
        "stock": 200,
        "saleUnit": "gram",
    },
    "PLZ-BAZ-070": {
        "code": "PLZ-BAZ-070",
        "name": "Bazuco",
        "price": 85000,
        "stock": 100,
        "saleUnit": "gram",
    },
    "PLZ-TUS-015": {
        "code": "PLZ-TUS-015",
        "name": "Tussi Rosa 2C-B",
        "price": 140000,
        "stock": 80,
        "saleUnit": "gram",
    },
    "PLZ-HNG-033": {
        "code": "PLZ-HNG-033",
        "name": "Hongos Psilocybe Cubensis",
        "price": 112000,
        "stock": 60,
        "saleUnit": "gram",
    },
    "PLZ-POP-007": {
        "code": "PLZ-POP-007",
        "name": "Popper Rush XL (Amil)",
        "price": 76000,
        "stock": 100,
        "saleUnit": "unit",
    },
    "PLZ-LSD-042": {
        "code": "PLZ-LSD-042",
        "name": "LSD-25 Blotter 200µg",
        "price": 50000,
        "stock": 50,
        "saleUnit": "unit",
    },
    "PLZ-MDM-088": {
        "code": "PLZ-MDM-088",
        "name": "MDMA Cristal Europa",
        "price": 168000,
        "stock": 200,
        "saleUnit": "gram",
    },
}


@pytest.fixture(autouse=True)
def mock_catalog():
    async def get_code(code):
        return CATALOG.get(code.strip().upper())

    async def search(q, page_size=10):
        from app.graph.chat_graph import _strip_accents

        s = _strip_accents(q.strip().upper())
        matches = [
            p
            for p in CATALOG.values()
            if not s
            or s in _strip_accents(p["name"].upper())
            or s in p["code"].upper()
        ]
        matches.sort(key=lambda p: p["code"])
        return matches[:page_size]

    async def search_paged(q, page_size=50, page=1):
        items = await search(q, page_size=page_size)
        return items, len(items)

    with (
        patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),
        patch.object(chat_graph.dotnet_tools, "search_products", side_effect=search),
        patch.object(chat_graph.dotnet_tools, "search_products_paged", side_effect=search_paged),
    ):
        SESSIONS.clear()
        yield
        SESSIONS.clear()


@pytest.mark.parametrize(
    ("slang", "expected"),
    [
        ("perico", "cocaina"),
        ("perica", "cocaina"),
        ("drogaina", "cocaina"),
        ("blanca", "marihuana"),
        ("la blanca", "marihuana"),
        ("mota", "marihuana"),
        ("paco", "bazuco"),
        ("tusi", "tussi"),
        ("champis", "hongos"),
        ("rush", "poppers"),
        ("acido", "lsd"),
        ("molly", "mdma"),
        ("meth", "metanfetamina"),
        ("k2", "k2"),
        ("juice", "esteroides"),
        ("flakka", "flakka"),
        ("dxm", "dxm"),
        ("dextrometorfano", "dxm"),
        ("fentanilo", "fentanilo"),
        ("ghb", "ghb"),
        ("benzos", "benzodiacepinas"),
    ],
)
def test_resolve_product_alias_maps_slang(slang, expected):
    assert _resolve_product_alias(slang) == expected


def test_extract_search_terms_perico():
    queries = _extract_search_queries("quiero perico")
    assert "cocaina" in queries
    assert _extract_search_terms("perico") == "cocaina"


def test_extract_search_terms_drogaina():
    queries = _extract_search_queries("drogaina")
    assert "cocaina" in queries
    assert _extract_search_terms("drogaina") == "cocaina"


def test_extract_search_terms_blanca():
    assert _extract_search_terms("blanca") == "marihuana"
    assert _extract_search_terms("la blanca") == "marihuana"


def test_extract_purchase_details_quiero_perico():
    qty, unit, product = _extract_purchase_details("quiero perico")
    assert qty is None
    assert unit is None
    assert product == "cocaina"


@pytest.mark.asyncio
async def test_quiero_perico_starts_purchase_flow():
    result = await run_chat("slang-perico", "quiero perico")

    session = SESSIONS["slang-perico"]
    assert session["phase"] == "awaiting_quantity"
    assert session["product_code"] == "PLZ-COC-099"
    assert "No encontré productos" not in result.response
    assert "Cocaína" in result.response or "PLZ-COC" in result.response


@pytest.mark.asyncio
async def test_drogaina_finds_cocaine_products():
    result = await run_chat("slang-drogaina", "drogaina")

    assert "No encontré productos" not in result.response
    assert "Cocaína" in result.response or "PLZ-COC" in result.response


@pytest.mark.asyncio
async def test_blanca_finds_marijuana():
    result = await run_chat("slang-blanca", "blanca")

    assert "No encontré productos" not in result.response
    assert "Marihuana" in result.response or "PLZ-MJ" in result.response


@pytest.mark.asyncio
async def test_paco_finds_bazuco():
    result = await run_chat("slang-paco", "paco")

    assert "No encontré productos" not in result.response
    assert "Bazuco" in result.response or "PLZ-BAZ" in result.response


@pytest.mark.asyncio
async def test_mota_finds_marijuana():
    result = await run_chat("slang-mota", "mota")

    assert "No encontré productos" not in result.response
    assert "Marihuana" in result.response or "PLZ-MJ" in result.response
