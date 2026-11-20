"""Read-only diagnosis of Sonic: signal funnel per 4h bar and trade anatomy.

Uses the deployed strategy functions and the same data as the performance test (project cache
+ gap months / November from Binance's public API, in memory). Writes only to argv[1].
"""

from __future__ import annotations

import bisect
import calendar
import csv
import io
import json
import statistics as st
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(r"C:/Users/sa/Projects/sonic_bot")
sys.path.insert(0, str(ROOT / "research"))
sys.path.insert(0, str(ROOT / "server"))

from sonic import risk, strategy  # noqa: E402
from sonic_research import data as data_mod  # noqa: E402

OUT = Path(sys.argv[1])
TRADES = json.loads(Path(sys.argv[2]).read_text())["trades"]
CUTOFF = 1795128633539
FILL = {"2022-02", "2022-04", "2026-10", "2026-11"}
H, B4 = 3_600_000, 14_400_000
orig = data_mod._fetch


def api_month(symbol: str, interval: str, month: str) -> bytes | None:
    y, m = map(int, month.split("-"))
    a = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
    b = int(datetime(y, m, calendar.monthrange(y, m)[1], 23, 59, tzinfo=UTC).timestamp() * 1000)
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}"
           f"&startTime={a}&endTime={b}&limit=1500")
    rows = [r for r in json.loads(urllib.request.urlopen(url, timeout=30).read())
            if int(r[6]) < CUTOFF]
    if not rows:
        return None
    text = io.StringIO()
    csv.writer(text).writerows(rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", text.getvalue())
    return buf.getvalue()


def fetch(url: str, path: Path) -> bytes | None:
    month = path.name[-11:-4]
    if month in FILL and "/klines/" in url:
        parts = path.name.split("-")
        return api_month(parts[0], parts[1], month)
    return orig(url, path)


data_mod._fetch = fetch
months = data_mod.month_range("2020-11", "2026-11")
SYMS = list(risk.SMALL_ACCOUNT_SYMBOLS)


def bars_of(symbol: str, interval: str) -> list[strategy.Bar]:
    raw = data_mod.load_bars(symbol, months, interval)
    return [strategy.Bar(int(t), o, h, low, c) for t, o, h, low, c in
            zip(raw.t, raw.o, raw.h, raw.low, raw.c, strict=True)]


def fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


b4 = {s: bars_of(s, "4h") for s in ["BTCUSDT", *SYMS]}
b1 = {s: bars_of(s, "1h") for s in SYMS}
t4 = {s: [b.t for b in v] for s, v in b4.items()}
t1 = {s: [b.t for b in v] for s, v in b1.items()}

START = int(datetime(2021, 6, 1, tzinfo=UTC).timestamp() * 1000)
btc = b4["BTCUSDT"]
btc_c = [b.c for b in btc]
btc_ema = strategy.ema(btc_c, strategy.REGIME_PERIOD)

# ---------- 1. funnel: every closed 4h bar, every coin
funnel = Counter()
by_year = defaultdict(Counter)
btc_up_bars = Counter()
signals = []
for i, bar in enumerate(btc):
    if bar.t < START or i < 1000:
        continue
    close_ms = bar.t + B4
    btc_hist = btc[max(0, i - 999): i + 1]
    btc_up = strategy.btc_uptrend(btc_hist)
    year = fmt(bar.t)[:4]
    btc_up_bars[(year, btc_up)] += 1
    for s in SYMS:
        j = bisect.bisect_right(t4[s], bar.t) - 1
        if j < 0 or t4[s][j] != bar.t:
            funnel["no_data"] += 1
            by_year[year]["no_data"] += 1
            continue
        hist = b4[s][max(0, j - 999): j + 1]
        reason = strategy.entry_reason(hist, btc_up)
        funnel[reason] += 1
        by_year[year][reason] += 1
        if reason == "buy":
            closes = [b.c for b in hist]
            signals.append({"symbol": s, "bar_ms": bar.t, "close_ms": close_ms,
                            "btc_gap": btc_c[i] / btc_ema[i] - 1,
                            "coin_gap200": closes[-1] / strategy.ema(closes, 200)[-1] - 1,
                            "stop_pct": strategy.stop_distance(hist) / closes[-1]})

# match signals to trades (a trade opens at the first tick after the close)
opened = {(t["symbol"], t["opened_ms"]) for t in TRADES}
taken = []
for sig in signals:
    hit = [t for t in TRADES if t["symbol"] == sig["symbol"]
           and sig["close_ms"] <= t["opened_ms"] < sig["close_ms"] + H]
    sig["taken"] = bool(hit)
    if not hit:
        busy = [t for t in TRADES if t["symbol"] == sig["symbol"]
                and t["opened_ms"] < sig["close_ms"] <= t["closed_ms"]]
        n_open = sum(1 for t in TRADES if t["opened_ms"] < sig["close_ms"] <= t["closed_ms"])
        sig["why_not"] = ("same_coin_open" if busy else "max_open" if n_open >= 3
                          else "size_or_budget")
    taken.append(sig)
why_not = Counter(s.get("why_not") for s in taken if not s["taken"])

# ---------- 2. trade anatomy
def first_stop_dist(t: dict) -> float | None:
    if not t["r"]:
        return None
    risk_usdt = t["pnl"] / t["r"]
    return risk_usdt / t["qty"] if t["qty"] else None


rows = []
for t in TRADES:
    s = t["symbol"]
    dist = first_stop_dist(t)
    i0 = bisect.bisect_left(t1[s], t["opened_ms"])
    i1 = bisect.bisect_right(t1[s], t["closed_ms"])
    i2 = bisect.bisect_right(t1[s], t["closed_ms"] + 20 * 86_400_000)
    during = b1[s][i0:i1] or b1[s][i0:i0 + 1]
    after = b1[s][i1:i2]
    entry = t["entry_price"]
    mfe = (max(b.h for b in during) - entry) / dist if dist else None
    mae = (entry - min(b.low for b in during)) / dist if dist else None
    after_max = (max(b.h for b in after) - entry) / dist if dist and after else None
    j = bisect.bisect_right(t4["BTCUSDT"], t["opened_ms"] - B4) - 1
    btc_gap = btc_c[j] / btc_ema[j] - 1 if j >= 0 else None
    gross = t["pnl"] + t["fees"]
    rows.append({**t, "dist": dist, "stop_pct": dist / entry if dist else None,
                 "mfe_r": mfe, "mae_r": mae, "after20d_max_r": after_max, "btc_gap": btc_gap,
                 "gross": gross, "hold_days": (t["closed_ms"] - t["opened_ms"]) / 86_400_000,
                 "year": fmt(t["opened_ms"])[:4]})


def summ(group: list[dict]) -> dict:
    if not group:
        return {"n": 0}
    rs = [g["r"] for g in group if g["r"] is not None]
    wins = [g for g in group if g["pnl"] > 0]
    losses = [g for g in group if g["pnl"] <= 0]
    gw = sum(g["pnl"] for g in wins)
    gl = -sum(g["pnl"] for g in losses)
    return {"n": len(group), "win": round(len(wins) / len(group), 3),
            "pnl": round(sum(g["pnl"] for g in group), 2),
            "avg_r": round(st.mean(rs), 3) if rs else None,
            "avg_win_r": round(st.mean([g["r"] for g in wins]), 3) if wins else None,
            "avg_loss_r": round(st.mean([g["r"] for g in losses]), 3) if losses else None,
            "pf": round(gw / gl, 2) if gl else None,
            "fees": round(sum(g["fees"] for g in group), 2)}


def bucket(v: float | None, edges: list[float]) -> str:
    if v is None:
        return "na"
    for e in edges:
        if v < e:
            return f"<{e:g}"
    return f">={edges[-1]:g}"


out = {
    "funnel_coin_bars": dict(funnel), "funnel_by_year": {k: dict(v) for k, v in by_year.items()},
    "btc_up_bars": {f"{y}-{'up' if u else 'down'}": n for (y, u), n in sorted(btc_up_bars.items())},
    "buy_signals": len(signals), "signals_taken": sum(s["taken"] for s in taken),
    "signals_not_taken": dict(why_not),
    "trades": len(TRADES),
    "by_exit": {k: summ([r for r in rows if r["exit_reason"] == k])
                for k in sorted({r["exit_reason"] for r in rows})},
    "by_scaled": {str(k): summ([r for r in rows if r["scaled"] == k]) for k in (0, 1)},
    "by_coin": {s: summ([r for r in rows if r["symbol"] == s]) for s in SYMS},
    "by_year": {y: summ([r for r in rows if r["year"] == y]) for y in sorted({r["year"] for r in rows})},
    "by_btc_gap": {b: summ([r for r in rows if bucket(r["btc_gap"], [0.03, 0.08, 0.15, 0.25]) == b])
                   for b in ["<0.03", "<0.08", "<0.15", "<0.25", ">=0.25"]},
    "by_stop_pct": {b: summ([r for r in rows if bucket(r["stop_pct"], [0.04, 0.06, 0.08, 0.11]) == b])
                    for b in ["<0.04", "<0.06", "<0.08", "<0.11", ">=0.11"]},
    "totals": {"gross_before_fees": round(sum(r["gross"] for r in rows), 2),
               "fees": round(sum(r["fees"] for r in rows), 2),
               "net": round(sum(r["pnl"] for r in rows), 2)},
    "r_distribution": dict(Counter(bucket(r["r"], [-0.9, -0.3, 0.0, 0.05, 0.12, 0.3, 1, 2, 4])
                                   for r in rows)),
    "mfe": {
        "median_mfe_r_all": round(st.median([r["mfe_r"] for r in rows if r["mfe_r"] is not None]), 2),
        "breakeven_trades_median_mfe_r": round(st.median(
            [r["mfe_r"] for r in rows if r["exit_reason"] == "breakeven" and r["mfe_r"] is not None]), 2),
        "breakeven_trades_mfe_ge_1r": sum(1 for r in rows if r["exit_reason"] == "breakeven"
                                          and (r["mfe_r"] or 0) >= 1),
        "breakeven_trades_mfe_ge_0_5r": sum(1 for r in rows if r["exit_reason"] == "breakeven"
                                            and (r["mfe_r"] or 0) >= 0.5),
        "breakeven_trades_after20d_ge_1r": sum(1 for r in rows if r["exit_reason"] == "breakeven"
                                               and (r["after20d_max_r"] or 0) >= 1),
        "stop_loss_trades_mfe_ge_0_2r": sum(1 for r in rows if r["exit_reason"] == "stop_loss"
                                            and (r["mfe_r"] or 0) >= 0.2),
        "signal_exit_mfe_vs_r": [(round(r["mfe_r"], 2), round(r["r"], 2)) for r in rows
                                 if r["exit_reason"] == "trade_exit_signal"][:60],
    },
    "top_trades": sorted([(r["symbol"], fmt(r["opened_ms"]), round(r["pnl"], 2), round(r["r"], 2))
                          for r in rows], key=lambda x: -x[2])[:10],
    "hold_days_median": round(st.median(r["hold_days"] for r in rows), 1),
    "signal_detail_not_taken": [(s["symbol"], fmt(s["close_ms"]), s["why_not"]) for s in taken
                                if not s["taken"]],
}
OUT.write_text(json.dumps(out, indent=1))
print(json.dumps({k: out[k] for k in ("funnel_coin_bars", "buy_signals", "signals_taken",
                                       "signals_not_taken", "trades", "totals")}, indent=1))
