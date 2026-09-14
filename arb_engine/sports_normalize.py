from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
import re
from typing import Any

from .config import Settings
from .features import canonicalize_label, event_title_key, event_title_tokens, normalize_text
from .models import MarketSnapshot, OutcomeQuote
from .opn_adapter import (
    extract_opn_category,
    extract_opn_chain,
    extract_opn_closes_at,
    extract_opn_description,
    extract_opn_liquidity,
    extract_opn_outcomes,
    extract_opn_tags,
    extract_opn_token,
    extract_opn_title,
    extract_opn_volume,
)
from .resolve import build_sports_resolution_profile, resolution_profile_signature
from .sports_models import SportsMarket, SportsOutcome
from .utils import parse_datetime, safe_float, text_hash


SPORT_ALIASES = {
    "soccer": "soccer",
    "football": "soccer",
    "basketball": "basketball",
    "nba": "basketball",
    "wnba": "basketball",
    "boxing": "boxing",
    "mma": "mma",
    "ufc": "mma",
    "hockey": "hockey",
    "nhl": "hockey",
    "baseball": "baseball",
    "mlb": "baseball",
    "tennis": "tennis",
    "golf": "golf",
    "cricket": "cricket",
    "esports": "esports",
    "counter-strike": "esports",
    "cs2": "esports",
    "csgo": "esports",
    "dota": "esports",
    "dota2": "esports",
    "kpl": "esports",
    "lpl": "esports",
    "lck": "esports",
    "lec": "esports",
    "lcs": "esports",
    "league of legends": "esports",
    "lol": "esports",
    "vct": "esports",
    "valorant": "esports",
    "rocket league": "esports",
    "overwatch": "esports",
}

FUTURE_MARKERS = ("championship", "winner", "to win", "tournament", "league winner", "season", "cup")
TOTAL_MARKERS = ("over", "under", "total")
SPREAD_MARKERS = ("spread", "handicap", "asian handicap")
MAP_MARKERS = ("map ", "map winner", "bo3", "bo5", "best of")
ROUND_MARKERS = ("round ", "1st half", "2nd half", "1st period", "2nd period", "3rd period")
PROP_MARKERS = ("kills", "corners", "shots", "rebounds", "points", "assists", "yellow cards")
ESPORT_MARKERS = ("esports", "counter-strike", "cs2", "csgo", "dota", "valorant", "league of legends", "rocket league")
NO_DRAW_SPORTS = {"boxing", "mma", "tennis", "basketball", "baseball", "golf", "esports"}
GENERIC_COMPETITION_KEYS = {"unknown", "other", "sports"}
EVENT_STAGE_PATTERNS = (
    r"round of \d+",
    r"quarter[\s-]?finals?",
    r"semi[\s-]?finals?",
    r"grand final",
    r"finals?",
    r"group [a-z0-9]+",
    r"matchday \d+",
    r"week \d+",
)
MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
PARTICIPANT_DROP_TOKENS = {"fc", "cf", "sc", "esports", "team", "club", "the"}
PARTICIPANT_TOKEN_ALIASES = {
    "st": "state",
    "st.": "state",
    "utd": "united",
    "ath": "athletic",
    "athl": "athletic",
    "ft": "fort",
    "mt": "mount",
    "intl": "international",
}
COMPETITION_PHRASE_ALIASES = {
    "professional basketball": "nba",
    "national basketball association": "nba",
    "college basketball": "ncaa basketball",
    "ncaa hoops": "ncaa basketball",
    "premier league": "epl",
    "english premier league": "epl",
    "champions league": "uefa champions league",
    "euro league": "euroleague",
    "counter strike": "cs2",
    "counter strike 2": "cs2",
}
COMPETITION_TOKEN_ALIASES = {
    "epl": "epl",
    "nba": "nba",
    "wnba": "wnba",
    "nhl": "nhl",
    "mlb": "mlb",
    "ufc": "ufc",
    "ncaa": "ncaa",
    "ncaab": "ncaa basketball",
    "ncaaf": "ncaa football",
    "uefa": "uefa",
    "ucl": "uefa champions league",
    "euroleague": "euroleague",
    "kpl": "kpl",
    "lpl": "lpl",
    "lck": "lck",
    "lec": "lec",
    "lcs": "lcs",
    "vct": "vct",
    "csgo": "cs2",
    "cs2": "cs2",
}
COMPETITION_DROP_TOKENS = {
    "league",
    "cup",
    "conference",
    "division",
    "group",
    "stage",
    "winner",
    "winners",
    "match",
    "game",
    "games",
    "season",
    "tournament",
    "playoffs",
    "playoff",
    "professional",
}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            import json

            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return [value]
            return parsed if isinstance(parsed, list) else [value]
        return [value]
    return [value]


def _cost_from_order_book(order_book: dict[str, Any], payout_size: float) -> tuple[float | None, float | None, float | None, float]:
    bids = order_book.get("bids", [])
    asks = order_book.get("asks", [])
    best_bid = safe_float(bids[0]["price"]) if bids else None
    best_ask = safe_float(asks[0]["price"]) if asks else None
    remaining = payout_size
    cost = 0.0
    max_fill = 0.0
    for level in asks:
        price = safe_float(level.get("price"))
        size = safe_float(level.get("size"), 0.0) or 0.0
        if price is None or not isfinite(price):
            continue
        max_fill += size
        if remaining <= 0:
            continue
        take = min(size, remaining)
        cost += take * price
        remaining -= take
    if remaining > 0:
        return best_bid, best_ask, None, max_fill
    return best_bid, best_ask, cost, max_fill


def _fallback_quote(
    outcome_id: str,
    label: str,
    price: float | None,
    payout_size: float,
    fee_bps: int,
    slippage_bps: int,
    max_fill_usd: float,
    fetched_at,
    metadata: dict[str, Any] | None = None,
) -> OutcomeQuote:
    total_multiplier = (1 + fee_bps / 10_000) * (1 + slippage_bps / 10_000)
    quoted_cost = price * payout_size * total_multiplier if price is not None else None
    best_bid = None if price is None else max(price - price * (slippage_bps / 20_000), 0.0)
    return OutcomeQuote(
        outcome_id=outcome_id,
        label=label,
        quote_type="sports_fallback",
        best_bid=best_bid,
        best_ask=price,
        quoted_cost_for_size=quoted_cost,
        fee_bps=fee_bps,
        slippage_bps_est=slippage_bps,
        max_fill_usd=max_fill_usd,
        quote_timestamp=fetched_at,
        metadata=metadata or {},
    )


def _top_of_book_quote(
    outcome_id: str,
    label: str,
    best_bid: float | None,
    best_ask: float | None,
    payout_size: float,
    fee_bps: int,
    slippage_bps: int,
    max_fill_usd: float,
    fetched_at,
    metadata: dict[str, Any] | None = None,
) -> OutcomeQuote:
    total_multiplier = (1 + fee_bps / 10_000) * (1 + slippage_bps / 10_000)
    quoted_cost = best_ask * payout_size * total_multiplier if best_ask is not None else None
    return OutcomeQuote(
        outcome_id=outcome_id,
        label=label,
        quote_type="sports_top_of_book",
        best_bid=best_bid,
        best_ask=best_ask,
        quoted_cost_for_size=quoted_cost,
        fee_bps=fee_bps,
        slippage_bps_est=slippage_bps,
        max_fill_usd=max_fill_usd,
        quote_timestamp=fetched_at,
        metadata=metadata or {},
    )


def _normalize_key(value: str) -> str:
    cleaned = normalize_text(value)
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    return cleaned or "unknown"


def _normalized_tokens(
    value: str,
    aliases: dict[str, str],
    drop_tokens: set[str],
    phrase_aliases: dict[str, str] | None = None,
) -> list[str]:
    normalized = normalize_text(value)
    for source, target in (phrase_aliases or {}).items():
        normalized = normalized.replace(source, target)
    cleaned = re.sub(r"[^a-z0-9]+", " ", normalized)
    tokens: list[str] = []
    for token in cleaned.split():
        if not token or token in drop_tokens:
            continue
        expanded = aliases.get(token, token)
        for expanded_token in expanded.split():
            if expanded_token and expanded_token not in drop_tokens:
                tokens.append(expanded_token)
    return tokens


def _canonical_participant(value: str) -> str:
    tokens = _normalized_tokens(value, PARTICIPANT_TOKEN_ALIASES, PARTICIPANT_DROP_TOKENS)
    return " ".join(tokens) or "unknown"


def _participant_signature_keys(value: str) -> tuple[str, ...]:
    canonical = _canonical_participant(value)
    if canonical == "unknown":
        return ()
    tokens = [token for token in canonical.split() if token]
    signatures = {canonical}
    if tokens:
        if len(tokens[-1]) >= 4:
            signatures.add(tokens[-1])
        if len(tokens) >= 2:
            signatures.add(" ".join(tokens[-2:]))
            prefix_key = " ".join(token[:4] for token in tokens)
            if prefix_key and prefix_key != canonical:
                signatures.add(prefix_key)
            initials = "".join(token[0] for token in tokens if token)
            if len(initials) >= 2:
                signatures.add(initials)
    return tuple(sorted(signature for signature in signatures if len(signature) >= 2))


def _clean_event_side(value: str) -> str:
    cleaned = value.strip()
    cleaned = re.sub(r"^[A-Za-z0-9 .&'+/-]{2,20}:\s*", "", cleaned)
    cleaned = re.sub(r"\([^)]*\)", "", cleaned)
    cleaned = re.sub(r"\b(?:total|spread|handicap|winner)\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:?,.")
    return cleaned


def _extract_participants(title: str, raw_participants: list[str] | None = None) -> tuple[str | None, str | None]:
    if raw_participants and len(raw_participants) >= 2:
        distinct = [value for value in raw_participants if value and value.strip()]
        canonical = {canonicalize_label(value) for value in distinct}
        if len(canonical) >= 2:
            return distinct[0], distinct[1]
    for separator in (" vs ", " v ", " - ", " @ ", " at "):
        if separator in title.lower():
            parts = re.split(separator, title, maxsplit=1, flags=re.IGNORECASE)
            parts = [_clean_event_side(part) for part in parts if part.strip()]
            if len(parts) >= 2:
                return parts[0], parts[1]
    match = re.search(
        r"will\s+(.+?)\s+(?:beat|defeat|win against)\s+(.+?)(?:\s+in\b|\s+at\b|\?|$)",
        title,
        flags=re.IGNORECASE,
    )
    if match:
        return _clean_event_side(match.group(1)), _clean_event_side(match.group(2))
    return None, None


def _extract_best_of(value: str) -> int | None:
    match = re.search(r"(?:bo|best of)\s*([1-9])", value, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _extract_period_scope(value: str) -> tuple[str, int | None, int | None]:
    lowered = value.lower()
    map_match = re.search(r"map\s*([1-9])", lowered)
    if map_match:
        return "map", int(map_match.group(1)), None
    round_match = re.search(r"round\s*([1-9]\d*)", lowered)
    if round_match:
        return "round", None, int(round_match.group(1))
    if "1st half" in lowered:
        return "half_1", None, None
    if "2nd half" in lowered:
        return "half_2", None, None
    if "1st period" in lowered:
        return "period_1", None, None
    if "2nd period" in lowered:
        return "period_2", None, None
    if "3rd period" in lowered:
        return "period_3", None, None
    return "full_match", None, None


def _extract_line_value(value: str) -> float | None:
    match = re.search(r"([+-]?\d+(?:\.\d+)?)", value)
    return float(match.group(1)) if match else None


def _extract_season_key(value: str) -> str | None:
    match = re.search(r"\b(20\d{2})(?:[-/](20\d{2}))?\b", value)
    if not match:
        return None
    left = match.group(1)
    right = match.group(2)
    return f"{left}-{right}" if right else left


def _canonical_participant_keys(participants: tuple[str | None, str | None]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for value in participants:
        if not value:
            continue
        canonical = _canonical_participant(value)
        if canonical != "unknown":
            normalized.add(canonical)
    return tuple(sorted(normalized))


def _date_bucket(start_time) -> str | None:
    return start_time.date().isoformat() if start_time else None


def _extract_event_stage_key(title: str, description: str) -> str | None:
    combined = normalize_text(f"{title} {description}")
    for pattern in EVENT_STAGE_PATTERNS:
        match = re.search(pattern, combined, flags=re.IGNORECASE)
        if match:
            return _normalize_key(match.group(0))
    return None


def _competition_identity_key(value: str) -> str | None:
    tokens = _normalized_tokens(
        value,
        COMPETITION_TOKEN_ALIASES,
        COMPETITION_DROP_TOKENS,
        phrase_aliases=COMPETITION_PHRASE_ALIASES,
    )
    normalized = _normalize_key(" ".join(tokens))
    if normalized in GENERIC_COMPETITION_KEYS:
        return None
    if normalized.startswith("kx") and len(normalized) > 12:
        return None
    return normalized


def _underlying_event_title(
    title: str,
    participants: tuple[str | None, str | None],
    event_class: str,
    event_stage_key: str | None,
) -> str:
    if event_class != "future" and participants[0] and participants[1]:
        base = f"{participants[0].strip()} vs {participants[1].strip()}"
        if event_stage_key:
            base = f"{base} ({event_stage_key.replace('_', ' ')})"
        return base
    return title.strip()


def _fixture_event_keys(
    sport_key: str,
    competition_key: str,
    participant_keys: tuple[str, ...],
    date_bucket: str | None,
    event_stage_key: str | None,
    underlying_title_key: str,
) -> tuple[str | None, str | None]:
    participant_component = "|".join(participant_keys) or underlying_title_key
    if not participant_component:
        return None, None
    bucket = date_bucket or "unknown_date"
    soft_event_key = "|".join(["fixture", sport_key, participant_component, bucket])
    strict_parts = ["fixture", sport_key]
    competition_component = _competition_identity_key(competition_key)
    if competition_component:
        strict_parts.append(competition_component)
    strict_parts.extend([participant_component, bucket])
    if event_stage_key:
        strict_parts.append(event_stage_key)
    return "|".join(strict_parts), soft_event_key


def _future_event_keys(
    sport_key: str,
    competition_key: str,
    season_key: str | None,
    underlying_title_key: str,
) -> tuple[str | None, str | None]:
    if not underlying_title_key:
        return None, None
    competition_component = _competition_identity_key(competition_key) or "unknown"
    strict_event_key = "|".join(["future", sport_key, competition_component, season_key or "na", underlying_title_key])
    soft_event_key = "|".join(["future", sport_key, underlying_title_key])
    return strict_event_key, soft_event_key


def _detect_sport_key(*values: str) -> tuple[str, bool]:
    combined = normalize_text(" ".join(values))

    def has_marker(marker: str) -> bool:
        normalized_marker = normalize_text(marker)
        if not normalized_marker:
            return False
        return re.search(rf"(?<![a-z0-9]){re.escape(normalized_marker)}(?![a-z0-9])", combined) is not None

    for marker in ESPORT_MARKERS:
        if has_marker(marker):
            return "esports", True
    for marker, sport_key in SPORT_ALIASES.items():
        if has_marker(marker):
            return sport_key, sport_key == "esports"
    return "other", False


def _competition_key(*values: str) -> str:
    for value in values:
        key = _normalize_key(value)
        if key != "unknown":
            return key
    return "unknown"


def _market_family(title: str, description: str, outcome_labels: list[str], event_class: str) -> str:
    combined = " ".join([title, description, *outcome_labels]).lower()
    if any(marker in combined for marker in TOTAL_MARKERS) and _extract_line_value(combined) is not None:
        return "total"
    if any(marker in combined for marker in SPREAD_MARKERS) and _extract_line_value(combined) is not None:
        return "handicap" if "handicap" in combined else "spread"
    if "map winner" in combined or "map " in combined:
        return "map_winner"
    if "round winner" in combined or "round " in combined:
        return "round_winner"
    if event_class == "future":
        return "outright"
    if len(outcome_labels) == 3 and any("draw" in canonicalize_label(label) for label in outcome_labels):
        return "three_way"
    if len(outcome_labels) == 2:
        if any(marker in combined for marker in PROP_MARKERS):
            return "prop"
        return "moneyline_2way"
    return "prop"


def _event_class(title: str, description: str, market_family: str, participants: tuple[str | None, str | None]) -> str:
    combined = f"{title} {description}".lower()
    single_game_winner = participants[0] and participants[1] and "winner" in combined and not any(
        marker in combined for marker in ("championship", "tournament", "league winner", "season", "cup")
    )
    if any(marker in combined for marker in FUTURE_MARKERS) and not single_game_winner:
        return "future"
    if market_family in {"spread", "total", "handicap", "map_winner", "round_winner"}:
        return "line"
    if market_family == "prop":
        return "prop"
    if participants[0] and participants[1]:
        return "match"
    return "future" if "winner" in combined else "prop"


def _looks_like_participant_labels(outcome_labels: list[str]) -> bool:
    if len(outcome_labels) != 2:
        return False
    canonical = {canonicalize_label(label) for label in outcome_labels}
    if canonical & {"yes", "no", "draw"}:
        return False
    combined = " ".join(outcome_labels).lower()
    if any(marker in combined for marker in TOTAL_MARKERS + SPREAD_MARKERS + PROP_MARKERS):
        return False
    return True


def _selection_key(market_family: str, label: str, line_value: float | None, line_side: str | None) -> str:
    canonical = _normalize_key(canonicalize_label(label))
    if market_family == "total" and line_value is not None:
        return f"{canonical}_{line_value:g}"
    if market_family in {"spread", "handicap"} and line_value is not None:
        return f"{line_side or canonical}_{line_value:g}"
    return canonical


def _sport_market_features(
    title: str,
    description: str,
    participants: tuple[str | None, str | None],
    competition_key: str,
    start_time,
    sport_key: str,
    event_class: str,
    season_key: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    participant_list = [value for value in participants if value]
    participant_keys = _canonical_participant_keys(participants)
    participant_signature_keys = sorted(
        {
            signature
            for participant in participant_list
            for signature in _participant_signature_keys(participant)
        }
    )
    date_bucket = _date_bucket(start_time)
    event_stage_key = _extract_event_stage_key(title, description)
    underlying_event_title = _underlying_event_title(title, participants, event_class, event_stage_key)
    underlying_event_title_key = event_title_key(underlying_event_title, participant_list)
    underlying_event_title_tokens = sorted(event_title_tokens(underlying_event_title, participant_list))
    competition_identity_key = _competition_identity_key(competition_key)
    if event_class == "future":
        strict_event_key, soft_event_key = _future_event_keys(
            sport_key,
            competition_key,
            season_key,
            underlying_event_title_key,
        )
    else:
        strict_event_key, soft_event_key = _fixture_event_keys(
            sport_key,
            competition_key,
            participant_keys,
            date_bucket,
            event_stage_key,
            underlying_event_title_key,
        )
    features = {
        "event_title_key": event_title_key(title, participant_list),
        "event_title_tokens": sorted(event_title_tokens(title, participant_list)),
        "underlying_event_title": underlying_event_title,
        "underlying_event_title_key": underlying_event_title_key,
        "underlying_event_title_tokens": underlying_event_title_tokens,
        "strict_event_key": strict_event_key,
        "soft_event_key": soft_event_key,
        "event_stage_key": event_stage_key,
        "participants_key": "|".join(participant_keys) or None,
        "participant_signature_keys": participant_signature_keys,
        "competition_key_norm": competition_identity_key or _normalize_key(competition_key),
        "date_bucket": date_bucket,
    }
    if extra:
        features.update(extra)
    return features


def _attach_source_metadata(features: dict[str, Any], snapshot: MarketSnapshot) -> dict[str, Any]:
    features = dict(features)
    features["source_variant"] = str(snapshot.source.get("variant") or "default")
    features["source_status"] = str(snapshot.source.get("status") or "ok")
    completeness = snapshot.source.get("completeness")
    try:
        features["source_completeness"] = float(completeness) if completeness is not None else 1.0
    except (TypeError, ValueError):
        features["source_completeness"] = 1.0
    return features


def _clean_kalshi_participant(value: str) -> str:
    cleaned = value.strip(" ?.,:-")
    cleaned = re.sub(r"^.*?\b(?:refers to the|win the|between)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:the|if|will)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\s+(?:men's|women's|college|professional|basketball|baseball|football|soccer|hockey|game|match|winner)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" ?.,:-")


def _extract_kalshi_participants(title: str, description: str, raw_yes_label: str, raw_no_label: str) -> tuple[str | None, str | None]:
    labels = [value for value in [raw_yes_label, raw_no_label] if value]
    canonical_labels = {canonicalize_label(value) for value in labels}
    if len(canonical_labels) >= 2:
        participants = _extract_participants(title, labels)
        if participants[0] and participants[1]:
            return participants

    patterns = [
        r"\bwin the\s+(.+?)\s+(?:vs|v|at)\s+(.+?)(?:\s*[:?(,]|\s+(?:round|match|game|winner)\b|$)",
        r"\brefers to the\s+(.+?)\s+(?:vs|v|at)\s+(.+?)(?:\s*[:?(,]|\s+(?:round|match|game|winner)\b|$)",
        r"\bin the\s+(.+?)\s+(?:vs|v|at)\s+(.+?)(?:\s+(?:men's|women's|college|professional|basketball|baseball|football|soccer|hockey|game|match)\b|\?|,|$)",
        r"\bbetween\s+(.+?)\s+and\s+(.+?)(?:\s+(?:in|on|for)\b|\?|,|$)",
        r"\b(.+?)\s+(?:vs|v|at)\s+(.+?)(?:\s+(?:men's|women's|college|professional|basketball|baseball|football|soccer|hockey|game|match|winner)\b|\?|,|$)",
    ]
    for source in (description, title):
        for pattern in patterns:
            match = re.search(pattern, source, flags=re.IGNORECASE)
            if not match:
                continue
            left = _clean_kalshi_participant(match.group(1))
            right = _clean_kalshi_participant(match.group(2))
            if left and right and canonicalize_label(left) != canonicalize_label(right):
                return left, right
    return _extract_participants(title, labels)


def _parse_kalshi_date(raw: dict[str, Any], description: str):
    base_time = parse_datetime(str(raw.get("close_time") or raw.get("expiration_time") or raw.get("expected_expiration_time") or ""))
    description_match = re.search(r"\b(?:scheduled for|on)\s+([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(20\d{2})", description, flags=re.IGNORECASE)
    if description_match:
        month = MONTHS.get(description_match.group(1)[:3].lower())
        day = int(description_match.group(2))
        year = int(description_match.group(3))
        if month:
            if base_time:
                return base_time.replace(year=year, month=month, day=day)
            return datetime(year, month, day, tzinfo=UTC)

    ticker_source = str(raw.get("event_ticker") or raw.get("ticker") or "")
    ticker_match = re.search(r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})", ticker_source)
    if ticker_match:
        year = 2000 + int(ticker_match.group(1))
        month = MONTHS[ticker_match.group(2).lower()]
        day = int(ticker_match.group(3))
        if base_time:
            return base_time.replace(year=year, month=month, day=day)
        return datetime(year, month, day, tzinfo=UTC)
    return base_time


def _kalshi_competition_key(raw: dict[str, Any], title: str, description: str) -> str:
    event_ticker = str(raw.get("event_ticker") or "")
    event_series_ticker = str(raw.get("event_series_ticker") or "")
    event_category = str(raw.get("event_category") or "")
    combined = f"{title} {description} {event_ticker} {event_series_ticker} {event_category}".lower()
    if "ncaa" in combined or "college basketball" in combined:
        return "ncaa_basketball"
    if "nba" in combined or "professional basketball" in combined:
        return "nba"
    if "nhl" in combined or "hockey" in combined:
        return "nhl"
    if "mlb" in combined or "baseball" in combined:
        return "mlb"
    return _competition_key(event_series_ticker, event_ticker, event_category, title, description)


def _is_yes_no_market(outcome_labels: list[str]) -> bool:
    canonical = {canonicalize_label(label) for label in outcome_labels}
    return canonical == {"yes", "no"}


def _auto_arb_enabled(
    settings: Settings,
    market_family: str,
    event_class: str,
    outcome_labels: list[str],
) -> bool:
    if market_family not in settings.sports_auto_arb_families:
        return False
    if event_class not in {"match", "future"}:
        return False
    if market_family == "outright" and _is_yes_no_market(outcome_labels):
        return False
    return True


def _line_side(label: str, participants: tuple[str | None, str | None]) -> str | None:
    canonical = canonicalize_label(label)
    for participant in participants:
        if participant and canonicalize_label(participant) in canonical:
            return _normalize_key(participant)
    return None


def _build_polymarket(snapshot: MarketSnapshot, settings: Settings) -> SportsMarket | None:
    raw = snapshot.raw
    title = str(raw.get("question") or raw.get("title") or snapshot.market_id)
    description = str(raw.get("description") or "")
    category_value = " ".join(str(raw.get(key) or "") for key in ("category", "groupItemTitle", "series"))
    sport_key, is_esports = _detect_sport_key(title, description, category_value)
    if sport_key == "other":
        return None

    outcome_labels = [str(item) for item in _as_list(raw.get("outcomes"))]
    outcome_prices = [safe_float(item) for item in _as_list(raw.get("outcomePrices"))]
    clob_token_ids = [str(item) for item in _as_list(raw.get("clobTokenIds"))]
    order_books = raw.get("orderBooks") if isinstance(raw.get("orderBooks"), dict) else {}
    participants = _extract_participants(title)
    rough_family = _market_family(title, description, outcome_labels, "match")
    event_class = _event_class(title, description, rough_family, participants)
    market_family = _market_family(title, description, outcome_labels, event_class)
    if event_class == "match" and participants[0] and participants[1] and _is_yes_no_market(outcome_labels):
        market_family = "moneyline_2way" if sport_key in NO_DRAW_SPORTS else "prop"
    period_scope, map_no, round_no = _extract_period_scope(f"{title} {description}")
    line_value = _extract_line_value(f"{title} {' '.join(outcome_labels)}") if market_family in {"spread", "total", "handicap"} else None
    best_of = _extract_best_of(f"{title} {description}")
    competition_key = _competition_key(str(raw.get("groupItemTitle") or ""), str(raw.get("series") or ""), title)
    season_key = _extract_season_key(f"{title} {description}")
    start_time = parse_datetime(str(raw.get("eventStartTime") or raw.get("gameStartTime") or raw.get("endDate") or ""))

    outcomes: list[SportsOutcome] = []
    for index, label in enumerate(outcome_labels):
        token_id = clob_token_ids[index] if index < len(clob_token_ids) else f"{snapshot.market_id}:{index}"
        book = order_books.get(token_id) if isinstance(order_books, dict) else None
        price = outcome_prices[index] if index < len(outcome_prices) else None
        side = _line_side(label, participants)
        if isinstance(book, dict):
            best_bid, best_ask, quoted_cost, max_fill = _cost_from_order_book(book, settings.quote_size_usd)
            quote = OutcomeQuote(
                outcome_id=token_id,
                label=label,
                quote_type="clob_ask_depth",
                best_bid=best_bid,
                best_ask=best_ask if best_ask is not None else price,
                quoted_cost_for_size=quoted_cost if quoted_cost is not None else (price * settings.quote_size_usd if price else None),
                fee_bps=settings.polymarket_fee_bps,
                slippage_bps_est=0,
                max_fill_usd=max_fill,
                quote_timestamp=snapshot.fetched_at,
                metadata={"token_id": token_id},
            )
        else:
            quote = _fallback_quote(
                outcome_id=token_id,
                label=label,
                price=price,
                payout_size=settings.quote_size_usd,
                fee_bps=settings.polymarket_fee_bps,
                slippage_bps=0,
                max_fill_usd=safe_float(raw.get("liquidityNum"), safe_float(raw.get("liquidity"), settings.quote_size_usd)) or settings.quote_size_usd,
                fetched_at=snapshot.fetched_at,
                metadata={"token_id": token_id},
            )
        if market_family == "moneyline_2way" and participants[0] and participants[1] and _is_yes_no_market(outcome_labels):
            canonical = canonicalize_label(label)
            selection_key = _normalize_key(participants[0] if canonical == "yes" else participants[1])
        else:
            selection_key = _selection_key(market_family, label, line_value, side)
        outcomes.append(
            SportsOutcome(
                outcome_id=token_id,
                label=label,
                canonical_label=canonicalize_label(label),
                selection_key=selection_key,
                quote=quote,
            )
        )

    participants_sorted = tuple(sorted(_canonical_participant(value) for value in participants if value))
    features = _attach_source_metadata(_sport_market_features(
        title,
        description,
        participants,
        competition_key,
        start_time,
        sport_key,
        event_class,
        season_key,
        {"resolution_rules_hash": text_hash(f"{description}|{title}")},
    ), snapshot)
    resolution_profile = build_sports_resolution_profile(
        title=title,
        description=description,
        raw=raw,
        event_class=event_class,
        market_family=market_family,
        period_scope=period_scope,
        line_value=line_value,
        line_side=None,
        participants=[value for value in participants if value],
        start_time=start_time,
    )
    features["resolution_profile_signature"] = resolution_profile_signature(resolution_profile)
    return SportsMarket(
        exchange="polymarket",
        market_id=snapshot.market_id,
        title=title,
        description=description,
        event_class=event_class,
        match_lane=event_class,
        sport_key=sport_key,
        competition_key=competition_key,
        season_key=season_key,
        is_esports=is_esports,
        participants_home=participants[0],
        participants_away=participants[1],
        participants_sorted=participants_sorted,
        start_time=start_time,
        best_of=best_of,
        period_scope=period_scope,
        map_no=map_no,
        round_no=round_no,
        market_family=market_family,
        line_value=line_value,
        line_side=None,
        auto_arb_enabled=_auto_arb_enabled(settings, market_family, event_class, outcome_labels),
        chain="polygon",
        token="USDC",
        outcomes=outcomes,
        volume_usd=safe_float(raw.get("volumeNum"), safe_float(raw.get("volume"), 0.0)) or 0.0,
        liquidity_usd=safe_float(raw.get("liquidityNum"), safe_float(raw.get("liquidity"), 0.0)) or 0.0,
        fetched_at=snapshot.fetched_at,
        raw_data=raw,
        resolution_profile=resolution_profile,
        features=features,
    )


def _azuro_chain(environment: str) -> tuple[str, str]:
    mapping = {
        "PolygonUSDT": ("polygon", "USDT"),
        "PolygonDGEN": ("polygon", "DGEN"),
        "GnosisXDAI": ("gnosis", "XDAI"),
        "BaseWETH": ("base", "WETH"),
    }
    return mapping.get(environment, ("unknown", environment))


def _azuro_outcome_labels(game: dict[str, Any], condition: dict[str, Any]) -> list[str]:
    condition_outcomes = condition.get("outcomes") if isinstance(condition.get("outcomes"), list) else []
    explicit = [str(item.get("title")) for item in condition_outcomes if item.get("title")]
    if explicit and len(explicit) == len(condition_outcomes):
        return explicit
    participants = [str(item.get("name")) for item in game.get("participants", []) if item.get("name")]
    if len(condition_outcomes) == 2 and len(participants) >= 2:
        return participants[:2]
    if len(condition_outcomes) == 3 and len(participants) >= 2:
        return [participants[0], "Draw", participants[1]]
    return [f"Outcome {index + 1}" for index in range(len(condition_outcomes))]


def _build_azuro(snapshot: MarketSnapshot, settings: Settings) -> SportsMarket | None:
    raw = snapshot.raw
    environment = str(raw.get("environment") or "unknown")
    game = dict(raw.get("game") or {})
    condition = dict(raw.get("condition") or {})
    sport_name = str(game.get("sport", {}).get("name") or game.get("sport", {}).get("slug") or "")
    league_name = str(game.get("league", {}).get("name") or game.get("league", {}).get("slug") or "")
    title = str(condition.get("title") or game.get("title") or snapshot.market_id)
    description = ""
    outcome_labels = _azuro_outcome_labels(game, condition)
    sport_key, is_esports = _detect_sport_key(sport_name, league_name, title)
    if sport_key == "other":
        return None
    participants = _extract_participants(title, [str(item.get("name")) for item in game.get("participants", []) if item.get("name")])
    rough_family = _market_family(title, description, outcome_labels, "match")
    event_class = _event_class(title, description, rough_family, participants)
    market_family = _market_family(title, description, outcome_labels, event_class)
    period_scope, map_no, round_no = _extract_period_scope(f"{title} {description}")
    line_value = _extract_line_value(f"{title} {' '.join(outcome_labels)}") if market_family in {"spread", "total", "handicap"} else None
    best_of = _extract_best_of(f"{title} {description}")
    chain, token = _azuro_chain(environment)
    start_time = parse_datetime(str(game.get("startsAt") or ""))
    season_key = _extract_season_key(f"{league_name} {title}")
    margin_bps = int((safe_float(condition.get("margin"), 0.0) or 0.0) * 100)

    outcomes: list[SportsOutcome] = []
    for index, outcome in enumerate(condition.get("outcomes", [])):
        label = outcome_labels[index] if index < len(outcome_labels) else f"Outcome {index + 1}"
        odds = safe_float(outcome.get("odds"))
        price = (1 / odds) if odds and odds > 0 else None
        side = _line_side(label, participants)
        quote = _fallback_quote(
            outcome_id=str(outcome.get("outcomeId") or index),
            label=label,
            price=price,
            payout_size=settings.quote_size_usd,
            fee_bps=0,
            slippage_bps=settings.azuro_slippage_bps + margin_bps,
            max_fill_usd=max(100.0, safe_float(game.get("turnover"), 0.0) or 0.0),
            fetched_at=snapshot.fetched_at,
            metadata={"odds": odds, "environment": environment},
        )
        outcomes.append(
            SportsOutcome(
                outcome_id=str(outcome.get("outcomeId") or index),
                label=label,
                canonical_label=canonicalize_label(label),
                selection_key=_selection_key(market_family, label, line_value, side),
                quote=quote,
            )
        )

    participants_sorted = tuple(sorted(_canonical_participant(value) for value in participants if value))
    competition_key = _competition_key(league_name, str(game.get("country", {}).get("name") or ""), title)
    features = _attach_source_metadata(_sport_market_features(
        title,
        description,
        participants,
        competition_key,
        start_time,
        sport_key,
        event_class,
        season_key,
        {"environment": environment},
    ), snapshot)
    resolution_profile = build_sports_resolution_profile(
        title=title,
        description=description,
        raw=raw,
        event_class=event_class,
        market_family=market_family,
        period_scope=period_scope,
        line_value=line_value,
        line_side=None,
        participants=[value for value in participants if value],
        start_time=start_time,
    )
    features["resolution_profile_signature"] = resolution_profile_signature(resolution_profile)
    return SportsMarket(
        exchange="azuro",
        market_id=snapshot.market_id,
        title=title,
        description=description,
        event_class=event_class,
        match_lane=event_class,
        sport_key=sport_key,
        competition_key=competition_key,
        season_key=season_key,
        is_esports=is_esports,
        participants_home=participants[0],
        participants_away=participants[1],
        participants_sorted=participants_sorted,
        start_time=start_time,
        best_of=best_of,
        period_scope=period_scope,
        map_no=map_no,
        round_no=round_no,
        market_family=market_family,
        line_value=line_value,
        line_side=None,
        auto_arb_enabled=_auto_arb_enabled(settings, market_family, event_class, outcome_labels),
        chain=chain,
        token=token,
        outcomes=outcomes,
        volume_usd=safe_float(game.get("turnover"), 0.0) or 0.0,
        liquidity_usd=safe_float(game.get("turnover"), 0.0) or 0.0,
        fetched_at=snapshot.fetched_at,
        raw_data=raw,
        resolution_profile=resolution_profile,
        features=features,
    )


def _myriad_chain(network_id: object) -> str:
    mapping = {274133: "abstract", "274133": "abstract"}
    return mapping.get(network_id, f"network_{network_id}")


def _build_myriad(snapshot: MarketSnapshot, settings: Settings) -> SportsMarket | None:
    raw = snapshot.raw
    title = str(raw.get("title") or snapshot.market_id)
    description = str(raw.get("description") or "")
    topics = [str(item) for item in raw.get("topics", []) if item]
    sport_key, is_esports = _detect_sport_key(title, description, " ".join(topics), str(raw.get("category") or ""))
    if sport_key == "other":
        return None
    outcomes_payload = raw.get("outcomes") if isinstance(raw.get("outcomes"), list) else []
    outcome_labels = [str(item.get("title") or f"Outcome {index + 1}") for index, item in enumerate(outcomes_payload)]
    participants = _extract_participants(title)
    rough_family = _market_family(title, description, outcome_labels, "match")
    event_class = _event_class(title, description, rough_family, participants)
    market_family = _market_family(title, description, outcome_labels, event_class)
    if event_class == "match" and participants[0] and participants[1] and _is_yes_no_market(outcome_labels):
        market_family = "moneyline_2way" if sport_key in NO_DRAW_SPORTS else "prop"
    period_scope, map_no, round_no = _extract_period_scope(f"{title} {description}")
    line_value = _extract_line_value(f"{title} {' '.join(outcome_labels)}") if market_family in {"spread", "total", "handicap"} else None
    best_of = _extract_best_of(f"{title} {description}")
    fee_bps = int(
        (safe_float(raw.get("fees", {}).get("buy", {}).get("fee"), safe_float(raw.get("fee"), 0.0)) or 0.0) * 10_000
    )
    liquidity = safe_float(raw.get("liquidity"), 0.0) or 0.0

    outcomes: list[SportsOutcome] = []
    for index, outcome in enumerate(outcomes_payload):
        label = outcome_labels[index]
        side = _line_side(label, participants)
        quote = _fallback_quote(
            outcome_id=str(outcome.get("id") or index),
            label=label,
            price=safe_float(outcome.get("price")),
            payout_size=settings.quote_size_usd,
            fee_bps=fee_bps or settings.myriad_default_fee_bps,
            slippage_bps=settings.myriad_slippage_bps,
            max_fill_usd=max(50.0, liquidity * 0.50),
            fetched_at=snapshot.fetched_at,
            metadata={"holders": outcome.get("holders"), "shares": outcome.get("shares")},
        )
        if market_family == "moneyline_2way" and participants[0] and participants[1] and _is_yes_no_market(outcome_labels):
            canonical = canonicalize_label(label)
            selection_key = _normalize_key(participants[0] if canonical == "yes" else participants[1])
        else:
            selection_key = _selection_key(market_family, label, line_value, side)
        outcomes.append(
            SportsOutcome(
                outcome_id=str(outcome.get("id") or index),
                label=label,
                canonical_label=canonicalize_label(label),
                selection_key=selection_key,
                quote=quote,
            )
        )

    participants_sorted = tuple(sorted(_canonical_participant(value) for value in participants if value))
    competition_key = _competition_key(str(raw.get("category") or ""), str(raw.get("series") or ""), " ".join(topics), title)
    start_time = parse_datetime(str(raw.get("resolvesAt") or raw.get("expiresAt") or ""))
    season_key = _extract_season_key(f"{title} {description}")
    features = _attach_source_metadata(_sport_market_features(
        title,
        description,
        participants,
        competition_key,
        start_time,
        sport_key,
        event_class,
        season_key,
        {"topics": topics},
    ), snapshot)
    resolution_profile = build_sports_resolution_profile(
        title=title,
        description=description,
        raw=raw,
        event_class=event_class,
        market_family=market_family,
        period_scope=period_scope,
        line_value=line_value,
        line_side=None,
        participants=[value for value in participants if value],
        start_time=start_time,
    )
    features["resolution_profile_signature"] = resolution_profile_signature(resolution_profile)
    return SportsMarket(
        exchange="myriad",
        market_id=snapshot.market_id,
        title=title,
        description=description,
        event_class=event_class,
        match_lane=event_class,
        sport_key=sport_key,
        competition_key=competition_key,
        season_key=season_key,
        is_esports=is_esports,
        participants_home=participants[0],
        participants_away=participants[1],
        participants_sorted=participants_sorted,
        start_time=start_time,
        best_of=best_of,
        period_scope=period_scope,
        map_no=map_no,
        round_no=round_no,
        market_family=market_family,
        line_value=line_value,
        line_side=None,
        auto_arb_enabled=_auto_arb_enabled(settings, market_family, event_class, outcome_labels),
        chain=_myriad_chain(raw.get("networkId")),
        token=str(raw.get("token", {}).get("symbol") or "USDC"),
        outcomes=outcomes,
        volume_usd=safe_float(raw.get("volume"), 0.0) or 0.0,
        liquidity_usd=liquidity,
        fetched_at=snapshot.fetched_at,
        raw_data=raw,
        resolution_profile=resolution_profile,
        features=features,
    )


def _build_opn(snapshot: MarketSnapshot, settings: Settings) -> SportsMarket | None:
    raw = snapshot.raw
    title = extract_opn_title(raw) or snapshot.market_id
    description = extract_opn_description(raw)
    tags = extract_opn_tags(raw)
    sport_key, is_esports = _detect_sport_key(title, description, " ".join(tags))
    if sport_key == "other":
        return None

    outcome_specs = extract_opn_outcomes(raw)
    outcome_labels = [str(item["label"]) for item in outcome_specs]
    participants = _extract_participants(title, outcome_labels if _looks_like_participant_labels(outcome_labels) else None)
    rough_family = _market_family(title, description, outcome_labels, "match")
    event_class = _event_class(title, description, rough_family, participants)
    market_family = _market_family(title, description, outcome_labels, event_class)
    period_scope, map_no, round_no = _extract_period_scope(f"{title} {description}")
    line_value = _extract_line_value(f"{title} {' '.join(outcome_labels)}") if market_family in {"spread", "total", "handicap"} else None
    best_of = _extract_best_of(f"{title} {description}")
    start_time = extract_opn_closes_at(raw)

    outcomes: list[SportsOutcome] = []
    for index, outcome in enumerate(outcome_specs):
        label = str(outcome["label"])
        side = _line_side(label, participants)
        quote = _fallback_quote(
            outcome_id=str(outcome.get("outcome_id") or index),
            label=label,
            price=safe_float(outcome.get("price")),
            payout_size=settings.quote_size_usd,
            fee_bps=settings.opn_fee_bps,
            slippage_bps=settings.opn_slippage_bps,
            max_fill_usd=max(extract_opn_liquidity(raw), settings.quote_size_usd),
            fetched_at=snapshot.fetched_at,
            metadata={"source": raw.get("sourceMode")},
        )
        outcomes.append(
            SportsOutcome(
                outcome_id=str(outcome.get("outcome_id") or index),
                label=label,
                canonical_label=canonicalize_label(label),
                selection_key=_selection_key(market_family, label, line_value, side),
                quote=quote,
            )
        )

    participants_sorted = tuple(sorted(_canonical_participant(value) for value in participants if value))
    category = extract_opn_category(raw, title, description)
    competition_key = _competition_key(category, *tags, title)
    season_key = _extract_season_key(f"{title} {description}")
    features = _attach_source_metadata(_sport_market_features(
        title,
        description,
        participants,
        competition_key,
        start_time,
        sport_key,
        event_class,
        season_key,
        {"tags": tags},
    ), snapshot)
    resolution_profile = build_sports_resolution_profile(
        title=title,
        description=description,
        raw=raw,
        event_class=event_class,
        market_family=market_family,
        period_scope=period_scope,
        line_value=line_value,
        line_side=None,
        participants=[value for value in participants if value],
        start_time=start_time,
    )
    features["resolution_profile_signature"] = resolution_profile_signature(resolution_profile)
    return SportsMarket(
        exchange="opn",
        market_id=snapshot.market_id,
        title=title,
        description=description,
        event_class=event_class,
        match_lane=event_class,
        sport_key=sport_key,
        competition_key=competition_key,
        season_key=season_key,
        is_esports=is_esports,
        participants_home=participants[0],
        participants_away=participants[1],
        participants_sorted=participants_sorted,
        start_time=start_time,
        best_of=best_of,
        period_scope=period_scope,
        map_no=map_no,
        round_no=round_no,
        market_family=market_family,
        line_value=line_value,
        line_side=None,
        auto_arb_enabled=_auto_arb_enabled(settings, market_family, event_class, outcome_labels),
        chain=extract_opn_chain(raw),
        token=extract_opn_token(raw),
        outcomes=outcomes,
        volume_usd=extract_opn_volume(raw),
        liquidity_usd=extract_opn_liquidity(raw),
        fetched_at=snapshot.fetched_at,
        raw_data=raw,
        resolution_profile=resolution_profile,
        features=features,
    )


def _kalshi_price(raw: dict[str, Any], dollars_key: str, cents_key: str) -> float | None:
    dollars = raw.get(dollars_key)
    if dollars not in (None, ""):
        return safe_float(dollars)
    cents = safe_float(raw.get(cents_key))
    return (cents / 100.0) if cents is not None else None


def _build_kalshi(snapshot: MarketSnapshot, settings: Settings) -> SportsMarket | None:
    raw = snapshot.raw
    title = str(raw.get("title") or raw.get("event_title") or snapshot.market_id)
    description = " ".join(
        part
        for part in [
            str(raw.get("rules_primary") or ""),
            str(raw.get("rules_secondary") or ""),
            str(raw.get("event_sub_title") or ""),
            str(raw.get("event_category") or ""),
        ]
        if part
    )
    raw_yes_label = str(raw.get("yes_sub_title") or raw.get("yesLabel") or "Yes")
    raw_no_label = str(raw.get("no_sub_title") or raw.get("noLabel") or "No")
    yes_label = raw_yes_label
    no_label = raw_no_label
    if canonicalize_label(raw_yes_label) == canonicalize_label(raw_no_label):
        yes_label = "Yes"
        no_label = "No"
    outcome_labels = [yes_label, no_label]
    sport_key, is_esports = _detect_sport_key(
        title,
        description,
        str(raw.get("event_ticker") or ""),
        str(raw.get("event_category") or ""),
        str(raw.get("event_series_ticker") or ""),
        str(raw.get("event_title") or ""),
    )
    if sport_key == "other":
        return None
    participants = _extract_kalshi_participants(title, description, raw_yes_label, raw_no_label)
    rough_family = _market_family(title, description, outcome_labels, "match")
    event_class = _event_class(title, description, rough_family, participants)
    market_family = _market_family(title, description, outcome_labels, event_class)
    period_scope, map_no, round_no = _extract_period_scope(f"{title} {description}")
    line_value = _extract_line_value(f"{title} {' '.join(outcome_labels)}") if market_family in {"spread", "total", "handicap"} else None
    best_of = _extract_best_of(f"{title} {description}")
    start_time = _parse_kalshi_date(raw, description) or parse_datetime(str(raw.get("event_strike_date") or ""))

    yes_quote = _top_of_book_quote(
        outcome_id=f"{snapshot.market_id}:yes",
        label=yes_label,
        best_bid=_kalshi_price(raw, "yes_bid_dollars", "yes_bid"),
        best_ask=_kalshi_price(raw, "yes_ask_dollars", "yes_ask"),
        payout_size=settings.quote_size_usd,
        fee_bps=settings.kalshi_fee_bps,
        slippage_bps=settings.kalshi_slippage_bps,
        max_fill_usd=safe_float(raw.get("yes_ask_size_fp"), settings.quote_size_usd) or settings.quote_size_usd,
        fetched_at=snapshot.fetched_at,
        metadata={"ticker": raw.get("ticker"), "side": "yes"},
    )
    no_quote = _top_of_book_quote(
        outcome_id=f"{snapshot.market_id}:no",
        label=no_label,
        best_bid=_kalshi_price(raw, "no_bid_dollars", "no_bid"),
        best_ask=_kalshi_price(raw, "no_ask_dollars", "no_ask"),
        payout_size=settings.quote_size_usd,
        fee_bps=settings.kalshi_fee_bps,
        slippage_bps=settings.kalshi_slippage_bps,
        max_fill_usd=safe_float(raw.get("no_ask_size_fp"), settings.quote_size_usd) or settings.quote_size_usd,
        fetched_at=snapshot.fetched_at,
        metadata={"ticker": raw.get("ticker"), "side": "no"},
    )

    participants_sorted = tuple(sorted(_canonical_participant(value) for value in participants if value))
    competition_key = _kalshi_competition_key(raw, title, description)
    season_key = _extract_season_key(f"{title} {description}")
    features = _attach_source_metadata(_sport_market_features(
        title,
        description,
        participants,
        competition_key,
        start_time,
        sport_key,
        event_class,
        season_key,
        {"event_ticker": str(raw.get("event_ticker") or "")},
    ), snapshot)
    resolution_profile = build_sports_resolution_profile(
        title=title,
        description=description,
        raw=raw,
        event_class=event_class,
        market_family=market_family,
        period_scope=period_scope,
        line_value=line_value,
        line_side=None,
        participants=[value for value in participants if value],
        start_time=start_time,
    )
    features["resolution_profile_signature"] = resolution_profile_signature(resolution_profile)
    return SportsMarket(
        exchange="kalshi",
        market_id=snapshot.market_id,
        title=title,
        description=description,
        event_class=event_class,
        match_lane=event_class,
        sport_key=sport_key,
        competition_key=competition_key,
        season_key=season_key,
        is_esports=is_esports,
        participants_home=participants[0],
        participants_away=participants[1],
        participants_sorted=participants_sorted,
        start_time=start_time,
        best_of=best_of,
        period_scope=period_scope,
        map_no=map_no,
        round_no=round_no,
        market_family=market_family,
        line_value=line_value,
        line_side=None,
        auto_arb_enabled=_auto_arb_enabled(settings, market_family, event_class, outcome_labels),
        chain="kalshi",
        token="USD",
        outcomes=[
            SportsOutcome(
                outcome_id=f"{snapshot.market_id}:yes",
                label=yes_label,
                canonical_label=canonicalize_label(yes_label),
                selection_key=_selection_key(market_family, yes_label, line_value, _line_side(yes_label, participants)),
                quote=yes_quote,
            ),
            SportsOutcome(
                outcome_id=f"{snapshot.market_id}:no",
                label=no_label,
                canonical_label=canonicalize_label(no_label),
                selection_key=_selection_key(market_family, no_label, line_value, _line_side(no_label, participants)),
                quote=no_quote,
            ),
        ],
        volume_usd=safe_float(raw.get("volume_fp"), safe_float(raw.get("volume"), 0.0)) or 0.0,
        liquidity_usd=safe_float(raw.get("liquidity_dollars"), safe_float(raw.get("liquidity"), 0.0)) or 0.0,
        fetched_at=snapshot.fetched_at,
        raw_data=raw,
        resolution_profile=resolution_profile,
        features=features,
    )


def normalize_sports_snapshots(snapshots: list[MarketSnapshot], settings: Settings) -> list[SportsMarket]:
    normalized: list[SportsMarket] = []
    for snapshot in snapshots:
        if snapshot.exchange == "polymarket":
            market = _build_polymarket(snapshot, settings)
        elif snapshot.exchange == "azuro":
            market = _build_azuro(snapshot, settings)
        elif snapshot.exchange == "opn":
            market = _build_opn(snapshot, settings)
        elif snapshot.exchange == "kalshi":
            market = _build_kalshi(snapshot, settings)
        elif snapshot.exchange == "myriad":
            market = _build_myriad(snapshot, settings)
        else:
            market = None
        if market is not None:
            normalized.append(market)
    return normalized
