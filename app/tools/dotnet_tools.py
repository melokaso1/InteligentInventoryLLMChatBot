import os
from typing import Any

import httpx

from app.utils.json_normalize import pick, pick_list, normalize_product_code
from app.utils.measure_units import (
    extract_quantity_with_unit,
    format_stock,
    normalize_unit,
    resolve_sale_quantity,
    unit_label,
    unit_short,
)

DOTNET_API_URL = os.getenv("DOTNET_API_URL", "http://localhost:5151").rstrip("/")
CHATBOT_API_KEY = os.getenv("CHATBOT_API_KEY", "elplonsazo-chatbot-dev-key")
CHATBOT_API_KEY_HEADER = "X-Chatbot-Api-Key"
TAX_RATE = 0.19


class CatalogError(Exception):
    """Raised when the .NET catalog API is unavailable or rejects the service key."""


CATALOG_CONNECTION_ERROR = (
    "No se pudo conectar con la API (.NET). "
    "Verifica que el backend esté en ejecución en el puerto 5151 "
    "(cd Backend/Api && dotnet run)."
)


def _service_headers() -> dict[str, str]:
    return {CHATBOT_API_KEY_HEADER: CHATBOT_API_KEY}


def _raise_catalog_error(exc: httpx.HTTPStatusError) -> None:
    if exc.response.status_code in (401, 403):
        raise CatalogError(
            "No pude consultar el catálogo (autenticación del servicio). "
            "Verifica CHATBOT_API_KEY y Chatbot:ApiKey en la API .NET."
        ) from exc
    raise CatalogError("No pude consultar el catálogo en este momento. Intenta de nuevo más tarde.") from exc


def _raise_catalog_connection_error(exc: httpx.RequestError) -> None:
    raise CatalogError(CATALOG_CONNECTION_ERROR) from exc


async def search_products(query: str, *, page_size: int = 5) -> list[dict[str, Any]]:
    items, _ = await search_products_paged(query, page_size=page_size)
    return items


async def search_products_paged(
    query: str,
    *,
    page: int = 1,
    page_size: int = 5,
) -> tuple[list[dict[str, Any]], int]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(
                f"{DOTNET_API_URL}/api/chatbot/products",
                params={"q": query, "page": page, "pageSize": page_size},
                headers=_service_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_catalog_error(exc)
        except httpx.RequestError as exc:
            _raise_catalog_connection_error(exc)
        payload = response.json()
        items = pick_list(payload, "items", "Items")
        total = int(pick(payload, "totalCount", "TotalCount", default=len(items)))
        return items, total


async def get_product_by_code(code: str) -> dict[str, Any] | None:
    normalized = normalize_product_code(code)
    products = await search_products(normalized)
    for product in products:
        if str(pick(product, "code", "Code", default="")).upper() == normalized:
            return product
    return None


async def check_stock(product_code: str) -> dict[str, Any]:
    product = await get_product_by_code(product_code)
    if not product:
        raise ValueError(f"No encontré el producto {product_code} en el catálogo El Plonsazo.")
    return {
        "code": pick(product, "code", "Code"),
        "name": pick(product, "name", "Name"),
        "stock": float(pick(product, "stock", "Stock", default=0)),
        "price": float(pick(product, "price", "Price", default=0)),
        "status": pick(product, "status", "Status"),
        "saleUnit": normalize_unit(
            str(pick(product, "saleUnit", "SaleUnit", default="unit"))
        ),
        "allowsFractional": bool(
            pick(product, "allowsFractional", "AllowsFractional", default=False)
        ),
    }


async def create_sale(
    customer_name: str,
    customer_email: str,
    line_items: list[dict[str, Any]],
    session_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "lineItems": [
            {
                "productCode": normalize_product_code(item["product_code"]),
                "quantity": item["quantity"],
                **(
                    {"measureUnit": normalize_unit(item["measure_unit"])}
                    if item.get("measure_unit")
                    else {}
                ),
            }
            for item in line_items
        ],
        "customerName": customer_name,
        "customerEmail": customer_email,
    }
    if session_id:
        payload["sessionId"] = session_id
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            response = await client.post(
                f"{DOTNET_API_URL}/api/sales/from-chatbot",
                json=payload,
                headers=_service_headers(),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ValueError(
                    "No pude registrar la venta (autenticación del servicio). "
                    "Verifica CHATBOT_API_KEY y Chatbot:ApiKey en la API .NET."
                ) from exc
            raise ValueError(exc.response.text) from exc

        if response.status_code >= 400:
            detail = response.text
            try:
                detail = response.json().get("title") or response.json().get("detail") or detail
            except Exception:
                pass
            raise ValueError(str(detail))
        return response.json()


async def get_invoice(invoice_number: str) -> dict[str, Any] | None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{DOTNET_API_URL}/api/invoices", params={"q": invoice_number})
        if response.status_code >= 400:
            return None
        items = pick_list(response.json(), "items", "Items")
        target = invoice_number.strip().upper()
        for invoice in items:
            number = str(pick(invoice, "invoiceNumber", "InvoiceNumber", default="")).upper()
            if number == target:
                return invoice
        return items[0] if items else None
