import pytest

from app.utils.measure_units import (
    convert_quantity,
    extract_quantity_with_unit,
    is_quantity_reply,
    normalize_unit,
    resolve_sale_quantity,
)


@pytest.mark.parametrize(
    ("message", "qty", "unit"),
    [
        ("500 gramos", 500.0, "gram"),
        ("2 kilos", 2.0, "kilogram"),
        ("1.5 litros", 1.5, "liter"),
        ("3 unidades", 3.0, "unit"),
        ("una unidad", 1.0, "unit"),
        ("4 pastillas", 4.0, "unit"),
        ("250 mg", 250.0, "milligram"),
        ("100 ml", 100.0, "milliliter"),
    ],
)
def test_extract_quantity_with_unit(message: str, qty: float, unit: str) -> None:
    parsed_qty, parsed_unit = extract_quantity_with_unit(message)
    assert parsed_qty == qty
    assert parsed_unit == unit


def test_convert_kilos_to_grams() -> None:
    assert convert_quantity(2, "kilogram", "gram") == 2000


def test_resolve_sale_quantity_for_gram_product() -> None:
    normalized, unit = resolve_sale_quantity(2, "kilogram", "gram")
    assert normalized == 2000
    assert unit == "gram"


def test_resolve_sale_quantity_rejects_fractional_units() -> None:
    with pytest.raises(ValueError):
        resolve_sale_quantity(1.5, "unit", "unit")


@pytest.mark.parametrize(
    "message",
    [
        "15 gramos",
        "quiero 15 gramos",
        "una unidad",
        "500 g",
        "2 kilos",
        "100 ml",
    ],
)
def test_is_quantity_reply(message: str) -> None:
    assert is_quantity_reply(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "quiero 15 gramos de marihuana",
        "buscar marihuana",
        "PLZ-MJ-001",
    ],
)
def test_is_quantity_reply_rejects_product_queries(message: str) -> None:
    assert is_quantity_reply(message) is False
