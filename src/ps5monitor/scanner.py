"""One monitoring cycle: commands -> due sources (isolated) -> evaluate -> persist -> alerts -> health."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from ps5monitor import commands
from ps5monitor.config import Settings
from ps5monitor.models import Decision, Offer, SourceResult, Status, Stock
from ps5monitor.normalization import fmt_eur
from ps5monitor.notifier import (
    AlertContext,
    Telegram,
    TelegramError,
    esc,
    flush_outbox,
    format_alert,
    format_summary,
)
from ps5monitor.rules import decide_alert, evaluate
from ps5monitor.schedule import PEAK, fmt_local, interval_for, is_due, local, mode_at, next_transition
from ps5monitor.sources import base, chollometro, game, mediamarkt
from ps5monitor.storage import Storage

log = logging.getLogger(__name__)

Adapter = Callable[[base.Fetcher, datetime, Decimal, Settings], SourceResult]

ADAPTERS: dict[str, Adapter] = {
    "chollometro": lambda f, now, mx, s: chollometro.fetch(f, now),
    "game": lambda f, now, mx, s: game.fetch(f, now, mx, s.game_shipping_eur),
    "mediamarkt": lambda f, now, mx, s: mediamarkt.fetch(f, now, mx),
}


@dataclass
class Candidate:
    offer: Offer
    decision: Decision
    kind: str
    ctx: AlertContext


@dataclass
class RunReport:
    mode: str
    ran: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    alerts: int = 0
    sent: int = 0
    send_failed: int = 0


def _process_commands(storage: Storage, settings: Settings, tg: Telegram | None, now: datetime) -> None:
    if tg is None:
        return
    try:
        if storage.get("commands_set") != "1":
            tg.set_commands()
            storage.set("commands_set", "1")
        offset = storage.get("tg_offset")
        updates = tg.get_updates(int(offset) if offset else None)
        for reply in commands.handle_updates(updates, storage, settings, now):
            storage.enqueue(reply, priority=1)
    except TelegramError as exc:
        log.warning("Telegram commands unavailable this run: %s", exc)


def _update_health(storage: Storage, settings: Settings, src: str, now: datetime, mode: str,
                   result: SourceResult | None, error: base.SourceError | None) -> None:
    h = storage.health(src)
    h.last_attempt = now
    if result is not None:
        if h.down_alert_sent:
            storage.enqueue(f"🟢 La fuente <b>{esc(src)}</b> vuelve a responder.", priority=3)
        h.last_success, h.consecutive_failures, h.last_error = now, 0, None
        h.backoff_until, h.down_alert_sent = None, False
        if not result.not_modified:
            h.last_offer_count = len(result.offers)
        storage.set(f"down_since:{src}", None)
    else:
        assert error is not None
        h.consecutive_failures += 1
        h.last_error = str(error)[:200]
        if h.consecutive_failures == 1:
            storage.set(f"down_since:{src}", now.isoformat())
        step = interval_for(src, mode, settings)
        if error.status in (403, 429):
            h.backoff_until = now + min(step * 2 ** (h.consecutive_failures - 1), timedelta(minutes=60))
        elif h.consecutive_failures >= 3:
            h.backoff_until = now + min(step * 2 ** (h.consecutive_failures - 3), timedelta(minutes=30))
        since_raw = storage.get(f"down_since:{src}")
        since = datetime.fromisoformat(since_raw) if since_raw else now
        limit = timedelta(minutes=settings.source_down_alert_peak_min if mode == PEAK
                          else settings.source_down_alert_normal_min)
        if not h.down_alert_sent and now - since >= limit:
            storage.enqueue(
                f"🔴 La fuente <b>{esc(src)}</b> no responde desde {esc(fmt_local(since, settings))} "
                f"({esc(h.last_error)}). Las demás fuentes siguen funcionando; esto NO significa "
                f"'sin stock'.", priority=2)
            h.down_alert_sent = True
    storage.save_health(h)


def _recent_verified_alert(storage: Storage, retailer: str, total: Decimal, now: datetime) -> bool:
    row = storage.conn.execute(
        """SELECT 1 FROM sent_alerts s JOIN listings l ON l.listing_id = s.listing_id
        WHERE l.source != 'chollometro' AND lower(l.retailer) = lower(?) AND s.total = ? AND s.ts >= ?""",
        (retailer, f"{total:.2f}", (now - timedelta(hours=24)).isoformat(timespec="seconds")),
    ).fetchone()
    return row is not None


def _process_offers(storage: Storage, settings: Settings, offers: list[Offer], max_total: Decimal,
                    now: datetime) -> list[Candidate]:
    candidates: list[Candidate] = []
    global_lowest = storage.global_lowest_alerted()
    for offer in offers:
        decision = evaluate(offer, max_total, allow_marketplace=settings.allow_marketplace_sellers,
                            alert_preorders=settings.alert_preorders)
        prev = storage.upsert_listing(offer, decision)
        qualifies = decision.status == Status.ELIGIBLE or (
            decision.status == Status.UNVERIFIED and settings.send_unverified_alerts)
        if not qualifies:
            if decision.status == Status.REJECTED and storage.alert_state(offer.listing_id) is not None:
                reason = "stock" if offer.stock != Stock.IN_STOCK else "price"
                storage.mark_left_eligibility(offer.listing_id, reason)
            continue
        price = decision.total if decision.total is not None else offer.item_price
        assert price is not None
        if offer.source == "chollometro" and _recent_verified_alert(storage, offer.retailer, price, now):
            continue
        state = storage.alert_state(offer.listing_id)
        ad = decide_alert(state, price, now, drop_step=settings.price_drop_step_eur, global_lowest=global_lowest)
        if not ad.send:
            continue
        ctx = AlertContext(ad.kind, prev.last_total if prev else None, prev.lowest_total if prev else None,
                           storage.history_since(offer.listing_id))
        candidates.append(Candidate(offer, decision, ad.kind, ctx))
    return candidates


def _emit_alerts(storage: Storage, settings: Settings, candidates: list[Candidate], max_total: Decimal,
                 now: datetime, first_run: bool) -> int:
    candidates.sort(key=lambda c: (c.decision.status != Status.ELIGIBLE,
                                   c.decision.total or c.offer.item_price or Decimal("99999")))
    recorded: list[Candidate] = []
    for c in candidates:
        price = c.decision.total if c.decision.total is not None else c.offer.item_price
        assert price is not None
        state = storage.alert_state(c.offer.listing_id)
        cycle = state.last_alert_at.isoformat() if state and state.last_alert_at else "first"
        fp = f"{c.offer.listing_id}:{c.kind}:{price:.2f}:{cycle}"
        if storage.record_alert(c.offer.listing_id, c.kind, price, now, fp):
            recorded.append(c)
    if first_run or len(recorded) > settings.max_individual_alerts_per_run:
        title = "Resumen inicial" if first_run else "Resumen de nuevas ofertas"
        if recorded:
            for chunk in format_summary([(c.offer, c.decision) for c in recorded], settings, max_total, title):
                storage.enqueue(chunk, priority=2)
        # The cheapest few still get their own detailed message with a button.
        for c in recorded[: min(3, settings.max_individual_alerts_per_run)]:
            text, buttons = format_alert(c.offer, c.decision, c.ctx, settings, max_total)
            storage.enqueue(text, buttons, priority=2)
    else:
        for c in recorded:
            text, buttons = format_alert(c.offer, c.decision, c.ctx, settings, max_total)
            storage.enqueue(text, buttons, priority=2)
    return len(recorded)


def _housekeeping_messages(storage: Storage, settings: Settings, now: datetime, mode: str,
                           max_total: Decimal) -> None:
    last_mode = storage.get("mode")
    if last_mode and last_mode != mode:
        if mode == PEAK:
            end = fmt_local(settings.peak_end_exclusive_utc - timedelta(seconds=1), settings)
            storage.enqueue(f"🔥 <b>Modo BLACK FRIDAY activado</b>: máxima prioridad hasta {esc(end)} (Madrid).",
                            priority=3)
        else:
            storage.enqueue("🟢 Fin del modo Black Friday. El monitor sigue activo 24/7 en modo normal.",
                            priority=3)
    storage.set("mode", mode)

    last_run = storage.get("last_run")
    if last_run:
        gap = now - datetime.fromisoformat(last_run)
        limit = timedelta(minutes=settings.run_gap_alert_peak_min if mode == PEAK
                          else settings.run_gap_alert_normal_min)
        if gap >= limit:
            storage.enqueue(
                f"⚠️ Hueco de cobertura: el monitor no se ejecutó entre "
                f"{esc(fmt_local(datetime.fromisoformat(last_run), settings))} y {esc(fmt_local(now, settings))} "
                f"({int(gap.total_seconds() // 60)} min). Causa probable: retraso de GitHub Actions/cron-job.org.",
                priority=3)

    hb = settings.heartbeat_local_time
    today = local(now, settings).date().isoformat()
    if hb and local(now, settings).time() >= hb and storage.get("heartbeat_date") != today \
            and storage.get("initialized") == "1":
        rows = storage.qualifying((Status.ELIGIBLE, Status.UNVERIFIED), max_age=timedelta(hours=24))
        verified = [r for r in rows if r.last_status == Status.ELIGIBLE.value]
        best = fmt_eur(verified[0].last_total) if verified else "ninguna verificada"
        nt = next_transition(now, settings)
        lines = [f"💓 <b>Monitor activo</b> · modo {'BLACK FRIDAY' if mode == PEAK else 'normal'}",
                 f"Ofertas ≤ {esc(fmt_eur(max_total))} (24 h): {len(verified)} verificadas, "
                 f"{len(rows) - len(verified)} sin verificar · mejor: {esc(best)}"]
        for src in settings.enabled_sources:
            h = storage.health(src)
            lines.append(f"• {src}: {'✅' if h.consecutive_failures == 0 and h.last_success else '❌'}")
        if nt:
            lines.append(f"Próximo cambio de modo: {esc(fmt_local(nt[0], settings))}")
        storage.enqueue("\n".join(lines), priority=4)
        storage.set("heartbeat_date", today)


def run_once(settings: Settings, storage: Storage, *, client: base.httpx.Client | None = None,
             tg: Telegram | None = None, now: datetime, dry_run: bool = False,
             adapters: dict[str, Adapter] | None = None) -> RunReport:
    adapters = adapters or ADAPTERS
    mode = mode_at(now, settings)
    report = RunReport(mode)
    _process_commands(storage, settings, None if dry_run else tg, now)
    max_total = commands.effective_threshold(storage, settings)
    first_run = storage.get("initialized") != "1"
    _housekeeping_messages(storage, settings, now, mode, max_total)

    cache = json.loads(storage.get("http_cache") or "{}")
    own_client = client is None
    client = client or base.make_client(settings.user_agent, settings.http_timeout_s)
    fetcher = base.Fetcher(client, cache)
    candidates: list[Candidate] = []
    try:
        for src in settings.enabled_sources:
            h = storage.health(src)
            if not first_run and not is_due(src, h.last_attempt, h.backoff_until, now, settings):
                continue
            try:
                result = adapters[src](fetcher, now, max_total, settings)
            except base.SourceError as exc:
                report.failed[src] = str(exc)
                log.warning("source %s failed: %s", src, exc)
                _update_health(storage, settings, src, now, mode, None, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - isolate unexpected adapter bugs too
                log.exception("source %s crashed", src)
                err = base.SourceError(f"error interno: {type(exc).__name__}")
                report.failed[src] = str(err)
                _update_health(storage, settings, src, now, mode, None, err)
                continue
            report.ran.append(src)
            _update_health(storage, settings, src, now, mode, result, None)
            candidates.extend(_process_offers(storage, settings, result.offers, max_total, now))
    finally:
        if own_client:
            client.close()
    storage.set("http_cache", json.dumps(cache))

    paused = storage.get("paused") == "1"
    if candidates and not paused:
        report.alerts = _emit_alerts(storage, settings, candidates, max_total, now, first_run)
    if first_run:
        ok = [s for s in report.ran]
        msg = (f"✅ <b>Monitor PS5 Slim en marcha</b>\nLímite: total entregado ≤ {esc(fmt_eur(max_total))}\n"
               f"Fuentes OK: {esc(', '.join(ok) or 'ninguna')}"
               + (f"\nFuentes con error: {esc(', '.join(report.failed))}" if report.failed else "")
               + ("" if report.alerts else "\nAhora mismo no hay ofertas que cumplan el límite. "
                  "Te avisaré en cuanto aparezca una."))
        storage.enqueue(msg, priority=1)
        storage.set("initialized", "1")
    storage.set("last_run", now.isoformat(timespec="seconds"))
    if now.minute < 10 and now.hour == 3:
        storage.prune()
    storage.commit()
    report.sent, report.send_failed = flush_outbox(storage, tg, dry_run=dry_run)
    storage.commit()
    return report
