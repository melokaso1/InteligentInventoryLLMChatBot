from contextvars import ContextVar
from typing import Any

current_session: ContextVar[dict[str, Any] | None] = ContextVar("current_session", default=None)
current_session_id: ContextVar[str | None] = ContextVar("current_session_id", default=None)
