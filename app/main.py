from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.graph.chat_graph import run_chat
from app.schemas import ChatMessageRequest, ChatMessageResponse

app = FastAPI(title="El Plonsazo Chatbot", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5151", "http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "el-plonsazo-chatbot"}


@app.post("/chat/message", response_model=ChatMessageResponse)
async def chat_message(request: ChatMessageRequest) -> ChatMessageResponse:
    return await run_chat(request.session_id, request.message)
