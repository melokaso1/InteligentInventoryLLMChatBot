import os
from typing import Any

import httpx

DOTNET_API_URL = os.getenv("DOTNET_API_URL", "http://localhost:5151").rstrip("/")
TAX_RATE = 0.08


async def search_products(query: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{DOTNET_API_URL}/api/products",
            params={"q": query, "pageSize": 5},
        )
        response.raise_for_status()
        data = response.json()
        return data.get("items") or data.get("Items") or []


async def get_product_by_code(code: str) -> dict[str, Any] | None:
    products = await search_products(code)
    normalized = code.strip().upper()
    for product in products:
        if str(product.get("code", "")).upper() == normalized:
            return product
    return products[0] if products else None


async def check_stock(product_code: str) -> dict[str, Any]:
    product = await get_product_by_code(product_code)
    if not product:
        raise ValueError(f"No encontré el producto {product_code} en el catálogo El Plonsazo.")
    return {
        "code": product.get("code"),
        "name": product.get("name"),
        "stock": product.get("stock", 0),
        "price": float(product.get("price", 0)),
        "status": product.get("status"),
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
                "productCode": product_code,
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
        data = response.json()
        items = data.get("items") or data.get("Items") or []
        for invoice in items:
            if str(invoice.get("invoiceNumber", "")).upper() == invoice_number.upper():
                return invoice
        return items[0] if items else None
