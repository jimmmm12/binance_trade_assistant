from __future__ import annotations


def maker_limit_price(
    side: str,
    *,
    best_bid: float | None = None,
    best_ask: float | None = None,
    reference_price: float | None = None,
    fallback_offset_bps: float = 3.0,
) -> float | None:
    """Return a passive entry price using the current top of book when available."""
    normalized_side = side.upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if normalized_side == "BUY" and best_bid is not None and best_bid > 0:
        return float(best_bid)
    if normalized_side == "SELL" and best_ask is not None and best_ask > 0:
        return float(best_ask)
    if reference_price is None or reference_price <= 0:
        return None
    offset = max(0.0, fallback_offset_bps) / 10_000
    return reference_price * (1 - offset if normalized_side == "BUY" else 1 + offset)
