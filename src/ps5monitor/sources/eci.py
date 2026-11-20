"""El Corte Inglés group (elcorteingles.es, hipercor.es): public PS5 console category page.

The category page embeds a `dataLayer` product list (name, price, status, provider). Candidates that are
not sold out and are <= the ceiling are confirmed on the product page's schema.org Product/Offer.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from ps5monitor.models import Condition, Offer, SourceResult, Stock
from ps5monitor.normalization import canonical_url, classify, fold, parse_eur
from ps5monitor.sources.base import (
    Fetcher,
    SourceError,
    as_list,
    iter_typed,
    jsonld_blocks,
    schema_condition,
    schema_shipping,
    schema_stock,
)

SITES = {
    "elcorteingles": ("El Corte Inglés", "https://www.elcorteingles.es"),
    "hipercor": ("Hipercor", "https://www.hipercor.es"),
}
CATEGORY_PATH = "/videojuegos/ps5/consolas/"
MAX_PRODUCT_PAGES = 6
_DL = re.compile(r"dataLayer\s*=\s*(\[.*?\]);\s*(?:</script>|\n|var |window)", re.S)


def _status(raw: str | None) -> Stock:
    s = (raw or "").upper()
    if "SOLD" in s or "OUT" in s or "AGOTADO" in s:
        return Stock.OUT_OF_STOCK
    if "PRE" in s or "RESERV" in s or "COMING" in s:
        return Stock.PREORDER
    if s in ("AVAILABLE", "ADD", "ADD_TO_CART", "IN_STOCK", "BUY"):
        return Stock.IN_STOCK
    return Stock.UNKNOWN


def _trusted(provider: str | None) -> bool:
    return fold(provider or "") in ("el corte ingles", "hipercor")


def parse_category(html: str, base_url: str) -> list[dict[str, Any]]:
    m = _DL.search(html)
    if not m:
        raise SourceError("no se encontró la lista de productos (¿cambio de la web?)")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise SourceError("lista de productos ilegible") from exc
    out: list[dict[str, Any]] = []
    for entry in as_list(data):
        for p in as_list(entry.get("products") if isinstance(entry, dict) else None):
            code = p.get("code_a")
            if not code or not p.get("name"):
                continue
            link = re.search(rf'href="(/[^"]*/{re.escape(code)}-[^"]+)"', html)
            out.append({
                "name": str(p["name"]), "code": code, "status": _status(p.get("status")),
                "price": parse_eur((p.get("price") or {}).get("f_price")), "provider": p.get("eci_provider"),
                "gtin": p.get("gtin"), "url": base_url + link.group(1) if link else None,
            })
    return out


def parse_product(html: str, url: str, now: datetime, source: str, retailer: str, provider: str | None,
                  shipping_override: Decimal | None) -> Offer | None:
    products = [p for b in jsonld_blocks(html) for p in iter_typed(b, "Product")]
    if not products:
        return None
    p = products[0]
    offers = [o for o in as_list(p.get("offers")) if isinstance(o, dict)]
    if not offers:
        return None
    o = offers[0]
    name = str(p.get("name") or "")
    match = classify(name)
    ship_raw = schema_shipping(o)
    shipping = parse_eur(ship_raw) if ship_raw is not None else shipping_override
    seller = (o.get("seller") or {}).get("name") if isinstance(o.get("seller"), dict) else None
    seller = seller or provider or retailer
    cond = schema_condition(o.get("itemCondition"))
    return Offer(
        source=source, retailer=retailer, seller=seller, title=name, url=canonical_url(url), variant=match.variant,
        checked_at=now, item_price=parse_eur(o.get("price")), shipping=shipping,
        currency=str(o.get("priceCurrency") or "EUR"), stock=schema_stock(o.get("availability")),
        condition=match.condition if match.condition != Condition.UNKNOWN else (
            cond if cond != Condition.UNKNOWN else Condition.NEW),
        is_bundle=match.is_bundle, bundle_note=match.bundle_note, model_confirmed=match.model_confirmed,
        verified_source=True, trusted_seller=_trusted(seller), sku=str(p.get("sku") or "") or None,
        gtin=p.get("gtin13") or p.get("gtin"), notes=list(match.reasons),
        evidence={"precio/stock": "schema.org Offer en la página de producto",
                  "envío": "schema.org shippingRate" if ship_raw is not None else (
                      "configuración" if shipping_override is not None else "no indicado en la página")},
    )


def fetch(source: str, fetcher: Fetcher, now: datetime, max_total: Decimal,
          shipping_override: Decimal | None = None) -> SourceResult:
    retailer, base_url = SITES[source]
    items = parse_category(fetcher.get(base_url + CATEGORY_PATH).text, base_url)
    offers: list[Offer] = []
    budget = MAX_PRODUCT_PAGES
    for it in items:
        match = classify(it["name"])
        if match.variant.value not in ("slim_disc", "slim_digital"):
            continue
        url = it["url"] or base_url + CATEGORY_PATH
        if it["status"] != Stock.OUT_OF_STOCK and (it["price"] is None or it["price"] <= max_total) \
                and it["url"] and budget > 0:
            budget -= 1
            offer = parse_product(fetcher.get(it["url"]).text, it["url"], now, source, retailer, it["provider"],
                                  shipping_override)
            if offer is not None:
                offers.append(offer)
                continue
        offers.append(Offer(
            source=source, retailer=retailer, seller=it["provider"] or retailer, title=it["name"],
            url=canonical_url(url), variant=match.variant, checked_at=now, item_price=it["price"],
            shipping=shipping_override, stock=it["status"],
            condition=match.condition if match.condition != Condition.UNKNOWN else Condition.NEW,
            is_bundle=match.is_bundle, bundle_note=match.bundle_note, model_confirmed=match.model_confirmed,
            verified_source=True, trusted_seller=_trusted(it["provider"]), sku=it["code"], gtin=it["gtin"],
            notes=list(match.reasons), evidence={"precio/stock": "lista de la categoría (dataLayer)"},
        ))
    return SourceResult(source, True, offers)
