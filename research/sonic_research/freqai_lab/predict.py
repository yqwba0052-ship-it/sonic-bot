"""Run FreqAI's walk-forward training inside `freqtrade backtesting` and collect its predictions.

Freqtrade runs with an empty key and secret in dry-run backtesting on local feather files. It may
read Binance's PUBLIC market list (no account, no orders). Models, predictions and logs go to
D:/Sonic-FreqAI. Run with D:/Sonic-FreqAI/venv/Scripts/python.

python -m sonic_research.freqai_lab.predict <name> <YYYYMMDD-YYYYMMDD> <SYMBOL> [<SYMBOL> ...]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from . import BTC, LAB

STRATEGIES = Path(__file__).resolve().parent / "strategies"
FREQTRADE = Path(sys.executable).parent / "freqtrade.exe"
MODEL_PARAMS = {"n_estimators": 300, "learning_rate": 0.03, "num_leaves": 15,
                "random_state": 42, "deterministic": True, "n_jobs": 4, "verbose": -1}


def pair(symbol: str) -> str:
    return f"{symbol[:-4]}/USDT"


def build_config(name: str, symbols: list[str]) -> dict:
    return {
        "trading_mode": "spot", "max_open_trades": 3, "stake_currency": "USDT",
        "stake_amount": 10, "dry_run": True, "dry_run_wallet": 1000, "timeframe": "4h",
        "dataformat_ohlcv": "feather", "fee": 0.0,
        "entry_pricing": {"price_side": "same", "use_order_book": False,
                          "price_last_balance": 0.0},
        "exit_pricing": {"price_side": "same", "use_order_book": False},
        "exchange": {"name": "binance", "key": "", "secret": "",
                     "pair_whitelist": [pair(s) for s in symbols + [BTC]],
                     "pair_blacklist": []},
        "pairlists": [{"method": "StaticPairList"}],
        "sonic_predictions_dir": str(LAB / "predictions" / name),
        "freqai": {
            "enabled": True, "identifier": name, "purge_old_models": False,
            "train_period_days": 210, "backtest_period_days": 30, "live_retrain_hours": 0,
            "save_backtest_models": True, "activate_tensorboard": False,
            "feature_parameters": {
                "include_timeframes": ["4h"], "include_corr_pairlist": [pair(BTC)],
                "label_period_candles": 24, "include_shifted_candles": 2,
                "indicator_periods_candles": [14], "DI_threshold": 0, "weight_factor": 0,
                "principal_component_analysis": False, "use_SVM_to_remove_outliers": False,
            },
            "data_split_parameters": {"test_size": 0.1, "shuffle": False},
            "model_training_parameters": MODEL_PARAMS,
        },
    }


def run(name: str, timerange: str, symbols: list[str], data: str = "binance") -> dict:
    """One FreqAI walk-forward run; returns where its predictions and log are."""
    user = LAB / "user_data"
    for sub in ("models", "backtest_results", "logs", "data"):
        (user / sub).mkdir(parents=True, exist_ok=True)
    cfg_path = LAB / "configs" / f"{name}.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = build_config(name, symbols)
    cfg["exchange"]["pair_whitelist"] = [pair(s) for s in symbols]
    cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    log = LAB / "logs" / f"{name}.log"
    env = dict(os.environ, TMP=str(LAB / "cache" / "tmp"), TEMP=str(LAB / "cache" / "tmp"))
    cmd = [str(FREQTRADE), "backtesting", "-c", str(cfg_path), "--userdir", str(user),
           "--datadir", str(LAB / "data" / data), "--strategy", "SonicFreqAI",
           "--strategy-path", str(STRATEGIES), "--freqaimodel", "LightGBMRegressor",
           "--timerange", timerange, "--cache", "none", "--logfile", str(log)]
    started = time.time()
    done = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    (LAB / "logs" / f"{name}.stdout.txt").write_text(done.stdout + "\n" + done.stderr,
                                                     encoding="utf-8")
    return {"name": name, "returncode": done.returncode, "seconds": round(time.time() - started),
            "predictions": str(LAB / "predictions" / name), "log": str(log),
            "at": datetime.now(UTC).isoformat(timespec="seconds")}


if __name__ == "__main__":
    args = sys.argv[3:]
    data = args.pop(0).split("=", 1)[1] if args and args[0].startswith("--data=") else "binance"
    print(json.dumps(run(sys.argv[1], sys.argv[2], args, data=data), indent=1))
