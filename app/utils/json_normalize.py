from typing import Any


def pick(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Lee la primera clave presente (soporta camelCase y PascalCase)."""
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def pick_list(data: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    value = pick(data, *keys, default=[])
    return value if isinstance(value, list) else []


def normalize_product_code(code: str) -> str:
    return code.strip().upper()
