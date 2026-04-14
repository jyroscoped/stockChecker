import os
import tempfile
import unittest

from macbook_raspi_bridge import (
    PiBridgeService,
    is_sender_allowed,
    parse_allowed_senders,
    parse_imessage_command,
)
from raspberry_ingester import DataStore, PriceBar


class CommandParsingTests(unittest.TestCase):
    def test_parse_analyze_with_symbol(self):
        parsed = parse_imessage_command("Analyze $nvda")
        self.assertEqual(parsed.action, "analyze")
        self.assertEqual(parsed.symbol, "NVDA")

    def test_parse_unknown_without_symbol(self):
        parsed = parse_imessage_command("random text")
        self.assertEqual(parsed.action, "unknown")
        self.assertIsNone(parsed.symbol)

    def test_parse_updates_command(self):
        parsed = parse_imessage_command("updates $aapl")
        self.assertEqual(parsed.action, "updates")
        self.assertEqual(parsed.symbol, "AAPL")


class SenderFilteringTests(unittest.TestCase):
    def test_allowed_sender_list_and_match(self):
        allowed = parse_allowed_senders("goldbergerkids@icloud.com, other@example.com")
        self.assertTrue(is_sender_allowed("GoldbergerKids@iCloud.com", allowed))
        self.assertFalse(is_sender_allowed("intruder@example.com", allowed))


class BridgeServiceTests(unittest.TestCase):
    def test_build_response_analyze(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "bridge.db")
            store = DataStore(db_path)

            store.insert_price_bars(
                [
                    PriceBar(
                        provider="yahoo",
                        symbol="NVDA",
                        timestamp="2026-04-14T00:00:00+00:00",
                        open=100.0,
                        high=110.0,
                        low=99.0,
                        close=108.5,
                        volume=10,
                        raw_json="{}",
                    )
                ]
            )
            store.insert_news_items(
                [
                    {
                        "source": "yahoo_finance_rss",
                        "symbol": "NVDA",
                        "title": "NVDA surges",
                        "url": "https://example.com/nvda",
                        "published_at": "2026-04-14",
                        "summary": "Strong momentum in chip demand",
                        "sentiment_score": 0.8,
                    }
                ]
            )
            store.insert_sec_filings(
                [
                    {
                        "cik": "0001045810",
                        "ticker": "NVDA",
                        "company_name": "NVIDIA CORP",
                        "form": "10-Q",
                        "filed_at": "2026-04-10",
                        "accession_no": "0001045810-26-000123",
                        "primary_doc": "nvda10q.htm",
                        "url": "https://www.sec.gov/ixviewer/ix.html",
                    }
                ]
            )

            service = PiBridgeService(db_path)
            output = service.build_response("Analyze $NVDA")

            self.assertIn("Analysis for NVDA", output)
            self.assertIn("latest close=108.50", output)
            self.assertIn("NVDA surges", output)
            self.assertIn("10-Q", output)


if __name__ == "__main__":
    unittest.main()
