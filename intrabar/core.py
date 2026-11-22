"""Intrabar paper trading core (separate from Sonic; paper only, never sends orders).

The owner's fast style: buy when a candle starts rising (B1) or breaks its high with buying
pressure (B2), exit with a small profit before the SAME candle closes. Several variants run
side by side, each as its own paper account, so live results show which one works.

Definitions are the ones frozen in the research (part_b.py), applied to live trades:
- 1-second bars from trades; a decision happens at the END of a second that had trades,
  using only that second and earlier ones;
- B1: rise since the candle's first trade >= threshold, and price above 10 s ago;
- B2: price above every high of the candle before this second, and the aggressive-buy share
  of the last 30 s >= threshold;
- decide only with >= 1/3 of the candle left; "session_opens" variants only in the first
  hour after 00:00, 08:00 and 13:30 UTC;
- limit entry at the decision price, filled only when a later trade prints BELOW it within
  the wait time; target is a limit (filled only when a trade prints ABOVE it); stop and the
  candle-close exit are market orders;
- per variant: one position at a time, at most 2 trades per coin per UTC day, and the
  owner's day rules (2 wins -> stop; win+loss -> go on until the day is positive; 2 losses ->
  stop).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

FEE_MAKER = 0.0002
FEE_TAKER = 0.0005
SLIP = 0.0001
MIN_REMAINING = 1 / 3
LIMIT_WAIT_S = {60: 60, 300: 120, 900: 300}
SESSION_OPENS_S = (0, 8 * 3600, 13 * 3600 + 1800)
SESSION_LEN_S = 3600
MAX_PER_COIN_DAY = 2


@dataclass(frozen=True)
class Variant:
    signal: str          # "B1" or "B2"
    tf: int              # candle length in seconds: 60, 300, 900
    q: float             # top-q tail used for the threshold
    target: float        # target = stop, as a fraction (0.001 = 0.1 %)
    window: str          # "all_day" or "session_opens"

    @property
    def name(self) -> str:
        return f"{self.signal}-{self.tf // 60}m-q{int(self.q * 100)}-t{self.target * 100:g}-{self.window}"


@dataclass
class Second:
    sec: int
    o: float
    h: float
    low: float
    c: float
    buy: float = 0.0
    sell: float = 0.0


class SymbolBars:
    """Rolling 1-second bars of one symbol and the per-timeframe candle state."""

    def __init__(self, timeframes: tuple[int, ...]) -> None:
        self.cur: Second | None = None
        self.recent: deque[Second] = deque(maxlen=64)   # closed seconds, newest last
        self.timeframes = timeframes
        self.candle: dict[int, dict] = {}                # tf -> {start, open, high_before}

    def add_trade(self, sec: int, price: float, qty: float, buyer_is_maker: bool) -> Second | None:
        """Returns the second that just CLOSED (decision time), or None."""
        closed = None
        if self.cur is not None and sec != self.cur.sec:
            closed = self.cur
            self._close(closed)
        if self.cur is None or sec != self.cur.sec:
            self.cur = Second(sec, price, price, price, price)
        s = self.cur
        s.h, s.low, s.c = max(s.h, price), min(s.low, price), price
        if buyer_is_maker:
            s.sell += qty
        else:
            s.buy += qty
        return closed

    def _close(self, s: Second) -> None:
        for tf in self.timeframes:
            start = s.sec - s.sec % tf
            st = self.candle.get(tf)
            if st is None or st["start"] != start:
                st = {"start": start, "open": s.o, "high_before": None, "high": s.h}
                self.candle[tf] = st
            else:
                st["high_before"] = st["high"]
                st["high"] = max(st["high"], s.h)
        self.recent.append(s)

    def vol(self, sec: int, span: int = 60) -> float:
        """(highest high - lowest low) / last close over the closed seconds in (sec-span, sec]."""
        hi = lo = last = None
        for b in reversed(self.recent):
            if b.sec > sec:
                continue
            if b.sec <= sec - span:
                break
            if last is None:
                last = b.c
            hi = b.h if hi is None else max(hi, b.h)
            lo = b.low if lo is None else min(lo, b.low)
        return (hi - lo) / last if last else 0.0

    def features(self, s: Second, tf: int) -> dict | None:
        """Features at the END of second s (already closed), from s and earlier only."""
        st = self.candle.get(tf)
        if st is None or st["start"] != s.sec - s.sec % tf:
            return None
        ten = None
        buy30 = sell30 = 0.0
        for b in reversed(self.recent):
            if b.sec > s.sec:
                continue
            if b.sec > s.sec - 30:
                buy30 += b.buy
                sell30 += b.sell
            if ten is None and b.sec <= s.sec - 10:
                ten = b.c
        total = buy30 + sell30
        remaining = (st["start"] + tf - s.sec - 1) / tf
        sod = s.sec % 86400
        return {
            "rise": s.c / st["open"] - 1.0,
            "up10": ten is not None and s.c > ten,
            "breakout": st["high_before"] is not None and s.c > st["high_before"],
            "buyshare30": buy30 / total if total > 0 else None,
            "decidable": remaining >= MIN_REMAINING,
            "in_session": any(start <= sod < start + SESSION_LEN_S for start in SESSION_OPENS_S),
            "candle_end": st["start"] + tf,
        }


def fires(v: Variant, f: dict, thr: dict) -> bool:
    if not f["decidable"]:
        return False
    if v.window == "session_opens" and not f["in_session"]:
        return False
    if v.signal == "B1":
        return f["rise"] >= thr["rise"] and f["up10"]
    return f["breakout"] and f["buyshare30"] is not None and f["buyshare30"] >= thr["buyshare30"]


@dataclass
class Account:
    """One paper account per variant."""

    variant: Variant
    pending: dict | None = None
    position: dict | None = None
    days: dict = field(default_factory=dict)
    closed: deque = field(default_factory=lambda: deque(maxlen=50))  # recent only: bounded memory

    def day(self, sec: int) -> dict:
        key = sec // 86400
        if key not in self.days:  # keep today and yesterday only: bounded memory
            for old in [k for k in self.days if k < key - 1]:
                del self.days[old]
        return self.days.setdefault(sec // 86400, {"wins": 0, "losses": 0, "pnl": 0.0,
                                                    "stopped": False, "per_coin": {}})

    def can_open(self, symbol: str, sec: int) -> bool:
        if self.pending or self.position:
            return False
        d = self.day(sec)
        return not d["stopped"] and d["per_coin"].get(symbol, 0) < MAX_PER_COIN_DAY

    def place(self, symbol: str, s: Second, candle_end: int, extra: dict | None = None) -> None:
        """extra: what the bot saw at the decision (carried to the closed record, unchanged)."""
        self.pending = {"symbol": symbol, "price": s.c, "decided": s.sec,
                        "expires": min(s.sec + LIMIT_WAIT_S[self.variant.tf], candle_end),
                        "candle_end": candle_end, "extra": extra if extra is not None else {}}

    def on_trade(self, symbol: str, sec: int, price: float, prev_price: float) -> dict | None:
        """Process one live trade for this account; returns a closed trade record or None."""
        p = self.pending
        if p and p["symbol"] == symbol:
            if sec > p["expires"] or sec >= p["candle_end"]:
                self.pending = None
            elif sec > p["decided"] and price < p["price"]:
                entry = p["price"]
                x = self.variant.target
                self.position = {"symbol": symbol, "entry": entry, "decided": p["decided"],
                                 "opened": sec, "target": entry * (1 + x), "stop": entry * (1 - x),
                                 "candle_end": p["candle_end"], "extra": p.get("extra", {})}
                self.pending = None
                # the filling trade itself may already be through the stop (conservative)
                return self._check_exit(symbol, sec, price, prev_price)
        if self.position and self.position["symbol"] == symbol:
            return self._check_exit(symbol, sec, price, prev_price)
        return None

    def _check_exit(self, symbol: str, sec: int, price: float, prev_price: float) -> dict | None:
        pos = self.position
        if sec >= pos["candle_end"]:   # candle closed: exit at the last price before it
            return self._close(prev_price * (1 - SLIP), FEE_TAKER, "candle_close", sec)
        if price <= pos["stop"]:
            return self._close(price * (1 - SLIP), FEE_TAKER, "stop", sec)
        if price > pos["target"]:
            return self._close(pos["target"], FEE_MAKER, "target", sec)
        return None

    def _close(self, exit_price: float, exit_fee: float, kind: str, sec: int) -> dict:
        pos = self.position
        gross = exit_price / pos["entry"] - 1
        fees = FEE_MAKER + exit_fee
        rec = {"variant": self.variant.name, "symbol": pos["symbol"], "decided": pos["decided"],
               "opened": pos["opened"], "closed": sec, "entry": pos["entry"], "exit": exit_price,
               "kind": kind, "gross": gross, "fees": fees, "net": gross - fees,
               "extra": pos.get("extra", {})}
        self.position = None
        self.count_day(pos["symbol"], pos["decided"], rec["net"])
        self.closed.append(rec)
        return rec

    def count_day(self, symbol: str, decided: int, net: float) -> None:
        """The owner's day rules; also used to rebuild today's state after a restart."""
        d = self.day(decided)
        d["per_coin"][symbol] = d["per_coin"].get(symbol, 0) + 1
        d["pnl"] += net
        if net > 0:
            d["wins"] += 1
        else:
            d["losses"] += 1
        if (d["wins"] >= 2 and d["losses"] == 0) or d["losses"] >= 2 or \
                (d["wins"] >= 1 and d["losses"] >= 1 and d["pnl"] > 0):
            d["stopped"] = True


class Engine:
    """Feeds live trades of several symbols to all variants."""

    def __init__(self, variants: list[Variant], symbols: list[str], thresholds: dict) -> None:
        self.variants = variants
        self.accounts = {v.name: Account(v) for v in variants}
        tfs = tuple(sorted({v.tf for v in variants}))
        self.bars = {s: SymbolBars(tfs) for s in symbols}
        self.thresholds = thresholds          # {symbol: {tf: {q: {"rise":..,"buyshare30":..}}}}
        self.last_price: dict[str, float] = {}

    def on_trade(self, symbol: str, time_ms: int, price: float, qty: float,
                 buyer_is_maker: bool) -> list[dict]:
        sec = time_ms // 1000
        prev = self.last_price.get(symbol, price)
        out = []
        # 1) existing orders/positions react to this trade first (it happened after decisions)
        for acc in self.accounts.values():
            rec = acc.on_trade(symbol, sec, price, prev)
            if rec:
                out.append(rec)
        # 2) a second may have closed: decisions at its end
        closed = self.bars[symbol].add_trade(sec, price, qty, buyer_is_maker)
        if closed is not None:
            feats = {}
            for acc in self.accounts.values():
                v = acc.variant
                if not acc.can_open(symbol, closed.sec):
                    continue
                f = feats.get(v.tf)
                if f is None:
                    f = feats[v.tf] = self.bars[symbol].features(closed, v.tf)
                thr = self.thresholds.get(symbol, {}).get(v.tf, {}).get(v.q)
                if f is None or thr is None:
                    continue
                if fires(v, f, thr):
                    seen = {k: f[k] for k in ("rise", "up10", "breakout", "buyshare30",
                                              "in_session")}
                    seen.update(tf=v.tf, hour=(closed.sec % 86400) // 3600,
                                vol60=self.bars[symbol].vol(closed.sec, 60))
                    acc.place(symbol, closed, f["candle_end"], {"features": seen})
        self.last_price[symbol] = price
        return out
