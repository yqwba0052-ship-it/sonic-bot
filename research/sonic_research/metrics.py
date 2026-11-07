"""Result summaries for a list of trades."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from .backtest import Trade

RISK_PER_TRADE = 0.01  # equity curve: each trade risks 1% of equity at its stop


def summarize(trades: Sequence[Trade]) -> dict[str, float]:
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "avg_r": 0.0,
                "total_return": 0.0, "max_drawdown": 0.0}
    r = np.array([trade.r for trade in sorted(trades, key=lambda item: item.exit_t)])
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    # Clip single-trade results so one gap cannot dominate the curve (a stop cannot do better).
    equity = np.cumprod(1 + RISK_PER_TRADE * np.clip(r, -3, None))
    peak = np.maximum.accumulate(np.concatenate([[1.0], equity]))[1:]
    return {
        "trades": int(len(r)),
        "win_rate": float((r > 0).mean()),
        "profit_factor": float(gains / losses) if losses else float("inf"),
        "avg_r": float(r.mean()),
        "total_return": float(equity[-1] - 1),
        "max_drawdown": float((1 - equity / peak).max()),
    }
