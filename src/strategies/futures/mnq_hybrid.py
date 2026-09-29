"""MNQ hybrid scalp: an exact simulator for the "Trader MNQ" strategy.

Source: an IQ Capital video, reverse-engineered by its presenter, with the
timestamps cited next to each rule below. Two unrelated modules on 5-minute
Micro Nasdaq futures:

    LONG   A limit buy follows the lower ATR band, set at the previous close
           minus 3.1 x ATR(14), and is re-placed every candle (8:33).
           Exits: +2.0 ATR target, -1.5 ATR stop, using the PREVIOUS candle's
           ATR and fixed at entry (10:01-10:28).
    SHORT  A shooting-star candle (upper wick >= 2x body, lower wick <= 1/10 of
           the upper wick, either colour; 17:35-18:24). Sell at the next open
           and buy back at the open after that: a single 5-minute candle,
           "no matter what" (19:18-19:48). No target and no stop.
    BOTH   Never trade the opening candle (11:14). Size so a stop-out loses at
           most 0.5% of the STARTING balance (14:55).

WHY A DEDICATED SIMULATOR, NOT src/backtest/engine.py
-----------------------------------------------------
Both engines in this repo enter at the NEXT candle's open and charge fees as a
share of notional. This strategy's long module IS a resting limit order that
fills inside the candle, and CME micros cost a fixed amount per contract at $2
a point. Run through the generic engine it would be a different strategy with
different costs. The simulator emits trades shaped like the engine's `Trade`,
so `compute_metrics()` and `ResultStore.save_run()` still produce the report,
and metrics keep a single source of truth.

INTRABAR ORDERING (design ported from hypo-development-project)
---------------------------------------------------------------
With a 1.5 ATR stop and a 2 ATR target, a single 5-minute candle can reach
both, and OHLC data cannot say which came first. The limit entry makes it worse,
because the fill ALSO happens inside a candle. Resolution cascade, tagged per
trade:
    RESOLVED      the candle holds no ambiguity
    RESOLVED_1M   ordered by replaying the candle's 1-minute bars
    HEURISTIC     ordered by candle direction: green = open-low-high-close,
                  red = open-high-low-close
    UNCERTAIN     still ambiguous (even inside one 1-minute bar); the
                  configured policy decides, conservative by default
The heuristic's 96% accuracy was measured on XAUUSD in hypo, not on MNQ. With
1-minute data it's rarely needed.

WHAT REAL DATA FORCES THAT THE VIDEO NEVER MENTIONS
---------------------------------------------------
- A short is only taken when its one-candle hold is a real 5 minutes. A signal
  before the daily halt or the weekend would otherwise hold across it.
- Contract rolls: a continuous series jumps when the front month changes. A
  band computed on the old contract's close means nothing for the new one, so
  orders are cancelled and any open position is closed at the last candle
  before the roll.
- A trade that sizes below one contract is skipped, not rounded up to 1.
  Rounding up would break the very limit the sizing exists to respect.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TZ = "America/New_York"
FIVE_MIN_MS = 5 * 60 * 1000
# CME Globex runs 18:00 -> 17:00 New York. Shifting by 6h maps the 18:00 open
# onto the next calendar day, so one trading session gets one date.
SESSION_SHIFT = pd.Timedelta(hours=6)

RESOLVED = "RESOLVED"
RESOLVED_1M = "RESOLVED_1M"
HEURISTIC = "HEURISTIC"
UNCERTAIN = "UNCERTAIN"
_CONF_RANK = {RESOLVED: 0, RESOLVED_1M: 1, HEURISTIC: 2, UNCERTAIN: 3}


# ═══════════════════════════════════════════════════════════════════════════
# COSTS — selected by name, never defaulted (the repo's fee-profile rule)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FuturesFeeModel:
    """Per-contract costs for a CME future."""

    name: str
    commission_per_side: float   # USD per contract per side, all-in
    slippage_ticks: int          # adverse ticks on MARKET and STOP fills
    tick: float = 0.25
    point_value: float = 2.0     # MNQ: $2 per index point
    note: str = ""

    @property
    def tick_value(self) -> float:
        return self.tick * self.point_value


FUTURES_FEE_PROFILES: dict[str, FuturesFeeModel] = {
    "cme_mnq_retail": FuturesFeeModel(
        "cme_mnq_retail", commission_per_side=0.62, slippage_ticks=1,
        note="~$0.25 broker + ~$0.35 exchange + $0.02 NFA per side. Approximate; "
             "check your own statement. 1 tick slippage on market and stop fills; "
             "limit fills (long entry, target) get none.",
    ),
    "cme_mnq_zero": FuturesFeeModel(
        "cme_mnq_zero", commission_per_side=0.0, slippage_ticks=0,
        note="Idealised: no fees and no slippage. For comparing with the video, whose "
             "cost settings are unknown.",
    ),
}


def make_futures_fee_model(name: str) -> FuturesFeeModel:
    """Select a fee profile by name. There is deliberately no default."""
    try:
        return FUTURES_FEE_PROFILES[name]
    except KeyError:
        raise ValueError(
            f"unknown futures fee profile {name!r}; choose one of "
            f"{sorted(FUTURES_FEE_PROFILES)}"
        ) from None


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class MNQHybridConfig:
    long_on: bool = True
    short_on: bool = True

    # Long: ATR band scalp
    atr_len: int = 14                # 11:55
    band_mult: float = 3.1           # 11:55
    tp_mult: float = 2.0             # 10:01
    sl_mult: float = 1.5             # 10:28

    # Short: shooting star. The presenter's objective rule (17:35-18:24).
    wick_body_ratio: float = 2.0
    lower_wick_frac: float = 0.10
    min_wick_atr: float = 0.0        # not in the video; 0 = off
    daily_filter: bool = False       # 24:33, previous session closed red
    # The video's short has NO stop. A stop at the sizing distance is the
    # mechanical version of "close early before the max-position-loss rule"
    # (26:08). Off = the video's rules exactly.
    short_stop_on: bool = False
    short_risk_mult: float = 1.5     # sizing distance for shorts; not in the video

    # Sizing (14:55). fixed_qty=None means risk sizing.
    fixed_qty: int | None = None
    risk_pct: float = 0.5
    starting_balance: float = 100_000.0
    max_qty: int = 20

    # Session
    skip_open: bool = True           # 11:14
    open_hhmm: int = 930             # which open isn't stated; 09:30 NY assumed
    short_needs_contiguous_hold: bool = True

    # Intrabar policy, used only when 1-minute data can't settle the order:
    # "conservative" (stop first), "heuristic" (candle direction), "optimistic".
    ambiguity: str = "conservative"

    @property
    def risk_limit_usd(self) -> float:
        """The prop firm's per-position loss limit, whatever the sizing mode."""
        return self.starting_balance * self.risk_pct / 100.0


# ═══════════════════════════════════════════════════════════════════════════
# TRADE — the engine's Trade fields first, so metrics and storage accept it
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class HybridTrade:
    entry_idx: int
    exit_idx: int
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    commission: float
    exit_reason: str
    module: str = ""
    entry_ts: int = 0
    exit_ts: int = 0
    atr: float = 0.0
    target: float | None = None
    stop: float | None = None
    planned_risk_usd: float = 0.0
    confidence: str = RESOLVED
    held_across_gap: bool = False


@dataclass
class SimResult:
    trades: list[HybridTrade]
    equity: pd.Series                # realized equity at each 5m close, indexed by ms
    bars: pd.DataFrame
    config: MNQHybridConfig
    fee: FuturesFeeModel
    used_1m: bool
    counts: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
# PRIMITIVES — each one checkable by hand
# ═══════════════════════════════════════════════════════════════════════════

def round_half_up(x: float) -> int:
    """Pine's math.round: ties round up. Python's round() uses banker's rounding."""
    return int(math.floor(x + 0.5))


def to_tick(price: float, tick: float) -> float:
    """Nearest tick, as math.round_to_mintick does (12:45: conform to 0.25)."""
    return round(round_half_up(price / tick) * tick, 10)


def ticks_for(distance: float, tick: float) -> int:
    return round_half_up(distance / tick)


def wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    """TradingView's ta.atr: RMA of true range, seeded with the SMA of the
    first n values. The first bar's true range is high - low."""
    size = len(high)
    tr = np.empty(size)
    tr[0] = high[0] - low[0]
    if size > 1:
        prev = close[:-1]
        tr[1:] = np.maximum.reduce([
            high[1:] - low[1:],
            np.abs(high[1:] - prev),
            np.abs(low[1:] - prev),
        ])
    atr = np.full(size, np.nan)
    if size >= n:
        atr[n - 1] = tr[:n].mean()
        for i in range(n, size):
            atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


def is_shooting_star(o: float, h: float, l: float, c: float, atr: float,
                     cfg: MNQHybridConfig) -> bool:
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    if upper <= 0:
        # A flat candle passes both ratio tests at 0 >= 0; it isn't a star.
        return False
    shape = upper >= cfg.wick_body_ratio * body and lower <= cfg.lower_wick_frac * upper
    size = cfg.min_wick_atr <= 0 or upper >= cfg.min_wick_atr * atr
    return shape and size


def qty_for(stop_points: float, cfg: MNQHybridConfig, fee: FuturesFeeModel) -> int:
    """floor(allowed loss / (stop distance x $ per point)), capped."""
    if cfg.fixed_qty is not None:
        return min(cfg.fixed_qty, cfg.max_qty)
    per_contract = stop_points * fee.point_value
    if per_contract <= 0:
        return 0
    return min(int(math.floor(cfg.risk_limit_usd / per_contract)), cfg.max_qty)


# ═══════════════════════════════════════════════════════════════════════════
# DATA PREP
# ═══════════════════════════════════════════════════════════════════════════

def _as_utc_ms(ts: pd.Series) -> np.ndarray:
    """Any timestamp column -> UTC epoch milliseconds.

    Numbers are read by magnitude: TradingView exports Unix SECONDS, most
    vendors milliseconds, and Databento nanoseconds. Strings and datetimes
    without a zone are taken as UTC.
    """
    # pandas' dtype check, not numpy's: np.issubdtype can't read a timezone-aware
    # datetime dtype, which is exactly what Databento's to_df() returns.
    if pd.api.types.is_numeric_dtype(ts):
        v = ts.to_numpy(dtype="float64")
        big = np.nanmax(np.abs(v)) if len(v) else 0
        if big < 1e11:
            v = v * 1000          # seconds
        elif big >= 1e17:
            v = v / 1e6           # nanoseconds
        elif big >= 1e14:
            v = v / 1e3           # microseconds
        return v.astype("int64")
    dt = pd.to_datetime(ts, utc=True)
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    return ((dt - epoch) // pd.Timedelta(milliseconds=1)).to_numpy(dtype="int64")


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Columns ts (UTC ms), open, high, low, close[, volume, instrument_id]."""
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    if "ts" not in out.columns:
        for cand in ("timestamp", "time", "datetime", "date", "ts_event"):
            if cand in out.columns:
                out = out.rename(columns={cand: "ts"})
                break
    if "ts" not in out.columns:
        raise ValueError("bars need a timestamp column (ts / timestamp / time)")
    out["ts"] = _as_utc_ms(out["ts"])
    missing = {"open", "high", "low", "close"} - set(out.columns)
    if missing:
        raise ValueError(f"bars are missing columns: {sorted(missing)}")
    out = out.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].astype(float)
    return out


def resample_1m_to_5m(df1: pd.DataFrame) -> pd.DataFrame:
    """Left-labelled 5-minute candles: the 09:30 candle covers 09:30-09:34."""
    d = normalise(df1)
    d["bucket"] = d["ts"] - (d["ts"] % FIVE_MIN_MS)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in d.columns:
        agg["volume"] = "sum"
    if "instrument_id" in d.columns:
        agg["instrument_id"] = "last"
    out = d.groupby("bucket", sort=True).agg(agg).reset_index().rename(columns={"bucket": "ts"})
    if "instrument_id" in d.columns:
        mixed = d.groupby("bucket")["instrument_id"].nunique()
        out["roll_inside"] = out["ts"].map(mixed).fillna(1).astype(int) > 1
    return out


def prepare_bars(df5: pd.DataFrame, cfg: MNQHybridConfig) -> pd.DataFrame:
    """Everything the loop reads, computed once and vectorised."""
    b = normalise(df5)
    et = pd.to_datetime(b["ts"], unit="ms", utc=True).dt.tz_convert(TZ)
    b["hhmm"] = et.dt.hour * 100 + et.dt.minute
    b["session"] = (et + SESSION_SHIFT).dt.date
    b["atr"] = wilder_atr(b["high"].to_numpy(), b["low"].to_numpy(),
                          b["close"].to_numpy(), cfg.atr_len)

    # Previous session red, with no lookahead: a session's open is its first
    # candle's open and its close its last candle's close, and every candle
    # only ever sees the session BEFORE its own.
    sess = b.groupby("session", sort=True).agg(s_open=("open", "first"), s_close=("close", "last"))
    sess["red"] = sess["s_close"] < sess["s_open"]
    sess["prev_red"] = sess["red"].shift(1, fill_value=False).astype(bool)
    b["prev_red"] = b["session"].map(sess["prev_red"]).fillna(False).astype(bool)

    if "instrument_id" not in b.columns:
        b["instrument_id"] = -1
    b["instrument_id"] = b["instrument_id"].fillna(-1).astype("int64")
    return b


def index_1m(bars5: pd.DataFrame, df1: pd.DataFrame | None):
    """For each 5m candle, the [start, end) slice of its 1-minute bars."""
    if df1 is None:
        return None
    d = normalise(df1)
    t1 = d["ts"].to_numpy()
    t5 = bars5["ts"].to_numpy()
    starts = np.searchsorted(t1, t5, side="left")
    ends = np.searchsorted(t1, t5 + FIVE_MIN_MS, side="left")
    return {
        "o": d["open"].to_numpy(), "h": d["high"].to_numpy(),
        "l": d["low"].to_numpy(), "c": d["close"].to_numpy(),
        "start": starts, "end": ends,
    }


# ═══════════════════════════════════════════════════════════════════════════
# INTRABAR PATHS
# ═══════════════════════════════════════════════════════════════════════════

def _segments(i: int, o, h, l, c, m1) -> tuple[list[tuple[float, float, float, float]], bool]:
    """The candle as ordered pieces: its 1-minute bars if present, else itself."""
    if m1 is not None:
        s, e = int(m1["start"][i]), int(m1["end"][i])
        if e > s:
            return [(m1["o"][k], m1["h"][k], m1["l"][k], m1["c"][k]) for k in range(s, e)], True
    return [(o[i], h[i], l[i], c[i])], False


def _both_hit_stop_first(seg, policy: str, used_1m: bool) -> tuple[bool, str]:
    """Stop and target inside one segment. Returns (stop_first, confidence).
    For a long: a green segment goes open-low-high-close, so the stop comes first."""
    so, _, _, sc = seg
    if policy == "heuristic" and not used_1m:
        return sc >= so, HEURISTIC
    if policy == "optimistic":
        return False, UNCERTAIN
    return True, UNCERTAIN


def _worse(a: str, b: str) -> str:
    return a if _CONF_RANK[a] >= _CONF_RANK[b] else b


def _long_path(segs, used_1m: bool, *, limit: float | None, entry: float | None,
               tp_ticks: int, sl_ticks: int, fee: FuturesFeeModel, policy: str):
    """Walk one candle for a long: optional limit fill, then target/stop.

    Returns (entry_price or None, exit_price or None, reason, confidence,
    target, stop). entry is given when the position was opened earlier.
    """
    tick = fee.tick
    slip = fee.slippage_ticks * tick
    conf = RESOLVED_1M if used_1m else RESOLVED
    tp = sl = None
    if entry is not None:
        tp = round(entry + tp_ticks * tick, 10)
        sl = round(entry - sl_ticks * tick, 10)

    for seg in segs:
        so, sh, sl_, sc = seg
        fill_mid_segment = False
        if entry is None:
            if limit is None:
                return None, None, "", conf, None, None
            if so <= limit:
                entry = so                     # gapped below: filled at the open
            elif sl_ <= limit:
                entry = limit                  # touched: filled at the band
                fill_mid_segment = True
            else:
                continue
            tp = round(entry + tp_ticks * tick, 10)
            sl = round(entry - sl_ticks * tick, 10)

        if not fill_mid_segment:
            # The whole segment happens after entry.
            if so <= sl:
                return entry, so - slip, "stop_loss", conf, tp, sl
            if so >= tp:
                return entry, so, "take_profit", conf, tp, sl
            hit_sl, hit_tp = sl_ <= sl, sh >= tp
            if hit_sl and hit_tp:
                stop_first, c = _both_hit_stop_first(seg, policy, used_1m)
                conf = _worse(conf, c)
                return (entry, sl - slip, "stop_loss", conf, tp, sl) if stop_first \
                    else (entry, tp, "take_profit", conf, tp, sl)
            if hit_sl:
                return entry, sl - slip, "stop_loss", conf, tp, sl
            if hit_tp:
                return entry, tp, "take_profit", conf, tp, sl
            continue

        # Filled partway down this segment. Price had to cross the band to reach
        # the low, so a low at or under the stop means the stop came AFTER the
        # fill: certain. Whether the high came after the fill is not known.
        hit_sl, hit_tp = sl_ <= sl, sh >= tp
        if hit_sl and hit_tp:
            conf = _worse(conf, UNCERTAIN)
            if policy == "optimistic":
                return entry, tp, "take_profit", conf, tp, sl
            return entry, sl - slip, "stop_loss", conf, tp, sl
        if hit_sl:
            return entry, sl - slip, "stop_loss", conf, tp, sl
        if hit_tp:
            if policy == "optimistic":
                return entry, tp, "take_profit", _worse(conf, UNCERTAIN), tp, sl
            if policy == "heuristic" and not used_1m and sc >= so:
                # Green: open-low-high-close, so the high came after the fill.
                return entry, tp, "take_profit", _worse(conf, HEURISTIC), tp, sl
            conf = _worse(conf, HEURISTIC if policy == "heuristic" and not used_1m else UNCERTAIN)
            # Conservative: the high may have come first; the target isn't counted.
    return entry, None, "", conf, tp, sl


def _short_path(segs, used_1m: bool, *, entry: float, stop: float | None,
                fee: FuturesFeeModel):
    """The short's single candle: only a max-loss stop can end it early."""
    slip = fee.slippage_ticks * fee.tick
    conf = RESOLVED_1M if used_1m else RESOLVED
    if stop is None:
        return None, "", conf
    for so, sh, _, _ in segs:
        if so >= stop:
            return so + slip, "max_loss_stop", conf
        if sh >= stop:
            return stop + slip, "max_loss_stop", conf
    return None, "", conf


# ═══════════════════════════════════════════════════════════════════════════
# THE SIMULATION
# ═══════════════════════════════════════════════════════════════════════════

def simulate(bars5: pd.DataFrame, cfg: MNQHybridConfig, fee: FuturesFeeModel,
             bars1: pd.DataFrame | None = None) -> SimResult:
    """Run both modules over 5-minute candles, optionally ordered by 1-minute bars.

    Orders are decided at a candle's CLOSE and live during the NEXT candle,
    which is exactly how the TradingView version behaves.
    """
    if cfg.ambiguity not in ("conservative", "heuristic", "optimistic"):
        raise ValueError(f"ambiguity must be conservative/heuristic/optimistic, got {cfg.ambiguity!r}")
    b = prepare_bars(bars5, cfg)
    m1 = index_1m(b, bars1)
    n = len(b)
    t = b["ts"].to_numpy()
    o, h, l, c = (b[k].to_numpy() for k in ("open", "high", "low", "close"))
    atr = b["atr"].to_numpy()
    hhmm = b["hhmm"].to_numpy()
    prev_red = b["prev_red"].to_numpy()
    inst = b["instrument_id"].to_numpy()
    tick, pv = fee.tick, fee.point_value
    slip = fee.slippage_ticks * tick

    trades: list[HybridTrade] = []
    counts = {"long_orders": 0, "short_signals": 0, "skipped_size": 0,
              "skipped_opening": 0, "skipped_gap": 0, "roll_closes": 0}
    realized = np.zeros(n)

    pos: dict | None = None
    pend_long: dict | None = None
    pend_short: dict | None = None
    short_exit_next_open = False

    def close_trade(p: dict, exit_px: float, exit_i: int, exit_ts: int, reason: str, conf: str):
        qty = p["qty"]
        if p["side"] == "LONG":
            gross = (exit_px - p["entry"]) * qty * pv
        else:
            gross = (p["entry"] - exit_px) * qty * pv
        commission = 2 * qty * fee.commission_per_side
        pnl = gross - commission
        notional = p["entry"] * qty * pv
        trades.append(HybridTrade(
            entry_idx=p["entry_i"], exit_idx=exit_i, side=p["side"],
            entry_price=round(p["entry"], 10), exit_price=round(exit_px, 10),
            quantity=float(qty), pnl=pnl, pnl_pct=pnl / notional if notional else 0.0,
            commission=commission, exit_reason=reason, module=p["module"],
            entry_ts=int(p["entry_ts"]), exit_ts=int(exit_ts), atr=p["atr"],
            target=p.get("tp"), stop=p.get("sl"), planned_risk_usd=p["risk_usd"],
            confidence=_worse(p.get("conf", RESOLVED), conf),
            held_across_gap=p.get("gap", False),
        ))
        realized[exit_i] += pnl

    for i in range(n):
        segs, used_1m = _segments(i, o, h, l, c, m1)

        # A. The short's one-candle exit, at this candle's open.
        if pos is not None and pos["side"] == "SHORT" and short_exit_next_open:
            close_trade(pos, o[i] + slip, i, t[i], "time_exit", RESOLVED)
            pos, short_exit_next_open = None, False

        # B. A pending short enters at this open.
        if pend_short is not None and pos is None:
            entry = o[i] - slip
            stop = round(entry + pend_short["stop_ticks"] * tick, 10) if cfg.short_stop_on else None
            pos = {"side": "SHORT", "module": "short", "entry": entry, "entry_i": i,
                   "entry_ts": t[i], "qty": pend_short["qty"], "atr": pend_short["atr"],
                   "sl": stop, "tp": None, "risk_usd": pend_short["risk_usd"]}
        pend_short = None

        # C. A pending long limit may fill during this candle, and even exit in it.
        if pend_long is not None and pos is None:
            e_px, x_px, why, conf, tp, sl = _long_path(
                segs, used_1m, limit=pend_long["limit"], entry=None,
                tp_ticks=pend_long["tp_ticks"], sl_ticks=pend_long["sl_ticks"],
                fee=fee, policy=cfg.ambiguity)
            if e_px is not None:
                pos = {"side": "LONG", "module": "long", "entry": e_px, "entry_i": i,
                       "entry_ts": t[i], "qty": pend_long["qty"], "atr": pend_long["atr"],
                       "tp": tp, "sl": sl, "tp_ticks": pend_long["tp_ticks"],
                       "sl_ticks": pend_long["sl_ticks"], "conf": conf,
                       "risk_usd": pend_long["risk_usd"]}
                if x_px is not None:
                    close_trade(pos, x_px, i, t[i], why, conf)
                    pos = None
            pend_long = None
        elif pos is not None and pos["side"] == "LONG" and pos["entry_i"] < i:
            # D. A long opened earlier: its fixed target and stop.
            _, x_px, why, conf, _, _ = _long_path(
                segs, used_1m, limit=None, entry=pos["entry"],
                tp_ticks=pos["tp_ticks"], sl_ticks=pos["sl_ticks"],
                fee=fee, policy=cfg.ambiguity)
            if x_px is not None:
                close_trade(pos, x_px, i, t[i], why, conf)
                pos = None
            else:
                pos["conf"] = _worse(pos.get("conf", RESOLVED), conf)

        # E. The short in its only candle: a max-loss stop, if enabled.
        if pos is not None and pos["side"] == "SHORT" and pos["entry_i"] == i:
            x_px, why, conf = _short_path(segs, used_1m, entry=pos["entry"],
                                          stop=pos["sl"], fee=fee)
            if x_px is not None:
                close_trade(pos, x_px, i, t[i], why, conf)
                pos = None
            else:
                short_exit_next_open = True

        # F. At this close, the orders for the next candle.
        if i + 1 >= n:
            break
        next_gap = t[i + 1] - t[i] != FIVE_MIN_MS
        if pos is not None and pos["side"] == "LONG" and next_gap:
            pos["gap"] = True

        if inst[i] >= 0 and inst[i + 1] >= 0 and inst[i + 1] != inst[i]:
            # Contract roll next candle: nothing carries across it.
            if pos is not None:
                px = c[i] - slip if pos["side"] == "LONG" else c[i] + slip
                close_trade(pos, px, i, t[i], "roll", RESOLVED)
                pos, short_exit_next_open = None, False
                counts["roll_closes"] += 1
            continue
        if np.isnan(atr[i]) or atr[i] <= 0:
            continue

        flat = pos is None
        next_is_open = cfg.skip_open and hhmm[i + 1] == cfg.open_hhmm
        this_is_open = cfg.skip_open and hhmm[i] == cfg.open_hhmm

        short_signal = False
        if cfg.short_on and flat and is_shooting_star(o[i], h[i], l[i], c[i], atr[i], cfg):
            stop_ticks = ticks_for(cfg.short_risk_mult * atr[i], tick)
            qty = qty_for(stop_ticks * tick, cfg, fee)
            contiguous = (not next_gap and i + 2 < n and t[i + 2] - t[i + 1] == FIVE_MIN_MS)
            if this_is_open or next_is_open:
                counts["skipped_opening"] += 1
            elif cfg.daily_filter and not prev_red[i]:
                pass
            elif cfg.short_needs_contiguous_hold and not contiguous:
                counts["skipped_gap"] += 1
            elif qty < 1:
                counts["skipped_size"] += 1
            else:
                short_signal = True
                counts["short_signals"] += 1
                pend_short = {"qty": qty, "stop_ticks": stop_ticks, "atr": float(atr[i]),
                              "risk_usd": stop_ticks * tick * pv * qty}

        if cfg.long_on and flat and not short_signal:
            if next_is_open:
                counts["skipped_opening"] += 1
            else:
                tp_ticks = ticks_for(cfg.tp_mult * atr[i], tick)
                sl_ticks = ticks_for(cfg.sl_mult * atr[i], tick)
                qty = qty_for(sl_ticks * tick, cfg, fee)
                if qty < 1:
                    counts["skipped_size"] += 1
                else:
                    pend_long = {"limit": to_tick(c[i] - cfg.band_mult * atr[i], tick),
                                 "tp_ticks": tp_ticks, "sl_ticks": sl_ticks, "qty": qty,
                                 "atr": float(atr[i]), "risk_usd": sl_ticks * tick * pv * qty}
                    counts["long_orders"] += 1

    # An open position at the end of data closes at the last close.
    if pos is not None:
        px = c[-1] - slip if pos["side"] == "LONG" else c[-1] + slip
        close_trade(pos, px, n - 1, t[-1], "end_of_data", RESOLVED)

    equity = pd.Series(cfg.starting_balance + np.cumsum(realized), index=t, name="equity")
    return SimResult(trades=trades, equity=equity, bars=b, config=cfg, fee=fee,
                     used_1m=m1 is not None, counts=counts)


# ═══════════════════════════════════════════════════════════════════════════
# ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════

def module_stats(trades: list[HybridTrade], limit_usd: float) -> dict:
    """The numbers the video reports, plus the prop-firm checks it doesn't."""
    if not trades:
        return {"trades": 0}
    pnl = np.array([t.pnl for t in trades])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    dd = float((peak - eq).max()) if len(eq) else 0.0
    conf = pd.Series([t.confidence for t in trades]).value_counts().to_dict()
    reasons = pd.Series([t.exit_reason for t in trades]).value_counts().to_dict()
    worst = float(pnl.min())
    return {
        "trades": int(len(trades)),
        "net_pnl": round(float(pnl.sum()), 2),
        "profit_factor": round(float(wins.sum() / -losses.sum()), 3) if losses.sum() < 0 else None,
        "win_rate": round(float(len(wins) / len(pnl)), 4),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "max_drawdown_usd": round(dd, 2),
        "worst_trade": round(worst, 2),
        "commission_total": round(float(sum(t.commission for t in trades)), 2),
        "avg_contracts": round(float(np.mean([t.quantity for t in trades])), 2),
        "limit_breaches": int((-pnl > limit_usd).sum()),
        "limit_usd": round(limit_usd, 2),
        "exit_reasons": reasons,
        "confidence": conf,
        "held_across_gap": int(sum(t.held_across_gap for t in trades)),
    }


def by_period(trades: list[HybridTrade], freq: str = "Q") -> pd.DataFrame:
    """Net P&L and trade count per period. Where an edge lives, and where it doesn't."""
    if not trades:
        return pd.DataFrame(columns=["period", "module", "trades", "net_pnl"])
    df = pd.DataFrame({
        "exit": pd.to_datetime([t.exit_ts for t in trades], unit="ms", utc=True).tz_convert(TZ),
        "module": [t.module for t in trades],
        "pnl": [t.pnl for t in trades],
    })
    df["period"] = df["exit"].dt.tz_localize(None).dt.to_period(freq).astype(str)
    g = df.groupby(["period", "module"])["pnl"].agg(trades="count", net_pnl="sum").reset_index()
    g["net_pnl"] = g["net_pnl"].round(2)
    return g
