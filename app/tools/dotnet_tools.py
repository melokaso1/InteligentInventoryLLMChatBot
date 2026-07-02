import os
from typing import Any

import httpx

from app.utils.json_normalize import pick, pick_list, normalize_product_code

DOTNET_API_URL = os.getenv("DOTNET_API_URL", "http://localhost:5151").rstrip("/")
TAX_RATE = 0.08


async def search_products(query: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{DOTNET_API_URL}/api/products",
            params={"q": query, "pageSize": 5},
        )
        response.raise_for_status()
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
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{DOTNET_API_URL}/api/sales/from-chatbot",
            json={
                "productCode": normalize_product_code(product_code),
                "quantity": quantity,
                "customerName": customer_name,
                "customerEmail": customer_email,
            },
        )
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
