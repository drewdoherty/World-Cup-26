"""Benchmarking harness: score the generated card + commands against actual
outcomes over time (calibration, hit rate, CLV vs close, ROI) broken down by
market / venue / edge bucket.

Reads the #71 parquet archive when present and degrades to the live legacy
sources (``model_predictions_log.jsonl``, the ``bets``/``odds_snapshots``
tables) otherwise, so it produces numbers today and auto-upgrades as the
archive fills.
"""
from wca.bench.report import build_report, render_markdown  # noqa: F401
