# WC26 winner: model vs market-anchored favourites

_Generated 2026-06-18. Supersedes the "Argentina is the favourite" reading of the
pure-model advancement sim._

## TL;DR

Argentina is the **model's** favourite, not the **market's**. Once the
tournament sim's winner output is anchored to the live Polymarket "World Cup
Winner" book, **France** is the clear favourite and Argentina falls to 3rd–4th.
The earlier "Argentina #1" call was a pure Elo single-match over-reaction
(see [under_the_hood / advancement_edges](advancement_edges.md) and the
Argentina post-mortem). The price history shows the market moved **opposite** to
the model across matchday 1.

## The correction

`src/wca/advancement.py::make_prob_fn` drives the sim on a **50/50 Elo+DC blend
with no market term**, justified by the claim that *"no knockout odds exist"*.
That claim is **false**. Polymarket prices the knockout/winner/group surface:

- `find_world_cup_markets()` returns ~448 WC events including a 60-market
  **"World Cup Winner"** book (per-team "Will X win the 2026 FIFA World Cup?"),
  **"World Cup Group X Winner"**, and per-team **reach round-of-16 / QF / SF /
  final** and **advance-to-knockout** markets.
- The full **share-price history** is fetchable from the CLOB endpoint
  `https://clob.polymarket.com/prices-history` (now wrapped by
  `wca.data.polymarket.get_prices_history`).

These markets settle on **extra-time/penalties** — the correct basis for an
outright/advancement bet — and must stay on their own settlement track, never
mixed with 90-minute legs (documented fake-arb trap, see `TODO.md`).

## Redo: winner probability under three weightings

Model = 20,000-sim Elo+Dixon-Coles tournament run, conditioned on all 24 played
group results (cached fit `data/advancement_models.pkl`). Market = de-vigged
Polymarket winner book (48 teams, overround 1.023). Blend = `w·model +
(1−w)·market`.

| Rank | Model only (w=1.0) | Market-anchored (w=0.5) | Market only (w=0.0) |
|---|---|---|---|
| 1 | **Argentina 17.8%** | France 14.8% | France 18.0% |
| 2 | Spain 16.1% | Spain 14.7% | Spain 13.3% |
| 3 | France 11.6% | **Argentina 14.7%** | England 12.6% |
| 4 | England 9.8% | England 11.2% | **Argentina 11.5%** |
| 5 | Brazil 9.7% | Brazil 8.1% | Portugal 7.2% |
| 6 | Colombia 5.8% | Portugal 5.6% | Brazil 6.5% |

**Argentina does not survive as favourite once anchored.** At w=0.5 it is #3
(tied ~14.7% with Spain/France); the market alone puts it #4.

## What the market actually did across matchday 1

Polymarket winner share-price history (`get_prices_history`, fidelity 720):

| Team | start | now | move |
|---|---|---|---|
| France | 16.5% | 18.4% | **+1.9 pp** |
| Argentina | 14.0% | 11.8% | **−2.2 pp** |
| Spain | 16.0% | 13.7% | −2.4 pp |
| England | 13.5% | 12.8% | −0.7 pp |

The market **faded Argentina and backed France** — the opposite of the model,
which promoted Argentina to #1. This is the strongest evidence that the model's
call is an Elo artifact, not a corroborated signal.

## Design: anchoring the sim to the market

New building blocks (shipped, `src/wca/data/polymarket.py`):

- `get_prices_history(token_id, fidelity=, interval=)` — CLOB price-history
  series `[{ts, price}]`.
- `winner_market_implied(events=None)` — de-vigged per-team winner probability
  (multiplicative normalisation of the 48 YES prices to sum 1).

Recommended next step (staged, not yet wired into the live sim): blend the sim's
**stage/winner outputs** (`run_advancement` columns `P(win)`, `P(SF)`, …) toward
the de-vigged market at **w ≈ 0.5**, per market family:

- **Winner**: `winner_market_implied()` (48-way normalised).
- **Group winner**: normalise each 4–5-way "Group X Winner" event.
- **Reach-stage / advance**: per-team binary YES price (fee/spread adjusted),
  no cross-normalisation.

Anchor at the **stage-probability output level**, not per-match — the market does
not price every internal fixture, only the stage outcomes. Keep the pure-model
column available for the "model disagreement" edge view.

## Reproduce

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import pickle
from wca.advancement import run_advancement, load_played_group_results
from wca.data.polymarket import winner_market_implied
from wca.data.teamnames import canonical
m = pickle.load(open("data/advancement_models.pkl","rb"))
sim = run_advancement(m, n_sims=20000, seed=42, results=load_played_group_results())
model = {canonical(t): r["P(win)"] for t,r in sim.iterrows()}
mkt = {canonical(k): v for k,v in winner_market_implied().items()}
for w in (1.0,0.5,0.0):
    bl = sorted(((t, w*model[t]+(1-w)*mkt[t]) for t in set(model)&set(mkt)), key=lambda x:-x[1])
    print(w, bl[:4])
PY
```
