"""INFORMATION ONLY: the rejection stands. The three settings below were picked on 2021-2023
data alone (busiest setting with PF >= 1.2 and profit, per timeframe; diag.json) and frozen
with a hash before this run. Nothing here may be used to adopt or tune Sonic Fast."""
import datetime as dt, hashlib, json, sys
from sonic_research import fast, fast_run

W = sys.argv[1]
diag = json.load(open(f"{W}/diag.json"))
picks = {tf: v["busiest_profitable_setting"]["params"] for tf, v in diag.items()}
frozen = json.dumps(picks, sort_keys=True).encode()
open(f"{W}/info_frozen.json", "wb").write(frozen)
print("frozen picks", hashlib.sha256(frozen).hexdigest()[:16], picks, flush=True)

sonic = fast_run.sonic_trades()
lo, hi = fast_run.ms(fast_run.HOLDOUT[0]), fast_run.ms(fast_run.HOLDOUT[1] + dt.timedelta(days=1))
hold = [s for s in sonic if lo <= s["closed_ms"] < hi]
as_trades = [fast.Trade(s["symbol"], s["opened_ms"], s["closed_ms"], s["entry_price"], s["exit_price"],
                        s["qty"], s["pnl"], s["risk_usdt"] or 0.0, s.get("exit_reason") or "") for s in hold]
out = {"note": "information only; rejection unchanged", "picks": picks, "timeframes": {}}
for tf, text in picks.items():
    p = eval("fast." + text)
    data, cache = fast_run.load(tf), {}
    suitable = {d for d in data["suitable"] if fast_run.HOLDOUT[0] <= d <= fast_run.HOLDOUT[1]}
    if "sonic" not in out:
        out["sonic"] = fast.metrics(as_trades, suitable, *fast_run.HOLDOUT, fast_run.CAPITAL, 0.0014)
        out["sonic_worst_month_pct"] = fast_run.worst_month_pct([(t.closed_ms, t.pnl) for t in as_trades], 30.0)
    res = {}
    for cost in fast_run.COSTS:
        m, trades = fast_run.evaluate(data, tf, p, fast_run.HOLDOUT, cost, cache, sonic)
        both = [(t.closed_ms, t.pnl) for t in as_trades] + [(t.closed_ms, t.pnl) for t in trades]
        res[str(cost)] = {**m, "combined_worst_month_pct": fast_run.worst_month_pct(both, 60.0),
                          "combined_net": round(sum(x for _, x in both), 2)}
    out["timeframes"][tf] = res
    print(tf, json.dumps(res["0.0014"], default=str)[:400], flush=True)
json.dump(out, open(f"{W}/info_oos.json", "w"), indent=1, default=str)
