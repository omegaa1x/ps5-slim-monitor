"""Typed, validated settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

# Verified retailer sources first, community deal feeds last (cross-source dedupe prefers verified data).
SOURCES = ("game", "elcorteingles", "hipercor", "mediamarkt", "chollometro", "blogdechollos", "ofertitas")

# Default per-source polling intervals in minutes: (normal, peak).
# On GitHub Actions the effective minimum is the trigger cadence (~5 min).
DEFAULT_INTERVALS: dict[str, tuple[int, int]] = {
    "chollometro": (10, 2),
    "blogdechollos": (15, 5),
    "ofertitas": (15, 5),
    "game": (15, 3),
    "elcorteingles": (15, 3),
    "hipercor": (30, 10),  # mostly mirrors the El Corte Inglés catalogue
    "mediamarkt": (15, 3),
}


class ConfigError(ValueError):
    pass


def _dec(name: str, default: str) -> Decimal:
    raw = os.environ.get(name, default).strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a decimal number, got {raw!r}") from exc
    if not value.is_finite() or value < 0:
        raise ConfigError(f"{name} must be a non-negative number")
    return value.quantize(Decimal("0.01"))


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    if raw.strip().lower() in ("1", "true", "yes", "si", "sí", "on"):
        return True
    if raw.strip().lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true/false, got {raw!r}")


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines; never overrides variables already set."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    max_total_price_eur: Decimal = Decimal("575.00")
    timezone: str = "Europe/Madrid"
    peak_start_local: datetime = datetime(2026, 11, 23, 0, 0, 0)
    peak_end_exclusive_local: datetime = datetime(2026, 12, 1, 0, 0, 0)
    intervals: dict[str, tuple[int, int]] = field(default_factory=lambda: dict(DEFAULT_INTERVALS))
    enabled_sources: tuple[str, ...] = SOURCES
    price_drop_step_eur: Decimal = Decimal("5.00")
    send_unverified_alerts: bool = True
    allow_marketplace_sellers: bool = False
    alert_preorders: bool = False
    max_individual_alerts_per_run: int = 5
    heartbeat_local_time: time | None = time(9, 0)
    source_down_alert_peak_min: int = 30
    source_down_alert_normal_min: int = 120
    run_gap_alert_peak_min: int = 20
    run_gap_alert_normal_min: int = 60
    db_path: str = "data/monitor.db"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    user_agent: str = "Mozilla/5.0 (compatible; ps5-slim-monitor/1.0; personal price alert)"
    http_timeout_s: float = 20.0
    shipping_overrides: dict[str, Decimal] = field(default_factory=dict)  # source -> confirmed shipping €

    def shipping_for(self, source: str) -> Decimal | None:
        return self.shipping_overrides.get(source)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def peak_start_utc(self) -> datetime:
        return self.peak_start_local.replace(tzinfo=self.tz).astimezone(ZoneInfo("UTC"))

    @property
    def peak_end_exclusive_utc(self) -> datetime:
        return self.peak_end_exclusive_local.replace(tzinfo=self.tz).astimezone(ZoneInfo("UTC"))

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @classmethod
    def from_env(cls) -> Settings:
        tzname = os.environ.get("TIMEZONE", "Europe/Madrid").strip()
        try:
            ZoneInfo(tzname)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"Unknown TIMEZONE {tzname!r}") from exc

        def _dt(name: str, default: str) -> datetime:
            raw = os.environ.get(name, default).strip()
            try:
                value = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be ISO datetime (YYYY-MM-DDTHH:MM:SS)") from exc
            if value.tzinfo is not None:
                raise ConfigError(f"{name} must be a local (naive) time interpreted in TIMEZONE")
            return value

        start = _dt("BLACK_FRIDAY_PEAK_START", "2026-11-23T00:00:00")
        end = _dt("BLACK_FRIDAY_PEAK_END_EXCLUSIVE", "2026-12-01T00:00:00")
        if end <= start:
            raise ConfigError("BLACK_FRIDAY_PEAK_END_EXCLUSIVE must be after BLACK_FRIDAY_PEAK_START")

        intervals: dict[str, tuple[int, int]] = {}
        for src, (normal, peak) in DEFAULT_INTERVALS.items():
            intervals[src] = (
                _int(f"INTERVAL_{src.upper()}_NORMAL_MIN", normal, 1),
                _int(f"INTERVAL_{src.upper()}_PEAK_MIN", peak, 1),
            )

        enabled_raw = os.environ.get("ENABLED_SOURCES", ",".join(SOURCES))
        enabled = tuple(s.strip().lower() for s in enabled_raw.split(",") if s.strip())
        unknown = [s for s in enabled if s not in SOURCES]
        if unknown:
            raise ConfigError(f"Unknown sources in ENABLED_SOURCES: {unknown}")

        hb_raw = os.environ.get("HEARTBEAT_LOCAL_TIME", "09:00").strip()
        heartbeat: time | None
        if hb_raw.lower() in ("", "off", "none"):
            heartbeat = None
        else:
            try:
                heartbeat = time.fromisoformat(hb_raw)
            except ValueError as exc:
                raise ConfigError("HEARTBEAT_LOCAL_TIME must be HH:MM or 'off'") from exc

        overrides: dict[str, Decimal] = {}
        for src in SOURCES:
            raw = os.environ.get(f"SHIPPING_EUR_{src.upper()}", "").strip()
            if not raw and src == "game":
                raw = os.environ.get("GAME_SHIPPING_EUR", "").strip()  # backwards-compatible alias
            if raw:
                overrides[src] = _dec(f"SHIPPING_EUR_{src.upper()}", raw)
        return cls(
            max_total_price_eur=_dec("MAX_TOTAL_PRICE_EUR", "575.00"),
            timezone=tzname,
            peak_start_local=start,
            peak_end_exclusive_local=end,
            intervals=intervals,
            enabled_sources=enabled,
            price_drop_step_eur=_dec("PRICE_DROP_STEP_EUR", "5.00"),
            send_unverified_alerts=_bool("SEND_UNVERIFIED_ALERTS", True),
            allow_marketplace_sellers=_bool("ALLOW_MARKETPLACE_SELLERS", False),
            alert_preorders=_bool("ALERT_PREORDERS", False),
            max_individual_alerts_per_run=_int("MAX_INDIVIDUAL_ALERTS_PER_RUN", 5, 1),
            heartbeat_local_time=heartbeat,
            source_down_alert_peak_min=_int("SOURCE_DOWN_ALERT_PEAK_MIN", 30, 5),
            source_down_alert_normal_min=_int("SOURCE_DOWN_ALERT_NORMAL_MIN", 120, 5),
            run_gap_alert_peak_min=_int("RUN_GAP_ALERT_PEAK_MIN", 20, 5),
            run_gap_alert_normal_min=_int("RUN_GAP_ALERT_NORMAL_MIN", 60, 5),
            db_path=os.environ.get("DB_PATH", "data/monitor.db").strip(),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            user_agent=os.environ.get("HTTP_USER_AGENT", cls.user_agent).strip() or cls.user_agent,
            http_timeout_s=float(os.environ.get("HTTP_TIMEOUT_S", "20")),
            shipping_overrides=overrides,
        )
