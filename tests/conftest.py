from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ps5monitor.config import Settings
from ps5monitor.models import Condition, Offer, Stock, Variant

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return dataclasses.replace(Settings(), db_path=str(tmp_path / "monitor.db"), heartbeat_local_time=None,
                               enabled_sources=("game", "mediamarkt"))


def mk_offer(price: str, *, shipping: str | None = "0", stock: Stock = Stock.IN_STOCK, source: str = "game",
             retailer: str = "GAME", seller: str = "GAME", variant: Variant = Variant.SLIM_DISC,
             title: str = "PlayStation 5 Slim 1TB", url: str = "https://www.game.es/ps5-slim/1001",
             sku: str = "1001", now: datetime | None = None, trusted: bool = True) -> Offer:
    return Offer(
        source=source, retailer=retailer, seller=seller, title=title, url=url, variant=variant,
        checked_at=now or datetime(2026, 11, 20, 12, 0, tzinfo=UTC), item_price=Decimal(price),
        shipping=None if shipping is None else Decimal(shipping), stock=stock, condition=Condition.NEW,
        sku=sku, trusted_seller=trusted,
    )
