"""Extract structured bets from a betslip screenshot via the Anthropic vision API.

The manager photographs (or screenshots) a placed betslip and the bot turns
the pixels into ledger-ready rows. We send the image to the Anthropic Messages
API with a strict prompt that asks for one JSON object describing every
selection on the slip, then defensively parse the model's reply.

Everything here is built on :mod:`requests` only (the one HTTP dependency the
project already carries) — no Anthropic SDK, no image libraries. The image is
base64-encoded with :mod:`base64` from the standard library and posted as an
``image`` content block.

The extractor never writes to the ledger; it only returns
:class:`ExtractedBet` objects. The caller (the bot loop) is responsible for
showing them to the human and, on confirmation, calling
``wca.ledger.store.record_bet``.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import requests

# A single shared session so repeated extractions reuse the HTTPS connection.
# Tests inject their own session, so this is only the production default.
_SESSION = requests.Session()

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024

_VALID_STATUSES = ("open", "won", "lost", "void")

# The instruction we hand to the model. It must return ONLY the JSON object so
# our parser has the smallest possible surface to clean up.
PROMPT = (
    "You are reading a sports betting betslip from an image. Transcribe EVERY "
    "bet and EVERY selection visible on the slip — singles, and each leg of any "
    "accumulator/parlay as its own entry.\n\n"
    "Return ONLY a single JSON object, no prose, no markdown fences, of the form:\n"
    '{"bets": [ {bet}, {bet}, ... ] }\n\n'
    "Each {bet} object MUST have exactly these keys:\n"
    '  "bookmaker": the sportsbook name. Infer it from any visible logo, '
    "branding, wordmark, or distinctive brand colour (e.g. green=bet365, "
    "blue=Sky Bet/William Hill, etc.). null if you truly cannot tell.\n"
    '  "match": the event/fixture description, e.g. "England vs France". For '
    "OUTRIGHT/futures bets (Golden Boot, Tournament Winner, Top Scorer) use "
    'the competition + market, e.g. "FIFA World Cup 2026 Golden Boot". null if absent.\n'
    '  "market": the market, e.g. "Match Result", "Over/Under 2.5", "Anytime Goalscorer".\n'
    '  "selection": the picked outcome, e.g. "England", "Over 2.5", "Harry Kane".\n'
    '  "odds_decimal": the price as a DECIMAL number. Convert fractional odds '
    '(e.g. "31/20" -> 2.55, "2/9" -> 1.2222) and "EVS"/"Evens" -> 2.0. On '
    "prediction-market screenshots (Polymarket/Kalshi) prices are cents per "
    'share (e.g. "69c", "$0.69", "avg 69c"): convert to decimal odds as '
    "1/price, so 69c -> 1.449. null if not visible.\n"
    '  "stake": the amount staked/traded as a number (no currency symbol). '
    "null if not visible.\n"
    '  "currency": ISO code of the money on the slip, inferred from symbols '
    'and platform: "£" -> "GBP", "$" -> "USD", "€" -> "EUR". '
    'Polymarket and Kalshi are ALWAYS "USD". null if unclear.\n'
    '  "returns": the potential/total returns as a number. null if not visible.\n'
    '  "status": one of "open", "won", "lost", "void" based on any settlement '
    'marker on the slip; default "open" if unsettled.\n'
    '  "is_boost": true if the slip shows a price boost / enhanced odds / '
    "boost flame icon for this selection, else false.\n"
    '  "is_free_bet": true if this is a FREE BET / bonus / token stake — look '
    "for a purple/gift icon (Virgin, Paddy Power), a 'Free Bet'/'Bonus'/'Token' "
    "label, or a returns figure that EXCLUDES the stake (e.g. £1 stake at 10.0 "
    "returning £9, not £10). A free bet is stake-not-returned: the stake is not "
    "the bettor's own money and is not part of the winnings. Else false.\n"
    '  "confidence": your confidence in this row from 0 to 1.\n\n'
    "Use null for anything not legible. Output the JSON object and nothing else."
)

# The instruction for reading a PRICE-BOOST / enhanced-odds promo screenshot
# (a different surface from a placed betslip): we want the single boosted
# selection, its enhanced decimal price and — when shown — the "was" price.
BOOST_PROMPT = (
    "You are reading a sportsbook PRICE BOOST / enhanced-odds promo from an "
    "image (a boosted single selection, e.g. a 'Price Boost', 'Enhanced "
    "Odds', 'Boost' or flame-icon offer). Read the ONE boosted selection.\n\n"
    "Return ONLY a single JSON object, no prose, no markdown fences, with "
    "exactly these keys:\n"
    '  "bookmaker": the sportsbook name. Infer it from any visible logo, '
    "branding, wordmark, or distinctive brand colour (green=bet365, "
    "blue=Sky Bet/William Hill, etc.). null if you truly cannot tell.\n"
    '  "match": the fixture, e.g. "Brazil vs Morocco". null if absent.\n'
    '  "market": the market the boost is on, e.g. "Match Result", '
    '"Over 2.5 Goals", "Both Teams To Score", "Correct Score", '
    '"Anytime Goalscorer".\n'
    '  "selection": the picked outcome, e.g. "Brazil", "Draw", "Over", '
    '"Yes", "2-1", "Harry Kane".\n'
    '  "boosted_odds": the ENHANCED/boosted price as a DECIMAL number. This is '
    "the bigger, highlighted price the boost upgraded TO. Convert fractional "
    'odds ("31/20" -> 2.55, "2/9" -> 1.2222) and "EVS"/"Evens" -> 2.0. '
    "null if not visible.\n"
    '  "was_odds": the ORIGINAL pre-boost price (often shown struck-through or '
    'labelled "was"), as a DECIMAL number with the same conversions. null if '
    "no original price is shown.\n"
    '  "is_inplay": true ONLY if the screenshot clearly shows a live / in-play '
    "game (a live clock, current score, or an explicit In-Play / Live label); "
    "false otherwise.\n\n"
    "Use null for anything not legible. Output the JSON object and nothing else."
)


class VisionError(RuntimeError):
    """Raised when the vision extraction cannot complete (config/HTTP/parse)."""


@dataclass
class ExtractedBet:
    """One selection parsed off a betslip image.

    Mirrors the fields the ledger cares about, plus provenance (``confidence``,
    ``raw_text``) so the human can sanity-check before the bet is recorded.
    """

    match_desc: str
    market: str
    selection: str
    bookmaker: Optional[str] = None
    decimal_odds: Optional[float] = None
    stake: Optional[float] = None
    potential_returns: Optional[float] = None
    status: str = "open"
    is_boost: bool = False
    is_free_bet: bool = False
    confidence: float = 0.0
    raw_text: str = ""
    currency: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Odds coercion.
# ---------------------------------------------------------------------------


def fractional_to_decimal(s: str) -> float:
    """Coerce a UK/fractional/plain odds string to decimal odds.

    Handles ``"31/20"`` -> 2.55, ``"2/9"`` -> ~1.2222, ``"EVS"``/``"evens"``
    -> 2.0, and plain decimal strings like ``"2.55"`` -> 2.55. Raises
    ``ValueError`` for anything unparseable.
    """
    if s is None:
        raise ValueError("cannot parse odds from None")
    text = str(s).strip().lower()
    if not text:
        raise ValueError("cannot parse odds from empty string")

    if text in ("evs", "evens", "even", "1/1"):
        return 2.0

    if "/" in text:
        num_s, _, den_s = text.partition("/")
        try:
            num = float(num_s.strip())
            den = float(den_s.strip())
        except ValueError as exc:
            raise ValueError("bad fractional odds: %r" % s) from exc
        if den == 0:
            raise ValueError("zero denominator in fractional odds: %r" % s)
        return num / den + 1.0

    # Plain decimal (tolerate a leading currency-ish character just in case).
    cleaned = text.lstrip("@").strip()
    try:
        return float(cleaned)
    except ValueError as exc:
        raise ValueError("unparseable odds: %r" % s) from exc


def _coerce_odds(value: Any) -> Optional[float]:
    """Best-effort odds coercion that never raises; returns None on failure."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return fractional_to_decimal(str(value))
    except ValueError:
        return None


def _coerce_float(value: Any) -> Optional[float]:
    """Coerce a numeric-ish value (possibly a money string) to float or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    # Strip currency symbols, thousands separators and whitespace.
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _coerce_status(value: Any) -> str:
    if value is None:
        return "open"
    text = str(value).strip().lower()
    return text if text in _VALID_STATUSES else "open"


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("true", "1", "yes", "y")


def _coerce_confidence(value: Any) -> float:
    f = _coerce_float(value)
    if f is None:
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _str_or_empty(value: Any) -> str:
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# Response parsing.
# ---------------------------------------------------------------------------


def _strip_code_fences(text: str) -> str:
    """Remove a wrapping ```json ... ``` (or plain ```) fence if present."""
    t = text.strip()
    if t.startswith("```"):
        # Drop the opening fence line (``` or ```json) ...
        first_newline = t.find("\n")
        if first_newline != -1:
            t = t[first_newline + 1:]
        # ... and the trailing fence.
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _extract_json_object(text: str) -> Dict[str, Any]:
    """Pull the first balanced ``{...}`` JSON object out of ``text``.

    Tolerates leading prose and ```json fences. Raises ``VisionError`` if no
    valid JSON object can be recovered.
    """
    cleaned = _strip_code_fences(text)

    # Fast path: the whole thing is JSON.
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass

    # Slow path: scan for the first balanced object, respecting strings.
    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start:i + 1]
                    try:
                        obj = json.loads(candidate)
                    except ValueError:
                        break  # malformed; try next "{"
                    if isinstance(obj, dict):
                        return obj
                    break
        start = cleaned.find("{", start + 1)

    raise VisionError("no JSON object found in model reply: %r" % text[:200])


def _bet_from_obj(obj: Dict[str, Any], raw_text: str) -> ExtractedBet:
    return ExtractedBet(
        match_desc=_str_or_empty(obj.get("match")),
        market=_str_or_empty(obj.get("market")),
        selection=_str_or_empty(obj.get("selection")),
        bookmaker=(None if obj.get("bookmaker") is None else str(obj["bookmaker"])),
        decimal_odds=_coerce_odds(obj.get("odds_decimal")),
        stake=_coerce_float(obj.get("stake")),
        potential_returns=_coerce_float(obj.get("returns")),
        status=_coerce_status(obj.get("status")),
        is_boost=_coerce_bool(obj.get("is_boost")),
        is_free_bet=_coerce_bool(obj.get("is_free_bet")),
        confidence=_coerce_confidence(obj.get("confidence")),
        raw_text=raw_text,
        currency=_coerce_currency(obj.get("currency"), obj.get("bookmaker")),
    )


_CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}


def currency_symbol(code: Optional[str]) -> str:
    """Display symbol for an ISO currency code; falls back to the code or £."""
    if not code:
        return "£"
    return _CURRENCY_SYMBOLS.get(code.upper(), code.upper() + " ")


def _coerce_currency(value: Any, bookmaker: Any) -> Optional[str]:
    """Normalize a currency hint; prediction-market platforms are always USD."""
    book = str(bookmaker or "").lower()
    if "polymarket" in book or "kalshi" in book:
        return "USD"
    if value is None:
        return None
    s = str(value).strip().upper()
    if s in {"£", "GBP"}:
        return "GBP"
    if s in {"$", "USD", "USDC"}:
        return "USD"
    if s in {"€", "EUR"}:
        return "EUR"
    return s[:3] if s else None


def _detect_accas(bets: List[ExtractedBet]) -> List[ExtractedBet]:
    """Group individual legs into accas when detected.

    Detects accas by finding groups of bets with:
    - Same bookmaker
    - Same stake
    - Combined returns ≈ stake × product of odds
    - 2+ legs
    """
    if len(bets) < 2:
        return bets

    # Group by (bookmaker, stake, currency)
    from collections import defaultdict
    groups = defaultdict(list)
    used = set()

    for i, b in enumerate(bets):
        if i in used:
            continue
        # Try to form an acca starting from this bet
        candidates = [i]
        for j in range(i + 1, len(bets)):
            if j in used:
                continue
            if (bets[j].bookmaker == b.bookmaker and
                abs((bets[j].stake or 0) - (b.stake or 0)) < 0.01 and
                bets[j].currency == b.currency):
                candidates.append(j)

        # Check if this is an acca (2+ legs with matching returns to combined odds)
        if len(candidates) >= 2:
            legs = [bets[idx] for idx in candidates]
            # Calculate combined odds
            combined_odds = 1.0
            for leg in legs:
                if leg.decimal_odds and leg.decimal_odds > 0:
                    combined_odds *= leg.decimal_odds

            expected_returns = (b.stake or 0) * combined_odds if b.stake else None
            # Find actual returns from any leg that has it
            actual_returns = next(
                (leg.potential_returns for leg in legs if leg.potential_returns),
                None
            ) or 0

            # If returns match (within 5% tolerance), it's an acca
            if (expected_returns and actual_returns > 0 and
                abs(expected_returns - actual_returns) / actual_returns < 0.05):
                # Merge into one acca bet
                selection = " + ".join(
                    f"{leg.selection} ({leg.market})"
                    for leg in legs
                )
                acca_bet = ExtractedBet(
                    match_desc=" | ".join(set(leg.match_desc for leg in legs if leg.match_desc)),
                    market="Accumulator",
                    selection=selection,
                    bookmaker=b.bookmaker,
                    decimal_odds=combined_odds,
                    stake=b.stake,
                    potential_returns=actual_returns,
                    status=b.status,
                    is_boost=False,
                    confidence=min(leg.confidence for leg in legs),
                    raw_text=b.raw_text,
                    currency=b.currency,
                )
                groups["acca"].append(acca_bet)
                for idx in candidates:
                    used.add(idx)
                continue

        # Not an acca, keep as single
        groups["single"].append(b)
        used.add(i)

    return groups["acca"] + groups["single"]


def _parse_message_body(body: Dict[str, Any]) -> List[ExtractedBet]:
    content = body.get("content")
    if not content or not isinstance(content, list):
        raise VisionError(
            "Anthropic response had no content (API error?): %s"
            % json.dumps(body)[:300]
        )
    # The text we want is the first text block.
    text = ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            break
    else:
        # No text block at all — fall back to first block's text field.
        first = content[0]
        text = first.get("text", "") if isinstance(first, dict) else ""

    if not text:
        raise VisionError("Anthropic response text block was empty")

    obj = _extract_json_object(text)
    bets_raw = obj.get("bets", [])
    if not isinstance(bets_raw, list):
        return []
    bets = [
        _bet_from_obj(b, raw_text=text)
        for b in bets_raw
        if isinstance(b, dict)
    ]
    # Post-process to detect and merge accas
    return _detect_accas(bets)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def sniff_media_type(image_bytes: bytes) -> Optional[str]:
    """Detect the image MIME type from magic bytes.

    Telegram (and screenshots in general) often carry a misleading file
    extension — e.g. a PNG stored as ``.jpg`` — and the Anthropic API rejects
    a mismatched ``media_type``, so the declared type must come from the
    bytes themselves.  Returns ``None`` if the format is unrecognised.
    """
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF8"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return None


def extract_bets_from_image(
    image_bytes: bytes,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    media_type: str = "image/jpeg",
    timeout: float = 60.0,
    session: Optional[Any] = None,
) -> List[ExtractedBet]:
    """Extract structured bets from a betslip screenshot via Anthropic vision.

    Parameters
    ----------
    image_bytes:
        Raw bytes of the betslip image (PNG/JPEG/etc).
    api_key:
        Anthropic API key. Defaults to ``ANTHROPIC_API_KEY`` in the
        environment; raises :class:`VisionError` if neither is provided.
    model:
        Vision model id. Defaults to ``ANTHROPIC_VISION_MODEL`` env var, then
        the literal ``"claude-sonnet-4-6"``.
    media_type:
        MIME type of the image, e.g. ``"image/jpeg"`` or ``"image/png"``.
    timeout:
        Per-request HTTP timeout in seconds.
    session:
        Optional ``requests``-style session (injected for testing). Falls back
        to the module-level shared session.

    Returns
    -------
    list of :class:`ExtractedBet`
        One per selection on the slip; ``[]`` if the model finds no bets.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise VisionError(
            "no Anthropic API key: pass api_key= or set ANTHROPIC_API_KEY"
        )

    mdl = model or os.environ.get("ANTHROPIC_VISION_MODEL") or DEFAULT_MODEL
    sess = session or _SESSION

    # Trust the bytes over the caller's declared type: Telegram documents
    # frequently arrive as PNGs with .jpg names, and the API 400s on mismatch.
    media_type = sniff_media_type(image_bytes) or media_type

    b64 = base64.b64encode(image_bytes).decode("ascii")

    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": mdl,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
    }

    try:
        resp = sess.post(API_URL, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise VisionError("Anthropic request failed: %s" % exc) from exc

    status = getattr(resp, "status_code", 200)
    try:
        body = resp.json()
    except ValueError as exc:
        snippet = getattr(resp, "text", "")[:200]
        raise VisionError("Anthropic returned non-JSON: %s" % snippet) from exc

    if status >= 400:
        # Anthropic error bodies look like {"type":"error","error":{...}}.
        err = body.get("error") if isinstance(body, dict) else None
        msg = err.get("message") if isinstance(err, dict) else body
        raise VisionError("Anthropic API error (HTTP %s): %s" % (status, msg))

    if not isinstance(body, dict):
        raise VisionError("Anthropic response was not a JSON object")

    return _parse_message_body(body)


# ---------------------------------------------------------------------------
# Price-boost extraction.
#
# A boost screenshot is a different surface from a placed betslip: one boosted
# selection at an enhanced price, which we want to *price against the model*
# (see :mod:`wca.boosts`) rather than log to the ledger. The transport,
# auth, base64 image block and defensive JSON parsing below mirror
# :func:`extract_bets_from_image` exactly — same shared session, same error
# handling, same "JSON object only" discipline — just with the boost prompt
# and a single-object reply.
# ---------------------------------------------------------------------------


def _post_image_for_text(
    image_bytes: bytes,
    prompt: str,
    *,
    api_key: Optional[str],
    model: Optional[str],
    media_type: str,
    timeout: float,
    session: Optional[Any],
) -> Dict[str, Any]:
    """POST an image + prompt to Anthropic and return the first JSON object.

    Shared transport for the vision extractors: resolves the key/model, sniffs
    the media type from the bytes, base64-encodes the image, posts it with the
    ``prompt`` text block, and runs the same defensive parsing the betslip path
    uses. Raises :class:`VisionError` on any config / HTTP / parse failure.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise VisionError(
            "no Anthropic API key: pass api_key= or set ANTHROPIC_API_KEY"
        )

    mdl = model or os.environ.get("ANTHROPIC_VISION_MODEL") or DEFAULT_MODEL
    sess = session or _SESSION

    # Trust the bytes over the caller's declared type (Telegram mislabels PNGs).
    media_type = sniff_media_type(image_bytes) or media_type

    b64 = base64.b64encode(image_bytes).decode("ascii")

    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": mdl,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }

    try:
        resp = sess.post(API_URL, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise VisionError("Anthropic request failed: %s" % exc) from exc

    status = getattr(resp, "status_code", 200)
    try:
        body = resp.json()
    except ValueError as exc:
        snippet = getattr(resp, "text", "")[:200]
        raise VisionError("Anthropic returned non-JSON: %s" % snippet) from exc

    if status >= 400:
        err = body.get("error") if isinstance(body, dict) else None
        msg = err.get("message") if isinstance(err, dict) else body
        raise VisionError("Anthropic API error (HTTP %s): %s" % (status, msg))

    if not isinstance(body, dict):
        raise VisionError("Anthropic response was not a JSON object")

    content = body.get("content")
    if not content or not isinstance(content, list):
        raise VisionError(
            "Anthropic response had no content (API error?): %s"
            % json.dumps(body)[:300]
        )
    text = ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            break
    else:
        first = content[0]
        text = first.get("text", "") if isinstance(first, dict) else ""
    if not text:
        raise VisionError("Anthropic response text block was empty")

    return _extract_json_object(text)


def boost_from_obj(obj: Dict[str, Any]) -> "Any":
    """Build a :class:`wca.boosts.Boost` from a parsed boost JSON object.

    Coerces odds (decimal/fractional/EVS) via :func:`_coerce_odds`, the in-play
    flag via :func:`_coerce_bool`, and leaves text fields as best-effort
    strings. ``boosted_odds`` defaults to ``0.0`` when illegible so a downstream
    pricing call still runs (and simply yields a non-positive edge) rather than
    crashing on ``None``. Imported lazily so :mod:`wca.bot.vision` has no hard
    dependency on :mod:`wca.boosts` at import time.
    """
    from wca.boosts import Boost

    boosted = _coerce_odds(obj.get("boosted_odds"))
    return Boost(
        site=_str_or_empty(obj.get("bookmaker")),
        fixture=_str_or_empty(obj.get("match")),
        market=_str_or_empty(obj.get("market")),
        selection=_str_or_empty(obj.get("selection")),
        boosted_odds=float(boosted) if boosted is not None else 0.0,
        was_odds=_coerce_odds(obj.get("was_odds")),
        is_inplay=_coerce_bool(obj.get("is_inplay")),
    )


def extract_boost(
    image_bytes: bytes,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    media_type: str = "image/jpeg",
    timeout: float = 60.0,
    session: Optional[Any] = None,
) -> "Any":
    """Extract one price-boost selection from a promo screenshot.

    Sends the image to Anthropic vision with :data:`BOOST_PROMPT` and returns a
    :class:`wca.boosts.Boost` describing the single boosted selection (its
    enhanced decimal price, optional ``was`` price, and an in-play flag). The
    caller prices it via :func:`wca.boosts.evaluate_boost` — this function never
    touches the ledger.

    Parameters mirror :func:`extract_bets_from_image` (``api_key``, ``model``,
    ``media_type``, ``timeout``, ``session``); the same shared session and
    error handling apply. Raises :class:`VisionError` on config / HTTP / parse
    failure.
    """
    obj = _post_image_for_text(
        image_bytes,
        BOOST_PROMPT,
        api_key=api_key,
        model=model,
        media_type=media_type,
        timeout=timeout,
        session=session,
    )
    return boost_from_obj(obj)
