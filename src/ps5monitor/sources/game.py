"""GAME España: public PS5 listing page (schema.org ItemList + product cards) and product pages
(schema.org Product/Offer: price, availability, condition, seller). robots.txt only disallows /api etc.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import urljoin

from ps5monitor.models import Condition, Offer, SourceResult, Stock
from ps5monitor.normalization import canonical_url, classify, fold, parse_eur
from ps5monitor.sources.base import (
    Fetcher,
    SourceError,
    as_list,
    iter_typed,
    jsonld_blocks,
    schema_condition,
    schema_stock,
    strip_tags,
)

BASE = "https://www.game.es"
LISTING_URLS = ["https://www.game.es/buscar/ps5%20slim", "https://www.game.es/buscar/ps5%20slim?pagina=2"]
MAX_PRODUCT_PAGES = 10
_PRICE = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€")


def parse_listing(html: str) -> list[tuple[str, str, Decimal | None, Stock]]:
    """Return (name, absolute url, card price, card stock) for each product in the listing."""
    items: list[tuple[str, str, Decimal | None, Stock]] = []
    seen: set[str] = set()
    for block in jsonld_blocks(html):
        for li in iter_typed(block, "ListItem"):
            name, url = li.get("name"), li.get("url")
            if not name or not url or url in seen:
                continue
            seen.add(url)
            path = url.replace(BASE, "")
            idx = html.rfind(f'href="{path}"')
            price: Decimal | None = None
            stock = Stock.UNKNOWN
            if idx >= 0:
                card = strip_tags(html[idx: idx + 2500])
                m = _PRICE.search(card[:400])
                price = parse_eur(m.group(1)) if m else None
                # Stock words precede the price on each card; never read into the next card.
                f = fold(card[: m.end()] if m else card[:250])
                if "agotado" in f:
                    stock = Stock.OUT_OF_STOCK
                elif "reserva" in f or "preventa" in f:
                    stock = Stock.PREORDER
            items.append((name, urljoin(BASE, url), price, stock))
    return items


def parse_product(html: str, url: str, now: datetime, shipping_override: Decimal | None) -> Offer | None:
    products = [p for b in jsonld_blocks(html) for p in iter_typed(b, "Product")]
    if not products:
        return None
    p = products[0]
    name = str(p.get("name") or "")
    match = classify(name)
    offers = [o for o in as_list(p.get("offers")) if isinstance(o, dict)]
    if not offers:
        return None
    o = offers[0]
    seller = (o.get("seller") or {}).get("name") if isinstance(o.get("seller"), dict) else None
    condition = schema_condition(o.get("itemCondition"))
    if match.condition != Condition.UNKNOWN:
        condition = match.condition
    text = fold(strip_tags(html))
    shipping: Decimal | None = shipping_override
    ship_ev = "configuración GAME_SHIPPING_EUR" if shipping_override is not None else "no indicado en la página"
    if shipping is None:
        if re.search(r"envio (?:a domicilio )?gratis|envio gratuito|gastos de envio gratis", text):
            shipping, ship_ev = Decimal("0.00"), "página de producto: envío gratis"
        else:
            m = re.search(r"(?:envio a domicilio|gastos de envio)[^0-9]{0,30}(\d{1,3},\d{2}) ?€", text)
            if m:
                shipping, ship_ev = parse_eur(m.group(1)), "página de producto"
    return Offer(
        source="game", retailer="GAME", seller=seller or "GAME", title=name, url=canonical_url(url),
        variant=match.variant, checked_at=now, item_price=parse_eur(o.get("price")), shipping=shipping,
        currency=str(o.get("priceCurrency") or "EUR"), stock=schema_stock(o.get("availability")),
        condition=condition, is_bundle=match.is_bundle, bundle_note=match.bundle_note,
        model_confirmed=match.model_confirmed, verified_source=True,
        trusted_seller=fold(seller or "game") == "game", sku=str(p.get("sku") or "") or None,
        gtin=p.get("gtin13") or p.get("gtin"), notes=list(match.reasons),
        evidence={"precio/stock/vendedor": "schema.org Offer en la página de producto", "envío": ship_ev},
    )


def fetch(fetcher: Fetcher, now: datetime, max_total: Decimal, shipping_override: Decimal | None = None
          ) -> SourceResult:
    listing: list[tuple[str, str, Decimal | None, Stock]] = []
    for i, url in enumerate(LISTING_URLS):
        try:
            page = fetcher.get(url)
        except SourceError:
            if i == 0:
                raise
            continue  # later pages are best-effort
        listing.extend(parse_listing(page.text))
    if not listing:
        raise SourceError("listado sin productos (¿cambio de la web?)")
    offers: list[Offer] = []
    budget = MAX_PRODUCT_PAGES
    for name, url, card_price, card_stock in listing:
        match = classify(name)
        if match.variant.value not in ("slim_disc", "slim_digital"):
            continue
        needs_check = card_stock not in (Stock.OUT_OF_STOCK,) and (card_price is None or card_price <= max_total)
        if needs_check and budget > 0:
            budget -= 1
            page = fetcher.get(url)
            offer = parse_product(page.text, url, now, shipping_override)
            if offer is not None:
                offers.append(offer)
                continue
        offers.append(Offer(
            source="game", retailer="GAME", seller="GAME", title=name, url=canonical_url(url),
            variant=match.variant, checked_at=now, item_price=card_price, shipping=shipping_override,
            stock=card_stock, condition=match.condition if match.condition != Condition.UNKNOWN else Condition.NEW,
            is_bundle=match.is_bundle, bundle_note=match.bundle_note, model_confirmed=match.model_confirmed,
            verified_source=True, sku=url.rsplit("/", 1)[-1], notes=list(match.reasons),
            evidence={"precio/stock": "tarjeta del listado GAME"},
        ))
    return SourceResult("game", True, offers)
