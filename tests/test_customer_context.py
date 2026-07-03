import pytest

from app.graph.chat_graph import SESSIONS, run_chat


@pytest.fixture(autouse=True)
def clear_sessions():
    SESSIONS.clear()
    yield
    SESSIONS.clear()


@pytest.mark.asyncio
async def test_run_chat_applies_logged_in_customer_context():
    result = await run_chat(
        "cust-ctx-1",
        "hola",
        customer_name="Juan Pérez García",
        customer_email="juan@example.com",
    )

    assert result.state_json is not None
    assert result.state_json["customer_name"] == "Juan Pérez García"
    assert result.state_json["customer_email"] == "juan@example.com"


@pytest.mark.asyncio
async def test_run_chat_overrides_generic_customer_on_each_message():
    await run_chat("cust-ctx-2", "hola")
    assert SESSIONS["cust-ctx-2"]["customer_name"] == "Cliente El Plonsazo"

    await run_chat(
        "cust-ctx-2",
        "ver catálogo",
        customer_name="María López",
        customer_email="maria@example.com",
    )

    assert SESSIONS["cust-ctx-2"]["customer_name"] == "María López"
    assert SESSIONS["cust-ctx-2"]["customer_email"] == "maria@example.com"
