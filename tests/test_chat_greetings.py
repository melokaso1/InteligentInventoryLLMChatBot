import unittest
from unittest.mock import AsyncMock, patch

from app.graph import chat_graph
from app.graph.chat_graph import SESSIONS, _is_greeting, _looks_like_product_search, run_chat

CATALOG = {
    "PLZ-MJ-001": {"code": "PLZ-MJ-001", "name": "Marihuana", "price": 45000, "stock": 100},
}


class TestGreetingDetection(unittest.TestCase):
    def test_hola_is_greeting(self):
        self.assertTrue(_is_greeting("hola"))

    def test_product_query_detected(self):
        self.assertTrue(_looks_like_product_search("marihuana"))


class TestRunChatGreeting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        SESSIONS.clear()

    def tearDown(self):
        SESSIONS.clear()

    async def test_hola_welcome(self):
        result = await run_chat("sg", "hola")
        self.assertIn("Drogui", result.response)
        self.assertIn("Consultar stock", " ".join(result.chips or []))

    async def test_comprar_isolated(self):
        async def get_code(code):
            return CATALOG.get(code.strip().upper())

        async def search(q):
            from app.graph.chat_graph import _strip_accents
            s = _strip_accents(q.strip().upper())
            return [
                p
                for p in CATALOG.values()
                if not s or s in _strip_accents(p["name"].upper()) or s in p["code"]
            ]

        with (
            patch.object(chat_graph.dotnet_tools, "get_product_by_code", side_effect=get_code),
            patch.object(chat_graph.dotnet_tools, "search_products", side_effect=search),
        ):
            await run_chat("sa", "quiero comprar marihuana")
            await run_chat("sb", "hola")
        self.assertTrue(SESSIONS["sa"]["product_code"].startswith("PLZ-MJ-"))

    async def test_hola_not_catalog_search(self):
        result = await run_chat("g", "hola")
        self.assertNotIn("No encontré productos", result.response)
