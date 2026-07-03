import pytest
from unittest.mock import patch

from app.graph import chat_graph
from app.graph.chat_graph import SESSIONS, _extract_purchase_details, run_chat

CATALOG = {
    "PLZ-MJ-012": {
        "code": "PLZ-MJ-012",
        "name": "Aceite CBD 10%",
        "price": 180_000,
        "stock": 38,
        "saleUnit": "unit",
        "allowsFractional": False,
    },
    "PLZ-EXT-056": {
        "code": "PLZ-EXT-056",
        "name": "Éxtasis Tesla 300mg",
        "price": 60_000,
        "stock": 120,
        "saleUnit": "unit",
        "allowsFractional": False,
    },
    "PLZ-COC-099": {
        "code": "PLZ-COC-099",
        "name": "Cocaína Perlada — Polvo",
        "price": 85_000,
        "stock": 5000,
        "saleUnit": "gram",
        "allowsFractional": True,
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

    with (
        patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),
        patch.object(chat_graph.dotnet_tools, "search_products", side_effect=search),
        patch.object(chat_graph.dotnet_tools, "search_products_paged", side_effect=search_paged),
    ):
        SESSIONS.clear()
        yield
        SESSIONS.clear()


def test_extract_purchase_details_spanish_unit_word():
    qty, unit, product = _extract_purchase_details("quiero comprar una unidad de aceite CBD")
    assert qty == 1
    assert unit == "unit"
    assert product == "aceite cbd"


def test_extract_purchase_details_pastillas():
    qty, unit, product = _extract_purchase_details("4 pastillas de extasis")
    assert qty == 4
    assert unit == "unit"
    assert product == "extasis"


def test_extract_purchase_details_gramos():
    qty, unit, product = _extract_purchase_details("quiero 2 gramos de cocaina")
    assert qty == 2
    assert unit == "gram"
    assert product == "cocaina"


@pytest.mark.asyncio
async def test_complete_request_aceite_cbd_goes_to_confirmation():
    result = await run_chat("cbd-complete", "quiero comprar una unidad de aceite CBD")

    session = SESSIONS["cbd-complete"]
    assert session["phase"] == "awaiting_confirmation"
    assert session["product_code"] == "PLZ-MJ-012"
    assert session["quantity"] == 1
    assert "cuantos" not in result.response.lower()
    assert "cuántos" not in result.response.lower()


@pytest.mark.asyncio
async def test_complete_request_pastillas_extasis_goes_to_confirmation():
    result = await run_chat("ext-complete", "4 pastillas de extasis")

    session = SESSIONS["ext-complete"]
    assert session["phase"] == "awaiting_confirmation"
    assert session["product_code"] == "PLZ-EXT-056"
    assert session["quantity"] == 4
    assert "cuantos" not in result.response.lower()
    assert "cuántos" not in result.response.lower()


@pytest.mark.asyncio
async def test_complete_request_gramos_cocaina_goes_to_confirmation():
    result = await run_chat("coc-complete", "quiero 2 gramos de cocaina")

    session = SESSIONS["coc-complete"]
    assert session["phase"] == "awaiting_confirmation"
    assert session["product_code"] == "PLZ-COC-099"
    assert session["quantity"] == 2
    assert session["measure_unit"] == "gram"
    assert "cuantos" not in result.response.lower()
    assert "cuántos" not in result.response.lower()
