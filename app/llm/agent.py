import logging
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.llm.config import (
    OPENAI_API_KEY,
    OPENAI_ENABLED,
    OPENAI_HISTORY_LIMIT,
    OPENAI_MODEL,
    OPENAI_TIMEOUT,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Te llamas **Drogui** y eres el asistente de ventas de **El Plonsazo**, un catálogo de inventario ficticio de un taller universitario. Tu tono es natural, cercano y amable.

## Cómo funciona el chat
- El usuario escribe en **lenguaje natural** o usa los **botones del menú** (Consultar stock, Buscar producto, Ver ofertas, Cancelar).

## Tu rol
- Responde de forma natural y breve (1–3 párrafos).
- Si quiere comprar, buscar o consultar stock, **guíalo con lenguaje natural** (ej. «Escribe el nombre del producto o su SKU»).
- **No inventes** precios, stock ni SKUs.
- Responde en **español**."""

MENU_CHIPS = ["Consultar stock", "Buscar producto", "Ver ofertas", "¿Cómo me comunico?"]

_llm: Any | None = None


class AgentUnavailableError(Exception):
    """No hay proveedor LLM configurado o no respondió."""


@dataclass
class AgentTurnResult:
    response: str
    chips: list[str] | None = None
    invoice_number: str | None = None
    operation_summary: dict[str, Any] | None = None
    offers: list[dict[str, Any]] | None = None
    offers_total_count: int | None = None


def _get_llm() -> Any:
    global _llm
    if _llm is None:
        if not OPENAI_ENABLED:
            raise AgentUnavailableError("OPENAI_API_KEY no configurada")
        from langchain_openai import ChatOpenAI

        _llm = ChatOpenAI(
            api_key=OPENAI_API_KEY,
            model=OPENAI_MODEL,
            temperature=0.5,
            timeout=OPENAI_TIMEOUT,
        )
    return _llm


def _session_context(session: dict[str, Any]) -> str:
    parts = [f"Fase de conversación: {session.get('phase', 'idle')}"]
    if session.get("product_name"):
        parts.append(
            f"Producto en contexto: {session['product_name']} ({session.get('product_code', '')})"
        )
    if session.get("pending_sale"):
        parts.append("Hay una compra pendiente de confirmación.")
    return "\n".join(parts)


def _history_messages(session: dict[str, Any]) -> list[HumanMessage | AIMessage]:
    history = session.get("chat_history") or []
    messages: list[HumanMessage | AIMessage] = []
    for item in history[-OPENAI_HISTORY_LIMIT:]:
        role = item.get("role", "")
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))
    return messages


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text", "")))
            elif isinstance(block, str):
                chunks.append(block)
        return "\n".join(chunk for chunk in chunks if chunk).strip()
    return str(content).strip()


def _suggest_chips(message: str, response_text: str) -> list[str]:
    combined = f"{message} {response_text}".lower()
    if any(w in combined for w in ("comprar", "compra", "quiero")):
        return ["Buscar producto", "Ver ofertas", "¿Cómo me comunico?"]
    if any(w in combined for w in ("stock", "precio", "cuesta")):
        return ["Consultar stock", "Buscar producto", "¿Cómo me comunico?"]
    return list(MENU_CHIPS)


async def run_agent_turn(
    message: str,
    session: dict[str, Any],
    session_id: str = "",
) -> AgentTurnResult:
    del session_id
    if not OPENAI_ENABLED:
        raise AgentUnavailableError("OPENAI_API_KEY no configurada")

    system_text = f"{SYSTEM_PROMPT}\n\n## Contexto de sesión\n{_session_context(session)}"
    result = await _get_llm().ainvoke(
        [
            SystemMessage(content=system_text),
            *_history_messages(session),
            HumanMessage(content=message),
        ]
    )
    response_text = _extract_text(result.content)
    if not response_text:
        raise AgentUnavailableError("El proveedor LLM no devolvió una respuesta de texto")

    return AgentTurnResult(
        response=response_text,
        chips=_suggest_chips(message, response_text),
    )
