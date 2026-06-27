"""conductor-009-474dbb: Sportsbook Odds Audit

Identify which sportsbooks consistently offer poor odds across
1X2 (h2h), BTTS, and Over/Under totals markets during WC2026,
using all raw snapshot JSONs captured in data/raw/snapshots/.

Run::

    python backtests/sportsbook_odds_audit.py [--send-telegram]

Markets covered: h2h (1X2), btts (BTTS), totals (O/U goals).
Note: correct_score / scorelines are not in the captured snapshot
data — TheOddsAPI does not serve them in the bulk /odds endpoint.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_SNAPSHOTS = _REPO / "data" / "raw" / "snapshots"

# ---------------------------------------------------------------------------
# .env loader (searches repo root then parent dirs)
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    candidates = [_REPO / ".env", _REPO.parent / ".env"]
    for p in candidates:
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
            return


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

MARKET_LABELS = {
    "h2h": "1X2 (Match Result)",
    "btts": "BTTS (Both Teams to Score)",
    "totals": "O/U Goals (Totals)",
}
TARGET_MARKETS = set(MARKET_LABELS.keys())


def _load_snapshots() -> pd.DataFrame:
    """Load all raw snapshot JSONs into one DataFrame."""
    frames: List[pd.DataFrame] = []
    files = sorted(_SNAPSHOTS.glob("oddsapi_*.json"))
    if not files:
        raise FileNotFoundError("No snapshot files found in %s" % _SNAPSHOTS)

    for f in files:
        try:
            records = json.loads(f.read_text())
            if not records:
                continue
            df = pd.DataFrame(records)
            # Keep only target markets
            if "market" in df.columns:
                df = df[df["market"].isin(TARGET_MARKETS)]
            if not df.empty:
                frames.append(df)
        except Exception:
            continue

    if not frames:
        raise ValueError("No usable data found across %d snapshot files" % len(files))

    combined = pd.concat(frames, ignore_index=True)
    # Normalise types
    combined["decimal_odds"] = pd.to_numeric(combined["decimal_odds"], errors="coerce")
    combined = combined.dropna(subset=["decimal_odds", "bookmaker_key", "market"])
    return combined


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _outcome_key(row) -> str:
    """Stable key for an outcome within a market (handles totals lines)."""
    if pd.notna(row.get("outcome_point")):
        return "%s|%.2f" % (row["outcome_name"], float(row["outcome_point"]))
    return str(row["outcome_name"])


def analyse(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """
    For each market, return a DataFrame ranked by how consistently poor
    each bookmaker's odds are.

    Columns returned per market:
        bookmaker_key, bookmaker_title,
        n_obs         — number of (snapshot, match, selection) observations
        mean_gap_pct  — average % below the best available price
        worst_rate    — % of observations where this book has the worst odds
        best_rate     — % of observations where this book has the best odds
        never_best    — True if best_rate == 0
    """
    # Build a group key per observation so we can rank across books
    df = df.copy()
    df["outcome_key"] = df.apply(_outcome_key, axis=1)

    # snapshot-level dedupe: some snapshots captured the same match
    # multiple times within seconds; normalise to 1-minute buckets
    df["retrieved_at"] = pd.to_datetime(df.get("retrieved_at"), utc=True, errors="coerce")
    df["ts_bucket"] = df["retrieved_at"].dt.floor("1min")

    group_cols = ["ts_bucket", "event_id", "market", "outcome_key"]

    # For each group compute best / worst odds
    grp = df.groupby(group_cols)["decimal_odds"]
    df["best_odds"] = grp.transform("max")
    df["worst_odds"] = grp.transform("min")
    df["n_books"] = grp.transform("count")

    # Only keep groups with ≥2 books so comparisons are meaningful
    df = df[df["n_books"] >= 2].copy()

    df["gap_pct"] = (df["best_odds"] - df["decimal_odds"]) / df["best_odds"] * 100
    df["is_worst"] = df["decimal_odds"] == df["worst_odds"]
    df["is_best"] = df["decimal_odds"] == df["best_odds"]

    results: Dict[str, pd.DataFrame] = {}

    for market in sorted(df["market"].unique()):
        mdf = df[df["market"] == market]

        agg = (
            mdf.groupby(["bookmaker_key", "bookmaker_title"])
            .agg(
                n_obs=("gap_pct", "count"),
                mean_gap_pct=("gap_pct", "mean"),
                worst_rate=("is_worst", "mean"),
                best_rate=("is_best", "mean"),
            )
            .reset_index()
        )
        agg["worst_rate_pct"] = agg["worst_rate"] * 100
        agg["best_rate_pct"] = agg["best_rate"] * 100
        agg["never_best"] = agg["best_rate"] == 0.0
        agg = agg.sort_values("mean_gap_pct", ascending=False)
        results[market] = agg

    return results


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

TASK_TITLE = "conductor-009-474dbb"


def _bar(val: float, max_val: float = 30.0, width: int = 10) -> str:
    """Simple ASCII progress bar."""
    filled = int(round(val / max_val * width)) if max_val > 0 else 0
    filled = min(filled, width)
    return "█" * filled + "░" * (width - filled)


def format_report(results: Dict[str, pd.DataFrame], n_snapshots: int, n_matches: int) -> str:
    lines = [
        "*%s*" % TASK_TITLE,
        "Sportsbook Odds Audit — WC2026",
        "Snapshots analysed: %d | Unique matches: %d" % (n_snapshots, n_matches),
        "Note: Scorelines not in snapshot data (TheOddsAPI bulk endpoint limitation)",
        "",
    ]

    for market_key in ["h2h", "btts", "totals"]:
        if market_key not in results:
            lines.append("*%s*: no data captured" % MARKET_LABELS.get(market_key, market_key))
            lines.append("")
            continue

        df = results[market_key]
        label = MARKET_LABELS.get(market_key, market_key)
        lines.append("*%s*" % label)
        lines.append("Ranked worst → best by avg gap from best price:")
        lines.append("")

        # Header
        lines.append("%-20s %6s %8s %8s %6s" % ("Book", "Obs", "AvgGap%", "Worst%", "Best%"))
        lines.append("-" * 54)

        for _, row in df.iterrows():
            name = str(row["bookmaker_title"])[:20]
            flag = " ⚠" if row["mean_gap_pct"] > 2.0 and row["worst_rate_pct"] > 20 else ""
            lines.append(
                "%-20s %6d %7.2f%% %7.1f%% %5.1f%%%s"
                % (
                    name,
                    int(row["n_obs"]),
                    row["mean_gap_pct"],
                    row["worst_rate_pct"],
                    row["best_rate_pct"],
                    flag,
                )
            )

        lines.append("")

        # Call out the systematic offenders
        bad = df[(df["mean_gap_pct"] > 1.5) | (df["worst_rate_pct"] > 25)]
        if not bad.empty:
            lines.append("Consistent poor value (avg gap >1.5% OR worst >25% of time):")
            for _, row in bad.iterrows():
                lines.append(
                    "  • %s — avg %.2f%% below best, worst %.0f%% of obs"
                    % (row["bookmaker_title"], row["mean_gap_pct"], row["worst_rate_pct"])
                )
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram send
# ---------------------------------------------------------------------------

def _send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").split(",")[0].strip()
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping send")
        return

    sys.path.insert(0, str(_REPO / "src"))
    from wca.bot.telegram import TelegramClient
    client = TelegramClient(token=token)
    client.send_message(chat_id, text, parse_mode="Markdown")
    print("Sent to Telegram chat %s" % chat_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Sportsbook odds audit backtest")
    parser.add_argument(
        "--send-telegram",
        action="store_true",
        help="Send results to TELEGRAM_CHAT_ID",
    )
    parser.add_argument(
        "--snapshots-dir",
        default=str(_SNAPSHOTS),
        help="Override snapshot directory",
    )
    args = parser.parse_args()

    _load_dotenv()

    global _SNAPSHOTS
    _SNAPSHOTS = Path(args.snapshots_dir)

    print("Loading snapshots from %s ..." % _SNAPSHOTS)
    df = _load_snapshots()

    n_snapshots = df["ts_bucket"].nunique() if "ts_bucket" in df.columns else 0
    df["ts_bucket"] = pd.to_datetime(df.get("retrieved_at"), utc=True, errors="coerce").dt.floor("1min")
    n_snapshots = df["ts_bucket"].nunique()
    n_matches = df["event_id"].nunique()

    print(
        "Loaded %d rows across %d unique 1-min buckets, %d matches, markets: %s"
        % (len(df), n_snapshots, n_matches, ", ".join(sorted(df["market"].unique())))
    )

    results = analyse(df)

    report = format_report(results, n_snapshots, n_matches)
    print("\n" + "=" * 60)
    print(report)
    print("=" * 60)

    if args.send_telegram:
        _send_telegram(report)


if __name__ == "__main__":
    main()
