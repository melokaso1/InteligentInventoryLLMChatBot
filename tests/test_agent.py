import pytest
from unittest.mock import AsyncMock, patch
from langchain_core.messages import AIMessage
from app.llm.agent import AgentUnavailableError, run_agent_turn

@pytest.mark.asyncio
async def test_agent_turn_when_openai_enabled():
    with patch("app.llm.agent.OPENAI_ENABLED", True):
        with patch("app.llm.agent._get_llm") as llm:
            llm.return_value.ainvoke = AsyncMock(return_value=AIMessage(content="¡Hola! Soy Drogui."))
            result = await run_agent_turn("hola", {"phase": "idle", "chat_history": []})
    assert "Drogui" in result.response

@pytest.mark.asyncio
async def test_agent_disabled_without_api_key():
    with patch("app.llm.agent.OPENAI_ENABLED", False):
        with pytest.raises(AgentUnavailableError):
            await run_agent_turn("hola", {"phase": "idle", "chat_history": []})
