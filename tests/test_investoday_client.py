from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investoday_client import (  # noqa: E402
    InvestodayClient,
    InvestodayError,
    _decode_payload,
    _endpoint,
)


class InvestodayClientTests(unittest.TestCase):
    def test_endpoint_adds_api_key_without_logging_or_replacing_existing_query(self) -> None:
        endpoint = _endpoint("https://example.test/data/mcp/preset?mode=live", "secret-key")
        self.assertIn("mode=live", endpoint)
        self.assertIn("apiKey=secret-key", endpoint)

    def test_decode_structured_content(self) -> None:
        payload = {
            "result": {
                "structuredContent": {
                    "code": 0,
                    "data": [{"title": "AI"}],
                }
            }
        }
        self.assertEqual(_decode_payload(payload)["data"][0]["title"], "AI")

    def test_list_news_maps_mcp_data(self) -> None:
        client = InvestodayClient("test-key", "https://example.test/mcp")
        with patch.object(
            client,
            "call",
            return_value={"code": 0, "data": [{"title": "GPU交期缩短"}]},
        ) as call:
            rows = client.list_news(title="GPU", page_size=4)
        self.assertEqual(rows[0]["title"], "GPU交期缩短")
        self.assertEqual(call.call_args.args[0], "list_news")
        self.assertEqual(call.call_args.args[1]["pageSize"], 4)

    def test_missing_key_is_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(InvestodayError):
                InvestodayClient().list_news()


if __name__ == "__main__":
    unittest.main()
