import os
import socket
import tempfile
import unittest
import urllib.error

from macbook_raspi_bridge import PiBridgeService, format_network_error, parse_imessage_command
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

            self.assertIn("Analysis for NVDA", output)
            self.assertIn("latest close=108.50", output)
            self.assertIn("NVDA surges", output)


class NetworkErrorFormattingTests(unittest.TestCase):
    def test_format_network_error_unauthorized(self):
        exc = urllib.error.HTTPError(
            url="http://raspberrypi.local:8787/command",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        message = format_network_error(exc, "http://raspberrypi.local:8787")
        self.assertIn("HTTP 401 Unauthorized", message)
        self.assertIn("BRIDGE_TOKEN", message)

    def test_format_network_error_host_resolution(self):
        exc = urllib.error.URLError(socket.gaierror("Name or service not known"))
        message = format_network_error(exc, "http://raspberrypi.local:8787")
        self.assertIn("Could not resolve host", message)

    def test_format_network_error_timeout(self):
        exc = urllib.error.URLError(TimeoutError("timed out"))
        message = format_network_error(exc, "http://raspberrypi.local:8787")
        self.assertIn("timed out", message)

    def test_format_network_error_connection_refused(self):
        exc = urllib.error.URLError(ConnectionRefusedError("refused"))
        message = format_network_error(exc, "http://raspberrypi.local:8787")
        self.assertIn("Connection refused", message)


if __name__ == "__main__":
    unittest.main()
