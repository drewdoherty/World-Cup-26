#!/usr/bin/env python3
"""
World Cup Alpha — Kelly Criterion Equity Analysis
Generates a PDF comparing Full / Half / Quarter Kelly sizing
over the actual bet history, starting from $1 000 USD.

FX: 1 GBP = 1.33 USD

USAGE (from repo root):
  pip3 install matplotlib        # first time only
  python3 scripts/generate_kelly_analysis.py
  open data/analysis/kelly_equity_analysis.pdf
"""

import json
import math
import os
import sys
from datetime import datetime

try:
    import matplotlib
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "matplotlib"])
    import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.gridspec as gridspec
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STARTING_BANKROLL = 1000.0      # USD
FX_GBP_USD       = 1.33         # 1 GBP → USD
KELLY_CAP        = 0.05         # hard cap per bet (5 % of bankroll)

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "site", "data.json")
OUT_PDF   = os.path.join(os.path.dirname(__file__), "..", "data", "analysis", "kelly_equity_analysis.pdf")

# Colours
C_FULL   = "#e74c3c"   # red   — full Kelly
C_HALF   = "#f39c12"   # amber — half Kelly
C_QTR    = "#27ae60"   # green — quarter Kelly (actual strategy)
C_ACTUAL = "#2980b9"   # blue  — actual bets at real stakes
C_OFFER  = "#9b59b6"   # purple
C_PUNT   = "#95a5a6"   # grey


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
with open(DATA_FILE) as fh:
    data = json.load(fh)

summary_totals = data["totals"]
clv_summary    = data["clv"]
source_summary = data["source_summary"]


def to_usd(amount, currency):
    return amount * FX_GBP_USD if currency == "GBP" else float(amount)


def parse_bets(positions_list, include_open=False):
    bets = []
    for b in positions_list:
        status = b.get("status", "open")
        if not include_open and status == "open":
            continue
        if status == "void":
            continue

        currency = b.get("currency", "GBP")
        stake    = float(b.get("stake", 0) or 0)
        pl_raw   = float(b.get("pl", 0) or 0)
        odds     = float(b.get("decimal_odds", 0) or 0)
        prob     = b.get("model_prob")
        if prob is not None:
            prob = float(prob)

        ts = b.get("ts_utc") or b.get("settled_ts") or ""
        if not ts:
            ts = "2026-06-24T00:00:00"

        bets.append(dict(
            id           = b.get("id"),
            ts           = ts[:19],
            match        = b.get("match", b.get("match_id", "")),
            selection    = b.get("selection", ""),
            odds         = odds,
            stake_usd    = to_usd(stake, currency),
            model_prob   = prob,
            source       = b.get("source", "punt"),
            currency     = currency,
            status       = status,
            pl_usd       = to_usd(pl_raw, currency),
            clv          = b.get("clv"),
            platform     = b.get("platform", ""),
        ))

    bets.sort(key=lambda x: x["ts"])
    return bets


closed_bets = parse_bets(data.get("closed_positions", []))
open_positions = parse_bets(data.get("positions", []), include_open=True)

# Separate by source
model_bets = [b for b in closed_bets if b["source"] == "model"]
offer_bets = [b for b in closed_bets if b["source"] == "offer"]
punt_bets  = [b for b in closed_bets if b["source"] == "punt"]
hedge_bets = [b for b in closed_bets if b["source"] == "hedge"]


# ---------------------------------------------------------------------------
# Kelly helpers
# ---------------------------------------------------------------------------
def full_kelly(prob, odds):
    """Edge / (Odds - 1).  Returns 0 if no edge or no prob."""
    if prob is None or prob <= 0 or odds <= 1.0:
        return 0.0
    f = (prob * odds - 1.0) / (odds - 1.0)
    return max(0.0, f)


def sim_kelly(bets, kelly_mult, start=STARTING_BANKROLL):
    """
    Simulate bankroll evolution.
    Model bets: sized at kelly_mult * full_kelly, capped at KELLY_CAP.
    Non-model bets: included at actual stake as fraction of $1000, scaled
                    by running bankroll (proportional flat sizing).
    Offer/free-bets with pl=0 on loss: no bankroll deduction on loss.
    """
    bankroll = start
    curve    = [bankroll]
    events   = []

    for b in bets:
        br = bankroll
        p, o = b["model_prob"], b["odds"]
        source = b["source"]

        if o <= 0:
            curve.append(bankroll)
            events.append(dict(**b, sim_stake=0, sim_pl=0))
            continue

        # Determine simulated stake
        if p is not None and p > 0 and o > 1:
            f = full_kelly(p, o) * kelly_mult
            f = min(f, KELLY_CAP)
            sim_stake = f * br
        else:
            # non-model: use actual proportion × running bankroll
            base_frac = b["stake_usd"] / STARTING_BANKROLL
            sim_stake = base_frac * br

        sim_stake = max(0.0, sim_stake)

        # Detect free-bet (offer + zero or positive pl only on win)
        # A real free bet: loss gives pl=0, win gives pl=(odds-1)*stake
        is_snr_free_bet = (
            source == "offer"
            and b["status"] == "lost"
            and abs(b["pl_usd"]) < 0.01
        )

        if is_snr_free_bet:
            sim_pl = 0.0   # stake not at risk
        elif b["status"] == "won":
            sim_pl = sim_stake * (o - 1.0)
        else:
            sim_pl = -sim_stake

        bankroll += sim_pl
        bankroll  = max(bankroll, 0.01)  # can't go below 1 cent
        curve.append(bankroll)
        events.append(dict(**b, sim_stake=sim_stake, sim_pl=sim_pl))

    return np.array(curve), events


# ---------------------------------------------------------------------------
# Run three simulations + actual
# ---------------------------------------------------------------------------
curve_full, events_full = sim_kelly(closed_bets, 1.0)
curve_half, events_half = sim_kelly(closed_bets, 0.5)
curve_qtr,  events_qtr  = sim_kelly(closed_bets, 0.25)

# Actual performance curve: cumulative P&L added to $1 000
cumulative_pl = np.cumsum([0.0] + [b["pl_usd"] for b in closed_bets])
curve_actual  = STARTING_BANKROLL + cumulative_pl

# Bet indices and dates
n = len(closed_bets)
bet_nums  = np.arange(n + 1)
dates     = [b["ts"][:10] for b in closed_bets]
unique_dates = sorted(set(dates))


def max_drawdown(curve):
    peak = curve[0]
    mdd  = 0.0
    for v in curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > mdd:
            mdd = dd
    return mdd


def sharpe_ratio(curve):
    returns = np.diff(curve) / curve[:-1]
    if returns.std() == 0:
        return 0.0
    return returns.mean() / returns.std() * math.sqrt(len(returns))


stats = {}
for label, curve, events in [
    ("Full Kelly",    curve_full, events_full),
    ("Half Kelly",    curve_half, events_half),
    ("Quarter Kelly", curve_qtr,  events_qtr),
    ("Actual",        curve_actual, None),
]:
    final  = curve[-1]
    ret_pct = (final / STARTING_BANKROLL - 1) * 100
    mdd    = max_drawdown(curve) * 100
    sharpe = sharpe_ratio(curve)
    stats[label] = dict(final=final, ret_pct=ret_pct, mdd=mdd, sharpe=sharpe)


# ---------------------------------------------------------------------------
# Compute CLV stats
# ---------------------------------------------------------------------------
clv_vals = [b["clv"] for b in closed_bets if b["clv"] is not None]
avg_clv  = sum(clv_vals) / len(clv_vals) if clv_vals else 0
pct_beat = sum(1 for c in clv_vals if c > 0) / len(clv_vals) if clv_vals else 0

source_pl = {}
for b in closed_bets:
    src = b["source"]
    source_pl.setdefault(src, {"pl": 0.0, "n": 0, "won": 0})
    source_pl[src]["pl"] += b["pl_usd"]
    source_pl[src]["n"]  += 1
    if b["status"] == "won":
        source_pl[src]["won"] += 1

n_won  = sum(1 for b in closed_bets if b["status"] == "won")
n_lost = sum(1 for b in closed_bets if b["status"] == "lost")
n_settled = n_won + n_lost
win_rate = n_won / n_settled if n_settled else 0

# Rolling win rate (10-bet window)
outcomes = [1 if b["status"] == "won" else 0 for b in closed_bets]
rolling_wr = []
win_dts = []
window = 10
for i in range(window - 1, len(outcomes)):
    rolling_wr.append(sum(outcomes[i - window + 1: i + 1]) / window)
    win_dts.append(i)

# Open P&L (mark-to-market)
open_pm_pnl = sum(
    float(p.get("cash_pnl", 0) or 0)
    for p in data.get("positions", [])
    if p.get("currency") == "USD"
)


# ---------------------------------------------------------------------------
# Build PDF
# ---------------------------------------------------------------------------
os.makedirs(os.path.dirname(OUT_PDF), exist_ok=True)

with PdfPages(OUT_PDF) as pdf:

    # ── Helper ──────────────────────────────────────────────────────────────
    def new_page(title=None, figsize=(11.69, 8.27)):
        fig = plt.figure(figsize=figsize)
        if title:
            fig.suptitle(title, fontsize=14, fontweight="bold", y=0.97)
        return fig

    def footer(fig, page):
        fig.text(
            0.5, 0.01,
            f"World Cup Alpha 2026 — Kelly Equity Analysis — Generated {datetime.utcnow():%Y-%m-%d %H:%M} UTC — Page {page}",
            ha="center", fontsize=7, color="#888"
        )

    # =========================================================================
    # PAGE 1 — Cover / Executive Summary
    # =========================================================================
    fig = new_page(figsize=(11.69, 8.27))
    ax  = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")

    # Dark header band
    ax.add_patch(mpatches.FancyBboxPatch(
        (0.0, 0.78), 1.0, 0.22, boxstyle="square,pad=0",
        facecolor="#1a252f", edgecolor="none", transform=ax.transAxes
    ))
    ax.text(0.5, 0.90, "⚽  World Cup Alpha 2026", fontsize=24, fontweight="bold",
            color="white", ha="center", va="center", transform=ax.transAxes)
    ax.text(0.5, 0.83, "Kelly Criterion Equity Analysis  ·  $1,000 Starting Bankroll",
            fontsize=14, color="#bdc3c7", ha="center", va="center", transform=ax.transAxes)

    # Summary boxes
    box_data = [
        ("Tournament Window",  "Jun 11 – Jun 24, 2026"),
        ("Total Bets Settled", f"{n_settled}  (W:{n_won} / L:{n_lost})"),
        ("Win Rate",           f"{win_rate:.1%}"),
        ("FX Rate Applied",    "$1.33 = £1"),
        ("Actual P&L (USD)",   f"${curve_actual[-1] - STARTING_BANKROLL:+.2f}"),
        ("CLV (28 bets)",      f"{avg_clv:+.1%}  ({pct_beat:.0%} beat close)"),
        ("Open Positions",     f"{len(data.get('positions', []))} (PM Mark-to-Mkt: ${open_pm_pnl:+.2f})"),
        ("Strategy Used",      "¼ Kelly  (scaling ladder: ¼ → ½ → ½ max at 100 settled bets)"),
    ]
    ncols, nrows = 2, 4
    for i, (label, val) in enumerate(box_data):
        col = i % ncols
        row = i // ncols
        x0  = 0.05 + col * 0.48
        y0  = 0.65 - row * 0.135
        ax.add_patch(mpatches.FancyBboxPatch(
            (x0, y0 - 0.09), 0.44, 0.10,
            boxstyle="round,pad=0.01", linewidth=1,
            facecolor="#f4f6f7", edgecolor="#bdc3c7",
            transform=ax.transAxes
        ))
        ax.text(x0 + 0.02, y0 - 0.015, label, fontsize=9, color="#7f8c8d",
                transform=ax.transAxes, fontweight="bold")
        ax.text(x0 + 0.02, y0 - 0.055, val,   fontsize=11, color="#2c3e50",
                transform=ax.transAxes, fontweight="bold")

    # Final bankroll table
    table_y = 0.12
    ax.text(0.5, table_y + 0.07, "Simulated Final Bankroll from $1,000 (settled bets only)",
            fontsize=11, fontweight="bold", ha="center", transform=ax.transAxes, color="#2c3e50")
    cols_h = ["Strategy", "Final Bankroll", "Return %", "Max Drawdown", "Approx Sharpe"]
    col_xs = [0.07, 0.28, 0.45, 0.60, 0.78]
    for cx, ch in zip(col_xs, cols_h):
        ax.text(cx, table_y, ch, fontsize=9, fontweight="bold", color="#2c3e50",
                transform=ax.transAxes)
    colors_ = [C_FULL, C_HALF, C_QTR, C_ACTUAL]
    for j, (lbl, col) in enumerate(zip(stats.keys(), colors_)):
        s = stats[lbl]
        row_y = table_y - 0.045 * (j + 1)
        vals = [lbl, f"${s['final']:,.2f}", f"{s['ret_pct']:+.1f}%",
                f"{s['mdd']:.1f}%", f"{s['sharpe']:.2f}"]
        for cx, v in zip(col_xs, vals):
            ax.text(cx, row_y, v, fontsize=10, color=col if cx == col_xs[0] else "#2c3e50",
                    fontweight="bold" if cx == col_xs[0] else "normal",
                    transform=ax.transAxes)

    ax.text(0.5, 0.015, "Disclaimer: This analysis is for personal record-keeping only. Past simulated results do not guarantee future outcomes.",
            fontsize=7, color="#aaa", ha="center", transform=ax.transAxes)
    footer(fig, 1)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)

    # =========================================================================
    # PAGE 2 — Main Equity Curves
    # =========================================================================
    fig, ax = plt.subplots(figsize=(11.69, 8.27))
    fig.suptitle("Equity Curves: Full / Half / Quarter Kelly vs Actual  (Starting $1,000)", fontsize=13, fontweight="bold")

    ax.plot(bet_nums, curve_full,   lw=2.0, color=C_FULL,   label="Full Kelly  (1×)",   zorder=4)
    ax.plot(bet_nums, curve_half,   lw=2.0, color=C_HALF,   label="Half Kelly  (½×)",   zorder=3)
    ax.plot(bet_nums, curve_qtr,    lw=2.5, color=C_QTR,    label="Quarter Kelly (¼×) ← actual", zorder=5)
    ax.plot(bet_nums, curve_actual, lw=1.5, color=C_ACTUAL, label="Actual bets (real stakes → $1k base)", linestyle="--", zorder=2)
    ax.axhline(STARTING_BANKROLL, color="#bdc3c7", linewidth=0.8, linestyle=":", zorder=1)

    ax.fill_between(bet_nums, STARTING_BANKROLL, curve_qtr, where=curve_qtr >= STARTING_BANKROLL,
                    alpha=0.08, color=C_QTR)
    ax.fill_between(bet_nums, STARTING_BANKROLL, curve_qtr, where=curve_qtr < STARTING_BANKROLL,
                    alpha=0.08, color=C_FULL)

    # Annotate major events
    annotations = []
    for i, b in enumerate(closed_bets):
        if abs(b["pl_usd"]) > 20 or (b.get("clv") and abs(b["clv"]) > 0.4):
            match_short = b["match"][:30] if b["match"] else b["selection"][:30]
            pl_str = f"+${b['pl_usd']:.0f}" if b["pl_usd"] > 0 else f"-${abs(b['pl_usd']):.0f}"
            annotations.append((i + 1, curve_qtr[i + 1], match_short, pl_str))

    for ann_x, ann_y, ann_match, ann_pl in annotations[:8]:
        ax.annotate(
            f"{ann_match}\n{ann_pl}",
            xy=(ann_x, ann_y), xytext=(ann_x + 1, ann_y + 40),
            fontsize=6.5, color="#444",
            arrowprops=dict(arrowstyle="-", color="#999", lw=0.5),
        )

    ax.set_xlabel("Bet Number (chronological)", fontsize=11)
    ax.set_ylabel("Bankroll (USD)", fontsize=11)
    ax.legend(fontsize=10, loc="upper left")
    ax.grid(True, alpha=0.3)
    ax.set_xlim(0, n)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))

    # Date labels on x-axis
    date_ticks = []
    date_labels = []
    for i, b in enumerate(closed_bets):
        d = b["ts"][:10]
        if not date_ticks or d != closed_bets[date_ticks[-1]]["ts"][:10]:
            date_ticks.append(i + 1)
            date_labels.append(d[5:])   # MM-DD

    ax.set_xticks(date_ticks[::max(1, len(date_ticks) // 12)])
    ax.set_xticklabels(date_labels[::max(1, len(date_ticks) // 12)], rotation=45, fontsize=8)

    footer(fig, 2)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)

    # =========================================================================
    # PAGE 3 — Kelly Sizing Explanation & Final Bankroll Bar Chart
    # =========================================================================
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.suptitle("Kelly Criterion — Sizing Explanation & Final Bankroll Comparison", fontsize=13, fontweight="bold")
    gs = gridspec.GridSpec(1, 2, figure=fig, wspace=0.35, left=0.07, right=0.96, top=0.87, bottom=0.12)

    # Left: text explanation
    ax_txt = fig.add_subplot(gs[0, 0])
    ax_txt.axis("off")
    explanation = (
        "THE KELLY CRITERION\n"
        "─────────────────────\n\n"
        "The Kelly formula gives the optimal fraction of\n"
        "bankroll to stake on a +EV bet:\n\n"
        "   f* = (p × o − 1) / (o − 1)\n\n"
        "  p = model win probability\n"
        "  o = decimal odds\n\n"
        "A cap of 5% per bet is applied to limit\n"
        "single-event ruin risk.\n\n"
        "───  Strategy Fractions  ───\n\n"
        "  Full Kelly  (1×)   →  maximises log-wealth\n"
        "                          growth in theory, but\n"
        "                          can cause large drawdowns\n\n"
        "  Half Kelly  (½×)   →  ~75% of Full Kelly growth\n"
        "                          with ~¼ the variance\n\n"
        "  Quarter Kelly (¼×) →  used so far this tournament\n"
        "                          ~44% of FK growth, very\n"
        "                          low volatility\n\n"
        "─────────────────────────────\n"
        "TOURNAMENT APPROACH\n\n"
        "Started at ¼ Kelly (rung 0).\n"
        "Ladder increases to ⅓ after 50 settled\n"
        "bets with positive rolling CLV, and ½\n"
        "after 100 settled bets.\n\n"
        f"Current settled: {n_settled} bets\n"
        f"Current rung: 0  (¼ Kelly)\n"
        f"Next rung at: 50 bets + positive CLV\n"
    )
    ax_txt.text(0.02, 0.98, explanation, transform=ax_txt.transAxes,
                fontsize=9.5, family="monospace", va="top",
                bbox=dict(facecolor="#f8f9fa", edgecolor="#dee2e6", boxstyle="round,pad=0.4"))

    # Right: bar chart of final bankrolls
    ax_bar = fig.add_subplot(gs[0, 1])
    labels_bar = list(stats.keys())
    finals_bar = [stats[l]["final"] for l in labels_bar]
    colors_bar = [C_FULL, C_HALF, C_QTR, C_ACTUAL]
    bars = ax_bar.bar(labels_bar, finals_bar, color=colors_bar, width=0.5, alpha=0.85, edgecolor="white", linewidth=1.5)
    ax_bar.axhline(STARTING_BANKROLL, color="#7f8c8d", linewidth=1, linestyle="--", label="$1,000 start")

    for bar, val in zip(bars, finals_bar):
        ax_bar.text(bar.get_x() + bar.get_width() / 2, val + 5,
                    f"${val:,.0f}", ha="center", va="bottom", fontsize=10, fontweight="bold")

    ret_labels = [f"{stats[l]['ret_pct']:+.1f}%" for l in labels_bar]
    ax_bar.set_title("Final Simulated Bankroll (from $1,000)", fontsize=11, fontweight="bold")
    ax_bar.set_ylabel("USD", fontsize=11)
    ax_bar.set_ylim(0, max(finals_bar) * 1.18)
    ax_bar.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax_bar.legend(fontsize=9)
    ax_bar.grid(True, axis="y", alpha=0.3)

    # Return % sub-labels
    for i, (bar, rl) in enumerate(zip(bars, ret_labels)):
        ax_bar.text(bar.get_x() + bar.get_width() / 2, 30,
                    rl, ha="center", va="bottom", fontsize=9.5, color="white", fontweight="bold")

    footer(fig, 3)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)

    # =========================================================================
    # PAGE 4 — Per-Bet P&L Waterfall (sorted chronologically)
    # =========================================================================
    fig, axes = plt.subplots(2, 1, figsize=(11.69, 8.27), gridspec_kw={"height_ratios": [3, 1]})
    fig.suptitle("Per-Bet P&L (USD) — Settled Bets Only", fontsize=13, fontweight="bold")
    fig.subplots_adjust(top=0.90, bottom=0.12, hspace=0.35)

    ax_wf = axes[0]
    ax_cum = axes[1]

    xs = np.arange(n)
    pls = np.array([b["pl_usd"] for b in closed_bets])
    src_colors = [
        C_QTR    if b["source"] == "model"  else
        C_OFFER  if b["source"] == "offer"  else
        C_PUNT   if b["source"] == "punt"   else
        "#e67e22" if b["source"] == "hedge" else
        "#95a5a6"
        for b in closed_bets
    ]

    ax_wf.bar(xs, pls, color=src_colors, width=0.8, alpha=0.85)
    ax_wf.axhline(0, color="#333", linewidth=0.8)
    ax_wf.set_ylabel("P&L per bet (USD)", fontsize=10)
    ax_wf.set_title("Individual Bet P&L by Source", fontsize=10)
    ax_wf.grid(True, axis="y", alpha=0.3)
    ax_wf.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))

    legend_patches = [
        mpatches.Patch(color=C_QTR,    label="Model"),
        mpatches.Patch(color=C_OFFER,  label="Offer / Promo"),
        mpatches.Patch(color=C_PUNT,   label="Punt"),
        mpatches.Patch(color="#e67e22", label="Hedge"),
    ]
    ax_wf.legend(handles=legend_patches, fontsize=8, loc="upper right")

    # Annotate big wins
    for i, (pl, b) in enumerate(zip(pls, closed_bets)):
        if abs(pl) >= 30:
            ax_wf.annotate(
                f"{b['selection'][:20]}\n${pl:+.0f}",
                xy=(i, pl), xytext=(i, pl + (15 if pl > 0 else -20)),
                fontsize=6, ha="center", color="#222",
                arrowprops=dict(arrowstyle="-", color="#aaa", lw=0.5),
            )

    # Cumulative P&L
    ax_cum.fill_between(xs, 0, np.cumsum(pls), where=np.cumsum(pls) >= 0,
                        alpha=0.4, color=C_QTR, label="Cumulative profit")
    ax_cum.fill_between(xs, 0, np.cumsum(pls), where=np.cumsum(pls) < 0,
                        alpha=0.4, color=C_FULL, label="Cumulative loss")
    ax_cum.plot(xs, np.cumsum(pls), color="#2c3e50", lw=1.5)
    ax_cum.axhline(0, color="#333", lw=0.8, linestyle="--")
    ax_cum.set_ylabel("Cumulative P&L (USD)", fontsize=9)
    ax_cum.set_xlabel("Bet # (chronological)", fontsize=9)
    ax_cum.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax_cum.legend(fontsize=8)
    ax_cum.grid(True, alpha=0.3)

    footer(fig, 4)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)

    # =========================================================================
    # PAGE 5 — Drawdown & Rolling Win Rate
    # =========================================================================
    fig, axes = plt.subplots(1, 3, figsize=(11.69, 8.27))
    fig.suptitle("Risk Metrics", fontsize=13, fontweight="bold")
    fig.subplots_adjust(left=0.07, right=0.97, top=0.87, bottom=0.12, wspace=0.35)

    # ── Drawdown chart ──
    ax_dd = axes[0]
    for (lbl, curve, col) in [
        ("Full Kelly",    curve_full,   C_FULL),
        ("Half Kelly",    curve_half,   C_HALF),
        ("Quarter Kelly", curve_qtr,    C_QTR),
        ("Actual",        curve_actual, C_ACTUAL),
    ]:
        peak = np.maximum.accumulate(curve)
        dd   = (curve - peak) / peak * 100
        ax_dd.plot(bet_nums, dd, lw=1.8, color=col, label=lbl)

    ax_dd.axhline(0, color="#333", lw=0.8)
    ax_dd.set_xlabel("Bet #", fontsize=9)
    ax_dd.set_ylabel("Drawdown (%)", fontsize=9)
    ax_dd.set_title("Drawdown from Peak", fontsize=10, fontweight="bold")
    ax_dd.legend(fontsize=7)
    ax_dd.grid(True, alpha=0.3)
    ax_dd.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f}%"))

    # ── Rolling 10-bet win rate ──
    ax_wr = axes[1]
    if rolling_wr:
        ax_wr.plot(win_dts, rolling_wr, color="#2980b9", lw=2)
        ax_wr.axhline(win_rate, color="#e74c3c", lw=1.2, linestyle="--",
                      label=f"Overall {win_rate:.1%}")
        ax_wr.axhline(0.5, color="#bdc3c7", lw=0.8, linestyle=":")
        ax_wr.fill_between(win_dts, win_rate, rolling_wr,
                           where=np.array(rolling_wr) >= win_rate,
                           alpha=0.2, color=C_QTR)
        ax_wr.fill_between(win_dts, win_rate, rolling_wr,
                           where=np.array(rolling_wr) < win_rate,
                           alpha=0.2, color=C_FULL)
    ax_wr.set_title("Rolling Win Rate (10-bet window)", fontsize=10, fontweight="bold")
    ax_wr.set_xlabel("Bet #", fontsize=9)
    ax_wr.set_ylabel("Win Rate", fontsize=9)
    ax_wr.set_ylim(0, 1)
    ax_wr.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0%}"))
    ax_wr.legend(fontsize=8)
    ax_wr.grid(True, alpha=0.3)

    # ── Max drawdown summary bars ──
    ax_mdd = axes[2]
    mdd_labels = ["Full Kelly", "Half Kelly", "Quarter Kelly", "Actual"]
    mdd_vals   = [stats[l]["mdd"] for l in mdd_labels]
    mdd_cols   = [C_FULL, C_HALF, C_QTR, C_ACTUAL]
    mdd_bars   = ax_mdd.barh(mdd_labels, mdd_vals, color=mdd_cols, alpha=0.85,
                              edgecolor="white", linewidth=1.2)
    for bar, val in zip(mdd_bars, mdd_vals):
        ax_mdd.text(val + 0.3, bar.get_y() + bar.get_height() / 2,
                    f"{val:.1f}%", va="center", fontsize=10, fontweight="bold")
    ax_mdd.set_xlabel("Max Drawdown (%)", fontsize=9)
    ax_mdd.set_title("Max Drawdown by Strategy", fontsize=10, fontweight="bold")
    ax_mdd.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f}%"))
    ax_mdd.grid(True, axis="x", alpha=0.3)

    footer(fig, 5)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)

    # =========================================================================
    # PAGE 6 — CLV & Source Breakdown
    # =========================================================================
    fig, axes = plt.subplots(1, 3, figsize=(11.69, 8.27))
    fig.suptitle("Closing-Line Value (CLV) & Bet Source Analysis", fontsize=13, fontweight="bold")
    fig.subplots_adjust(left=0.07, right=0.97, top=0.87, bottom=0.14, wspace=0.38)

    # ── CLV distribution ──
    ax_clv = axes[0]
    if clv_vals:
        ax_clv.hist(clv_vals, bins=15, color=C_ACTUAL, alpha=0.75, edgecolor="white")
        ax_clv.axvline(0, color="#333", lw=0.8, linestyle="--")
        ax_clv.axvline(avg_clv, color=C_FULL, lw=2.0, linestyle="-",
                       label=f"Mean CLV: {avg_clv:+.1%}")
        ax_clv.set_title(f"CLV Distribution  (n={len(clv_vals)})", fontsize=10, fontweight="bold")
        ax_clv.set_xlabel("CLV per bet", fontsize=9)
        ax_clv.set_ylabel("Count", fontsize=9)
        ax_clv.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.0%}"))
        ax_clv.legend(fontsize=8)
        ax_clv.grid(True, alpha=0.3)
    else:
        ax_clv.text(0.5, 0.5, "No CLV data", ha="center", va="center",
                    transform=ax_clv.transAxes, fontsize=12)
        ax_clv.axis("off")

    # ── P&L by source ──
    ax_src = axes[1]
    src_keys  = [k for k in ["model", "offer", "punt", "hedge"] if k in source_pl]
    src_pls   = [source_pl[k]["pl"] for k in src_keys]
    src_ns    = [source_pl[k]["n"]  for k in src_keys]
    src_cols  = [C_QTR, C_OFFER, C_PUNT, "#e67e22"][:len(src_keys)]
    src_bars  = ax_src.bar(src_keys, src_pls, color=src_cols, alpha=0.85,
                           edgecolor="white", linewidth=1.5)
    ax_src.axhline(0, color="#333", lw=0.8)
    for bar, val, n_b in zip(src_bars, src_pls, src_ns):
        ypos = val + (5 if val >= 0 else -15)
        ax_src.text(bar.get_x() + bar.get_width() / 2, ypos,
                    f"${val:+.0f}\n(n={n_b})", ha="center", va="bottom", fontsize=9)
    ax_src.set_title("P&L by Bet Source (USD)", fontsize=10, fontweight="bold")
    ax_src.set_ylabel("USD", fontsize=9)
    ax_src.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax_src.grid(True, axis="y", alpha=0.3)

    # ── Quarter-Kelly simulated per-bet stakes histogram ──
    ax_ks = axes[2]
    kelly_stakes = []
    for e in events_qtr:
        if e["model_prob"] is not None and e["odds"] > 1:
            kelly_stakes.append(e["sim_stake"])
    if kelly_stakes:
        ax_ks.hist(kelly_stakes, bins=20, color=C_QTR, alpha=0.75, edgecolor="white")
        ax_ks.set_title("¼ Kelly Simulated Stake Distribution", fontsize=10, fontweight="bold")
        ax_ks.set_xlabel("Stake (USD)", fontsize=9)
        ax_ks.set_ylabel("Count", fontsize=9)
        ax_ks.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:.0f}"))
        ax_ks.grid(True, alpha=0.3)
        ax_ks.axvline(np.mean(kelly_stakes), color=C_FULL, lw=1.8, linestyle="--",
                      label=f"Mean: ${np.mean(kelly_stakes):.1f}")
        ax_ks.legend(fontsize=8)
    else:
        ax_ks.text(0.5, 0.5, "No Kelly stakes", ha="center", va="center",
                   transform=ax_ks.transAxes, fontsize=12)
        ax_ks.axis("off")

    footer(fig, 6)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)

    # =========================================================================
    # PAGE 7 — Open Positions & Forward-Looking
    # =========================================================================
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.suptitle("Open Positions & Tournament Outlook", fontsize=13, fontweight="bold")

    gs7 = gridspec.GridSpec(2, 2, figure=fig, wspace=0.35, hspace=0.45,
                            left=0.06, right=0.97, top=0.88, bottom=0.10)

    # Open position table
    ax_op = fig.add_subplot(gs7[0, :])
    ax_op.axis("off")
    open_pos = data.get("positions", [])
    if open_pos:
        col_headers = ["Market / Selection", "Platform", "Stake (USD)", "Entry Odds",
                       "Current Value (USD)", "Mark-to-Mkt P&L"]
        row_data = []
        for p in open_pos:
            sel   = (p.get("match", p.get("selection", ""))[:45])
            plat  = p.get("platform", "")
            stk   = f"${float(p.get('stake', 0) or 0):,.2f}"
            odds  = f"{float(p.get('decimal_odds', 0) or 0):.3f}"
            cur   = f"${float(p.get('cur_value', 0) or 0):,.2f}"
            pnl   = float(p.get('cash_pnl', 0) or 0)
            pnl_s = f"${pnl:+,.2f}"
            row_data.append([sel, plat, stk, odds, cur, pnl_s])

        tbl = ax_op.table(
            cellText=row_data,
            colLabels=col_headers,
            loc="center",
            cellLoc="left",
        )
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(7.5)
        tbl.auto_set_column_width(col=list(range(len(col_headers))))
        for (row, col), cell in tbl.get_celld().items():
            if row == 0:
                cell.set_facecolor("#2c3e50")
                cell.set_text_props(color="white", fontweight="bold")
            elif row % 2:
                cell.set_facecolor("#f4f6f7")
        ax_op.set_title("Open Positions (live mark-to-market from Polymarket API)", fontsize=10,
                         fontweight="bold", pad=4)

    # Expected bankroll vs scenarios
    ax_fwd = fig.add_subplot(gs7[1, 0])
    ax_fwd.axis("off")
    open_stake_usd = sum(
        to_usd(float(p.get("stake", 0) or 0), p.get("currency", "USD"))
        for p in open_pos
    )
    open_cur_val = sum(float(p.get("cur_value", 0) or 0) for p in open_pos
                       if p.get("currency") == "USD")

    fwd_text = (
        "FORWARD OUTLOOK\n"
        "──────────────────────────────────────\n\n"
        f"Open positions at cost:       ${open_stake_usd:>10,.2f}\n"
        f"Open positions at market:     ${open_cur_val:>10,.2f}\n"
        f"Mark-to-market P&L:           ${open_pm_pnl:>+10.2f}\n\n"
        f"Settled P&L (USD):            ${curve_actual[-1] - STARTING_BANKROLL:>+10.2f}\n\n"
        "   — If all open positions expire at 0:\n"
        f"     Bankroll (est.):  ${curve_actual[-1] - open_stake_usd:>10,.2f}\n\n"
        "   — If all open positions resolve at\n"
        "     current market value:\n"
        f"     Bankroll (est.):  ${curve_actual[-1] + open_pm_pnl:>10,.2f}\n\n"
        "CLV SUMMARY\n"
        "──────────────────────────────────────\n\n"
        f"Bets with closing odds:    {clv_summary.get('n_with_close', 0)}\n"
        f"Average CLV:               {clv_summary.get('avg_clv', 0):+.2%}\n"
        f"% beating the close:       {clv_summary.get('pct_beat_close', 0):.1%}\n\n"
        "Positive CLV is the primary quality\n"
        "signal — it means bets were placed\n"
        "at prices better than market close.\n"
        "Target: avg CLV > 0 consistently."
    )
    ax_fwd.text(0.02, 0.95, fwd_text, transform=ax_fwd.transAxes,
                fontsize=9, family="monospace", va="top",
                bbox=dict(facecolor="#f8f9fa", edgecolor="#dee2e6", boxstyle="round,pad=0.4"))

    # Venue P&L donut
    ax_donut = fig.add_subplot(gs7[1, 1])
    venue_pl  = {}
    for b in closed_bets:
        plat = b["platform"] or "Unknown"
        venue_pl.setdefault(plat, 0.0)
        venue_pl[plat] += b["pl_usd"]

    # Filter to top-6 venues by absolute P&L
    top_venues = sorted(venue_pl.items(), key=lambda x: abs(x[1]), reverse=True)[:8]
    v_labels = [v[0][:12] for v in top_venues]
    v_vals   = [abs(v[1]) for v in top_venues]
    v_pls    = [v[1] for v in top_venues]
    palette  = plt.cm.Set3(np.linspace(0, 1, len(v_labels)))

    wedges, texts, autotexts = ax_donut.pie(
        v_vals, labels=v_labels, autopct="%1.0f%%",
        startangle=90, colors=palette,
        pctdistance=0.82, labeldistance=1.08,
        wedgeprops=dict(width=0.5),
    )
    for t in texts:
        t.set_fontsize(7.5)
    for at in autotexts:
        at.set_fontsize(7)
    ax_donut.set_title("Stake Volume by Venue\n(absolute $, top 8)", fontsize=9, fontweight="bold")

    footer(fig, 7)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)

    # =========================================================================
    # PAGE 8 — Bet-by-Bet Kelly Table
    # =========================================================================
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.suptitle("Bet-by-Bet Kelly Sizing Comparison (Model Bets Only)", fontsize=13, fontweight="bold")
    ax_tbl = fig.add_subplot(1, 1, 1)
    ax_tbl.axis("off")

    model_events = [
        (b, e_q, e_h, e_f)
        for b, e_q, e_h, e_f in zip(closed_bets, events_qtr, events_half, events_full)
        if b["model_prob"] is not None and b["odds"] > 1
    ][:35]   # limit rows to fit page

    if model_events:
        tbl_headers = ["#", "Date", "Selection", "Odds", "Model p", "Full Kelly",
                       "½ Kelly Stake", "¼ Kelly Stake", "Outcome", "¼K P&L"]
        tbl_rows = []
        for i, (b, e_q, e_h, e_f) in enumerate(model_events, 1):
            fk = full_kelly(b["model_prob"], b["odds"])
            tbl_rows.append([
                str(i),
                b["ts"][:10],
                (b["selection"] or b["match"])[:22],
                f"{b['odds']:.2f}",
                f"{b['model_prob']:.2%}",
                f"{fk:.2%}",
                f"${e_h['sim_stake']:.2f}",
                f"${e_q['sim_stake']:.2f}",
                b["status"].upper(),
                f"${e_q['sim_pl']:+.2f}",
            ])

        tbl_obj = ax_tbl.table(
            cellText=tbl_rows,
            colLabels=tbl_headers,
            loc="center",
            cellLoc="center",
        )
        tbl_obj.auto_set_font_size(False)
        tbl_obj.set_fontsize(7)
        tbl_obj.auto_set_column_width(col=list(range(len(tbl_headers))))
        tbl_obj.scale(1, 1.35)

        for (row, col), cell in tbl_obj.get_celld().items():
            if row == 0:
                cell.set_facecolor("#2c3e50")
                cell.set_text_props(color="white", fontweight="bold")
            elif row % 2 == 0:
                cell.set_facecolor("#eaf2ff")
            # Colour outcome column
            if col == 8 and row > 0:
                txt = cell.get_text().get_text()
                cell.set_facecolor("#d5f5e3" if txt == "WON" else "#fdf2f8")
                cell.set_text_props(color="#1e8449" if txt == "WON" else "#922b21",
                                    fontweight="bold")

        note = f"Showing first 35 model bets (total model bets: {sum(1 for b in closed_bets if b['model_prob'] is not None)})."
        ax_tbl.text(0.5, 0.01, note, ha="center", fontsize=8, color="#888", transform=ax_tbl.transAxes)

    footer(fig, 8)
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


print(f"\n✓ PDF written → {os.path.abspath(OUT_PDF)}")

# ── Console summary ──────────────────────────────────────────────────────────
print("\n" + "═" * 60)
print(f"  WCA Kelly Equity Analysis — {datetime.utcnow():%Y-%m-%d}")
print("═" * 60)
print(f"  Bets analysed : {n_settled} settled")
print(f"  Win rate      : {win_rate:.1%}  ({n_won}W / {n_lost}L)")
print(f"  Avg CLV       : {avg_clv:+.2%} ({pct_beat:.0%} beat close, n={len(clv_vals)})")
print()
print(f"  {'Strategy':<20} {'Final ($)':>10}  {'Return':>8}  {'Max DD':>8}  {'Sharpe':>8}")
print("  " + "─" * 56)
for lbl, s in stats.items():
    print(f"  {lbl:<20} ${s['final']:>9,.2f}  {s['ret_pct']:>7.1f}%  {s['mdd']:>7.1f}%  {s['sharpe']:>8.2f}")
print("═" * 60)
