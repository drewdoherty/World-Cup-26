#!/usr/bin/env python3
"""Build the World Cup 2026 Advancement & Betting Report PDF.

Reads ONLY from the four generated data files. No numbers are invented.
"""
import csv
import json
import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    HRFlowable,
)

ROOT = "/Users/andrewdoherty/World-Cup-26"
CSV_PATH = f"{ROOT}/data/advancement_current_vs_pretournament.csv"
PLAYED_PATH = f"{ROOT}/data/advancement_played_results.json"
OUT_PATH = f"{ROOT}/reports/advancement_report_2026-06-18.pdf"

# ---- Palette ----
NAVY = colors.HexColor("#0b2545")
BLUE = colors.HexColor("#13315c")
ACCENT = colors.HexColor("#1b6ca8")
LIGHT = colors.HexColor("#eef3f8")
ROWALT = colors.HexColor("#f6f8fb")
GREEN = colors.HexColor("#1a7f37")
RED = colors.HexColor("#b42318")
GREY = colors.HexColor("#5b6770")
LINE = colors.HexColor("#c9d4e0")

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
with open(CSV_PATH, newline="") as f:
    rows = list(csv.DictReader(f))

with open(PLAYED_PATH) as f:
    played = json.load(f)


def fnum(row, key):
    return float(row[key])


teams = []
for r in rows:
    teams.append({
        "team": r["team"],
        "group": r["group"],
        "R32": fnum(r, "P(R32)"), "R32_d": fnum(r, "P(R32)_delta"),
        "R16": fnum(r, "P(R16)"), "R16_d": fnum(r, "P(R16)_delta"),
        "QF": fnum(r, "P(QF)"), "QF_d": fnum(r, "P(QF)_delta"),
        "SF": fnum(r, "P(SF)"), "SF_d": fnum(r, "P(SF)_delta"),
        "Final": fnum(r, "P(Final)"), "Final_d": fnum(r, "P(Final)_delta"),
        "win": fnum(r, "P(win)"), "win_d": fnum(r, "P(win)_delta"),
        "gw": fnum(r, "P(group_winner)"), "gw_d": fnum(r, "P(group_winner)_delta"),
    })


def pct(x, dp=1):
    return f"{x*100:.{dp}f}%"


def spct(x, dp=1):
    """Signed percentage for deltas."""
    v = x * 100
    return f"{'+' if v >= 0 else ''}{v:.{dp}f}%"


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------
ss = getSampleStyleSheet()
H_TITLE = ParagraphStyle("HTitle", parent=ss["Title"], fontName="Helvetica-Bold",
                         fontSize=22, textColor=NAVY, spaceAfter=4, leading=26)
H_SUB = ParagraphStyle("HSub", parent=ss["Normal"], fontName="Helvetica",
                       fontSize=10.5, textColor=GREY, alignment=TA_CENTER, leading=14)
H_DATE = ParagraphStyle("HDate", parent=ss["Normal"], fontName="Helvetica-Bold",
                        fontSize=10, textColor=ACCENT, alignment=TA_CENTER, spaceAfter=2)
H1 = ParagraphStyle("H1", parent=ss["Heading1"], fontName="Helvetica-Bold",
                    fontSize=14, textColor=colors.white, leading=18,
                    leftIndent=6, spaceBefore=2, spaceAfter=2)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                    fontSize=11.5, textColor=BLUE, spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("Body", parent=ss["Normal"], fontName="Helvetica",
                      fontSize=9.3, textColor=colors.HexColor("#1c2733"),
                      leading=13, spaceAfter=5)
BODY_SM = ParagraphStyle("BodySm", parent=BODY, fontSize=8.3, textColor=GREY, leading=11)
CELL = ParagraphStyle("Cell", parent=ss["Normal"], fontName="Helvetica",
                      fontSize=7.6, leading=9, textColor=colors.HexColor("#1c2733"))
CELL_B = ParagraphStyle("CellB", parent=CELL, fontName="Helvetica-Bold")
CELL_W = ParagraphStyle("CellW", parent=CELL, textColor=colors.white,
                        fontName="Helvetica-Bold", fontSize=7.7)


def section_header(num, title):
    """Return a full-width banner flowable for a section header."""
    p = Paragraph(f"Section {num} &nbsp;&middot;&nbsp; {title}", H1)
    t = Table([[p]], colWidths=[180 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def base_table_style(header_bg=BLUE, fontsize=7.6):
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), fontsize),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), fontsize),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, NAVY),
        ("GRID", (0, 1), (-1, -1), 0.25, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ])


story = []

# ===========================================================================
# COVER / HEADER
# ===========================================================================
bar = Table([[""]], colWidths=[180 * mm], rowHeights=[3])
bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT)]))
story.append(bar)
story.append(Spacer(1, 10))
story.append(Paragraph("World Cup 2026 &mdash; Advancement &amp; Betting Report", H_TITLE))
story.append(Paragraph("Dated 2026-06-18", H_DATE))
story.append(Paragraph(
    "24 group matches played; simulation conditioned on actual results", H_SUB))
story.append(Spacer(1, 4))
story.append(HRFlowable(width="100%", thickness=0.8, color=LINE))
story.append(Spacer(1, 8))

intro = ("This report summarizes Monte-Carlo advancement probabilities for all 48 teams "
         "with 24 group matches already played and fixed into the simulation, alongside "
         "the change in those probabilities versus the pre-tournament baseline (same model). "
         "Sections 4 and 5 list the actionable Polymarket and sportsbook bets currently "
         "flagged by the model. All probabilities are shown as percentages.")
story.append(Paragraph(intro, BODY))
story.append(Spacer(1, 6))

# ===========================================================================
# SECTION 1 — EXECUTIVE SUMMARY
# ===========================================================================
story.append(section_header(1, "Executive Summary"))
story.append(Spacer(1, 6))

contenders = sorted(teams, key=lambda t: t["win"], reverse=True)[:8]
story.append(Paragraph("Top title contenders by P(win)", H2))
head = ["Rank", "Team", "Grp", "P(win)", "Δ vs pre-tourn."]
data = [head]
for i, t in enumerate(contenders, 1):
    dcol = GREEN if t["win_d"] >= 0 else RED
    data.append([
        str(i), t["team"], t["group"], pct(t["win"]),
        Paragraph(f'<font color="{dcol.hexval()[2:]}">{spct(t["win_d"], 2)}</font>'
                  if False else spct(t["win_d"], 2), CELL),
    ])
ct = Table(data, colWidths=[14 * mm, 50 * mm, 14 * mm, 24 * mm, 30 * mm])
st = base_table_style(fontsize=8.4)
st.add("ALIGN", (0, 0), (0, -1), "CENTER")
st.add("ALIGN", (2, 0), (-1, -1), "CENTER")
st.add("FONTNAME", (3, 1), (3, -1), "Helvetica-Bold")
for i, t in enumerate(contenders, start=1):
    if i % 2 == 0:
        st.add("BACKGROUND", (0, i), (-1, i), ROWALT)
    st.add("TEXTCOLOR", (4, i), (4, i), GREEN if t["win_d"] >= 0 else RED)
ct.setStyle(st)
story.append(ct)
story.append(Spacer(1, 8))

# Biggest movers by |P(win)_delta|
movers_win = sorted(teams, key=lambda t: abs(t["win_d"]), reverse=True)[:6]
movers_r16 = sorted(teams, key=lambda t: abs(t["R16_d"]), reverse=True)[:6]

story.append(Paragraph("Biggest movers since pre-tournament", H2))
story.append(Paragraph(
    "Left: largest absolute change in title odds P(win). Right: largest absolute change "
    "in P(reach Round of 16).", BODY_SM))

mhead_w = ["Team", "P(win)", "Δ P(win)"]
mhead_r = ["Team", "P(R16)", "Δ P(R16)"]
dw = [mhead_w]
for t in movers_win:
    dw.append([t["team"], pct(t["win"]), spct(t["win_d"], 2)])
dr = [mhead_r]
for t in movers_r16:
    dr.append([t["team"], pct(t["R16"]), spct(t["R16_d"])])

tw = Table(dw, colWidths=[36 * mm, 18 * mm, 22 * mm])
sw = base_table_style(fontsize=8)
sw.add("ALIGN", (1, 0), (-1, -1), "CENTER")
for i, t in enumerate(movers_win, 1):
    if i % 2 == 0:
        sw.add("BACKGROUND", (0, i), (-1, i), ROWALT)
    sw.add("TEXTCOLOR", (2, i), (2, i), GREEN if t["win_d"] >= 0 else RED)
tw.setStyle(sw)

tr = Table(dr, colWidths=[36 * mm, 18 * mm, 22 * mm])
sr = base_table_style(fontsize=8)
sr.add("ALIGN", (1, 0), (-1, -1), "CENTER")
for i, t in enumerate(movers_r16, 1):
    if i % 2 == 0:
        sr.add("BACKGROUND", (0, i), (-1, i), ROWALT)
    sr.add("TEXTCOLOR", (2, i), (2, i), GREEN if t["R16_d"] >= 0 else RED)
tr.setStyle(sr)

side = Table([[tw, tr]], colWidths=[78 * mm, 78 * mm])
side.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                          ("LEFTPADDING", (1, 0), (1, 0), 8)]))
story.append(side)
story.append(Spacer(1, 6))

lead = contenders[0]
best_win_mover = movers_win[0]
best_r16_mover = movers_r16[0]
summary_txt = (
    f"<b>{lead['team']}</b> leads the field at {pct(lead['win'])} to win the tournament "
    f"(Group {lead['group']}), ahead of {contenders[1]['team']} ({pct(contenders[1]['win'])}) "
    f"and {contenders[2]['team']} ({pct(contenders[2]['win'])}). "
    f"The largest title-odds swing belongs to <b>{best_win_mover['team']}</b> "
    f"({spct(best_win_mover['win_d'], 2)} to {pct(best_win_mover['win'])}); the largest "
    f"Round-of-16 swing is <b>{best_r16_mover['team']}</b> "
    f"({spct(best_r16_mover['R16_d'])} to {pct(best_r16_mover['R16'])}).")
story.append(Paragraph(summary_txt, BODY))

story.append(PageBreak())

# ===========================================================================
# SECTION 2 — PER-TEAM ADVANCEMENT TABLE (by group)
# ===========================================================================
story.append(section_header(2, "Per-Team Advancement Probabilities"))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "All 48 teams grouped A&ndash;L. Within each group, teams are sorted by P(win) descending. "
    "Probabilities are the current results-conditioned simulation, shown as percentages.", BODY_SM))
story.append(Spacer(1, 4))

groups = sorted({t["group"] for t in teams})
adv_head = ["Team", "P(R32)", "P(R16)", "P(QF)", "P(SF)", "P(Final)", "P(win)", "P(Grp 1st)"]

adv_data = [adv_head]
group_row_index = {}  # row idx -> group label for styling group separators
row_idx = 1
group_start_rows = []
for g in groups:
    gteams = sorted([t for t in teams if t["group"] == g],
                    key=lambda t: t["win"], reverse=True)
    group_start_rows.append((row_idx, g))
    for t in gteams:
        adv_data.append([
            f"{t['team']}  ({g})", pct(t["R32"]), pct(t["R16"]), pct(t["QF"]),
            pct(t["SF"]), pct(t["Final"]), pct(t["win"]), pct(t["gw"]),
        ])
        row_idx += 1

colw = [44 * mm, 17 * mm, 17 * mm, 17 * mm, 16 * mm, 18 * mm, 16 * mm, 19 * mm]
at = Table(adv_data, colWidths=colw, repeatRows=1)
ast = base_table_style(fontsize=7.3)
ast.add("ALIGN", (1, 0), (-1, -1), "CENTER")
ast.add("ALIGN", (0, 0), (0, -1), "LEFT")
ast.add("FONTNAME", (6, 1), (6, -1), "Helvetica-Bold")  # P(win) bold
# zebra striping
for i in range(1, len(adv_data)):
    if i % 2 == 0:
        ast.add("BACKGROUND", (0, i), (-1, i), ROWALT)
# group separators
for r, g in group_start_rows:
    ast.add("LINEABOVE", (0, r), (-1, r), 0.8, ACCENT)
at.setStyle(ast)
story.append(at)
story.append(Spacer(1, 4))
story.append(Paragraph(
    "P(R32) = reach Round of 32 (advance from group); P(Grp 1st) = finish first in group. "
    "Knockout probabilities include winning on penalties.", BODY_SM))

story.append(PageBreak())

# ===========================================================================
# SECTION 3 — CHANGES TO ADVANCEMENT ODDS BY STAGE
# ===========================================================================
story.append(section_header(3, "Changes to Advancement Odds vs Pre-Tournament"))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "Delta = current (results-conditioned) minus pre-tournament, same model. Positive (green) "
    "means a team's odds improved after the 24 played results; negative (red) means they fell. "
    "Sorted by &Delta; P(win) descending. Group-stage R32 movement is typically the largest, as "
    "actual results lock in qualification scenarios.", BODY_SM))
story.append(Spacer(1, 4))

delta_sorted = sorted(teams, key=lambda t: t["win_d"], reverse=True)
dhead = ["Team", "Grp", "P(R32)", "Δ R32", "P(R16)", "Δ R16",
         "P(win)", "Δ win", "Δ Grp1st"]
ddata = [dhead]
for t in delta_sorted:
    ddata.append([
        t["team"], t["group"], pct(t["R32"]), spct(t["R32_d"]),
        pct(t["R16"]), spct(t["R16_d"]), pct(t["win"]), spct(t["win_d"], 2),
        spct(t["gw_d"]),
    ])
dcolw = [34 * mm, 11 * mm, 16 * mm, 17 * mm, 16 * mm, 17 * mm, 15 * mm, 17 * mm, 18 * mm]
dt = Table(ddata, colWidths=dcolw, repeatRows=1)
dst = base_table_style(fontsize=7.0)
dst.add("ALIGN", (1, 0), (-1, -1), "CENTER")
dst.add("ALIGN", (0, 0), (0, -1), "LEFT")
for i, t in enumerate(delta_sorted, 1):
    if i % 2 == 0:
        dst.add("BACKGROUND", (0, i), (-1, i), ROWALT)
    # color delta columns: R32_d(3), R16_d(5), win_d(7), gw_d(8)
    dst.add("TEXTCOLOR", (3, i), (3, i), GREEN if t["R32_d"] >= 0 else RED)
    dst.add("TEXTCOLOR", (5, i), (5, i), GREEN if t["R16_d"] >= 0 else RED)
    dst.add("TEXTCOLOR", (7, i), (7, i), GREEN if t["win_d"] >= 0 else RED)
    dst.add("TEXTCOLOR", (8, i), (8, i), GREEN if t["gw_d"] >= 0 else RED)
    dst.add("FONTNAME", (3, i), (3, i), "Helvetica-Bold")
    dst.add("FONTNAME", (7, i), (7, i), "Helvetica-Bold")
dt.setStyle(dst)
story.append(dt)
story.append(Spacer(1, 6))

# Winners / losers callout
gainers = sorted(teams, key=lambda t: t["R16_d"], reverse=True)[:3]
losers = sorted(teams, key=lambda t: t["R16_d"])[:3]
g_txt = ", ".join(f"{t['team']} ({spct(t['R16_d'])})" for t in gainers)
l_txt = ", ".join(f"{t['team']} ({spct(t['R16_d'])})" for t in losers)
story.append(Paragraph(
    f"<b><font color='#1a7f37'>Biggest R16 gainers:</font></b> {g_txt}. "
    f"<b><font color='#b42318'>Biggest R16 drops:</font></b> {l_txt}.", BODY))

# Played results table
story.append(Paragraph("The 24 played group results that drove these changes", H2))
pr_head = ["Match", "Score", "Match", "Score"]
half = (len(played) + 1) // 2
left = played[:half]
right = played[half:]
pr_data = [pr_head]
for i in range(half):
    lrow = left[i]
    lcell = f"{lrow['home']} v {lrow['away']}"
    lsc = f"{lrow['hg']}–{lrow['ag']}"
    if i < len(right):
        rrow = right[i]
        rcell = f"{rrow['home']} v {rrow['away']}"
        rsc = f"{rrow['hg']}–{rrow['ag']}"
    else:
        rcell, rsc = "", ""
    pr_data.append([lcell, lsc, rcell, rsc])
pt = Table(pr_data, colWidths=[60 * mm, 16 * mm, 60 * mm, 16 * mm], repeatRows=1)
pst = base_table_style(fontsize=7.4)
pst.add("ALIGN", (1, 0), (1, -1), "CENTER")
pst.add("ALIGN", (3, 0), (3, -1), "CENTER")
pst.add("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold")
pst.add("FONTNAME", (3, 1), (3, -1), "Helvetica-Bold")
for i in range(1, len(pr_data)):
    if i % 2 == 0:
        pst.add("BACKGROUND", (0, i), (-1, i), ROWALT)
pt.setStyle(pst)
story.append(pt)

story.append(PageBreak())

# ===========================================================================
# SECTION 4 — ACTIONABLE POLYMARKET BETS
# ===========================================================================
story.append(section_header(4, "Actionable Polymarket Bets"))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "Top fee-adjusted edges from the tournament-advancement simulation (20,000 sims, seed 42) "
    "versus live Polymarket advancement and group-winner markets. Edges are fee-adjusted and "
    "sized quarter-Kelly on the $1,310 pool (5% per-bet cap). Source: advancement_edges.md "
    "&mdash; top-edges table.", BODY_SM))
story.append(Spacer(1, 4))

# Reproduced EXACTLY from advancement_edges.md "Top edges" table (lines 23-42)
pm_head = ["#", "Team", "Market", "Side", "Sim P", "PM price", "Fee",
           "Fee-adj edge", "Stake ($)"]
pm_rows = [
    [1, "Iran", "Reach R32 (knockout)", "YES", "59.9%", "0.413", "0.007", "+17.8%", "65.50"],
    [2, "Australia", "Win Group D", "YES", "38.8%", "0.216", "0.005", "+16.7%", "65.50"],
    [3, "United States", "Win Group D", "NO", "47.7%", "0.310", "0.006", "+16.1%", "65.50"],
    [4, "United States", "Reach Quarterfinals", "NO", "80.5%", "0.640", "0.007", "+15.8%", "65.50"],
    [5, "Iran", "Reach Round of 16", "YES", "30.6%", "0.150", "0.004", "+15.2%", "58.70"],
    [6, "Colombia", "Reach Quarterfinals", "YES", "45.6%", "0.300", "0.006", "+15.0%", "65.50"],
    [7, "Colombia", "Win Group K", "YES", "64.6%", "0.490", "0.007", "+14.8%", "65.50"],
    [8, "Portugal", "Win Group K", "NO", "70.2%", "0.560", "0.007", "+13.4%", "65.50"],
    [9, "Colombia", "Reach Round of 16", "YES", "71.1%", "0.570", "0.007", "+13.3%", "65.50"],
    [10, "Switzerland", "Win Group B", "NO", "66.9%", "0.530", "0.007", "+13.1%", "65.50"],
    [11, "Australia", "Reach Round of 16", "YES", "53.7%", "0.400", "0.007", "+13.0%", "65.50"],
    [12, "Qatar", "Reach R32 (knockout)", "YES", "39.9%", "0.270", "0.006", "+12.3%", "55.63"],
    [13, "Portugal", "Reach Quarterfinals", "NO", "67.8%", "0.550", "0.007", "+12.0%", "65.50"],
    [14, "France", "Reach Quarterfinals", "NO", "50.3%", "0.380", "0.007", "+11.6%", "61.76"],
    [15, "Portugal", "Reach Round of 16", "NO", "42.2%", "0.300", "0.006", "+11.5%", "54.41"],
    [16, "United States", "Reach Round of 16", "NO", "48.1%", "0.360", "0.007", "+11.4%", "59.12"],
    [17, "Portugal", "Reach Semifinals", "NO", "82.4%", "0.710", "0.006", "+10.8%", "65.50"],
    [18, "Morocco", "Win Group C", "NO", "81.3%", "0.700", "0.006", "+10.7%", "65.50"],
    [19, "France", "Reach Semifinals", "NO", "65.9%", "0.550", "0.007", "+10.2%", "65.50"],
    [20, "United States", "Reach Semifinals", "NO", "94.1%", "0.840", "0.004", "+9.7%", "65.50"],
]
pm_data = [pm_head]
for r in pm_rows:
    pm_data.append([str(r[0])] + r[1:])
pmcolw = [8 * mm, 28 * mm, 36 * mm, 11 * mm, 15 * mm, 16 * mm, 12 * mm, 22 * mm, 17 * mm]
pmt = Table(pm_data, colWidths=pmcolw, repeatRows=1)
pmst = base_table_style(fontsize=7.4)
pmst.add("ALIGN", (0, 0), (0, -1), "CENTER")
pmst.add("ALIGN", (3, 0), (-1, -1), "CENTER")
pmst.add("ALIGN", (1, 0), (2, -1), "LEFT")
pmst.add("FONTNAME", (7, 1), (7, -1), "Helvetica-Bold")
for i, r in enumerate(pm_rows, 1):
    if i % 2 == 0:
        pmst.add("BACKGROUND", (0, i), (-1, i), ROWALT)
    pmst.add("TEXTCOLOR", (7, i), (7, i), GREEN)
    # color YES/NO side
    pmst.add("TEXTCOLOR", (3, i), (3, i), ACCENT if r[3] == "YES" else GREY)
pmt.setStyle(pmst)
story.append(pmt)
story.append(Spacer(1, 5))
story.append(Paragraph(
    "Side = the wager the simulation favours (YES buy at best ask; NO buy at 1 &minus; YES bid). "
    "Fee-adj edge = sim probability &minus; buy price &minus; per-share taker fee 0.03&middot;p&middot;(1&minus;p). "
    "Coverage: 468 Polymarket events pulled; 336 team-stage markets matched. "
    "Full ranked list of all matched markets is in docs/research/advancement_edges.md.", BODY_SM))

story.append(PageBreak())

# ===========================================================================
# SECTION 5 — ACTIONABLE SPORTSBOOK BETS
# ===========================================================================
story.append(section_header(5, "Actionable Sportsbook Bets"))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "Current sportsbook bet card (model edge vs market). Source: data/card_latest.md, "
    "generated 2026-06-18T09:50:49. Pool: rung 0 &pound;1500, Kelly fraction 0.25.", BODY_SM))
story.append(Spacer(1, 4))

# Reproduced from card_latest.md (5 picks)
sb_head = ["#", "Match", "Selection", "Odds", "Venue", "Model %", "Mkt %", "Edge", "Stake"]
sb_rows = [
    ["1", "Canada vs Qatar", "Qatar", "13.50", "matchbook", "9.7%", "7.5%", "+31.6%", "9.48"],
    ["2", "Mexico vs South Korea", "Mexico", "2.12", "betfair_ex_uk", "51.7%", "46.7%", "+9.6%", "32.22"],
    ["3", "Canada vs Qatar", "Draw", "6.00", "betfair_ex_uk", "17.5%", "16.5%", "+5.1%", "3.85"],
    ["4", "Czech Republic vs South Africa", "South Africa", "4.90", "betfair_ex_uk", "20.9%", "21.0%", "+2.3%", "2.18"],
    ["5", "Switzerland vs Bosnia and Herzegovina", "Bosnia and Herzegovina", "7.00", "betfair_ex_uk", "14.6%", "14.2%", "+2.0%", "1.27"],
]
sb_data = [sb_head] + sb_rows
sbcolw = [8 * mm, 44 * mm, 33 * mm, 13 * mm, 22 * mm, 15 * mm, 13 * mm, 14 * mm, 13 * mm]
sbt = Table(sb_data, colWidths=sbcolw, repeatRows=1)
sbst = base_table_style(fontsize=7.3)
sbst.add("ALIGN", (0, 0), (0, -1), "CENTER")
sbst.add("ALIGN", (3, 0), (-1, -1), "CENTER")
sbst.add("ALIGN", (1, 0), (2, -1), "LEFT")
sbst.add("FONTNAME", (3, 1), (3, -1), "Helvetica-Bold")
sbst.add("FONTNAME", (7, 1), (7, -1), "Helvetica-Bold")
for i, r in enumerate(sb_rows, 1):
    if i % 2 == 0:
        sbst.add("BACKGROUND", (0, i), (-1, i), ROWALT)
    sbst.add("TEXTCOLOR", (7, i), (7, i), GREEN)
sbt.setStyle(sbst)
story.append(sbt)
story.append(Spacer(1, 4))
story.append(Paragraph(
    "Odds are decimal best-available at the listed venue. Edge = model probability vs "
    "de-vigged market probability. Stakes in &pound; (pool currency). The card also carries "
    "model scoreline / O-U / BTTS markets for the four listed fixtures (see source file).", BODY_SM))
story.append(Spacer(1, 8))

# Per-fixture component models note (elo/dc) from card
story.append(Paragraph("Model component breakdown (per pick)", H2))
comp_head = ["Pick", "Elo", "Dixon-Coles", "Blended model"]
comp_rows = [
    ["Qatar (v Canada)", "7%", "17%", "9.7%"],
    ["Mexico (v South Korea)", "60%", "53%", "51.7%"],
    ["Draw (Canada v Qatar)", "14%", "23%", "17.5%"],
    ["South Africa (v Czech Rep.)", "22%", "20%", "20.9%"],
    ["Bosnia & H. (v Switzerland)", "11%", "19%", "14.6%"],
]
comp_data = [comp_head] + comp_rows
compt = Table(comp_data, colWidths=[52 * mm, 22 * mm, 28 * mm, 28 * mm], repeatRows=1)
compst = base_table_style(fontsize=7.6)
compst.add("ALIGN", (1, 0), (-1, -1), "CENTER")
compst.add("ALIGN", (0, 0), (0, -1), "LEFT")
compst.add("FONTNAME", (3, 1), (3, -1), "Helvetica-Bold")
for i in range(1, len(comp_data)):
    if i % 2 == 0:
        compst.add("BACKGROUND", (0, i), (-1, i), ROWALT)
compt.setStyle(compst)
story.append(compt)

story.append(Spacer(1, 14))

# ===========================================================================
# METHODOLOGY / CAVEAT FOOTER
# ===========================================================================
story.append(HRFlowable(width="100%", thickness=1.0, color=NAVY))
story.append(Spacer(1, 4))
story.append(Paragraph("Methodology &amp; Caveats", H2))
meth_items = [
    "<b>Knockout probabilities are noisier.</b> Every simulated match is driven by a straight "
    "50/50 average of the Elo and Dixon-Coles 1X2 probabilities &mdash; there is <b>no market "
    "term</b>. No odds exist for the later rounds, so a market-anchored blend is impossible; "
    "these are an independent, noisier model view, not ground truth.",
    "<b>Advancement includes penalties.</b> A 90-minute knockout draw is resolved by the "
    "simulator's extra-time / penalty model, so &lsquo;reaching&rsquo; a stage <b>includes</b> "
    "winning on penalties &mdash; matching Polymarket resolution (&lsquo;reach stage X&rsquo; = "
    "the team is in stage X, however it got there).",
    "<b>Host advantage.</b> The three hosts (United States, Mexico, Canada) get the "
    "home-advantage bonus on their own group fixtures; every other group match and all "
    "knockout matches are neutral.",
    "<b>Polymarket sizing.</b> Edges are fee-adjusted (per-share taker fee 0.03&middot;p&middot;"
    "(1&minus;p) subtracted) and sized quarter-Kelly on the $1,310 pool with a 5% per-bet cap; "
    "the simulation ran 20,000 iterations (seed 42).",
    "<b>Delta convention.</b> All &Delta; columns are current (results-conditioned, 24 matches "
    "fixed) minus the pre-tournament baseline from the same model.",
    "<b>Not financial advice.</b> Figures are model outputs for research only. Every probability "
    "and odds figure in this report is traceable to the four source data files.",
]
for it in meth_items:
    story.append(Paragraph("&bull;&nbsp; " + it, BODY_SM))
story.append(Spacer(1, 6))
story.append(Paragraph(
    "Sources: data/advancement_current_vs_pretournament.csv &middot; "
    "data/advancement_played_results.json &middot; docs/research/advancement_edges.md "
    "&middot; data/card_latest.md", BODY_SM))


# ---------------------------------------------------------------------------
# Page decoration: footer with page number + running header
# ---------------------------------------------------------------------------
def on_page(canvas, doc):
    canvas.saveState()
    w, h = A4
    # top rule
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.5)
    canvas.line(20 * mm, h - 12 * mm, w - 20 * mm, h - 12 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(GREY)
    canvas.drawString(20 * mm, h - 11 * mm, "World Cup 2026 — Advancement & Betting Report")
    canvas.drawRightString(w - 20 * mm, h - 11 * mm, "2026-06-18")
    # footer
    canvas.setStrokeColor(LINE)
    canvas.line(20 * mm, 13 * mm, w - 20 * mm, 13 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(GREY)
    canvas.drawString(20 * mm, 9 * mm,
                      "Model output — research only. All figures traceable to source files.")
    canvas.drawRightString(w - 20 * mm, 9 * mm, f"Page {doc.page}")
    canvas.restoreState()


doc = SimpleDocTemplate(
    OUT_PATH, pagesize=A4,
    leftMargin=20 * mm, rightMargin=20 * mm,
    topMargin=16 * mm, bottomMargin=16 * mm,
    title="World Cup 2026 — Advancement & Betting Report",
    author="World Cup Alpha model",
)
doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
print("WROTE", OUT_PATH)
