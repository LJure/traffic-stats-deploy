#!/usr/bin/env python3
"""Store a minute-by-minute, read-only snapshot of 3x-ui client traffic."""

import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

XUI_DB = "file:/etc/x-ui/x-ui.db?mode=ro"
STATE_DIR = Path("/var/lib/traffic-stats")
STATE_DB = STATE_DIR / "traffic.sqlite3"
def init_db(con):
    con.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS samples (
            captured_at INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            inbound_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            label TEXT NOT NULL,
            up_bytes INTEGER NOT NULL,
            down_bytes INTEGER NOT NULL,
            PRIMARY KEY (captured_at, client_id, inbound_id)
        );
        CREATE INDEX IF NOT EXISTS samples_captured_at ON samples(captured_at);
        CREATE TABLE IF NOT EXISTS five_min_usage (
            bucket_at INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            inbound_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            label TEXT NOT NULL,
            up_bytes INTEGER NOT NULL DEFAULT 0,
            down_bytes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bucket_at, client_id, inbound_id)
        );
        CREATE INDEX IF NOT EXISTS five_min_usage_bucket_at ON five_min_usage(bucket_at);
        CREATE TABLE IF NOT EXISTS daily_usage (
            day TEXT NOT NULL,
            client_id INTEGER NOT NULL,
            inbound_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            label TEXT NOT NULL,
            up_bytes INTEGER NOT NULL DEFAULT 0,
            down_bytes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, client_id, inbound_id)
        );
        CREATE INDEX IF NOT EXISTS daily_usage_day ON daily_usage(day);
        CREATE TABLE IF NOT EXISTS collector_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )


def read_xui():
    con = sqlite3.connect(XUI_DB, uri=True)
    try:
        return con.execute(
            """
            SELECT c.id, ci.inbound_id, c.email, COALESCE(c.comment, ''),
                   COALESCE(t.up, 0), COALESCE(t.down, 0)
            FROM clients AS c
            JOIN client_inbounds AS ci ON ci.client_id = c.id
            JOIN inbounds AS i ON i.id = ci.inbound_id
            LEFT JOIN client_traffics AS t
                ON t.inbound_id = ci.inbound_id AND t.email = c.email
            WHERE c.enable = 1 AND i.enable = 1
            ORDER BY c.id, ci.inbound_id
            """
        ).fetchall()
    finally:
        con.close()


def main():
    rows = read_xui()
    if not rows:
        raise RuntimeError("3x-ui returned no active client traffic rows")

    STATE_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)
    now = int(time.time())
    con = sqlite3.connect(STATE_DB)
    try:
        init_db(con)
        samples = []
        daily_deltas = []
        five_min_deltas = []
        day = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).date().isoformat()
        five_min_bucket = now - (now % 300)
        for client_id, inbound_id, email, comment, up, down in rows:
            # In 3x-ui, email identifies the device and comment describes its type.
            label = comment or email
            up, down = int(up), int(down)
            previous = con.execute(
                """
                SELECT up_bytes, down_bytes FROM samples
                WHERE client_id = ? AND inbound_id = ? AND captured_at < ?
                ORDER BY captured_at DESC LIMIT 1
                """,
                (client_id, inbound_id, now),
            ).fetchone()
            if previous:
                previous_up, previous_down = previous
                # A lower counter means 3x-ui reset it; start the new counter segment.
                delta_up = up - previous_up if up >= previous_up else up
                delta_down = down - previous_down if down >= previous_down else down
                daily_deltas.append(
                    (day, client_id, inbound_id, email, label, delta_up, delta_down)
                )
                five_min_deltas.append(
                    (five_min_bucket, client_id, inbound_id, email, label, delta_up, delta_down)
                )
            samples.append((now, client_id, inbound_id, email, label, up, down))
        con.executemany(
            """
            INSERT OR REPLACE INTO samples
            (captured_at, client_id, inbound_id, email, label, up_bytes, down_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            samples,
        )
        con.executemany(
            """
            INSERT INTO daily_usage
            (day, client_id, inbound_id, email, label, up_bytes, down_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day, client_id, inbound_id) DO UPDATE SET
                email = excluded.email,
                label = excluded.label,
                up_bytes = daily_usage.up_bytes + excluded.up_bytes,
                down_bytes = daily_usage.down_bytes + excluded.down_bytes
            """,
            daily_deltas,
        )
        con.executemany(
            """
            INSERT INTO five_min_usage
            (bucket_at, client_id, inbound_id, email, label, up_bytes, down_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_at, client_id, inbound_id) DO UPDATE SET
                email = excluded.email,
                label = excluded.label,
                up_bytes = five_min_usage.up_bytes + excluded.up_bytes,
                down_bytes = five_min_usage.down_bytes + excluded.down_bytes
            """,
            five_min_deltas,
        )
        con.execute(
            "INSERT OR REPLACE INTO collector_state(key, value) VALUES ('last_success', ?)",
            (str(now),),
        )
        # Keep detailed samples for 30 days. Rollups will be added before this expires.
        con.execute("DELETE FROM samples WHERE captured_at < ?", (now - 30 * 86400,))
        # Keep 5-minute aggregates for about six months; daily totals remain indefinitely.
        con.execute("DELETE FROM five_min_usage WHERE bucket_at < ?", (now - 183 * 86400,))
        con.commit()
    finally:
        con.close()

    print(f"captured {len(rows)} active client rows at {now}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"traffic-stats collector failed: {exc}", file=sys.stderr)
        sys.exit(1)
