"""Durable SQLite storage: listings, price snapshots, alert state/fingerprints, source health, outbox."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ps5monitor.models import Decision, Offer, Status, Stock
from ps5monitor.rules import AlertState

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    listing_id TEXT PRIMARY KEY, source TEXT, retailer TEXT, seller TEXT, url TEXT, title TEXT,
    variant TEXT, is_bundle INTEGER, first_seen TEXT, last_seen TEXT, last_price TEXT, last_shipping TEXT,
    last_total TEXT, last_stock TEXT, last_status TEXT, last_reasons TEXT, lowest_total TEXT,
    prev_total TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT, ts TEXT, price TEXT, shipping TEXT,
    total TEXT, stock TEXT, status TEXT
);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(listing_id, ts);
CREATE TABLE IF NOT EXISTS alert_state (
    listing_id TEXT PRIMARY KEY, last_alert_price TEXT, last_alert_at TEXT, armed INTEGER,
    last_exit_reason TEXT, last_kind TEXT
);
CREATE TABLE IF NOT EXISTS sent_alerts (
    fingerprint TEXT PRIMARY KEY, ts TEXT, listing_id TEXT, kind TEXT, total TEXT
);
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY, last_attempt TEXT, last_success TEXT, consecutive_failures INTEGER,
    last_error TEXT, backoff_until TEXT, down_alert_sent INTEGER, last_offer_count INTEGER
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, priority INTEGER, text TEXT, buttons TEXT,
    attempts INTEGER DEFAULT 0, sent_at TEXT, last_error TEXT
);
"""


def _ts(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat(timespec="seconds") if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _d(s: str | None) -> Decimal | None:
    return Decimal(s) if s not in (None, "") else None


def _s(d: Decimal | None) -> str | None:
    return None if d is None else f"{d:.2f}"


@dataclass
class Health:
    source: str
    last_attempt: datetime | None
    last_success: datetime | None
    consecutive_failures: int
    last_error: str | None
    backoff_until: datetime | None
    down_alert_sent: bool
    last_offer_count: int


@dataclass
class ListingRow:
    listing_id: str
    source: str
    retailer: str
    seller: str | None
    url: str
    title: str
    variant: str
    is_bundle: bool
    last_seen: datetime
    last_total: Decimal | None
    last_price: Decimal | None
    last_stock: str
    last_status: str
    lowest_total: Decimal | None
    prev_total: Decimal | None


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    # ------------------------------------------------------------- kv
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM kv WHERE key=?", (key,))
        else:
            self.conn.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value))

    # ------------------------------------------------------------- listings
    def get_listing(self, listing_id: str) -> ListingRow | None:
        r = self.conn.execute("SELECT * FROM listings WHERE listing_id=?", (listing_id,)).fetchone()
        return self._row(r) if r else None

    def _row(self, r: sqlite3.Row) -> ListingRow:
        return ListingRow(
            r["listing_id"], r["source"], r["retailer"], r["seller"], r["url"], r["title"], r["variant"],
            bool(r["is_bundle"]), _dt(r["last_seen"]) or datetime.now(UTC), _d(r["last_total"]),
            _d(r["last_price"]), r["last_stock"], r["last_status"], _d(r["lowest_total"]), _d(r["prev_total"]),
        )

    def upsert_listing(self, offer: Offer, decision: Decision) -> ListingRow | None:
        """Store the observation; returns the previous row (or None if new)."""
        prev = self.get_listing(offer.listing_id)
        total = decision.total if decision.total is not None else offer.total
        lowest = prev.lowest_total if prev else None
        if total is not None and offer.stock == Stock.IN_STOCK and (lowest is None or total < lowest):
            lowest = total
        prev_total = prev.last_total if prev and prev.last_total != total else (prev.prev_total if prev else None)
        now = _ts(offer.checked_at)
        self.conn.execute(
            """INSERT INTO listings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(listing_id) DO UPDATE SET seller=excluded.seller, url=excluded.url,
            title=excluded.title, last_seen=excluded.last_seen, last_price=excluded.last_price,
            last_shipping=excluded.last_shipping, last_total=excluded.last_total, last_stock=excluded.last_stock,
            last_status=excluded.last_status, last_reasons=excluded.last_reasons,
            lowest_total=excluded.lowest_total, prev_total=excluded.prev_total""",
            (offer.listing_id, offer.source, offer.retailer, offer.seller, offer.url, offer.title,
             offer.variant.value, int(offer.is_bundle), now, now, _s(offer.item_price), _s(offer.shipping),
             _s(total), offer.stock.value, decision.status.value, json.dumps(decision.reasons, ensure_ascii=False),
             _s(lowest), _s(prev_total)),
        )
        changed = prev is None or prev.last_total != total or prev.last_stock != offer.stock.value \
            or prev.last_status != decision.status.value
        if changed:
            self.conn.execute(
                "INSERT INTO snapshots(listing_id,ts,price,shipping,total,stock,status) VALUES(?,?,?,?,?,?,?)",
                (offer.listing_id, now, _s(offer.item_price), _s(offer.shipping), _s(total), offer.stock.value,
                 decision.status.value),
            )
        return prev

    def qualifying(self, statuses: tuple[Status, ...] = (Status.ELIGIBLE,),
                   max_age: timedelta | None = None) -> list[ListingRow]:
        rows = self.conn.execute(
            f"SELECT * FROM listings WHERE last_status IN ({','.join('?' * len(statuses))})",
            [s.value for s in statuses],
        ).fetchall()
        out = [self._row(r) for r in rows]
        if max_age is not None:
            cutoff = datetime.now(UTC) - max_age
            out = [r for r in out if r.last_seen >= cutoff]
        return sorted(out, key=lambda r: (r.last_total is None, r.last_total or Decimal(0)))

    def global_lowest_alerted(self) -> Decimal | None:
        totals = [_d(r["total"]) for r in self.conn.execute(
            "SELECT DISTINCT total FROM sent_alerts WHERE total IS NOT NULL").fetchall()]
        values = [t for t in totals if t is not None]
        return min(values) if values else None

    def history_since(self, listing_id: str) -> datetime | None:
        row = self.conn.execute("SELECT MIN(ts) AS t FROM snapshots WHERE listing_id=?", (listing_id,)).fetchone()
        return _dt(row["t"]) if row and row["t"] else None

    def prune(self, keep_days: int = 180) -> None:
        cutoff = _ts(datetime.now(UTC) - timedelta(days=keep_days))
        self.conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
        self.conn.execute("DELETE FROM sent_alerts WHERE ts < ?", (cutoff,))
        self.conn.execute("DELETE FROM outbox WHERE sent_at IS NOT NULL AND sent_at < ?", (cutoff,))

    # ------------------------------------------------------------- alerts
    def alert_state(self, listing_id: str) -> AlertState | None:
        r = self.conn.execute("SELECT * FROM alert_state WHERE listing_id=?", (listing_id,)).fetchone()
        if not r:
            return None
        return AlertState(_d(r["last_alert_price"]), _dt(r["last_alert_at"]), bool(r["armed"]),
                          r["last_exit_reason"])

    def record_alert(self, listing_id: str, kind: str, total: Decimal, at: datetime, fingerprint: str) -> bool:
        """Record an alert; returns False if this exact fingerprint was already recorded (dedupe)."""
        try:
            self.conn.execute("INSERT INTO sent_alerts VALUES(?,?,?,?,?)",
                              (fingerprint, _ts(at), listing_id, kind, _s(total)))
        except sqlite3.IntegrityError:
            return False
        self.conn.execute(
            """INSERT INTO alert_state VALUES(?,?,?,0,NULL,?) ON CONFLICT(listing_id) DO UPDATE SET
            last_alert_price=excluded.last_alert_price, last_alert_at=excluded.last_alert_at, armed=0,
            last_exit_reason=NULL, last_kind=excluded.last_kind""",
            (listing_id, _s(total), _ts(at), kind),
        )
        return True

    def mark_left_eligibility(self, listing_id: str, reason: str) -> None:
        """Arm a previously alerted listing so that its return can be alerted again."""
        self.conn.execute(
            "UPDATE alert_state SET armed=1, last_exit_reason=COALESCE(last_exit_reason, ?) WHERE listing_id=?",
            (reason, listing_id),
        )

    # ------------------------------------------------------------- health
    def health(self, source: str) -> Health:
        r = self.conn.execute("SELECT * FROM source_health WHERE source=?", (source,)).fetchone()
        if not r:
            return Health(source, None, None, 0, None, None, False, 0)
        return Health(source, _dt(r["last_attempt"]), _dt(r["last_success"]), r["consecutive_failures"] or 0,
                      r["last_error"], _dt(r["backoff_until"]), bool(r["down_alert_sent"]),
                      r["last_offer_count"] or 0)

    def save_health(self, h: Health) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO source_health VALUES(?,?,?,?,?,?,?,?)",
            (h.source, _ts(h.last_attempt), _ts(h.last_success), h.consecutive_failures, h.last_error,
             _ts(h.backoff_until), int(h.down_alert_sent), h.last_offer_count),
        )

    # ------------------------------------------------------------- outbox (Telegram queue)
    def enqueue(self, text: str, buttons: list[tuple[str, str]] | None = None, priority: int = 5) -> None:
        self.conn.execute("INSERT INTO outbox(created,priority,text,buttons) VALUES(?,?,?,?)",
                          (_ts(datetime.now(UTC)), priority, text, json.dumps(buttons or [])))

    def pending(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM outbox WHERE sent_at IS NULL AND attempts < 20 ORDER BY priority, id LIMIT ?", (limit,)
        ).fetchall()

    def mark_sent(self, msg_id: int) -> None:
        self.conn.execute("UPDATE outbox SET sent_at=? WHERE id=?", (_ts(datetime.now(UTC)), msg_id))
        self.conn.commit()

    def mark_failed(self, msg_id: int, error: str) -> None:
        self.conn.execute("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?", (error[:300], msg_id))
        self.conn.commit()
