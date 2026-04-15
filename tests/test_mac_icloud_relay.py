import sqlite3
import unittest

from mac_icloud_relay import _fetch_new_incoming_texts


class MacIcloudRelayTests(unittest.TestCase):
    def _make_conn_with_messages(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT)")
        conn.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, is_from_me INTEGER)")
        conn.execute(
            "CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER)"
        )
        conn.execute(
            "INSERT INTO chat (ROWID, chat_identifier) VALUES (?, ?)",
            (1, "myself@example.com"),
        )
        conn.execute(
            "INSERT INTO message (ROWID, text, is_from_me) VALUES (?, ?, ?)",
            (10, "Analyze $NVDA", 0),
        )
        conn.execute(
            "INSERT INTO message (ROWID, text, is_from_me) VALUES (?, ?, ?)",
            (11, "Unknown command. Use Help for supported commands.", 1),
        )
        conn.execute(
            "INSERT INTO message (ROWID, text, is_from_me) VALUES (?, ?, ?)",
            (12, "analyze $AAPL", 1),
        )
        conn.execute(
            "INSERT INTO message (ROWID, text, is_from_me) VALUES (?, ?, ?)",
            (13, "hello there", 0),
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            (1, 10),
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            (1, 11),
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            (1, 12),
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            (1, 13),
        )
        return conn

    def test_fetch_new_incoming_texts_includes_supported_self_commands(self):
        conn = self._make_conn_with_messages()
        try:
            rows = _fetch_new_incoming_texts(conn, "myself@example.com", 0)
        finally:
            conn.close()
        self.assertEqual(rows, [(10, "Analyze $NVDA"), (12, "analyze $AAPL"), (13, "hello there")])

    def test_fetch_new_incoming_texts_respects_min_rowid(self):
        conn = self._make_conn_with_messages()
        try:
            rows = _fetch_new_incoming_texts(conn, "myself@example.com", 10)
        finally:
            conn.close()
        self.assertEqual(rows, [(12, "analyze $AAPL"), (13, "hello there")])


if __name__ == "__main__":
    unittest.main()
