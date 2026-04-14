import os
import tempfile
import unittest

from raspberry_ingester import DataStore, IngestionClient, SentimentAnalyzer, _parse_symbols


class SentimentAnalyzerTests(unittest.TestCase):
    def test_score_text_positive(self):
        score = SentimentAnalyzer.score_text("Strong growth and record profits")
        self.assertGreater(score, 0)

    def test_score_text_negative(self):
        score = SentimentAnalyzer.score_text("Bearish decline and weak momentum")
        self.assertLess(score, 0)


class ParseSymbolTests(unittest.TestCase):
    def test_parse_symbols_trims_and_uppercases(self):
        self.assertEqual(_parse_symbols(" aapl, msft ,,Nvda "), ["AAPL", "MSFT", "NVDA"])


class DataStoreTests(unittest.TestCase):
    def test_schema_created(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "test.db")
            store = DataStore(db_path)
            cur = store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='price_bars'"
            )
            self.assertEqual(cur.fetchone()[0], "price_bars")


class YahooParsingTests(unittest.TestCase):
    def test_fetch_yahoo_bars_parses_payload(self):
        client = IngestionClient()

        sample_payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1712620800],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [100.0],
                                    "high": [110.0],
                                    "low": [95.0],
                                    "close": [105.0],
                                    "volume": [123456],
                                }
                            ]
                        },
                    }
                ]
            }
        }

        client.fetch_json = lambda *args, **kwargs: sample_payload
        bars = client.fetch_yahoo_bars("AAPL")

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].provider, "yahoo")
        self.assertEqual(bars[0].symbol, "AAPL")
        self.assertEqual(bars[0].open, 100.0)


if __name__ == "__main__":
    unittest.main()
