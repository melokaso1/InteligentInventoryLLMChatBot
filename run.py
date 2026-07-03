"""Punto de entrada del chatbot. Ejecutar desde la raíz de LLMChatBot."""

import os
from pathlib import Path

from dotenv import load_dotenv
import uvicorn

load_dotenv(Path(__file__).resolve().parent / ".env")

# Fuerza UTF-8 en Windows cuando la consola no lo tiene por defecto.
os.environ.setdefault("PYTHONUTF8", "1")

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
