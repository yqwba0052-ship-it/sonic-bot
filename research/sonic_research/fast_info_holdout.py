"""INFORMATION ONLY: the rejection of Sonic Fast stands.

The three settings were picked on 2021-2023 data alone (per timeframe, the busiest setting with
profit factor >= 1.2 and a profit) and frozen with a hash before one run on 2024-2026. Nothing
here may be used to adopt or tune Sonic Fast. Result: research/reports/sonic_fast_info_holdout.json

python -m sonic_research.fast_info_holdout <workspace with diag.json>
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys

from . import fast, fast_run


def params_of(text: str) -> fast.Params:
    """Params(L=30, k=6, ks=2.0, kt=1.0, guard='fixed') -> fast.Params, without eval."""
    found = dict(re.findall(r"(\w+)=('?[\w.]+'?)", text))
    return fast.Params(int(found["L"]), int(found["k"]), float(found["ks"]), float(found["kt"]),
                       found["guard"].strip("'"))


def main(workspace: str) -> None:
    diag = json.load(open(f"{workspace}/diag.json", encoding="utf-8"))
    picks = {tf: v["busiest_profitable_setting"]["params"] for tf, v in diag.items()}
    frozen = json.dumps(picks, sort_keys=True).encode()
    open(f"{workspace}/info_frozen.json", "wb").write(frozen)
    print("frozen picks", hashlib.sha256(frozen).hexdigest()[:16], picks, flush=True)

    sonic = fast_run.sonic_trades()
    lo = fast_run.ms(fast_run.HOLDOUT[0])
    hi = fast_run.ms(fast_run.HOLDOUT[1] + dt.timedelta(days=1))
    hold = [s for s in sonic if lo <= s["closed_ms"] < hi]
    as_trades = [fast.Trade(s["symbol"], s["opened_ms"], s["closed_ms"], s["entry_price"],
                            s["exit_price"], s["qty"], s["pnl"], s["risk_usdt"] or 0.0,
                            s.get("exit_reason") or "") for s in hold]
    sonic_closed = [(t.closed_ms, t.pnl) for t in as_trades]
    out: dict = {"note": "information only; rejection unchanged", "picks": picks,
                 "timeframes": {}}
    for tf, text in picks.items():
        p = params_of(text)
        data, cache = fast_run.load(tf), {}
        suitable = {d for d in data["suitable"]
                    if fast_run.HOLDOUT[0] <= d <= fast_run.HOLDOUT[1]}
        if "sonic" not in out:
            out["sonic"] = fast.metrics(as_trades, suitable, *fast_run.HOLDOUT,
                                        fast_run.CAPITAL, 0.0014)
            out["sonic_worst_month_pct"] = fast_run.worst_month_pct(sonic_closed, 30.0)
        res = {}
        for cost in fast_run.COSTS:
            m, trades = fast_run.evaluate(data, tf, p, fast_run.HOLDOUT, cost, cache, sonic)
            both = sonic_closed + [(t.closed_ms, t.pnl) for t in trades]
            res[str(cost)] = {**m, "combined_worst_month_pct": fast_run.worst_month_pct(both, 60.0),
                              "combined_net": round(sum(x for _, x in both), 2)}
        out["timeframes"][tf] = res
        print(tf, json.dumps(res["0.0014"], default=str)[:400], flush=True)
    with open(f"{workspace}/info_oos.json", "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=1, default=str)


if __name__ == "__main__":
    main(sys.argv[1])
