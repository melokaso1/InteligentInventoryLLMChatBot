import re
from typing import Any

UNIT_ALIASES: dict[str, str] = {
    "unit": "unit",
    "units": "unit",
    "unidad": "unit",
    "unidades": "unit",
    "u": "unit",
    "gram": "gram",
    "grams": "gram",
    "gramo": "gram",
    "gramos": "gram",
    "g": "gram",
    "kilogram": "kilogram",
    "kilograms": "kilogram",
    "kilogramo": "kilogram",
    "kilogramos": "kilogram",
    "kilo": "kilogram",
    "kilos": "kilogram",
    "kg": "kilogram",
    "milligram": "milligram",
    "milligrams": "milligram",
    "miligramo": "milligram",
    "miligramos": "milligram",
    "mg": "milligram",
    "milliliter": "milliliter",
    "milliliters": "milliliter",
    "mililitro": "milliliter",
    "mililitros": "milliliter",
    "ml": "milliliter",
    "liter": "liter",
    "liters": "liter",
    "litro": "liter",
    "litros": "liter",
    "l": "liter",
}

WEIGHT_UNITS = frozenset({"gram", "kilogram", "milligram"})
VOLUME_UNITS = frozenset({"milliliter", "liter"})

UNIT_LABELS: dict[str, tuple[str, str]] = {
    "unit": ("unidad", "unidades"),
    "gram": ("gramo", "gramos"),
    "kilogram": ("kilogramo", "kilogramos"),
    "milligram": ("miligramo", "miligramos"),
    "milliliter": ("mililitro", "mililitros"),
    "liter": ("litro", "litros"),
}

UNIT_SHORT: dict[str, str] = {
    "unit": "u.",
    "gram": "g",
    "kilogram": "kg",
    "milligram": "mg",
    "milliliter": "ml",
    "liter": "L",
}

MEASURE_UNITS_PATTERN = (
    r"(?:"
    r"kilogramos|kilos|kg|"
    r"gramos|gramo|g(?!\w)|"
    r"miligramos|mg|"
    r"mililitros|ml|"
    r"litros|litro|l(?!\w)|"
    r"unidades|unidad|u(?!\w)"
    r")"
)

_QUANTITY_WITH_UNIT_RE = re.compile(
    rf"(?P<qty>\d+(?:[.,]\d+)?)\s*(?P<unit>{MEASURE_UNITS_PATTERN})?",
    re.IGNORECASE,
)

_QUANTITY_VERB_PREFIX_RE = re.compile(
    r"^(?:yo\s+)?(?:dame|quiero|necesito)\s+",
    re.IGNORECASE,
)


def normalize_unit(value: str | None, default: str = "unit") -> str:
    if not value:
        return default
    return UNIT_ALIASES.get(value.strip().lower(), default)


def unit_label(unit: str, plural: bool = False) -> str:
    labels = UNIT_LABELS.get(normalize_unit(unit), ("unidad", "unidades"))
    return labels[1] if plural else labels[0]


def unit_short(unit: str) -> str:
    return UNIT_SHORT.get(normalize_unit(unit), "u.")


def allows_fractional(unit: str) -> bool:
    return normalize_unit(unit) != "unit"


def is_compatible(requested: str, product_unit: str) -> bool:
    requested_norm = normalize_unit(requested)
    product_norm = normalize_unit(product_unit)
    if requested_norm == product_norm:
        return True
    if product_norm in WEIGHT_UNITS:
        return requested_norm in WEIGHT_UNITS
    if product_norm in VOLUME_UNITS:
        return requested_norm in VOLUME_UNITS
    return product_norm == "unit" and requested_norm == "unit"


def convert_quantity(quantity: float, from_unit: str, to_unit: str) -> float:
    from_norm = normalize_unit(from_unit)
    to_norm = normalize_unit(to_unit)
    if from_norm == to_norm:
        return quantity
    if not is_compatible(from_norm, to_norm):
        raise ValueError(f"No se puede convertir de {from_norm} a {to_norm}")

    if from_norm in WEIGHT_UNITS or to_norm in WEIGHT_UNITS:
        grams = quantity
        if from_norm == "kilogram":
            grams = quantity * 1000
        elif from_norm == "milligram":
            grams = quantity / 1000
        if to_norm == "gram":
            return grams
        if to_norm == "kilogram":
            return grams / 1000
        if to_norm == "milligram":
            return grams * 1000

    if from_norm in VOLUME_UNITS or to_norm in VOLUME_UNITS:
        milliliters = quantity
        if from_norm == "liter":
            milliliters = quantity * 1000
        if to_norm == "milliliter":
            return milliliters
        if to_norm == "liter":
            return milliliters / 1000

    return quantity


def resolve_sale_quantity(
    quantity: float,
    measure_unit: str | None,
    product_sale_unit: str,
) -> tuple[float, str]:
    if quantity <= 0:
        raise ValueError("La cantidad debe ser mayor que cero")
    requested = normalize_unit(measure_unit, product_sale_unit)
    product_unit = normalize_unit(product_sale_unit)
    if not is_compatible(requested, product_unit):
        raise ValueError(
            f"Este producto se vende por {unit_label(product_unit, plural=True)}"
        )
    normalized = round(convert_quantity(quantity, requested, product_unit), 4)
    if not allows_fractional(product_unit) and normalized != int(normalized):
        raise ValueError(
            f"Este producto solo se vende en {unit_label(product_unit, plural=True)} enteras"
        )
    return normalized, product_unit


def extract_quantity_with_unit(message: str) -> tuple[float | None, str | None]:
    text = message.strip().lower()
    match = _QUANTITY_WITH_UNIT_RE.search(text)
    if not match:
        return None, None
    qty_raw = match.group("qty").replace(",", ".")
    qty = float(qty_raw)
    unit_raw = match.group("unit")
    unit = normalize_unit(unit_raw) if unit_raw else None
    return (qty if qty > 0 else None), unit


def is_quantity_reply(message: str) -> bool:
    """True when the message is only a quantity (optionally prefixed by quiero/dame/necesito)."""
    text = message.strip().lower()
    if not text:
        return False
    text = _QUANTITY_VERB_PREFIX_RE.sub("", text).strip()
    if not text:
        return False
    match = _QUANTITY_WITH_UNIT_RE.match(text)
    if not match:
        return False
    qty_raw = match.group("qty").replace(",", ".")
    try:
        qty = float(qty_raw)
    except ValueError:
        return False
    if qty <= 0:
        return False
    remainder = text[match.end() :].strip(" .,!?")
    return remainder == ""


def format_stock(product: dict[str, Any]) -> str:
    stock = float(product.get("stock", 0) or 0)
    sale_unit = normalize_unit(str(product.get("saleUnit") or product.get("sale_unit") or "unit"))
    label = unit_label(sale_unit, plural=stock != 1)
    return f"{stock:g} {label}"
