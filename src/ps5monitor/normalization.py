"""Model matching, euro price parsing, URL canonicalization and risk-pattern detection."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit

from ps5monitor.models import Condition, Variant

_CENTS = Decimal("0.01")


def fold(text: str) -> str:
    """Lowercase and strip accents for robust keyword matching."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


# ---------------------------------------------------------------- prices

_PRICE_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.\s  ]\d{3})+|\d+)(?:([.,])(\d{1,2}|-{1,2}))?(?![\d])")


def parse_eur(text: str | float | int | Decimal | None) -> Decimal | None:
    """Parse a European/English formatted euro amount into Decimal (2 dp).

    Handles '549,99 €', '1.299,00€', '€549.99', '549', '549,-', '1 299,99'.
    Returns None for empty/unparseable input. Never uses float arithmetic on strings.
    """
    if text is None:
        return None
    if isinstance(text, Decimal):
        return text.quantize(_CENTS)
    if isinstance(text, int):
        return Decimal(text).quantize(_CENTS)
    if isinstance(text, float):
        return Decimal(repr(text)).quantize(_CENTS)
    s = str(text).replace("EUR", "").replace("€", "").strip()
    if not s:
        return None
    m = _PRICE_RE.search(s)
    if not m:
        return None
    integer, sep, frac = m.group(1), m.group(2), m.group(3)
    integer = re.sub(r"[.\s  ]", "", integer)
    if sep and frac and not frac.startswith("-"):
        amount = f"{integer}.{frac.ljust(2, '0')}"
    else:
        amount = integer
    try:
        return Decimal(amount).quantize(_CENTS)
    except InvalidOperation:
        return None


def fmt_eur(value: Decimal | None) -> str:
    if value is None:
        return "—"
    q = value.quantize(_CENTS)
    whole, frac = f"{q:.2f}".split(".")
    whole = f"{int(whole):,}".replace(",", ".")
    return f"{whole},{frac} €"


# ---------------------------------------------------------------- URLs

_TRACKING = re.compile(r"^(utm_|gclid|fbclid|ref|tag|aff|campaign|source|mc_)", re.I)


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = "https"
    host = parts.netloc.lower()
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = "&".join(
        q for q in parts.query.split("&") if q and not _TRACKING.match(q.split("=", 1)[0])
    )
    return urlunsplit((scheme, host, path, query, ""))


# ---------------------------------------------------------------- model matching

_EXCLUDE_ANYWHERE = {
    "ps4": r"\bps4\b|playstation 4",
    "ps3": r"\bps3\b|playstation 3",
    "otra consola": r"\bxbox\b|nintendo|\bswitch\b|steam deck",
    "PlayStation Portal": r"\bportal\b",
    "PS VR": r"\bvr2?\b|psvr",
    "caja vacía": r"caja vacia|empty box|solo caja",
    "alquiler/suscripción": r"alquiler|renting|suscripcion|subscription|ps plus|playstation plus",
    "tarjeta regalo": r"tarjeta regalo|gift ?card|psn card|saldo psn",
}
_ACCESSORY = (
    r"funda|carcasa|skin|vinilo|pegatina|soporte|\bstand\b|cooling|refrigera|ventilador|cargador|"
    r"base de carga|estacion de carga|dock\b|\bcable\b|auricular|headset|cascos|volante|"
    r"unidad de disco|lector de disco|disc drive|tapas?\b|cubierta|cover\b|faceplate|placas? lateral|"
    r"protector|almacenamiento|\bssd\b(?!.*consola)|disco duro|mando\b|dualsense|controller|"
    r"juego para|videojuego|\bjuego\b|limpiador|filtro antipolvo|organizador|maletin|bolsa"
)
_CONSOLE_HINT = (
    r"consola|\bslim\b|chassis [de]\b|cfi-?2\d{3}|edicion digital|digital edition|edicion estandar|"
    r"standard edition|\b1 ?tb\b|\b825 ?gb\b|\b1000 ?gb\b|\b1024 ?gb\b"
)
_PRO = r"\bps5 ?pro\b|playstation ?5 ?pro\b|edicion pro\b|\bpro edition\b|\bpro 2 ?tb\b|\bpro\b"
_FAT = r"cfi-?1[0-2]\d{2}|chassis [abc]\b|modelo original|\bfat\b|primera generacion"
_SLIM = r"\bslim\b|chassis [de]\b|cfi-?2\d{3}"
_ONE_TB = r"\b1 ?tb\b|\b1000 ?gb\b|\b1024 ?gb\b"
_DIGITAL = r"digital|sin lector|sin unidad de disco|without disc"
_BUNDLE = r"\bpack\b|bundle|voucher|\+|\bcon (?:juego|codigo|mando)|incluye|\baon\b|edicion limitada"
_USED = {
    Condition.REFURBISHED: r"reacondicionad|refurbish|renewed|renovad|outlet|grado [abc]\b",
    Condition.USED: r"segunda mano|\busad[oa]\b|seminuev|de ocasion|como nuevo|abierto|open box",
}


@dataclass
class Match:
    variant: Variant
    model_confirmed: bool
    is_bundle: bool
    condition: Condition
    reasons: list[str] = field(default_factory=list)
    bundle_note: str | None = None


def classify(title: str) -> Match:
    """Positive/negative matching of a listing title against the PS5 Slim model.

    Only the title is used (descriptions mention unrelated words such as 'ProPLAY').
    """
    t = fold(title)
    reasons: list[str] = []
    condition = Condition.UNKNOWN
    for cond, pat in _USED.items():
        if re.search(pat, t):
            condition = cond
            reasons.append(f"estado: {cond.value}")
            break
    for label, pat in _EXCLUDE_ANYWHERE.items():
        if re.search(pat, t):
            return Match(Variant.OTHER, False, False, condition, [f"excluido: {label}"])
    is_ps5 = bool(re.search(r"\bps5\b|playstation ?5\b|playstation5", t))
    if not is_ps5:
        return Match(Variant.OTHER, False, False, condition, ["no es PS5"])
    has_console = bool(re.search(_CONSOLE_HINT, t))
    for_console = bool(re.search(r"\baccesorio|para (?:la |tu |tus )?(?:consolas?|ps5|playstation)|compatible con", t))
    is_console_product = bool(re.search(r"\bconsola\b", t)) and not for_console
    if for_console or (re.search(_ACCESSORY, t) and not (
            is_console_product or (re.search(_BUNDLE, t) and has_console))):
        return Match(Variant.ACCESSORY, False, False, condition, ["accesorio/juego, no consola"])
    if re.search(_PRO, t):
        return Match(Variant.PRO, False, False, condition, ["PS5 Pro"])
    if re.search(_FAT, t):
        return Match(Variant.FAT, False, False, condition, ["PS5 original (no Slim)"])
    if not has_console:
        return Match(Variant.OTHER, False, False, condition, ["no se identifica como consola"])

    is_bundle = bool(re.search(_BUNDLE, t))
    bundle_note = None
    if is_bundle:
        bundle_note = "pack con seguro AON (obligatorio)" if re.search(r"\baon\b", t) else "pack/bundle"
    digital = bool(re.search(_DIGITAL, t))
    slim = bool(re.search(_SLIM, t))
    one_tb = bool(re.search(_ONE_TB, t))
    if slim or one_tb:
        # Only the Slim generation ships with 1 TB (Pro is 2 TB, original was 825 GB).
        variant = Variant.SLIM_DIGITAL if digital else Variant.SLIM_DISC
        if not slim:
            reasons.append("Slim deducido por 1 TB")
        return Match(variant, True, is_bundle, condition, reasons, bundle_note)
    # 825 GB without explicit Slim marker: could be the 2025 Slim Digital or an old original model.
    variant = Variant.SLIM_DIGITAL if digital else Variant.SLIM_DISC
    reasons.append("modelo Slim no confirmado en el título")
    return Match(variant, False, is_bundle, condition, reasons, bundle_note)


# ---------------------------------------------------------------- misleading price patterns

_RISKS = {
    "financiación/cuota mensual": r"/ ?mes\b|al mes\b|\bcuotas?\b|financia|meses sin intereses|\bmensual",
    "precio con entrega de consola usada": r"entregando|al entregar|con tu (?:antigua|vieja)|trade.?in|"
    r"recompra|plan renove|valoracion de tu",
    "precio solo para socios/miembros": r"solo (?:para )?(?:socios|miembros)|exclusivo (?:para )?(?:socios|miembros"
    r"|prime)|precio socio|con tarjeta (?:club|socio)|precio miembro",
    "reserva/señal (no es el total)": r"\bsenal\b|pago inicial|entrada de|deposito",
    "precio 'desde' (no es un total concreto)": r"\bdesde \d",
}
_COUPON = r"\bcupon\b|\bcodigo\b|\bcode\b|aplicando|al aplicar"


def detect_risks(*texts: str) -> tuple[list[str], bool]:
    """Return (blocking risk flags, coupon_mentioned)."""
    t = fold(" ".join(x for x in texts if x))
    flags = [label for label, pat in _RISKS.items() if re.search(pat, t)]
    return flags, bool(re.search(_COUPON, t))
