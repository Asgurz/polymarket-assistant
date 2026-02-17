import asyncio
import json
import time

import requests
import websockets
from datetime import datetime, timezone, timedelta

import config


class State:
    def __init__(self):
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []
        self.mid: float = 0.0

        self.trades: list[dict] = []

        self.klines: list[dict] = []
        self.cur_kline: dict | None = None

        self.pm_up_id:  str | None = None
        self.pm_dn_id:  str | None = None
        self.pm_up:     float | None = None
        self.pm_dn:     float | None = None

        # Market identity
        self.coin: str = ""
        self.tf: str = ""
        self.current_slug: str = ""
        self.market_expires_at: float = 0.0

        # Signals market refresh needed to pm_feed
        self.market_changed: asyncio.Event = asyncio.Event()

        # Per-market price tracking for strategy simulator
        # Each entry: {slug, min_up, max_up, min_dn, max_dn, outcome, timestamp}
        self.market_history: list[dict] = []
        # Temp accumulator for the current market
        self._cur_prices: dict = {}


OB_POLL_INTERVAL = 2


async def ob_poller(symbol: str, state: State):
    url = f"{config.BINANCE_REST}/depth"
    print(f"  [Binance OB] polling {symbol} every {OB_POLL_INTERVAL}s")
    while True:
        try:
            resp = requests.get(url, params={"symbol": symbol, "limit": 20}, timeout=3).json()
            state.bids = [(float(p), float(q)) for p, q in resp["bids"]]
            state.asks = [(float(p), float(q)) for p, q in resp["asks"]]
            if state.bids and state.asks:
                state.mid = (state.bids[0][0] + state.asks[0][0]) / 2
        except Exception:
            pass
        await asyncio.sleep(OB_POLL_INTERVAL)


async def binance_feed(symbol: str, kline_iv: str, state: State):
    sym = symbol.lower()
    streams = "/".join([
        f"{sym}@trade",
        f"{sym}@kline_{kline_iv}",
    ])
    url = f"{config.BINANCE_WS}?streams={streams}"

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=60,
                close_timeout=10
            ) as ws:
                print(f"  [Binance WS] connected – {symbol}")

                while True:
                    try:
                        data   = json.loads(await ws.recv())
                        stream = data.get("stream", "")
                        pay    = data["data"]

                        if "@trade" in stream:
                            state.trades.append({
                                "t":      pay["T"] / 1000.0,
                                "price":  float(pay["p"]),
                                "qty":    float(pay["q"]),
                                "is_buy": not pay["m"],
                            })
                            if len(state.trades) > 5000:
                                cut = time.time() - config.TRADE_TTL
                                state.trades = [t for t in state.trades if t["t"] >= cut]

                        elif "@kline" in stream:
                            k = pay["k"]
                            candle = {
                                "t": k["t"] / 1000.0,
                                "o": float(k["o"]), "h": float(k["h"]),
                                "l": float(k["l"]), "c": float(k["c"]),
                                "v": float(k["v"]),
                            }
                            state.cur_kline = candle
                            if k["x"]:
                                state.klines.append(candle)
                                state.klines = state.klines[-config.KLINE_MAX:]

                    except websockets.exceptions.ConnectionClosed:
                        print(f"  [Binance WS] connection closed, reconnecting...")
                        break

        except Exception as e:
            print(f"  [Binance WS] connection error: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)


async def bootstrap(symbol: str, interval: str, state: State):
    resp = requests.get(
        f"{config.BINANCE_REST}/klines",
        params={"symbol": symbol, "interval": interval, "limit": config.KLINE_BOOT},
    ).json()
    state.klines = [
        {
            "t": r[0] / 1e3,
            "o": float(r[1]), "h": float(r[2]),
            "l": float(r[3]), "c": float(r[4]),
            "v": float(r[5]),
        }
        for r in resp
    ]
    print(f"  [Binance] loaded {len(state.klines)} historical candles")


_MONTHS = ["", "january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december"]


def _et_now() -> datetime:
    utc = datetime.now(timezone.utc)
    year = utc.year

    mar1_dow  = datetime(year, 3, 1).weekday()
    mar_sun   = 1 + (6 - mar1_dow) % 7
    dst_start = datetime(year, 3, mar_sun + 7, 2, 0, 0, tzinfo=timezone.utc)

    nov1_dow = datetime(year, 11, 1).weekday()
    nov_sun  = 1 + (6 - nov1_dow) % 7
    dst_end  = datetime(year, 11, nov_sun, 6, 0, 0, tzinfo=timezone.utc)

    offset = timedelta(hours=-4) if dst_start <= utc < dst_end else timedelta(hours=-5)
    return utc + offset


def _et_offset_hours() -> int:
    utc = datetime.now(timezone.utc)
    year = utc.year
    mar1_dow  = datetime(year, 3, 1).weekday()
    mar_sun   = 1 + (6 - mar1_dow) % 7
    dst_start = datetime(year, 3, mar_sun + 7, 2, 0, 0, tzinfo=timezone.utc)
    nov1_dow = datetime(year, 11, 1).weekday()
    nov_sun  = 1 + (6 - nov1_dow) % 7
    dst_end  = datetime(year, 11, nov_sun, 6, 0, 0, tzinfo=timezone.utc)
    return -4 if dst_start <= utc < dst_end else -5


def _to_12h(hour24: int) -> str:
    if hour24 == 0:
        return "12am"
    if hour24 < 12:
        return f"{hour24}am"
    if hour24 == 12:
        return "12pm"
    return f"{hour24 - 12}pm"


def _build_slug(coin: str, tf: str) -> str | None:
    now_utc = datetime.now(timezone.utc)
    now_ts  = int(now_utc.timestamp())
    et      = _et_now()

    if tf == "5m":
        ts = (now_ts // 300) * 300
        return f"{config.COIN_PM[coin]}-updown-5m-{ts}"

    if tf == "15m":
        ts = (now_ts // 900) * 900
        return f"{config.COIN_PM[coin]}-updown-15m-{ts}"

    if tf == "4h":
        ts = ((now_ts - 3600) // 14400) * 14400 + 3600
        return f"{config.COIN_PM[coin]}-updown-4h-{ts}"

    if tf == "1h":
        return (f"{config.COIN_PM_LONG[coin]}-up-or-down-"
                f"{_MONTHS[et.month]}-{et.day}-{_to_12h(et.hour)}-et")

    if tf == "daily":
        resolution = et.replace(hour=12, minute=0, second=0, microsecond=0)
        target      = et if et < resolution else et + timedelta(days=1)
        return (f"{config.COIN_PM_LONG[coin]}-up-or-down-on-"
                f"{_MONTHS[target.month]}-{target.day}")

    return None


def _next_market_expiry(tf: str) -> float:
    """Return the Unix timestamp when the current market expires."""
    now_ts = time.time()
    if tf == "5m":
        return (int(now_ts // 300) + 1) * 300.0
    if tf == "15m":
        return (int(now_ts // 900) + 1) * 900.0
    if tf == "4h":
        return (int((now_ts - 3600) // 14400) + 1) * 14400.0 + 3600.0
    if tf == "1h":
        return (int(now_ts // 3600) + 1) * 3600.0
    if tf == "daily":
        utc_now = datetime.now(timezone.utc)
        et_h = _et_offset_hours()
        et_now = utc_now + timedelta(hours=et_h)
        # Next midnight ET
        next_midnight_et = et_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        # Back to UTC
        next_midnight_utc = next_midnight_et - timedelta(hours=et_h)
        return next_midnight_utc.timestamp()
    return now_ts + 3600.0


def _finalize_market(state: State):
    """Record the outcome of the closing market into history."""
    if not state.current_slug or not state._cur_prices:
        return

    p = state._cur_prices
    max_up = p.get("max_up", 0.0) or 0.0
    max_dn = p.get("max_dn", 0.0) or 0.0

    # Detect outcome: whichever side reached above 0.85 wins
    if max_up >= 0.85 and max_up > max_dn:
        outcome = "UP"
    elif max_dn >= 0.85 and max_dn > max_up:
        outcome = "DOWN"
    else:
        outcome = None  # Market closed without a clear price signal

    record = {
        "slug":      state.current_slug,
        "min_up":    p.get("min_up"),
        "max_up":    max_up if max_up else None,
        "min_dn":    p.get("min_dn"),
        "max_dn":    max_dn if max_dn else None,
        "outcome":   outcome,
        "timestamp": time.time(),
    }
    state.market_history.append(record)
    if len(state.market_history) > 500:
        state.market_history = state.market_history[-500:]

    print(f"  [Market] finalized {state.current_slug} → outcome={outcome} "
          f"min_up={p.get('min_up')} min_dn={p.get('min_dn')}")


async def market_monitor(state: State):
    """
    Watches for market expiry and automatically refreshes Polymarket tokens
    when the current market closes and a new one opens.
    """
    # Wait until state has coin/tf set
    while not state.coin or not state.tf:
        await asyncio.sleep(1)

    while True:
        expiry = _next_market_expiry(state.tf)
        wait   = expiry - time.time()

        if wait > 0:
            print(f"  [Market] expires in {wait:.0f}s  (slug={state.current_slug})")
            await asyncio.sleep(wait)

        # Give Polymarket a few seconds to open the new market
        await asyncio.sleep(12)

        # Finalize the old market before switching
        _finalize_market(state)

        # Try to fetch new market tokens (retry up to 6 × 10s = 60s)
        for attempt in range(6):
            up_id, dn_id = fetch_pm_tokens(state.coin, state.tf)
            if up_id:
                new_slug = _build_slug(state.coin, state.tf)
                state.pm_up_id         = up_id
                state.pm_dn_id         = dn_id
                state.current_slug     = new_slug
                state.market_expires_at = _next_market_expiry(state.tf)
                state._cur_prices      = {}
                state.pm_up            = None
                state.pm_dn            = None
                state.market_changed.set()
                print(f"  [Market] switched to {new_slug}")
                break
            print(f"  [Market] new market not available yet (attempt {attempt + 1}/6), retrying in 10s…")
            await asyncio.sleep(10)
        else:
            print("  [Market] could not find new market after 6 attempts – will retry next cycle")


def fetch_pm_event_data(coin: str, tf: str) -> dict | None:
    """Fetch full event data from Polymarket API."""
    slug = _build_slug(coin, tf)
    if slug is None:
        return None
    try:
        data = requests.get(config.PM_GAMMA, params={"slug": slug, "limit": 1}, timeout=5).json()
        if not data or data[0].get("ticker") != slug:
            print(f"  [PM] no active market for slug: {slug}")
            return None
        return data[0]
    except Exception as e:
        print(f"  [PM] event fetch failed ({slug}): {e}")
        return None


def fetch_pm_tokens(coin: str, tf: str) -> tuple:
    """Fetch PM token IDs for up/down markets."""
    event_data = fetch_pm_event_data(coin, tf)
    if event_data is None:
        return None, None
    try:
        ids = json.loads(event_data["markets"][0]["clobTokenIds"])
        return ids[0], ids[1]
    except Exception as e:
        print(f"  [PM] token extraction failed: {e}")
        return None, None


async def pm_feed(state: State):
    if not state.pm_up_id:
        print("  [PM] no tokens for this coin/timeframe – skipped")
        return

    while True:
        if not state.pm_up_id:
            await asyncio.sleep(5)
            continue

        assets = [state.pm_up_id, state.pm_dn_id]
        state.market_changed.clear()

        try:
            async with websockets.connect(
                config.PM_WS,
                ping_interval=20,
                ping_timeout=60,
                close_timeout=10
            ) as ws:
                await ws.send(json.dumps({"assets_ids": assets, "type": "market"}))
                print(f"  [PM] connected  (up={state.pm_up_id[:16]}…)")

                while True:
                    # Market rolled over – reconnect with new token IDs
                    if state.market_changed.is_set():
                        print("  [PM] market changed – reconnecting…")
                        break

                    try:
                        raw = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))

                        if isinstance(raw, list):
                            for entry in raw:
                                _pm_apply(entry.get("asset_id"), entry.get("asks", []), state)

                        elif isinstance(raw, dict) and raw.get("event_type") == "price_change":
                            for ch in raw.get("price_changes", []):
                                if ch.get("best_ask"):
                                    _pm_set(ch["asset_id"], float(ch["best_ask"]), state)

                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        print("  [PM] connection closed, reconnecting...")
                        break

        except Exception as e:
            print(f"  [PM] connection error: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)


def _pm_apply(asset, asks, state):
    if asks:
        _pm_set(asset, min(float(a["price"]) for a in asks), state)


def _pm_set(asset, price, state):
    if asset == state.pm_up_id:
        state.pm_up = price
        cp = state._cur_prices
        if "min_up" not in cp or price < cp["min_up"]:
            cp["min_up"] = price
        if "max_up" not in cp or price > cp["max_up"]:
            cp["max_up"] = price
    elif asset == state.pm_dn_id:
        state.pm_dn = price
        cp = state._cur_prices
        if "min_dn" not in cp or price < cp["min_dn"]:
            cp["min_dn"] = price
        if "max_dn" not in cp or price > cp["max_dn"]:
            cp["max_dn"] = price
