import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

# Ensure `import app...` works when running tests from repo root.
LLM_CHATBOT_ROOT = Path(__file__).resolve().parents[1]
if str(LLM_CHATBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LLM_CHATBOT_ROOT))

import app.graph.chat_graph as chat_graph


class ChatbotCancelTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        chat_graph.SESSIONS.clear()

    async def test_cancel_at_quantity_step(self) -> None:
        session_id = "test-cancel-qty"
        product = {"code": "PLZ-LAP-001", "name": "Laptop", "price": 100000, "stock": 20}

        with patch.object(chat_graph.dotnet_tools, "get_product_by_code", new=AsyncMock(return_value=product)):
            resp1 = await chat_graph.run_chat(session_id, "PLZ-LAP-001")

        self.assertEqual(resp1.state, "awaiting_quantity")
        self.assertIn("¿Cuántas unidades necesitas?", resp1.response)

        with patch.object(chat_graph.dotnet_tools, "get_product_by_code", new=AsyncMock(return_value=product)):
            resp2 = await chat_graph.run_chat(session_id, "cancelar")

        self.assertEqual(resp2.state, "idle")
        self.assertIn("Operación cancelada", resp2.response)
        self.assertIn("Consultar stock", resp2.chips or [])

        session = chat_graph.SESSIONS[session_id]
        self.assertEqual(session["phase"], "idle")
        self.assertEqual(session["quantity"], 0)
        self.assertEqual(session["product_code"], "")
        self.assertFalse(session["pending_sale"])

    async def test_cancel_after_selecting_product_flow(self) -> None:
        """
        "Seleccionar producto" in this simplified bot means tapping "Buscar producto",
        which moves the bot into `awaiting_product_search`. Cancel there should reset
        back to `idle`.
        """
        session_id = "test-cancel-product-search"

        resp1 = await chat_graph.run_chat(session_id, "buscar producto")
        self.assertEqual(resp1.state, "awaiting_product_search")

        resp2 = await chat_graph.run_chat(session_id, "Cancelar la solicitud")
        self.assertEqual(resp2.state, "idle")
        self.assertIn("Operación cancelada", resp2.response)

        session = chat_graph.SESSIONS[session_id]
        self.assertEqual(session["phase"], "idle")
        self.assertEqual(session["quantity"], 0)

    async def test_cancel_after_stock_lookup(self) -> None:
        session_id = "test-cancel-after-stock"
        stock_info = {"code": "PLZ-LAP-001", "name": "Laptop", "stock": 5, "price": 100000, "status": "active"}

        resp1 = await chat_graph.run_chat(session_id, "consultar stock")
        self.assertEqual(resp1.state, "awaiting_stock_sku")

        with patch.object(chat_graph.dotnet_tools, "check_stock", new=AsyncMock(return_value=stock_info)):
            resp2 = await chat_graph.run_chat(session_id, "PLZ-LAP-001")

        self.assertEqual(resp2.state, "idle")
        self.assertIn("Stock de", resp2.response)

        resp3 = await chat_graph.run_chat(session_id, "cancelar")
        self.assertEqual(resp3.state, "idle")
        # When not inside the purchase flow, the bot should show the idle welcome
        # (and never go back to asking for quantity).
        self.assertIn("¿En qué te puedo ayudar?", resp3.response)
        self.assertNotIn("Indica la cantidad en números", resp3.response)

        session = chat_graph.SESSIONS[session_id]
        self.assertEqual(session["phase"], "idle")
        self.assertEqual(session["quantity"], 0)
        self.assertEqual(session["product_code"], "")

