"""Strategy Research Catalog — structured storage and querying for strategy research.

Loads strategy metadata from config/catalog.toml. Links to validation results
stored in SQLite (via src/data/storage.py).

Usage:
    from src.research.catalog import StrategyCatalog

    catalog = StrategyCatalog()
    catalog.load()
    print(catalog.summary_table())
    entry = catalog.get("wf_ema")
    results = catalog.filter(min_sharpe=2.0, status=StrategyStatus.RESEARCHED)
"""

from __future__ import annotations

import enum
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import msgspec
import orjson

from src.utils.logger import get_logger

log = get_logger("catalog")

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CATALOG = _ROOT / "config" / "catalog.toml"
_DEFAULT_DB = _ROOT / "data" / "trades.db"


# ── Enums ─────────────────────────────────────────────────────────────────────


class StrategyStatus(str, enum.Enum):
    RESEARCHED = "researched"
    IMPLEMENTING = "implementing"
    IMPLEMENTED = "implemented"
    VALIDATED = "validated"
    REJECTED = "rejected"


class StrategyCategory(str, enum.Enum):
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    MARKET_MAKING = "market_making"
    STAT_ARB = "stat_arb"
    RL = "rl"
    TREND_FOLLOWING = "trend_following"
    BREAKOUT = "breakout"
    PORTFOLIO = "portfolio"


class SourceType(str, enum.Enum):
    PAPER = "paper"
    REPO = "repo"
    BLOG = "blog"
    THESIS = "thesis"
    INTERNAL = "internal"


# ── Data Structs ──────────────────────────────────────────────────────────────


class CatalogLink(msgspec.Struct, frozen=True):
    label: str
    url: str


class CatalogEntry(msgspec.Struct):
    """A single strategy in the research catalog."""
    id: str
    name: str
    category: str                       # StrategyCategory value
    source_type: str                    # SourceType value
    source_url: str = ""
    source_label: str = ""
    sharpe: float | None = None
    max_drawdown_pct: float | None = None
    markets: list[str] = []
    timeframes: list[str] = []
    effort_days: int | None = None
    status: str = "researched"          # StrategyStatus value
    impl_file: str | None = None
    config_section: str | None = None
    edge: str = ""
    failure_modes: list[str] = []
    key_params: dict = {}
    links: list[CatalogLink] = []
    notes: str = ""


class ValidationRecord(msgspec.Struct, frozen=True):
    """A stored validation run result."""
    id: int
    strategy_id: str
    timestamp: str
    protocol: str
    verdict: str
    intervals_profitable: int
    intervals_tested: int
    timeframes_profitable: int
    timeframes_tested: int
    avg_profit_factor: float
    worst_drawdown: float
    total_trades: int
    symbol: str
    created_at: str


# ── Catalog Class ─────────────────────────────────────────────────────────────


class StrategyCatalog:
    """Load, query, and manage the strategy research catalog."""

    def __init__(
        self,
        catalog_path: Path | str | None = None,
        db_path: Path | str | None = None,
    ):
        self._path = Path(catalog_path) if catalog_path else _DEFAULT_CATALOG
        self._db_path = Path(db_path) if db_path else _DEFAULT_DB
        self._entries: list[CatalogEntry] = []
        self._by_id: dict[str, CatalogEntry] = {}

    # ── Loading ───────────────────────────────────────────────────────────

    def load(self) -> None:
        """(Re)load catalog from TOML file."""
        with open(self._path, "rb") as f:
            data = tomllib.load(f)

        strategies = data.get("strategy", [])
        decoder = msgspec.json.Decoder(CatalogEntry)
        self._entries = []
        self._by_id = {}

        for raw in strategies:
            # Convert links from list[dict] to list[CatalogLink]
            if "links" in raw:
                raw["links"] = [
                    {"label": lk["label"], "url": lk["url"]}
                    for lk in raw["links"]
                ]
            entry = decoder.decode(msgspec.json.encode(raw))
            self._entries.append(entry)
            self._by_id[entry.id] = entry

        log.info("catalog_loaded", count=len(self._entries), path=str(self._path))

    @property
    def entries(self) -> list[CatalogEntry]:
        return list(self._entries)

    def get(self, strategy_id: str) -> CatalogEntry | None:
        return self._by_id.get(strategy_id)

    # ── Filtering ─────────────────────────────────────────────────────────

    def filter(
        self,
        status: str | StrategyStatus | None = None,
        category: str | StrategyCategory | None = None,
        min_sharpe: float | None = None,
        max_effort_days: int | None = None,
        market: str | None = None,
    ) -> list[CatalogEntry]:
        """Filter entries by criteria. All filters are AND-combined."""
        results = self._entries
        if status is not None:
            s = status.value if isinstance(status, StrategyStatus) else status
            results = [e for e in results if e.status == s]
        if category is not None:
            c = category.value if isinstance(category, StrategyCategory) else category
            results = [e for e in results if e.category == c]
        if min_sharpe is not None:
            results = [e for e in results if e.sharpe is not None and e.sharpe >= min_sharpe]
        if max_effort_days is not None:
            results = [e for e in results if e.effort_days is not None and e.effort_days <= max_effort_days]
        if market is not None:
            results = [e for e in results if market in e.markets]
        return results

    # ── Display ───────────────────────────────────────────────────────────

    def summary_table(self, entries: list[CatalogEntry] | None = None) -> str:
        """Formatted ASCII table of strategies."""
        items = entries if entries is not None else self._entries
        if not items:
            return "  (no strategies in catalog)"

        lines = []
        header = f"  {'ID':<20} {'Name':<35} {'Cat':<15} {'Sharpe':>7} {'DD%':>6} {'Days':>5} {'Status':<12} {'TFs'}"
        sep = "  " + "-" * (len(header) - 2)
        lines.append(sep)
        lines.append(header)
        lines.append(sep)

        for e in items:
            sharpe_s = f"{e.sharpe:.2f}" if e.sharpe is not None else "N/A"
            dd_s = f"{e.max_drawdown_pct:.1f}" if e.max_drawdown_pct is not None else "N/A"
            days_s = str(e.effort_days) if e.effort_days is not None else "N/A"
            tfs = ", ".join(e.timeframes) if e.timeframes else "N/A"
            lines.append(
                f"  {e.id:<20} {e.name:<35} {e.category:<15} {sharpe_s:>7} {dd_s:>6} {days_s:>5} {e.status:<12} {tfs}"
            )

        lines.append(sep)
        lines.append(f"  Total: {len(items)} strategies")
        return "\n".join(lines)

    def detail_view(self, strategy_id: str) -> str:
        """Full detail view for a single strategy."""
        e = self.get(strategy_id)
        if e is None:
            return f"  Strategy '{strategy_id}' not found in catalog."

        lines = [
            "",
            f"  {'=' * 80}",
            f"  {e.name}  [{e.id}]",
            f"  {'=' * 80}",
            f"  Category:     {e.category}",
            f"  Status:       {e.status}",
            f"  Source:       {e.source_label} ({e.source_type})",
        ]
        if e.source_url:
            lines.append(f"  URL:          {e.source_url}")
        if e.sharpe is not None:
            lines.append(f"  Sharpe:       {e.sharpe:.3f}")
        if e.max_drawdown_pct is not None:
            lines.append(f"  Max DD:       {e.max_drawdown_pct:.1f}%")
        lines.append(f"  Markets:      {', '.join(e.markets) if e.markets else 'N/A'}")
        lines.append(f"  Timeframes:   {', '.join(e.timeframes) if e.timeframes else 'N/A'}")
        if e.effort_days is not None:
            lines.append(f"  Effort:       {e.effort_days} days")
        if e.impl_file:
            lines.append(f"  Impl File:    {e.impl_file}")

        lines.append(f"\n  Edge:")
        lines.append(f"    {e.edge}")

        if e.failure_modes:
            lines.append(f"\n  Failure Modes:")
            for fm in e.failure_modes:
                lines.append(f"    - {fm}")

        if e.key_params:
            lines.append(f"\n  Key Parameters:")
            for k, v in e.key_params.items():
                lines.append(f"    {k}: {v}")

        if e.links:
            lines.append(f"\n  Links:")
            for lk in e.links:
                lines.append(f"    [{lk.label}] {lk.url}")

        if e.notes:
            lines.append(f"\n  Notes:")
            lines.append(f"    {e.notes}")

        lines.append(f"  {'=' * 80}")
        return "\n".join(lines)

    def category_summary(self) -> str:
        """High-level counts by status and category."""
        status_counts: dict[str, int] = {}
        cat_counts: dict[str, int] = {}
        for e in self._entries:
            status_counts[e.status] = status_counts.get(e.status, 0) + 1
            cat_counts[e.category] = cat_counts.get(e.category, 0) + 1

        lines = [
            "",
            f"  {'=' * 50}",
            f"  CATALOG SUMMARY ({len(self._entries)} strategies)",
            f"  {'=' * 50}",
            "",
            "  By Status:",
        ]
        for s, c in sorted(status_counts.items()):
            lines.append(f"    {s:<15} {c}")
        lines.append("")
        lines.append("  By Category:")
        for cat, c in sorted(cat_counts.items()):
            lines.append(f"    {cat:<15} {c}")

        # Sharpe leaderboard
        with_sharpe = [e for e in self._entries if e.sharpe is not None and e.status != "rejected"]
        if with_sharpe:
            with_sharpe.sort(key=lambda e: e.sharpe or 0, reverse=True)
            lines.append("")
            lines.append("  Sharpe Leaderboard (non-rejected):")
            for e in with_sharpe[:10]:
                lines.append(f"    {e.sharpe:>7.2f}  {e.name}  [{e.status}]")

        lines.append(f"\n  {'=' * 50}")
        return "\n".join(lines)

    # ── Validation Storage ────────────────────────────────────────────────

    def _get_db(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS validation_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                protocol TEXT NOT NULL,
                verdict TEXT NOT NULL,
                intervals_profitable INTEGER DEFAULT 0,
                intervals_tested INTEGER DEFAULT 0,
                timeframes_profitable INTEGER DEFAULT 0,
                timeframes_tested INTEGER DEFAULT 0,
                avg_profit_factor REAL DEFAULT 0,
                worst_drawdown REAL DEFAULT 0,
                total_trades INTEGER DEFAULT 0,
                symbol TEXT DEFAULT '',
                report_json TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        return conn

    def link_validation(self, strategy_id: str, report) -> int:
        """Store a ValidationReport for a catalog entry.

        Args:
            strategy_id: Catalog entry ID (e.g., "wf_ema").
            report: A ValidationReport from src.backtest.validator.

        Returns:
            Row ID of the stored result.
        """
        conn = self._get_db()

        # Extract summary metrics from report
        intervals_profitable = 0
        intervals_tested = 0
        timeframes_profitable = 0
        timeframes_tested = 0
        total_trades = 0
        worst_dd = 0.0
        pf_values = []

        if hasattr(report, "interval_results"):
            for r in report.interval_results:
                intervals_tested += 1
                if r.profitable:
                    intervals_profitable += 1
                total_trades += r.trades
                worst_dd = min(worst_dd, r.max_drawdown_pct)
                if r.profit_factor is not None:
                    pf_values.append(r.profit_factor)

        if hasattr(report, "timeframe_results"):
            for r in report.timeframe_results:
                timeframes_tested += 1
                if r.profitable:
                    timeframes_profitable += 1
                total_trades += r.trades
                worst_dd = min(worst_dd, r.max_drawdown_pct)
                if r.profit_factor is not None:
                    pf_values.append(r.profit_factor)

        avg_pf = sum(pf_values) / len(pf_values) if pf_values else 0.0
        verdict = getattr(report, "verdict", "UNKNOWN")
        symbol = getattr(report, "symbol", "")

        # Serialize full report to JSON
        report_json = ""
        try:
            report_json = orjson.dumps(
                msgspec.structs.asdict(report) if hasattr(report, '__struct_fields__') else str(report)
            ).decode()
        except Exception:
            report_json = str(report)

        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            """INSERT INTO validation_results
               (strategy_id, timestamp, protocol, verdict,
                intervals_profitable, intervals_tested,
                timeframes_profitable, timeframes_tested,
                avg_profit_factor, worst_drawdown, total_trades, symbol, report_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                strategy_id, now, "full_validation", verdict,
                intervals_profitable, intervals_tested,
                timeframes_profitable, timeframes_tested,
                avg_pf, worst_dd, total_trades, symbol, report_json,
            ),
        )
        conn.commit()
        row_id = cursor.lastrowid or 0
        conn.close()
        log.info("validation_stored", strategy_id=strategy_id, verdict=verdict, row_id=row_id)
        return row_id

    def get_validations(self, strategy_id: str) -> list[ValidationRecord]:
        """Retrieve stored validation results for a strategy."""
        conn = self._get_db()
        rows = conn.execute(
            """SELECT id, strategy_id, timestamp, protocol, verdict,
                      intervals_profitable, intervals_tested,
                      timeframes_profitable, timeframes_tested,
                      avg_profit_factor, worst_drawdown, total_trades, symbol, created_at
               FROM validation_results
               WHERE strategy_id = ?
               ORDER BY created_at DESC""",
            (strategy_id,),
        ).fetchall()
        conn.close()

        return [
            ValidationRecord(
                id=r[0], strategy_id=r[1], timestamp=r[2], protocol=r[3],
                verdict=r[4], intervals_profitable=r[5], intervals_tested=r[6],
                timeframes_profitable=r[7], timeframes_tested=r[8],
                avg_profit_factor=r[9], worst_drawdown=r[10],
                total_trades=r[11], symbol=r[12], created_at=r[13],
            )
            for r in rows
        ]

    def validations_table(self, strategy_id: str) -> str:
        """Formatted table of validation history for a strategy."""
        records = self.get_validations(strategy_id)
        if not records:
            return f"  No validation results for '{strategy_id}'."

        lines = []
        header = f"  {'Date':<22} {'Protocol':<18} {'Verdict':<30} {'Int':>5} {'TF':>5} {'PF':>6} {'DD%':>7} {'Trades':>7}"
        sep = "  " + "-" * (len(header) - 2)
        lines.append(sep)
        lines.append(header)
        lines.append(sep)

        for r in records:
            date = r.timestamp[:19] if len(r.timestamp) > 19 else r.timestamp
            int_s = f"{r.intervals_profitable}/{r.intervals_tested}"
            tf_s = f"{r.timeframes_profitable}/{r.timeframes_tested}"
            lines.append(
                f"  {date:<22} {r.protocol:<18} {r.verdict:<30} {int_s:>5} {tf_s:>5} "
                f"{r.avg_profit_factor:>6.2f} {r.worst_drawdown:>6.1f}% {r.total_trades:>7}"
            )

        lines.append(sep)
        return "\n".join(lines)
