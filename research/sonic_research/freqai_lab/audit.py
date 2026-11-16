"""Computed evidence for design rules 4 (reproducible) and 5 (no leak), not typed in by hand.

- leak_check: FreqAI names each saved model folder `sub-train-<COIN>_<train end, seconds>`.
  Every prediction row (candle open time d) must have a model whose training ended at or before
  d; a row with no such model would have been predicted with knowledge of its own period.
- reproducible: two runs under different identifiers must give byte-identical prediction files,
  and neither log may show FreqAI reusing a cached prediction file.
- freqai_cut_in_source: guards against a Freqtrade upgrade that stops computing targets on data
  cut at the training window's end.

python -m sonic_research.freqai_lab.audit <name> <twin-name> <YYYY-MM-DD> <SYMBOL>...
(FreqAI environment) writes D:/Sonic-FreqAI/reports/<name>_audit.json
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

CACHED = "Found backtesting prediction file"
CUT = 'dataframe_base_train = dataframe.loc[dataframe["date"] < tr_train.stopdt, :]'


def _model_times(models: Path, coin: str) -> list[int]:
    prefix = f"sub-train-{coin}_"
    return sorted(int(p.name[len(prefix):]) for p in Path(models).glob(prefix + "*")
                  if p.name[len(prefix):].isdigit())


def leak_check(models: Path, predictions: Path, symbols: list[str], start: str) -> dict:
    """start: first predicted day, 'YYYY-MM-DD' (UTC); earlier rows are warm-up, not checked."""
    start_s = datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp()
    bad, rows = [], 0
    for symbol in symbols:
        coin = symbol[:-4]
        times = _model_times(models, coin)
        with (Path(predictions) / f"{coin}_USDT.csv").open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["&-fwd_ret"] in ("", "nan"):
                    continue
                d = int(datetime.fromisoformat(row["date"]).timestamp())
                if d < start_s:
                    continue
                rows += 1
                if bisect.bisect_right(times, d) == 0:  # no model trained before this candle
                    bad.append({"symbol": symbol, "date": row["date"]})
    return {"ok": not bad and rows > 0, "rows": rows, "bad": bad[:20]}


def _digest(folder: Path, symbols: list[str]) -> dict[str, str]:
    return {s: hashlib.sha256((Path(folder) / f"{s[:-4]}_USDT.csv").read_bytes()).hexdigest()
            for s in symbols}


def reproducible(a: Path, b: Path, a_log: Path, b_log: Path, symbols: list[str]) -> dict:
    da, db = _digest(a, symbols), _digest(b, symbols)
    cached = {str(p): Path(p).read_text(encoding="utf-8", errors="replace").count(CACHED)
              for p in (a_log, b_log)}
    same = da == db
    return {"ok": same and not any(cached.values()), "identical_files": same,
            "cached_prediction_lines": cached, "sha256": da}


def freqai_cut_in_source() -> bool:
    import inspect

    from freqtrade.freqai.freqai_interface import IFreqaiModel
    return CUT in inspect.getsource(IFreqaiModel.start_backtesting)


def main(name: str, twin: str, start: str, symbols: list[str]) -> dict:
    from . import LAB
    out = {
        "leak": leak_check(LAB / "user_data" / "models" / name, LAB / "predictions" / name,
                           symbols, start),
        "reproducible": reproducible(LAB / "predictions" / name, LAB / "predictions" / twin,
                                     LAB / "logs" / f"{name}.log", LAB / "logs" / f"{twin}.log",
                                     symbols),
        "freqai_cut_in_source": freqai_cut_in_source(),
    }
    out["leak_tests"] = out["leak"]["ok"] and out["freqai_cut_in_source"]
    (LAB / "reports").mkdir(parents=True, exist_ok=True)
    (LAB / "reports" / f"{name}_audit.json").write_text(json.dumps(out, indent=1),
                                                        encoding="utf-8")
    return out


if __name__ == "__main__":
    result = main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:])
    print(json.dumps({k: (v["ok"] if isinstance(v, dict) else v) for k, v in result.items()}))
