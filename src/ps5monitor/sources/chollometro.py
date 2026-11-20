"""Chollometro (Pepper network) official RSS feed: community-posted deals from all Spanish stores.

Prices are user-reported, so offers from here are never 'verified'; they are sent as UNVERIFIED alerts.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime

from ps5monitor.models import Offer, SourceResult, Stock
from ps5monitor.normalization import canonical_url, classify, detect_risks, fold, parse_eur
from ps5monitor.sources.base import Fetcher, SourceError, strip_tags

FEED_URL = "https://www.chollometro.com/rss/nuevos"
NS = {"pepper": "http://www.pepper.com/rss"}
_FREE_SHIP = re.compile(r"envio gratis|envio gratuito|gastos de envio gratis|envio incluido|sin gastos de envio")
_EXPIRED = re.compile(r"\bexpirad|\bagotad|\bfinalizad")


def parse_feed(xml_text: str, now: datetime) -> list[Offer]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceError(f"RSS inválido: {exc}") from exc
    offers: list[Offer] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        match = classify(title)
        if match.variant.value not in ("slim_disc", "slim_digital"):
            continue
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        desc_html = item.findtext("description") or ""
        desc = strip_tags(desc_html)
        merchant = item.find("pepper:merchant", NS)
        retailer = (merchant.get("name") if merchant is not None else None) or "tienda no indicada"
        raw_price = merchant.get("price") if merchant is not None else None
        price = parse_eur(raw_price)
        if price is None:
            m = re.search(r"<strong>\s*([^<]*?€)", desc_html)
            price = parse_eur(m.group(1)) if m else None
        risks, coupon = detect_risks(title, desc)
        folded = fold(f"{title} {desc}")
        offer = Offer(
            source="chollometro", retailer=retailer, title=title, url=canonical_url(link) if link else FEED_URL,
            variant=match.variant, checked_at=now, item_price=price,
            shipping=parse_eur(0) if _FREE_SHIP.search(folded) else None,
            stock=Stock.UNKNOWN, condition=match.condition, is_bundle=match.is_bundle,
            bundle_note=match.bundle_note, model_confirmed=match.model_confirmed, verified_source=False,
            trusted_seller=True, external_id=guid, risk_flags=risks,
            notes=(["cupón"] if coupon else []) + match.reasons,
            evidence={"precio": f"Chollometro RSS ({raw_price or 'texto'})",
                      "publicado": item.findtext("pubDate") or ""},
        )
        if _EXPIRED.search(fold(title)):
            offer.risk_flags.append("chollo marcado como expirado/agotado")
        if offer.shipping is not None:
            offer.evidence["envío"] = "según la publicación (envío gratis)"
        offers.append(offer)
    return offers


def fetch(fetcher: Fetcher, now: datetime) -> SourceResult:
    page = fetcher.get(FEED_URL, conditional=True)
    if page.not_modified:
        return SourceResult("chollometro", True, [], not_modified=True)
    if "<rss" not in page.text[:500]:
        raise SourceError("la respuesta no es un RSS")
    return SourceResult("chollometro", True, parse_feed(page.text, now))
