"""FreqAI lab tests. Those needing pandas/freqtrade run in D:/Sonic-FreqAI/venv."""

import io
import zipfile

import pytest

JAN_2023 = 1_672_531_200_000
FOUR = 4 * 3_600_000


def _zip(rows):
    text = "".join(",".join(str(x) for x in r) + "\n" for r in rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", text)
    return buf.getvalue()


def test_frame_reads_our_zips_with_volume(tmp_path):
    pytest.importorskip("pandas")
    from sonic_research.freqai_lab.convert import frame
    (tmp_path / "SOLUSDT-4h-2023-01.zip").write_bytes(_zip([
        [JAN_2023 + FOUR, 2, 4, 1, 3, 10, 0], [JAN_2023, 1, 2, 0.5, 1.5, 5, 0],
        [JAN_2023, 1, 2, 0.5, 1.5, 5, 0]]))
    (tmp_path / "SOLUSDT-4h-2022-12.zip").write_bytes(b"")  # before listing: empty file
    df = frame("SOLUSDT", ["2022-12", "2023-01"], "4h", klines=tmp_path)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert df["close"].tolist() == [1.5, 3.0] and df["volume"].tolist() == [5.0, 10.0]
    assert str(df["date"].dt.tz) == "UTC" and df["date"].iloc[0].value // 10**6 == JAN_2023


def test_frame_refuses_months_after_the_allowed_end(tmp_path):
    pytest.importorskip("pandas")
    from sonic_research.freqai_lab.convert import frame
    with pytest.raises(ValueError, match="beyond_allowed_end"):
        frame("SOLUSDT", ["2023-12", "2024-01"], "4h", klines=tmp_path, end="2023-12")


def test_feather_name_is_freqtrade_spot_style():
    from sonic_research.freqai_lab.convert import feather_name
    assert feather_name("SOLUSDT", "4h") == "SOL_USDT-4h.feather"
    assert feather_name("BTCUSDT", "1h") == "BTC_USDT-1h.feather"


# ---- strategy: past-only features, future target, FreqAI cuts the target at the window end


def _candles(n=400, seed=3):
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    o = np.r_[100.0, c[:-1]]
    return pd.DataFrame({"date": pd.date_range("2023-01-01", periods=n, freq="4h", tz="UTC"),
                         "open": o, "high": np.maximum(o, c) * 1.003,
                         "low": np.minimum(o, c) * 0.997, "close": c,
                         "volume": rng.uniform(100, 200, n)})


def _strategy_module():
    pytest.importorskip("freqtrade")
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "sonic_research" / "freqai_lab" / "strategies" \
        / "SonicFreqAI.py"
    spec = importlib.util.spec_from_file_location("SonicFreqAI", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_features_at_t_do_not_change_when_the_future_changes():
    mod = _strategy_module()
    df = _candles()
    cut = 300
    later = df.copy()
    later.loc[cut + 1:, ["open", "high", "low", "close"]] *= 1.5  # rewrite everything after t
    later.loc[cut + 1:, "volume"] *= 3
    a = mod.sonic_features(df.copy())
    b = mod.sonic_features(later)
    cols = [c for c in a.columns if c.startswith("%")]
    assert len(cols) == 13
    assert a.loc[:cut, cols].equals(b.loc[:cut, cols])


def test_target_looks_24_candles_ahead_net_of_costs():
    import math
    mod = _strategy_module()
    df = mod.sonic_target(_candles())
    c = df["close"]
    assert df["&-fwd_ret"].iloc[10] == pytest.approx(math.log(c.iloc[34] / c.iloc[10]) - 0.0014)
    assert df["&-fwd_ret"].iloc[-24:].isna().all()


def test_target_on_data_cut_at_window_end_has_no_future():
    """What FreqAI does per window: targets are computed on rows before the window end only."""
    mod = _strategy_module()
    df = _candles()
    end = 300
    cut = mod.sonic_target(df.iloc[:end].copy())
    assert cut["&-fwd_ret"].iloc[end - 24:].isna().all()
    assert cut["&-fwd_ret"].iloc[: end - 24].notna().all()


# ---- evaluation: filter (B), rank (C), metrics, pre-registered verdict


def _ev():
    pytest.importorskip("jesse")
    from sonic_research.freqai_lab import evaluate
    return evaluate


def test_filter_keeps_positive_rejects_rest_and_keeps_missing():
    ev = _ev()
    preds = {("SOLUSDT", 100): 0.01, ("XRPUSDT", 100): -0.02, ("ADAUSDT", 100): 0.0}
    cands = [("SOLUSDT", 1.0, 0.1), ("XRPUSDT", 1.0, 0.1), ("ADAUSDT", 1.0, 0.1),
             ("DOGEUSDT", 1.0, 0.1)]
    log = ev.Decisions()
    kept = ev.choose("B", cands, preds, 100, log)
    assert [c[0] for c in kept] == ["SOLUSDT", "DOGEUSDT"]
    assert [r["symbol"] for r in log.rejected] == ["XRPUSDT", "ADAUSDT"]
    assert [m["symbol"] for m in log.missing] == ["DOGEUSDT"]


def test_rank_orders_by_prediction_missing_last():
    ev = _ev()
    preds = {("SOLUSDT", 100): -0.01, ("XRPUSDT", 100): 0.03, ("ADAUSDT", 100): 0.01}
    cands = [("SOLUSDT", 1.0, 0.1), ("DOGEUSDT", 1.0, 0.1), ("XRPUSDT", 1.0, 0.1),
             ("ADAUSDT", 1.0, 0.1)]
    log = ev.Decisions()
    assert [c[0] for c in ev.choose("C", cands, preds, 100, log)] == [
        "XRPUSDT", "ADAUSDT", "SOLUSDT", "DOGEUSDT"]
    assert log.reordered == 1
    assert ev.choose("A", cands, preds, 100, log) == cands


def _t(symbol, opened_day, pnl):
    day = 86_400_000
    return {"symbol": symbol, "opened_ms": 1_640_995_200_000 + opened_day * day,
            "closed_ms": 1_640_995_200_000 + (opened_day + 1) * day, "pnl": pnl,
            "legs": [{"kind": "entry", "raw": 1.0, "qty": 1.0}, {"kind": "exit", "raw": 1.0 + pnl,
                                                                 "qty": 1.0}]}


def test_metrics_profit_factor_drawdown_and_days():
    ev = _ev()
    m = ev.metrics([_t("SOLUSDT", 0, 3.0), _t("XRPUSDT", 0, -1.5), _t("SOLUSDT", 40, 1.0)])
    assert m["trades"] == 3 and m["trading_days"] == 2
    assert m["net"] == pytest.approx(costs_net([3.0, -1.5, 1.0]))
    assert m["profit_factor"] > 1 and m["max_dd_pct"] > 0
    assert set(m["by_year"]) == {"2022"}


def costs_net(moves):
    from sonic_research.jesse_audit.costs import costs
    legs = [[{"raw": 1.0, "qty": 1.0}, {"raw": 1.0 + x, "qty": 1.0}] for x in moves]
    return sum(costs(pair)["pnl"] for pair in legs)


def test_verdict_follows_the_preregistered_rules():
    ev = _ev()
    base = {"net": 10.0, "max_dd_pct": 10.0}
    good = {"net": 12.0, "max_dd_pct": 10.0}
    stable = {"years_better": 2, "after_drop_best_trade": 1.0, "after_drop_each_coin_min": 0.5,
              "reproducible": True, "leak_tests": True}
    assert ev.verdict(base, good, stable)["promising"] is True
    assert ev.verdict(base, good, dict(stable, years_better=1))["promising"] is False
    assert ev.verdict(base, good, dict(stable, after_drop_each_coin_min=-0.1))["promising"] is False
    safer = {"net": 9.2, "max_dd_pct": 7.9}  # -8% profit, -21% drawdown
    assert ev.verdict(base, safer, stable)["promising"] is True
    worse = {"net": 8.5, "max_dd_pct": 7.0}  # -15% profit
    assert ev.verdict(base, worse, stable)["promising"] is False
