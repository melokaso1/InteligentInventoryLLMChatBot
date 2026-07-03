import json
from typing import Any

from langchain_core.tools import tool

from app.llm.context import current_session, current_session_id
from app.tools import dotnet_tools
from app.utils.json_normalize import pick
from app.utils.measure_units import normalize_unit, resolve_sale_quantity, unit_label

OFFERS_LIMIT = 8
TAX_RATE = 0.08


def _format_product(product: dict[str, Any]) -> dict[str, Any]:
    sale_unit = normalize_unit(str(pick(product, "saleUnit", "SaleUnit", default="unit")))
    return {
        "code": pick(product, "code", "Code"),
        "name": pick(product, "name", "Name"),
        "stock": float(pick(product, "stock", "Stock", default=0)),
        "price": float(pick(product, "price", "Price", default=0)),
        "status": pick(product, "status", "Status"),
        "saleUnit": sale_unit,
        "allowsFractional": bool(pick(product, "allowsFractional", "AllowsFractional", default=False)),
    }


def _build_summary(session: dict[str, Any]) -> dict[str, Any]:
    subtotal = session["unit_price"] * session["quantity"]
    tax = round(subtotal * TAX_RATE, 2)
    total = round(subtotal + tax, 2)
    return {
        "transactionId": f"TXN-{session['product_code']}-{session['quantity']}",
        "status": "Completada",
        "productCode": session["product_code"],
        "productName": session["product_name"],
        "quantity": session["quantity"],
        "unitPrice": session["unit_price"],
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
    }


@tool
async def buscar_productos(consulta: str) -> str:
    """Busca productos en el catálogo El Plonsazo por nombre o SKU. Devuelve hasta 5 coincidencias."""
    products = await dotnet_tools.search_products(consulta.strip())
    if not products:
        return json.dumps({"found": 0, "products": []}, ensure_ascii=False)
    formatted = [_format_product(product) for product in products[:5]]
    return json.dumps({"found": len(formatted), "products": formatted}, ensure_ascii=False)


@tool
async def consultar_stock(codigo_o_nombre: str) -> str:
    """Consulta stock, precio y estado de un producto por SKU o nombre."""
    try:
        stock_info = await dotnet_tools.check_stock(codigo_o_nombre.strip())
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    return json.dumps(stock_info, ensure_ascii=False)


@tool
async def listar_ofertas() -> str:
    """Lista productos destacados con stock disponible en el catálogo."""
    products = await dotnet_tools.search_products("")
    offers: list[dict[str, Any]] = []
    for product in products:
        stock = int(pick(product, "stock", "Stock", default=0))
        if stock <= 0:
            continue
        offers.append(_format_product(product))
        if len(offers) >= OFFERS_LIMIT:
            break
    return json.dumps({"count": len(offers), "offers": offers}, ensure_ascii=False)


@tool
async def registrar_venta(codigo_producto: str, cantidad: float, unidad_medida: str | None = None) -> str:
    """Registra una venta confirmada. Usar solo cuando el usuario confirmó explícitamente y la cantidad es válida."""
    session = current_session.get()
    session_id = current_session_id.get()
    if session is None:
        return json.dumps({"error": "Sesión no disponible"}, ensure_ascii=False)

    if cantidad <= 0:
        return json.dumps({"error": "La cantidad debe ser mayor que cero"}, ensure_ascii=False)

    try:
        stock_info = await dotnet_tools.check_stock(codigo_producto.strip())
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    stock = float(stock_info.get("stock", 0))
    sale_unit = stock_info.get("saleUnit", "unit")
    try:
        normalized_qty, resolved_unit = resolve_sale_quantity(cantidad, unidad_medida, sale_unit)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    if normalized_qty > stock:
        return json.dumps(
            {
                "error": (
                    f"Solo hay {stock:g} {unit_label(sale_unit, plural=True)} disponibles "
                    f"de {stock_info.get('name', codigo_producto)}"
                ),
                "stock": stock,
            },
            ensure_ascii=False,
        )

    try:
        result = await dotnet_tools.create_sale(
            session.get("customer_name", "Cliente El Plonsazo"),
            session.get("customer_email", "cliente@elplonsazo.com"),
            [
                {
                    "product_code": stock_info["code"],
                    "quantity": normalized_qty,
                    "measure_unit": resolved_unit,
                }
            ],
            session_id,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    invoice_number = result.get("invoiceNumber") or result.get("invoice_number", "")
    order_number = result.get("orderNumber") or result.get("order_number", "")
    session.update(
        {
            "phase": "sale_completed",
            "product_code": stock_info["code"],
            "product_name": stock_info["name"],
            "unit_price": float(stock_info["price"]),
            "stock": stock,
            "quantity": normalized_qty,
            "measure_unit": resolved_unit,
            "invoice_number": invoice_number,
            "pending_sale": False,
            "awaiting_quantity": False,
            "awaiting_stock_sku": False,
            "awaiting_product_search": False,
        }
    )
    summary = _build_summary(session)
    session["operation_summary"] = summary

    return json.dumps(
        {
            "success": True,
            "orderNumber": order_number,
            "invoiceNumber": invoice_number,
            "product": stock_info,
            "quantity": normalized_qty,
            "measure_unit": resolved_unit,
            "summary": summary,
        },
        ensure_ascii=False,
    )


CHAT_TOOLS = [buscar_productos, consultar_stock, listar_ofertas, registrar_venta]
