"""Store-neutral offer record and evaluation results."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Variant(StrEnum):
    SLIM_DISC = "slim_disc"
    SLIM_DIGITAL = "slim_digital"
    PRO = "pro"
    FAT = "fat"
    ACCESSORY = "accessory"
    OTHER = "other"


class Stock(StrEnum):
    IN_STOCK = "in_stock"
    PREORDER = "preorder"
    OUT_OF_STOCK = "out_of_stock"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class Condition(StrEnum):
    NEW = "new"
    USED = "used"
    REFURBISHED = "refurbished"
    UNKNOWN = "unknown"


class Status(StrEnum):
    ELIGIBLE = "eligible"  # verified on the retailer page, total <= max
    UNVERIFIED = "unverified"  # plausible <= max but something could not be verified
    REJECTED = "rejected"


VARIANT_LABEL = {
    Variant.SLIM_DISC: "Slim Disco (con lector)",
    Variant.SLIM_DIGITAL: "Slim Digital (sin lector)",
}


@dataclass
class Offer:
    source: str
    retailer: str
    title: str
    url: str
    variant: Variant
    checked_at: datetime  # timezone-aware UTC
    seller: str | None = None
    item_price: Decimal | None = None
    shipping: Decimal | None = None  # None = unknown
    fees: Decimal = Decimal("0.00")
    currency: str = "EUR"
    stock: Stock = Stock.UNKNOWN
    condition: Condition = Condition.UNKNOWN
    is_bundle: bool = False
    bundle_note: str | None = None
    model_confirmed: bool = True
    verified_source: bool = True  # data read from the retailer's own page
    trusted_seller: bool = True
    sku: str | None = None
    gtin: str | None = None
    external_id: str | None = None  # e.g. Chollometro thread guid
    reference_price: Decimal | None = None  # seller-claimed, informational only
    risk_flags: list[str] = field(default_factory=list)  # financing, trade-in, membership, coupon...
    notes: list[str] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)

    @property
    def listing_id(self) -> str:
        """Stable id per retailer + seller + specific listing/variant."""
        key = "|".join(
            [
                self.source,
                self.retailer.lower(),
                (self.seller or "").lower(),
                self.external_id or self.sku or self.url,
                self.variant.value,
                "bundle" if self.is_bundle else "console",
            ]
        )
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    @property
    def total(self) -> Decimal | None:
        if self.item_price is None or self.shipping is None:
            return None
        return (self.item_price + self.shipping + self.fees).quantize(Decimal("0.01"))


@dataclass
class Decision:
    status: Status
    total: Decimal | None
    reasons: list[str]


@dataclass
class SourceResult:
    source: str
    ok: bool
    offers: list[Offer] = field(default_factory=list)
    error: str | None = None
    not_modified: bool = False
    requests: int = 0
