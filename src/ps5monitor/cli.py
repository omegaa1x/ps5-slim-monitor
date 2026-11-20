"""CLI: scan-once, run, test-telegram, status, list-deals, sources."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta

from ps5monitor.config import ConfigError, Settings, load_dotenv
from ps5monitor.notifier import RedactFilter, Telegram, TelegramError, esc
from ps5monitor.schedule import fmt_local, interval_for, mode_at, next_transition, utcnow
from ps5monitor.storage import Storage


def _setup_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactFilter([settings.telegram_bot_token]))
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs (incl. bot token)


def _now(arg: str | None) -> datetime:
    if not arg:
        return utcnow()
    dt = datetime.fromisoformat(arg)
    if dt.tzinfo is None:
        raise SystemExit("--now must include a UTC offset, e.g. 2026-11-23T00:00:00+01:00")
    return dt


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(prog="ps5monitor", description="Monitor PS5 Slim ≤ límite (España)")
    p.add_argument("--env-file", default=os.environ.get("PS5MONITOR_ENV_FILE", ".env"))
    sub = p.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("scan-once", help="Una pasada (usado por GitHub Actions)")
    s1.add_argument("--dry-run", action="store_true", help="No envía a Telegram; imprime los mensajes")
    s1.add_argument("--now", help="Fecha simulada ISO con zona horaria (pruebas)")
    s2 = sub.add_parser("run", help="Bucle continuo (para un servidor siempre encendido)")
    s2.add_argument("--dry-run", action="store_true")
    sub.add_parser("test-telegram", help="Envía un mensaje de prueba")
    s4 = sub.add_parser("status", help="Modo, fuentes y próximas comprobaciones")
    s4.add_argument("--now")
    s5 = sub.add_parser("list-deals", help="Ofertas registradas que cumplen el límite")
    s5.add_argument("--all", action="store_true", help="Incluye las sin verificar")
    sub.add_parser("sources", help="Método y estado documentado de cada tienda")
    args = p.parse_args(argv)

    load_dotenv(args.env_file)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuración inválida: {exc}", file=sys.stderr)
        return 2
    _setup_logging(settings)
    tg = Telegram(settings.telegram_bot_token, settings.telegram_chat_id) if settings.telegram_configured else None

    if args.cmd == "sources":
        from ps5monitor.sources import SUPPORTED, UNSUPPORTED
        for k, v in SUPPORTED.items():
            print(f"[activa]       {k}: {v}")
        for k, v in UNSUPPORTED.items():
            print(f"[no soportada] {k}: {v}")
        return 0

    if args.cmd == "test-telegram":
        if tg is None:
            print("Faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
            return 2
        try:
            tg.send(f"✅ <b>Prueba</b> del monitor PS5 Slim · {esc(fmt_local(utcnow(), settings))} (Madrid)")
        except TelegramError as exc:
            print(f"Error de Telegram: {exc}", file=sys.stderr)
            return 1
        print("Mensaje de prueba enviado.")
        return 0

    storage = Storage(settings.db_path)
    try:
        if args.cmd == "status":
            from ps5monitor.commands import status_text
            now = _now(args.now)
            import re
            print(re.sub(r"<[^>]+>", "", status_text(storage, settings, now)))
            mode = mode_at(now, settings)
            for src in settings.enabled_sources:
                h = storage.health(src)
                nxt = (h.last_attempt + interval_for(src, mode, settings)) if h.last_attempt else now
                if h.backoff_until and h.backoff_until > nxt:
                    nxt = h.backoff_until
                print(f"  próxima comprobación {src}: {fmt_local(nxt, settings)} "
                      f"(intervalo {interval_for(src, mode, settings)})")
            nt = next_transition(now, settings)
            if nt:
                print(f"  faltan {nt[0] - now} para pasar a modo {nt[1]}")
            return 0
        if args.cmd == "list-deals":
            import re

            from ps5monitor.commands import deals_text
            print(re.sub(r"<[^>]+>", "", deals_text(storage, settings)))
            return 0

        from ps5monitor.scanner import run_once
        if args.cmd == "scan-once":
            report = run_once(settings, storage, tg=tg, now=_now(args.now), dry_run=args.dry_run)
            print(f"modo={report.mode} fuentes_ok={report.ran} fallos={report.failed} "
                  f"avisos={report.alerts} enviados={report.sent} envio_fallido={report.send_failed}")
            return 0
        if args.cmd == "run":
            while True:
                now = utcnow()
                try:
                    run_once(settings, storage, tg=tg, now=now, dry_run=args.dry_run)
                except Exception:  # noqa: BLE001
                    logging.exception("cycle failed; continuing")
                mode = mode_at(utcnow(), settings)
                step = min(interval_for(s, mode, settings) for s in settings.enabled_sources)
                time.sleep(max(30.0, min(step, timedelta(minutes=5)).total_seconds()))
    finally:
        storage.close()
    return 0
