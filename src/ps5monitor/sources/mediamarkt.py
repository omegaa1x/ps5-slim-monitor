"""MediaMarkt España: public search page (schema.org ItemList) + product pages (schema.org offers with
seller, availability, condition and shipping rate). Marketplace sellers are rejected by default.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ps5monitor.models import Offer, SourceResult, Stock
from ps5monitor.normalization import canonical_url, classify, fold, parse_eur
from ps5monitor.sources.base import (
    Fetcher,
    SourceError,
    as_list,
    iter_typed,
    jsonld_blocks,
    schema_condition,
    schema_stock,
)

SEARCH_URLS = ["https://www.mediamarkt.es/es/search.html?query=ps5%20slim"]
MAX_PRODUCT_PAGES = 6


def _trusted(seller: str | None) -> bool:
    return fold(seller or "").replace(" ", "") in ("mediamarkt", "mediamarktes", "mediamarktespana")


def parse_search(html: str) -> list[tuple[str, str, Decimal | None]]:
    out: list[tuple[str, str, Decimal | None]] = []
    for block in jsonld_blocks(html):
        for prod in iter_typed(block, "Product"):
            name, url = prod.get("name"), prod.get("url")
            if not name or not url:
                continue
            offers = [o for o in as_list(prod.get("offers")) if isinstance(o, dict)]
            price = parse_eur(offers[0].get("price")) if offers else None
            out.append((str(name), str(url), price))
    return out


def parse_product(html: str, url: str, now: datetime) -> list[Offer]:
    products = [p for b in jsonld_blocks(html) for p in iter_typed(b, "Product")]
    if not products:
        return []
    p = products[0]
    name = str(p.get("name") or "")
    match = classify(name)
    result: list[Offer] = []
    for o in as_list(p.get("offers")):
        if not isinstance(o, dict):
            continue
        seller = o.get("seller", {}).get("name") if isinstance(o.get("seller"), dict) else None
        shipping = None
        ship_ev = "no indicado"
        for sd in as_list(o.get("shippingDetails")):
            rate = sd.get("shippingRate") if isinstance(sd, dict) else None
            if isinstance(rate, dict) and rate.get("value") is not None:
                shipping, ship_ev = parse_eur(rate.get("value")), "schema.org shippingRate (España)"
                break
        cond = schema_condition(o.get("itemCondition"))
        result.append(Offer(
            source="mediamarkt", retailer="MediaMarkt", seller=seller or "desconocido", title=name,
            url=canonical_url(str(o.get("url") or url)), variant=match.variant, checked_at=now,
            item_price=parse_eur(o.get("price")), shipping=shipping,
            currency=str(o.get("priceCurrency") or "EUR"), stock=schema_stock(o.get("availability")),
            condition=match.condition if match.condition.value != "unknown" else cond,
            is_bundle=match.is_bundle, bundle_note=match.bundle_note, model_confirmed=match.model_confirmed,
            verified_source=True, trusted_seller=_trusted(seller), sku=str(p.get("sku") or "") or None,
            gtin=p.get("gtin13") or p.get("gtin"), notes=list(match.reasons),
            evidence={"precio/stock/vendedor": "schema.org Offer en la página de producto", "envío": ship_ev},
        ))
    return result


def fetch(fetcher: Fetcher, now: datetime, max_total: Decimal) -> SourceResult:
    listing: list[tuple[str, str, Decimal | None]] = []
    for url in SEARCH_URLS:
        listing.extend(parse_search(fetcher.get(url).text))
    if not listing:
        raise SourceError("búsqueda sin productos (¿cambio de la web?)")
    offers: list[Offer] = []
    budget = MAX_PRODUCT_PAGES
    seen: set[str] = set()
    for name, url, price in listing:
        if url in seen:
            continue
        seen.add(url)
        match = classify(name)
        if match.variant.value not in ("slim_disc", "slim_digital"):
            continue
        if (price is None or price <= max_total) and budget > 0:
            budget -= 1
            parsed = parse_product(fetcher.get(url).text, url, now)
            if parsed:
                offers.extend(parsed)
                continue
        # Listing price above the ceiling (or product page unavailable): keep history only.
        offers.append(Offer(
            source="mediamarkt", retailer="MediaMarkt", seller=None, title=name, url=canonical_url(url),
            variant=match.variant, checked_at=now, item_price=price, stock=Stock.UNKNOWN,
            is_bundle=match.is_bundle, model_confirmed=match.model_confirmed, verified_source=True,
            trusted_seller=False, notes=list(match.reasons), evidence={"precio": "listado de búsqueda"},
        ))
    return SourceResult("mediamarkt", True, offers)
