"""Generic RSS deal feeds (WordPress-style) from Spanish deal sites. Prices come from editors/users,
so these offers are always UNVERIFIED. The final price is read from the title ('de X a Y' -> Y).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime

from ps5monitor.models import Offer, SourceResult, Stock
from ps5monitor.normalization import canonical_url, classify, detect_risks, fold, parse_eur
from ps5monitor.sources.base import Fetcher, SourceError, strip_tags

FEEDS = {
    "blogdechollos": ("Blogdechollos", "https://www.blogdechollos.com/feed/"),
    "ofertitas": ("Ofertitas", "https://www.ofertitas.es/feed/"),
}
KNOWN_RETAILERS = {
    "amazon": "Amazon", "el corte ingles": "El Corte Inglés", "mediamarkt": "MediaMarkt",
    "media markt": "MediaMarkt", "carrefour": "Carrefour", "pccomponentes": "PcComponentes", "fnac": "Fnac",
    "game": "GAME", "worten": "Worten", "alcampo": "Alcampo", "hipercor": "Hipercor", "xtralife": "Xtralife",
    "lidl": "Lidl", "aliexpress": "AliExpress", "ebay": "eBay", "miravia": "Miravia", "toys r us": "Toys R Us",
    "playstation direct": "PlayStation Direct", "eroski": "Eroski", "coolmod": "Coolmod",
}
_AMOUNT = re.compile(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:€|euros?\b)", re.I)
_FREE_SHIP = re.compile(r"envio gratis|envio gratuito|gastos de envio gratis|envio incluido")


def title_price(title: str) -> str | None:
    """'de 41,51€ a 32,98€' -> '32,98'; 'por sólo 449€' -> '449'; several amounts -> the last one."""
    f = fold(title)
    m = re.search(r"\bde\s+([\d.,]+)\s*(?:€|euros?)\s+a\s+([\d.,]+)\s*(?:€|euros?)", f)
    if m:
        return m.group(2)
    amounts = _AMOUNT.findall(f)
    return amounts[-1] if amounts else None


def detect_retailer(text: str) -> str:
    f = fold(text)
    for key, name in KNOWN_RETAILERS.items():
        if re.search(rf"\b(?:en|de|por)\s+{re.escape(key)}\b", f) or re.search(rf"\b{re.escape(key)}\b", f):
            return name
    return "tienda no indicada"


def parse_feed(xml_text: str, now: datetime, source: str) -> list[Offer]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceError(f"RSS inválido: {exc}") from exc
    offers: list[Offer] = []
    for item in root.iter("item"):
        title = strip_tags(item.findtext("title") or "")
        match = classify(title)
        if match.variant.value not in ("slim_disc", "slim_digital"):
            continue
        link = (item.findtext("link") or "").strip()
        desc = strip_tags(item.findtext("description") or "")
        raw = title_price(title)
        risks, coupon = detect_risks(title, desc)
        offers.append(Offer(
            source=source, retailer=detect_retailer(f"{title} {desc[:300]}"), title=title,
            url=canonical_url(link) if link else FEEDS[source][1], variant=match.variant, checked_at=now,
            item_price=parse_eur(raw), shipping=parse_eur(0) if _FREE_SHIP.search(fold(f"{title} {desc}")) else None,
            stock=Stock.UNKNOWN, condition=match.condition, is_bundle=match.is_bundle, bundle_note=match.bundle_note,
            model_confirmed=match.model_confirmed, verified_source=False,
            external_id=(item.findtext("guid") or link).strip(), risk_flags=risks,
            notes=(["cupón"] if coupon else []) + match.reasons,
            evidence={"precio": f"título del artículo en {FEEDS[source][0]}"},
        ))
    return offers


def fetch(source: str, fetcher: Fetcher, now: datetime) -> SourceResult:
    page = fetcher.get(FEEDS[source][1], conditional=True)
    if page.not_modified:
        return SourceResult(source, True, [], not_modified=True)
    if "<rss" not in page.text[:500]:
        raise SourceError("la respuesta no es un RSS")
    return SourceResult(source, True, parse_feed(page.text, now, source))
