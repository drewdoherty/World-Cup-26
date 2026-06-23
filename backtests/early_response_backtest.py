#!/usr/bin/env python3
"""Backtest early World Cup Alpha bets against captured closing lines.

This harness is intentionally narrow: it uses only settled ledger rows that
already have entry odds, captured closing odds, CLV, stake, and realised P&L.
Closing prices are not re-derived here.

Run:
    python3 backtests/early_response_backtest.py --write-md
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Sequence


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "wca.db")
DEFAULT_REPORT = os.path.join(
    REPO_ROOT, "docs", "research", "backtests", "early_response_backtest.md"
)

OVERALL_ROI_CLAIM = 0.1442
HIGH_EDGE_ROI_CLAIM = -0.069
HIGH_EDGE_FLOOR = 0.20

@dataclass(frozen=True)
class Bet:
    id: int
    ts_utc: str
    status: str
    stake: float
    settled_pl: float
    decimal_odds: float
    closing_odds: float
    clv: float
    edge: Optional[float]


@dataclass(frozen=True)
class Summary:
    label: str
    n: int
    stake: float
    settled_pl: float
    roi: float
    avg_clv: float
    start_date: Optional[str]
    end_date: Optional[str]


@dataclass(frozen=True)
class BacktestResult:
    source: str
    rows: List[Bet]
    overall: Summary
    buckets: List[Summary]
    verdict: str
    verdict_reasons: List[str]


def pct(x: float) -> str:
    if math.isnan(x):
        return "nan"
    return f"{x * 100:.2f}%"


def _date_part(ts: str) -> str:
    return (ts or "")[:10]


def _to_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _bucket_label(edge: Optional[float]) -> str:
    if edge is None:
        return "missing edge"
    if edge < 0.0:
        return "<0%"
    if edge < 0.05:
        return "0% to <5%"
    if edge < 0.10:
        return "5% to <10%"
    if edge < HIGH_EDGE_FLOOR:
        return "10% to <20%"
    return ">=20%"


BUCKET_ORDER = [
    "missing edge",
    "<0%",
    "0% to <5%",
    "5% to <10%",
    "10% to <20%",
    ">=20%",
]


def summarize(label: str, rows: Sequence[Bet]) -> Summary:
    n = len(rows)
    stake = sum(r.stake for r in rows)
    settled_pl = sum(r.settled_pl for r in rows)
    roi = settled_pl / stake if stake > 0.0 else math.nan
    avg_clv = sum(r.clv for r in rows) / n if n else math.nan
    dates = sorted(_date_part(r.ts_utc) for r in rows if _date_part(r.ts_utc))
    return Summary(
        label=label,
        n=n,
        stake=stake,
        settled_pl=settled_pl,
        roi=roi,
        avg_clv=avg_clv,
        start_date=dates[0] if dates else None,
        end_date=dates[-1] if dates else None,
    )


def summarize_buckets(rows: Sequence[Bet]) -> List[Summary]:
    out: List[Summary] = []
    for label in BUCKET_ORDER:
        bucket_rows = [r for r in rows if _bucket_label(r.edge) == label]
        out.append(summarize(label, bucket_rows))
    return out


def load_settled_bets(db_path: str = DEFAULT_DB) -> List[Bet]:
    """Load settled, close-captured bets from the SQLite ledger."""
    if not os.path.exists(db_path):
        return []

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, ts_utc, status, stake, settled_pl, decimal_odds,
                   closing_odds, clv, ev
            FROM bets
            WHERE status IN ('won', 'lost')
              AND stake IS NOT NULL
              AND settled_pl IS NOT NULL
              AND decimal_odds IS NOT NULL
              AND closing_odds IS NOT NULL
              AND clv IS NOT NULL
            ORDER BY ts_utc, id
            """
        ).fetchall()
    finally:
        con.close()

    bets: List[Bet] = []
    for row in rows:
        stake = _to_float(row["stake"])
        settled_pl = _to_float(row["settled_pl"])
        decimal_odds = _to_float(row["decimal_odds"])
        closing_odds = _to_float(row["closing_odds"])
        clv = _to_float(row["clv"])
        edge = _to_float(row["ev"])
        if (
            stake is None
            or stake <= 0.0
            or settled_pl is None
            or decimal_odds is None
            or decimal_odds <= 0.0
            or closing_odds is None
            or closing_odds <= 0.0
            or clv is None
        ):
            continue
        bets.append(
            Bet(
                id=int(row["id"]),
                ts_utc=str(row["ts_utc"]),
                status=str(row["status"]),
                stake=stake,
                settled_pl=settled_pl,
                decimal_odds=decimal_odds,
                closing_odds=closing_odds,
                clv=clv,
                edge=edge,
            )
        )
    return bets


def verdict_for(
    overall: Summary,
    buckets: Sequence[Summary],
    min_overall_n: int,
    min_bucket_n: int,
    tolerance_pct_points: float,
) -> tuple[str, List[str]]:
    tolerance = tolerance_pct_points / 100.0
    high_edge = next((b for b in buckets if b.label == ">=20%"), summarize(">=20%", []))

    reasons: List[str] = []
    if overall.n < min_overall_n:
        reasons.append(f"overall n={overall.n} is below min_overall_n={min_overall_n}")
    if high_edge.n < min_bucket_n:
        reasons.append(f">=20% edge n={high_edge.n} is below min_bucket_n={min_bucket_n}")
    if reasons:
        return "insufficient sample", reasons

    overall_ok = abs(overall.roi - OVERALL_ROI_CLAIM) <= tolerance
    high_edge_ok = abs(high_edge.roi - HIGH_EDGE_ROI_CLAIM) <= tolerance

    reasons.append(
        "overall ROI %s vs claim %s"
        % (pct(overall.roi), pct(OVERALL_ROI_CLAIM))
    )
    reasons.append(
        ">=20%% edge ROI %s vs claim %s"
        % (pct(high_edge.roi), pct(HIGH_EDGE_ROI_CLAIM))
    )
    if overall_ok and high_edge_ok:
        return "reproduced", reasons
    return "refuted", reasons


def run_backtest(
    db_path: str = DEFAULT_DB,
    min_overall_n: int = 1,
    min_bucket_n: int = 1,
    tolerance_pct_points: float = 0.05,
) -> BacktestResult:
    rows = load_settled_bets(db_path)
    overall = summarize("overall", rows)
    buckets = summarize_buckets(rows)
    verdict, reasons = verdict_for(
        overall=overall,
        buckets=buckets,
        min_overall_n=min_overall_n,
        min_bucket_n=min_bucket_n,
        tolerance_pct_points=tolerance_pct_points,
    )
    return BacktestResult(
        source=db_path,
        rows=rows,
        overall=overall,
        buckets=buckets,
        verdict=verdict,
        verdict_reasons=reasons,
    )


def _summary_line(summary: Summary) -> str:
    date_range = (
        f"{summary.start_date} to {summary.end_date}"
        if summary.start_date and summary.end_date
        else "n/a"
    )
    return (
        f"{summary.label:12} n={summary.n:3d}  dates={date_range:23}  "
        f"stake={summary.stake:8.2f}  P&L={summary.settled_pl:8.2f}  "
        f"ROI={pct(summary.roi):>8}  avg CLV={pct(summary.avg_clv):>8}"
    )


def format_text(result: BacktestResult) -> str:
    lines = [
        "Early response backtest",
        f"Data source: {result.source}",
        "Scope: settled won/lost bets with entry odds, captured closing_odds, clv, stake, and settled_pl.",
        "",
        _summary_line(result.overall),
        "",
        "Model-edge cohorts:",
    ]
    lines.extend(_summary_line(bucket) for bucket in result.buckets)
    lines.extend(["", f"Verdict: {result.verdict}"])
    lines.extend(f"- {reason}" for reason in result.verdict_reasons)
    return "\n".join(lines)


def format_markdown(result: BacktestResult) -> str:
    lines = [
        "# Early Response Backtest",
        "",
        "## Data Source",
        "",
        f"- Ledger: `{os.path.relpath(result.source, REPO_ROOT) if os.path.isabs(result.source) else result.source}`",
        "- Rows: settled `won`/`lost` bets with non-null `decimal_odds`, `closing_odds`, `clv`, `stake`, and `settled_pl`.",
        "- Model-edge buckets use the stored `ev` field where present; rows without `ev` are reported in `missing edge`.",
        "- Closing line policy: uses the already-captured `closing_odds` and `clv` fields on settled bets; this report does not re-derive closing prices.",
        "- Related committed analysis file: `data/analysis/clv_by_bet.csv` documents the CLV-by-bet convention but does not contain stake/P&L, so ROI is computed from the ledger.",
        "",
        "## Results",
        "",
        "| Cohort | n | Date range | Stake | P&L | ROI | Avg CLV |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for summary in [result.overall, *result.buckets]:
        date_range = (
            f"{summary.start_date} to {summary.end_date}"
            if summary.start_date and summary.end_date
            else "n/a"
        )
        lines.append(
            "| %s | %d | %s | %.2f | %.2f | %s | %s |"
            % (
                summary.label,
                summary.n,
                date_range,
                summary.stake,
                summary.settled_pl,
                pct(summary.roi),
                pct(summary.avg_clv),
            )
        )
    lines.extend(
        [
            "",
            "## Verdict",
            "",
            f"**{result.verdict}**",
            "",
        ]
    )
    lines.extend(f"- {reason}" for reason in result.verdict_reasons)
    lines.extend(
        [
            "",
            "Claims under test:",
            "",
            f"- Overall ROI: `{pct(OVERALL_ROI_CLAIM)}`",
            f"- `>=20%` model-edge cohort ROI: `{pct(HIGH_EDGE_ROI_CLAIM)}`",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(path: str, result: BacktestResult) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(format_markdown(result))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite ledger path")
    p.add_argument("--write-md", action="store_true", help="Write markdown report")
    p.add_argument("--md", default=DEFAULT_REPORT, help="Markdown report path")
    p.add_argument("--min-overall-n", type=int, default=1)
    p.add_argument("--min-bucket-n", type=int, default=1)
    p.add_argument(
        "--tolerance-pct-points",
        type=float,
        default=0.05,
        help="Reproduction tolerance in percentage points",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_backtest(
        db_path=args.db,
        min_overall_n=args.min_overall_n,
        min_bucket_n=args.min_bucket_n,
        tolerance_pct_points=args.tolerance_pct_points,
    )
    print(format_text(result))
    if args.write_md:
        write_report(args.md, result)
        print(f"\nWrote {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
