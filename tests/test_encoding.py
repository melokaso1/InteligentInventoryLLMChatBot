from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.graph.chat_graph import run_chat
from app.main import app
from app.schemas import ChatMessageResponse, OperationSummary


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.asyncio
async def test_run_chat_preserves_accented_characters():
    result = await run_chat("accent-rules", "¿Cómo me comunico?")

    assert "líquido" in result.response or "lenguaje" in result.response
    assert "confirmación" not in result.response or "ó" in result.response


def test_chat_message_json_utf8_charset(client):
    accented_response = (
        "Resumen: **15 gramos de Éxtasis Tesla 300mg** — "
        "Pendiente de confirmación. Producto líquido disponible."
    )
    summary = OperationSummary(
        transaction_id="TXN-TEST-15",
        status="Pendiente de confirmación",
        product_code="PLZ-EXT-056",
        product_name="Éxtasis Tesla 300mg",
        quantity=15,
        measure_unit="gram",
        unit_price=60000,
        subtotal=900000,
        tax=171000,
        total=1071000,
    )
    mock_result = ChatMessageResponse(
        response=accented_response,
        state="awaiting_confirmation",
        operation_summary=summary,
    )

    with patch("app.main.run_chat", new=AsyncMock(return_value=mock_result)):
        response = client.post(
            "/chat/message",
            json={"sessionId": "utf8-test", "message": "confirmar"},
        )

    assert response.status_code == 200
    content_type = response.headers.get("content-type", "").lower()
    assert "charset=utf-8" in content_type

    body = response.json()
    assert body["response"] == accented_response
    assert "confirmación" in body["response"]
    assert "Éxtasis" in body["response"]
    assert "líquido" in body["response"]
    assert body["operationSummary"]["status"] == "Pendiente de confirmación"
    assert "\\u00" not in response.text
