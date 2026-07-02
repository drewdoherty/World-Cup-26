# 09 — Match-Event Models: Design (Track B)

**Status:** DESIGN (read-only phase). No `src/` modified; no `.db` written. All
sketches are `props.py`-style and back-compat by construction (new params are
optional with defaults that reproduce today's numbers).

**Operative steer:** trust the model but *improve it first*; the match-event
markets are the *less-efficient* surface we are trying to beat with an
independent, history-calibrated model. Where existing code is sub-optimal,
replace it — but every dependent (`card.py`, `wca_event_ev.py`, `accas.py`,
bot, `sitedata.py`) must keep working through optional params + fallbacks +
the existing `model or DefaultModel()` call sites.

---

## 0. Empirical foundation (real numbers, recomputed)

Source corpus: `data/processed/props_matches.csv` (128 WC2018+2022 matches,
StatsBomb), recomputed by `scripts/...` style aggregation (see Track-B script).
Team-level = home and away rows stacked (256 team-matches).

| Quantity | mean | var | var/mean | k (method-of-moments) |
|---|---|---|---|---|
| TEAM corners | 4.484 | 6.594 | **1.470** | **9.5** |
| TEAM yellows | 1.676 | 2.032 | 1.212 | 7.9 |
| TEAM reds | 0.031 | 0.030 | 0.97 | ∞ (Poisson) |
| TEAM fouls | 14.262 | 24.240 | **1.700** | **20.4** |
| TEAM shots | 12.500 | 32.289 | **2.583** | **7.9** |
| TOTAL corners | 8.969 | 9.405 | 1.049 | 184.3 |
| TOTAL yellows | 3.352 | 4.853 | 1.448 | 7.5 |
| TOTAL fouls | 28.523 | 53.718 | 1.883 | 32.3 |
| TOTAL shots | 25.000 | 52.250 | 2.090 | 22.9 |
| TOTAL cards (y+2nd-yellow-as-red) | 3.414 | 5.071 | 1.485 | **7.0** |

Cross-correlations (team-level, 256 obs):

| pair | r | implication |
|---|---|---|
| team fouls ↔ team cards | **0.508** | strong → FoulsModel is a real driver of cards |
| team shots ↔ team xG | **0.696** | strong → shots/SoT scale with attack; elasticity 0.6 is defensible |
| team corners ↔ team xG | **0.315** | weak → keep corner damping (don't fully scale on xG) |
| team corners ↔ team shots | 0.590 | corners track *shot volume* better than *xG* |

**Two design-critical facts the current code gets wrong or under-uses:**

1. **TEAM corners are overdispersed (var/mean 1.47, k≈9.5) but `CornersModel`
   prices team corners with the *total* dispersion `k=157.5`** (`props.py:139`).
   That makes team-corner tails far too thin. Fix: a separate `team_dispersion`.
2. **Corners correlate with *shots* (0.59) more than *xG* (0.315).** The xG-only
   coupling is the weakest of the three drivers; the EB team prior (below) is
   where the edge actually lives, not the xG elasticity.

**Data gaps (constrain the SoT model):** neither
`props_matches.csv` nor `props_players.csv` contains **shots-on-target**. SoT
must therefore be modelled as `shots × conversion-to-target ratio` with the
ratio taken from an external/live prior (TheOddsApi SoT lines + a literature
WC SoT/shot ratio ≈ 0.34–0.36), *or* left as a live-odds-anchored model until a
StatsBomb SoT pull lands (`src/wca/data/statsbomb.py`). Flagged below.

WC2026 realized data (`wc2026_results.json`, `completed_fixtures.json`) carries
**score only**, no per-match corners/cards/fouls — so it cannot recalibrate the
event means, but it *is* the settlement/CLV ledger for live-priced legs.

---

## 1. ShotsOnTargetModel (NEW) — team + player NB

### Why
No SoT model exists. `betbuilder.py:68` carries a bare `("sot", (4.2, 9.0))`
prior with no attack scaling and no player layer. SoT is a high-liquidity book
market (player SoT 1+/2+ are among the most-offered props). Team shots↔xG is
0.696, so SoT inherits a clean, defensible xG link.

### Structure
- **Team SoT** = `shots_mean(λ) × on_target_ratio`, shots scaled off team xG with
  elasticity 0.6 (matches `betbuilder.SHOT_ELASTICITY`), `on_target_ratio`
  default 0.345 (WC literature; refit when SoT data lands).
- **Player SoT** = Poisson-thinning of team SoT by the player's shot share
  (from `props_players.csv` shots/90; mean 1.01 sh/90, max 4.98), minutes-prorated.
- Counts are NB. Team k from shots overdispersion (k≈7.9 team shots; SoT slightly
  tighter, default k=6.0). Player k=4.0 (matches `betbuilder.PLAYER_DISPERSION`).

### Sketch (`props.py` style)
```python
class ShotsOnTargetModel:
    """Team & player shots-on-target as NB, scaled off xG via shots.

    Team SoT mean = base_shots * (1 + elasticity*(lambda_team/base_lambda - 1))
                    * on_target_ratio
    Player SoT    = team_sot_mean * player_shot_share * (minutes/90)

    SoT is absent from the StatsBomb props pull, so on_target_ratio is an
    external prior (WC ~0.345) until src/wca/data/statsbomb.py exposes SoT.
    """
    def __init__(self, base_shots=12.5, base_lambda=1.35, on_target_ratio=0.345,
                 elasticity=0.6, dispersion=6.0, player_dispersion=4.0):
        # validate >0, ratio in (0,1], elasticity in [0,1]
        ...

    def team_mean(self, lambda_team):
        rel = lambda_team / self.base_lambda - 1.0
        shots = max(self.base_shots * (1.0 + self.elasticity * rel), 0.0)
        return shots * self.on_target_ratio

    def prob_team_over(self, line, lambda_team):
        return _nb_sf_over(line, self.team_mean(lambda_team), self.dispersion)

    def fair_odds_team_over_under(self, line, lambda_team):
        return _fair_odds(self.prob_team_over(line, lambda_team))

    def player_mean(self, lambda_team, player_shot_share, expected_minutes=90.0):
        # player_shot_share in [0,1]; thinning of the team SoT process
        return self.team_mean(lambda_team) * player_shot_share * (expected_minutes/90.0)

    def prob_player_over(self, line, lambda_team, player_shot_share,
                         expected_minutes=90.0):
        mu = self.player_mean(lambda_team, player_shot_share, expected_minutes)
        return _nb_sf_over(line, mu, self.player_dispersion)
```

### Priors from history
- `base_shots=12.5` (TEAM shots mean, exact from corpus).
- player_shot_share derived per-team from `props_players.csv`:
  `share_i = shots_i / Σ_team shots`, EB-shrunk toward a positional prior (see §6).
- `on_target_ratio` is the one external number — flag for refit.

### Dependents & switch
- **NEW model**: zero existing callers, so no break risk on introduction.
- Wire into `betbuilder.py`: replace the `TEAM_PRIORS["sot"]` bare tuple path
  (`betbuilder.py:68`, `team_total_*`) so SoT routes through this class. Keep
  `TEAM_PRIORS["sot"]` as the *fallback* when `lambda_team` is unavailable.
- Add `team_total_sot(..., model=None)` / `player_sot(...)` mirroring
  `team_total_shots` (`betbuilder.py:341`). Default `model = model or ShotsOnTargetModel()`.

### Live market & settlement
- Books: `player_shots_on_target` (1+/2+/3+), `alternate_*`, team SoT totals.
  Feed live via `theoddsapi.get_event_odds(..., markets="player_shots_on_target,...")`
  (`theoddsapi.py:178` event-odds path handles 422-prone prop markets).
- Settlement identity: count SoT in regulation+ET per book rules; settle vs
  realized SoT (needs a post-match SoT feed — currently absent in `wc2026_results.json`,
  so CLV/closing-line is the near-term scoring path, not result-settled P&L).

### Edge thesis
Books price player SoT off season club rates; an xG-coupled, minutes-aware,
tournament-context model corrects for (a) opponent strength (favourites'
forwards get more SoT vs weak R32 sides), (b) rotation minutes. Shots↔xG 0.696
makes the team anchor sharp; the edge is concentrated in mispriced *minutes* and
*opponent* adjustments the book's flat club-rate ignores.

---

## 2. CornersModel (DE-HARDCODE) — team EB priors + keep 8.97 fallback

### Why
`CornersModel.__init__` hard-codes `base_corners=8.97` for *every* fixture
(`props.py:91`). The fixture-to-fixture corner mean is then driven only by the
damped xG term (elasticity 0.3) — but corners↔xG is only 0.315, so almost all
real cross-fixture variance (high-corner teams vs low) is thrown away. The lever
with signal is the **team identity**, not xG.

### Structure (replace 8.97 with team priors + EB shrinkage)
- Per-team corners-for / corners-against per-match rate, Empirical-Bayes shrunk
  toward the league mean 4.484/team (handles small WC samples — most teams have
  3–7 matches).
- Match mean = `(corners_for_home_prior · def_factor_away)` combined with the
  symmetric away term, then a *light* xG nudge (keep elasticity but as a
  second-order term).
- **Fix the team-dispersion bug:** team corners use k≈9.5 (overdispersed), not
  the total k=157.5. Add `team_dispersion` param.

EB shrinkage (Morris/James-Stein form for a rate from `n` matches):
```
prior_team = (n*rate_team + tau*league_mean) / (n + tau)
```
with `tau` ≈ league-variance-based weight (default tau≈4 matches → a team with
4 WC matches is shrunk 50% toward 4.484).

### Sketch
```python
class CornersModel:
    def __init__(self, base_corners=8.97, base_goals=3.07, dispersion=157.5,
                 elasticity=0.30, team_dispersion=9.5,        # NEW: team-corner k
                 team_priors=None, league_team_mean=4.484,    # NEW: EB priors
                 eb_tau=4.0):                                 # NEW: shrinkage strength
        # all existing args keep their exact defaults -> back-compat
        self.team_priors = team_priors or {}   # {team: {"for": r, "against": r, "n": k}}
        self.team_dispersion = float(team_dispersion)
        self.league_team_mean = float(league_team_mean)
        self.eb_tau = float(eb_tau)
        ...

    def _eb_rate(self, team, side):
        rec = self.team_priors.get(team)
        if not rec:                                  # FALLBACK to flat base
            return self.league_team_mean
        n = rec.get("n", 0); r = rec.get(side, self.league_team_mean)
        return (n*r + self.eb_tau*self.league_team_mean) / (n + self.eb_tau)

    def mean_total(self, lambda_home, lambda_away, home=None, away=None):
        if home is None or away is None:           # EXACT legacy path preserved
            rel = (lambda_home + lambda_away)/self.base_goals - 1.0
            return max(self.base_corners*(1.0 + self.elasticity*rel), 0.0)
        # NEW path: team EB priors (corners-for vs corners-against blend) + xG nudge
        cf_h = 0.5*(self._eb_rate(home,"for") + self._eb_rate(away,"against"))
        cf_a = 0.5*(self._eb_rate(away,"for") + self._eb_rate(home,"against"))
        base = cf_h + cf_a
        rel = (lambda_home + lambda_away)/self.base_goals - 1.0
        return max(base*(1.0 + self.elasticity*rel), 0.0)

    def prob_team_over(self, line, lambda_team, lambda_opponent,
                       team=None, opponent=None):
        mu = self.team_mean(lambda_team, lambda_opponent, team, opponent)
        return _nb_sf_over(line, mu, self.team_dispersion)   # FIX: team k, not 157.5
```
`mean_total`/`team_mean` gain **optional** `home/away`/`team/opponent` names. When
omitted, byte-for-byte the old behaviour (8.97 path, k=157.5) — so every current
caller is unaffected.

### xG–corner coupling, revisited
Keep elasticity 0.30 as a small second-order nudge but demote it: with team EB
priors carrying the cross-fixture signal (and corners↔shots 0.59 > corners↔xG
0.315), consider feeding a **shots** proxy instead of/in addition to xG in a
later iteration. For now: EB team prior is primary, xG nudge secondary, document
that the elasticity is the weaker lever.

### Priors from history
- `league_team_mean=4.484` (exact), `team_dispersion=9.5` (exact MoM).
- `team_priors`: built offline from `props_matches.csv` corners-for/against per
  team; for WC2026 sides absent from 2018+2022, fall back to league mean (the
  EB form does this automatically with n=0).

### Dependents & switch
- `card.py:1331` `cm = corners_model or CornersModel()` — unchanged; when names
  aren't passed it uses the legacy path. To *activate* EB, `card.py` passes
  `home/away` into `cm.mean_total(...)` (additive change, guarded).
- `accas.py:545` `CornersModel()` — unchanged default; opt-in by passing team
  priors + names.
- `betbuilder.py:374` `team_corners(...)` — pass `team/opponent` through to
  `prob_team_over`; default None keeps current output.
- `nextmatch.py:481`, bot, `sitedata.py` — all use the default-constructed model;
  unaffected until they choose to pass names.
- **Fallback guarantee:** empty `team_priors` ⇒ every `_eb_rate` returns the
  league mean ⇒ `mean_total` with names ≈ flat base; with names omitted ⇒ exact
  8.97 legacy. Two independent fallbacks.

### Live market & settlement
- Books: `totals_corners` (match O 8.5/9.5/10.5), `team_totals_corners`,
  `alternate_totals_corners`. Add to `EVENT_MARKETS` in `wca_event_ev.py:38`.
- Settlement: count corners awarded in regulation (book-specific on ET); CLV vs
  closing corner line is the immediate scoring path.

### Edge thesis
The book's corner line is essentially a team-pair lookup; our EB priors replicate
that *and* add the correct overdispersed team tails (the k=9.5 fix widens
team-corner O/U tails the current 157.5 wrongly compresses). Edge sits in (a)
team-corner *tails* the book under-prices/over-prices via thin Poisson-like
pricing, and (b) opponent-adjusted corners-against that flat lines miss.

---

## 3. CardsModel (REFIT) — referee + team-aggression + foul-rate priors

### Why
`CardsModel` multiplies `base_cards=3.41` by `aggression_home·aggression_away`
that both **default to 1.0** (`props.py:175`), i.e. today every match prices at
the base rate. Real signal: team fouls↔team cards r=0.508, and referee identity
is the single largest exogenous driver of card totals in the literature.

### Structure
Mean = `base_cards × ref_factor × agg_home × agg_away × stakes_mult`, where:
- `agg_team` derived from the team's **foul rate** via FoulsModel (§4):
  `agg_team = (foul_rate_team / league_foul_mean)^beta`, β≈0.5 (cards are a
  sub-linear function of fouls; r=0.508 not 1.0). League foul mean 14.262/team.
- `ref_factor` = referee EB-shrunk cards-per-match / league mean (referee
  appointments published pre-match; until a referee table lands, default 1.0 ⇒
  back-compat).
- `base_cards=3.41` (exact), `dispersion=6.9` (≈ exact MoM k=7.0).

### Sketch
```python
class CardsModel:
    def __init__(self, base_cards=3.41, dispersion=6.9,
                 league_foul_mean=14.262, foul_beta=0.5,   # NEW
                 ref_factor=1.0):                          # NEW (per-call overridable)
        ...

    @staticmethod
    def aggression_from_fouls(foul_rate_team, league_foul_mean=14.262, beta=0.5):
        if foul_rate_team is None or foul_rate_team <= 0:
            return 1.0                                     # FALLBACK
        return (foul_rate_team / league_foul_mean) ** beta

    def mean_total(self, aggression_home=1.0, aggression_away=1.0,
                   stakes_mult=1.0, ref_factor=None):
        rf = self.ref_factor if ref_factor is None else ref_factor
        # existing signature preserved; ref_factor is new & optional
        return self.base_cards * aggression_home * aggression_away * stakes_mult * rf
```
`aggression_home/away` keep defaulting to 1.0, so **all current call sites
(`card.py:1332`, `accas.py:546`) reproduce 3.41 exactly**. Callers opt in by
computing `agg = CardsModel.aggression_from_fouls(team_foul_rate)` and passing it.

### Priors from history
- Team foul rates from `props_matches.csv` (`fouls_home/away`), EB-shrunk like §2.
- `foul_beta=0.5` chosen so the implied cards spread matches the 0.508
  fouls↔cards correlation (calibrate β so model card-variance-explained ≈ r²≈0.26;
  flag for a small grid-search refit).
- `ref_factor`: referee cards/match table (external) EB-shrunk to league.

### Dependents & switch
- `card.py:1332`, `accas.py:546`, bot, `sitedata.py`: default-constructed,
  aggression=1.0 ⇒ identical output. Opt-in by injecting foul-derived aggression
  + ref_factor. Backward-compatible by default.
- `betbuilder.py:490` `cards = CardsModel()` — unchanged.

### Live market & settlement
- Books: `totals_cards` (O 3.5/4.5), `team_totals_cards`, `player_to_be_carded`,
  `booking_points` (some books: 10/yellow, 25/red). Map our card NB to booking
  points via a fixed point transform if the book uses points.
- Settlement: yellow=1, red=1 (2nd-yellow → the corpus counts as one red; book
  rules vary — document per-book). CLV vs closing cards line near-term.

### Edge thesis
Books move card lines heavily on referee, but slowly on *team* aggression and
*matchup* (two high-foul sides compound). Our multiplicative foul→aggression with
referee EB captures the interaction the book's additive nudges miss; the
overdispersed k=7 NB also prices the high-card tail (O 5.5/6.5) the book often
sets with thin Poisson-like spread.

---

## 4. FoulsModel (NEW) — team NB feeding cards

### Why
No fouls model. Fouls are (a) a directly tradeable market and (b) the *input*
that makes CardsModel's aggression real (r=0.508). Team fouls mean 14.262,
var/mean 1.70, k≈20.4.

### Structure
- Team fouls NB, mean = team EB foul prior, lightly opponent-adjusted (a team
  fouls more vs strong attackers — small possession-based term; keep modest).
- Player fouls = thinning by player foul share (priors from `betbuilder`
  `PLAYER_P90_PRIORS["fouls"]=1.2` until a StatsBomb player-foul pull lands).
- Feeds CardsModel via `aggression_from_fouls`.

### Sketch
```python
class FoulsModel:
    """Team & player fouls committed as NB. Feeds CardsModel aggression."""
    def __init__(self, base_fouls=14.262, dispersion=20.4,
                 league_mean=14.262, team_priors=None, eb_tau=4.0,
                 player_dispersion=6.0):
        self.team_priors = team_priors or {}
        ...

    def team_mean(self, team=None, opponent=None):
        if not team or team not in self.team_priors:
            return self.base_fouls                  # FALLBACK to league mean
        rec = self.team_priors[team]; n = rec.get("n",0)
        r = rec.get("fouls", self.league_mean)
        return (n*r + self.eb_tau*self.league_mean)/(n + self.eb_tau)

    def prob_team_over(self, line, team=None, opponent=None):
        return _nb_sf_over(line, self.team_mean(team, opponent), self.dispersion)

    def player_mean(self, team, player_foul_share, expected_minutes=90.0):
        return self.team_mean(team) * player_foul_share * (expected_minutes/90.0)
```

### Priors from history
- `base_fouls=14.262`, `dispersion=20.4` (exact MoM). Team priors from
  `props_matches.csv` fouls columns, EB-shrunk.

### Dependents & switch
- **New** ⇒ no break risk. Becomes the source for CardsModel aggression
  (CardsModel still defaults aggression=1.0, so wiring is opt-in).
- Add `team_total_fouls` already exists (`betbuilder.py:361`) on a flat prior —
  route it through FoulsModel.team_mean when team known; keep
  `TEAM_PRIORS["fouls"]` tuple as fallback.

### Live market & settlement
- Books: `player_fouls`, team fouls totals (less liquid than cards/SoT, but
  present on some books via event-odds). Live via `theoddsapi` event-odds.
- Settlement: fouls conceded per official stats; realized-foul feed absent in
  `wc2026_results.json`, so CLV-only near-term.

### Edge thesis
Fouls markets are thin/soft (low book attention). A team-EB NB with the correct
k≈20 overdispersion can find stale lines, and the *same* foul estimate
double-duties as the CardsModel driver, so any foul edge propagates into cards.

---

## 5. Cross-model dependency graph

```
        xG λ (dixon_coles.expected_goals, props pinned to 1X2)
          │
   ┌──────┼───────────────┬───────────────┐
   ▼      ▼               ▼               ▼
Corners  ShotsOnTarget  (AnytimeScorer)  │
(EB+xG)  (shots×ratio)                    │
                                          ▼
                                     FoulsModel ──(aggression_from_fouls, β)──► CardsModel ◄── ref_factor
                                     (team EB NB)                                (×agg_h×agg_a×ref)
```
- FoulsModel is **upstream** of CardsModel (new hard dependency, but guarded:
  CardsModel aggression defaults to 1.0 so it runs standalone).
- ShotsOnTargetModel shares the xG/shots elasticity with `betbuilder` (reuse
  `SHOT_ELASTICITY=0.6`, `BASE_TEAM_LAMBDA=1.35`).
- All four read team-EB priors from one offline-built table (see §6) — single
  prior-build script, injected via constructor `team_priors`.

---

## 6. Prior-build pipeline (the missing `matchevents.py`)

Create `src/wca/data/matchevents.py` (NEW; not in scope to write now) that, from
`props_matches.csv` (+ a future StatsBomb refresh), emits a JSON:
```json
{"team": {"Brazil": {"corners_for": 5.6, "corners_against": 3.9,
                      "fouls": 13.1, "cards": 1.5, "n": 7}, ...},
 "league": {"corners_team": 4.484, "fouls_team": 14.262, "cards_team": 1.71,
            "sot_ratio": 0.345},
 "referee": {"<ref>": {"cards_pm": 4.2, "n": 9}, ...}}
```
EB shrinkage applied at **read** time inside each model (so τ is tunable without
rebuilding). This file is the single injection point for all four models'
`team_priors` and keeps `props.py` pure (no IO), matching its module contract
(`props.py:1-7`).

Player shares (SoT, fouls, scorer) come from `props_players.csv` shares,
EB-shrunk toward positional priors — wire through the existing
`betbuilder.RateStore`/`PlayerRate` path (`betbuilder.py:202-283`) so the bot's
player props inherit them.

---

## 7. Back-compat test matrix (what must stay green)

`tests/` has ~973 tests. The switch is safe iff:

1. **Default construction reproduces today's numbers.** Every new param defaults
   to the legacy value (`base_corners=8.97`, aggression=1.0, ref_factor=1.0,
   names=None → legacy code path). Existing prop-model tests pass unchanged.
2. **New optional args are additive.** `mean_total(λh, λa)` and
   `mean_total(λh, λa, home, away)` both valid; first = legacy.
3. **Fallback paths exercised:** team absent from priors ⇒ league mean; empty
   priors dict ⇒ flat base. Add tests asserting `CornersModel(team_priors={})`
   ≡ `CornersModel()` on a grid of λ.
4. **Dispersion-bug fix is the one *intended* numeric change** (team-corner tails
   widen). Snapshot tests on team-corner O/U must be **updated**, not asserted
   equal — call this out in the PR. Match-total corner numbers are unchanged.
5. New models (`ShotsOnTargetModel`, `FoulsModel`) ship with their own unit
   tests (mean monotonic in λ, NB→Poisson as k→∞, prob_over∈[0,1], thinning sums
   to team mean).

Call-site audit (all currently default-construct ⇒ safe):
`card.py:1331-1332`, `accas.py:545-546`, `nextmatch.py:481`,
`betbuilder.py:374,489-490`, plus bot/`sitedata` via `card.py`.

---

## 8. Live tradeability summary (feed is LIVE)

| Model | Book market keys (TheOddsApi) | Endpoint | Settlement | Near-term score |
|---|---|---|---|---|
| ShotsOnTarget | `player_shots_on_target`, team SoT totals | event-odds (`theoddsapi.py:178`) | SoT count, reg/ET per book | CLV (no realized-SoT feed) |
| Corners | `totals_corners`,`team_totals_corners`,`alternate_totals_corners` | event-odds; add to `EVENT_MARKETS` (`wca_event_ev.py:38`) | corners awarded, reg | CLV |
| Cards | `totals_cards`,`team_totals_cards`,`player_to_be_carded`,`booking_points` | event-odds | yellow=red=1 (book-specific 2nd-yellow) | CLV |
| Fouls | `player_fouls`, team fouls (soft) | event-odds | fouls conceded | CLV |

CLV path: log model fair vs first/closing book line into
`data/model_predictions_log.jsonl`-style ledger (`predledger/*`), score against
the closing line. Result-settled P&L waits on a post-match event-stat feed
(absent in `wc2026_results.json` today). **No profitability claimed** — these are
calibration/edge hypotheses to validate against closing lines.

---

## 9. xG-too-low note (cross-ref to Track A)

User reports statistically significant evidence the model forecasts **xG too
low**. This *directly* hits these models, because Corners (xG nudge), SoT (xG
elasticity) and AnytimeScorer (λ_team) all scale on the same λ. If the goal
*total* is biased low (the total rests on `base_goals≈3.07`/`BASE_TEAM_LAMBDA`
assumptions, not pinned by 1X2 — see `dixon_coles.expected_goals`, `props.py`
docstring), then:
- SoT and corner means are biased **low** → systematic lean to the **Over** would
  be expected if the bias is real. Build the prior pipeline to read λ *after* the
  Track-A total correction, not before.
- Recommend the SoT/corner elasticity refit be done **jointly** with the λ-total
  fix so we don't double-count the correction. Flagged as an open dependency on
  Track A's `base_goals`/total recalibration.
