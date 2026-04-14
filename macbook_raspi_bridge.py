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
from typing import Any, Dict, Optional, Set, Tuple


TICKER_SYMBOL_PATTERN = re.compile(r"\$([A-Za-z][A-Za-z0-9\.-]{0,9})")
MAX_REQUEST_BODY_SIZE = 10_000
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_ALLOWED_SENDER = "goldbergerkids@icloud.com"


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
    if lower.startswith("updates"):
        return ParsedCommand("updates", symbol)
    if lower.startswith("filings"):
        return ParsedCommand("filings", symbol)
    if lower.startswith("help"):
        return ParsedCommand("help", symbol)
    return ParsedCommand("unknown", symbol)


def _normalize_sender(value: str) -> str:
    return value.strip().lower()


def parse_allowed_senders(raw: str) -> Set[str]:
    parsed = {_normalize_sender(item) for item in raw.split(",") if item.strip()}
    return parsed or {_normalize_sender(DEFAULT_ALLOWED_SENDER)}


def is_sender_allowed(sender: str, allowed_senders: Set[str]) -> bool:
    return _normalize_sender(sender) in allowed_senders


class PiBridgeService:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _query_one(self, sql: str, params: Tuple[Any, ...]) -> Optional[Tuple[Any, ...]]:
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(sql, params)
            return cur.fetchone()
        finally:
            conn.close()

    def _query_many(self, sql: str, params: Tuple[Any, ...]) -> list[Tuple[Any, ...]]:
        conn = sqlite3.connect(self.db_path)
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

    def get_sentiment(self, symbol: str) -> Optional[float]:
        row = self._query_one(
            """
            SELECT AVG(sentiment_score)
            FROM news_items
            WHERE symbol = ? AND sentiment_score IS NOT NULL
            """,
            (symbol.upper(),),
        )
        if not row or row[0] is None:
            return None
        return float(row[0])

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

    def get_recent_sec_filings(self, symbol: str, limit: int = 3) -> list[Tuple[str, str, str]]:
        rows = self._query_many(
            """
            SELECT form, COALESCE(filed_at, ''), COALESCE(url, '')
            FROM sec_filings
            WHERE UPPER(ticker) = ?
            ORDER BY COALESCE(filed_at, '') DESC, id DESC
            LIMIT ?
            """,
            (symbol.upper(), limit),
        )
        return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]

    def _build_analysis_text(self, symbol: str, close: float, timestamp: str, provider: str) -> str:
        sentiment = self.get_sentiment(symbol)
        sentiment_text = "n/a" if sentiment is None else f"{sentiment:.3f}"
        headlines = self.get_latest_headlines(symbol)
        headline_text = " | ".join(headlines) if headlines else "No recent headlines"
        filings = self.get_recent_sec_filings(symbol)
        filing_text = (
            " | ".join([f"{form} ({filed_at}) {url}".strip() for form, filed_at, url in filings])
            if filings
            else "No recent SEC filings"
        )
        return (
            f"Analysis for {symbol}: latest close={close:.2f} ({provider}) at {timestamp}; "
            f"avg news sentiment={sentiment_text}; headlines={headline_text}; sec_filings={filing_text}"
        )

    def build_response(self, text: str) -> str:
        parsed = parse_imessage_command(text)
        if parsed.action == "help":
            return (
                "Commands: Analyze $TICKER, Price $TICKER, News $TICKER, "
                "Sentiment $TICKER, Filings $TICKER, Updates $TICKER"
            )

        if not parsed.symbol:
            return "No ticker found. Try: Analyze $NVDA"

        symbol = parsed.symbol
        if parsed.action in {"analyze", "price", "updates"}:
            latest = self.get_latest_price(symbol)
            if not latest:
                return f"No price data available yet for {symbol}."
            close, timestamp, provider = latest
            if parsed.action == "price":
                return (
                    f"{symbol} latest close: {close:.2f} "
                    f"(provider={provider}, timestamp={timestamp})"
                )

            return self._build_analysis_text(symbol, close, timestamp, provider)

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

        if parsed.action == "filings":
            filings = self.get_recent_sec_filings(symbol)
            if not filings:
                return f"No SEC filing data available yet for {symbol}."
            filing_text = " | ".join([f"{form} ({filed_at}) {url}".strip() for form, filed_at, url in filings])
            return f"Latest {symbol} SEC filings: {filing_text}"

        return "Unknown command. Use Help for supported commands."


class PiBridgeHandler(BaseHTTPRequestHandler):
    service: PiBridgeService
    token: str
    allowed_senders: Set[str]

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
        if not is_sender_allowed(sender, self.allowed_senders):
            self._send_json(403, {"error": "forbidden sender"})
            return
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


def run_pi_server(host: str, port: int, db_path: str, token: str, allowed_senders: Set[str]) -> int:
    if not token:
        raise SystemExit("BRIDGE_TOKEN or --token is required for serve-pi")

    PiBridgeHandler.service = PiBridgeService(db_path)
    PiBridgeHandler.token = token
    PiBridgeHandler.allowed_senders = allowed_senders
    server = ThreadingHTTPServer((host, port), PiBridgeHandler)
    print(f"Pi bridge listening on http://{host}:{port} (db={db_path})")
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
    serve.add_argument(
        "--allowed-senders",
        default=os.environ.get("BRIDGE_ALLOWED_SENDERS", DEFAULT_ALLOWED_SENDER),
        help="Comma-separated sender IDs allowed to issue commands",
    )

    send = sub.add_parser("send-mac", help="Forward command from MacBook/iOS bridge to Pi")
    send.add_argument("--pi-url", default=os.environ.get("PI_BRIDGE_URL", "http://raspberrypi.local:8787"))
    send.add_argument("--token", default=os.environ.get("BRIDGE_TOKEN", ""))
    send.add_argument("--text", required=True)
    send.add_argument("--sender", default=DEFAULT_ALLOWED_SENDER)
    send.add_argument("--timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT_SECONDS)

    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.mode == "serve-pi":
        return run_pi_server(
            args.host,
            args.port,
            args.db_path,
            args.token,
            parse_allowed_senders(args.allowed_senders),
        )

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
