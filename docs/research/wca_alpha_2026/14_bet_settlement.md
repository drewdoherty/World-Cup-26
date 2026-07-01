# 14 — Bet Settlement Reconciliation (8 open bets)

**Track D · analysis-only · read-only phase.** Re-run after the interrupt; odds feed
confirmed LIVE (`src/wca/data/theoddsapi.py`, ~96k quota). This document derives the
**actual** settlement of every `status='open'` row from realized results, gives the
**exact** settle command the user can run, and flags settlement bugs / void risks.

It does **not** execute settlement, does **not** write the ledger, does **not**
modify `src/`. Final authority is the user.

- DB (read-only): `data/wca.db` table `bets`
- Realized results: `data/processed/wc2026_results.json` (group stage thru 2026-06-20 only),
  plus web-verified results for everything after (cited inline).
- As-of date: **2026-06-30**, mid-Round-of-32.
- FX: live **GBP/USD ≈ 1.32** (poundsterlinglive, 2026-06-29 close 1.3200); repo
  fallback `FALLBACK_USD_PER_GBP = 1.33` at `src/wca/fx.py:16`.

---

## TL;DR settlement map

| id | bet (short) | venue | ccy | stake | odds | **verdict** | realized basis |
|----|-------------|-------|-----|-------|------|-------------|----------------|
| 11 | Kane Golden Boot (win-only) | betfair | GBP | 10 | 7.5 | **OPEN** | tournament live; Kane on 3 (2026), Messi leads 6; England R32 vs DRC is **Jul 1** |
| 14 | "Japan reach R16 – No" | polymarket | USD | 60 | 1.6667 | **WON** | Japan eliminated R32 by Brazil 2-1 → did NOT reach R16 → No wins |
| 57 | "Ghana not eliminated R32 – No" | polymarket | USD | 1 | 1.4708 | **OPEN** | Ghana vs Colombia R32 scheduled **Jul 3**, not yet played |
| 88 | England builder (-2.5 / -2.0 / win 1-3-0) | betfair | GBP | 5 | 5.52 | **LOST** | England 0-0 Ghana (2026-06-23) — England did not win |
| 94 | Belgium win + Lukaku score | paddypower | GBP | 10 | 2.37 | **LOST** | Belgium 0-0 Iran (2026-06-21) — no Belgium win, Lukaku blank |
| 99 | Treble Ronaldo/Kane/Diaz O1.5 SOT | betfred | GBP | 10 | 5.05 | **LOST** | Kane 0 SOT & Diaz 0 SOT (Ronaldo leg won) → treble loses |
| 100 | Eng HT + Eng 2H + Kane/Bell O1.5 SOT | betfred | GBP | 10 | 7.9 | **LOST** | England 0-0 — no HT/2H win; Kane 0 SOT, Bellingham 0 SOT |
| 101 | 2UP 4-fold Ger/IvC/Ned/Jpn | virginbet | GBP | 50 | 3.37 | **LOST** | Germany lost 1-2 to Ecuador (never 2-up) AND Japan drew 1-1 (never 2-up) |

**Resolved now: 1 WON (id 14), 5 LOST (88, 94, 99, 100, 101). Still OPEN: 11, 57.**

---

## Per-bet detail and the EXACT command to settle

Two settle paths exist and they are **not** identical on free bets — read the
"settlement-engine bug" section before settling 88, 99, 100.

- Bot form: `/settle <bet-id> <outcome> [closing-odds]` — `src/wca/bot/app.py:939`
- CLI: `python scripts/wca_settle.py --bet-id <id> --outcome <won|lost|void> [--closing-odds X]`

`outcome` ∈ {won, lost, void}. `closing-odds` should be the **de-vigged fair**
close (CLV input), optional and only used for CLV — it never changes payout
(`app.py:993`, `wca_settle.py:90`). Payout always pays at the **backed** price.

### id 14 — "Japan reach R16 – No" @1.6667 (polymarket, USD 60) → **WON**

Market: "Will Japan reach the Round of 16 at the 2026 FIFA World Cup?" Position is
**No**. Japan finished Group F runners-up (1-1 vs Sweden, 2026-06-25), then **lost
the Round-of-32 tie 2-1 to Brazil** (Sano 29'; Casemiro eq.; Martinelli 90+ winner).
Japan was eliminated in the R32 and therefore **did not reach the R16** → the **No**
side **WINS**.

- Realized P&L = stake·(odds−1) = 60 · 0.6667 = **+40.00 USD** ≈ **+£30.3** @1.32.
- This is the only model-sourced bet in the batch (`source='model'`, model_prob 0.685,
  fair "No" ≈ 0.666). Worth capturing a closing line for CLV.

```
/settle 14 won            # plus a de-vigged close if you have one, e.g. /settle 14 won 1.55
```
CLI equivalent: `python scripts/wca_settle.py --bet-id 14 --outcome won`

### id 11 — Kane Golden Boot @7.5 (betfair, GBP 10, free bet) → **OPEN — do not settle**

Outright on the **2026 tournament** Golden Boot, win-only (EW declined). As of
2026-06-30 the award is undecided: Kane has **3 goals** (level group, behind Messi 6;
Mbappé/Dembélé/Haaland/Vinícius on 4). England top Group L and play **DR Congo in the
R32 on Jul 1** — Kane is still alive. Caution on data: the Eng-Gha report citing "Kane
on 10 World Cup goals" is his **all-time** WC tally (level with Lineker), **not** the
2026 Golden Boot count — do not conflate. **Leave open.** Settle only when the Golden
Boot is mathematically decided or England is eliminated.

### id 57 — "Ghana not eliminated R32 – No" @1.4708 (polymarket, USD 1) → **OPEN**

Market expanded 2026-06-20 to a Round-of-32 elimination question; position is **No**
("Ghana **not** eliminated in R32"). Ghana advanced as the best Group L third-place
team (0-0 England, 1-0 Panama, 1-2 Croatia) and face **Colombia in the R32 on Jul 3**
(Arrowhead, Kansas City). **Not yet played → OPEN.** (USD account-2; trivial $1
activation punt.)

### id 88 — England builder @5.52 (betfair, GBP 5, free bet) → **LOST**

Legs: England −2.5 goals + England (−2.0) handicap + England correct score 1-0/2-0/3-0,
all on **England vs Ghana → 0-0** (2026-06-23, Gillette Stadium; 19 shots, only 3 on
target; O'Reilly hit the bar). England did not win → **every** leg fails → builder LOST.

- `source='offer'` (note: "£5 inc £5 free bet"). On a free (stake-not-returned) bet a
  **loss costs £0**, not −£5. The bot form books £0 for offers; the CLI books −stake.
  **Use the bot** (or void via CLI) — see bug section.

```
/settle 88 lost
```

### id 94 — Belgium + Lukaku @2.37 (paddypower, GBP 10) → **LOST**

Bet Builder: Belgium FTR (EP early-payout) + Lukaku anytime scorer, on **Belgium vs
Iran → 0-0** (2026-06-21, LA; Ngoy sent off). Belgium did not win and Lukaku did not
score → both legs fail → LOST. The "EP early payout" only triggers the FTR leg early if
Belgium go **2 goals up**, which never happened (0-0). `source='punt'` (real cash stake)
→ realized P&L = **−£10.00**.

```
/settle 94 lost
```

### id 99 — Betfred treble, player O1.5 SOT @5.05 (betfred, GBP 10, free bet) → **LOST**

Legs (need **2+ shots on target** each):
- Ronaldo O1.5 SOT — Por **5-0** Uzb: Ronaldo scored a brace (6', 39'); 2 SOT → **WON**.
- Kane O1.5 SOT — Eng **0-0** Gha: Kane **0 SOT** (4 attempts, none on target) → **LOST**.
- Diaz O1.5 SOT — Col **1-0** DRC: Díaz **0 SOT** (chances ruled out for foul/offside) → **LOST**.

Two legs fail → treble **LOST**. (SOT per ESPN shot-maps, gameId 760458 and 760459.)

```
/settle 99 lost
```
**Do not let the bot settle this as a LAY** — see bug section; if in doubt use
`/settle 99 void` and re-book P&L manually as £0, or settle via CLI with a manual £0.

### id 100 — Betfred Eng-Gha acca @7.9 (betfred, GBP 10, free bet) → **LOST**

Legs on **England 0-0 Ghana**:
- England Half Time (England winning at HT) — 0-0 at HT → **LOST**.
- England 2nd Half (England wins 2H) — 0-0 → **LOST**.
- Kane O1.5 SOT — **0 SOT** → **LOST**.
- Bellingham O1.5 SOT — **0 SOT** (1 attempt, off target) → **LOST**.

All four fail → acca **LOST**.

```
/settle 100 lost
```
Same LAY-misflag caution as id 99.

### id 101 — Virgin 2UP 4-fold @3.37 (virginbet, GBP 50) → **LOST**

"2UP" = each leg pays as a winner if the team goes **2 goals ahead** at any point;
otherwise it settles on the **full-time** result. Legs (2026-06-25):
- Germany (vs Ecuador) — **Ecuador 2-1 Germany**; Germany led 1-0 (Sané 2') but **never
  2-up**, and lost FT → **LOST**.
- Ivory Coast (vs Curaçao) — IvC 2-0; 2-up reached (7', 64') → WON.
- Netherlands (vs Tunisia) — Ned 3-1; 2-up reached → WON.
- Japan (vs Sweden) — **1-1 draw**; Japan **never 2-up**, FT draw → **LOST**.

Two failing legs (Germany, Japan) → 4-fold **LOST**. `source='punt'` (real £50; model
flagged ~−35% EV pre-bet) → realized P&L = **−£50.00**.

```
/settle 101 lost
```

---

## CRITICAL — settlement-engine bug on the free-bet accas (ids 99, 100)

The bot's `/settle` handler infers a **lay** bet by substring:
`is_lay = "lay" in (market + " " + selection).lower()` — `src/wca/bot/app.py:980`.

Bets 99 and 100 contain the word **"Player"/"player"** in the market label
("Treble — **Play**er 1+ SOT", "+ **play**er SOT"). `"play"` contains `"lay"`, so the
heuristic returns **is_lay=True** (verified: `instr` hits at pos 11 / 25). For a LOST
bet the handler then books

```
settled_pl = -liability = -(stake * (odds - 1))      # app.py:998-1000
```

i.e. **id 99 → −£40.50** and **id 100 → −£69.00**, instead of the correct **£0**
(these are `source='offer'` free bets — stake is not at risk, a loss costs £0; the
free-bet branch at `app.py:1003-1004` is never reached because the lay branch wins).

**This is a real mis-settlement of −£109.50 across the two bets.** Three independent
problems collide here:
1. `is_lay` substring match false-positives on "play"/"player" (also "overlay",
   "display", "replay", "parlay").
2. The lay branch takes precedence over the free-bet branch, so an offer flagged as lay
   loses its £0-on-loss treatment.
3. The CLI `scripts/wca_settle.py` has **no** free-bet logic at all — it always books
   `−stake` on loss (`wca_settle.py:94-95`), so the CLI is wrong on offers too (it would
   book −£10 each for 88/99/100 rather than £0).

**Safe settle workarounds for 99 and 100 (no code change, read-only phase):**
- Preferred: settle as `void` via either path (`settled_pl = 0.0`, the correct realized
  value for a lost free bet), then note "lost free bet, £0" — `void` is the only outcome
  whose P&L is engine-independent and matches reality here.
  `/settle 99 void` · `/settle 100 void`
- Or settle `lost` via the bot and then **manually correct** `settled_pl` to `0.0` for
  ids 99/100 (the bot will have written −£40.50 / −£69.00).
- Do **not** settle 99/100 `lost` via the CLI either (books −£10 each, also wrong sign
  magnitude for a free bet).

For id **88** (also `source='offer'`, but no "lay"/"play" substring): the **bot**
correctly books £0 on `lost`; the **CLI** would wrongly book −£5. → settle 88 via the
**bot** (`/settle 88 lost`), not the CLI.

### IMPLEMENT-mode fix (proposed; not applied — guardrails block `src/` edits)

When you exit the read-only phase, replace the lay heuristic with a typed flag and make
the free-bet branch authoritative, keeping the API backward-compatible:

- Add a `bet_type`/`is_lay` column (or reuse `manual_override`) instead of substring
  sniffing; default `is_lay` from an **exact** token/regex (`\blay\b`) as a fallback so
  existing rows still parse. This kills the "play/overlay/parlay" class of bugs.
- Order the branches: **void → free-bet(loss=£0) → lay → back**, so an `offer` row is
  never taxed a lay liability.
- Port the free-bet branch into `scripts/wca_settle.py` (it has none) so both settle
  paths agree; add a regression test asserting `settle(99,'lost')==0.0` and
  `settle(<lay>,'lost')==-liability`.
- Backward-compat: keep `/settle <id> <outcome> [odds]` signature unchanged; existing
  settled rows untouched; add `--free/--no-free` override on the CLI defaulting to the
  stored `source`.

---

## FX-aware netting (GBP vs USD)

Realized to date (settle the 6 decided bets; leave 11 and 57 open):

**GBP book (account 1, betfair/paddypower/betfred/virginbet):**
| id | outcome | source | realized £ |
|----|---------|--------|-----------|
| 88 | lost | offer (free) | **0.00** (free bet; bot path) |
| 94 | lost | punt (cash) | **−10.00** |
| 99 | lost | offer (free) | **0.00** (must avoid lay-misflag) |
| 100 | lost | offer (free) | **0.00** (must avoid lay-misflag) |
| 101 | lost | punt (cash) | **−50.00** |
| **GBP subtotal** | | | **−£60.00** |

If 99/100 are mis-settled as lay-lost the GBP subtotal becomes **−£169.50** (a £109.50
phantom loss) — the headline reason to use the void/manual workaround.

**USD book (account 1, polymarket):**
| id | outcome | realized $ |
|----|---------|-----------|
| 14 | won | **+40.00** |
| **USD subtotal** | | **+$40.00** |

**Netted (report currency GBP, USD→GBP at 1.32):**
- USD +$40.00 → **+£30.30**
- **Net realized this batch ≈ −£60.00 + £30.30 = −£29.70** (≈ −$39.2 at 1.32).
- Still at risk / open: id 11 (£10 free), id 57 ($1) — excluded from realized.

Notes on netting: account-2 ($1, id 57) is a separate Polymarket wallet and stays open.
The repo's FX convention is **USD per GBP** (`src/wca/fx.py`, `arbfx.py:13`,
`accas.py:605` default 1.33); to convert a USD P&L to GBP divide by `usd_per_gbp`
(40/1.32 = £30.30). Use a single rate consistently across the batch; 1.32 (live) vs 1.33
(fallback) moves the netted USD leg by ~£0.23 — immaterial here.

---

## Player-participation / void risk on the SOT accas — moot but logged

The standing risk on player-prop accas (99, 100) is that a named player **doesn't
participate**, which most books treat as a **void leg** (acca re-prices on the remaining
legs) rather than a loss. That risk is now **moot**: all four named players (Ronaldo,
Kane, Díaz, Bellingham) **started and featured**, so no leg voids — both accas are clean
**LOSSES on the SOT lines**, not voids. No participation-void adjustment applies.

Residual confirmations to do before booking (low effort, do not block settlement):
- **Venues** are inferred for 94/99/100/101 (notes say "confirm Paddy Power / Betfred /
  Virgin"). Venue does not change the verdict but matters for the ledger and for any
  free-bet vs cash distinction — confirm from the bet slips.
- **Free-bet flags**: 11, 88, 99, 100 are `source='offer'`. Confirm each was a genuine
  stake-not-returned free bet (note for 88 says "inc £5 free bet"); that is what makes
  the loss £0 rather than −stake.
- **id 94 EP**: if the slip shows the FTR leg was paid out under early-payout *before*
  the 0-0 (it was not, since Belgium never led), that would change things — confirmed not
  triggered.

---

## Settle-command crib (copy/paste; user runs, not the agent)

```
# decided — safe order
/settle 14 won          # +40.00 USD  (model bet; add a de-vigged close for CLV if available)
/settle 94 lost         # -£10.00     (cash punt)
/settle 101 lost        # -£50.00     (cash punt)
/settle 88 lost         # £0          (free bet — settle via BOT, not CLI)
/settle 99 void         # £0          (free bet acca LOST; 'void' dodges the lay-misflag bug -> books 0.0)
/settle 100 void        # £0          (same; or settle 'lost' via bot then correct settled_pl to 0.0)

# leave OPEN — do not settle
# 11  Kane Golden Boot   (tournament live; England R32 vs DRC Jul 1)
# 57  Ghana not-elim R32 (Ghana vs Colombia R32 Jul 3, not played)
```

If you prefer the CLI for the cash bets:
`python scripts/wca_settle.py --bet-id 94 --outcome lost` and `--bet-id 101 --outcome lost`
are correct. Do **not** use the CLI for 88/99/100 (no free-bet logic → wrong −stake/−liability).

---

## Sources (web-verified results post-2026-06-20)

- England 0-0 Ghana (2026-06-23), Kane/Bellingham 0 SOT: ESPN gameId 760458; Al Jazeera
  match report; FIFA match centre 400021506.
- Belgium 0-0 Iran (2026-06-21): FIFA 400021477; ESPN story 49135281.
- Portugal 5-0 Uzbekistan, Ronaldo brace, 9 team SOT (2026-06-23): ESPN gameId 760461.
- Colombia 1-0 DR Congo, Díaz 0 SOT (2026-06-23): ESPN gameId 760459.
- Ecuador 2-1 Germany (2026-06-25): ESPN gameId 760468.
- Curaçao 0-2 Ivory Coast (2026-06-25): ESPN gameId 760473.
- Tunisia 1-3 Netherlands (2026-06-25): FIFA 400021473.
- Japan 1-1 Sweden (2026-06-25): ESPN gameId 760471; Japan eliminated R32 by Brazil 2-1:
  ESPN story 49181192 / Olympics live blog.
- Ghana advanced as best 3rd Group L; R32 vs Colombia scheduled Jul 3: ESPN gameId 760501.
- England top Group L, R32 vs DR Congo Jul 1; Golden Boot (Messi 6, Kane 3 as of Jun 29):
  FOX Sports Golden Boot tracker; Goal.com.
- GBP/USD 1.3200 (2026-06-29): poundsterlinglive.
