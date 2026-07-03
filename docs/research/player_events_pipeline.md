# Player-event pipeline — schema, migration, coverage limits (2026-07-03)

## What changed

Player-prop probabilities were a pure structural cascade off team lambda
through two constants (`SHOTS_PER_GOAL=10.0`, `SOT_PER_SHOT=0.35`): the
shrinkage machinery (`shrink_rate`, `PLAYER_P90_PRIORS`) existed but had no
empirical sample to shrink toward, so it always fell through (confirmed live:
Salah SoT priced with zero shrinkage). Now:

```
data/player_events.db          (NEW — append-only, survives everything)
  player_matches(player, team, match_id, date, competition, minutes,
                 shots, sot, goals, assists, yellows, reds,
                 corners_taken, source, ingested_utc)
  PRIMARY KEY (player, team, match_id) — re-ingest replaces, never dups

data/players.db                (existing aggregate — rebuilt atomically by
  players/team_rates/squads     build_players_db; per-match history is NOT
                                in this file BY DESIGN: the rebuild replaces
                                the whole file via os.replace)
```

Resolution order inside `wca.models.playerprops.rates_from_players_db` —
the single choke point every priced surface goes through:

1. `player_events.db` per-match empirics → `PlayerPropRates` with TRUE
   `sample_minutes` → `shrink_rate()` finally engages;
2. aggregate `players.db` (SoT only, moderate-evidence default) — the old path;
3. `None` → structural derivation off team lambda (unchanged fallback).

**Migration path for callers: none required.** `price_player_props`,
`price_fixture_props[_detailed]`, `scripts/wca_player_props.py` and
`/betbuilder`'s `RateStore` keep their signatures; the empirics arrive through
the choke point. The only new (optional) parameter is
`rates_from_players_db(..., events_db_path=)`.

## Sources & ingest

- **StatsBomb open-data WC2018+2022** (`--from-statsbomb` backfill, and the
  daily `playersdb` job): real per-match rows, ~128 matches, minutes from
  Starting-XI/substitution events, FIFA second-yellow logic reused from the
  tested `statsbomb.player_shares`. This is the same source that always fed
  `props_players.csv` — now persisted per-match instead of as a static
  aggregate snapshot.
- **`analyst_csv`** (`--csv rows.csv`): the ONLY current-tournament path.
  There is no public per-player event feed for WC2026 in our stack (StatsBomb
  open-data does not carry it; TheOddsAPI is prices only). Column contract on
  the CLI docstring. Rows carry `source='analyst_csv'` so provenance is never
  ambiguous.
- `corners_taken`, not "corners won": StatsBomb attributes corner *kicks* to
  the taker; "won" has no clean attribution — we store what the data supports.

## Correlation layer (`wca.models.propcorr`)

(team result × player prop) and (advancement × player prop) builder legs are
now priced jointly, not as naive products: conditional on team goals `g`, the
player rate scales by `1 + BETA·(g/λ_team − 1)` (BETA=0.7, explicit, re-fit it
from `player_events.db` when 2026 sample allows; BETA=0 provably recovers
independence — tested). Advancement legs make the ET/pens coin explicit
(`p_win_given_level`, default 0.5) and FLAG that the prop itself is 90-minute
sportsbook basis. Typical magnitudes at default BETA: win×over uplift ~1.1–1.3,
loss×over ~0.6–0.8 — exactly the correction the Egypt+Salah parlay evaluation
lacked.

## Coverage limits — read before trusting a number

`SHRINK_K = 6` effective matches. Empirical weight = n_matches/(n_matches+6):

| history behind a player | empirical weight |
|---|---|
| 2 matches (typical 2026-only player mid-tournament) | 25% |
| 4 matches (deep run, no prior WC) | 40% |
| 2026 run + prior-WC history (e.g. Salah 2018) | 45–60% |

So for MOST players in a short tournament the **priors still dominate** —
that is intended (the alternative is trusting a 2-match SoT rate). Expect
prop prices to move meaningfully off the structural cascade only for players
with prior-WC minutes or deep 2026 runs. The analyst CSV is the lever: every
ingested matchday adds ~16%-points of empirical weight per player.

## Known gaps (deliberate, documented)

- `data/prop_calibration.json` (the Action Desk `event_props` input) has NO
  live generator — only the dead `build_benchmarks.py` ever referenced
  writing it; `wca_betrecs.py` tolerates its absence. Wiring the scanner to
  emit it is the natural next PR once 2026 rows exist to make it worthwhile.
- No live 2026 event feed: the pipeline is ingest-ready, the feed is manual.
- `exposure_corr` still owns 1X2↔1X2; `propcorr` covers result↔prop. A
  whole-book bridge (prop exposure inside the scoreline-matrix book check)
  is future work.
