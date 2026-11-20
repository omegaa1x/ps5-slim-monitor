"""Eligibility evaluation (<= MAX_TOTAL_PRICE_EUR, inclusive) and repeat-alert policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from ps5monitor.models import Condition, Decision, Offer, Status, Stock, Variant

SLIM = (Variant.SLIM_DISC, Variant.SLIM_DIGITAL)


def is_within_ceiling(total: Decimal, max_total: Decimal) -> bool:
    """The user's rule: total_price_eur <= MAX_TOTAL_PRICE_EUR (exact Decimal comparison)."""
    return total <= max_total


def evaluate(offer: Offer, max_total: Decimal, *, allow_marketplace: bool = False,
             alert_preorders: bool = False) -> Decision:
    reasons: list[str] = []
    if offer.variant not in SLIM:
        return Decision(Status.REJECTED, None, [f"no es PS5 Slim ({offer.variant.value})"])
    if offer.currency != "EUR":
        return Decision(Status.REJECTED, None, ["moneda no EUR"])
    if offer.condition in (Condition.USED, Condition.REFURBISHED):
        return Decision(Status.REJECTED, None, [f"no es nueva ({offer.condition.value})"])
    if offer.item_price is None:
        return Decision(Status.REJECTED, None, ["sin precio"])
    if offer.risk_flags:
        return Decision(Status.REJECTED, None, [f"precio engañoso: {', '.join(offer.risk_flags)}"])
    if not is_within_ceiling(offer.item_price, max_total):
        return Decision(Status.REJECTED, None, ["precio del artículo por encima del límite"])

    unverified = False
    if offer.verified_source:
        if offer.stock == Stock.PREORDER and alert_preorders:
            reasons.append("reserva (preventa)")
        elif offer.stock != Stock.IN_STOCK:
            return Decision(Status.REJECTED, None, [f"sin stock confirmado ({offer.stock.value})"])
        if offer.condition != Condition.NEW:
            unverified = True
            reasons.append("estado 'nuevo' no confirmado")
        if not offer.trusted_seller:
            if not allow_marketplace:
                return Decision(Status.REJECTED, None, [f"vendedor de marketplace no fiable: {offer.seller}"])
            unverified = True
            reasons.append(f"vendedor de marketplace: {offer.seller}")
    else:
        unverified = True
        reasons.append("publicado por usuarios; no comprobado en la web de la tienda")

    total = offer.total
    if total is None:
        unverified = True
        reasons.append("gastos de envío no confirmados")
    elif not is_within_ceiling(total, max_total):
        return Decision(Status.REJECTED, total, ["total con envío por encima del límite"])
    if not offer.model_confirmed:
        unverified = True
        reasons.append("modelo Slim no confirmado")
    if "cupón" in offer.notes:
        unverified = True
        reasons.append("requiere cupón (comprobar en el enlace)")
    return Decision(Status.UNVERIFIED if unverified else Status.ELIGIBLE, total, reasons)


@dataclass
class AlertState:
    last_alert_price: Decimal | None
    last_alert_at: datetime | None
    armed: bool  # True when the listing left eligibility since the last alert
    last_exit_reason: str | None  # "stock" | "price" | None


@dataclass
class AlertDecision:
    send: bool
    kind: str  # new | drop | restock | below_ceiling | none


def decide_alert(state: AlertState | None, price: Decimal, now: datetime, *, drop_step: Decimal,
                 global_lowest: Decimal | None) -> AlertDecision:
    """Decide whether a currently qualifying listing deserves a (new or repeat) alert.

    - Never alerted before -> 'new'.
    - Alerted and still qualifying -> only on a further drop >= drop_step, or any drop that sets a new
      overall lowest price.
    - Re-entered eligibility after leaving: restock -> alert; price re-entry -> alert unless it is just an
      oscillation (same/higher price than last alert within 24 h).
    """
    if state is None or state.last_alert_price is None:
        return AlertDecision(True, "new")
    drop = state.last_alert_price - price
    new_low = global_lowest is None or price < global_lowest
    if state.armed:
        if state.last_exit_reason == "stock":
            return AlertDecision(True, "restock")
        recent = state.last_alert_at is not None and now - state.last_alert_at < timedelta(hours=24)
        if drop > 0 or not recent:
            return AlertDecision(True, "below_ceiling")
        return AlertDecision(False, "none")
    if drop >= drop_step or (drop > 0 and new_low):
        return AlertDecision(True, "drop")
    return AlertDecision(False, "none")
