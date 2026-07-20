#!/usr/bin/env python3
"""Persist per-device sing-box traffic counters collected by nftables."""

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STATE_DIR = Path("/var/lib/traffic-stats")
STATE_DB = STATE_DIR / "traffic.sqlite3"
NFT_TABLE = ("inet", "traffic_stats")

# Keep the original 3x-ui composite identities so historical dashboard rows and
# new sing-box rows continue to aggregate into the same device cards.
DEVICES = {
    "windows-LUV": (1, 1, "windows-LUV", "windows pc"),
    "xiaomi-pad7-pro": (2, 1, "xiaomi-pad7-pro", "Android"),
    "iPhone17": (3, 1, "iPhone17", "iOS"),
    # The authentication name was intentionally kept as deployed in sing-box.
    "xiaomi-tubro4-pro": (4, 1, "xiaomi-turbo4", "Android"),
}


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


def read_nft_counters():
    result = subprocess.run(
        ["/usr/sbin/nft", "--json", "list", "table", *NFT_TABLE],
        check=True,
        capture_output=True,
        text=True,
    )
    document = json.loads(result.stdout)
    totals = {device: {"upload": 0, "download": 0} for device in DEVICES}
    found = set()
    for item in document.get("nftables", []):
        rule = item.get("rule")
        if not rule:
            continue
        comment = rule.get("comment", "")
        if not comment.startswith("traffic-stats:"):
            continue
        _, device, direction = comment.split(":", 2)
        if device not in totals or direction not in totals[device]:
            raise RuntimeError(f"unexpected nft counter comment: {comment}")
        counter = next(
            (expression["counter"] for expression in rule.get("expr", []) if "counter" in expression),
            None,
        )
        if counter is None:
            raise RuntimeError(f"nft rule has no counter: {comment}")
        totals[device][direction] = int(counter["bytes"])
        found.add((device, direction))
    expected = {(device, direction) for device in DEVICES for direction in ("upload", "download")}
    if found != expected:
        missing = ", ".join(f"{device}/{direction}" for device, direction in sorted(expected - found))
        raise RuntimeError(f"missing nft traffic counters: {missing}")
    return totals


def main():
    counters = read_nft_counters()
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
        for auth_name, (client_id, inbound_id, email, label) in DEVICES.items():
            # nft upload is client -> destination traffic; download is the reverse.
            up = counters[auth_name]["upload"]
            down = counters[auth_name]["download"]
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
                # nftables counters restart after reboot or a rules reload.
                delta_up = up - previous_up if up >= previous_up else up
                delta_down = down - previous_down if down >= previous_down else down
                daily_deltas.append((day, client_id, inbound_id, email, label, delta_up, delta_down))
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
        con.execute("DELETE FROM samples WHERE captured_at < ?", (now - 30 * 86400,))
        con.execute("DELETE FROM five_min_usage WHERE bucket_at < ?", (now - 183 * 86400,))
        con.commit()
    finally:
        con.close()

    print(f"captured {len(DEVICES)} sing-box device counters at {now}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"traffic-stats collector failed: {exc}", file=sys.stderr)
        sys.exit(1)
