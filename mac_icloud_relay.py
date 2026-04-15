#!/usr/bin/env python3
"""Poll Mac Messages DB for incoming iCloud sender texts and relay to Raspberry Pi bridge."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
from typing import Optional

from macbook_raspi_bridge import DEFAULT_REQUEST_TIMEOUT_SECONDS, send_command_to_pi


DEFAULT_MESSAGES_DB_PATH = "~/Library/Messages/chat.db"
DEFAULT_PI_BRIDGE_URL = "http://raspberrypi.local:8787"


def _normalized_messages_db_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _connect_messages_db(path: str) -> sqlite3.Connection:
    db_path = _normalized_messages_db_path(path)
    if not os.path.exists(db_path):
        raise SystemExit(f"Messages DB not found at {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("SELECT 1")
        return conn
    except sqlite3.Error as exc:
        raise SystemExit(
            "Cannot access Messages database. On macOS, grant your terminal Full Disk Access "
            f"and verify this path exists: {db_path}. Details: {exc}"
        ) from exc


def _latest_seen_rowid(conn: sqlite3.Connection, icloud_sender: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(message.ROWID), 0)
        FROM message
        JOIN handle ON handle.ROWID = message.handle_id
        WHERE message.is_from_me = 0
          AND handle.id = ?
        """,
        (icloud_sender,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _send_imessage_reply(recipient: str, text: str) -> None:
    script = """
on run argv
  set recipient to item 1 of argv
  set outgoingText to item 2 of argv
  tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy recipient of targetService
    send outgoingText to targetBuddy
  end tell
end run
"""
    completed = subprocess.run(
        ["osascript", "-e", script, recipient, text],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "").strip()
        print(f"Warning: failed to send iMessage reply to {recipient}: {details}")


def _fetch_new_incoming_texts(
    conn: sqlite3.Connection, icloud_sender: str, min_rowid_exclusive: int
) -> list[tuple[int, str]]:
    rows = conn.execute(
        """
        SELECT message.ROWID, COALESCE(message.text, '')
        FROM message
        JOIN handle ON handle.ROWID = message.handle_id
        WHERE message.is_from_me = 0
          AND handle.id = ?
          AND message.ROWID > ?
        ORDER BY message.ROWID ASC
        """,
        (icloud_sender, min_rowid_exclusive),
    ).fetchall()
    return [(int(row[0]), str(row[1]).strip()) for row in rows]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Relay new iMessage texts from a specific iCloud sender to Raspberry Pi."
    )
    parser.add_argument("--icloud-sender", required=True, help="Sender iCloud address to watch")
    parser.add_argument("--pi-url", default=os.environ.get("PI_BRIDGE_URL", DEFAULT_PI_BRIDGE_URL))
    parser.add_argument("--token", default=os.environ.get("BRIDGE_TOKEN", ""))
    parser.add_argument("--messages-db-path", default=DEFAULT_MESSAGES_DB_PATH)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    parser.add_argument("--relay-sender-name", default="icloud")
    parser.add_argument("--reply-to-imessage", action="store_true")
    parser.add_argument("--process-existing", action="store_true")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if not args.token:
        raise SystemExit("BRIDGE_TOKEN or --token is required")
    if args.poll_seconds <= 0:
        raise SystemExit("--poll-seconds must be > 0")

    conn = _connect_messages_db(args.messages_db_path)
    try:
        last_rowid = 0 if args.process_existing else _latest_seen_rowid(conn, args.icloud_sender)
        print(
            "Listening for new messages "
            f"from {args.icloud_sender} (starting_after_rowid={last_rowid})"
        )
        while True:
            for rowid, text in _fetch_new_incoming_texts(conn, args.icloud_sender, last_rowid):
                last_rowid = max(last_rowid, rowid)
                if not text:
                    continue
                try:
                    response = send_command_to_pi(
                        pi_url=args.pi_url,
                        token=args.token,
                        text=text,
                        sender=args.relay_sender_name,
                        timeout=args.timeout,
                    )
                except urllib.error.URLError as exc:
                    print(f"Relay failed (rowid={rowid}): {exc}")
                    continue

                response_text: Optional[str] = None
                if isinstance(response, dict):
                    raw = response.get("response_text")
                    response_text = str(raw) if raw is not None else None
                print(
                    json.dumps(
                        {
                            "rowid": rowid,
                            "incoming_text": text,
                            "pi_response": response,
                        },
                        ensure_ascii=False,
                    )
                )
                if args.reply_to_imessage and response_text:
                    _send_imessage_reply(args.icloud_sender, response_text)
            time.sleep(args.poll_seconds)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
