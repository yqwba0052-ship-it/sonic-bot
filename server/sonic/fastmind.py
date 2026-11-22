"""River learner for the fast desk: shadow mode only (it predicts, it never decides).

The experience lives in the tool-neutral table fast_experience (fastdesk.py). The model file is
only a cache: rebuild() replays every finished experience row in order, so deleting River or
swapping it for another tool loses nothing.

Promotion out of shadow mode needs ALL of: >= 300 experiences, the trades it would have taken
(p >= 0.5) beat all trades after fees over the last 28 days, and the owner's approval. This
module only reports eligibility; nothing here changes a trade.
"""

from __future__ import annotations

import asyncio
import json
import math
import pickle
import sqlite3
from pathlib import Path

MIN_EXPERIENCE = 300
REPORT_DAYS = 28
TAKE_AT = 0.5
SAVE_EVERY = 50

try:  # optional: the desk records experience with or without River
    from river import compose, linear_model, preprocessing
except ImportError:  # pragma: no cover - exercised by monkeypatching `available`
    compose = linear_model = preprocessing = None


def x_of(features: dict) -> dict[str, float]:
    """Numeric inputs from what the bot saw at the decision (never from the outcome)."""
    hour = float(features.get("hour", 0))
    share = features.get("buyshare30")
    return {
        "rise": float(features.get("rise", 0.0)),
        "up10": 1.0 if features.get("up10") else 0.0,
        "breakout": 1.0 if features.get("breakout") else 0.0,
        "buyshare30": 0.5 if share is None else float(share),
        "in_session": 1.0 if features.get("in_session") else 0.0,
        "tf": float(features.get("tf", 60)) / 900.0,
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "vol60": float(features.get("vol60", 0.0)),
    }


class Mind:
    def __init__(self, db: sqlite3.Connection, model_path: str | None = None) -> None:
        self.db = db
        self.model_path = Path(model_path) if model_path else None
        self.available = compose is not None
        self.learned = 0
        self.model = None
        self.needs_rebuild = False
        self.queued: list[tuple[dict, bool]] = []  # learned while a rebuild runs
        if not self.available:
            return
        if self.model_path and self.model_path.exists():
            try:
                with self.model_path.open("rb") as fh:
                    self.model, self.learned = pickle.load(fh)  # noqa: S301 - our own file
            except Exception:  # noqa: BLE001 - a broken cache is rebuilt from experience
                self.model = None
        if self.model is None:
            done = db.execute("SELECT COUNT(*) FROM fast_experience WHERE net IS NOT NULL"
                              ).fetchone()[0]
            if done == 0:
                self.model = self._new_model()
            else:  # never block Sonic's start: rebuild_in_background() does it
                self.needs_rebuild = True

    @staticmethod
    def _new_model():
        return compose.Pipeline(preprocessing.StandardScaler(),
                                linear_model.LogisticRegression())

    def predict(self, features: dict) -> float | None:
        if not self.available or self.model is None:
            return None
        return float(self.model.predict_proba_one(x_of(features)).get(True, 0.5))

    def learn(self, features: dict, net: float) -> None:
        if not self.available:
            return
        if self.model is None:
            if self.needs_rebuild:
                self.queued.append((x_of(features), net > 0))
            return
        self.model.learn_one(x_of(features), net > 0)
        self.learned += 1
        if self.learned % SAVE_EVERY == 0:
            self.save()

    def save(self) -> bool:
        """The file is only a cache: a failed save is harmless (rebuilt from experience)."""
        if not (self.available and self.model is not None and self.model_path):
            return False
        try:
            tmp = self.model_path.with_suffix(".tmp")
            with tmp.open("wb") as fh:
                pickle.dump((self.model, self.learned), fh)
            tmp.replace(self.model_path)
            return True
        except OSError:
            return False

    def _rows(self) -> list[tuple[dict, bool]]:
        return [(x_of(json.loads(f)), net > 0) for f, net in self.db.execute(
            "SELECT features, net FROM fast_experience WHERE net IS NOT NULL ORDER BY id")]

    def _train(self, rows: list[tuple[dict, bool]]):
        model = self._new_model()
        for x, y in rows:
            model.learn_one(x, y)
        return model

    def rebuild(self) -> int:
        """Fresh model from the whole experience table, oldest first (blocking)."""
        if not self.available:
            return 0
        rows = self._rows()
        self.model, self.learned = self._train(rows), len(rows)
        self.queued, self.needs_rebuild = [], False
        self.save()
        return self.learned

    async def rebuild_in_background(self) -> int:
        """Read in the loop thread (shared sqlite connection), train in a worker thread."""
        if not (self.available and self.needs_rebuild):
            return 0
        rows = self._rows()
        model = await asyncio.to_thread(self._train, rows)
        for x, y in self.queued:  # learned while training: nothing is lost
            model.learn_one(x, y)
        self.model, self.learned = model, len(rows) + len(self.queued)
        self.queued, self.needs_rebuild = [], False
        self.save()
        return self.learned

    def shadow_report(self, now_ms: int) -> dict:
        since = now_ms // 1000 - REPORT_DAYS * 86400
        n = self.db.execute("SELECT COUNT(*) FROM fast_experience WHERE net IS NOT NULL"
                            ).fetchone()[0]
        all_n, all_mean = self.db.execute(
            "SELECT COUNT(*), AVG(net) FROM fast_experience WHERE closed >= ? AND net IS NOT NULL",
            (since,)).fetchone()
        taken_n, taken_mean = self.db.execute(
            "SELECT COUNT(*), AVG(net) FROM fast_experience WHERE closed >= ? "
            "AND net IS NOT NULL AND predicted >= ?", (since, TAKE_AT)).fetchone()
        eligible = bool(n >= MIN_EXPERIENCE and taken_n and all_mean is not None
                        and taken_mean > all_mean)
        return {"available": self.available, "experience": n, "learned": self.learned,
                "rebuilding": self.needs_rebuild,
                "all_n_28d": all_n, "all_mean_28d": all_mean,
                "taken_n_28d": taken_n, "taken_mean_28d": taken_mean,
                "eligible_for_owner_review": eligible, "mode": "shadow"}
