"""Unit tests for support agent tools."""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from support_tools import SupportSession, SupportToolExecutor, support_tool_definitions


class SupportToolDefinitionsTests(unittest.TestCase):
    def test_includes_create_support_ticket(self) -> None:
        names = [t["function"]["name"] for t in support_tool_definitions()]
        self.assertIn("create_support_ticket", names)
        self.assertIn("search_wiki", names)
        self.assertIn("escalate_to_human", names)


class SupportToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_support_ticket_via_async(self) -> None:
        retriever = MagicMock()
        executor = SupportToolExecutor(lambda: retriever)
        session = SupportSession(call_id="s1", channel="chat")

        with patch(
            "support_ticket_service.create_and_notify_ticket",
            new_callable=AsyncMock,
            return_value={"ok": True, "hubspot_ticket_id": "100", "message": "Thanks"},
        ):
            raw = await executor.execute_tool(
                "create_support_ticket",
                {
                    "dealership_name": "Acme",
                    "first_name": "Pat",
                    "last_name": "Smith",
                    "email": "pat@acme.com",
                    "phone": "+15551112222",
                    "issue_summary": "Billing",
                    "resolved": False,
                },
                session,
            )
        data = json.loads(raw)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("hubspot_ticket_id"), "100")


if __name__ == "__main__":
    unittest.main()
