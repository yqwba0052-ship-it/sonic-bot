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
