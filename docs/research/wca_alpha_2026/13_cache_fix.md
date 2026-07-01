# 13 — Stale advancement-model cache: diagnosis & fix design

_Generated 2026-06-30. READ-ONLY w.r.t. production: no `src/` edits, no `.db`
writes, no regeneration of the production cache. Code below is **sketch only**._

## TL;DR

`data/advancement_models.pkl` was written by an **older branch**
(`codex/report-send-command`, worktree `worktrees/report-send`) whose
`EloRater` had **no `initial_ratings` attribute**. The current `main` `EloRater`
references `self.initial_ratings` inside `get_rating()`. Because `pickle`
restores an instance by writing its saved `__dict__` directly and **bypasses
`__init__`**, the un-pickled rater is missing the attribute, and the first call
into the Elo leg raises:

```
AttributeError: 'EloRater' object has no attribute 'initial_ratings'
```

The error is **not** raised at `pickle.load()` (the object deserialises fine);
it fires the first time the rater is *used* (`get_rating` / `expected_home`).
`scripts/wca_advancement.py::_load_or_fit_models` wraps the load + first use in a
broad `except Exception` and silently **falls back to a fresh ~2-minute refit**.
The bracket pipeline therefore still runs, but on a fit that is **not** the
cached, reviewed one — a silent degradation, not a hard failure.

Recommended fix: **(c) move the cache off pickle to versioned JSON of model
params** (each sub-model already has `to_dict`/`from_dict`), with **(b) a
defensive `__setstate__`** as a cheap immediate stopgap. A one-off pickle
re-gen (a) only papers over the next schema drift.

---

## 1. Reproduction (read-only, venv)

Run from the repo root with `.venv/bin/python`.

```python
import pickle
obj = pickle.load(open("data/advancement_models.pkl", "rb"))   # SUCCEEDS
r = obj.rater
r.get_rating("Brazil")        # AttributeError: ... no attribute 'initial_ratings'
r.expected_home("Brazil", "Argentina", neutral=True, host="USA")  # same error
```

Observed:

- `pickle.load` returns a `wca.card.FittedModels` with fields
  `rater: EloRater`, `elo_outcome: EloOutcomeModel`, `dc: DixonColesModel`,
  `n_matches: int`.
- The pickled `EloRater.__dict__` keys are exactly:
  `['home_advantage', 'host_advantage', 'initial_rating', 'k_factors', 'ratings']`
  — **`initial_ratings` (plural) is absent.**
- `hasattr(r, "initial_ratings")` → `False`.
- `r.get_rating("Brazil")` → `AttributeError: 'EloRater' object has no attribute 'initial_ratings'`.
- `r.expected_home(...)` → same (it routes through `_rating_diff` → `get_rating`).

This matches the Workflow-2 report verbatim ("EloRater missing initial_ratings
attribute — cache predates a code change").

### What `get_rating` looks like on each side

Current `main` — `src/wca/models/elo.py:218-222`:

```python
def get_rating(self, team: str) -> float:
    return self.ratings.get(
        team,
        self.initial_ratings.get(team, self.initial_rating),   # <-- needs self.initial_ratings
    )
```

Branch that wrote the pickle — `worktrees/report-send/src/wca/models/elo.py:218-220`
(`codex/report-send-command`, commit `251c57a`):

```python
def get_rating(self, team: str) -> float:
    return self.ratings.get(team, self.initial_rating)   # no initial_ratings layer
```

---

## 2. Root cause

### 2a. The schema change

`initial_ratings` (a per-team initial-rating seed) was **added** to `EloRater`
in commit **`cebefb4`** (2026-06-23, "Salvage Codex work: backtest harness, Elo
calibration, PM trade-logging, market-anchored advancement", #25). Confirmed via
`git log -S "initial_ratings" -- src/wca/models/elo.py` (single hit).

The current `__init__` (`src/wca/models/elo.py:177-203`) always sets
`self.initial_ratings`, and `fit_models` now *uses* it: it seeds Elo from the
Dixon-Coles socio-economic prior —
`rater = EloRater(initial_ratings=elo_initial_ratings, k_factors=k_factors)`
(`src/wca/card.py:587`, with `elo_seed_from_dc_prior=True` by default, scaled by
`DEFAULT_ELO_POINTS_PER_DC_PRIOR = 400.0`, `src/wca/card.py:58`).

### 2b. Why the cache is stale

The production `.pkl` (mtime 2026-06-29 22:10) was written by a process running
the **`codex/report-send-command`** worktree, whose `elo.py` still has the
pre-`cebefb4` `EloRater` with **no `initial_ratings`**. Verified by scanning all
worktrees: every other worktree's `elo.py` contains `initial_ratings`; only
`worktrees/report-send/src/wca/models/elo.py` is **MISSING** it. So the file
mtime being *after* `cebefb4` is a red herring — it was written by a stale
checkout, not stale code on `main`.

### 2c. Why pickle is the fragile link

`pickle` for a normal object stores the class reference
(`wca.models.elo.EloRater`) plus the instance `__dict__`. On load it
**instantiates without calling `__init__`** and assigns the saved `__dict__`.
Any attribute that newer code expects but that wasn't in the saved `__dict__`
is simply absent. This makes the on-disk format **coupled to the live class
schema**: add/rename/remove an attribute and every older pickle silently breaks
the moment the attribute is touched. This is a classic pickle-versioning hazard,
and it will recur on the *next* attribute change unless the format changes.

---

## 3. Operational impact

- **Not a crash, a silent downgrade.** `_load_or_fit_models`
  (`scripts/wca_advancement.py:50-62`) catches the `AttributeError` and prints
  `"Cache load failed (...); refitting."` to **stderr**, then refits. If stderr
  isn't surfaced in the Workflow log view, the degradation is invisible.
- **The cache is dead weight.** Every advancement run pays the full ~2-minute
  refit (the cache exists precisely to avoid this), so the cache provides zero
  benefit until regenerated by current code.
- **Reproducibility risk.** The reviewed/blessed fit on disk is not the one
  used; runs silently depend on whatever `fit_models` + current results CSV
  produce at call time. If the results file or fit defaults drift, advancement
  edges move without an audit trail tying them to a named cache artifact.
- **Blast radius = the whole Elo leg of advancement.** `advancement.py` blends
  Elo + Dixon-Coles (+ market): `p = w.elo*e + w.dc*d + w.market*m`
  (`src/wca/advancement.py:248-250`); the Elo probabilities come from
  `elo_probs(...)` → `rater.expected_home` → `get_rating`. A rater that raises
  on `get_rating` would zero out the Elo contribution entirely if the refit
  fallback didn't exist. With the fallback, the numbers are *plausible* but
  *unaudited*.
- **Scope note (per steer):** this is the liquid outright/advancement pipeline,
  where the model has **no proven edge**. The cache bug does not directly touch
  the match-event (SoT/corners/cards/xG) work that is the trading focus — but a
  silently-refitting Elo/DC core is a shared dependency for any match-event
  model that reuses these ratings, so it is worth fixing before building on top.

---

## 4. Fix options

### (a) One-off re-generation via the canonical fit path

The writer is **`scripts/wca_advancement.py`**; the canonical fit is
`wca.card.fit_models` (called by `_load_or_fit_models`,
`scripts/wca_advancement.py:50-79`). Regeneration is simply running the script
with `--refit` (which forces a fresh fit and overwrites the cache) from current
`main`:

```
python scripts/wca_advancement.py --refit        # writes data/advancement_models.pkl
```

- **Pro:** zero code change; restores a valid, current-schema cache immediately.
- **Con:** does **not** prevent the next drift. The moment another `EloRater` /
  `DixonColesModel` / `EloOutcomeModel` attribute is added or renamed, every
  existing pickle breaks again. Treats the symptom.
- _(Guardrail: this regenerates the production cache, so it is **out of scope**
  here — listed for completeness as the operator's quick unblock.)_

### (b) Backward-compatible `__setstate__` / default for `initial_ratings`

Make `EloRater` tolerant of old pickles by supplying defaults for any attribute
introduced after a pickle may have been written. **Sketch only — do not apply:**

```python
# src/wca/models/elo.py  (SKETCH — not applied)
class EloRater:
    # Attributes that may be absent in pickles written before they were added.
    _PICKLE_DEFAULTS = {
        "initial_ratings": dict,   # callable -> fresh empty dict per instance
    }

    def __setstate__(self, state: dict) -> None:
        # Restore the saved attributes, then backfill any missing newer ones.
        self.__dict__.update(state)
        for name, default in self._PICKLE_DEFAULTS.items():
            if name not in self.__dict__:
                self.__dict__[name] = default() if callable(default) else default
```

An even lighter variant: make `get_rating` defensive —
`getattr(self, "initial_ratings", {})` — but that scatters compat logic across
every accessor and is easy to miss on the next attribute. `__setstate__`
centralises it.

- **Pro:** old pickle becomes usable again with `initial_ratings = {}`, which is
  *semantically correct* here — the pickled rater already has concrete `ratings`
  for all 336 teams, so `get_rating` never falls through to the (empty)
  initial-ratings layer for any known team. Verified: a reconstructed rater with
  `initial_ratings={}` returns Brazil = 2062.03, 336 teams intact.
- **Con:** still pickle; the `_PICKLE_DEFAULTS` table must be maintained forever,
  and **renamed** or **removed** attributes are not covered (only additions
  with a safe default). Compat debt accumulates.

### (c) **[RECOMMENDED]** Versioned JSON of params instead of pickle

Each sub-model already serialises to a plain dict via `to_dict`/`from_dict`
(`EloRater`: `src/wca/models/elo.py:393-419`; `EloOutcomeModel`:
`...:582-605`; `FittedModels` would need a thin wrapper). Replace the pickle
cache with a **schema-versioned JSON** document and reconstruct via the
`from_dict` constructors, which already use `.get(key, default)` and are
therefore **forward/backward tolerant** by construction.

```python
# scripts/wca_advancement.py  (SKETCH — not applied)
import json

CACHE_SCHEMA_VERSION = 2   # bump whenever a to_dict/from_dict contract changes

def _models_to_doc(models) -> dict:
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "n_matches": models.n_matches,
        "rater": models.rater.to_dict(),
        "elo_outcome": models.elo_outcome.to_dict(),
        "dc": models.dc.to_dict(),          # requires DixonColesModel.to_dict/from_dict
    }

def _models_from_doc(doc: dict):
    from wca.card import FittedModels
    from wca.models.elo import EloRater, EloOutcomeModel
    from wca.models.dixon_coles import DixonColesModel
    ver = doc.get("schema_version")
    if ver != CACHE_SCHEMA_VERSION:
        raise ValueError(f"cache schema {ver} != {CACHE_SCHEMA_VERSION}; refit")
    return FittedModels(
        rater=EloRater.from_dict(doc["rater"]),
        elo_outcome=EloOutcomeModel.from_dict(doc["elo_outcome"]),
        dc=DixonColesModel.from_dict(doc["dc"]),
        n_matches=int(doc["n_matches"]),
    )

# in _load_or_fit_models: write json.dump(_models_to_doc(models)); read with a
# version check. A mismatched/old version triggers an *explicit, logged* refit
# instead of a silent AttributeError swallowed by `except Exception`.
```

Path: keep `.pkl` for one release writing **both** `.pkl` and `.json`, prefer
`.json` on read, then drop `.pkl`. Cache file becomes
`data/advancement_models.json`.

- **Pro:** decouples on-disk format from live class layout. `from_dict`'s
  `.get(..., default)` already absorbs *added* fields; the explicit
  `schema_version` gate turns *incompatible* changes into a **loud, intentional
  refit** rather than a silent swallowed exception. JSON is human-inspectable
  and diffable (you can eyeball that Brazil's Elo is sane). No
  arbitrary-code-execution surface (a secondary pickle hazard).
- **Con:** requires adding `to_dict`/`from_dict` to `DixonColesModel` (verify it
  has them; the Elo pair already do) and a small `FittedModels` (de)serialiser.
  Slightly more code than (a)/(b). JSON of a 336-team rating table is larger
  on disk than pickle, but trivially so (~tens of KB).

### Recommendation

Adopt **(c)** as the durable fix; it removes the class-drift coupling that
caused this and makes the *next* drift a logged, intentional refit. Ship **(b)
`__setstate__`** alongside as a one-line-of-effort safety net for any pickle
still in flight during the migration. Use **(a) `--refit`** only as the
operator's immediate unblock — it does not address the underlying fragility.

---

## 5. Evidence index (file:line)

- `src/wca/models/elo.py:177-203` — `EloRater.__init__` sets `self.initial_ratings`.
- `src/wca/models/elo.py:218-222` — `get_rating` reads `self.initial_ratings` (the failing line on `main`).
- `src/wca/models/elo.py:393-419` — `EloRater.to_dict` / `from_dict` (the JSON path; `from_dict` uses `.get` defaults).
- `worktrees/report-send/src/wca/models/elo.py:218-220` — older `get_rating`, **no** `initial_ratings` (the writer of the stale pickle); branch `codex/report-send-command`, commit `251c57a`.
- `src/wca/card.py:58` — `DEFAULT_ELO_POINTS_PER_DC_PRIOR = 400.0`.
- `src/wca/card.py:494-499` — `FittedModels` dataclass fields.
- `src/wca/card.py:570-587` — `fit_models` seeds `EloRater(initial_ratings=...)`.
- `scripts/wca_advancement.py:38-79` — `_load_or_fit_models`: pickle cache + broad-except refit fallback (the silent-degradation site).
- `src/wca/advancement.py:236-250` — Elo leg (`elo_probs` → `expected_home` → `get_rating`) blended into advancement probabilities.
- git: `cebefb4` (2026-06-23) added `initial_ratings`; production `.pkl` mtime 2026-06-29 22:10 written by stale `report-send` checkout.
- Recovery verified read-only: reconstructing via `from_dict` with
  `initial_ratings={}` yields Brazil = 2062.03 Elo over 336 teams — i.e. the old
  pickle's ratings are fully usable once the missing attribute is backfilled.
