"""Requirements 5-7, 9-12 and 15 (end to end with fake adapters, file-backed SQLite, mocked Telegram)."""

from __future__ import annotations

import dataclasses
import json
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from conftest import mk_offer

from ps5monitor.commands import handle_updates
from ps5monitor.config import Settings
from ps5monitor.models import Decision, SourceResult, Status, Stock, Variant
from ps5monitor.notifier import AlertContext, Telegram, TelegramError, format_alert, format_summary
from ps5monitor.scanner import run_once
from ps5monitor.sources.base import Fetcher, SourceError
from ps5monitor.storage import Storage

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class World:
    """Mutable fake retail world consumed by fake adapters."""

    def __init__(self) -> None:
        self.offers: dict[str, list[Any]] = {"game": [], "mediamarkt": []}
        self.down: set[str] = set()

    def adapters(self) -> dict[str, Any]:
        def make(src: str) -> Any:
            def fetch(fetcher: Fetcher, now: datetime, mx: Decimal, s: Settings) -> SourceResult:
                if src in self.down:
                    raise SourceError("HTTP 503", 503)
                offers = []
                for o in self.offers[src]:
                    o = dataclasses.replace(o, checked_at=now)
                    offers.append(o)
                return SourceResult(src, True, offers)
            return fetch
        return {"game": make("game"), "mediamarkt": make("mediamarkt")}


def run(settings: Settings, storage: Storage, world: World, now: datetime) -> Any:
    return run_once(settings, storage, now=now, adapters=world.adapters(), client=httpx.Client(), dry_run=True)


def messages(storage: Storage) -> list[str]:
    return [r["text"] for r in storage.conn.execute("SELECT text FROM outbox ORDER BY id")]


def alerts(storage: Storage) -> list[tuple[str, str]]:
    return [(r["kind"], r["total"]) for r in storage.conn.execute("SELECT kind,total FROM sent_alerts ORDER BY ts")]


# 5 + 6
def test_first_alert_once_then_drop(settings: Settings) -> None:
    st, w = Storage(settings.db_path), World()
    w.offers["game"] = [mk_offer("575.00")]
    run(settings, st, w, T0)
    assert alerts(st) == [("new", "575.00")]
    assert any("TOTAL ENTREGADO: 575,00 €" in m for m in messages(st))
    for i in range(1, 4):  # unchanged scans: no duplicates
        run(settings, st, w, T0 + timedelta(minutes=20 * i))
    assert alerts(st) == [("new", "575.00")]
    w.offers["game"] = [mk_offer("565.00")]
    run(settings, st, w, T0 + timedelta(minutes=100))
    assert alerts(st) == [("new", "575.00"), ("drop", "565.00")]
    w.offers["mediamarkt"] = [mk_offer("540.00", source="mediamarkt", retailer="MediaMarkt", seller="MediaMarkt",
                                       url="https://www.mediamarkt.es/x", sku="m1")]
    run(settings, st, w, T0 + timedelta(minutes=120))
    w.offers["game"] = [mk_offer("563.00")]  # small drop, not a new overall low (540) -> no ping
    run(settings, st, w, T0 + timedelta(minutes=140))
    assert ("drop", "563.00") not in alerts(st)
    w.offers["game"] = [mk_offer("539.00")]  # small drop but new overall lowest -> ping
    run(settings, st, w, T0 + timedelta(minutes=160))
    assert alerts(st)[-1] == ("drop", "539.00")


# 7
def test_restock_triggers_alert(settings: Settings) -> None:
    st, w = Storage(settings.db_path), World()
    w.offers["game"] = [mk_offer("550.00", stock=Stock.OUT_OF_STOCK)]
    run(settings, st, w, T0)
    assert alerts(st) == []
    w.offers["game"] = [mk_offer("550.00")]
    run(settings, st, w, T0 + timedelta(minutes=20))
    assert alerts(st) == [("new", "550.00")]
    w.offers["game"] = [mk_offer("550.00", stock=Stock.OUT_OF_STOCK)]
    run(settings, st, w, T0 + timedelta(minutes=40))
    w.offers["game"] = [mk_offer("550.00")]
    run(settings, st, w, T0 + timedelta(minutes=60))
    assert alerts(st)[-1] == ("restock", "550.00")
    assert any("Vuelve a estar disponible" in m for m in messages(st))


# 9
def test_outage_is_isolated_and_not_out_of_stock(settings: Settings) -> None:
    st, w = Storage(settings.db_path), World()
    w.offers["game"] = [mk_offer("540.00")]
    run(settings, st, w, T0)
    lid = mk_offer("540.00").listing_id
    w.down.add("game")
    w.offers["mediamarkt"] = [mk_offer("560.00", source="mediamarkt", retailer="MediaMarkt", seller="MediaMarkt",
                                       url="https://www.mediamarkt.es/x", sku="m1")]
    t = T0
    for _ in range(9):  # ~3 h of failures in normal mode
        t += timedelta(minutes=20)
        rep = run(settings, st, w, t)
    assert "game" in rep.failed or st.health("game").backoff_until is not None
    assert ("new", "560.00") in alerts(st)  # other source unaffected
    listing = st.get_listing(lid)
    assert listing is not None and listing.last_stock == "in_stock"  # not assumed sold out
    state = st.alert_state(lid)
    assert state is not None and state.armed is False  # no spurious "back in stock" later
    assert any("no responde" in m and "NO significa" in m for m in messages(st))
    w.down.clear()
    run(settings, st, w, t + timedelta(hours=2))
    assert any("vuelve a responder" in m for m in messages(st))
    assert ("restock", "540.00") not in alerts(st)


# 10
def test_state_survives_restart_and_redeploy(settings: Settings, tmp_path: Path) -> None:
    w = World()
    w.offers["game"] = [mk_offer("520.00")]
    st = Storage(settings.db_path)
    run(settings, st, w, T0)
    st.close()
    moved = tmp_path / "redeploy" / "monitor.db"  # e.g. restored from the 'state' branch
    moved.parent.mkdir()
    shutil.copy(settings.db_path, moved)
    s2 = dataclasses.replace(settings, db_path=str(moved))
    st2 = Storage(s2.db_path)
    run(s2, st2, w, T0 + timedelta(minutes=30))
    assert alerts(st2) == [("new", "520.00")]
    assert st2.get("initialized") == "1"


# 15: qualifying offer outside the peak window still alerts
def test_offer_outside_peak_window_alerts(settings: Settings) -> None:
    st, w = Storage(settings.db_path), World()
    w.offers["game"] = [mk_offer("499.00")]
    rep = run(settings, st, w, datetime(2027, 1, 15, 3, 0, tzinfo=UTC))
    assert rep.mode == "normal" and alerts(st) == [("new", "499.00")]


def test_initial_summary_lists_all_offers(settings: Settings) -> None:
    st, w = Storage(settings.db_path), World()
    w.offers["game"] = [mk_offer(f"{500 + i}.00", url=f"https://www.game.es/p/{i}", sku=str(i)) for i in range(8)]
    run(settings, st, w, T0)
    text = "\n".join(messages(st))
    assert "Resumen inicial" in text
    assert all(f"https://www.game.es/p/{i}" in text for i in range(8))
    assert text.index("500,00 €") < text.index("507,00 €")  # cheapest first


def test_mode_switch_message_and_gap_warning(settings: Settings) -> None:
    st, w = Storage(settings.db_path), World()
    run(settings, st, w, datetime(2026, 11, 22, 22, 50, tzinfo=UTC))
    run(settings, st, w, datetime(2026, 11, 22, 23, 0, tzinfo=UTC))
    assert any("Modo BLACK FRIDAY activado" in m for m in messages(st))
    run(settings, st, w, datetime(2026, 11, 22, 23, 45, tzinfo=UTC))
    assert any("Hueco de cobertura" in m for m in messages(st))


# 11
def test_message_escaping_and_links(settings: Settings) -> None:
    o = mk_offer("549.99", title='PS5 Slim <b>"Pack"</b> & más', url="https://www.game.es/p?id=1&v=2")
    text, buttons = format_alert(o, Decision(Status.ELIGIBLE, Decimal("549.99"), []),
                                 AlertContext("new", None, None, None), settings, Decimal("575"))
    assert "&lt;b&gt;" in text and "&amp; más" in text and "<b>\"Pack\"" not in text
    assert 'href="https://www.game.es/p?id=1&amp;v=2"' in text
    assert buttons == [("🛒 Abrir producto", "https://www.game.es/p?id=1&v=2")]
    chunks = format_summary([(mk_offer("500", url=f"https://x.es/{i}", sku=str(i)),
                              Decision(Status.ELIGIBLE, Decimal("500"), [])) for i in range(120)],
                            settings, Decimal("575"), "Resumen")
    assert len(chunks) > 1 and all(len(c) <= 4096 for c in chunks)


def test_telegram_retry_and_no_token_leak() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})
        if len(calls) == 2:
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})

    tg = Telegram("123:SECRET", "42", httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    tg.send("hola")
    assert len(calls) == 2
    try:
        tg.send("x")
    except TelegramError as exc:
        assert "SECRET" not in str(exc)
    else:
        raise AssertionError("expected TelegramError")


def test_fetcher_outage_raises_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    f = Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    try:
        f.get("https://example.es")
    except SourceError as exc:
        assert "red/timeout" in str(exc)
    assert f.requests == 3


# 12
def test_commands_only_from_authorized_chat(settings: Settings) -> None:
    s = dataclasses.replace(settings, telegram_chat_id="42")
    st = Storage(s.db_path)
    upd = [{"update_id": 10, "message": {"chat": {"id": 666}, "text": "/pause"}},
           {"update_id": 11, "message": {"chat": {"id": 666}, "text": "/threshold 9999"}}]
    assert handle_updates(upd, st, s, T0) == []
    assert st.get("paused") is None and st.get("threshold_override") is None
    assert st.get("tg_offset") == "12"
    ok = [{"update_id": 12, "message": {"chat": {"id": 42}, "text": "/pause"}},
          {"update_id": 13, "message": {"chat": {"id": 42}, "text": "/threshold abc"}},
          {"update_id": 14, "message": {"chat": {"id": 42}, "text": "/threshold 550,5"}}]
    replies = handle_updates(ok, st, s, T0)
    assert st.get("paused") == "1" and st.get("threshold_override") == "550.50"
    assert "no válido" in replies[1]


def test_paused_alerts_are_delivered_after_resume(settings: Settings) -> None:
    st, w = Storage(settings.db_path), World()
    run(settings, st, w, T0)
    st.set("paused", "1")
    w.offers["game"] = [mk_offer("530.00", variant=Variant.SLIM_DIGITAL)]
    run(settings, st, w, T0 + timedelta(minutes=20))
    assert alerts(st) == []
    st.set("paused", None)
    run(settings, st, w, T0 + timedelta(minutes=40))
    assert alerts(st) == [("new", "530.00")]
    assert json.loads(st.get("http_cache") or "{}") == {}
