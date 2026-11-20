"""El Corte Inglés group adapter, generic deal feeds and cross-source dedupe (mocked fixtures only)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from conftest import fixture, mk_offer
from test_scanner import World, alerts, run

from ps5monitor.config import Settings
from ps5monitor.models import Status, Stock
from ps5monitor.rules import evaluate
from ps5monitor.sources import deal_feeds, eci
from ps5monitor.sources.base import Fetcher
from ps5monitor.storage import Storage

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
MAX = Decimal("575.00")


def test_eci_category_and_product() -> None:
    items = eci.parse_category(fixture("eci_category.html"), "https://www.elcorteingles.es")
    assert [(i["code"], i["status"], i["price"]) for i in items] == [
        ("A100", Stock.IN_STOCK, Decimal("549.90")), ("A101", Stock.OUT_OF_STOCK, Decimal("499.90")),
        ("A102", Stock.IN_STOCK, Decimal("799.90"))]
    assert items[0]["url"] == "https://www.elcorteingles.es/videojuegos/A100-consola-playstation-5-slim-chasis-e-1tb/"

    pages = {"/videojuegos/ps5/consolas/": fixture("eci_category.html"),
             "/videojuegos/A100-consola-playstation-5-slim-chasis-e-1tb/": fixture("eci_product.html")}
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, text=pages[request.url.path])

    f = Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    result = eci.fetch("elcorteingles", f, NOW, MAX)
    by_sku = {o.sku: o for o in result.offers}
    assert set(by_sku) == {"A100", "A101"}  # Pro excluded
    assert len(requested) == 2  # sold-out item not fetched; only the in-stock candidate's product page
    slim = by_sku["A100"]
    assert (slim.total, slim.stock, slim.trusted_seller) == (Decimal("549.90"), Stock.IN_STOCK, True)
    assert evaluate(slim, MAX).status == Status.ELIGIBLE
    digital = by_sku["A101"]
    assert evaluate(digital, MAX).status == Status.REJECTED  # sold out (and model not confirmed)


def test_wordpress_feed_parsing() -> None:
    offers = {o.external_id: o for o in deal_feeds.parse_feed(fixture("wordpress_feed.xml"), NOW, "blogdechollos")}
    assert set(offers) == {"b1", "b2"}  # controller excluded
    assert (offers["b1"].item_price, offers["b1"].retailer, offers["b1"].shipping) == (
        Decimal("449.00"), "Amazon", None)
    assert (offers["b2"].item_price, offers["b2"].retailer, offers["b2"].shipping) == (
        Decimal("399.99"), "Carrefour", Decimal("0.00"))
    assert all(evaluate(o, MAX).status == Status.UNVERIFIED for o in offers.values())


def test_same_deal_from_two_feeds_alerts_once(settings: Settings) -> None:
    s = dataclasses.replace(settings, enabled_sources=("game", "chollometro", "blogdechollos"))
    st, w = Storage(s.db_path), World()
    w.offers.update({"chollometro": [], "blogdechollos": []})
    run(s, st, w, NOW)  # initialise
    feed = dict(source="chollometro", retailer="Amazon", seller=None, shipping=None, title="PS5 Slim Digital 1TB")
    a = mk_offer("449.00", url="https://www.chollometro.com/ofertas/1", sku="c1", **feed)  # type: ignore[arg-type]
    b = mk_offer("449.00", url="https://www.blogdechollos.com/ofertas/1", sku="b1",
                 **{**feed, "source": "blogdechollos"})  # type: ignore[arg-type]
    a.verified_source = b.verified_source = False
    w.offers["chollometro"], w.offers["blogdechollos"] = [a], [b]
    run(s, st, w, NOW + timedelta(minutes=20))
    assert alerts(st) == [("new", "449.00")]
    run(s, st, w, NOW + timedelta(minutes=40))  # still only once on later runs
    assert alerts(st) == [("new", "449.00")]


def test_verified_store_offer_suppresses_duplicate_feed_alert(settings: Settings) -> None:
    s = dataclasses.replace(settings, enabled_sources=("game", "chollometro"))
    st, w = Storage(s.db_path), World()
    w.offers.update({"chollometro": []})
    run(s, st, w, NOW)
    w.offers["game"] = [mk_offer("529.00")]
    fo = mk_offer("529.00", source="chollometro", seller=None, shipping=None, url="https://www.chollometro.com/x",
                  sku="c9")
    fo.verified_source = False
    w.offers["chollometro"] = [fo]
    run(s, st, w, NOW + timedelta(minutes=20))
    assert alerts(st) == [("new", "529.00")]
    rows = st.conn.execute("SELECT l.source FROM sent_alerts s JOIN listings l USING(listing_id)").fetchall()
    assert [r["source"] for r in rows] == ["game"]


def test_shipping_override_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SHIPPING_EUR_ELCORTEINGLES", "0")
    monkeypatch.setenv("GAME_SHIPPING_EUR", "3,99".replace(",", "."))
    st = Settings.from_env()
    assert st.shipping_for("elcorteingles") == Decimal("0.00") and st.shipping_for("game") == Decimal("3.99")
