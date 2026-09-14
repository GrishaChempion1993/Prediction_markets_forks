from __future__ import annotations

from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
import math
import re

from .utils import normalize_spaces

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "be",
    "before",
    "by",
    "for",
    "from",
    "if",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "vs",
    "will",
    "with",
}

EVENT_TITLE_STOPWORDS = STOPWORDS | {
    "beat",
    "beats",
    "game",
    "games",
    "half",
    "handicap",
    "map",
    "maps",
    "market",
    "markets",
    "match",
    "matches",
    "period",
    "points",
    "prop",
    "props",
    "round",
    "spread",
    "team",
    "total",
    "totals",
    "winner",
    "winners",
    "wins",
    "who",
    "win",
    "their",
}

EVENT_TIME_STOPWORDS = {
    "am",
    "pm",
    "utc",
    "et",
    "gmt",
    "jan",
    "january",
    "feb",
    "february",
    "mar",
    "march",
    "apr",
    "april",
    "may",
    "jun",
    "june",
    "jul",
    "july",
    "aug",
    "august",
    "sep",
    "sept",
    "september",
    "oct",
    "october",
    "nov",
    "november",
    "dec",
    "december",
}

SUBJECT_CONTEXT_VERBS = (
    "win",
    "be",
    "get",
    "reach",
    "finish",
    "announce",
    "capture",
    "qualify",
    "miss",
    "take",
    "have",
    "beat",
    "become",
    "launch",
    "release",
    "invade",
    "claim",
    "hold",
    "return",
    "declare",
)

ALIASES = {
    "btc": "bitcoin",
    "xbt": "bitcoin",
    "eth": "ethereum",
    "donald trump": "trump",
    "donald": "trump",
    "los angeles lakers": "lakers",
    "la lakers": "lakers",
}

SYNONYM_LABELS = {
    "yes": "yes",
    "no": "no",
    "draw": "draw",
    "other": "other",
    "misc": "other",
}


def normalize_text(value: str) -> str:
    text = value.lower()
    text = re.sub(r"[^a-z0-9$%.\s/-]", " ", text)
    return normalize_spaces(text)


def tokenize(value: str) -> list[str]:
    normalized = normalize_text(value)
    return [token for token in normalized.split(" ") if token and token not in STOPWORDS]


def event_title_tokens(value: str, participants: list[str] | None = None) -> set[str]:
    title = re.sub(r"\([^)]*\)", " ", value)
    title = re.sub(r"\b(?:1h|2h|1st|2nd|3rd|4th)\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\b\d+(?:\.\d+)?\b", " ", title)
    tokens = []
    for token in normalize_text(title).split(" "):
        if not token or token in EVENT_TITLE_STOPWORDS or token in EVENT_TIME_STOPWORDS:
            continue
        if len(token) <= 1:
            continue
        tokens.append(ALIASES.get(token, token))
    for participant in participants or []:
        for token in tokenize(participant):
            if token not in EVENT_TITLE_STOPWORDS and token not in EVENT_TIME_STOPWORDS:
                tokens.append(ALIASES.get(token, token))
    return set(tokens)


def event_title_key(value: str, participants: list[str] | None = None) -> str:
    return " ".join(sorted(event_title_tokens(value, participants)))


def canonicalize_label(value: str) -> str:
    normalized = normalize_text(value)
    if normalized in SYNONYM_LABELS:
        return SYNONYM_LABELS[normalized]
    return ALIASES.get(normalized, normalized)


def is_binary_yes_no(outcome_labels: list[str]) -> bool:
    if len(outcome_labels) != 2:
        return False
    return {canonicalize_label(label) for label in outcome_labels} == {"yes", "no"}


def fuzzy_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, canonicalize_label(left), canonicalize_label(right)).ratio()


def extract_subject_context(
    title: str,
    description: str,
    tags: list[str],
    outcome_labels: list[str],
) -> tuple[str | None, str | None, str | None]:
    cleaned_title = normalize_spaces(title.strip().rstrip("?"))
    quoted_subject = cleaned_title.strip("\"' ")

    if len(outcome_labels) == 2:
        canonical_outcomes = [canonicalize_label(label) for label in outcome_labels]
        if canonical_outcomes[1] == "other" and canonical_outcomes[0] != "other":
            context_source = description or " ".join(
                tag
                for tag in tags
                if canonicalize_label(tag) not in {canonicalize_label(outcome_labels[0]), "other"}
            )
            return (
                canonicalize_label(outcome_labels[0].strip("\"' ")),
                normalize_text(context_source),
                "entity_vs_field",
            )

    if is_binary_yes_no(outcome_labels):
        pattern = re.compile(
            rf"(?i)^will\s+(.+?)\s+({'|'.join(re.escape(verb) for verb in SUBJECT_CONTEXT_VERBS)})\b(.+)$"
        )
        match = pattern.match(cleaned_title)
        if match:
            subject = canonicalize_label(match.group(1).strip("\"' "))
            verb = normalize_text(match.group(2))
            remainder = normalize_text(match.group(3))
            context = normalize_spaces(f"{verb} {remainder}".strip())
            if subject and context:
                return subject, context, "binary_subject"

        if quoted_subject and description:
            return canonicalize_label(quoted_subject), normalize_text(description), "binary_subject"

    return None, None, None


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def overlap_coefficient(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def extract_numbers(value: str) -> set[str]:
    numbers: set[str] = set()
    pattern = re.compile(r"\$?\d[\d,]*(?:\.\d+)?(?:k|m|b)?")
    for match in pattern.findall(value.lower()):
        candidate = match.replace("$", "").replace(",", "").strip()
        if not candidate:
            continue
        suffix = candidate[-1] if candidate[-1] in {"k", "m", "b"} else ""
        numeric = candidate[:-1] if suffix else candidate
        try:
            amount = float(numeric)
        except ValueError:
            continue
        multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix, 1)
        normalized = str(int(amount * multiplier))
        numbers.add(normalized)
    return numbers


def extract_keywords(value: str) -> set[str]:
    tokens = tokenize(value)
    return {ALIASES.get(token, token) for token in tokens if len(token) > 2}


def extract_entities(value: str, extra_terms: list[str] | None = None) -> set[str]:
    entities = extract_keywords(value)
    for term in extra_terms or []:
        entities.add(canonicalize_label(term))
    return {entity for entity in entities if entity not in STOPWORDS}


def category_family(value: str) -> str:
    candidate = normalize_text(value)
    if any(token in candidate for token in ("sport", "boxing", "soccer", "football", "nba", "ufc", "tennis")):
        return "sports"
    if any(token in candidate for token in ("crypto", "bitcoin", "ethereum", "solana")):
        return "crypto"
    if any(token in candidate for token in ("politic", "election", "senate", "president")):
        return "politics"
    if any(token in candidate for token in ("econom", "fed", "inflation", "gdp")):
        return "economics"
    return "other"


def detect_category_from_text(title: str, description: str) -> str:
    combined = f"{title} {description}".lower()
    if any(term in combined for term in ("bitcoin", "btc", "ethereum", "eth", "crypto")):
        return "crypto"
    if any(term in combined for term in ("election", "president", "senate", "trump", "biden")):
        return "politics"
    if any(term in combined for term in ("lakers", "boxing", "match", "fight", "championship", "league")):
        return "sports"
    return "other"


def split_tags(values: list[str] | None) -> list[str]:
    return [normalize_spaces(value) for value in values or [] if value]


def classify_market_type(outcome_labels: list[str]) -> str:
    return "binary" if len(outcome_labels) == 2 else "multi_outcome"


def is_championship_like(title: str, description: str) -> bool:
    combined = normalize_text(f"{title} {description}")
    markers = ("championship", "winner", "title", "cup", "season", "tournament", "league winner")
    return any(marker in combined for marker in markers)


def classify_event_kind(
    category: str,
    title: str,
    description: str,
    outcome_labels: list[str],
    tags: list[str],
) -> str:
    family = category_family(category)
    if family == "sports":
        return "sports_futures" if is_championship_like(title, description) else "sports_match"
    if len(outcome_labels) > 2:
        return "multi_outcome_generic"
    combined = normalize_text(f"{title} {description} {' '.join(tags)}")
    if "next move" in combined or "reach" in combined or "hit" in combined:
        return "price_target"
    return "binary_generic"


def extract_participants_from_title(title: str) -> list[str]:
    for separator in (" - ", " vs ", " v ", " at ", " @ "):
        if separator in title:
            parts = [normalize_spaces(part) for part in title.split(separator) if part.strip()]
            if len(parts) >= 2:
                return parts[:2]
    return []


def date_proximity_score(left: datetime | None, right: datetime | None) -> float:
    if left is None or right is None:
        return 0.5
    delta_seconds = abs((left - right).total_seconds())
    if delta_seconds == 0:
        return 1.0
    if delta_seconds < 3_600:
        return 0.95
    if delta_seconds < 86_400:
        return 0.8
    if delta_seconds < 7 * 86_400:
        return 0.5
    if delta_seconds < 30 * 86_400:
        return 0.2
    return 0.0


def cosine_similarity(left_tokens: Counter[str], right_tokens: Counter[str], idf: dict[str, float]) -> float:
    left_weights = {token: count * idf.get(token, 0.0) for token, count in left_tokens.items()}
    right_weights = {token: count * idf.get(token, 0.0) for token, count in right_tokens.items()}
    numerator = sum(
        left_weights.get(token, 0.0) * right_weights.get(token, 0.0)
        for token in set(left_weights) | set(right_weights)
    )
    left_norm = math.sqrt(sum(weight * weight for weight in left_weights.values()))
    right_norm = math.sqrt(sum(weight * weight for weight in right_weights.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)
