import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "60"))
OPENAI_HISTORY_LIMIT = int(os.getenv("OPENAI_HISTORY_LIMIT", "10"))
OPENAI_ENABLED = bool(OPENAI_API_KEY)
