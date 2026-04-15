#!/usr/bin/env python3
"""MacBook/iOS <-> Raspberry Pi communication bridge.

- Run `serve-pi` on Raspberry Pi to expose a small authenticated HTTP API.
- Run `send-mac` on MacBook/BlueBubbles side to forward iMessage commands.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple


TICKER_SYMBOL_PATTERN = re.compile(r"\$([A-Za-z][A-Za-z0-9\.-]{0,9})")
MAX_REQUEST_BODY_SIZE = 10_000
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20
CORE_PRELOADED_SYMBOLS = {"NVDA", "AAPL", "SPY", "VOO", "MSFT"}


@dataclass(frozen=True)
class ParsedCommand:
    action: str
    symbol: Optional[str]


def parse_imessage_command(text: str) -> ParsedCommand:
    lower = text.strip().lower()
    symbol_match = TICKER_SYMBOL_PATTERN.search(text)
    symbol = symbol_match.group(1).upper() if symbol_match else None

    if lower.startswith("analyze"):
        return ParsedCommand("analyze", symbol)
    if lower.startswith("price"):
        return ParsedCommand("price", symbol)
    if lower.startswith("news"):
        return ParsedCommand("news", symbol)
    if lower.startswith("sentiment"):
        return ParsedCommand("sentiment", symbol)
    if lower.startswith("help"):
        return ParsedCommand("help", symbol)
    return ParsedCommand("unknown", symbol)


class PiBridgeService:
    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(os.path.expanduser(db_path))

    def _validate_db_directory(self) -> None:
        db_dir = os.path.dirname(self.db_path) or "."
        if not os.path.isdir(db_dir):
            raise sqlite3.OperationalError(f"database directory does not exist: {db_dir}")

    def check_db_ready(self) -> None:
        self._validate_db_directory()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()

    def _open_conn(self) -> sqlite3.Connection:
        self._validate_db_directory()
        return sqlite3.connect(self.db_path)

    def _query_one(self, sql: str, params: Tuple[Any, ...]) -> Optional[Tuple[Any, ...]]:
        conn = self._open_conn()
        try:
            cur = conn.execute(sql, params)
            return cur.fetchone()
        finally:
            conn.close()

    def _query_many(self, sql: str, params: Tuple[Any, ...]) -> list[Tuple[Any, ...]]:
        conn = self._open_conn()
        try:
            cur = conn.execute(sql, params)
            return cur.fetchall()
        finally:
            conn.close()

    def get_latest_price(self, symbol: str) -> Optional[Tuple[float, str, str]]:
        row = self._query_one(
            """
            SELECT close, timestamp, provider
            FROM price_bars
            WHERE symbol = ? AND close IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (symbol.upper(),),
        )
        if not row:
            return None
        return float(row[0]), str(row[1]), str(row[2])

    def _get_trimmed_recent_average(
        self,
        table_name: str,
        symbol: str,
        time_column: str,
        limit: int = 40,
    ) -> Optional[float]:
        if table_name not in {"news_items", "social_posts"}:
            raise ValueError(f"unsupported sentiment source: {table_name}")
        if time_column not in {"published_at", "posted_at"}:
            raise ValueError(f"unsupported sentiment time column: {time_column}")
        rows = self._query_many(
            f"""
            SELECT sentiment_score
            FROM {table_name}
            WHERE symbol = ? AND sentiment_score IS NOT NULL
            ORDER BY COALESCE({time_column}, '') DESC, id DESC
            LIMIT ?
            """,
            (symbol.upper(), limit),
        )
        scores = [float(row[0]) for row in rows if row and row[0] is not None]
        if not scores:
            return None
        scores.sort()
        trim = int(len(scores) * 0.15)
        if len(scores) >= 7 and trim > 0:
            scores = scores[trim : len(scores) - trim]
        if not scores:
            return None
        return sum(scores) / len(scores)

    def get_sentiment(self, symbol: str) -> Optional[float]:
        return self._get_trimmed_recent_average(
            table_name="news_items",
            symbol=symbol,
            time_column="published_at",
        )

    def get_latest_headlines(self, symbol: str, limit: int = 3) -> list[str]:
        rows = self._query_many(
            """
            SELECT title
            FROM news_items
            WHERE symbol = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (symbol.upper(), limit),
        )
        return [str(r[0]) for r in rows]

    def get_social_sentiment(self, symbol: str) -> Optional[float]:
        return self._get_trimmed_recent_average(
            table_name="social_posts",
            symbol=symbol,
            time_column="posted_at",
        )

    def get_latest_two_closes(self, symbol: str) -> list[float]:
        rows = self._query_many(
            """
            SELECT close
            FROM price_bars
            WHERE symbol = ? AND close IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 2
            """,
            (symbol.upper(),),
        )
        return [float(r[0]) for r in rows if r and r[0] is not None]

    def get_latest_sec_filing(self, symbol: str) -> Optional[Tuple[str, str]]:
        row = self._query_one(
            """
            SELECT form, COALESCE(filed_at, '')
            FROM sec_filings
            WHERE ticker = ?
            ORDER BY COALESCE(filed_at, '') DESC, id DESC
            LIMIT 1
            """,
            (symbol.upper(),),
        )
        if not row:
            return None
        return str(row[0]), str(row[1])

    def _ingest_symbol_on_demand(self, symbol: str) -> Dict[str, Any]:
        from raspberry_ingester import DataStore, IngestionClient

        result: Dict[str, Any] = {
            "triggered": True,
            "price_bars": 0,
            "news_items": 0,
            "social_posts": 0,
            "sec_filings": 0,
            "notes": [],
        }

        store = DataStore(self.db_path)
        client = IngestionClient(timeout_seconds=20)
        try:
            try:
                bars = client.fetch_yahoo_bars(symbol)
                store.insert_price_bars(bars)
                result["price_bars"] = len(bars)
            except Exception as exc:  # noqa: BLE001
                result["notes"].append(f"Yahoo price fetch failed for {symbol}: {exc}")

            try:
                news = client.fetch_yahoo_news(symbol, max_items=10)
                store.insert_news_items(news)
                result["news_items"] = len(news)
            except Exception as exc:  # noqa: BLE001
                result["notes"].append(f"News fetch failed for {symbol}: {exc}")

            social_total = 0
            for subreddit in ("stocks", "investing", "wallstreetbets"):
                try:
                    posts = client.fetch_reddit_posts(symbol, subreddit, max_items=10)
                    store.insert_social_posts(posts)
                    social_total += len(posts)
                except Exception as exc:  # noqa: BLE001
                    result["notes"].append(f"Social fetch failed in r/{subreddit}: {exc}")
            result["social_posts"] = social_total

            try:
                filings = client.fetch_sec_filings(symbol, max_items=10)
                store.insert_sec_filings(filings)
                result["sec_filings"] = len(filings)
            except Exception as exc:  # noqa: BLE001
                result["notes"].append(f"SEC filing fetch failed for {symbol}: {exc}")
        finally:
            store.conn.close()

        return result

    @staticmethod
    def _sentiment_label(score: Optional[float]) -> str:
        if score is None:
            return "n/a"
        if score >= 0.15:
            return f"bullish ({score:.3f})"
        if score <= -0.15:
            return f"bearish ({score:.3f})"
        return f"neutral ({score:.3f})"

    def build_response(self, text: str) -> str:
        parsed = parse_imessage_command(text)
        if parsed.action == "help":
            return (
                "Commands: Analyze $TICKER, Price $TICKER, News $TICKER, Sentiment $TICKER"
            )

        if not parsed.symbol:
            return "No ticker found. Try: Analyze $NVDA"

        symbol = parsed.symbol
        try:
            if parsed.action in {"analyze", "price"}:
                on_demand_result: Optional[Dict[str, Any]] = None
                attempted_on_demand = False
                if parsed.action == "analyze" and symbol not in CORE_PRELOADED_SYMBOLS:
                    on_demand_result = self._ingest_symbol_on_demand(symbol)
                    attempted_on_demand = True

                latest = self.get_latest_price(symbol)
                if parsed.action == "analyze" and not latest and not attempted_on_demand:
                    on_demand_result = self._ingest_symbol_on_demand(symbol)
                    latest = self.get_latest_price(symbol)
                if not latest:
                    return f"No price data available yet for {symbol}."
                close, timestamp, provider = latest
                if parsed.action == "price":
                    return (
                        f"{symbol} latest close: {close:.2f} "
                        f"(provider={provider}, timestamp={timestamp})"
                    )

                sentiment = self.get_sentiment(symbol)
                social_sentiment = self.get_social_sentiment(symbol)
                headlines = self.get_latest_headlines(symbol)
                latest_two_closes = self.get_latest_two_closes(symbol)
                pct_change_text = "n/a"
                if len(latest_two_closes) == 2 and latest_two_closes[1] != 0:
                    pct_change = ((latest_two_closes[0] - latest_two_closes[1]) / latest_two_closes[1]) * 100
                    pct_change_text = f"{pct_change:+.2f}% vs prev close"

                filing = self.get_latest_sec_filing(symbol)
                filing_text = (
                    f"{filing[0]} filed {filing[1] or 'date n/a'}"
                    if filing
                    else "No recent SEC filing in local DB"
                )
                headline_lines = (
                    "\n".join(f"• {headline}" for headline in headlines)
                    if headlines
                    else "• No recent headlines"
                )
                source_line = "📦 Source: Preloaded Raspberry Pi data"
                if on_demand_result is not None:
                    source_line = (
                        "⚡ Source: On-demand Raspberry Pi refresh "
                        f"(prices={on_demand_result.get('price_bars', 0)}, "
                        f"news={on_demand_result.get('news_items', 0)}, "
                        f"social={on_demand_result.get('social_posts', 0)}, "
                        f"filings={on_demand_result.get('sec_filings', 0)})"
                    )
                return (
                    f"📊 {symbol} At-a-Glance\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💵 Latest close: {close:.2f} ({provider})\n"
                    f"🕒 Timestamp: {timestamp}\n"
                    f"📈 Momentum: {pct_change_text}\n"
                    f"🧠 News sentiment: {self._sentiment_label(sentiment)}\n"
                    f"💬 Social sentiment: {self._sentiment_label(social_sentiment)}\n"
                    f"🏛️ Financials: {filing_text}\n"
                    f"📰 Headlines:\n{headline_lines}\n"
                    f"{source_line}"
                )

            if parsed.action == "news":
                headlines = self.get_latest_headlines(symbol)
                if not headlines:
                    return f"No news data available yet for {symbol}."
                return f"Latest {symbol} headlines: " + " | ".join(headlines)

            if parsed.action == "sentiment":
                sentiment = self.get_sentiment(symbol)
                if sentiment is None:
                    return f"No sentiment data available yet for {symbol}."
                return f"{symbol} average news sentiment: {sentiment:.3f}"
        except sqlite3.Error as exc:
            return (
                f"Bridge database error: {exc}. "
                f"Verify --db-path points to the Raspberry Pi SQLite file."
            )

        return "Unknown command. Use Help for supported commands."


class PiBridgeHandler(BaseHTTPRequestHandler):
    service: PiBridgeService
    token: str

    def _read_json(self) -> Dict[str, Any]:
        content_len = int(self.headers.get("Content-Length", "0"))
        if content_len <= 0 or content_len > MAX_REQUEST_BODY_SIZE:
            return {}
        raw = self.rfile.read(content_len)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _authorized(self) -> bool:
        header_token = self.headers.get("X-Bridge-Token", "")
        return bool(self.token) and hmac.compare_digest(header_token, self.token)

    def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/command":
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        payload = self._read_json()
        text = str(payload.get("text", "")).strip()
        sender = str(payload.get("sender", "unknown")).strip()
        if not text:
            self._send_json(400, {"error": "missing text"})
            return

        response_text = self.service.build_response(text)
        self._send_json(
            200,
            {
                "ok": True,
                "sender": sender,
                "incoming_text": text,
                "response_text": response_text,
            },
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def run_pi_server(host: str, port: int, db_path: str, token: str) -> int:
    if not token:
        raise SystemExit("BRIDGE_TOKEN or --token is required for serve-pi")

    PiBridgeHandler.service = PiBridgeService(db_path)
    try:
        PiBridgeHandler.service.check_db_ready()
    except sqlite3.Error as exc:
        raise SystemExit(
            f"Unable to open bridge database at {PiBridgeHandler.service.db_path}: {exc}"
        ) from exc
    PiBridgeHandler.token = token
    server = ThreadingHTTPServer((host, port), PiBridgeHandler)
    print(f"Pi bridge listening on http://{host}:{port} (db={PiBridgeHandler.service.db_path})")
    server.serve_forever()
    return 0


def send_command_to_pi(
    pi_url: str,
    token: str,
    text: str,
    sender: str,
    timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    if not token:
        raise ValueError("token is required")

    url = pi_url.rstrip("/") + "/command"
    payload = json.dumps({"text": text, "sender": sender}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Bridge-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Pi bridge returned invalid JSON: {raw[:200]}") from exc


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MacBook/iOS to Raspberry Pi bridge")
    sub = parser.add_subparsers(dest="mode", required=True)

    serve = sub.add_parser("serve-pi", help="Run HTTP bridge on Raspberry Pi")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--db-path", default=os.environ.get("DB_PATH", "/home/pi/stockchecker_data.db"))
    serve.add_argument("--token", default=os.environ.get("BRIDGE_TOKEN", ""))

    send = sub.add_parser("send-mac", help="Forward command from MacBook/iOS bridge to Pi")
    send.add_argument("--pi-url", default=os.environ.get("PI_BRIDGE_URL", "http://raspberrypi.local:8787"))
    send.add_argument("--token", default=os.environ.get("BRIDGE_TOKEN", ""))
    send.add_argument("--text", required=True)
    send.add_argument("--sender", default="ios")
    send.add_argument("--timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT_SECONDS)

    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.mode == "serve-pi":
        return run_pi_server(args.host, args.port, args.db_path, args.token)

    if args.mode == "send-mac":
        try:
            response = send_command_to_pi(
                pi_url=args.pi_url,
                token=args.token,
                text=args.text,
                sender=args.sender,
                timeout=args.timeout,
            )
        except urllib.error.URLError as exc:
            raise SystemExit(f"Network error: {exc}") from exc

        print(json.dumps(response, indent=2))
        return 0

    parser.error("Invalid mode")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
