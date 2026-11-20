"""Telegram Bot API client (HTTPS), HTML message formatting with escaping, retry/backoff and outbox flush."""

from __future__ import annotations

import html
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from ps5monitor.config import Settings
from ps5monitor.models import VARIANT_LABEL, Decision, Offer, Status, Variant
from ps5monitor.normalization import fmt_eur
from ps5monitor.schedule import fmt_local

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
SOURCE_NAMES = {"chollometro": "Chollometro", "blogdechollos": "Blogdechollos", "ofertitas": "Ofertitas"}
MAX_LEN = 3900  # Telegram hard limit is 4096 characters


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def href(url: str) -> str:
    return html.escape(url, quote=True)


class TelegramError(Exception):
    pass


class RedactFilter(logging.Filter):
    """Removes secrets from every log record."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self.secrets:
            msg = msg.replace(s, "[REDACTED]")
        record.msg, record.args = msg, ()
        return True


class Telegram:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None,
                 sleep: Any = time.sleep) -> None:
        self.token = token
        self.chat_id = chat_id
        self.client = client or httpx.Client(timeout=20)
        self.sleep = sleep

    def _call(self, method: str, payload: dict[str, Any], attempts: int = 4) -> Any:
        url = f"{API}/bot{self.token}/{method}"
        last = "unknown error"
        for attempt in range(attempts):
            try:
                r = self.client.post(url, json=payload)
            except httpx.HTTPError as exc:
                last = type(exc).__name__
                self.sleep(min(30.0, 2 ** attempt) + random.uniform(0, 0.5))
                continue
            try:
                data = r.json()
            except ValueError:
                data = {}
            if r.status_code == 200 and data.get("ok"):
                return data.get("result")
            if r.status_code == 429:
                retry = int((data.get("parameters") or {}).get("retry_after", 3))
                self.sleep(min(retry, 60))
                last = "429 rate limited"
                continue
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                self.sleep(min(30.0, 2 ** attempt) + random.uniform(0, 0.5))
                continue
            # 4xx: permanent (bad request, chat not found, bot blocked...). Never include the token.
            raise TelegramError(f"{method}: HTTP {r.status_code} {str(data.get('description', ''))[:200]}")
        raise TelegramError(f"{method}: {last}")

    def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "url": u}] for t, u in buttons]}
        self._call("sendMessage", payload)

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 0, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return list(self._call("getUpdates", payload, attempts=2) or [])

    def set_commands(self) -> None:
        cmds = [("status", "Estado del monitor y fuentes"), ("deals", "Ofertas actuales <= límite"),
                ("pause", "Pausar avisos"), ("resume", "Reanudar avisos"),
                ("threshold", "Ver/cambiar el precio máximo"), ("test", "Mensaje de prueba")]
        self._call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in cmds]})


def flush_outbox(storage: Any, tg: Telegram | None, dry_run: bool = False, spacing_s: float = 1.1,
                 sleep: Any = time.sleep) -> tuple[int, int]:
    """Send queued messages in priority order. Failed messages stay queued for the next run."""
    sent = failed = 0
    for row in storage.pending():
        buttons = [tuple(b) for b in json.loads(row["buttons"] or "[]")]
        if dry_run or tg is None:
            print("----- [Telegram dry-run] -----\n" + row["text"] + (f"\n{buttons}" if buttons else ""))
            storage.mark_sent(row["id"])
            sent += 1
            continue
        try:
            tg.send(row["text"], buttons)  # type: ignore[arg-type]
            storage.mark_sent(row["id"])
            sent += 1
            sleep(spacing_s)
        except TelegramError as exc:
            log.warning("Telegram send failed (kept in queue): %s", exc)
            storage.mark_failed(row["id"], str(exc))
            failed += 1
    return sent, failed


# ------------------------------------------------------------------ formatting

KIND_LINE = {
    "new": "🆕 Nueva oferta detectada",
    "drop": "📉 Ha bajado más de precio",
    "restock": "🔄 Vuelve a estar disponible",
    "below_ceiling": "⬇️ Ha bajado del límite",
}


def variant_label(variant: Variant | str, is_bundle: bool, bundle_note: str | None) -> str:
    label = VARIANT_LABEL.get(Variant(variant), str(variant))
    if is_bundle:
        label += f" · 🎁 PACK ({bundle_note or 'bundle'}) — no es precio de consola sola"
    return label


@dataclass
class AlertContext:
    kind: str
    previous_total: Decimal | None
    lowest_before: Decimal | None
    history_since: datetime | None


def format_alert(offer: Offer, decision: Decision, ctx: AlertContext, settings: Settings,
                 max_total: Decimal) -> tuple[str, list[tuple[str, str]]]:
    verified = decision.status == Status.ELIGIBLE
    lines: list[str] = []
    if verified:
        lines.append(f"🚨 <b>PS5 SLIM — PRECIO ≤ {esc(fmt_eur(max_total))}</b>")
    else:
        lines.append(f"⚠️ <b>SIN VERIFICAR — posible PS5 Slim ≤ {esc(fmt_eur(max_total))}</b>")
    lines.append(esc(KIND_LINE.get(ctx.kind, ctx.kind)))
    lines.append(f"🎮 Versión: {esc(variant_label(offer.variant, offer.is_bundle, offer.bundle_note))}")
    lines.append(f"📝 {esc(offer.title[:180])}")
    if not offer.verified_source:
        lines.append(f"🏬 Tienda indicada: {esc(offer.retailer)} · Fuente: "
                     f"{esc(SOURCE_NAMES.get(offer.source, offer.source))} (web de chollos)")
    else:
        lines.append(f"🏬 Tienda: {esc(offer.retailer)} · Vendedor: {esc(offer.seller or 'no indicado')}")
    price_label = "Precio artículo" if verified else "Precio publicado"
    lines.append(f"💶 {price_label}: {esc(fmt_eur(offer.item_price))}")
    if offer.shipping is None:
        lines.append("🚚 Envío obligatorio: <b>no confirmado</b>")
    else:
        ship = "gratis (0,00 €)" if offer.shipping == 0 else fmt_eur(offer.shipping)
        lines.append(f"🚚 Envío obligatorio: {esc(ship)}")
    if decision.total is not None and verified:
        lines.append(f"✅ <b>TOTAL ENTREGADO: {esc(fmt_eur(decision.total))}</b> (≤ {esc(fmt_eur(max_total))})")
    elif decision.total is not None:
        lines.append(f"Σ Total según la publicación: {esc(fmt_eur(decision.total))} (sin confirmar)")
    if ctx.previous_total is not None and ctx.previous_total != decision.total:
        lines.append(f"📉 Último observado: {esc(fmt_eur(ctx.previous_total))}")
    # Only claim a "lowest price" when there is at least a week of observed history for this listing.
    if verified and ctx.history_since is not None and decision.total is not None \
            and offer.checked_at - ctx.history_since >= timedelta(days=7) \
            and ctx.lowest_before is not None and decision.total < ctx.lowest_before:
        lines.append(f"🏆 Precio más bajo registrado por el monitor (historial desde "
                     f"{esc(fmt_local(ctx.history_since, settings)[:10])})")
    if verified:
        lines.append("📦 Stock: disponible (confirmado en la web de la tienda)")
    else:
        lines.append("📦 Stock: sin confirmar")
        if decision.reasons:
            lines.append(f"❗ Motivo: {esc('; '.join(decision.reasons)[:300])}")
    lines.append(f"🕒 {'Verificado' if verified else 'Detectado'}: {esc(fmt_local(offer.checked_at, settings))}"
                 f" (hora de Madrid)")
    lines.append(f'🔗 <a href="{href(offer.url)}">{"Enlace directo al producto" if verified else "Ver chollo"}</a>')
    button = ("🛒 Abrir producto" if verified else "🔎 Ver chollo", offer.url)
    return "\n".join(lines), [button]


def format_summary(entries: list[tuple[Offer, Decision]], settings: Settings, max_total: Decimal,
                   title: str) -> list[str]:
    """Ordered summary (cheapest first) with direct links to ALL entries, split into several messages."""
    def key(e: tuple[Offer, Decision]) -> tuple[int, Decimal]:
        o, d = e
        return (0 if d.status == Status.ELIGIBLE else 1, d.total or o.item_price or Decimal("99999"))

    lines = []
    for i, (o, d) in enumerate(sorted(entries, key=key), 1):
        tag = "✅" if d.status == Status.ELIGIBLE else "⚠️ sin verificar"
        amount = fmt_eur(d.total) if d.total is not None else f"{fmt_eur(o.item_price)} + envío ?"
        where = o.retailer if o.verified_source else f"{o.retailer} ({SOURCE_NAMES.get(o.source, o.source)})"
        v = VARIANT_LABEL.get(o.variant, o.variant.value).split(" (")[0] + (" · PACK" if o.is_bundle else "")
        lines.append(f'{i}. <b>{esc(amount)}</b> · {esc(v)} · {esc(where)} {tag} — '
                     f'<a href="{href(o.url)}">enlace</a>')
    header = (f"📋 <b>{esc(title)}</b>\nOfertas PS5 Slim ≤ {esc(fmt_eur(max_total))}: {len(entries)}\n"
              f"🕒 {esc(fmt_local(entries[0][0].checked_at, settings) if entries else '')} (Madrid)\n")
    chunks, cur = [], header
    for line in lines:
        if len(cur) + len(line) + 1 > MAX_LEN:
            chunks.append(cur)
            cur = "📋 (continuación)\n"
        cur += line + "\n"
    chunks.append(cur)
    return chunks
