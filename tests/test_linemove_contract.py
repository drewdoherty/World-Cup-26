"""Cross-language contract tests: ``wca.linemove`` producer -> ``site/app.js``.

The producer writes ``site/linemove.json`` as ``{"events": [{"fixture",
"kickoff", "series": {"home": [[ts, prob], ...], "draw": [...], "away":
[...]}}]}`` and the front-end (``normLineMove`` / ``pointsFromSeries`` in
``site/app.js``) zips that series object into its internal point list.  A past
regression had ``app.js`` calling ``.map`` directly on the series *object*,
which threw and silently hid the chart; nothing on the Python side could catch
that.  These tests pin the contract from both ends:

* a pure-Python check that real :func:`build_linemove` output (after a JSON
  round-trip, i.e. exactly what lands in ``linemove.json``) has the shape the
  JS documents and reads;
* a static check that ``app.js`` still reads those exact keys; and
* an execution check that runs the real ``normLineMove`` + ``pointsFromSeries``
  source (extracted from ``app.js``) under macOS JXA (``osascript -l
  JavaScript``) against real producer output, skipped where JXA is unavailable.
"""

from __future__ import annotations

import functools
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import linemove  # noqa: E402

APP_JS = os.path.join(_ROOT, "site", "app.js")

MATCH_ID = "evt-mex-rsa"
HOME = "Mexico"
AWAY = "South Africa"
KICKOFF = "2026-06-11T19:00:00Z"

EVENT_META = {
    MATCH_ID: {
        "fixture": "Mexico vs South Africa",
        "home": HOME,
        "away": AWAY,
        "kickoff": KICKOFF,
    }
}

TS = ["2026-06-10T18:00:00Z", "2026-06-10T19:00:00Z", "2026-06-10T20:00:00Z"]

# One bookmaker per timestamp, with prices whose implied probs (1/odds) already
# sum to 1 — so the consensus median/normalisation passes values through and
# the expected probabilities are exact dyadic floats that survive the JSON and
# JS round-trips bit-for-bit.
PRICES = {
    TS[0]: {"home": 2.0, "draw": 4.0, "away": 4.0},
    TS[1]: {"home": 1.6, "draw": 4.0, "away": 8.0},
    TS[2]: {"home": 4.0, "draw": 4.0, "away": 2.0},
}
EXPECTED = {
    ts: {leg: 1.0 / price for leg, price in legs.items()}
    for ts, legs in PRICES.items()
}


@pytest.fixture()
def payload(tmp_path):
    """Real producer output for the seeded ledger, JSON round-tripped so the
    tests see exactly what ``linemove.json`` (and therefore the JS) sees."""
    path = str(tmp_path / "wca.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE odds_snapshots (
                ts_utc       TEXT    NOT NULL,
                source       TEXT    NOT NULL,
                match_id     TEXT    NOT NULL,
                market       TEXT    NOT NULL,
                selection    TEXT    NOT NULL,
                decimal_odds REAL,
                raw          TEXT
            )
            """
        )
        sel_for_leg = {"home": HOME, "draw": "Draw", "away": AWAY}
        for ts, legs in PRICES.items():
            for leg, price in legs.items():
                conn.execute(
                    "INSERT INTO odds_snapshots "
                    "(ts_utc, source, match_id, market, selection, "
                    " decimal_odds, raw) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ts, "theoddsapi", MATCH_ID, "h2h", sel_for_leg[leg],
                     price, "{}"),
                )
        conn.commit()
    finally:
        conn.close()
    out = linemove.build_linemove(path, EVENT_META, now_utc="GEN")
    return json.loads(json.dumps(out))


def _epoch_ms(iso_z):
    """Epoch milliseconds for a ``...T...Z`` timestamp — what the JS ``tsMs``
    (``Date.parse``) returns for the same string."""
    dt = datetime.strptime(iso_z, "%Y-%m-%dT%H:%M:%SZ")
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Producer side: the payload has the shape app.js documents and reads.
# ---------------------------------------------------------------------------


def test_payload_shape_matches_consumer_contract(payload):
    events = payload["events"]
    assert isinstance(events, list) and events  # normLineMove reads raw.events

    for ev in events:
        # normLineMove: label from ev.fixture, kickoff via tsMs(ev.kickoff).
        assert isinstance(ev["fixture"], str) and ev["fixture"]
        assert isinstance(ev["kickoff"], str)
        assert "T" in ev["kickoff"]
        datetime.strptime(ev["kickoff"], "%Y-%m-%dT%H:%M:%SZ")

        # pointsFromSeries: a non-array object holding three parallel legs.
        series = ev["series"]
        assert isinstance(series, dict)
        assert set(series) == {"home", "draw", "away"}
        for leg in ("home", "draw", "away"):
            pairs = series[leg]
            assert isinstance(pairs, list) and len(pairs) >= 2
            for pair in pairs:
                assert isinstance(pair, list) and len(pair) == 2
                ts, prob = pair
                assert isinstance(ts, str) and "T" in ts
                datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
                assert isinstance(prob, float) and math.isfinite(prob)
                assert 0.0 <= prob <= 1.0
        # Parallel arrays: every leg covers the same timestamps in order.
        ts_seq = [p[0] for p in series["home"]]
        assert [p[0] for p in series["draw"]] == ts_seq
        assert [p[0] for p in series["away"]] == ts_seq


# ---------------------------------------------------------------------------
# Consumer side: app.js still reads exactly those keys.
# ---------------------------------------------------------------------------


def _app_js_source():
    with open(APP_JS, "r", encoding="utf-8") as fh:
        return fh.read()


def _extract_js_function(source, name):
    """Return the full source of ``function <name>(...) {...}`` from app.js.

    Brace counting is naive (it would miscount a brace inside a string or
    regex literal) — the targeted helpers contain none, and a failed
    extraction fails the test, which is the point of a contract guard.
    """
    marker = "function {0}(".format(name)
    assert marker in source, "app.js no longer defines {0}()".format(name)
    start = source.index(marker)
    depth = 0
    for i in range(source.index("{", start), len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError("unbalanced braces extracting {0}()".format(name))


def test_app_js_reads_documented_keys():
    src = _app_js_source()

    points_src = _extract_js_function(src, "pointsFromSeries")
    for leg in ("home", "draw", "away"):
        assert '"{0}"'.format(leg) in points_src

    norm_src = _extract_js_function(src, "normLineMove")
    assert "raw.events" in norm_src
    assert "ev.fixture" in norm_src
    assert "ev.kickoff" in norm_src
    assert "pointsFromSeries(ev.series)" in norm_src


# ---------------------------------------------------------------------------
# Execution: run the real JS against real producer output under JXA.
# ---------------------------------------------------------------------------

# normLineMove's full dependency closure inside the app.js IIFE.
_JS_HELPERS = ["tsMs", "numOrNull", "pointsFromSeries", "normLineMove"]


@functools.lru_cache(maxsize=1)
def _jxa_unavailable():
    """Reason string if ``osascript -l JavaScript`` cannot run here, else None."""
    if sys.platform != "darwin":
        return "osascript JXA requires macOS"
    if shutil.which("osascript") is None:
        return "osascript not on PATH"
    try:
        probe = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", "1"],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return "osascript probe failed to run"
    if probe.returncode != 0:
        return "osascript JXA probe exited non-zero"
    return None


def test_normlinemove_zips_producer_payload(payload, tmp_path):
    reason = _jxa_unavailable()
    if reason:
        pytest.skip(reason)

    src = _app_js_source()
    script = "\n".join(
        ['"use strict";']
        + [_extract_js_function(src, name) for name in _JS_HELPERS]
        + [
            "var payload = {0};".format(json.dumps(payload)),
            "JSON.stringify(normLineMove(payload));",
        ]
    )
    script_path = tmp_path / "contract.js"
    script_path.write_text(script, encoding="utf-8")

    proc = subprocess.run(
        ["osascript", "-l", "JavaScript", str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        "app.js normLineMove threw on real producer output:\n" + proc.stderr
    )
    events = json.loads(proc.stdout)

    assert len(events) == 1
    ev = events[0]
    assert ev["label"] == EVENT_META[MATCH_ID]["fixture"]
    assert ev["kickoff"] == _epoch_ms(KICKOFF)

    points = ev["points"]
    assert [pt["t"] for pt in points] == [_epoch_ms(ts) for ts in TS]
    for ts, pt in zip(TS, points):
        for leg in ("home", "draw", "away"):
            assert pt[leg] is not None  # the zip joined all three legs by ts
            assert pt[leg] == pytest.approx(EXPECTED[ts][leg])
