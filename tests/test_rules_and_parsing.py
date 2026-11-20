"""Requirements 1-5, 8 and source parsing (mocked fixtures only)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from conftest import fixture, mk_offer

from ps5monitor.models import Status, Stock, Variant
from ps5monitor.normalization import canonical_url, classify, detect_risks, fmt_eur, parse_eur
from ps5monitor.rules import evaluate
from ps5monitor.sources import chollometro, game, mediamarkt

MAX = Decimal("575.00")
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


# 1. Inclusive ceiling with exact decimals
@pytest.mark.parametrize("price,status", [("574.99", Status.ELIGIBLE), ("575.00", Status.ELIGIBLE),
                                          ("575.01", Status.REJECTED)])
def test_ceiling_is_inclusive(price: str, status: Status) -> None:
    assert evaluate(mk_offer(price), MAX).status == status


# 2-3. Mandatory shipping is part of the total
def test_shipping_pushes_over_limit() -> None:
    d = evaluate(mk_offer("565", shipping="15"), MAX)
    assert d.status == Status.REJECTED and d.total == Decimal("580.00")


def test_shipping_exactly_at_limit() -> None:
    d = evaluate(mk_offer("565", shipping="10"), MAX)
    assert d.status == Status.ELIGIBLE and d.total == Decimal("575.00")


def test_unknown_shipping_is_never_claimed_as_within_limit() -> None:
    d = evaluate(mk_offer("560", shipping=None), MAX)
    assert d.status == Status.UNVERIFIED and d.total is None


# 4. Model matching
@pytest.mark.parametrize("title,variant,confirmed", [
    ("PlayStation 5 Slim Digital Edition 1TB", Variant.SLIM_DIGITAL, True),
    ("Consola PS5 Slim Estándar con lector 1 TB", Variant.SLIM_DISC, True),
    ("PlayStation 5 1TB Pack Voucher EA Sports FC26", Variant.SLIM_DISC, True),
    ("Consola PlayStation 5 Digital Chassis E AON", Variant.SLIM_DIGITAL, True),
    ("Consola PS5 - SONY PlayStation 5 Digital Edition, 825 GB", Variant.SLIM_DIGITAL, False),
    ("PS5 Pro 2TB", Variant.PRO, False),
    ("PlayStation 5 Edición PRO 2TB Pack Voucher", Variant.PRO, False),
    ("PS5 CFI-1216A chasis original", Variant.FAT, False),
    ("Consola PlayStation 5 Chassis B", Variant.FAT, False),
    ("Funda de silicona para PS5 Slim", Variant.ACCESSORY, False),
    ("Accesorio PS5 - Sony Soporte vertical para consolas PS5", Variant.ACCESSORY, False),
    ("Unidad de disco para PS5 Digital Slim", Variant.ACCESSORY, False),
    ("Mando DualSense PS5 Slim edición blanca", Variant.ACCESSORY, False),
    ("PS5 Mando + cable de carga", Variant.ACCESSORY, False),
    ("PlayStation 5 + 2 mandos DualSense", Variant.SLIM_DISC, False),
    ("PlayStation 5 digital + 2 mandos DualSense", Variant.SLIM_DIGITAL, False),
    ("PACK PlayStation 5 + Marvel Lobezno PS5", Variant.SLIM_DISC, False),
])
def test_classify(title: str, variant: Variant, confirmed: bool) -> None:
    m = classify(title)
    assert m.variant == variant
    if variant in (Variant.SLIM_DISC, Variant.SLIM_DIGITAL):
        assert m.model_confirmed is confirmed


@pytest.mark.parametrize("title", ["PlayStation Portal para PS5", "Consola PS4 Slim 1TB", "Caja vacía PS5 Slim",
                                   "PS5 Slim reacondicionada 1TB", "PS Plus 12 meses PS5"])
def test_other_exclusions(title: str) -> None:
    m = classify(title)
    assert m.variant not in (Variant.SLIM_DISC, Variant.SLIM_DIGITAL) or m.condition.value != "unknown"
    o = mk_offer("400", title=title)
    o.variant, o.condition = m.variant, (m.condition if m.condition.value != "unknown" else o.condition)
    assert evaluate(o, MAX).status == Status.REJECTED


def test_disc_and_digital_are_separate_listings() -> None:
    a = mk_offer("500", variant=Variant.SLIM_DISC)
    b = mk_offer("500", variant=Variant.SLIM_DIGITAL)
    assert a.listing_id != b.listing_id


# 5. No minimum discount: a 575.00 listing without any reference price is eligible
def test_575_without_discount_is_eligible() -> None:
    o = mk_offer("575.00")
    assert o.reference_price is None
    assert evaluate(o, MAX).status == Status.ELIGIBLE


# Prices
@pytest.mark.parametrize("raw,expected", [("549,99 €", "549.99"), ("1.299,00€", "1299.00"), ("€549.99", "549.99"),
                                          ("549", "549.00"), ("549,-", "549.00"), ("1 299,99 €", "1299.99"),
                                          (549.99, "549.99"), ("575,0", "575.00"), ("", None), ("gratis", None)])
def test_parse_eur(raw: object, expected: str | None) -> None:
    got = parse_eur(raw)  # type: ignore[arg-type]
    assert (str(got) if got is not None else None) == expected


def test_fmt_eur() -> None:
    assert fmt_eur(Decimal("1299.5")) == "1.299,50 €"


# 8. Financing, trade-in, membership and "desde" prices are not checkout totals
@pytest.mark.parametrize("text", ["PS5 Slim por 24,99€/mes", "PS5 Slim 299€ entregando tu PS4",
                                  "Precio exclusivo para socios: 499€", "PS5 Slim desde 399€",
                                  "Paga 50€ de señal y reserva"])
def test_misleading_prices_are_flagged(text: str) -> None:
    flags, _ = detect_risks(text)
    assert flags
    o = mk_offer("299")
    o.risk_flags = flags
    assert evaluate(o, MAX).status == Status.REJECTED


def test_marketplace_seller_rejected_by_default() -> None:
    o = mk_offer("500", seller="Tienda X", trusted=False)
    assert evaluate(o, MAX).status == Status.REJECTED
    assert evaluate(o, MAX, allow_marketplace=True).status == Status.UNVERIFIED


def test_out_of_stock_not_alerted() -> None:
    assert evaluate(mk_offer("500", stock=Stock.OUT_OF_STOCK), MAX).status == Status.REJECTED
    assert evaluate(mk_offer("500", stock=Stock.PREORDER), MAX).status == Status.REJECTED


# Source parsers on synthetic fixtures
def test_chollometro_feed() -> None:
    offers = {o.external_id: o for o in chollometro.parse_feed(fixture("chollometro_rss.xml"), NOW)}
    assert set(offers) == {"https://www.chollometro.com/ofertas/ps5-slim-digital-1001", "g1004", "g1005", "g1006"}
    a = offers["https://www.chollometro.com/ofertas/ps5-slim-digital-1001"]
    assert a.item_price == Decimal("449.99") and a.shipping == Decimal("0.00") and a.retailer == "Amazon"
    assert "utm_source" not in a.url
    assert evaluate(a, MAX).status == Status.UNVERIFIED  # community data is never "verified"
    assert evaluate(offers["g1004"], MAX).status == Status.REJECTED  # monthly financing
    assert evaluate(offers["g1005"], MAX).status == Status.REJECTED  # trade-in
    d = evaluate(offers["g1006"], MAX)
    assert d.status == Status.UNVERIFIED and any("cupón" in r for r in d.reasons)


def test_game_listing_and_product() -> None:
    items = game.parse_listing(fixture("game_listing.html"))
    assert [(n[:20], p, s) for n, _, p, s in items] == [
        ("PlayStation 5 Modelo", Decimal("549.99"), Stock.UNKNOWN),
        ("PlayStation 5 Edició", Decimal("799.99"), Stock.UNKNOWN),
        ("PlayStation 5 Edició", Decimal("449.99"), Stock.OUT_OF_STOCK),
    ]
    o = game.parse_product(fixture("game_product.html"), items[0][1], NOW, None)
    assert o is not None
    assert (o.item_price, o.shipping, o.stock, o.seller, o.trusted_seller) == (
        Decimal("549.99"), Decimal("0.00"), Stock.IN_STOCK, "GAME", True)
    assert evaluate(o, MAX).status == Status.ELIGIBLE


def test_mediamarkt_product_sellers() -> None:
    offers = mediamarkt.parse_product(fixture("mediamarkt_product.html"), "https://www.mediamarkt.es/x", NOW)
    own, other = offers
    assert own.trusted_seller and evaluate(own, MAX).status == Status.ELIGIBLE and own.total == Decimal("569.00")
    assert not other.trusted_seller and evaluate(other, MAX).status == Status.REJECTED
    assert own.listing_id != other.listing_id
    assert own.url == canonical_url("https://www.mediamarkt.es/es/product/_ps5-slim-digital-153492044.html")
