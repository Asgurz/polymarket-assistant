"""
Strategy simulator for the UP/DOWN straddle strategy.

The strategy:
  - Place a limit buy on UP at `up_entry` cents
  - Place a limit buy on DOWN at `dn_entry` cents
  - Both orders being filled means you bought both sides at a discount
  - Since exactly one side ALWAYS wins (pays 1.0), your P&L = 1.0 - up_entry - dn_entry

A fill is counted when the minimum price seen for that side during a market
was at or below the entry price.
"""


def run_simulation(market_history: list[dict], up_entry: float, dn_entry: float) -> dict:
    """
    Simulate buying UP at up_entry and DOWN at dn_entry across recorded markets.

    Parameters
    ----------
    market_history : list of market records from feeds.State.market_history
    up_entry       : the price you want to buy UP at (e.g. 0.33)
    dn_entry       : the price you want to buy DOWN at (e.g. 0.33)

    Returns
    -------
    dict with full simulation statistics
    """
    if not market_history:
        return _empty_result(up_entry, dn_entry)

    markets_with_data = [
        m for m in market_history
        if m.get("min_up") is not None or m.get("min_dn") is not None
    ]

    total_markets = len(markets_with_data)
    if total_markets == 0:
        return _empty_result(up_entry, dn_entry)

    up_filled_count   = 0
    dn_filled_count   = 0
    both_filled_count = 0
    trades            = []

    total_pnl         = 0.0
    total_invested    = 0.0
    wins_up           = 0
    wins_dn           = 0
    known_outcome     = 0

    for m in markets_with_data:
        min_up   = m.get("min_up")
        min_dn   = m.get("min_dn")
        outcome  = m.get("outcome")  # "UP", "DOWN", or None

        up_hit = min_up is not None and min_up <= up_entry
        dn_hit = min_dn is not None and min_dn <= dn_entry

        if up_hit:
            up_filled_count += 1
        if dn_hit:
            dn_filled_count += 1

        if up_hit and dn_hit:
            both_filled_count += 1
            cost   = up_entry + dn_entry
            payout = 1.0          # one side always wins
            pnl    = payout - cost
            total_pnl      += pnl
            total_invested += cost
            if outcome is not None:
                known_outcome += 1
                if outcome == "UP":
                    wins_up += 1
                elif outcome == "DOWN":
                    wins_dn += 1
            trades.append({
                "slug":     m.get("slug", ""),
                "min_up":   round(min_up, 4)   if min_up  is not None else None,
                "min_dn":   round(min_dn, 4)   if min_dn  is not None else None,
                "outcome":  outcome,
                "cost":     round(cost, 4),
                "pnl":      round(pnl, 4),
            })

    # Single-side analysis (for insight – not the core strategy)
    single_up_pnl = 0.0
    single_dn_pnl = 0.0
    for m in markets_with_data:
        min_up  = m.get("min_up")
        min_dn  = m.get("min_dn")
        outcome = m.get("outcome")
        up_hit  = min_up is not None and min_up <= up_entry
        dn_hit  = min_dn is not None and min_dn <= dn_entry

        if up_hit and not dn_hit and outcome is not None:
            single_up_pnl += (1.0 - up_entry) if outcome == "UP" else -up_entry

        if dn_hit and not up_hit and outcome is not None:
            single_dn_pnl += (1.0 - dn_entry) if outcome == "DOWN" else -dn_entry

    roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0

    return {
        "up_entry":          up_entry,
        "dn_entry":          dn_entry,
        "total_markets":     total_markets,
        "up_filled_count":   up_filled_count,
        "dn_filled_count":   dn_filled_count,
        "both_filled_count": both_filled_count,
        "total_pnl":         round(total_pnl, 4),
        "total_invested":    round(total_invested, 4),
        "roi_pct":           round(roi, 2),
        "pnl_per_trade":     round(total_pnl / both_filled_count, 4) if both_filled_count else 0.0,
        "profitable":        total_pnl > 0 if both_filled_count > 0 else None,
        "wins_up":           wins_up,
        "wins_dn":           wins_dn,
        "known_outcome":     known_outcome,
        "single_up_pnl":     round(single_up_pnl, 4),
        "single_dn_pnl":     round(single_dn_pnl, 4),
        "trades":            trades[-100:],   # last 100 entries
    }


def _empty_result(up_entry: float, dn_entry: float) -> dict:
    return {
        "up_entry":          up_entry,
        "dn_entry":          dn_entry,
        "total_markets":     0,
        "up_filled_count":   0,
        "dn_filled_count":   0,
        "both_filled_count": 0,
        "total_pnl":         0.0,
        "total_invested":    0.0,
        "roi_pct":           0.0,
        "pnl_per_trade":     0.0,
        "profitable":        None,
        "wins_up":           0,
        "wins_dn":           0,
        "known_outcome":     0,
        "single_up_pnl":     0.0,
        "single_dn_pnl":     0.0,
        "trades":            [],
        "note":              "No market history recorded yet. Data accumulates as markets close.",
    }
