"""Strategy storage CLI — list / show / register (task #117 G.8).

Read-side access to the `strategies` + `strategy_versions` tables populated
automatically by `run_deep_backtest()`. Mirrors the graveyard/catalog CLI
style — `rich` for nice tables if installed, plain text fallback otherwise.

Subcommands:
    list       — show all strategies with version counts + max-return headline
    show NAME  — show one strategy with all its versions, verdict, report link
    register NAME [--description X] [--family Y] [--tier Z] [--tags a,b]
                — stub an empty row for ideation before the first backtest

Auto-capture from deep_backtest makes `register` optional — fresh strategies
land in the DB on their first run. Use `register` when you want to annotate
parent metadata (description, family, tags) before backtesting.

Examples:
    PYTHONPATH=. python3 scripts/strategies.py list
    PYTHONPATH=. python3 scripts/strategies.py list --status deployable
    PYTHONPATH=. python3 scripts/strategies.py show donchian_gold
    PYTHONPATH=. python3 scripts/strategies.py register new_idea --family mean_reversion --description "RSI divergence at session highs"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure src/ is on sys.path when invoked directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategies.storage import (  # noqa: E402
    StoredStrategy,
    get_strategy,
    get_version,
    kill_strategy,
    list_strategies,
    list_version_runs,
    list_versions,
    query_best_by_max_return,
    query_deployable_by_calmar,
    query_vanity_traps,
    upsert_strategy,
    _to_absolute,
)


# ── Optional rich ─────────────────────────────────────────────────────

try:
    from rich.console import Console
    from rich.table import Table
    _HAVE_RICH = True
    _console = Console()
except ImportError:
    _HAVE_RICH = False
    _console = None


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _fmt_num(v: float | None, precision: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:.{precision}f}"


def _fmt_date(v: str | None) -> str:
    if v is None:
        return "never"
    return v[:10]


# ── list ──────────────────────────────────────────────────────────────


def cmd_list(args: argparse.Namespace) -> int:
    strategies = list_strategies(
        status=args.status,
        family=args.family,
        db_path=args.db,
    )
    if not strategies:
        print("(no strategies in storage — run deep_backtest or use `register` first)")
        return 0

    # Gather version rollups per strategy
    rows: list[dict] = []
    for s in strategies:
        versions = list_versions(strategy_name=s.name, db_path=args.db)
        best = None
        last_backtested = None
        for v in versions:
            if v.last_backtested_at and (last_backtested is None or v.last_backtested_at > last_backtested):
                last_backtested = v.last_backtested_at
            if v.max_return_pct is not None:
                if best is None or v.max_return_pct > (best.max_return_pct or -1e18):
                    best = v
        rows.append({
            "name": s.name,
            "family": s.family or "—",
            "status": s.status,
            "n_versions": len(versions),
            "last_backtested": _fmt_date(last_backtested),
            "best_max_return": _fmt_pct(best.max_return_pct) if best else "—",
            "best_sane": best.max_return_sane if best else None,
        })

    if _HAVE_RICH:
        table = Table(title="Strategies", show_lines=False)
        table.add_column("NAME", style="cyan")
        table.add_column("FAMILY")
        table.add_column("STATUS")
        table.add_column("VERSIONS", justify="right")
        table.add_column("LAST BACKTESTED")
        table.add_column("BEST MAX RETURN", justify="right")
        for r in rows:
            badge = "" if (r["best_sane"] is None or r["best_sane"]) else " !"
            table.add_row(
                r["name"],
                r["family"],
                r["status"],
                str(r["n_versions"]),
                r["last_backtested"],
                f"{r['best_max_return']}{badge}",
            )
        _console.print(table)
    else:
        header = f"{'NAME':<28} {'FAMILY':<16} {'STATUS':<14} {'VERS':>4} {'LAST BACKTESTED':<16} {'BEST MAX RETURN':>16}"
        print(header)
        print("-" * len(header))
        for r in rows:
            badge = "" if (r["best_sane"] is None or r["best_sane"]) else " !"
            print(
                f"{r['name']:<28} {r['family']:<16} {r['status']:<14} "
                f"{r['n_versions']:>4} {r['last_backtested']:<16} "
                f"{r['best_max_return']}{badge:>2}"
            )
    return 0


# ── show ──────────────────────────────────────────────────────────────


def cmd_show(args: argparse.Namespace) -> int:
    s = get_strategy(args.name, db_path=args.db)
    if s is None:
        print(f"error: no strategy '{args.name}' in storage", file=sys.stderr)
        print(f"       run deep_backtest or `scripts/strategies.py register {args.name}`", file=sys.stderr)
        return 1

    # Header
    header_parts = [f"{s.name} [{s.status}]"]
    if s.display_name and s.display_name != s.name:
        header_parts.append(f"({s.display_name})")
    print(" ".join(header_parts))
    if s.description:
        print(f"  {s.description}")
    meta_bits = []
    if s.family:
        meta_bits.append(f"Family: {s.family}")
    if s.category:
        meta_bits.append(f"Category: {s.category}")
    if s.tier:
        meta_bits.append(f"Tier: {s.tier}")
    if s.markets:
        meta_bits.append(f"Markets: {', '.join(s.markets)}")
    if s.default_timeframe:
        meta_bits.append(f"Default TF: {s.default_timeframe}")
    if s.leverage_range:
        meta_bits.append(f"Leverage: {s.leverage_range[0]:g}–{s.leverage_range[1]:g}x")
    if s.tags:
        meta_bits.append(f"Tags: {', '.join(s.tags)}")
    if meta_bits:
        print("  " + "  ·  ".join(meta_bits))
    print()

    versions = list_versions(strategy_name=s.name, db_path=args.db)
    if not versions:
        print("  No versions yet — run deep_backtest to auto-capture.")
        return 0

    print(f"  Versions ({len(versions)}):")
    if _HAVE_RICH:
        table = Table(show_lines=False, box=None, pad_edge=False)
        table.add_column("SLUG", style="cyan")
        table.add_column("VERDICT")
        table.add_column("MAX RET", justify="right")
        table.add_column("(DD%, Calmar)")
        table.add_column("REPORT")
        for v in versions:
            dd = v.max_return_cell.get("maxdd_pct") if v.max_return_cell else None
            calmar = v.max_return_cell.get("calmar") if v.max_return_cell else None
            risk_bits = f"({_fmt_num(dd)}%, {_fmt_num(calmar)})" if dd is not None else "—"
            ret_cell = _fmt_pct(v.max_return_pct)
            if v.max_return_sane is False:
                ret_cell = f"{ret_cell} !"
            table.add_row(
                v.version_slug,
                v.verdict or "—",
                ret_cell,
                risk_bits,
                v.report_dir or "—",
            )
        _console.print(table)
    else:
        print(f"    {'SLUG':<30} {'VERDICT':<14} {'MAX RET':>10} {'(DD%, CALMAR)':<20} REPORT")
        for v in versions:
            dd = v.max_return_cell.get("maxdd_pct") if v.max_return_cell else None
            calmar = v.max_return_cell.get("calmar") if v.max_return_cell else None
            risk_bits = f"({_fmt_num(dd)}%, {_fmt_num(calmar)})" if dd is not None else "—"
            ret_cell = _fmt_pct(v.max_return_pct)
            if v.max_return_sane is False:
                ret_cell = f"{ret_cell} !"
            print(
                f"    {v.version_slug:<30} {(v.verdict or '—'):<14} "
                f"{ret_cell:>10} {risk_bits:<20} {v.report_dir or '—'}"
            )

    # Warnings footer for flagged cells
    flagged = [v for v in versions if v.max_return_sane is False and v.max_return_warning]
    if flagged:
        print()
        print("  ! = vanity-flag on max-return cell:")
        for v in flagged:
            print(f"    {v.version_slug}: {v.max_return_warning}")
    return 0


# ── register ──────────────────────────────────────────────────────────


def cmd_open(args: argparse.Namespace) -> int:
    """Open a version's deep_backtest HTML report in the default browser."""
    import webbrowser

    v = get_version(args.name, args.slug, db_path=args.db)
    if v is None:
        print(f"error: no version '{args.slug}' for strategy '{args.name}'", file=sys.stderr)
        return 1
    if v.report_html_path is None:
        print(
            f"error: version '{args.slug}' has no report_html_path "
            f"(deep_backtest was run with --no-html?)",
            file=sys.stderr,
        )
        return 1
    abs_path = _to_absolute(v.report_html_path)
    if abs_path is None or not abs_path.exists():
        print(f"error: HTML file not found at {abs_path}", file=sys.stderr)
        return 1
    print(f"Opening {abs_path}")
    webbrowser.open(f"file://{abs_path}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Walk router.STRATEGY_REGISTRY and upsert empty strategy rows for any
    registered class not yet in the DB."""
    try:
        from src.strategies.router import STRATEGY_REGISTRY
    except ImportError as e:
        print(f"error: cannot import STRATEGY_REGISTRY: {e}", file=sys.stderr)
        return 1

    if not STRATEGY_REGISTRY:
        print("(STRATEGY_REGISTRY is empty)")
        return 0

    n_created = 0
    n_existing = 0
    for key, cls in sorted(STRATEGY_REGISTRY.items()):
        existing = get_strategy(key, db_path=args.db)
        if existing is not None:
            n_existing += 1
            continue

        # Try to introspect via class instantiation. Fail gracefully — many
        # strategies need __init__ args we don't have here.
        description = None
        tier = None
        markets: list[str] = []
        default_tf = None
        leverage_range = None
        try:
            inst = cls()
            doc = (cls.__doc__ or "").strip()
            description = doc.split("\n", 1)[0] if doc else None
            t = getattr(inst, "tier", None)
            tier = t.name if hasattr(t, "name") else (str(t) if t else None)
            markets = list(getattr(inst, "markets", []) or [])
            default_tf = getattr(inst, "timeframe", None)
            lr = getattr(inst, "leverage_range", None)
            if lr is not None:
                leverage_range = [float(lr[0]), float(lr[1])]
        except Exception:
            pass

        s = StoredStrategy(
            name=key,
            display_name=key,
            description=description,
            base_class=cls.__name__,
            tier=tier,
            markets=markets,
            default_timeframe=default_tf,
            leverage_range=leverage_range,
            status="researching",
        )
        upsert_strategy(s, db_path=args.db)
        n_created += 1
        print(f"  + {key} ({cls.__name__})")

    print(f"\nSync complete: {n_created} created, {n_existing} already existed.")
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    """Mark a strategy as killed and link to its graveyard entry."""
    s = get_strategy(args.name, db_path=args.db)
    if s is None:
        print(f"error: no strategy '{args.name}' in storage", file=sys.stderr)
        return 1
    kill_strategy(args.name, args.graveyard_id, db_path=args.db)
    print(f"Killed: {args.name} → graveyard_id={args.graveyard_id}")
    return 0


def cmd_vanity(args: argparse.Namespace) -> int:
    """List all vanity-trap versions: DEPLOYABLE verdict but max_return_sane=False.

    These are 'looks-great-but-actually-fragile' rows where the headline
    return number is driven by thin trades, huge drawdown, or low Calmar.
    Audit BEFORE promoting any version to live capital.
    """
    traps = query_vanity_traps(db_path=args.db)
    if not traps:
        print("No vanity traps detected — all DEPLOYABLE versions have sane max-return cells.")
        return 0

    print(f"{len(traps)} vanity trap(s) found (DEPLOYABLE verdict + max_return_sane=False):\n")
    for v in traps:
        print(f"  - strategy_id={v.strategy_id} slug={v.version_slug}")
        print(f"      max_return: {_fmt_pct(v.max_return_pct)}")
        print(f"      warning:    {v.max_return_warning}")
        print(f"      report:     {v.report_dir or '—'}")
        print()
    return 1  # exit non-zero so CI / scripts can detect


def cmd_top(args: argparse.Namespace) -> int:
    """Show the top N versions by max_return_pct (or by Calmar with --by-calmar)."""
    if args.by_calmar:
        results = query_deployable_by_calmar(use_wf_calmar=True, limit=args.limit, db_path=args.db)
        title = "Top DEPLOYABLE versions by walk-forward Calmar"
    else:
        results = query_best_by_max_return(verdict=args.verdict, limit=args.limit, db_path=args.db)
        v = args.verdict or "any verdict"
        title = f"Top versions by max return ({v})"

    print(title)
    print("-" * len(title))
    for v in results:
        badge = "" if (v.max_return_sane is None or v.max_return_sane) else " !"
        calmar_bit = f" wf_calmar={_fmt_num(v.wf_continuous_calmar)}" if v.wf_continuous_calmar is not None else ""
        print(
            f"  {v.version_slug:<30} verdict={(v.verdict or '—'):<13} "
            f"max={_fmt_pct(v.max_return_pct)}{badge}{calmar_bit}"
        )
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Show the run history for a version — every deep_backtest call that
    touched this slug, ordered newest first."""
    runs = list_version_runs(
        strategy_name=args.name,
        version_slug=args.slug,
        limit=args.limit,
        db_path=args.db,
    )
    if not runs:
        print(f"(no history for {args.name}/{args.slug})")
        return 0
    print(f"Run history for {args.name} / {args.slug} ({len(runs)} runs)")
    print("-" * 60)
    for r in runs:
        ts = (r.get("run_timestamp") or "")[:19]
        verdict = r.get("verdict") or "—"
        max_ret = r.get("max_return_pct")
        ret_str = f"{max_ret:+.2f}%" if max_ret is not None else "—"
        sane_glyph = "" if r.get("max_return_sane") in (None, 1) else " !"
        print(f"  {ts}  verdict={verdict:<14}  max_return={ret_str}{sane_glyph}")
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    existing = get_strategy(args.name, db_path=args.db)
    if existing is None:
        print(f"Creating new strategy row: {args.name}")
    else:
        print(f"Updating existing strategy row: {args.name}")

    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()] or None
    markets = [m.strip() for m in (args.markets or "").split(",") if m.strip()] or None

    s = StoredStrategy(
        name=args.name,
        display_name=args.display_name,
        description=args.description,
        family=args.family,
        category=args.category,
        tier=args.tier,
        markets=markets or [],
        default_timeframe=args.default_timeframe,
        status=args.status or "researching",
        tags=tags or [],
    )
    strategy_id = upsert_strategy(s, db_path=args.db)
    print(f"  id={strategy_id}")
    return 0


# ── CLI entry ─────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="strategies",
        description="Strategy storage CLI — browse and annotate strategy versions.",
    )
    ap.add_argument(
        "--db",
        default="data/trades.db",
        help="SQLite DB path (default: data/trades.db)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    # list
    p_list = sub.add_parser("list", help="list all strategies")
    p_list.add_argument("--status", default=None, help="filter by status (researching/deployable/deployed/killed)")
    p_list.add_argument("--family", default=None, help="filter by family")
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = sub.add_parser("show", help="show a single strategy with all its versions")
    p_show.add_argument("name", help="strategy name (e.g., donchian_gold)")
    p_show.set_defaults(func=cmd_show)

    # register
    p_reg = sub.add_parser("register", help="register or update a strategy row")
    p_reg.add_argument("name", help="strategy name")
    p_reg.add_argument("--display-name", default=None)
    p_reg.add_argument("--description", default=None, help="one-line description")
    p_reg.add_argument("--family", default=None, help="breakout / mean_reversion / momentum / stat_arb / ...")
    p_reg.add_argument("--category", default=None)
    p_reg.add_argument("--tier", default=None, help="Tier enum value (e.g., TIER_2)")
    p_reg.add_argument("--markets", default=None, help="comma-separated symbol list")
    p_reg.add_argument("--default-timeframe", default=None)
    p_reg.add_argument("--status", default=None, choices=["researching", "deployable", "deployed", "killed"])
    p_reg.add_argument("--tags", default=None, help="comma-separated tag list")
    p_reg.set_defaults(func=cmd_register)

    # open — launch the deep_backtest HTML report in browser
    p_open = sub.add_parser("open", help="open a version's deep_backtest HTML report in browser")
    p_open.add_argument("name", help="strategy name")
    p_open.add_argument("slug", help="version slug")
    p_open.set_defaults(func=cmd_open)

    # sync — walk router.STRATEGY_REGISTRY, upsert empty rows
    p_sync = sub.add_parser("sync", help="upsert empty rows for every strategy class in router.STRATEGY_REGISTRY")
    p_sync.set_defaults(func=cmd_sync)

    # kill — flip status to killed + link to graveyard
    p_kill = sub.add_parser("kill", help="mark a strategy as killed and link to graveyard entry")
    p_kill.add_argument("name", help="strategy name")
    p_kill.add_argument("--graveyard-id", type=int, required=True, help="strategy_graveyard.id row to link to")
    p_kill.set_defaults(func=cmd_kill)

    # vanity — list DEPLOYABLE versions with sane=False max-return cells
    p_vanity = sub.add_parser("vanity", help="list vanity-trap versions (DEPLOYABLE + sane=False)")
    p_vanity.set_defaults(func=cmd_vanity)

    # top — show the top N versions by max return or Calmar
    p_top = sub.add_parser("top", help="show the top N versions by max_return_pct (or wf_calmar with --by-calmar)")
    p_top.add_argument("--limit", type=int, default=10)
    p_top.add_argument("--verdict", default=None, help="filter by verdict (e.g., DEPLOYABLE)")
    p_top.add_argument("--by-calmar", action="store_true", help="rank by walk-forward Calmar instead of max return")
    p_top.set_defaults(func=cmd_top)

    # history — show the version_run history for a slug
    p_hist = sub.add_parser("history", help="show run history for a single version slug")
    p_hist.add_argument("name", help="strategy name")
    p_hist.add_argument("slug", help="version slug")
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
