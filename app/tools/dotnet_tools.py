import json
import os
import time
from typing import Any

import httpx

from app.utils.json_normalize import pick, pick_list, normalize_product_code

DOTNET_API_URL = os.getenv("DOTNET_API_URL", "http://localhost:5151").rstrip("/")
CHATBOT_API_KEY = os.getenv("CHATBOT_API_KEY", "elplonsazo-chatbot-dev-key")
CHATBOT_API_KEY_HEADER = "X-Chatbot-Api-Key"
TAX_RATE = 0.08
_DEBUG_LOG = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "debug-cb4b87.log"
)


class CatalogError(Exception):
    """Raised when the .NET catalog API is unavailable or rejects the service key."""


def _service_headers() -> dict[str, str]:
    return {CHATBOT_API_KEY_HEADER: CHATBOT_API_KEY}


def _dbg(location: str, message: str, data: dict[str, Any], hypothesis_id: str) -> None:
    # #region agent log
    try:
        entry = {
            "sessionId": "cb4b87",
            "timestamp": int(time.time() * 1000),
            "location": location,
            "message": message,
            "data": data,
            "hypothesisId": hypothesis_id,
            "runId": "post-fix",
        }
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
    # #endregion


def _raise_catalog_error(exc: httpx.HTTPStatusError) -> None:
    if exc.response.status_code in (401, 403):
        raise CatalogError(
            "No pude consultar el catálogo (autenticación del servicio). "
            "Verifica CHATBOT_API_KEY y Chatbot:ApiKey en la API .NET."
        ) from exc
    raise CatalogError("No pude consultar el catálogo en este momento. Intenta de nuevo más tarde.") from exc


async def search_products(query: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(
                f"{DOTNET_API_URL}/api/chatbot/products",
                params={"q": query, "pageSize": 5},
                headers=_service_headers(),
            )
            # #region agent log
            _dbg(
                "dotnet_tools.py:search_products",
                "chatbot products API response",
                {
                    "query": query,
                    "statusCode": response.status_code,
                    "hasServiceKey": bool(CHATBOT_API_KEY),
                },
                "H1",
            )
            # #endregion
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # #region agent log
            _dbg(
                "dotnet_tools.py:search_products",
                "chatbot products API error",
                {"query": query, "statusCode": exc.response.status_code},
                "H1",
            )
            # #endregion
            _raise_catalog_error(exc)
        return pick_list(response.json(), "items", "Items")


async def get_product_by_code(code: str) -> dict[str, Any] | None:
    normalized = normalize_product_code(code)
    products = await search_products(normalized)
    for product in products:
        if str(pick(product, "code", "Code", default="")).upper() == normalized:
            return product
    return products[0] if products else None


async def check_stock(product_code: str) -> dict[str, Any]:
    product = await get_product_by_code(product_code)
    if not product:
        raise ValueError(f"No encontré el producto {product_code} en el catálogo El Plonsazo.")
    return {
        "code": pick(product, "code", "Code"),
        "name": pick(product, "name", "Name"),
        "stock": pick(product, "stock", "Stock", default=0),
        "price": float(pick(product, "price", "Price", default=0)),
        "status": pick(product, "status", "Status"),
    }


async def create_sale(
    product_code: str,
    quantity: int,
    customer_name: str,
    customer_email: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "productCode": normalize_product_code(product_code),
        "quantity": quantity,
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
            # #region agent log
            _dbg(
                "dotnet_tools.py:create_sale",
                "from-chatbot API response",
                {
                    "productCode": payload.get("productCode"),
                    "quantity": payload.get("quantity"),
                    "statusCode": response.status_code,
                    "hasServiceKey": bool(CHATBOT_API_KEY),
                },
                "H2",
            )
            # #endregion
        except httpx.HTTPStatusError as exc:
            # #region agent log
            _dbg(
                "dotnet_tools.py:create_sale",
                "from-chatbot API error",
                {"statusCode": exc.response.status_code},
                "H2",
            )
            # #endregion
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
