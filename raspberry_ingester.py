#!/usr/bin/env python3
"""Raspberry Pi data ingestion pipeline for stockChecker.

This script automates:
- time-series market data ingestion (Yahoo Finance + optional Alpha Vantage/Alpaca)
- SEC filing ingestion
- financial news scraping
- social sentiment scraping
- SQLite persistence for historical + near-live data
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_HEADERS = {
    "User-Agent": os.environ.get(
        "INGESTER_USER_AGENT",
        "stockChecker-ingester/1.0 (educational project; contact: local@raspberry.pi)",
    )
}


class SentimentAnalyzer:
    """Very lightweight lexicon sentiment for zero-dependency Pi execution."""

    POSITIVE = {
        "beat",
        "beats",
        "bull",
        "bullish",
        "gain",
        "gains",
        "growth",
        "buy",
        "strong",
        "upgrade",
        "upside",
        "record",
        "surge",
        "profit",
        "profits",
        "outperform",
    }
    NEGATIVE = {
        "miss",
        "misses",
        "bear",
        "bearish",
        "loss",
        "losses",
        "sell",
        "downgrade",
        "fraud",
        "risk",
        "lawsuit",
        "slump",
        "drop",
        "decline",
        "weak",
        "warning",
        "underperform",
        "bankrupt",
    }

    @classmethod
    def score_text(cls, text: str) -> float:
        words = [w.strip(".,!?;:\"'()[]{}") for w in text.lower().split()]
        pos = sum(1 for w in words if w in cls.POSITIVE)
        neg = sum(1 for w in words if w in cls.NEGATIVE)
        total = pos + neg
        if total == 0:
            return 0.0
        return round((pos - neg) / total, 4)


@dataclass
class PriceBar:
    provider: str
    symbol: str
    timestamp: str
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[float]
    raw_json: str


class DataStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS price_bars (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                fetched_at TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                UNIQUE(provider, symbol, timestamp)
            );

            CREATE TABLE IF NOT EXISTS sec_filings (
                id INTEGER PRIMARY KEY,
                cik TEXT NOT NULL,
                ticker TEXT,
                company_name TEXT,
                form TEXT NOT NULL,
                filed_at TEXT,
                accession_no TEXT NOT NULL,
                primary_doc TEXT,
                url TEXT,
                fetched_at TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                UNIQUE(accession_no, form)
            );

            CREATE TABLE IF NOT EXISTS news_items (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                symbol TEXT,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                published_at TEXT,
                summary TEXT,
                sentiment_score REAL,
                fetched_at TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                UNIQUE(source, url)
            );

            CREATE TABLE IF NOT EXISTS social_posts (
                id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL,
                symbol TEXT,
                community TEXT,
                author TEXT,
                title TEXT,
                body TEXT,
                url TEXT NOT NULL,
                posted_at TEXT,
                sentiment_score REAL,
                fetched_at TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                UNIQUE(platform, url)
            );

            CREATE TABLE IF NOT EXISTS raw_payloads (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                payload TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    @staticmethod
    def _now() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat()

    def insert_price_bars(self, bars: Iterable[PriceBar]) -> int:
        rows = [
            (
                b.provider,
                b.symbol,
                b.timestamp,
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                self._now(),
                b.raw_json,
            )
            for b in bars
        ]
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO price_bars
            (provider, symbol, timestamp, open, high, low, close, volume, fetched_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes

    def insert_sec_filings(self, filings: Iterable[Dict[str, Any]]) -> int:
        rows = [
            (
                f.get("cik", ""),
                f.get("ticker"),
                f.get("company_name"),
                f.get("form", ""),
                f.get("filed_at"),
                f.get("accession_no", ""),
                f.get("primary_doc"),
                f.get("url"),
                self._now(),
                json.dumps(f, ensure_ascii=False),
            )
            for f in filings
            if f.get("accession_no") and f.get("form")
        ]
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO sec_filings
            (cik, ticker, company_name, form, filed_at, accession_no, primary_doc, url, fetched_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes

    def insert_news_items(self, items: Iterable[Dict[str, Any]]) -> int:
        rows = [
            (
                i.get("source", "unknown"),
                i.get("symbol"),
                i.get("title", ""),
                i.get("url", ""),
                i.get("published_at"),
                i.get("summary"),
                i.get("sentiment_score"),
                self._now(),
                json.dumps(i, ensure_ascii=False),
            )
            for i in items
            if i.get("title") and i.get("url")
        ]
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO news_items
            (source, symbol, title, url, published_at, summary, sentiment_score, fetched_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes

    def insert_social_posts(self, posts: Iterable[Dict[str, Any]]) -> int:
        rows = [
            (
                p.get("platform", "unknown"),
                p.get("symbol"),
                p.get("community"),
                p.get("author"),
                p.get("title"),
                p.get("body"),
                p.get("url", ""),
                p.get("posted_at"),
                p.get("sentiment_score"),
                self._now(),
                json.dumps(p, ensure_ascii=False),
            )
            for p in posts
            if p.get("url")
        ]
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO social_posts
            (platform, symbol, community, author, title, body, url, posted_at, sentiment_score, fetched_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes

    def insert_raw_payload(self, source: str, endpoint: str, payload: Any) -> None:
        self.conn.execute(
            "INSERT INTO raw_payloads (source, endpoint, payload, fetched_at) VALUES (?, ?, ?, ?)",
            (source, endpoint, json.dumps(payload, ensure_ascii=False), self._now()),
        )
        self.conn.commit()


class IngestionClient:
    def __init__(self, timeout_seconds: int = 30):
        self.timeout_seconds = timeout_seconds
        self.logger = logging.getLogger(self.__class__.__name__)
        self._sec_ticker_index: Optional[Dict[str, Dict[str, Any]]] = None

    def fetch_json(self, url: str, headers: Optional[Dict[str, str]] = None) -> Any:
        req = urllib.request.Request(url, headers=headers or DEFAULT_HEADERS)
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def fetch_xml(self, url: str, headers: Optional[Dict[str, str]] = None) -> ET.Element:
        req = urllib.request.Request(url, headers=headers or DEFAULT_HEADERS)
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
            content = response.read().decode("utf-8", errors="ignore")
        return ET.fromstring(content)

    def fetch_yahoo_bars(self, symbol: str, interval: str = "1d", period: str = "3mo") -> List[PriceBar]:
        endpoint = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
            f"?interval={urllib.parse.quote(interval)}&range={urllib.parse.quote(period)}"
        )
        payload = self.fetch_json(endpoint)
        result = payload.get("chart", {}).get("result", [])
        if not result:
            return []
        data = result[0]
        timestamps = data.get("timestamp") or []
        quote = ((data.get("indicators") or {}).get("quote") or [{}])[0]
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        bars: List[PriceBar] = []
        for i, ts in enumerate(timestamps):
            timestamp = dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()
            bars.append(
                PriceBar(
                    provider="yahoo",
                    symbol=symbol.upper(),
                    timestamp=timestamp,
                    open=opens[i] if i < len(opens) else None,
                    high=highs[i] if i < len(highs) else None,
                    low=lows[i] if i < len(lows) else None,
                    close=closes[i] if i < len(closes) else None,
                    volume=volumes[i] if i < len(volumes) else None,
                    raw_json=json.dumps(data, ensure_ascii=False),
                )
            )
        return bars

    def fetch_alpha_vantage_bars(self, symbol: str, api_key: str) -> List[PriceBar]:
        endpoint = (
            "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY"
            f"&symbol={urllib.parse.quote(symbol)}&apikey={urllib.parse.quote(api_key)}"
        )
        payload = self.fetch_json(endpoint)
        series = payload.get("Time Series (Daily)", {})
        bars: List[PriceBar] = []
        for ts, row in series.items():
            bars.append(
                PriceBar(
                    provider="alpha_vantage",
                    symbol=symbol.upper(),
                    timestamp=f"{ts}T00:00:00+00:00",
                    open=_to_float(row.get("1. open")),
                    high=_to_float(row.get("2. high")),
                    low=_to_float(row.get("3. low")),
                    close=_to_float(row.get("4. close")),
                    volume=_to_float(row.get("5. volume")),
                    raw_json=json.dumps(row, ensure_ascii=False),
                )
            )
        return bars

    def fetch_alpaca_bars(self, symbol: str, key_id: str, secret_key: str) -> List[PriceBar]:
        endpoint = (
            "https://data.alpaca.markets/v2/stocks/"
            f"{urllib.parse.quote(symbol)}/bars?timeframe=1Day&limit=1000"
        )
        headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            **DEFAULT_HEADERS,
        }
        payload = self.fetch_json(endpoint, headers=headers)
        bars: List[PriceBar] = []
        for row in payload.get("bars", []):
            bars.append(
                PriceBar(
                    provider="alpaca",
                    symbol=symbol.upper(),
                    timestamp=row.get("t", ""),
                    open=_to_float(row.get("o")),
                    high=_to_float(row.get("h")),
                    low=_to_float(row.get("l")),
                    close=_to_float(row.get("c")),
                    volume=_to_float(row.get("v")),
                    raw_json=json.dumps(row, ensure_ascii=False),
                )
            )
        return bars

    def _ensure_sec_index(self) -> Dict[str, Dict[str, Any]]:
        if self._sec_ticker_index is not None:
            return self._sec_ticker_index
        payload = self.fetch_json("https://www.sec.gov/files/company_tickers.json", headers=DEFAULT_HEADERS)
        index: Dict[str, Dict[str, Any]] = {}
        for _, row in payload.items():
            ticker = str(row.get("ticker", "")).upper()
            if ticker:
                index[ticker] = row
        self._sec_ticker_index = index
        return index

    def fetch_sec_filings(self, ticker: str, max_items: int = 25) -> List[Dict[str, Any]]:
        index = self._ensure_sec_index()
        row = index.get(ticker.upper())
        if not row:
            return []
        cik_raw = row.get("cik_str")
        if cik_raw is None:
            return []
        cik_number = int(cik_raw)
        cik = str(cik_number).zfill(10)
        endpoint = f"https://data.sec.gov/submissions/CIK{cik}.json"
        payload = self.fetch_json(endpoint, headers=DEFAULT_HEADERS)
        recent = (payload.get("filings") or {}).get("recent") or {}

        forms = recent.get("form") or []
        accession_numbers = recent.get("accessionNumber") or []
        filed_dates = recent.get("filingDate") or []
        primary_docs = recent.get("primaryDocument") or []

        filings: List[Dict[str, Any]] = []
        for i, form in enumerate(forms[:max_items]):
            accession_no = accession_numbers[i] if i < len(accession_numbers) else ""
            accession_clean = accession_no.replace("-", "")
            primary_doc = primary_docs[i] if i < len(primary_docs) else None
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_number}/{accession_clean}/{primary_doc}"
                if accession_clean and primary_doc
                else None
            )
            filings.append(
                {
                    "cik": cik,
                    "ticker": ticker.upper(),
                    "company_name": row.get("title"),
                    "form": form,
                    "filed_at": filed_dates[i] if i < len(filed_dates) else None,
                    "accession_no": accession_no,
                    "primary_doc": primary_doc,
                    "url": url,
                }
            )
        return filings

    def fetch_yahoo_news(self, symbol: str, max_items: int = 30) -> List[Dict[str, Any]]:
        endpoint = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={urllib.parse.quote(symbol)}&region=US&lang=en-US"
        root = self.fetch_xml(endpoint)
        items: List[Dict[str, Any]] = []
        for item in root.findall("./channel/item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            summary = (item.findtext("description") or "").strip()
            text = f"{title} {summary}".strip()
            items.append(
                {
                    "source": "yahoo_finance_rss",
                    "symbol": symbol.upper(),
                    "title": title,
                    "url": (item.findtext("link") or "").strip(),
                    "published_at": (item.findtext("pubDate") or "").strip(),
                    "summary": summary,
                    "sentiment_score": SentimentAnalyzer.score_text(text),
                }
            )
        return items

    def fetch_reddit_posts(self, symbol: str, subreddit: str, max_items: int = 20) -> List[Dict[str, Any]]:
        query = urllib.parse.quote(symbol)
        endpoint = (
            f"https://www.reddit.com/r/{urllib.parse.quote(subreddit)}/search.rss"
            f"?q={query}&restrict_sr=on&sort=new"
        )
        root = self.fetch_xml(endpoint)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        items: List[Dict[str, Any]] = []
        for entry in root.findall("a:entry", ns)[:max_items]:
            title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
            content = (entry.findtext("a:content", default="", namespaces=ns) or "").strip()
            author_el = entry.find("a:author/a:name", ns)
            link_el = entry.find("a:link", ns)
            href = link_el.attrib.get("href") if link_el is not None else ""
            text = f"{title} {content}".strip()
            items.append(
                {
                    "platform": "reddit",
                    "symbol": symbol.upper(),
                    "community": subreddit,
                    "author": author_el.text.strip() if author_el is not None and author_el.text else None,
                    "title": title,
                    "body": content,
                    "url": href,
                    "posted_at": (entry.findtext("a:updated", default="", namespaces=ns) or "").strip(),
                    "sentiment_score": SentimentAnalyzer.score_text(text),
                }
            )
        return items


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_symbols(csv_symbols: str) -> List[str]:
    return [s.strip().upper() for s in csv_symbols.split(",") if s.strip()]


def run_cycle(
    store: DataStore,
    client: IngestionClient,
    symbols: List[str],
    alpha_vantage_api_key: Optional[str],
    alpaca_key_id: Optional[str],
    alpaca_secret_key: Optional[str],
    max_sec_filings: int,
) -> None:
    logger = logging.getLogger("run_cycle")
    for symbol in symbols:
        logger.info("Ingesting symbol=%s", symbol)

        try:
            yahoo_bars = client.fetch_yahoo_bars(symbol)
            store.insert_price_bars(yahoo_bars)
            store.insert_raw_payload("yahoo", f"chart:{symbol}", [b.__dict__ for b in yahoo_bars])
            logger.info("Saved %d Yahoo bars for %s", len(yahoo_bars), symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Yahoo fetch failed for %s: %s", symbol, exc)

        if alpha_vantage_api_key:
            try:
                av_bars = client.fetch_alpha_vantage_bars(symbol, alpha_vantage_api_key)
                store.insert_price_bars(av_bars)
                logger.info("Saved %d Alpha Vantage bars for %s", len(av_bars), symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Alpha Vantage fetch failed for %s: %s", symbol, exc)

        if alpaca_key_id and alpaca_secret_key:
            try:
                alpaca_bars = client.fetch_alpaca_bars(symbol, alpaca_key_id, alpaca_secret_key)
                store.insert_price_bars(alpaca_bars)
                logger.info("Saved %d Alpaca bars for %s", len(alpaca_bars), symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Alpaca fetch failed for %s: %s", symbol, exc)

        try:
            news = client.fetch_yahoo_news(symbol)
            store.insert_news_items(news)
            logger.info("Saved %d news items for %s", len(news), symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("News fetch failed for %s: %s", symbol, exc)

        social_total = 0
        for subreddit in ("stocks", "investing", "wallstreetbets"):
            try:
                posts = client.fetch_reddit_posts(symbol, subreddit)
                store.insert_social_posts(posts)
                social_total += len(posts)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Social fetch failed for %s in r/%s: %s", symbol, subreddit, exc)
        logger.info("Saved %d social posts for %s", social_total, symbol)

        try:
            filings = client.fetch_sec_filings(symbol, max_items=max_sec_filings)
            store.insert_sec_filings(filings)
            logger.info("Saved %d SEC filings for %s", len(filings), symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SEC fetch failed for %s: %s", symbol, exc)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Raspberry Pi data ingestion runner")
    parser.add_argument(
        "--symbols",
        default=os.environ.get("SYMBOLS", "AAPL,MSFT,NVDA,SPY"),
        help="Comma-separated ticker symbols",
    )
    parser.add_argument(
        "--db-path",
        default=os.environ.get("DB_PATH", "/home/pi/stockchecker_data.db"),
        help="SQLite database path",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=int(os.environ.get("INGEST_INTERVAL_SECONDS", "900")),
        help="Seconds between ingestion cycles in continuous mode",
    )
    parser.add_argument(
        "--max-sec-filings",
        type=int,
        default=int(os.environ.get("MAX_SEC_FILINGS", "25")),
        help="Maximum recent SEC filings per symbol per cycle",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single ingestion cycle and exit",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    symbols = _parse_symbols(args.symbols)
    if not symbols:
        parser.error("No symbols provided. Use --symbols AAPL,MSFT")

    alpha_vantage_api_key = os.environ.get("ALPHA_VANTAGE_API_KEY")
    alpaca_key_id = os.environ.get("ALPACA_KEY_ID")
    alpaca_secret_key = os.environ.get("ALPACA_SECRET_KEY")

    store = DataStore(args.db_path)
    client = IngestionClient(timeout_seconds=30)

    logger = logging.getLogger("main")
    logger.info("Starting ingestion for symbols=%s db=%s", symbols, args.db_path)

    if args.once:
        run_cycle(
            store=store,
            client=client,
            symbols=symbols,
            alpha_vantage_api_key=alpha_vantage_api_key,
            alpaca_key_id=alpaca_key_id,
            alpaca_secret_key=alpaca_secret_key,
            max_sec_filings=args.max_sec_filings,
        )
        return 0

    while True:
        start = time.time()
        run_cycle(
            store=store,
            client=client,
            symbols=symbols,
            alpha_vantage_api_key=alpha_vantage_api_key,
            alpaca_key_id=alpaca_key_id,
            alpaca_secret_key=alpaca_secret_key,
            max_sec_filings=args.max_sec_filings,
        )
        elapsed = int(time.time() - start)
        sleep_for = max(0, args.interval_seconds - elapsed)
        logger.info("Cycle finished in %ss, sleeping %ss", elapsed, sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        logging.getLogger("main").error("Network error: %s", exc)
        raise
