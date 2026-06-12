# Model Prediction Snapshots

This directory stores timestamped snapshots of the World Cup Alpha model's output, allowing you to track how predictions and edges change as the model improves and odds move.

## Snapshot Structure

Each snapshot (named `YYYYMMDD-HHMMSS/`) contains:

- **card.md** — The full matchday card with:
  - All betting recommendations (picks with edges)
  - Scoreline predictions (per-fixture probability distribution)
  - Over/under and BTTS summaries
  - Model vs market probabilities

- **site_data.json** — Complete JSON export of all model data:
  - All open and closed positions
  - Predictions (fixtures with scorelines and probabilities)
  - P&L curves (cumulative realized P&L by settlement time)
  - CLV metrics (closing-line value, calibration)
  - Per-venue and per-platform breakdowns

- **metadata.json** — Snapshot metadata:
  - Timestamp and model version
  - Model blend weights (Elo / Dixon-Coles / Market)
  - Bankroll and Kelly fraction in effect
  - Fixture count and position summary

## Comparing Snapshots

### Track Edge Evolution

Compare `picks` and `edge` across snapshots to see how model improvements or odds moves affect recommended stakes:

```bash
# Show all picks from snapshot A vs B
jq '.picks[] | {match, selection, edge, stake}' data/snapshots/A/site_data.json
jq '.picks[] | {match, selection, edge, stake}' data/snapshots/B/site_data.json
```

### Track Scoreline Forecast Changes

Compare fixture-level predictions (probabilities, scorelines) across time:

```bash
# Show scorelines for "Canada vs Bosnia" across snapshots
grep -A 10 "Canada vs Bosnia" data/snapshots/*/card.md
```

### Track Model Calibration

Compare CLV, Brier score, and log-loss before and after model changes:

```bash
# Extract CLV metrics from all snapshots
jq '.clv' data/snapshots/*/site_data.json
```

## Workflow

When you improve the model (tune blend weights, add features, change devigging method):

1. **Make the change** to model code
2. **Rebuild the card:** `python scripts/wca_build_card.py`
3. **Regenerate site data:** `python scripts/wca_site.py`
4. **Create a new snapshot:** (manual or via a script)
5. **Compare** to the previous snapshot to validate the improvement

## Use Cases

- **Model drift detection** — Did the model's recommendations change? By how much?
- **Odds-move tracking** — Same model prediction, but market odds improved/worsened?
- **Model validation** — After tuning weights, did calibration (Brier/log-loss) improve?
- **Historical audit trail** — What did the model say on match day X for fixture Y?
