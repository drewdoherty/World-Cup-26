"""Multi-agent prediction pipeline for World Cup Alpha.

Agents communicate exclusively through the typed JSON contracts in
:mod:`wca.agents.contracts`. No agent reads another agent's prompts or
internal state directly.

Pipeline (sequential):
  0  Orchestrator    — parse command, route, aggregate
  1  DataCollector   — odds, PM, news
  2  TeamIntel       — lineup, injury, squad strength
  3  MarketIntel     — devig, steam, BM vs PM dislocation
  4  MatchModel      — Elo, Dixon-Coles, props ensemble
  5  EdgeDetector    — EV calc, ranking, threshold gate
  6  Adversarial     — LLM critic, failure-mode scan
  7  BetSizing       — fractional Kelly, exposure cap
  8  Publisher       — format + send Telegram alert
"""

from wca.agents.orchestrator import run_pipeline

__all__ = ["run_pipeline"]
