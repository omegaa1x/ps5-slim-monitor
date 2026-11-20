"""Telegram bot commands via getUpdates (no public webhook). Only the configured chat is obeyed."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from ps5monitor.config import Settings
from ps5monitor.models import Status
from ps5monitor.normalization import fmt_eur
from ps5monitor.notifier import esc, href
from ps5monitor.schedule import fmt_local, mode_at, next_transition
from ps5monitor.storage import Storage

log = logging.getLogger(__name__)


def effective_threshold(storage: Storage, settings: Settings) -> Decimal:
    raw = storage.get("threshold_override")
    return Decimal(raw) if raw else settings.max_total_price_eur


def status_text(storage: Storage, settings: Settings, now: datetime) -> str:
    from ps5monitor.sources import UNSUPPORTED

    mode = mode_at(now, settings)
    lines = [f"📊 <b>Estado del monitor</b> ({esc(fmt_local(now, settings))} Madrid)",
             f"Modo: {'🔥 BLACK FRIDAY (máxima prioridad)' if mode == 'peak' else '🟢 normal (24/7)'}",
             f"Límite: ≤ {esc(fmt_eur(effective_threshold(storage, settings)))}",
             f"Avisos: {'⏸️ PAUSADOS' if storage.get('paused') == '1' else '▶️ activos'}"]
    nt = next_transition(now, settings)
    if nt:
        lines.append(f"Próximo cambio de modo: {esc(fmt_local(nt[0], settings))} → {nt[1]}")
    last = storage.get("last_run")
    if last:
        lines.append(f"Última ejecución: {esc(fmt_local(datetime.fromisoformat(last), settings))}")
    lines.append("<b>Fuentes</b>:")
    for src in settings.enabled_sources:
        h = storage.health(src)
        if h.last_attempt is None:
            state = "⏳ aún sin comprobar"
        elif h.consecutive_failures == 0:
            state = f"✅ OK ({h.last_offer_count} anuncios PS5 Slim)"
        else:
            state = f"❌ fallando x{h.consecutive_failures}: {esc((h.last_error or '')[:80])}"
        when = esc(fmt_local(h.last_success, settings)) if h.last_success else "nunca"
        lines.append(f"• {src}: {state} · último éxito {when}")
    lines.append("<b>No soportadas directamente</b>: " + esc(", ".join(UNSUPPORTED)) + " (vía Chollometro)")
    return "\n".join(lines)


def deals_text(storage: Storage, settings: Settings) -> str:
    rows = storage.qualifying((Status.ELIGIBLE, Status.UNVERIFIED), max_age=timedelta(hours=24))
    if not rows:
        return "🔎 Ahora mismo no hay ofertas PS5 Slim ≤ límite detectadas (últimas 24 h)."
    out = ["🛒 <b>Ofertas actuales (últimas 24 h)</b>"]
    for i, r in enumerate(rows[:30], 1):
        tag = "✅" if r.last_status == Status.ELIGIBLE.value else "⚠️ sin verificar"
        amount = fmt_eur(r.last_total) if r.last_total is not None else f"{fmt_eur(r.last_price)} + envío ?"
        out.append(f'{i}. <b>{esc(amount)}</b> · {esc(r.retailer)} {tag} — <a href="{href(r.url)}">enlace</a>')
    return "\n".join(out)


def handle_updates(updates: list[dict[str, Any]], storage: Storage, settings: Settings,
                   now: datetime) -> list[str]:
    """Process commands; returns reply texts. Messages from any other chat are ignored."""
    replies: list[str] = []
    max_id = None
    for upd in updates:
        uid = upd.get("update_id")
        if isinstance(uid, int):
            max_id = uid if max_id is None else max(max_id, uid)
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if chat_id != str(settings.telegram_chat_id):
            log.warning("Ignored a Telegram message from an unauthorized chat")
            continue
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        arg = arg.strip()
        if cmd in ("/start", "/help"):
            replies.append("🤖 Comandos: /status, /deals, /pause, /resume, /threshold [importe|reset], /test")
        elif cmd == "/status":
            replies.append(status_text(storage, settings, now))
        elif cmd == "/deals":
            replies.append(deals_text(storage, settings))
        elif cmd == "/pause":
            storage.set("paused", "1")
            replies.append("⏸️ Avisos de ofertas pausados (las comprobaciones de salud siguen). /resume para reanudar.")
        elif cmd == "/resume":
            storage.set("paused", None)
            replies.append("▶️ Avisos reanudados.")
        elif cmd == "/threshold":
            if not arg:
                replies.append(f"💶 Límite actual: ≤ {esc(fmt_eur(effective_threshold(storage, settings)))}")
            elif arg.lower() == "reset":
                storage.set("threshold_override", None)
                replies.append(f"💶 Límite restaurado: ≤ {esc(fmt_eur(settings.max_total_price_eur))}")
            else:
                try:
                    value = Decimal(arg.replace("€", "").replace(",", ".").strip()).quantize(Decimal("0.01"))
                    if not (Decimal("1") <= value <= Decimal("5000")):
                        raise InvalidOperation
                except InvalidOperation:
                    replies.append("❌ Importe no válido. Ejemplo: /threshold 550")
                else:
                    storage.set("threshold_override", f"{value:.2f}")
                    replies.append(f"💶 Nuevo límite: ≤ {esc(fmt_eur(value))}")
        elif cmd == "/test":
            replies.append("✅ Mensaje de prueba del monitor PS5 Slim.")
        else:
            replies.append("❓ Comando desconocido. Usa /help")
    if max_id is not None:
        storage.set("tg_offset", str(max_id + 1))
    return replies
