"""Strategy dependency graph generator (task #130, Group D3).

Walks the `strategies` + `strategy_versions` tables and emits a Graphviz DOT
file showing the parent → variant → verdict relationships, color-coded by
status and verdict. Useful for documentation, README diagrams, and quick
visual audits of the variant landscape.

Usage:
    PYTHONPATH=. python3 scripts/strategy_graph.py > strategies.dot
    PYTHONPATH=. python3 scripts/strategy_graph.py --output strategies.dot
    PYTHONPATH=. python3 scripts/strategy_graph.py --by-family

Render with Graphviz (must be installed separately):
    dot -Tsvg strategies.dot > strategies.svg
    dot -Tpng strategies.dot -o strategies.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure src/ is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategies.storage import (  # noqa: E402
    list_strategies,
    list_versions,
)


_VERDICT_COLOR = {
    "DEPLOYABLE": "#0a7a3a",      # green
    "NEEDS_WF": "#e8b923",         # amber
    "RESEARCH_ONLY": "#c58a00",    # darker amber
    "FAILED": "#b22222",           # red
}

_STATUS_FILL = {
    "researching": "#f4f4f4",
    "deployable": "#d4f7da",
    "deployed": "#7fdc8f",
    "killed": "#f5d0d0",
}


def _esc(s: str) -> str:
    """DOT-string escape — quotes + backslashes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _short(s: str, n: int = 40) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def build_dot(*, db_path: str, by_family: bool = False) -> str:
    strategies = list_strategies(db_path=db_path)

    lines: list[str] = []
    lines.append("digraph strategies {")
    lines.append('  rankdir=LR;')
    lines.append('  node [shape=box, style=filled, fontname="Helvetica"];')
    lines.append('  edge [color="#888888"];')
    lines.append("")

    if by_family:
        # Group by family using subgraphs
        families: dict[str, list] = {}
        for s in strategies:
            families.setdefault(s.family or "uncategorized", []).append(s)
        for fam, fam_strats in sorted(families.items()):
            cluster_id = f'cluster_{fam.replace(" ", "_")}'
            lines.append(f'  subgraph "{cluster_id}" {{')
            lines.append(f'    label="{_esc(fam)}";')
            lines.append('    style="dashed";')
            for s in fam_strats:
                _emit_strategy_node(s, lines, db_path)
            lines.append("  }")
    else:
        for s in strategies:
            _emit_strategy_node(s, lines, db_path)

    lines.append("}")
    return "\n".join(lines)


def _emit_strategy_node(s, lines: list, db_path: str) -> None:
    fill = _STATUS_FILL.get(s.status, "#f4f4f4")
    parent_id = f"strategy_{s.id}"
    parent_label = f"{_esc(s.name)}\\n[{s.status}]"
    if s.tier:
        parent_label += f"\\n{s.tier}"
    lines.append(
        f'  "{parent_id}" [label="{parent_label}", fillcolor="{fill}", shape="box", '
        f'penwidth=2];'
    )

    versions = list_versions(strategy_name=s.name, db_path=db_path)
    for v in versions:
        version_id = f"version_{v.id}"
        verdict_color = _VERDICT_COLOR.get(v.verdict or "", "#888888")
        sane_glyph = " ⚠" if v.max_return_sane is False else ""
        ret = f"{v.max_return_pct:+.1f}%" if v.max_return_pct is not None else "—"
        version_label = (
            f"{_esc(_short(v.version_slug, 35))}\\n"
            f"{v.verdict or '?'}{sane_glyph}\\n"
            f"max: {ret}"
        )
        if v.wf_continuous_calmar is not None:
            version_label += f"\\nWF Calmar: {v.wf_continuous_calmar:.1f}"
        lines.append(
            f'  "{version_id}" [label="{version_label}", '
            f'fillcolor="white", color="{verdict_color}", shape="ellipse"];'
        )
        lines.append(f'  "{parent_id}" -> "{version_id}";')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Emit a Graphviz DOT graph of strategies → versions.",
    )
    ap.add_argument("--db", default="data/trades.db", help="SQLite DB path")
    ap.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output file path (default: stdout)",
    )
    ap.add_argument(
        "--by-family",
        action="store_true",
        help="Cluster strategies by family using DOT subgraphs",
    )
    args = ap.parse_args(argv)

    dot = build_dot(db_path=args.db, by_family=args.by_family)
    if args.output:
        args.output.write_text(dot)
        print(f"Wrote {args.output}", file=sys.stderr)
        n_strategies = dot.count('shape="box"')
        n_versions = dot.count('shape="ellipse"')
        print(f"  {n_strategies} strategies, {n_versions} versions", file=sys.stderr)
    else:
        print(dot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
