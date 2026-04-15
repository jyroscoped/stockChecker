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

The communication bridge is in `macbook_raspi_bridge.py`. Both the Raspberry Pi (server) and your Mac (client) must share the same secret token so only your Mac can call the Pi's API.

---

### Step 1 — Generate a shared secret token (do this once)

Run the following command on **either** machine (Pi or Mac) — it doesn't matter which. Copy the output; you'll paste it in the steps below.

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Example output:
```
a3f8c2e1d4b7a09e5f3c2b1d4e7a8f0c1b2d3e4f5a6b7c8d9e0f1a2b3c4d5e6
```

Keep this value — you need the **exact same string** on both the Pi and the Mac.

---

### Step 2 — Raspberry Pi side (server)

SSH into your Pi and run:

```bash
# Paste your generated token here
export BRIDGE_TOKEN="your_generated_token_here"

python3 macbook_raspi_bridge.py serve-pi \
  --host 0.0.0.0 \
  --port 8787 \
  --db-path /home/pi/stockchecker_data.db
```

You should see a message like `Serving on 0.0.0.0:8787`. Leave this terminal open.

> **To make the token permanent** (so you don't have to re-export after reboots):
> ```bash
> echo 'export BRIDGE_TOKEN="your_generated_token_here"' >> ~/.bashrc
> source ~/.bashrc
> ```
> Or add `BRIDGE_TOKEN=your_generated_token_here` to a `.env` file if you use one.

HTTP endpoints exposed by the Pi server:

| Endpoint | Method | Auth required |
|----------|--------|---------------|
| `/health` | GET | No |
| `/command` | POST | Yes — header `X-Bridge-Token` |

Supported command text values (sent in POST body):

- `Analyze $TICKER`
- `Price $TICKER`
- `News $TICKER`
- `Sentiment $TICKER`
- `Help`

`Analyze $TICKER` now returns a formatted at-a-glance response with section separators, emojis, price snapshot, momentum, sentiment, headlines, and latest SEC filing summary.  
If the ticker is outside the core preloaded set (`NVDA`, `AAPL`, `SPY`, `VOO`, `MSFT`), the Pi bridge performs an on-demand local ingestion refresh for that ticker before returning the analysis.

---

### Step 3 — Mac side (client forwarder)

Open a **new Terminal window on your Mac** and run:

```bash
# Use the SAME token you set on the Pi
export BRIDGE_TOKEN="your_generated_token_here"

python3 macbook_raspi_bridge.py send-mac \
  --pi-url http://raspberrypi.local:8787 \
  --text "Analyze $NVDA" \
  --sender "ios"
```

> **Tip:** Replace `raspberrypi.local` with your Pi's local IP address (e.g. `192.168.1.42`) if mDNS isn't resolving. Find it on the Pi with `hostname -I`.

The script prints a JSON response like:
```json
{"response_text": "NVDA — $890.12  +1.4% ..."}
```

`response_text` is what you route back through your BlueBubbles/iMessage responder flow.

> **To make the token permanent on Mac** (so you don't need to re-export):
> ```bash
> echo 'export BRIDGE_TOKEN="your_generated_token_here"' >> ~/.zshrc
> source ~/.zshrc
> ```
> (Use `~/.bash_profile` instead if your Mac uses bash.)

---

### Step 4 — Mac auto-relay for incoming iCloud messages

If you want your Mac to automatically relay new incoming iMessages from one iCloud sender to your Pi bridge, run:

```bash
# Use the SAME token you set on the Pi
export BRIDGE_TOKEN="your_generated_token_here"

python3 mac_icloud_relay.py \
  --icloud-sender your_icloud_sender@example.com \
  --pi-url http://raspberrypi.local:8787
```

Optional flags:

- `--reply-to-imessage` to send the Pi response back to that iMessage sender
- `--poll-seconds 2` to control polling frequency
- `--process-existing` to process older messages at startup (default is new messages only)
- `--messages-db-path ~/Library/Messages/chat.db` to override Messages DB location

> **Important (macOS permissions):** this script reads `~/Library/Messages/chat.db`. If access fails, grant Full Disk Access to Terminal (or your shell app) in System Settings → Privacy & Security → Full Disk Access.

---

### Quick reference — common errors

| Error message | Cause | Fix |
|---|---|---|
| `BRIDGE_TOKEN or --token is required for serve-pi` | Token not set | Run `export BRIDGE_TOKEN="..."` before the command, or pass `--token "..."` |
| `Unable to open bridge database ...` | Invalid `--db-path` or missing directory | Use an existing directory and point `--db-path` to the Pi SQLite file (example: `/home/pi/stockchecker_data.db`) |
| `401 Unauthorized` | Token mismatch between Pi and Mac | Make sure both sides use the exact same string |
| `Connection refused` | Pi server not running | Re-run the `serve-pi` command on the Pi |
| `Could not resolve host raspberrypi.local` | mDNS not working | Replace `raspberrypi.local` with the Pi's IP address |

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
