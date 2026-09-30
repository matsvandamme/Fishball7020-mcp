"""The transmit safety gate: band and power rules, and checked overrides.

Every transmit tool asks `check` before it touches the radio. Two layers:

1. Rules, in code, always on. A transmit must fall wholly inside one of the EU
   licence-free bands below, and its estimated output must stay under that
   band's limit. These are numbers, so code checks them - no network, no model.

2. Overrides. A transmit the rules flag is refused unless the call carries an
   `override_reason` - "cabled through 30 dB into the receiver", say. If
   TYPESAFE_API_KEY is set, TypeSafe's Jev model reads that sentence and the
   override only stands if it describes a setup where the flagged transmit is
   safe: a conducted path with nothing radiated, a shielded enclosure, or a
   licence that covers the frequency. If TypeSafe cannot be reached, the
   override is refused - this only ever runs on a transmit that is already
   flagged. Without a key the override is taken at its word and logged as
   unchecked.

3. Force. The gate advises; the operator decides. `force=True` transmits
   whatever the rules and TypeSafe say - no reason needed - and the reply and
   the log carry a warning naming what was overruled.

The power estimate is the README's figure: about +19 dBm at 0 dB gain and full
scale, falling 1 dB per dB of attenuation and with 20*log10(scale). It is an
estimate of conducted power; antenna gain is unknown and not included.

SDR_MCP_TX_BANDS, when set, still restricts frequencies on top of all this.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Estimated conducted output at 0 dB TX gain and full-scale amplitude.
FULL_SCALE_DBM = 19.0

# EU licence-free bands (ERC Recommendation 70-03), MHz, with the power limit
# applied here in dBm. ERP/EIRP limits are treated as conducted limits, which
# is right for a 0 dBi antenna and generous for anything with gain.
BANDS: list[tuple[str, float, float, float]] = [
    ("433 MHz SRD", 433.05, 434.79, 10.0),         # 10 mW ERP
    ("868 MHz SRD", 863.0, 870.0, 14.0),           # 25 mW ERP
    ("2.4 GHz ISM", 2400.0, 2483.5, 20.0),         # 100 mW EIRP
    ("5.8 GHz SRD", 5725.0, 5875.0, 14.0),         # 25 mW EIRP
]

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL = "jev-latest"
TYPESAFE_TIMEOUT_S = 15.0
# How sure Jev must be that the reason establishes a safe setup, and how
# unsure that it radiates. Chosen, not tuned: there is no labelled set of
# override reasons to tune them on yet.
ACCEPT_AT = 0.8
RADIATES_BELOW = 0.5


def estimate_dbm(tx_gain_db: float, scale: float) -> float:
    if scale <= 0:
        return -math.inf
    return FULL_SCALE_DBM + tx_gain_db + 20 * math.log10(scale)


@dataclass
class Flag:
    """Why the rules object to a transmit. Empty `reasons` means none."""
    low_hz: float
    high_hz: float
    dbm: float
    band: str | None = None
    limit_dbm: float | None = None
    reasons: list[str] = field(default_factory=list)


def assess(low_hz: float, high_hz: float, tx_gain_db: float, scale: float) -> Flag:
    """Apply the band and power rules to a transmit spanning low_hz..high_hz."""
    dbm = estimate_dbm(tx_gain_db, scale)
    flag = Flag(low_hz, high_hz, dbm)
    for name, lo, hi, limit in BANDS:
        if lo * 1e6 <= low_hz and high_hz <= hi * 1e6:
            flag.band, flag.limit_dbm = name, limit
            if dbm > limit:
                flag.reasons.append(
                    f"estimated output {dbm:+.1f} dBm is over the {name} limit of "
                    f"{limit:+.0f} dBm")
            return flag
    span = (f"{low_hz/1e6:.4f} MHz" if low_hz == high_hz
            else f"{low_hz/1e6:.4f}-{high_hz/1e6:.4f} MHz")
    flag.reasons.append(
        f"{span} is not wholly inside a licence-free band ("
        + ", ".join(f"{n} {lo:g}-{hi:g} MHz" for n, lo, hi, _ in BANDS) + ")")
    return flag


class TypeSafeUnavailable(RuntimeError):
    """TypeSafe could not give an answer: network, timeout, key or service."""


def _questions() -> dict:
    return {
        "conducted": {
            "type": "noul",
            "instructions": (
                "Does `override_reason` say the transmit port is connected by "
                "cable, directly or through attenuators, to a dummy load, "
                "attenuator, receiver input or measurement instrument, so that "
                "the signal is not radiated from an antenna?"),
        },
        "shielded": {
            "type": "noul",
            "instructions": (
                "Does `override_reason` say the transmission takes place inside "
                "a shielded enclosure, Faraday cage or screened room?"),
        },
        "licensed": {
            "type": "noul",
            "instructions": (
                "Does `override_reason` state that the operator holds a licence "
                "or authorisation that covers transmitting at "
                "`transmission.frequency_mhz`? A licence for a different band "
                "does not count."),
        },
        "radiates": {
            "type": "noul",
            "instructions": (
                "Does `override_reason` say or imply that the signal will be "
                "radiated over the air from an antenna?"),
        },
    }


def judge_override(reason: str, flag: Flag, what: str, key: str) -> dict:
    """Ask TypeSafe whether `reason` makes the flagged transmit safe.

    Returns {"accepted": bool, "answers": {question: probability}, "model": ...}.
    Raises TypeSafeUnavailable if no judgement came back.
    """
    state = {
        "override_reason": reason,
        "transmission": {
            "what": what,
            "frequency_mhz": round((flag.low_hz + flag.high_hz) / 2e6, 4),
            "span_mhz": [round(flag.low_hz / 1e6, 4), round(flag.high_hz / 1e6, 4)],
            "estimated_output_dbm": round(flag.dbm, 1),
            "flagged_because": flag.reasons,
        },
    }
    body = json.dumps({"state": state, "model": TYPESAFE_MODEL,
                       "questions": _questions()}).encode()
    req = urllib.request.Request(
        TYPESAFE_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TYPESAFE_TIMEOUT_S) as resp:
            reply = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="replace")
        raise TypeSafeUnavailable(f"TypeSafe answered HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TypeSafeUnavailable(f"TypeSafe could not be reached: {exc}") from exc
    try:
        p = {k: float(reply["answers"][k]["noul"]) for k in _questions()}
    except (KeyError, TypeError, ValueError) as exc:
        raise TypeSafeUnavailable(f"TypeSafe reply had no usable answers: {reply!r}") from exc
    safe_setup = (max(p["conducted"], p["shielded"]) >= ACCEPT_AT
                  and p["radiates"] < RADIATES_BELOW)
    accepted = safe_setup or p["licensed"] >= ACCEPT_AT
    return {"accepted": accepted, "answers": p, "model": reply.get("model")}


@dataclass
class Decision:
    allowed: bool
    message: str          # the refusal text, or a note for the reply
    log_line: str


def check(what: str, low_hz: float, high_hz: float, tx_gain_db: float,
          scale: float, override_reason: str | None,
          force: bool = False) -> Decision:
    """Decide whether a transmit may go ahead. Never touches the radio.

    With force, a refusal becomes a warning and the transmit goes ahead.
    """
    decision = _decide(what, low_hz, high_hz, tx_gain_db, scale, override_reason)
    if decision.allowed or not force:
        return decision
    advice = (decision.message.removeprefix(f"Refusing to {what}: ").split("\n\n")[0]
              .replace(" - or pass force=true to transmit anyway", ""))
    return Decision(
        True,
        f"WARNING - transmitting against the safety gate's advice, because "
        f"force=true: {advice} The operator is responsible for this transmission.",
        f"TX GATE FORCED by operator, overruling: {decision.log_line}")


def _decide(what: str, low_hz: float, high_hz: float, tx_gain_db: float,
            scale: float, override_reason: str | None) -> Decision:
    flag = assess(low_hz, high_hz, tx_gain_db, scale)
    if not flag.reasons:
        return Decision(True, "", f"TX GATE pass: {flag.band}, est {flag.dbm:+.1f} dBm")

    why = "; ".join(flag.reasons)
    reason = (override_reason or "").strip()
    if not reason:
        return Decision(
            False,
            f"Refusing to {what}: {why}.\n\n"
            f"If this is deliberate - into a dummy load or attenuator, inside a "
            f"shielded box, or under a licence that covers it - call again with "
            f"override_reason describing the setup. "
            + ("TypeSafe will check that the reason describes a safe setup. "
               if os.environ.get("TYPESAFE_API_KEY") else
               "The reason is logged. ")
            + "Or pass force=true to transmit anyway, with a warning.",
            f"TX GATE refused (no override): {why}")

    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        return Decision(
            True,
            f"Transmitting on an override: {why}. The reason was not checked "
            f"(no TYPESAFE_API_KEY) and is logged.",
            f"TX GATE override UNCHECKED: {why} | reason={reason!r}")

    try:
        verdict = judge_override(reason, flag, what, key)
    except TypeSafeUnavailable as exc:
        return Decision(
            False,
            f"Refusing to {what}: {why}. The override could not be checked - "
            f"{exc}. This is a service failure, not a judgement on the reason; "
            f"try again, unset TYPESAFE_API_KEY to take overrides unchecked, or "
            f"pass force=true to transmit anyway.",
            f"TX GATE refused (TypeSafe unavailable): {exc}")

    p = verdict["answers"]
    scores = ", ".join(f"{k} {v:.2f}" for k, v in p.items())
    if verdict["accepted"]:
        return Decision(
            True,
            f"Transmitting on an override: {why}. TypeSafe ({verdict['model']}) "
            f"accepted the reason ({scores}).",
            f"TX GATE override ACCEPTED by TypeSafe [{scores}]: {why} | reason={reason!r}")
    return Decision(
        False,
        f"Refusing to {what}: {why}. TypeSafe ({verdict['model']}) read the "
        f"override reason as not describing a safe setup ({scores}). It needs to "
        f"say the port is cabled to a load or attenuator with nothing radiated, "
        f"that it is inside a shielded enclosure, or that a licence covers "
        f"this frequency - or pass force=true to transmit anyway.",
        f"TX GATE override REJECTED by TypeSafe [{scores}]: {why} | reason={reason!r}")
