# stockChecker

Stock Checker and Updates via iMessage using Raspberry Pi, Windows, and Mac.

---

## Raspberry Pi First: Quantitative Financial Research Data Ingestion

This repository now includes a **Raspberry Pi-ready Python ingestion service** that handles the full Phase 2 ingestion scope from your plan:

- automated scheduled scraping/ingestion
- stock API time-series collection (Yahoo Finance + optional Alpha Vantage + optional Alpaca)
- SEC filing ingestion
- social/news scraping with sentiment scoring
- local historical/live database persistence (SQLite)

The script is intentionally **zero third-party dependency** so it can run immediately on a fresh Pi with Python 3.

---

## File Overview

- `raspberry_ingester.py`  
  Main ingestion service.
- `macbook_raspi_bridge.py`  
  Communication bridge script between MacBook/iOS command source and Raspberry Pi datastore.
- `tests/test_raspberry_ingester.py`  
  Focused unit tests for sentiment parsing, symbol parsing, schema creation, and Yahoo parsing.
- `tests/test_macbook_raspi_bridge.py`  
  Focused unit tests for iMessage command parsing and bridge response generation.

---

## What the Raspberry Script Does

### 1) Automated scraping/ingestion loop

The script supports:

- `--once` for a single ingestion run
- continuous mode with `--interval-seconds` for always-on ingestion on Raspberry Pi

### 2) Stock API ingestion

#### Yahoo Finance (no key required)

- Pulls OHLCV bars from Yahoo chart API for each symbol
- Stores bars in `price_bars` with provider=`yahoo`

#### Alpha Vantage (optional)

- Enabled when `ALPHA_VANTAGE_API_KEY` is set
- Pulls daily bars and stores with provider=`alpha_vantage`

#### Alpaca (optional)

- Enabled when both `ALPACA_KEY_ID` and `ALPACA_SECRET_KEY` are set
- Pulls bars and stores with provider=`alpaca`

### 3) SEC filing scraper

- Maps ticker -> CIK via SEC `company_tickers.json`
- Pulls latest submissions from `data.sec.gov`
- Stores recent filings in `sec_filings` with accession # and direct EDGAR URL

### 4) Financial news scraper + sentiment

- Pulls Yahoo Finance RSS headlines per symbol
- Scores each item with a lightweight sentiment lexicon
- Stores article metadata and sentiment in `news_items`

### 5) Social sentiment scraper

- Pulls Reddit RSS search results in:
  - `r/stocks`
  - `r/investing`
  - `r/wallstreetbets`
- Applies sentiment scoring to title/body text
- Stores posts in `social_posts`

### 6) Local database for historical + live data

SQLite schema is initialized automatically on startup. Tables:

- `price_bars`
- `sec_filings`
- `news_items`
- `social_posts`
- `raw_payloads`

Data is inserted with uniqueness constraints to avoid duplicate rows.

---

## Quick Start (Raspberry Pi)

### 0) Confirm Python

```bash
python3 --version
```

Python 3.9+ recommended.

### 1) Run one ingestion cycle

```bash
cd /path/to/stockChecker
python3 raspberry_ingester.py --once --symbols AAPL,MSFT,NVDA --db-path ./stockchecker_data.db
```

### 2) Run continuously (always-on)

```bash
cd /path/to/stockChecker
python3 raspberry_ingester.py --symbols AAPL,MSFT,NVDA,SPY --db-path ./stockchecker_data.db --interval-seconds 900
```

---

## Environment Variables

All are optional unless you want those providers:

- `SYMBOLS` (default: `AAPL,MSFT,NVDA,SPY`)
- `DB_PATH` (default: `/home/pi/stockchecker_data.db`)
- `INGEST_INTERVAL_SECONDS` (default: `900`)
- `MAX_SEC_FILINGS` (default: `25`)
- `LOG_LEVEL` (`DEBUG|INFO|WARNING|ERROR`, default: `INFO`)

API keys (optional):

- `ALPHA_VANTAGE_API_KEY`
- `ALPACA_KEY_ID`
- `ALPACA_SECRET_KEY`

HTTP user agent override (recommended for SEC politeness):

- `INGESTER_USER_AGENT`

Example:

```bash
export ALPHA_VANTAGE_API_KEY="your_key_here"
export ALPACA_KEY_ID="your_alpaca_key"
export ALPACA_SECRET_KEY="your_alpaca_secret"
export INGESTER_USER_AGENT="stockChecker-ingester/1.0 (you@email.com)"
```

---

## CLI Reference

```bash
python3 raspberry_ingester.py --help
```

Arguments:

- `--symbols` comma-separated tickers
- `--db-path` SQLite file path
- `--interval-seconds` cycle interval in continuous mode
- `--max-sec-filings` max recent filings per symbol per cycle
- `--once` run once and exit
- `--log-level` logging level

---

## MacBook/iOS ↔ Raspberry Pi Communication Script

The communication bridge is in `macbook_raspi_bridge.py`.

### Raspberry Pi side (server)

Run this on the Raspberry Pi to expose an authenticated endpoint that reads from the local SQLite data ingested by `raspberry_ingester.py`:

```bash
export BRIDGE_TOKEN="replace_with_long_random_token"
python3 macbook_raspi_bridge.py serve-pi --host 0.0.0.0 --port 8787 --db-path /home/pi/stockchecker_data.db
```

HTTP endpoints:

- `GET /health`
- `POST /command` (requires header `X-Bridge-Token`)

Example command JSON payload:

```json
{"text":"Analyze $NVDA","sender":"ios"}
```

Supported commands:

- `Analyze $TICKER`
- `Price $TICKER`
- `News $TICKER`
- `Sentiment $TICKER`
- `Help`

### MacBook/BlueBubbles side (client forwarder)

Use this on the MacBook side to forward parsed iMessage text to the Raspberry Pi:

```bash
export BRIDGE_TOKEN="replace_with_same_token_used_on_pi"
python3 macbook_raspi_bridge.py send-mac --pi-url http://raspberrypi.local:8787 --text "Analyze $NVDA" --sender "ios"
```

The script prints JSON containing `response_text`, which can be sent back through your BlueBubbles/iMessage responder flow.

---

## Database Schema Notes

### `price_bars`

Time-series OHLCV bars. Uniqueness on `(provider, symbol, timestamp)`.

### `sec_filings`

Recent filings per ticker with form type, filing date, accession #, and EDGAR URL.

### `news_items`

Financial news metadata + sentiment score.

### `social_posts`

Reddit social mentions + sentiment score.

### `raw_payloads`

Raw payload snapshots for auditability/debugging.

---

## Running Tests

This project currently uses built-in `unittest` for the ingestion script tests.

```bash
cd /path/to/stockChecker
python3 -m unittest discover -s tests -v
```

---

## Productionizing on Raspberry Pi (Recommended)

Use a systemd service for always-on behavior.

Example unit file (`/etc/systemd/system/stockchecker-ingester.service`):

```ini
[Unit]
Description=stockChecker Raspberry Pi Ingestion Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/stockChecker
Environment=SYMBOLS=AAPL,MSFT,NVDA,SPY
Environment=DB_PATH=/home/pi/stockchecker_data.db
Environment=INGEST_INTERVAL_SECONDS=900
Environment=LOG_LEVEL=INFO
ExecStart=/usr/bin/python3 /home/pi/stockChecker/raspberry_ingester.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable/start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable stockchecker-ingester
sudo systemctl start stockchecker-ingester
sudo systemctl status stockchecker-ingester
```

---

## Mapping to Your To-Do (Raspberry Scope)

Implemented now in script:

- ✅ Python automated scraping workflow
- ✅ Stock API ingestion (Yahoo + optional Alpaca/Alpha Vantage)
- ✅ SEC filing scraper
- ✅ Social sentiment + financial news scraping
- ✅ Historical/live database storage

Planned next phases (outside this script):

- GPU/NLP training + backtesting on Windows PC
- BlueBubbles API + iMessage command/response on Mac
- Portfolio analytics + research paper + polished project media

---

## Important Notes

- Respect API provider rate limits/terms.
- SEC requests should include a responsible user-agent/contact string.
- This script is an ingestion foundation; advanced model training/backtesting is intended for your Windows GPU phase.
