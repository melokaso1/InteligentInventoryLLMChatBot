import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.schemas import ChatMessageResponse, OperationSummary
from app.tools import dotnet_tools
from app.utils.json_normalize import pick

TAX_RATE = 0.08
SESSIONS: dict[str, dict[str, Any]] = {}

INTENT_PHRASES: dict[str, tuple[str, ...]] = {
    "consultar_stock": (
        "consultar stock",
        "ver stock",
        "revisar stock",
        "chequear stock",
        "consultar disponibilidad",
    ),
    "buscar_producto": (
        "buscar producto",
        "buscar otro producto",
        "buscar un producto",
        "nueva consulta",
    ),
    "ver_ofertas": (
        "ver ofertas",
        "ofertas",
        "ver promociones",
        "promociones",
    ),
    "cancelar": (
        "cancelar",
        "cancelar la solicitud",
        "cancelar pedido",
        "cancelar compra",
        "anular",
        "salir",
    ),
}

FLOW_PHASES = frozenset(
    {
        "awaiting_stock_sku",
        "awaiting_product_search",
        "awaiting_quantity",
        "awaiting_confirmation",
    }
)

_CANCEL_EXACT_PHRASES = frozenset(
    {
        "cancelar",
        "cancelar la solicitud",
        "cancelar pedido",
        "cancelar compra",
        "cancelo",
        "anular",
        "salir",
        "no",
        "no gracias",
        "nop",
    }
)

_ALL_INTENT_PHRASES = {phrase for phrases in INTENT_PHRASES.values() for phrase in phrases}


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


_SESSION_FIELDS = (
    "phase",
    "product_code",
    "product_name",
    "unit_price",
    "stock",
    "quantity",
    "pending_sale",
    "cart",
    "selected_product",
    "last_intent",
    "awaiting_quantity",
    "awaiting_stock_sku",
    "awaiting_product_search",
    "customer_name",
    "customer_email",
    "invoice_number",
    "operation_summary",
)


def _export_session_state(session: dict[str, Any]) -> dict[str, Any]:
    return {key: session.get(key) for key in _SESSION_FIELDS}


def _session(session_id: str) -> dict[str, Any]:
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {
            "phase": "idle",
            "product_code": "",
            "product_name": "",
            "unit_price": 0.0,
            "stock": 0,
            "quantity": 0,
            # Purchase-flow extras (cleared on cancel).
            "pending_sale": False,
            "cart": [],
            "selected_product": {},
            "last_intent": "",
            # Kept for backward-compatibility with earlier graph versions.
            "awaiting_quantity": False,
            "awaiting_stock_sku": False,
            "awaiting_product_search": False,
            "customer_name": "Cliente El Plonsazo",
            "customer_email": "cliente@elplonsazo.com",
            "invoice_number": "",
            "operation_summary": {},
        }
    return SESSIONS[session_id]


def _hydrate_session(session_id: str, state: dict[str, Any] | None) -> dict[str, Any]:
    session = _session(session_id)
    if state:
        session.update(state)
        SESSIONS[session_id] = session
    return session


def _normalize_message(message: str) -> str:
    return " ".join(message.lower().strip().split())


def _normalize_intent(message: str) -> str | None:
    """Map chip labels and menu phrases to intent keys (case-insensitive)."""
    text = _normalize_message(message).rstrip(".,!?")
    for intent, phrases in INTENT_PHRASES.items():
        if text in phrases:
            return intent
    return None


def _is_menu_intent(message: str) -> bool:
    """True when the user tapped a chip or sent a pure menu phrase (not a SKU/query)."""
    return _normalize_intent(message) is not None and _extract_code(message) is None


def _is_intent_phrase(message: str) -> bool:
    return _normalize_message(message).rstrip(".,!?") in _ALL_INTENT_PHRASES


def _extract_code(message: str) -> str | None:
    match = re.search(r"PLZ-[A-Z0-9-]+", message.upper())
    return match.group(0) if match else None


def _product_fields(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": str(pick(product, "code", "Code", default="")),
        "name": str(pick(product, "name", "Name", default="")),
        "price": float(pick(product, "price", "Price", default=0)),
        "stock": int(pick(product, "stock", "Stock", default=0)),
    }


def _idle_welcome() -> tuple[str, list[str]]:
    return (
        "¡Hola! Soy el asistente de El Plonsazo. "
        "Puedo ayudarte a consultar stock, buscar productos y realizar compras. "
        "¿En qué te puedo ayudar?",
        ["Consultar stock", "Buscar producto", "Ver ofertas"],
    )


def _cancel_ack() -> tuple[str, list[str]]:
    return (
        "Operación cancelada. ¿En qué más puedo ayudarte?",
        ["Consultar stock", "Buscar producto", "Ver ofertas"],
    )


def _reset_flow(session: dict[str, Any]) -> None:
    session.update(
        {
            "phase": "idle",
            "product_code": "",
            "product_name": "",
            "unit_price": 0.0,
            "stock": 0,
            "quantity": 0,
            "invoice_number": "",
            "operation_summary": {},
            "pending_sale": False,
            "cart": [],
            "selected_product": {},
            "last_intent": "",
            "awaiting_quantity": False,
            "awaiting_stock_sku": False,
            "awaiting_product_search": False,
        }
    )


def _start_awaiting_quantity(session: dict[str, Any], product: dict[str, Any]) -> None:
    fields = _product_fields(product)
    session.update(
        {
            "phase": "awaiting_quantity",
            "product_code": fields["code"],
            "product_name": fields["name"],
            "unit_price": fields["price"],
            "stock": fields["stock"],
            "pending_sale": True,
            "selected_product": fields,
            "awaiting_quantity": True,
            "awaiting_stock_sku": False,
            "awaiting_product_search": False,
        }
    )


def _looks_like_product_search(message: str) -> bool:
    if _is_menu_intent(message) or _is_intent_phrase(message):
        return False
    text = message.lower()
    return any(
        k in text
        for k in ("plz", "producto", "laptop", "monitor", "teclado", "mouse", "silla", "comprar")
    )


async def _lookup_product(query: str) -> dict[str, Any] | None:
    normalized = query.strip()
    if not normalized or _is_menu_intent(normalized) or _is_intent_phrase(normalized):
        return None
    search_query = _extract_code(normalized) or normalized
    if _is_menu_intent(search_query) or _is_intent_phrase(search_query):
        return None
    return await dotnet_tools.get_product_by_code(search_query)


def _format_product_lines(products: list[dict[str, Any]], limit: int = 5) -> str:
    lines: list[str] = []
    for product in products[:limit]:
        fields = _product_fields(product)
        if not fields["code"]:
            continue
        lines.append(
            f"- **{fields['name']}** (`{fields['code']}`) — "
            f"${fields['price']:,.0f} COP — stock: **{fields['stock']}** u."
        )
    return "\n".join(lines)


async def _handle_menu_intent(
    intent: str,
    session: dict[str, Any],
) -> tuple[str, list[str]]:
    """Handle chip/menu intents — never calls search_products with the label text."""
    if intent == "consultar_stock":
        session["phase"] = "awaiting_stock_sku"
        session["awaiting_stock_sku"] = True
        session["awaiting_quantity"] = False
        return (
            "Para consultar stock, indícame el **SKU** del producto "
            "(formato `PLZ-XX-000`, por ejemplo `PLZ-LAP-001`).",
            ["PLZ-LAP-001", "Buscar producto", "Cancelar"],
        )
    if intent == "buscar_producto":
        session["phase"] = "awaiting_product_search"
        session["awaiting_product_search"] = True
        session["awaiting_stock_sku"] = False
        session["awaiting_quantity"] = False
        return (
            "¿Qué producto buscas? Escribe el nombre o el SKU "
            "(por ejemplo: laptop, monitor, PLZ-LAP-001).",
            ["Ver ofertas", "Consultar stock", "Cancelar"],
        )
    response, chips = await _handle_ver_ofertas()
    session["phase"] = "idle"
    return response, chips


async def _handle_ver_ofertas() -> tuple[str, list[str]]:
    products = await dotnet_tools.search_products("PLZ")
    if not products:
        products = await dotnet_tools.search_products("")
    lines = _format_product_lines(products)
    if lines:
        response = (
            "Estas son algunas ofertas y productos disponibles en El Plonsazo:\n\n"
            f"{lines}\n\n"
            "Indícame un SKU o nombre si quieres comprar o revisar stock."
        )
    else:
        response = (
            "No pude cargar el catálogo en este momento. "
            "Prueba con un SKU como **PLZ-LAP-001** o usa **Buscar producto**."
        )
    return response, ["Consultar stock", "Buscar producto"]


def _extract_quantity(message: str) -> int | None:
    match = re.search(r"\b(\d{1,4})\b", message)
    if not match:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def _is_confirm(message: str) -> bool:
    text = message.lower()
    return any(word in text for word in ("confirmar", "confirmo", "sí", "si ", "si,", "adelante", "comprar"))


def _is_cancel(message: str, phase: str = "idle") -> bool:
    text = _normalize_message(message).rstrip(".,!?")
    if text in _CANCEL_EXACT_PHRASES or _normalize_intent(message) == "cancelar":
        return True
    if any(phrase in text for phrase in ("cancelar", "cancelo", "no quiero", "anular")):
        return True
    if phase in FLOW_PHASES and text in {"no", "no gracias", "nop", "salir"}:
        return True
    return False


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
    intent = _normalize_intent(message)
    session["last_intent"] = intent or ""

    if _is_cancel(message, phase):
        # Guard: once we handle cancel, we must return immediately and
        # not continue any phase logic (prevents "looping" states).
        was_in_flow = phase in FLOW_PHASES
        session["last_intent"] = "cancelar"
        _reset_flow(session)
        response, chips = _cancel_ack() if was_in_flow else _idle_welcome()
        return {
            **state,
            "phase": session["phase"],
            "response": response,
            "chips": chips,
            "invoice_number": "",
            "operation_summary": {},
        }

    # Normalize chip/menu intents BEFORE any phase logic or product search
    if intent in ("consultar_stock", "buscar_producto", "ver_ofertas") and _is_menu_intent(message):
        response, chips = await _handle_menu_intent(intent, session)
        return {
            **state,
            "phase": session["phase"],
            "response": response,
            "chips": chips,
            "invoice_number": invoice_number,
            "operation_summary": operation_summary or {},
        }

    if phase == "idle":
        if intent == "consultar_stock":
            session["phase"] = "awaiting_stock_sku"
            session["awaiting_stock_sku"] = True
            session["awaiting_quantity"] = False
            response = (
                "Para consultar stock, indícame el **SKU** del producto "
                "(formato `PLZ-XX-000`, por ejemplo `PLZ-LAP-001`)."
            )
            chips = ["PLZ-LAP-001", "Buscar producto", "Cancelar"]
        elif intent == "buscar_producto":
            session["phase"] = "awaiting_product_search"
            session["awaiting_product_search"] = True
            session["awaiting_stock_sku"] = False
            session["awaiting_quantity"] = False
            response = (
                "¿Qué producto buscas? Escribe el nombre o el SKU "
                "(por ejemplo: laptop, monitor, PLZ-LAP-001)."
            )
            chips = ["Ver ofertas", "Consultar stock", "Cancelar"]
        elif intent == "ver_ofertas":
            response, chips = await _handle_ver_ofertas()
            session["phase"] = "idle"
        else:
            code = _extract_code(message)
            if code or _looks_like_product_search(message):
                product = await _lookup_product(code or message)
                if not product:
                    query = code or message
                    response = (
                        f"No encontré productos para «{query}». "
                        "Prueba con otro nombre o SKU del catálogo."
                    )
                    chips = ["Buscar producto", "Ver ofertas", "Consultar stock"]
                    session["phase"] = "idle"
                else:
                    fields = _product_fields(product)
                    _start_awaiting_quantity(session, product)
                    response = (
                        f"Encontré **{fields['name']}** ({fields['code']}). "
                        f"Hay **{fields['stock']}** unidades disponibles a "
                        f"**${fields['price']:,.0f} COP** c/u. "
                        "¿Cuántas unidades necesitas?"
                    )
                    chips = ["50 unidades", "10 unidades", "Cancelar"]
            else:
                response, chips = _idle_welcome()
                session["phase"] = "idle"

    elif phase == "awaiting_stock_sku":
        if intent == "buscar_producto":
            session["phase"] = "awaiting_product_search"
            session["awaiting_stock_sku"] = False
            session["awaiting_product_search"] = True
            response = "¿Qué producto buscas? Escribe el nombre o el SKU."
            chips = ["Ver ofertas", "Cancelar"]
        elif intent == "ver_ofertas":
            session["phase"] = "idle"
            response, chips = await _handle_ver_ofertas()
        else:
            code = _extract_code(message)
            if not code and not _is_intent_phrase(message):
                product = await _lookup_product(message)
                if product:
                    code = _product_fields(product)["code"]
            if not code:
                response = (
                    "Necesito un SKU válido para consultar stock "
                    "(ej. `PLZ-LAP-001`). ¿Cuál producto quieres revisar?"
                )
                chips = ["PLZ-LAP-001", "Buscar producto", "Cancelar"]
            else:
                try:
                    stock_info = await dotnet_tools.check_stock(code)
                except ValueError:
                    response = (
                        f"No encontré el producto **{code}** en el catálogo. "
                        "Verifica el SKU o prueba **Buscar producto**."
                    )
                    chips = ["Buscar producto", "Ver ofertas", "Cancelar"]
                else:
                    session["phase"] = "idle"
                    response = (
                        f"Stock de **{stock_info['name']}** (`{stock_info['code']}`): "
                        f"**{stock_info['stock']}** unidades disponibles a "
                        f"**${float(stock_info['price']):,.0f} COP** c/u."
                    )
                    chips = ["Buscar producto", "Ver ofertas", "Consultar stock"]

    elif phase == "awaiting_product_search":
        if intent == "consultar_stock":
            session["phase"] = "awaiting_stock_sku"
            session["awaiting_stock_sku"] = True
            session["awaiting_quantity"] = False
            session["awaiting_product_search"] = False
            response = (
                "Para consultar stock, indícame el **SKU** del producto "
                "(formato `PLZ-XX-000`, por ejemplo `PLZ-LAP-001`)."
            )
            chips = ["PLZ-LAP-001", "Ver ofertas", "Cancelar"]
        elif intent == "ver_ofertas":
            session["phase"] = "idle"
            response, chips = await _handle_ver_ofertas()
        elif _is_intent_phrase(message):
            response = "Escribe el nombre o SKU del producto que buscas."
            chips = ["Ver ofertas", "Consultar stock", "Cancelar"]
        else:
            product = await _lookup_product(message)
            if not product:
                response = (
                    f"No encontré productos para «{message}». "
                    "Prueba con otro nombre o SKU."
                )
                chips = ["Ver ofertas", "Consultar stock", "Cancelar"]
            else:
                fields = _product_fields(product)
                _start_awaiting_quantity(session, product)
                response = (
                    f"Encontré **{fields['name']}** ({fields['code']}). "
                    f"Hay **{fields['stock']}** unidades disponibles a "
                    f"**${fields['price']:,.0f} COP** c/u. "
                    "¿Cuántas unidades necesitas?"
                )
                chips = ["50 unidades", "10 unidades", "Cancelar"]

    elif phase == "awaiting_quantity":
        if intent == "buscar_producto" or "buscar otro producto" in message.lower():
            session["phase"] = "awaiting_product_search"
            session["awaiting_product_search"] = True
            session["awaiting_quantity"] = False
            session["awaiting_stock_sku"] = False
            response = "¿Qué otro producto buscas? Escribe el nombre o el SKU."
            chips = ["Ver ofertas", "Consultar stock", "Cancelar"]
        else:
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
                session["awaiting_quantity"] = False
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
        if "modificar" in message.lower() or "cantidad" in message.lower():
            session["phase"] = "awaiting_quantity"
            session["awaiting_quantity"] = True
            session["awaiting_product_search"] = False
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
                state["session_id"],
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
        if intent == "buscar_producto" or intent == "consultar_stock" or intent == "ver_ofertas":
            if intent == "consultar_stock":
                session["phase"] = "awaiting_stock_sku"
                session["awaiting_stock_sku"] = True
                session["awaiting_quantity"] = False
                response = (
                    "Para consultar stock, indícame el **SKU** del producto "
                    "(formato `PLZ-XX-000`, por ejemplo `PLZ-LAP-001`)."
                )
                chips = ["PLZ-LAP-001", "Buscar producto", "Cancelar"]
            elif intent == "buscar_producto":
                session["phase"] = "awaiting_product_search"
                session["awaiting_product_search"] = True
                session["awaiting_stock_sku"] = False
                session["awaiting_quantity"] = False
                response = "¿Qué producto buscas? Escribe el nombre o el SKU."
                chips = ["Ver ofertas", "Consultar stock", "Cancelar"]
            else:
                response, chips = await _handle_ver_ofertas()
        elif _is_intent_phrase(message):
            response, chips = _idle_welcome()
        else:
            product = await _lookup_product(message)
            if product:
                fields = _product_fields(product)
                _start_awaiting_quantity(session, product)
                response = (
                    f"Encontré **{fields['name']}** ({fields['code']}). "
                    f"Hay **{fields['stock']}** unidades disponibles. ¿Cuántas necesitas?"
                )
                chips = ["50 unidades", "10 unidades", "Cancelar"]
            else:
                response, chips = _idle_welcome()

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


async def run_chat(
    session_id: str,
    message: str,
    state: dict[str, Any] | None = None,
) -> ChatMessageResponse:
    session = _hydrate_session(session_id, state)
    try:
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
    except dotnet_tools.CatalogError as exc:
        return ChatMessageResponse(
            response=str(exc),
            state=session.get("phase", "idle"),
            state_json=_export_session_state(session),
            invoice_number=None,
            chips=["Consultar stock", "Buscar producto", "Cancelar"],
            operation_summary=None,
        )

    summary_data = result.get("operation_summary") or {}
    summary = OperationSummary(**summary_data) if summary_data else None

    return ChatMessageResponse(
        response=result["response"],
        state=session["phase"],
        state_json=_export_session_state(session),
        invoice_number=result.get("invoice_number") or None,
        chips=result.get("chips") or None,
        operation_summary=summary,
    )
