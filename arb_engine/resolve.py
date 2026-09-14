from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from .features import canonicalize_label, normalize_text


VOID_MARKERS = (
    "void",
    "cancel",
    "cancellation",
    "refund",
    "postpone",
    "postponed",
    "suspended",
    "did not play",
    "does not play",
    "no action",
)


def _profile_text(*values: object) -> str:
    return normalize_text(" ".join(str(value or "") for value in values))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _scope_from_period(period_scope: str) -> str:
    mapping = {
        "full_match": "full_game",
        "half_1": "1h",
        "half_2": "2h",
        "period_1": "period_1",
        "period_2": "period_2",
        "period_3": "period_3",
        "map": "map",
        "round": "round",
    }
    return mapping.get(period_scope, period_scope or "full_game")


def _extract_void_conditions(*values: object) -> list[str]:
    text = _profile_text(*values)
    conditions = [marker for marker in VOID_MARKERS if marker in text]
    return sorted(set(conditions))


def _extract_line_side(title: str, outcome_labels: list[str]) -> str | None:
    text = _profile_text(title, *outcome_labels)
    if " over " in f" {text} ":
        return "over"
    if " under " in f" {text} ":
        return "under"
    if " above " in f" {text} ":
        return "above"
    if " below " in f" {text} ":
        return "below"
    if " up " in f" {text} ":
        return "up"
    if " down " in f" {text} ":
        return "down"
    return None


def _extract_line_value(title: str, description: str) -> float | None:
    match = re.search(r"([+-]?\d+(?:\.\d+)?)", f"{title} {description}")
    return float(match.group(1)) if match else None


def _participant_role(title: str, description: str, participants: list[str] | tuple[str, ...]) -> str | None:
    text = _profile_text(title, description)
    canonical_participants = [canonicalize_label(value) for value in participants if value]
    matched = [participant for participant in canonical_participants if participant and participant in text]
    if len(matched) == 1:
        return matched[0]
    if "team total" in text:
        return "team_total"
    if "game total" in text or "match total" in text:
        return "game_total"
    return None


def build_generic_resolution_profile(
    *,
    title: str,
    description: str,
    category_family: str,
    market_type: str,
    outcome_labels: list[str],
    raw: dict[str, Any],
    closes_at: datetime | None,
    resolves_at: datetime | None,
    resolution_source_url: str | None,
    features: dict[str, Any],
) -> dict[str, Any]:
    text = _profile_text(title, description, raw.get("rules"), raw.get("subtitle"), raw.get("marketType"))
    line_side = _extract_line_side(title, outcome_labels)
    line_value = _extract_line_value(title, description) if line_side else None
    participants = list(features.get("participants", []))

    if any(token in text for token in ("hourly", "1 hour", "1h", "60 minute")):
        resolve_scope = "hourly"
    elif any(token in text for token in ("daily", "day close", "end of day")):
        resolve_scope = "daily"
    elif any(token in text for token in ("weekly", "week close")):
        resolve_scope = "weekly"
    elif closes_at or resolves_at:
        resolve_scope = "expiry"
    else:
        resolve_scope = "event"

    if any(token in text for token in ("price", "close", "cutoff", "above", "below", "up or down", "up/down")):
        resolve_basis = "price_cutoff"
    elif category_family == "crypto" and any(token in text for token in ("reach", "hit", "touch", "trade at", "trades at")):
        resolve_basis = "price_cutoff"
    elif category_family in {"politics", "news", "entertainment"} and market_type == "binary":
        resolve_basis = "headline_outcome"
    elif category_family == "sports":
        resolve_basis = "event_result"
    elif market_type == "multi_outcome":
        resolve_basis = "outright_finish"
    else:
        resolve_basis = "event_result"

    market_family = market_type
    if line_side in {"over", "under"}:
        market_family = "total"
    elif line_side in {"above", "below", "up", "down"}:
        market_family = "threshold"

    rules_source = str(raw.get("rules") or raw.get("rules_primary") or raw.get("subtitle") or "")[:500]
    settlement_source = resolution_source_url or str(raw.get("resolutionSource") or raw.get("oracleUrl") or "")
    observation_window = resolve_scope if resolve_scope not in {"event", "expiry"} else None
    return {
        "resolve_basis": resolve_basis,
        "resolve_scope": resolve_scope,
        "resolve_at": _iso(resolves_at or closes_at),
        "resolve_window_end": _iso(closes_at or resolves_at),
        "observation_window": observation_window,
        "settlement_source": settlement_source or None,
        "rules_source": rules_source or None,
        "void_conditions": _extract_void_conditions(title, description, rules_source),
        "market_family": market_family,
        "line_value": line_value,
        "line_side": line_side,
        "participant_role": _participant_role(title, description, participants),
    }


def build_sports_resolution_profile(
    *,
    title: str,
    description: str,
    raw: dict[str, Any],
    event_class: str,
    market_family: str,
    period_scope: str,
    line_value: float | None,
    line_side: str | None,
    participants: list[str] | tuple[str, ...],
    start_time: datetime | None,
) -> dict[str, Any]:
    text = _profile_text(title, description, raw.get("rules"), raw.get("rules_primary"), raw.get("subtitle"))
    if market_family == "prop":
        resolve_basis = "stat_result"
    elif period_scope != "full_match":
        resolve_basis = "period_result"
    elif event_class == "future" or market_family == "outright":
        resolve_basis = "outright_finish"
    else:
        resolve_basis = "event_result"
    resolve_scope = "tournament" if event_class == "future" else _scope_from_period(period_scope)
    return {
        "resolve_basis": resolve_basis,
        "resolve_scope": resolve_scope,
        "resolve_at": _iso(start_time),
        "resolve_window_end": _iso(start_time),
        "observation_window": resolve_scope,
        "settlement_source": str(raw.get("event_ticker") or raw.get("series") or raw.get("league") or "") or None,
        "rules_source": str(raw.get("rules_primary") or raw.get("rules") or raw.get("subtitle") or "")[:500] or None,
        "void_conditions": _extract_void_conditions(text),
        "market_family": market_family,
        "line_value": line_value,
        "line_side": line_side,
        "participant_role": _participant_role(title, description, participants),
    }


def resolution_profile_signature(profile: dict[str, Any]) -> str:
    void_conditions = ",".join(sorted(str(value) for value in profile.get("void_conditions", []) if value))
    line_value = profile.get("line_value")
    line_component = f"{float(line_value):g}" if isinstance(line_value, (int, float)) else "na"
    return "|".join(
        [
            str(profile.get("resolve_basis") or "unknown"),
            str(profile.get("resolve_scope") or "unknown"),
            str(profile.get("market_family") or "unknown"),
            line_component,
            str(profile.get("line_side") or "na"),
            str(profile.get("participant_role") or "na"),
            void_conditions or "na",
        ]
    )


def resolve_profile_compatibility(left: dict[str, Any], right: dict[str, Any]) -> tuple[str, list[str], float]:
    reasons: list[str] = []
    if str(left.get("resolve_basis") or "unknown") != str(right.get("resolve_basis") or "unknown"):
        reasons.append("resolve_basis_mismatch")
    if str(left.get("resolve_scope") or "unknown") != str(right.get("resolve_scope") or "unknown"):
        reasons.append("resolve_scope_mismatch")
    if str(left.get("market_family") or "unknown") != str(right.get("market_family") or "unknown"):
        reasons.append("market_family_mismatch")

    left_line = left.get("line_value")
    right_line = right.get("line_value")
    if left_line is not None or right_line is not None:
        if left_line is None or right_line is None or abs(float(left_line) - float(right_line)) > 1e-9:
            reasons.append("line_value_mismatch")

    left_side = str(left.get("line_side") or "")
    right_side = str(right.get("line_side") or "")
    if left_side or right_side:
        if left_side != right_side:
            reasons.append("line_side_mismatch")

    left_role = str(left.get("participant_role") or "")
    right_role = str(right.get("participant_role") or "")
    if left_role or right_role:
        if left_role != right_role:
            reasons.append("participant_role_mismatch")

    left_void = sorted(str(value) for value in left.get("void_conditions", []) if value)
    right_void = sorted(str(value) for value in right.get("void_conditions", []) if value)
    if left_void and right_void and left_void != right_void:
        reasons.append("void_conditions_mismatch")

    if not reasons:
        return "compatible", [], 1.0

    allowed = {"void_conditions_mismatch"}
    if set(reasons).issubset(allowed):
        return "review", reasons, 0.6
    return "incompatible", reasons, 0.0
