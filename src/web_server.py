"""
FastAPI web server for the Polymarket Crypto Assistant.

Serves the web UI and broadcasts live dashboard data over WebSocket.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import feeds
import indicators as ind
import simulator as sim

# ── Path helpers ────────────────────────────────────────────────────────────
_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "static")

app = FastAPI(title="Polymarket Crypto Assistant")
app.mount("/static", StaticFiles(directory=_STATIC), name="static")

# ── Application manager ─────────────────────────────────────────────────────

class AppManager:
    def __init__(self):
        self.state: feeds.State | None  = None
        self.coin:  str                 = "BTC"
        self.tf:    str                 = "5m"
        self._tasks: list[asyncio.Task] = []
        self._clients: set[WebSocket]   = set()

    async def initialize(self, coin: str, tf: str):
        # Cancel any running feed tasks
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        self.coin = coin
        self.tf   = tf

        state       = feeds.State()
        state.coin  = coin
        state.tf    = tf
        self.state  = state

        # Fetch PM tokens
        state.pm_up_id, state.pm_dn_id = feeds.fetch_pm_tokens(coin, tf)
        state.current_slug              = feeds._build_slug(coin, tf) or ""
        state.market_expires_at         = feeds._next_market_expiry(tf)

        # Bootstrap historical candles
        binance_sym = config.COIN_BINANCE[coin]
        kline_iv    = config.TF_KLINE[tf]
        await feeds.bootstrap(binance_sym, kline_iv, state)

        # Launch background feed tasks
        self._tasks = [
            asyncio.create_task(feeds.ob_poller(binance_sym, state)),
            asyncio.create_task(feeds.binance_feed(binance_sym, kline_iv, state)),
            asyncio.create_task(feeds.pm_feed(state)),
            asyncio.create_task(feeds.market_monitor(state)),
        ]
        print(f"  [App] initialized {coin} {tf}  slug={state.current_slug}")

    async def broadcast(self, payload: dict):
        if not self._clients:
            return
        msg          = json.dumps(payload)
        disconnected = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(msg)
            except Exception:
                disconnected.add(ws)
        self._clients -= disconnected

    def dashboard_data(self) -> dict:
        st = self.state
        if st is None or st.mid == 0:
            return {"type": "update", "ready": False}

        # ── Indicators ───────────────────────────────────────────────────
        obi_v          = ind.obi(st.bids, st.asks, st.mid)
        bw, aw         = ind.walls(st.bids, st.asks)
        dep            = ind.depth_usd(st.bids, st.asks, st.mid)
        cvds           = {s: ind.cvd(st.trades, s) for s in config.CVD_WINDOWS}
        delta_v        = ind.cvd(st.trades, config.DELTA_WINDOW)
        poc, vp        = ind.vol_profile(st.klines)
        rsi_v          = ind.rsi(st.klines)
        macd_v, sig_v, hv = ind.macd(st.klines)
        vwap_v         = ind.vwap(st.klines)
        ema_s, ema_l   = ind.emas(st.klines)
        ha             = ind.heikin_ashi(st.klines)

        # ── Trend score ──────────────────────────────────────────────────
        score = 0
        if obi_v > config.OBI_THRESH:    score += 1
        elif obi_v < -config.OBI_THRESH: score -= 1
        cvd5 = cvds.get(300, 0)
        score += 1 if cvd5 > 0 else (-1 if cvd5 < 0 else 0)
        if rsi_v is not None:
            if rsi_v > config.RSI_OB:  score -= 1
            elif rsi_v < config.RSI_OS: score += 1
        if hv is not None:
            score += 1 if hv > 0 else -1
        if vwap_v and st.mid:
            score += 1 if st.mid > vwap_v else -1
        if ema_s is not None and ema_l is not None:
            score += 1 if ema_s > ema_l else -1
        score += min(len(bw), 2)
        score -= min(len(aw), 2)
        if len(ha) >= 3:
            last3 = ha[-3:]
            if all(c["green"] for c in last3):   score += 1
            elif all(not c["green"] for c in last3): score -= 1

        if score >= 3:   trend_label, trend_color = "BULLISH", "green"
        elif score <= -3: trend_label, trend_color = "BEARISH", "red"
        else:             trend_label, trend_color = "NEUTRAL",  "yellow"

        # ── OBI signal label ─────────────────────────────────────────────
        if obi_v > config.OBI_THRESH:    obi_signal = "BULLISH"
        elif obi_v < -config.OBI_THRESH: obi_signal = "BEARISH"
        else:                            obi_signal = "NEUTRAL"

        # ── Signals list ─────────────────────────────────────────────────
        signals = []
        if abs(obi_v) > config.OBI_THRESH:
            d = "BULLISH" if obi_v > 0 else "BEARISH"
            signals.append({"name": "OBI", "text": f"{d} ({obi_v*100:+.1f}%)",
                            "color": "green" if obi_v > 0 else "red"})
        if cvd5 != 0:
            d = "buy pressure" if cvd5 > 0 else "sell pressure"
            signals.append({"name": "CVD 5m", "text": d,
                            "color": "green" if cvd5 > 0 else "red"})
        if rsi_v is not None:
            if rsi_v > config.RSI_OB:
                signals.append({"name": "RSI", "text": f"overbought ({rsi_v:.0f})", "color": "red"})
            elif rsi_v < config.RSI_OS:
                signals.append({"name": "RSI", "text": f"oversold ({rsi_v:.0f})", "color": "green"})
        if hv is not None:
            d = "bullish" if hv > 0 else "bearish"
            signals.append({"name": "MACD", "text": f"hist {d}",
                            "color": "green" if hv > 0 else "red"})
        if vwap_v and st.mid:
            d = "above" if st.mid > vwap_v else "below"
            signals.append({"name": "VWAP", "text": f"price {d}",
                            "color": "green" if st.mid > vwap_v else "red"})
        if ema_s is not None and ema_l is not None:
            d = "golden cross" if ema_s > ema_l else "death cross"
            signals.append({"name": "EMA", "text": d,
                            "color": "green" if ema_s > ema_l else "red"})
        for p, q in bw[:2]:
            signals.append({"name": "BUY wall", "text": f"@ ${p:,.2f}", "color": "green"})
        for p, q in aw[:2]:
            signals.append({"name": "SELL wall", "text": f"@ ${p:,.2f}", "color": "red"})
        if len(ha) >= 3:
            last3 = ha[-3:]
            if all(c["green"] for c in last3):
                signals.append({"name": "Heikin Ashi", "text": "3+ green candles ↑", "color": "green"})
            elif all(not c["green"] for c in last3):
                signals.append({"name": "Heikin Ashi", "text": "3+ red candles ↓", "color": "red"})

        # ── Volume profile (trimmed window around POC) ────────────────────
        vp_display = []
        if vp:
            max_v = max(v for _, v in vp) or 1
            poc_i = min(range(len(vp)), key=lambda i: abs(vp[i][0] - poc))
            half  = config.VP_SHOW // 2
            start = max(0, poc_i - half)
            end   = min(len(vp), start + config.VP_SHOW)
            start = max(0, end - config.VP_SHOW)
            for i in range(end - 1, start - 1, -1):
                p_price, p_vol = vp[i]
                vp_display.append({
                    "price":  round(p_price, 2),
                    "volume": round(p_vol, 4),
                    "pct":    round(p_vol / max_v * 100, 1),
                    "is_poc": i == poc_i,
                })

        # ── RSI signal label ──────────────────────────────────────────────
        rsi_signal = None
        if rsi_v is not None:
            if rsi_v > config.RSI_OB:   rsi_signal = "OVERBOUGHT"
            elif rsi_v < config.RSI_OS: rsi_signal = "OVERSOLD"

        now           = time.time()
        time_remaining = max(0, int(st.market_expires_at - now)) if st.market_expires_at else 0

        return {
            "type":  "update",
            "ready": True,
            "ts":    now,
            "header": {
                "coin":             self.coin,
                "tf":               self.tf,
                "price":            round(st.mid, 2),
                "pm_up":            round(st.pm_up, 4) if st.pm_up is not None else None,
                "pm_dn":            round(st.pm_dn, 4) if st.pm_dn is not None else None,
                "trend_score":      score,
                "trend_label":      trend_label,
                "trend_color":      trend_color,
                "market_slug":      st.current_slug,
                "time_remaining_s": time_remaining,
                "market_expires_at": st.market_expires_at,
            },
            "orderbook": {
                "obi_pct":    round(obi_v * 100, 2),
                "obi_signal": obi_signal,
                "depth":      {str(k): int(v) for k, v in dep.items()},
                "buy_walls":  [{"price": round(p, 2), "qty": round(q, 4)} for p, q in bw[:3]],
                "sell_walls": [{"price": round(p, 2), "qty": round(q, 4)} for p, q in aw[:3]],
            },
            "flow": {
                "cvd_60":   round(cvds.get(60, 0),  2),
                "cvd_180":  round(cvds.get(180, 0), 2),
                "cvd_300":  round(cvds.get(300, 0), 2),
                "delta_60": round(delta_v, 2),
                "poc":      round(poc, 2) if poc else None,
                "volume_profile": vp_display,
            },
            "ta": {
                "rsi":         round(rsi_v, 2)   if rsi_v  is not None else None,
                "rsi_signal":  rsi_signal,
                "macd_line":   round(macd_v, 6)  if macd_v is not None else None,
                "macd_signal": round(sig_v, 6)   if sig_v  is not None else None,
                "macd_hist":   round(hv, 6)      if hv     is not None else None,
                "vwap":        round(vwap_v, 2)  if vwap_v else None,
                "ema5":        round(ema_s, 2)   if ema_s  is not None else None,
                "ema20":       round(ema_l, 2)   if ema_l  is not None else None,
                "ha_candles":  [{"green": c["green"]} for c in ha[-config.HA_COUNT:]] if ha else [],
            },
            "signals": signals,
            "trend": {
                "score": score,
                "label": trend_label,
                "color": trend_color,
            },
            "market_history": st.market_history[-50:],
            "coins":          config.COINS,
            "coin_timeframes": config.COIN_TIMEFRAMES,
        }


mgr = AppManager()


# ── Startup ──────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    await mgr.initialize("BTC", "5m")
    asyncio.create_task(_broadcast_loop())


async def _broadcast_loop():
    while True:
        try:
            if mgr.state and mgr.state.mid > 0:
                payload = mgr.dashboard_data()
                await mgr.broadcast(payload)
        except Exception as e:
            print(f"  [broadcast] error: {e}")
        tf = mgr.tf
        interval = config.REFRESH_5M if tf == "5m" else config.REFRESH
        await asyncio.sleep(interval)


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse(os.path.join(_STATIC, "index.html"))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    mgr._clients.add(ws)
    try:
        # Push current state immediately on connect
        if mgr.state and mgr.state.mid > 0:
            await ws.send_text(json.dumps(mgr.dashboard_data()))
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # Send a keepalive ping
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        mgr._clients.discard(ws)


class ConfigRequest(BaseModel):
    coin: str
    tf:   str


@app.post("/api/configure")
async def configure(req: ConfigRequest):
    coin = req.coin.upper()
    tf   = req.tf
    if coin not in config.COINS:
        return JSONResponse({"error": f"unknown coin {coin}"}, status_code=400)
    if tf not in config.COIN_TIMEFRAMES.get(coin, []):
        return JSONResponse({"error": f"timeframe {tf} not available for {coin}"}, status_code=400)
    await mgr.initialize(coin, tf)
    # Send updated state to all clients
    await mgr.broadcast({"type": "reconfigure", "coin": coin, "tf": tf})
    return {"status": "ok", "coin": coin, "tf": tf}


class SimulateRequest(BaseModel):
    up_entry: float = 0.33
    dn_entry: float = 0.33


@app.post("/api/simulate")
async def simulate(req: SimulateRequest):
    if mgr.state is None:
        return JSONResponse({"error": "not initialized"}, status_code=503)
    result = sim.run_simulation(mgr.state.market_history, req.up_entry, req.dn_entry)
    return result


@app.get("/api/status")
async def status():
    if mgr.state is None:
        return {"ready": False}
    st = mgr.state
    return {
        "ready":          st.mid > 0,
        "coin":           mgr.coin,
        "tf":             mgr.tf,
        "price":          round(st.mid, 2),
        "slug":           st.current_slug,
        "markets_logged": len(st.market_history),
        "clients":        len(mgr._clients),
    }
