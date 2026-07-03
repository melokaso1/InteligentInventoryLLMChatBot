from pathlib import Path
import sys

from dotenv import load_dotenv

# Permite ejecutar este archivo directamente (IDE / python app/main.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.graph.chat_graph import run_chat
from app.schemas import ChatMessageRequest, ChatMessageResponse

app = FastAPI(title="El Plonsazo Chatbot", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?|https?://.*\.ngrok-free\.dev|https?://.*\.ngrok\.io|https?://.*\.ngrok\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "el-plonsazo-chatbot",
    }


@app.post("/chat/message")
async def chat_message(request: ChatMessageRequest) -> JSONResponse:
    result: ChatMessageResponse = await run_chat(
        request.session_id, request.message, request.state
    )
    return JSONResponse(
        content=result.model_dump(mode="json", by_alias=True),
        media_type="application/json; charset=utf-8",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
