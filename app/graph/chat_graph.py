import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.schemas import ChatMessageResponse, OperationSummary
from app.tools import dotnet_tools

TAX_RATE = 0.08
SESSIONS: dict[str, dict[str, Any]] = {}


class GraphState(TypedDict):
    session_id: str
    message: str
    phase: str
    product_code: str
    product_name: str
    unit_price: float
    stock: int
    quantity: int
    customer_name: str
    customer_email: str
    response: str
    chips: list[str]
    invoice_number: str
    operation_summary: dict[str, Any]


def _session(session_id: str) -> dict[str, Any]:
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {
            "phase": "idle",
            "product_code": "",
            "product_name": "",
            "unit_price": 0.0,
            "stock": 0,
            "quantity": 0,
            "customer_name": "Cliente El Plonsazo",
            "customer_email": "cliente@elplonsazo.com",
            "invoice_number": "",
            "operation_summary": {},
        }
    return SESSIONS[session_id]


def _extract_code(message: str) -> str | None:
    match = re.search(r"PLZ-[A-Z0-9-]+", message.upper())
    return match.group(0) if match else None


def _extract_quantity(message: str) -> int | None:
    match = re.search(r"\b(\d{1,4})\b", message)
    if not match:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def _is_confirm(message: str) -> bool:
    text = message.lower()
    return any(word in text for word in ("confirmar", "confirmo", "sí", "si ", "si,", "adelante", "comprar"))


def _is_cancel(message: str) -> bool:
    text = message.lower()
    return any(word in text for word in ("cancelar", "cancelo", "no quiero", "anular"))


def _build_summary(session: dict[str, Any]) -> dict[str, Any]:
    subtotal = session["unit_price"] * session["quantity"]
    tax = round(subtotal * TAX_RATE, 2)
    total = round(subtotal + tax, 2)
    return {
        "transactionId": f"TXN-{session['product_code']}-{session['quantity']}",
        "status": "Pendiente de confirmación",
        "productCode": session["product_code"],
        "productName": session["product_name"],
        "quantity": session["quantity"],
        "unitPrice": session["unit_price"],
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
    }


async def process_node(state: GraphState) -> GraphState:
    session = _session(state["session_id"])
    message = state["message"].strip()
    phase = session["phase"]

    response = ""
    chips: list[str] = []
    invoice_number = session.get("invoice_number", "")
    operation_summary = session.get("operation_summary") or None

    if phase == "idle":
        code = _extract_code(message)
        text = message.lower()
        wants_product = code is not None or any(
            k in text for k in ("plz", "stock", "producto", "marihuana", "lsd", "tussi", "mdm", "ketamina", "comprar")
        )
        if not wants_product:
            response = (
                "¡Hola! Soy el asistente de El Plonsazo. "
                "Indícame el producto o SKU (ej. PLZ-MJ-001) para revisar disponibilidad."
            )
        else:
            query = code or message
            product = await dotnet_tools.get_product_by_code(query)
            if not product:
                response = f"No encontré productos para «{query}». Prueba con otro nombre o SKU del catálogo."
                session["phase"] = "idle"
            else:
                session.update(
                    {
                        "phase": "awaiting_quantity",
                        "product_code": product["code"],
                        "product_name": product["name"],
                        "unit_price": float(product["price"]),
                        "stock": int(product["stock"]),
                    }
                )
                response = (
                    f"Encontré **{product['name']}** ({product['code']}). "
                    f"Hay **{product['stock']}** unidades disponibles a **${float(product['price']):,.0f} COP** c/u. "
                    "¿Cuántas unidades necesitas?"
                )
                chips = ["50 unidades", "10 unidades", "Cancelar"]

    elif phase == "awaiting_quantity":
        quantity = _extract_quantity(message)
        if quantity is None:
            response = "Indica la cantidad en números (por ejemplo: 50)."
            chips = ["50 unidades", "20 unidades", "Cancelar"]
        elif quantity > session["stock"]:
            response = (
                f"Solo hay **{session['stock']}** unidades de {session['product_name']}. "
                "Ajusta la cantidad o elige otro producto."
            )
            chips = [f"{session['stock']} unidades", "Buscar otro producto", "Cancelar"]
        else:
            session["quantity"] = quantity
            session["phase"] = "awaiting_confirmation"
            summary = _build_summary(session)
            session["operation_summary"] = summary
            operation_summary = summary
            response = (
                f"Resumen: **{quantity}× {session['product_name']}** — "
                f"subtotal ${summary['subtotal']:,.0f} COP + IVA ${summary['tax']:,.0f} COP = "
                f"**${summary['total']:,.0f} COP**. ¿Confirmas la compra?"
            )
            chips = ["Confirmar compra", "Modificar cantidad", "Cancelar"]

    elif phase == "awaiting_confirmation":
        if _is_cancel(message):
            session["phase"] = "idle"
            session["operation_summary"] = {}
            operation_summary = None
            response = "Operación cancelada. ¿En qué más puedo ayudarte?"
        elif "modificar" in message.lower() or "cantidad" in message.lower():
            session["phase"] = "awaiting_quantity"
            session["operation_summary"] = {}
            operation_summary = None
            response = f"De acuerdo. ¿Cuántas unidades de {session['product_name']} deseas?"
            chips = ["50 unidades", "10 unidades", "Cancelar"]
        elif _is_confirm(message):
            result = await dotnet_tools.create_sale(
                session["product_code"],
                session["quantity"],
                session["customer_name"],
                session["customer_email"],
            )
            invoice_number = result.get("invoiceNumber") or result.get("invoice_number", "")
            session["invoice_number"] = invoice_number
            session["phase"] = "sale_completed"
            summary = _build_summary(session)
            summary["status"] = "Completada"
            session["operation_summary"] = summary
            operation_summary = summary
            response = (
                f"¡Compra confirmada! Pedido **{result.get('orderNumber', '')}** — "
                f"factura **{invoice_number}**. El inventario ya fue actualizado en El Plonsazo."
            )
            chips = ["Nueva consulta"]
        else:
            response = "Responde **Confirmar compra** para finalizar o **Cancelar** para anular."
            chips = ["Confirmar compra", "Cancelar"]
            operation_summary = session.get("operation_summary") or None

    elif phase == "sale_completed":
        session["phase"] = "idle"
        session["operation_summary"] = {}
        operation_summary = None
        invoice_number = ""
        code = _extract_code(message)
        query = code or message
        if len(query) >= 2:
            product = await dotnet_tools.get_product_by_code(query)
            if product:
                session.update(
                    {
                        "phase": "awaiting_quantity",
                        "product_code": product["code"],
                        "product_name": product["name"],
                        "unit_price": float(product["price"]),
                        "stock": int(product["stock"]),
                    }
                )
                response = (
                    f"Encontré **{product['name']}** ({product['code']}). "
                    f"Hay **{product['stock']}** unidades disponibles. ¿Cuántas necesitas?"
                )
                chips = ["50 unidades", "10 unidades", "Cancelar"]
            else:
                response = "¿Qué producto o SKU quieres consultar?"
        else:
            response = "¿Qué producto o SKU quieres consultar?"

    else:
        session["phase"] = "idle"
        response = "Reinicié la conversación. ¿Qué producto necesitas?"

    return {
        **state,
        "phase": session["phase"],
        "response": response,
        "chips": chips,
        "invoice_number": invoice_number,
        "operation_summary": operation_summary or {},
    }


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("process", process_node)
    graph.add_edge(START, "process")
    graph.add_edge("process", END)
    return graph.compile()


GRAPH = build_graph()


async def run_chat(session_id: str, message: str) -> ChatMessageResponse:
    session = _session(session_id)
    result = await GRAPH.ainvoke(
        {
            "session_id": session_id,
            "message": message,
            "phase": session["phase"],
            "product_code": session.get("product_code", ""),
            "product_name": session.get("product_name", ""),
            "unit_price": session.get("unit_price", 0.0),
            "stock": session.get("stock", 0),
            "quantity": session.get("quantity", 0),
            "customer_name": session.get("customer_name", ""),
            "customer_email": session.get("customer_email", ""),
            "response": "",
            "chips": [],
            "invoice_number": session.get("invoice_number", ""),
            "operation_summary": session.get("operation_summary", {}),
        }
    )

    summary_data = result.get("operation_summary") or {}
    summary = OperationSummary(**summary_data) if summary_data else None

    return ChatMessageResponse(
        response=result["response"],
        state=session["phase"],
        invoice_number=result.get("invoice_number") or None,
        chips=result.get("chips") or None,
        operation_summary=summary,
    )
