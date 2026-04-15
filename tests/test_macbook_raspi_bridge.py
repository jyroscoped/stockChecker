import os
import tempfile
import unittest

from macbook_raspi_bridge import PiBridgeService, parse_imessage_command
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

            service = PiBridgeService(db_path)
            output = service.build_response("Analyze $NVDA")

            self.assertIn("📊 NVDA At-a-Glance", output)
            self.assertIn("Latest close: 108.50", output)
            self.assertIn("NVDA surges", output)
            self.assertIn("Source: Preloaded Raspberry Pi data", output)

    def test_build_response_analyze_non_core_symbol_triggers_on_demand(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "bridge.db")
            store = DataStore(db_path)
            service = PiBridgeService(db_path)
            calls: list[str] = []

            def fake_ingest(symbol: str):
                calls.append(symbol)
                store.insert_price_bars(
                    [
                        PriceBar(
                            provider="yahoo",
                            symbol=symbol,
                            timestamp="2026-04-14T00:00:00+00:00",
                            open=50.0,
                            high=55.0,
                            low=49.0,
                            close=54.0,
                            volume=1000,
                            raw_json="{}",
                        ),
                        PriceBar(
                            provider="yahoo",
                            symbol=symbol,
                            timestamp="2026-04-13T00:00:00+00:00",
                            open=48.0,
                            high=51.0,
                            low=47.5,
                            close=50.0,
                            volume=900,
                            raw_json="{}",
                        ),
                    ]
                )
                store.insert_news_items(
                    [
                        {
                            "source": "yahoo_finance_rss",
                            "symbol": symbol,
                            "title": "IBM launches AI platform",
                            "url": "https://example.com/ibm-ai",
                            "published_at": "2026-04-14",
                            "summary": "Enterprise momentum",
                            "sentiment_score": 0.4,
                        }
                    ]
                )
                store.insert_social_posts(
                    [
                        {
                            "platform": "reddit",
                            "symbol": symbol,
                            "community": "stocks",
                            "author": "u/test",
                            "title": "IBM is interesting",
                            "body": "Strong growth potential",
                            "url": "https://reddit.com/r/stocks/test",
                            "posted_at": "2026-04-14",
                            "sentiment_score": 0.6,
                        }
                    ]
                )
                store.insert_sec_filings(
                    [
                        {
                            "cik": "0000051143",
                            "ticker": symbol,
                            "company_name": "International Business Machines Corporation",
                            "form": "10-Q",
                            "filed_at": "2026-04-13",
                            "accession_no": "0000051143-26-000001",
                            "primary_doc": "ibm10q.htm",
                            "url": "https://www.sec.gov/Archives/test",
                        }
                    ]
                )
                return {
                    "triggered": True,
                    "price_bars": 2,
                    "news_items": 1,
                    "social_posts": 1,
                    "sec_filings": 1,
                    "notes": [],
                }

            service._ingest_symbol_on_demand = fake_ingest  # type: ignore[method-assign]
            output = service.build_response("Analyze $IBM")

            self.assertEqual(calls, ["IBM"])
            self.assertIn("📊 IBM At-a-Glance", output)
            self.assertIn("🏛️ Financials: 10-Q filed 2026-04-13", output)
            self.assertIn("⚡ Source: On-demand Raspberry Pi refresh", output)

    def test_build_response_handles_unopenable_db_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "missing_dir", "bridge.db")
            service = PiBridgeService(db_path)
            output = service.build_response("Price $NVDA")
            self.assertIn("Bridge database error:", output)
            self.assertIn("Verify --db-path", output)


if __name__ == "__main__":
    unittest.main()
