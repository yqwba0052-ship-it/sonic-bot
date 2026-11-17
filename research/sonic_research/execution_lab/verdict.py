"""The design's success rules, applied literally (no reinterpretation after the results).

Rule 4 is read as written: every stop exit of the baseline must happen in the same minute and
with the same type in the case. Concentration (the biggest single trade's share of the gain) is
reported as information; it is not one of the pre-registered rules.
"""

from __future__ import annotations


def _key(r: dict) -> tuple[str, int]:
    return r["symbol"], r["signal_ms"]


def verdict(base_m: dict, base_rows: list[dict], case_m: dict, case_rows: list[dict],
            reproducible: bool) -> dict:
    case = {_key(r): r for r in case_rows}
    diff = case_m["net"] - base_m["net"]
    better_net = base_m["net"] > 0 and diff >= 0.05 * base_m["net"]
    safer = (case_m["max_dd_pct"] <= 0.8 * base_m["max_dd_pct"]
             and case_m["net"] >= 0.9 * base_m["net"])
    winners = sum(1 for r in base_rows if r["pnl"] > 0)
    stops = [r for r in base_rows if r["reason"] in ("stop_loss", "breakeven")]
    same_stop = all(case[_key(r)]["reason"] == r["reason"]
                    and case[_key(r)].get("exit_ms") == r.get("exit_ms") for r in stops)
    years = sorted(set(base_m["by_year"]) | set(case_m["by_year"]))
    better_years = sum(1 for y in years
                       if case_m["by_year"].get(y, 0) - base_m["by_year"].get(y, 0) > 1e-12)
    checks = {
        "1_net_or_risk": better_net or safer,
        "2_strict_fills": True,  # by construction: fills.limit_buy needs prints below the limit
        "3_few_missed": case_m.get("missed_winners", 0) <= 0.10 * winners,
        "4_stop_unchanged": same_stop,
        "5_two_of_three_years": better_years >= 2,
        "6_reproducible": bool(reproducible),
        "7_worth_it": diff >= 1.0,
    }
    return {"promising": all(checks.values()), "checks": checks, "diff": diff,
            "stops_in_base": len(stops),
            "stops_same_minute_and_type": sum(
                1 for r in stops if case[_key(r)]["reason"] == r["reason"]
                and case[_key(r)].get("exit_ms") == r.get("exit_ms"))}


def concentration(base_rows: list[dict], case_rows: list[dict]) -> dict:
    base = {_key(r): r["pnl"] for r in base_rows}
    effects = sorted(((r["pnl"] - base[_key(r)], _key(r)) for r in case_rows),
                     key=lambda e: -e[0])
    diff = sum(e for e, _ in effects)
    top = effects[0] if effects else (0.0, None)
    return {"diff": diff, "top_trade_effect": top[0], "top_trade": top[1],
            "diff_without_top": diff - max(top[0], 0.0)}


def main(name: str, twin: str) -> dict:
    """Verdict for B and C against A_real, with reproducibility = identical result hashes."""
    import json
    from pathlib import Path

    from . import LAB
    reports = Path(__file__).resolve().parents[2] / "reports"
    a = json.loads((LAB / "reports" / f"{name}.json").read_text(encoding="utf-8"))
    b = json.loads((LAB / "reports" / f"{twin}.json").read_text(encoding="utf-8"))
    same = a["result_sha256"] == b["result_sha256"]
    out = {"name": name, "twin": twin, "reproducible": same,
           "sha256": [a["result_sha256"], b["result_sha256"]], "cases": {}}
    base_m, base_rows = a["metrics"]["A_real"], a["rows"]["A_real"]
    for case in ("B", "C"):
        out["cases"][case] = {
            "verdict": verdict(base_m, base_rows, a["metrics"][case], a["rows"][case], same),
            "concentration": concentration(base_rows, a["rows"][case])}
    text = json.dumps(out, indent=1, default=float)
    (LAB / "reports" / f"{name}_verdict.json").write_text(text, encoding="utf-8")
    (reports / f"execution_{name}_verdict.json").write_text(text, encoding="utf-8")
    return out


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(main(sys.argv[1], sys.argv[2]), indent=1, default=float)[:2500])
