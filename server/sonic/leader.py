"""Learn from other traders' closed trades and test what was learned. Paper only.

Access (checked 2026-11-11 on developers.binance.com): the official copy-trading API has two
endpoints, both about the CALLER's own lead-trader account (userStatus, leadSymbol). No official
endpoint returns another lead trader's positions, or a follower's copy trades and the lead
trader they came from. The public lead pages run on internal, undocumented endpoints, which this
module does not use. Trades therefore come from:
- CSV import (one closed position per row, see TEMPLATE; or single fills, paired here), and
- the owner's own futures history through a READ-ONLY key (account.py). Binance does not say
  which lead trader a copied trade came from, so those trades are only attributed to a trader
  when the owner says so.

Per trader it stores the trades, links each one to public 4-hour market data, describes the
style (sides, trend, RSI zone, risk, holding time), learns an independent rule set from the
first 70% of the trades and tests it on the rest of the time span (out of sample) next to
Sonic's own rules in the same simulator. A learned rule set is a guess at patterns in the
trader's results, NOT the trader's real strategy. Only traders the owner added are studied.
Pausing a trader stops its paper trading; deleting it (the app asks "are you sure?" first)
takes it off the list and stops watching it. Either way its trades, models and test results
stay saved and keep taking part in the strategy ranking; adding the same trader again brings it
back. Paper trading runs in its own tables, never sends an order, never touches Sonic's strategy,
and no model is ever used for real trading.

Ranking per coin (compare_coins()): the latest learned version of every trader's strategy,
deleted traders included, is run again on each coin it trades, OUT OF SAMPLE only. When two or
more strategies trade the same coin they are compared over the same period (the overlap of their
test periods, so the same market for all); a coin with one strategy is judged on its own. Score:
profit after fees per year, profit factor, profitable months, deepest drawdown, worst single
trade, and win rate. Too little data, losses, deep drawdowns, big single losses or unstable
months leave a strategy out, with the reason. The best per coin is paper-traded on that coin
("champion" track). It is never used for real trading, and it never replaces a strategy the
owner approved or changes its money.
"""

from __future__ import annotations

import bisect
import csv
import io
import json
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from . import strategy
from .strategy import Bar

BAR_MS = strategy.BAR_MS
INTERVAL = strategy.INTERVAL
WARMUP = 300  # 4h bars before a trade so the indicators have settled
FEE = 0.0005  # taker fee per side
SLIP = 0.0002  # slippage per side
COST = 2 * (FEE + SLIP)  # round trip, as a share of the price
TRAIN_SHARE = 0.7
MIN_TRADES = 20  # fewer usable closed trades than this is too little to learn from
MAX_PAGES = 12  # 1500 bars a page: about 8 years of 4h bars
CAPITAL = 30.0
RISK = 0.01
MAX_OPEN = 3
SONIC_SYMBOLS = ("SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "SUIUSDT", "TRXUSDT",
                 "BNBUSDT", "AVAXUSDT")
TRADER_ID = re.compile(r"^[A-Za-z0-9_-]{3,40}$")
SYMBOL = re.compile(r"^[A-Z0-9]{2,20}USDT$")
ACCOUNT_ID = "my-account"  # owner's own synced trades not yet attributed to a trader
EPS = 1e-12

TEMPLATE = (
    "symbol,side,leverage,qty,entry_price,exit_price,opened_at,closed_at,pnl,fee\n"
    "BTCUSDT,long,10,0.002,67000,68200,2026-10-01 08:00,2026-10-02 14:30,2.40,0.13\n"
    "ETHUSDT,short,5,0.05,2600,2550,2026-10-03 12:15,2026-10-03 20:00,2.50,0.13\n"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS traders (
    trader_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    added_ms INTEGER NOT NULL,
    changed_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trader_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    leverage REAL,
    qty REAL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    opened_ms INTEGER NOT NULL,
    closed_ms INTEGER NOT NULL,
    pnl REAL,
    fee REAL,
    source TEXT NOT NULL,
    imported_ms INTEGER NOT NULL,
    UNIQUE (trader_id, symbol, side, opened_ms, entry_price)
);
CREATE TABLE IF NOT EXISTS trader_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_id TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    model TEXT,
    report TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trader_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    leverage REAL,
    qty REAL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    opened_ms INTEGER,
    closed_ms INTEGER,
    pnl REAL,
    fee REAL,
    missing TEXT NOT NULL,
    imported_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lab_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS champion_paper (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_id TEXT NOT NULL,
    model_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    opened_ms INTEGER NOT NULL,
    until_ms INTEGER NOT NULL,
    closed_ms INTEGER,
    exit_price REAL,
    ret REAL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS trader_paper (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_id TEXT NOT NULL,
    model_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    opened_ms INTEGER NOT NULL,
    until_ms INTEGER NOT NULL,
    closed_ms INTEGER,
    exit_price REAL,
    ret REAL,
    reason TEXT
);
"""

BarsUntil = Callable[[str, str, int, int], Awaitable[list[Bar]]]


# ---------- small helpers

def pct(values: list[float], q: float) -> float:
    """Linear percentile, q in 0..1 (nan when empty)."""
    data = sorted(values)
    if not data:
        return math.nan
    pos = (len(data) - 1) * q
    low, high = math.floor(pos), math.ceil(pos)
    return data[low] + (data[high] - data[low]) * (pos - low)


def median(values: list[float]) -> float:
    return pct(values, 0.5)


def share(flags: list[bool]) -> float | None:
    return sum(flags) / len(flags) if flags else None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def rnd(value: float | None, places: int = 4) -> float | None:
    return None if value is None or (isinstance(value, float) and math.isnan(value)) \
        else round(value, places)


DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def number(text: str) -> float | None:
    text = text.translate(DIGITS).strip().replace(",", "")
    text = re.sub(r"(?i)usdt|x$|%", "", text).strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d")


def moment(text: str, offset_min: int = 0) -> int | None:
    """Milliseconds, UTC. Epoch seconds or ms, ISO, or common date-time forms. A time without a
    zone is local time `offset_min` minutes east of UTC (0 = already UTC); a time that carries
    its own zone keeps it."""
    text = text.translate(DIGITS).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,13}", text):
        value = int(text)
        return value if value > 10**11 else value * 1000
    when = None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for form in TIME_FORMATS:
            try:
                when = datetime.strptime(text, form)
                break
            except ValueError:
                continue
    if when is None:
        return None
    if when.tzinfo is None:
        return int(when.replace(tzinfo=UTC).timestamp() * 1000) - offset_min * 60_000
    return int(when.timestamp() * 1000)


def symbol_of(text: str) -> str | None:
    value = re.sub(r"(?i)perp(etual)?|[\s/\-_:.]", "", text.translate(DIGITS)).upper()
    if value and not value.endswith("USDT") and re.fullmatch(r"[A-Z0-9]{2,15}", value):
        value += "USDT"
    return value if SYMBOL.fullmatch(value) else None


def side_of(text: str) -> str | None:
    value = text.strip().lower()
    if any(word in value for word in ("long", "buy", "شراء", "لونج")):
        return "BUY"
    if any(word in value for word in ("short", "sell", "بيع", "شورت")):
        return "SELL"
    return None


def trader_ref(text: str) -> str | None:
    """A lead trader id from a Binance link (.../lead-details/<id>, ?portfolioId=<id>) or the
    id itself. Only the text is read; the link is never opened."""
    text = text.strip()
    patterns = (r"lead-details/([A-Za-z0-9_-]{3,40})", r"[?&]portfolioId=([A-Za-z0-9_-]{3,40})")
    for pattern in patterns:
        found = re.search(pattern, text)
        if found:
            return found.group(1)
    return text if TRADER_ID.fullmatch(text) else None


# ---------- CSV import

ALIASES = {
    "symbol": ("symbol", "pair", "contract", "coin", "العملة", "الرمز", "الزوج"),
    "side": ("side", "direction", "position side", "position", "الاتجاه", "النوع", "long/short"),
    "leverage": ("leverage", "lev", "الرافعة"),
    "qty": ("qty", "quantity", "size", "position size", "max open interest", "amount",
            "الكمية", "الحجم"),
    "entry_price": ("entry_price", "entry price", "entry", "open price", "avg entry price",
                    "avg. entry price", "average entry price", "opening price",
                    "سعر الدخول"),
    "exit_price": ("exit_price", "exit price", "close price", "closing price",
                   "avg. close price", "average close price", "avg close price",
                   "avg close price", "سعر الخروج"),
    "opened_at": ("opened_at", "opened at", "open time", "opened", "entry time", "time opened",
                  "وقت الدخول", "وقت الفتح"),
    "closed_at": ("closed_at", "closed at", "close time", "closed", "exit time", "time closed",
                  "وقت الخروج", "وقت الإغلاق"),
    "pnl": ("pnl", "realized pnl", "closed pnl", "closing pnl", "realized profit", "profit",
            "الربح", "الربح والخسارة"),
    "fee": ("fee", "fees", "commission", "الرسوم", "العمولة"),
    "time": ("date(utc)", "date (utc)", "date", "time", "datetime", "التاريخ", "الوقت"),
    "price": ("price", "السعر"),
}


def _columns(header: list[str]) -> dict[str, int]:
    clean = [re.sub(r"\s+", " ", h.replace("﻿", "")).strip().lower() for h in header]
    bare = [re.sub(r"\s*\([^)]*\)", "", h).strip() for h in clean]  # "Opened (UTC)" -> "opened"
    found: dict[str, int] = {}
    for key, names in ALIASES.items():
        for names_of in (clean, bare):
            index = next((i for i, n in enumerate(names_of)
                          if n in names and i not in found.values()), None)
            if index is not None:
                found[key] = index
                break
    return found


def _cell(row: list[str], cols: dict[str, int], key: str) -> str:
    index = cols.get(key)
    return row[index] if index is not None and index < len(row) else ""


def parse_csv(text: str, tz_offset_min: int = 0) -> dict[str, Any]:
    """{'trades': [...], 'errors': [[row, code]], 'skipped': open positions, 'format': ...}.
    Missing values stay missing (None); nothing is guessed. Times without a zone are taken as
    local time tz_offset_min east of UTC, unless the file's time column says UTC (Binance)."""
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff").strip())))
    if len(rows) < 2:
        return {"trades": [], "drafts": [], "errors": [[1, "empty"]], "skipped": 0,
                "format": None, "rows": []}
    cols = _columns(rows[0])
    time_headers = [rows[0][cols[k]].lower() for k in ("opened_at", "closed_at", "time")
                    if k in cols and cols[k] < len(rows[0])]
    offset = 0 if any("utc" in h for h in time_headers) else tz_offset_min
    position_keys = {"symbol", "side", "entry_price", "exit_price", "opened_at", "closed_at"}
    fill_keys = {"symbol", "side", "price", "qty", "time"}
    if {"symbol", "side", "entry_price", "exit_price"} <= cols.keys():
        return {**_positions(rows[1:], cols, offset), "format": "positions",
                "utc_offset": offset}
    if fill_keys <= cols.keys():
        return {**pair_fills(_fill_rows(rows[1:], cols, offset), "pnl" in cols),
                "format": "fills", "utc_offset": offset}
    missing = sorted(position_keys - cols.keys())
    return {"trades": [], "drafts": [], "errors": [[1, "unknown_columns:" + ",".join(missing)]],
            "skipped": 0, "format": None, "rows": []}


NEEDED_TO_LEARN = ("opened_at", "closed_at")  # without these a trade cannot be linked to the market
OPTIONAL = ("leverage", "qty", "pnl", "fee")


def _positions(rows: list[list[str]], cols: dict[str, int], offset: int = 0) -> dict[str, Any]:
    """Each row ends as: a trade (complete), a draft (prices known, times missing: kept, but
    never used for learning or paper trading), still open (skipped) or an error. Every row
    reports the fields it lacks; nothing is filled in by guessing."""
    trades: list[dict[str, Any]] = []
    drafts: list[dict[str, Any]] = []
    errors: list[list[Any]] = []
    report: list[dict[str, Any]] = []
    skipped = 0
    for line, row in enumerate(rows, start=2):
        if not any(cell.strip() for cell in row):
            continue
        symbol = symbol_of(_cell(row, cols, "symbol"))
        side = side_of(_cell(row, cols, "side"))
        entry = number(_cell(row, cols, "entry_price"))
        exit_ = number(_cell(row, cols, "exit_price"))
        opened = moment(_cell(row, cols, "opened_at"), offset)
        closed = moment(_cell(row, cols, "closed_at"), offset)
        if exit_ is None and closed is None and symbol and side and entry and opened:
            skipped += 1  # still open: nothing to learn from yet
            report.append({"line": line, "status": "open", "missing": ["exit_price", "closed_at"]})
            continue
        problem = (
            "symbol" if symbol is None else "side" if side is None
            else "entry_price" if not entry or entry <= 0
            else "exit_price" if not exit_ or exit_ <= 0
            else "closed_at" if opened is not None and closed is not None and closed < opened
            else ""
        )
        if problem:
            errors.append([line, problem])
            report.append({"line": line, "status": "error", "missing": [problem]})
            continue
        item = {
            "symbol": symbol, "side": side,
            "leverage": number(_cell(row, cols, "leverage")),
            "qty": number(_cell(row, cols, "qty")),
            "entry_price": entry, "exit_price": exit_,
            "opened_ms": opened, "closed_ms": closed,
            "pnl": number(_cell(row, cols, "pnl")),
            "fee": number(_cell(row, cols, "fee")),
        }
        lacking = [k for k, v in (("opened_at", opened), ("closed_at", closed)) if v is None]
        optional = [k for k in OPTIONAL if item[k] is None]
        if lacking:
            drafts.append({**item, "line": line, "missing": lacking + optional})
            report.append({"line": line, "status": "draft", "missing": lacking + optional})
        else:
            trades.append(item)
            report.append({"line": line, "status": "saved", "missing": optional})
    return {"trades": trades, "drafts": drafts, "errors": errors, "skipped": skipped,
            "rows": report}


def _fill_rows(rows: list[list[str]], cols: dict[str, int], offset: int = 0) -> dict[str, Any]:
    fills, errors = [], []
    for line, row in enumerate(rows, start=2):
        if not any(cell.strip() for cell in row):
            continue
        symbol = symbol_of(_cell(row, cols, "symbol"))
        side = side_of(_cell(row, cols, "side"))
        price = number(_cell(row, cols, "price"))
        qty = number(_cell(row, cols, "qty"))
        at = moment(_cell(row, cols, "time"), offset)
        if not symbol or not side or not price or not qty or at is None:
            errors.append([line, "fill"])
            continue
        fills.append({"at": at, "symbol": symbol, "side": side, "price": price, "qty": abs(qty),
                      "pnl": number(_cell(row, cols, "pnl")) or 0.0,
                      "fee": abs(number(_cell(row, cols, "fee")) or 0.0)})
    return {"fills": fills, "errors": errors}


def pair_fills(parsed: dict[str, Any], has_pnl: bool) -> dict[str, Any]:
    """Pair single fills into closed positions: a position opens when the net size leaves zero
    and closes when it returns to zero; a flip closes one position and opens the next."""
    books: dict[str, dict[str, float | str | int]] = {}
    trades: list[dict[str, Any]] = []
    for fill in sorted(parsed["fills"], key=lambda f: f["at"]):
        symbol, side, price = fill["symbol"], fill["side"], fill["price"]
        left, pnl, fee = fill["qty"], fill["pnl"], fill["fee"]
        while left > EPS:
            book = books.get(symbol)
            if book is None:
                book = books[symbol] = {"side": side, "size": 0.0, "in_value": 0.0, "in_size": 0.0,
                                        "out_value": 0.0, "out_size": 0.0, "opened": fill["at"],
                                        "pnl": 0.0, "fee": 0.0}
            if side == book["side"]:
                book["size"] += left
                book["in_value"] += left * price
                book["in_size"] += left
                book["pnl"] += pnl
                book["fee"] += fee
                left = 0.0
                continue
            used = min(left, book["size"])
            part = used / left
            book["out_value"] += used * price
            book["out_size"] += used
            book["size"] -= used
            book["pnl"] += pnl  # realized profit belongs to the closing part only
            book["fee"] += fee * part
            pnl, fee, left = 0.0, fee * (1 - part), left - used
            if book["size"] <= EPS:
                trades.append({
                    "symbol": symbol, "side": book["side"], "leverage": None,
                    "qty": book["in_size"],
                    "entry_price": book["in_value"] / book["in_size"],
                    "exit_price": book["out_value"] / book["out_size"],
                    "opened_ms": book["opened"], "closed_ms": fill["at"],
                    "pnl": book["pnl"] if has_pnl else None,
                    "fee": book["fee"] or None,
                })
                del books[symbol]
    return {"trades": trades, "drafts": [], "errors": parsed["errors"], "skipped": len(books),
            "rows": []}


# ---------- screenshots of Binance's position history (text read by OCR on the server)

TIME_RE = re.compile(r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})[ T]+(\d{1,2}:\d{2}(?::\d{2})?)")
NUM = r"([-+]?\d[\d,]*\.?\d*)"
LABELS = {
    "entry_price": r"(?:entry(?:\s*price)?|open(?:ing)?\s*price|"
                   r"سعر\s*(?:ال)?دخول|سعر\s*(?:ال)?فتح)",
    "exit_price": r"(?:(?:avg\.?\s*)?clos(?:e|ing)\s*price|exit\s*price|"
                  r"سعر\s*(?:ال)?إغلاق|سعر\s*(?:ال)?اغلاق|سعر\s*(?:ال)?خروج)",
    "pnl": r"(?:clos(?:e|ing)\s*pnl|realized\s*pnl|pnl|الربح(?:\s*و\s*الخسارة)?)",
    "qty": r"(?:max\s*open\s*interest|closed\s*vol(?:ume)?\.?|size|qty|quantity|الكمية|الحجم)",
    "fee": r"(?:fee|commission|الرسوم|العمولة)",
}


def parse_screenshot(text: str, tz_offset_min: int = 0) -> dict[str, Any]:
    """Positions from the text of a screenshot. A block starts at each coin name (…USDT).
    Inside it: side and leverage (Long 20x), the first time = opened, the second = closed,
    prices and profit next to their labels. A field that is not clearly there stays missing:
    no prices -> error; prices without both times -> draft. Times on screen are the phone's
    local time unless the screen says UTC."""
    text = text.translate(DIGITS)
    offset = 0 if re.search(r"\bUTC\b", text) else tz_offset_min
    starts = [m.start() for m in re.finditer(r"\b[A-Z0-9]{2,15}USDT\b", text)]
    trades: list[dict[str, Any]] = []
    drafts: list[dict[str, Any]] = []
    errors: list[list[Any]] = []
    report: list[dict[str, Any]] = []
    for n, start in enumerate(starts):
        block = text[start: starts[n + 1] if n + 1 < len(starts) else len(text)]
        symbol = symbol_of(re.match(r"[A-Z0-9]{2,15}USDT", block).group(0))
        side = side_of(block[:200])
        lev = re.search(r"(\d{1,3})\s*[xX×]", block)
        values: dict[str, float | None] = {}
        for key, label in LABELS.items():
            found = re.search(label + r"[^\d\-+]{0,25}" + NUM, block, re.IGNORECASE)
            values[key] = number(found.group(1)) if found else None
        times = [moment(f"{d.replace('/', '-').replace('.', '-')} {t}", offset)
                 for d, t in TIME_RE.findall(block)]
        opened = times[0] if times else None
        closed = times[1] if len(times) > 1 else None
        line = n + 1
        item = {"symbol": symbol, "side": side,
                "leverage": float(lev.group(1)) if lev else None,
                "qty": abs(values["qty"]) if values["qty"] else None,
                "entry_price": values["entry_price"], "exit_price": values["exit_price"],
                "opened_ms": opened, "closed_ms": closed, "pnl": values["pnl"],
                "fee": abs(values["fee"]) if values["fee"] is not None else None}
        problem = ("symbol" if symbol is None else "side" if side is None
                   else "entry_price" if not item["entry_price"]
                   else "exit_price" if not item["exit_price"]
                   else "closed_at" if opened and closed and closed < opened else "")
        if problem:
            errors.append([line, problem])
            report.append({"line": line, "status": "error", "missing": [problem]})
            continue
        lacking = [k for k, v in (("opened_at", opened), ("closed_at", closed)) if v is None]
        optional = [k for k in OPTIONAL if item[k] is None]
        if lacking:
            drafts.append({**item, "line": line, "missing": lacking + optional})
            report.append({"line": line, "status": "draft", "missing": lacking + optional})
        else:
            trades.append(item)
            report.append({"line": line, "status": "saved", "missing": optional})
    return {"trades": trades, "drafts": drafts, "errors": errors, "skipped": 0, "rows": report,
            "format": "screenshot", "utc_offset": offset}


def same_trade(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two records of one closed position (from different sources): same coin and side, entry
    and exit within 0.05%, size equal within 1% when both are known."""
    def near(x: float | None, y: float | None, tol: float) -> bool:
        return x is not None and y is not None and abs(x - y) <= tol * max(abs(x), abs(y))
    if a["symbol"] != b["symbol"] or a["side"] != b["side"]:
        return False
    if not (near(a["entry_price"], b["entry_price"], 0.0005)
            and near(a["exit_price"], b["exit_price"], 0.0005)):
        return False
    if a.get("qty") and b.get("qty"):
        return near(a["qty"], b["qty"], 0.01)
    return True


# ---------- indicators on 4h bars

def rsi_series(closes: list[float], period: int = 14) -> list[float]:
    out = [50.0] * len(closes)
    gain = loss = 0.0
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        up, down = max(change, 0.0), max(-change, 0.0)
        if i <= period:
            gain += up / period
            loss += down / period
            if i < period:
                continue
        else:
            gain = (gain * (period - 1) + up) / period
            loss = (loss * (period - 1) + down) / period
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def atr_series(bars: list[Bar], period: int = strategy.ATR_PERIOD) -> list[float]:
    out = [0.0] * len(bars)
    value = 0.0
    for i in range(1, len(bars)):
        prev, bar = bars[i - 1], bars[i]
        item = max(bar.h - bar.low, abs(bar.h - prev.c), abs(bar.low - prev.c))
        if i <= period:
            value += item / period
        else:
            value = (value * (period - 1) + item) / period
        out[i] = value if i >= period else 0.0
    return out


class Series:
    """One symbol's 4h bars with every indicator the lab needs, computed once."""

    def __init__(self, bars: list[Bar]) -> None:
        self.bars = bars
        self.t = [b.t for b in bars]
        self.h = [b.h for b in bars]
        self.low = [b.low for b in bars]
        self.c = [b.c for b in bars]
        closes = self.c or [0.0]
        self.fast = strategy.ema(closes, strategy.FAST)
        self.slow = strategy.ema(closes, strategy.SLOW)
        self.long = strategy.ema(closes, strategy.COIN_TREND_PERIOD)
        self.regime = strategy.ema(closes, strategy.REGIME_PERIOD)
        self.rsi = rsi_series(self.c)
        self.atr = atr_series(bars)
        self.age = [0] * len(bars)  # bars since EMA 20 last crossed EMA 100
        for i in range(1, len(bars)):
            same = (self.fast[i] > self.slow[i]) == (self.fast[i - 1] > self.slow[i - 1])
            self.age[i] = min(self.age[i - 1] + 1, 500) if same else 0
        self.at = {t: i for i, t in enumerate(self.t)}

    def closed_before(self, ms: int) -> int:
        """Index of the last bar that had closed by `ms` (-1 if none)."""
        return bisect.bisect_right(self.t, ms - BAR_MS) - 1

    def holding(self, ms: int) -> int:
        """Index of the bar that contains `ms`."""
        return bisect.bisect_right(self.t, ms) - 1

    def btc_up(self, ms: int) -> bool | None:
        i = self.closed_before(ms)
        return None if i < WARMUP else self.c[i] > self.regime[i]


async def history(bars_until: BarsUntil, symbol: str, start_ms: int, end_ms: int) -> list[Bar]:
    """Closed 4h bars from start_ms to end_ms, paging back 1500 bars at a time."""
    out: list[Bar] = []
    end = end_ms
    for _ in range(MAX_PAGES):
        page = await bars_until(symbol, INTERVAL, end, 1500)
        page = [b for b in page if b.t < end and (not out or b.t < out[0].t)]
        if not page:
            break
        out = page + out
        if page[0].t <= start_ms:
            break
        end = page[0].t
    return [b for b in out if b.t + BAR_MS <= end_ms]


# ---------- per-trade market context

def context(trade: dict[str, Any], s: Series, btc: Series) -> dict[str, Any] | None:
    """The market around one trade, from bars that had closed before it opened. None when
    there is not enough history before the trade."""
    opened, closed = trade["opened_ms"], trade["closed_ms"]
    i = s.closed_before(opened)
    if i < WARMUP or not s.atr[i]:
        return None
    d = 1 if trade["side"] == "BUY" else -1
    close, atr = s.c[i], s.atr[i]
    atr_pct = atr / close
    j0, j1 = max(i + 1, s.holding(opened)), max(i + 1, s.holding(closed))
    if j0 >= len(s.t):
        return None
    high, low = max(s.h[j0:j1 + 1]), min(s.low[j0:j1 + 1])
    entry, exit_ = trade["entry_price"], trade["exit_price"]
    mfe = (high / entry - 1) if d == 1 else (1 - low / entry)
    mae = (1 - low / entry) if d == 1 else (high / entry - 1)
    ret = d * (exit_ / entry - 1)
    btc_up = btc.btc_up(opened)
    return {
        "side": trade["side"], "symbol": trade["symbol"], "opened_ms": opened,
        "win": (trade["pnl"] > 0) if trade.get("pnl") is not None else ret > 0,
        "ret": ret, "ret_atr": ret / atr_pct,
        "aligned": (s.fast[i] > s.slow[i]) == (d == 1),
        "rsi_side": s.rsi[i] if d == 1 else 100 - s.rsi[i],
        "dist_side": d * (close - s.fast[i]) / atr,
        "age": s.age[i],
        "atr_pct": atr_pct,
        "btc_with": None if btc_up is None else btc_up == (d == 1),
        "hour": datetime.fromtimestamp(opened / 1000, UTC).hour,
        "hold_bars": (closed - opened) / BAR_MS,
        "mfe_atr": max(mfe, 0.0) / atr_pct, "mae_atr": max(mae, 0.0) / atr_pct,
        "leverage": trade.get("leverage"),
    }


# ---------- style analysis

def describe(trades: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The trader's style in numbers plus plain-language findings (Arabic, for the app)."""
    wins = [r for r in rows if r["win"]]
    losses = [r for r in rows if not r["win"]]
    aligned = [r for r in rows if r["aligned"]]
    against = [r for r in rows if not r["aligned"]]
    btc_known = [r for r in rows if r["btc_with"] is not None]
    levs = [t["leverage"] for t in trades if t.get("leverage")]
    by_symbol = Counter(r["symbol"] for r in rows)
    hours = Counter(r["hour"] for r in rows)
    stats = {
        "trades": len(trades), "usable": len(rows),
        "win_rate": rnd(share([r["win"] for r in rows])),
        "long_share": rnd(share([r["side"] == "BUY" for r in rows])),
        "hold_hours_median": rnd(median([r["hold_bars"] * 4 for r in rows]), 1),
        "leverage_median": rnd(median(levs), 1) if levs else None,
        "leverage_max": max(levs) if levs else None,
        "symbols": [[sym, n, rnd(share([r["win"] for r in rows if r["symbol"] == sym]), 3)]
                    for sym, n in by_symbol.most_common(6)],
        "aligned_share": rnd(share([r["aligned"] for r in rows])),
        "win_rate_aligned": rnd(share([r["win"] for r in aligned])),
        "win_rate_against": rnd(share([r["win"] for r in against])),
        "btc_with_share": rnd(share([r["btc_with"] for r in btc_known])),
        "rsi_median": rnd(median([r["rsi_side"] for r in rows]), 1),
        "busy_hours_utc": [h for h, _ in hours.most_common(3)],
        "avg_win_pct": rnd(median([r["ret"] * 100 for r in wins]), 2),
        "avg_loss_pct": rnd(median([r["ret"] * 100 for r in losses]), 2),
        "winner_mae_atr_p80": rnd(pct([r["mae_atr"] for r in wins], 0.8), 2),
        "loser_mae_atr_median": rnd(median([r["mae_atr"] for r in losses]), 2),
        "fields_missing": sorted({key for t in trades for key in ("leverage", "qty", "pnl", "fee")
                                  if t.get(key) is None}),
    }
    notes: list[str] = []
    if stats["long_share"] is not None:
        notes.append(f"{stats['long_share'] * 100:.0f}% من صفقاته شراء و "
                     f"{(1 - stats['long_share']) * 100:.0f}% بيع.")
    if stats["aligned_share"] is not None:
        kind = "مع الاتجاه" if stats["aligned_share"] >= 0.6 else (
            "عكس الاتجاه" if stats["aligned_share"] <= 0.4 else "مع الاتجاه وعكسه")
        notes.append(f"يدخل غالبًا {kind} ({stats['aligned_share'] * 100:.0f}% مع اتجاه "
                     "المتوسطين 20/100).")
    if stats["hold_hours_median"] is not None:
        notes.append(f"متوسط مدة الصفقة {stats['hold_hours_median']:.0f} ساعة.")
    if stats["avg_win_pct"] is not None and stats["avg_loss_pct"] is not None:
        notes.append(f"الربح المعتاد {stats['avg_win_pct']:+.2f}% والخسارة المعتادة "
                     f"{stats['avg_loss_pct']:+.2f}% من السعر.")
    if stats["winner_mae_atr_p80"] is not None:
        notes.append(f"صفقاته الرابحة تحمّلت عكسها حتى {stats['winner_mae_atr_p80']:.1f} ATR "
                     "قبل أن تربح: وقفه الفعلي واسع بهذا القدر تقريبًا.")
    if stats["leverage_median"]:
        notes.append(f"الرافعة المعتادة ×{stats['leverage_median']:g} وأعلاها "
                     f"×{stats['leverage_max']:g}.")
    if stats["fields_missing"]:
        notes.append("بيانات غير متوفرة في السجل ولم تُخمَّن: " + "، ".join(stats["fields_missing"])
                     + ".")
    return {"stats": stats, "notes": notes}


# ---------- learning an independent rule set

def learn(rows: list[dict[str, Any]], trades: list[dict[str, Any]],
          q: float = 0.15) -> dict[str, Any]:
    """Rules from the trader's training trades. Context only from closed bars, so the same rules
    can be run on any market data later. A guess at patterns, not the trader's own method."""
    n = len(rows)
    sides = [s for s in ("BUY", "SELL")
             if sum(r["side"] == s for r in rows) >= max(3, 0.2 * n)]
    aligned = share([r["aligned"] for r in rows]) or 0.5
    trend = "with" if aligned >= 0.65 else "against" if aligned <= 0.35 else "any"
    btc_rule = {}
    for s in sides:
        known = [r["btc_with"] for r in rows if r["side"] == s and r["btc_with"] is not None]
        btc_rule[s] = bool(known) and (share(known) or 0) >= 0.75
    wins = [r for r in rows if r["win"]]
    stop_atr = clamp(pct([r["mae_atr"] for r in wins], 0.8) * 1.1, 0.5, 6.0) if wins else 2.5
    target_atr = clamp(median([r["ret_atr"] for r in wins]), 0.3, 10.0) if wins else 1.0
    symbols = [sym for sym, count in Counter(r["symbol"] for r in rows).most_common(10)
               if count >= 2] or sorted({r["symbol"] for r in rows})[:10]
    levs = [t["leverage"] for t in trades if t.get("leverage")]
    return {
        "sides": sides or ["BUY"],
        "trend": trend,
        "btc": btc_rule,
        "rsi": [rnd(pct([r["rsi_side"] for r in rows], q), 1),
                rnd(pct([r["rsi_side"] for r in rows], 1 - q), 1)],
        "dist": [rnd(pct([r["dist_side"] for r in rows], q), 2),
                 rnd(pct([r["dist_side"] for r in rows], 1 - q), 2)],
        "age": [math.floor(pct([r["age"] for r in rows], q)),
                math.ceil(pct([r["age"] for r in rows], 1 - q))],
        "stop_atr": rnd(stop_atr, 2),
        "target_atr": rnd(target_atr, 2),
        "max_hold_bars": int(clamp(math.ceil(pct([r["hold_bars"] for r in rows], 0.9)), 1, 240)),
        "symbols": symbols,
        "leverage": clamp(median(levs) if levs else 3, 1, 5),
        "trained_on": n,
    }


def fit(rows: list[dict[str, Any]], trades: list[dict[str, Any]], series: dict[str, Series],
        btc: Series, start_ms: int, end_ms: int) -> dict[str, Any]:
    """Tune the learned rules on the TRAINING period only:
    1. entry band width: the one whose number of signals is closest to the trader's own number
       of trades (a rule set that trades far more often is not the same style);
    2. exits: the stop / target / holding time that left the most profit after fees, among a
       small grid that includes the trader-like exits. A high win rate alone is not enough:
       a tiny target with a wide stop wins often and still loses money."""
    goal = len(rows)
    best, best_gap = None, math.inf
    for q in (0.1, 0.2, 0.3):
        model = learn(rows, trades, q)
        count = simulate("learned", model, series, btc, start_ms, end_ms)["trades"]
        gap = abs(math.log((count + 1) / (goal + 1)))
        if gap < best_gap:
            best, best_gap, signals, best_q = model, gap, count, q
    assert best is not None
    described = {k: best[k] for k in ("stop_atr", "target_atr", "max_hold_bars")}
    holds = sorted({best["max_hold_bars"],
                    int(clamp(math.ceil(median([r["hold_bars"] for r in rows])), 1, 240))})
    candidates = [described] + [
        {"stop_atr": stop, "target_atr": target, "max_hold_bars": hold}
        for stop in (1.0, 1.5, 2.0, 2.5, 3.0) for target in (0.5, 1.0, 1.5, 2.0, 3.0)
        for hold in holds
    ]
    scored = []
    for exits in candidates:
        result = simulate("learned", {**best, **exits}, series, btc, start_ms, end_ms)
        scored.append((result["net_pnl"] or 0, result["win_rate"] or 0, exits, result))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _, _, exits, result = scored[0]
    return {**best, **exits, "described_exits": described, "fit": {
        "band_q": best_q,
        "train_signals": signals, "trader_train_trades": goal,
        "train_net_pnl": result["net_pnl"], "train_win_rate": result["win_rate"],
    }}


def setup(model: dict[str, Any], s: Series, btc: Series, i: int, side: str) -> bool:
    if i < WARMUP or not s.atr[i]:
        return False
    d = 1 if side == "BUY" else -1
    up = s.fast[i] > s.slow[i]
    if model["trend"] == "with" and up != (d == 1):
        return False
    if model["trend"] == "against" and up == (d == 1):
        return False
    if model["btc"].get(side):
        btc_up = btc.btc_up(s.t[i] + BAR_MS)
        if btc_up is None or btc_up != (d == 1):
            return False
    rsi = s.rsi[i] if d == 1 else 100 - s.rsi[i]
    dist = d * (s.c[i] - s.fast[i]) / s.atr[i]
    age = model.get("age") or [0, 10**9]
    return (model["rsi"][0] <= rsi <= model["rsi"][1]
            and model["dist"][0] <= dist <= model["dist"][1]
            and age[0] <= s.age[i] <= age[1])


def model_signal(model: dict[str, Any], s: Series, btc: Series, i: int) -> str | None:
    """A side when its setup appears on bar i (true now, not on the bar before)."""
    for side in model["sides"]:
        if setup(model, s, btc, i, side) and not setup(model, s, btc, i - 1, side):
            return side
    return None


# ---------- one simulator for every rule set (learned or Sonic's), same fees and sizing

def simulate(kind: str, model: dict[str, Any] | None, series: dict[str, Series], btc: Series,
             start_ms: int, end_ms: int) -> dict[str, Any]:
    """$30, 1% risk per trade, at most 3 open trades, fees and slippage on both sides.
    kind "learned": stop/target/max hold from the model. kind "sonic": Sonic's live rules
    (EMA 20/100 cross, BTC above EMA 300, coin above EMA 200, stop 2.5 ATR, half profit at
    0.20R then stop to entry, exit on cross back or after 240 bars). Within a bar the stop is
    assumed to come first (conservative)."""
    equity = peak = CAPITAL
    max_dd = 0.0
    trades: list[dict[str, Any]] = []
    open_: dict[str, dict[str, Any]] = {}
    times = sorted({t for s in series.values() for t in s.t if start_ms <= t < end_ms})
    leverage = (model or {}).get("leverage", 3)

    def close(sym: str, pos: dict[str, Any], price: float, qty: float, t: int, why: str) -> None:
        nonlocal equity
        pnl = pos["d"] * (price - pos["entry"]) * qty - (pos["entry"] + price) * qty * (FEE + SLIP)
        equity += pnl
        pos["pnl"] += pnl
        pos["left"] -= qty
        if pos["left"] <= EPS:
            trades.append({"symbol": sym, "side": "BUY" if pos["d"] == 1 else "SELL",
                           "opened_ms": pos["opened"], "closed_ms": t + BAR_MS,
                           "pnl": pos["pnl"], "why": why,
                           "share": pos["pnl"] / pos["equity"]})  # of the amount at entry
            del open_[sym]

    for t in times:
        for sym, s in series.items():
            j = s.at.get(t)
            pos = open_.get(sym)
            if j is None or pos is None or j <= pos["i"]:
                continue
            d, hi, lo = pos["d"], s.h[j], s.low[j]
            hit_stop = lo <= pos["stop"] if d == 1 else hi >= pos["stop"]
            hit_target = hi >= pos["target"] if d == 1 else lo <= pos["target"]
            if hit_stop:
                close(sym, pos, pos["stop"], pos["left"], t, "stop")
            elif kind == "learned":
                if hit_target:
                    close(sym, pos, pos["target"], pos["left"], t, "target")
                elif j - pos["i"] >= model["max_hold_bars"]:
                    close(sym, pos, s.c[j], pos["left"], t, "time")
            else:
                if hit_target and not pos["scaled"]:
                    pos["scaled"] = True
                    close(sym, pos, pos["target"], pos["left"] / 2, t, "half")
                    pos["stop"] = pos["entry"]
                turned = s.fast[j] < s.slow[j]
                if sym in open_ and (turned or j - pos["i"] >= strategy.MAX_HOLD_BARS):
                    close(sym, pos, s.c[j], pos["left"], t, "exit")
        for sym, s in series.items():
            i = s.at.get(t)
            if i is None or sym in open_ or len(open_) >= MAX_OPEN or i < WARMUP or not s.atr[i]:
                continue
            if kind == "learned":
                side = model_signal(model, s, btc, i)
                stop_dist = model["stop_atr"] * s.atr[i]
                target_dist = model["target_atr"] * s.atr[i]
            else:
                crossed = s.fast[i] > s.slow[i] and s.fast[i - 1] <= s.slow[i - 1]
                side = "BUY" if crossed and btc.btc_up(t + BAR_MS) and s.c[i] > s.long[i] else None
                stop_dist = strategy.ATR_K * s.atr[i]
                target_dist = strategy.HALF_AT_R * stop_dist
            if side is None or stop_dist <= 0:
                continue
            d = 1 if side == "BUY" else -1
            entry = s.c[i]
            qty = min(equity * RISK / stop_dist, equity * leverage / entry)
            if qty <= 0:
                continue
            open_[sym] = {"d": d, "entry": entry, "stop": entry - d * stop_dist,
                          "target": entry + d * target_dist, "i": i, "opened": t + BAR_MS,
                          "left": qty, "scaled": False, "pnl": 0.0, "equity": equity}
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
    wins = sum(1 for tr in trades if tr["pnl"] > 0)
    gross_win = sum(tr["pnl"] for tr in trades if tr["pnl"] > 0)
    gross_loss = -sum(tr["pnl"] for tr in trades if tr["pnl"] < 0)
    months: dict[str, float] = {}
    for tr in trades:
        key = datetime.fromtimestamp(tr["closed_ms"] / 1000, UTC).strftime("%Y-%m")
        months[key] = months.get(key, 0.0) + tr["pnl"]
    days = max((end_ms - start_ms) / 86_400_000, 1)
    return {
        "trades": len(trades), "wins": wins,
        "win_rate": rnd(wins / len(trades)) if trades else None,
        "net_pnl": rnd(equity - CAPITAL, 2),
        "net_pct": rnd(equity / CAPITAL - 1),
        "yearly_pct": rnd((equity / CAPITAL) ** (365 / days) - 1) if equity > 0 else -1.0,
        "max_drawdown": rnd(max_dd),
        "profit_factor": rnd(min(gross_win / gross_loss, 10.0), 2) if gross_loss
        else (10.0 if gross_win else None),
        "months": len(months),
        "months_positive": rnd(sum(v > 0 for v in months.values()) / len(months)) if months
        else None,
        "worst_trade": rnd(min((tr["share"] for tr in trades), default=0.0)),
        "days": round(days),
        "open_at_end": len(open_),
    }


def actual(trades: list[dict[str, Any]], start_ms: int, end_ms: int) -> dict[str, Any]:
    """The trader's own results in a window, from the imported data only."""
    window = [t for t in trades if start_ms <= t["opened_ms"] < end_ms]
    known = [t for t in window if t.get("pnl") is not None]
    wins = [((t["pnl"] > 0) if t.get("pnl") is not None else
             (t["exit_price"] - t["entry_price"]) * (1 if t["side"] == "BUY" else -1) > 0)
            for t in window]
    return {
        "trades": len(window),
        "win_rate": rnd(share(wins)),
        "pnl_reported": rnd(sum(t["pnl"] for t in known), 2) if known else None,
        "pnl_missing": len(window) - len(known),
    }


# ---------- ranking every saved strategy (out-of-sample results only)

CHAMPION_PAPER_TRADES = 20  # paper trades a coin's best strategy needs before any judgement
COMMON_MIN_DAYS = 60  # shorter overlap: each strategy is judged on its own test period
LIMITS = {  # a strategy outside any of these is left out of the choice (one coin)
    "min_trades": 10, "min_days": 60, "min_months": 3,
    "max_drawdown": 0.20, "worst_trade": -0.03, "min_profit_factor": 1.2,
    "min_months_positive": 0.5,
}
WEIGHTS = {"profit": 0.25, "profit_factor": 0.20, "stability": 0.20, "drawdown": 0.20,
           "win_rate": 0.10, "risk": 0.05}


def exclusion(result: dict[str, Any] | None) -> str | None:
    """Why a tested strategy cannot be chosen (None = eligible). Arabic, for the app."""
    if not result or result.get("profit_factor", "missing") == "missing":
        return "اختبار قديم بلا تفاصيل كافية: أعد تحليل المتداول"
    if result["trades"] < LIMITS["min_trades"] or result.get("days", 0) < LIMITS["min_days"] \
            or (result.get("months") or 0) < LIMITS["min_months"]:
        return (f"بيانات غير كافية ({result['trades']} صفقة في {result.get('days', 0)} يوم؛ "
                f"المطلوب {LIMITS['min_trades']} صفقة و {LIMITS['min_days']} يومًا)")
    if (result["net_pnl"] or 0) <= 0:
        return f"خاسرة بعد الرسوم ({result['net_pnl']:+.2f}$)"
    if (result["max_drawdown"] or 0) > LIMITS["max_drawdown"]:
        return f"تراجع كبير ({result['max_drawdown'] * 100:.0f}%)"
    if (result["worst_trade"] or 0) < LIMITS["worst_trade"]:
        return f"خسارة كبيرة في صفقة واحدة ({result['worst_trade'] * 100:.1f}% من المبلغ)"
    if (result["profit_factor"] or 0) < LIMITS["min_profit_factor"]:
        return f"معامل ربح ضعيف ({result['profit_factor']})"
    if (result["months_positive"] or 0) < LIMITS["min_months_positive"]:
        return f"أرباح غير مستقرة ({result['months_positive'] * 100:.0f}% فقط من الأشهر رابحة)"
    return None


def score(result: dict[str, Any]) -> tuple[float, dict[str, float]]:
    """0-100. Sustainable profit and risk control weigh far more than the win rate alone."""
    parts = {
        "profit": clamp((result["yearly_pct"] or 0) / 0.5, 0, 1),
        "profit_factor": clamp(((result["profit_factor"] or 1) - 1) / 1.5, 0, 1),
        "stability": result["months_positive"] or 0,
        "drawdown": clamp(1 - (result["max_drawdown"] or 0) / LIMITS["max_drawdown"], 0, 1),
        "win_rate": result["win_rate"] or 0,
        "risk": clamp(1 - (result["worst_trade"] or 0) / LIMITS["worst_trade"], 0, 1),
    }
    return round(100 * sum(WEIGHTS[k] * v for k, v in parts.items()), 1), parts


def reasons(best: dict[str, Any], runner: dict[str, Any] | None) -> list[str]:
    r = best["result"]
    out = [
        f"ربح بعد الرسوم {r['yearly_pct'] * 100:+.0f}% سنويًا في فترة لم يتعلم منها "
        f"({r['days']} يوم، {r['trades']} صفقة).",
        f"معامل الربح {r['profit_factor']}: كل دولار خسارة قابله {r['profit_factor']} دولار ربح.",
        f"{r['months_positive'] * 100:.0f}% من الأشهر رابحة، وأقصى تراجع "
        f"{r['max_drawdown'] * 100:.0f}%، وأسوأ صفقة {r['worst_trade'] * 100:.1f}% من المبلغ.",
        f"نسبة الفوز {r['win_rate'] * 100:.0f}% (وزنها 10% فقط من التقييم).",
    ]
    if runner:
        ahead = [label for key, label in (("profit", "الربح"), ("profit_factor", "معامل الربح"),
                                          ("stability", "الاستقرار"), ("drawdown", "التراجع"),
                                          ("risk", "إدارة المخاطر"))
                 if best["parts"][key] > runner["parts"][key]]
        if ahead:
            out.append(f"تفوقت على الثانية ({runner['name']}) في: " + "، ".join(ahead) + ".")
    return out


def rank(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """entries: {trader_id, name, status, model_id, result}. Scores the eligible ones, lists the
    rest with the reason, and picks the best (alone or against others)."""
    ranked, excluded = [], []
    for entry in entries:
        why = exclusion(entry["result"])
        if why:
            excluded.append({**entry, "why": why})
            continue
        value, parts = score(entry["result"])
        rounded = {k: round(v, 3) for k, v in parts.items()}
        ranked.append({**entry, "score": value, "parts": rounded})
    ranked.sort(key=lambda e: (e["score"], e["result"]["yearly_pct"] or 0), reverse=True)
    best = None
    if ranked:
        top = ranked[0]
        best = {"trader_id": top["trader_id"], "model_id": top["model_id"], "name": top["name"],
                "score": top["score"],
                "reasons": reasons(top, ranked[1] if len(ranked) > 1 else None)}
    return {"ranked": ranked, "excluded": excluded, "best": best}

# ---------- the lab: traders, data, models, paper trading

class Lab:
    def __init__(self, db: sqlite3.Connection, now_ms: Callable[[], int]) -> None:
        self.db = db
        self.now_ms = now_ms
        self.db.executescript(SCHEMA)
        self.db.commit()

    # traders
    def add(self, ref: str, name: str = "", kind: str = "lead") -> dict[str, Any]:
        trader_id = trader_ref(ref)
        if trader_id is None:
            raise ValueError("invalid_trader")
        url = ref.strip() if ref.strip().startswith("http") else None
        now = self.now_ms()
        row = self.trader(trader_id)
        if row is None:
            self.db.execute(
                "INSERT INTO traders VALUES (?, ?, ?, ?, 'watching', ?, ?)",
                (trader_id, (name or trader_id)[:60], url, kind, now, now),
            )
        else:  # adding again brings back a paused or deleted trader, data and models intact
            self.db.execute(
                "UPDATE traders SET status = 'watching', name = COALESCE(NULLIF(?, ''), name), "
                "url = COALESCE(?, url), changed_ms = ? WHERE trader_id = ?",
                (name[:60], url, now, trader_id),
            )
        self.db.commit()
        return self.trader(trader_id) or {}

    def set_status(self, trader_id: str, status: str) -> dict[str, Any]:
        if status not in ("watching", "paused"):
            raise ValueError("invalid_status")
        if self.listed(trader_id) is None:
            raise KeyError("unknown_trader")
        self.db.execute("UPDATE traders SET status = ?, changed_ms = ? WHERE trader_id = ?",
                        (status, self.now_ms(), trader_id))
        self.db.commit()
        return self.trader(trader_id) or {}

    def listed(self, trader_id: str) -> dict[str, Any] | None:
        """The trader if it is on the owner's list (not deleted)."""
        row = self.trader(trader_id)
        return row if row and row["status"] != "removed" else None

    def trader(self, trader_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM traders WHERE trader_id = ?", (trader_id,)).fetchone()
        return dict(row) if row else None

    def delete(self, trader_id: str) -> dict[str, Any]:
        """Off the list and no longer watched. Its trades, strategies and test results are kept
        (they stay in the ranking); adding the trader again brings it back."""
        if self.listed(trader_id) is None:
            raise KeyError("unknown_trader")
        self.db.execute("UPDATE traders SET status = 'removed', changed_ms = ? WHERE trader_id = ?",
                        (self.now_ms(), trader_id))
        self.db.commit()
        return self.trader(trader_id) or {}

    def traders(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM trader_trades x WHERE x.trader_id = t.trader_id) "
            "AS trades, (SELECT COUNT(*) FROM trader_drafts d WHERE d.trader_id = t.trader_id) "
            "AS drafts FROM traders t ORDER BY added_ms"
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            if item["status"] == "removed":
                continue
            latest = self.latest_model(item["trader_id"])
            item["model"] = None if latest is None else {
                "id": latest["id"], "created_ms": latest["created_ms"],
                "learned": latest["model"] is not None,
                "test": latest["report"].get("test"),
            }
            item["paper"] = self.paper_summary(item["trader_id"])
            out.append(item)
        return out

    # data
    def import_trades(self, trader_id: str, trades: list[dict[str, Any]], source: str) -> int:
        """Complete trades. One already saved from another source (same position within a
        minute) is not saved twice; a matching draft is completed by it: fields missing on one
        side are taken from the other, both being real records, never guessed."""
        if self.listed(trader_id) is None:
            raise KeyError("unknown_trader")
        now, added = self.now_ms(), 0
        for t in trades:
            known = [dict(r) for r in self.db.execute(
                "SELECT * FROM trader_trades WHERE trader_id = ? AND symbol = ? AND side = ? AND "
                "ABS(opened_ms - ?) <= 60000", (trader_id, t["symbol"], t["side"], t["opened_ms"]))]
            if any(same_trade(t, k) for k in known):
                continue
            for draft in self.drafts(trader_id):
                if same_trade(t, draft):
                    t = {**t, **{k: draft[k] for k in ("leverage", "qty", "pnl", "fee")
                                 if t.get(k) is None and draft.get(k) is not None}}
                    self.db.execute("DELETE FROM trader_drafts WHERE id = ?", (draft["id"],))
                    break
            cur = self.db.execute(
                "INSERT OR IGNORE INTO trader_trades (trader_id, symbol, side, leverage, qty, "
                "entry_price, exit_price, opened_ms, closed_ms, pnl, fee, source, imported_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trader_id, t["symbol"], t["side"], t.get("leverage"), t.get("qty"),
                 t["entry_price"], t["exit_price"], t["opened_ms"], t["closed_ms"],
                 t.get("pnl"), t.get("fee"), source, now),
            )
            added += cur.rowcount
        self.db.commit()
        return added

    def save_drafts(self, trader_id: str, drafts: list[dict[str, Any]]) -> int:
        """Rows with prices but without times: kept as they are, never used for learning."""
        if self.listed(trader_id) is None:
            raise KeyError("unknown_trader")
        added = 0
        for d in drafts:
            if any(same_trade(d, x) for x in self.drafts(trader_id) + self.trades(trader_id)):
                continue  # already a draft, or already complete
            self.db.execute(
                "INSERT INTO trader_drafts (trader_id, symbol, side, leverage, qty, entry_price, "
                "exit_price, opened_ms, closed_ms, pnl, fee, missing, imported_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trader_id, d["symbol"], d["side"], d.get("leverage"), d.get("qty"),
                 d["entry_price"], d["exit_price"], d.get("opened_ms"), d.get("closed_ms"),
                 d.get("pnl"), d.get("fee"), json.dumps(d["missing"]), self.now_ms()))
            added += 1
        self.db.commit()
        return added

    def drafts(self, trader_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM trader_drafts WHERE trader_id = ? ORDER BY id",
                               (trader_id,))
        return [{**dict(r), "missing": json.loads(r["missing"])} for r in rows]

    def complete_draft(self, trader_id: str, draft_id: int, opened_ms: int | None,
                       closed_ms: int | None, fee: float | None) -> dict[str, Any]:
        """The owner adds the real times (and the fee if known): the draft becomes a trade."""
        row = self.db.execute("SELECT * FROM trader_drafts WHERE id = ? AND trader_id = ?",
                              (draft_id, trader_id)).fetchone()
        if row is None:
            raise KeyError("unknown_draft")
        opened = opened_ms if opened_ms is not None else row["opened_ms"]
        closed = closed_ms if closed_ms is not None else row["closed_ms"]
        if opened is None or closed is None or closed < opened:
            raise ValueError("draft_times")
        trade = {k: row[k] for k in ("symbol", "side", "leverage", "qty", "entry_price",
                                     "exit_price", "pnl")}
        trade.update(opened_ms=opened, closed_ms=closed, fee=fee if fee is not None else row["fee"])
        added = self.import_trades(trader_id, [trade], "draft")
        self.db.execute("DELETE FROM trader_drafts WHERE id = ?", (draft_id,))
        self.db.commit()
        return {"added": added}

    def drop_completed_drafts(self, trader_id: str, trades: list[dict[str, Any]]) -> int:
        """Drafts that a full record of the same position now covers (any source)."""
        removed = 0
        for draft in self.drafts(trader_id):
            if any(same_trade(t, draft) for t in trades + self.trades(trader_id)):
                self.db.execute("DELETE FROM trader_drafts WHERE id = ?", (draft["id"],))
                removed += 1
        self.db.commit()
        return removed

    def needs_study(self, trader_id: str) -> bool:
        """At least MIN_TRADES complete trades, and more of them than the last study saw."""
        count = len(self.trades(trader_id))
        if count < MIN_TRADES:
            return False
        latest = self.latest_model(trader_id)
        seen = (latest or {}).get("report", {}).get("data", {}).get("trades", 0) if latest else 0
        return count > seen

    def move_trades(self, from_id: str, to_id: str, start_ms: int, end_ms: int) -> int:
        """The owner says which trader some synced trades came from (never guessed)."""
        if self.listed(to_id) is None:
            raise KeyError("unknown_trader")
        cur = self.db.execute(
            "UPDATE OR IGNORE trader_trades SET trader_id = ? WHERE trader_id = ? "
            "AND opened_ms >= ? AND opened_ms < ?", (to_id, from_id, start_ms, end_ms))
        self.db.commit()
        return cur.rowcount

    def trades(self, trader_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM trader_trades WHERE trader_id = ? ORDER BY opened_ms", (trader_id,))
        return [dict(row) for row in rows]

    def latest_model(self, trader_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM trader_models WHERE trader_id = ? ORDER BY id DESC LIMIT 1",
            (trader_id,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "created_ms": row["created_ms"],
                "model": json.loads(row["model"]) if row["model"] else None,
                "report": json.loads(row["report"])}

    def models(self, trader_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id, created_ms, model IS NOT NULL AS learned FROM trader_models "
            "WHERE trader_id = ? ORDER BY id DESC", (trader_id,))
        return [dict(row) for row in rows]

    # learning
    async def study(self, trader_id: str, bars_until: BarsUntil) -> dict[str, Any]:
        """Analyse, learn on the first 70%, test on the rest, compare, save a new version."""
        trades = self.trades(trader_id)
        if not trades:
            raise ValueError("no_trades")
        now = self.now_ms()
        first, last = trades[0]["opened_ms"], max(t["closed_ms"] for t in trades)
        start = first - (WARMUP + 10) * BAR_MS
        cache: dict[str, Series] = {}

        async def series_of(symbol: str, begin: int, end: int) -> Series:
            if symbol not in cache:
                cache[symbol] = Series(await history(bars_until, symbol, begin, end))
            return cache[symbol]

        btc = await series_of("BTCUSDT", start, now)
        rows = []
        for t in trades:
            s = await series_of(t["symbol"], start, min(now, last + BAR_MS * 2))
            row = context(t, s, btc)
            if row is not None:
                rows.append(row)
        report: dict[str, Any] = {
            "trader_id": trader_id, "made_ms": now, **describe(trades, rows),
            "data": {"from_ms": first, "to_ms": last, "trades": len(trades),
                     "usable": len(rows)},
            "warning": "القواعد المستنتجة تخمين لأنماط في نتائجه، وليست استراتيجيته الحقيقية.",
        }
        model = None
        if len(rows) < MIN_TRADES:
            report["learn"] = {"status": "not_enough_trades", "need": MIN_TRADES}
        else:
            rows.sort(key=lambda r: r["opened_ms"])
            cut = int(len(rows) * TRAIN_SHARE)
            split = rows[cut]["opened_ms"]
            draft = learn(rows[:cut], trades)
            test_end = min(now, max(last, split + 30 * 86_400_000))
            for symbol in draft["symbols"]:
                await series_of(symbol, start, test_end)
            learned_series = {sym: cache[sym] for sym in draft["symbols"] if cache[sym].t}
            model = fit(rows[:cut], trades, learned_series, btc, first, split)
            sonic_start = split - (WARMUP + 10) * BAR_MS
            sonic_series = {}
            for symbol in SONIC_SYMBOLS:
                s = cache.get(symbol) or await series_of(symbol, sonic_start, test_end)
                if s.t:
                    sonic_series[symbol] = s
            report["learn"] = {"status": "ok", "train_trades": cut, "test_trades": len(rows) - cut,
                               "split_ms": split, "test_end_ms": test_end}
            report["train"] = {"learned": simulate("learned", model, learned_series, btc,
                                                   first, split)}
            report["test"] = {
                "from_ms": split, "to_ms": test_end,
                "days": round((test_end - split) / 86_400_000),
                "learned": simulate("learned", model, learned_series, btc, split, test_end),
                "sonic": simulate("sonic", None, sonic_series, btc, split, test_end),
                "trader": actual(trades, split, test_end),
            }
            if report["test"]["days"] < 30:
                report["notes"].append("فترة الاختبار أقل من شهر: النتائج غير موثوقة بعد.")
        if self.listed(trader_id) is None:
            # deleted while this study was running: keep nothing of it
            raise KeyError("unknown_trader")
        cur = self.db.execute(
            "INSERT INTO trader_models (trader_id, created_ms, model, report) VALUES (?, ?, ?, ?)",
            (trader_id, now, json.dumps(model) if model else None, json.dumps(report)),
        )
        self.db.commit()
        report["model_id"] = cur.lastrowid
        report["model"] = model
        return report

    # ranking and the chosen strategy
    def state(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM lab_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put_state(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO lab_state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                        "value = excluded.value", (key, json.dumps(value)))
        self.db.commit()

    def strategies(self) -> list[dict[str, Any]]:
        """The latest learned version of every trader's strategy, deleted traders included."""
        out = []
        for row in self.db.execute(
            "SELECT m.id, m.trader_id, m.created_ms, m.model, m.report, t.name, t.status "
            "FROM trader_models m JOIN traders t ON t.trader_id = m.trader_id "
            "WHERE m.model IS NOT NULL AND m.id = (SELECT MAX(id) FROM trader_models x "
            "WHERE x.trader_id = m.trader_id AND x.model IS NOT NULL)"
        ).fetchall():
            report = json.loads(row["report"])
            test = report.get("test") or {}
            out.append({"trader_id": row["trader_id"], "name": row["name"],
                        "status": row["status"], "model_id": row["id"],
                        "created_ms": row["created_ms"], "model": json.loads(row["model"]),
                        "from_ms": test.get("from_ms"), "to_ms": test.get("to_ms")})
        return out

    async def compare_coins(self, bars_until: BarsUntil) -> dict[str, Any]:
        """Run every saved strategy again on each of its coins, out of sample, and pick the best
        per coin. Strategies sharing a coin are compared over their common test period."""
        strategies = [e for e in self.strategies() if e["from_ms"] and e["to_ms"]]
        coins: dict[str, list[dict[str, Any]]] = {}
        for entry in strategies:
            for symbol in entry["model"]["symbols"]:
                coins.setdefault(symbol, []).append(entry)
        cache: dict[str, Series] = {}
        earliest = min((e["from_ms"] for e in strategies), default=self.now_ms())
        start = earliest - (WARMUP + 10) * BAR_MS

        async def series_of(symbol: str) -> Series:
            if symbol not in cache:
                cache[symbol] = Series(await history(bars_until, symbol, start, self.now_ms()))
            return cache[symbol]

        btc = await series_of("BTCUSDT") if strategies else None
        result: dict[str, Any] = {}
        for symbol, entries in sorted(coins.items()):
            s = await series_of(symbol)
            if not s.t or btc is None:
                continue
            common_from = max(e["from_ms"] for e in entries)
            common_to = min(e["to_ms"] for e in entries)
            same = len(entries) > 1 and common_to - common_from >= COMMON_MIN_DAYS * 86_400_000
            rows = []
            for entry in entries:
                begin, end = ((common_from, common_to) if same
                              else (entry["from_ms"], entry["to_ms"]))
                outcome = simulate("learned", entry["model"], {symbol: s}, btc, begin, end)
                rows.append({k: entry[k] for k in ("trader_id", "name", "status", "model_id")}
                            | {"result": outcome, "from_ms": begin, "to_ms": end})
            ranked = rank(rows)
            result[symbol] = {
                "symbol": symbol, "strategies": len(entries),
                "same_period": same, "from_ms": common_from if same else None,
                "to_ms": common_to if same else None, **ranked,
            }
        state = {"made_ms": self.now_ms(), "coins": result}
        self.put_state("coins", state)
        return state

    def ranking(self, approved: dict[str, int] | None = None) -> dict[str, Any]:
        """The last per-coin comparison, with each coin's paper track. `approved` maps a coin to
        the model the owner approved for it: a better one is only flagged, never swapped in."""
        state = self.state("coins") or {"made_ms": None, "coins": {}}
        for symbol, coin in state["coins"].items():
            best = coin.get("best")
            if best:
                paper = self.paper_summary(best["model_id"], table="champion_paper", by="model_id",
                                           symbol=symbol)
                best["paper"] = {**paper, "needed": CHAMPION_PAPER_TRADES}
            mine = (approved or {}).get(symbol)
            coin["approved_model"] = mine
            coin["better_than_approved"] = bool(best and mine and best["model_id"] != mine)
        state["total"] = len(self.strategies())
        state["limits"], state["weights"] = LIMITS, WEIGHTS
        return state

    # independent paper trading of the latest models
    async def paper_tick(self, bars: Callable[[str], Awaitable[list[Bar]]]) -> None:
        """Once per closed 4h bar: close paper trades that hit stop, target or time, then open
        new ones for watched traders whose model gives a signal on the last closed bar."""
        btc = Series(await bars("BTCUSDT"))
        cache: dict[str, Series] = {}
        open_rows = [(table, row) for table in ("trader_paper", "champion_paper")
                     for row in self.db.execute(f"SELECT * FROM {table} WHERE closed_ms IS NULL")]
        for table, row in open_rows:
            s = cache.get(row["symbol"]) or Series(await bars(row["symbol"]))
            cache[row["symbol"]] = s
            d = 1 if row["side"] == "BUY" else -1
            for j in range(len(s.t)):
                if s.t[j] < row["opened_ms"]:
                    continue
                hi, lo = s.h[j], s.low[j]
                price, why = None, ""
                if (lo <= row["stop_price"]) if d == 1 else (hi >= row["stop_price"]):
                    price, why = row["stop_price"], "stop"
                elif (hi >= row["target_price"]) if d == 1 else (lo <= row["target_price"]):
                    price, why = row["target_price"], "target"
                elif s.t[j] + BAR_MS >= row["until_ms"]:
                    price, why = s.c[j], "time"
                if price is not None:
                    ret = d * (price / row["entry_price"] - 1) - COST
                    self.db.execute(
                        f"UPDATE {table} SET closed_ms = ?, exit_price = ?, ret = ?, reason = ?"
                        " WHERE id = ?", (s.t[j] + BAR_MS, price, ret, why, row["id"]))
                    break
        tracks = []
        for trader in self.traders():
            latest = self.latest_model(trader["trader_id"])
            if trader["status"] == "watching" and latest and latest["model"]:
                tracks.append(("trader_paper", trader["trader_id"], latest["id"], latest["model"]))
        for symbol, coin in ((self.state("coins") or {}).get("coins") or {}).items():
            best = coin.get("best")
            if not best:
                continue
            row = self.db.execute("SELECT model FROM trader_models WHERE id = ?",
                                  (best["model_id"],)).fetchone()
            if row and row["model"]:
                model = {**json.loads(row["model"]), "symbols": [symbol]}
                tracks.append(("champion_paper", best["trader_id"], best["model_id"], model))
        for table, trader_id, model_id, model in tracks:
            for symbol in model["symbols"]:
                busy = self.db.execute(
                    f"SELECT 1 FROM {table} WHERE model_id = ? AND symbol = ? AND "
                    "closed_ms IS NULL", (model_id, symbol)).fetchone()
                if busy:
                    continue
                s = cache.get(symbol) or Series(await bars(symbol))
                cache[symbol] = s
                i = len(s.t) - 1
                side = model_signal(model, s, btc, i) if i > WARMUP else None
                if side is None:
                    continue
                d = 1 if side == "BUY" else -1
                atr, entry = s.atr[i], s.c[i]
                self.db.execute(
                    f"INSERT INTO {table} (trader_id, model_id, symbol, side, entry_price, "
                    "stop_price, target_price, opened_ms, until_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                    (trader_id, model_id, symbol, side, entry,
                     entry - d * model["stop_atr"] * atr, entry + d * model["target_atr"] * atr,
                     s.t[i] + BAR_MS, s.t[i] + BAR_MS + model["max_hold_bars"] * BAR_MS))
        self.db.commit()

    def paper_summary(self, key: Any, table: str = "trader_paper", by: str = "trader_id",
                      symbol: str | None = None) -> dict[str, Any]:
        rows = self.db.execute(
            f"SELECT * FROM {table} WHERE {by} = ? AND (? IS NULL OR symbol = ?) "
            "ORDER BY opened_ms", (key, symbol, symbol)).fetchall()
        closed = [r for r in rows if r["closed_ms"] is not None]
        equity = peak = CAPITAL
        max_dd = 0.0
        for r in closed:  # 1% of the amount risked per trade, as in the tests
            stop_pct = abs(r["entry_price"] - r["stop_price"]) / r["entry_price"]
            equity *= 1 + RISK * (r["ret"] / stop_pct if stop_pct else 0)
            peak = max(peak, equity)
            max_dd = max(max_dd, 1 - equity / peak)
        wins = sum(1 for r in closed if r["ret"] > 0)
        gain = sum(r["ret"] for r in closed if r["ret"] > 0)
        loss = -sum(r["ret"] for r in closed if r["ret"] < 0)
        return {
            "open": len(rows) - len(closed), "trades": len(closed),
            "win_rate": rnd(wins / len(closed)) if closed else None,
            "net_pnl": rnd(equity - CAPITAL, 2), "max_drawdown": rnd(max_dd),
            "profit_factor": rnd(min(gain / loss, 10.0), 2) if loss else (10.0 if gain else None),
        }
