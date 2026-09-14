from __future__ import annotations

from datetime import datetime
from typing import Any
import re

from .features import detect_category_from_text, split_tags
from .utils import parse_datetime, safe_float


USDT_ADDRESSES = {
    "0x55d398326f99059ff775485246999027b3197955": "USDT",
}


def _resolution_url_from_text(*values: str) -> str | None:
    for value in values:
        match = re.search(r"https?://[^\s)]+", value)
        if match:
            return match.group(0).rstrip(".,)")
    return None


def _first_non_empty(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def extract_opn_description(raw: dict[str, Any]) -> str:
    return _first_non_empty(raw.get("description"), raw.get("subtitle"), raw.get("abstract"), raw.get("content"), raw.get("rules"))


def extract_opn_title(raw: dict[str, Any]) -> str:
    return _first_non_empty(raw.get("marketTitle"), raw.get("question"), raw.get("title"), raw.get("name"), raw.get("slug"))


def extract_opn_tags(raw: dict[str, Any]) -> list[str]:
    label_names = [str(value) for value in raw.get("labelName", []) if value]
    tags = [
        *[str(value) for value in raw.get("tags", []) if value],
        str(raw.get("topic") or ""),
        str(raw.get("series") or ""),
        str(raw.get("subcategory") or ""),
        *label_names,
        str(raw.get("collectionMarketDetail", {}).get("collection", {}).get("title") or ""),
        str(raw.get("collectionMarketDetail", {}).get("collection", {}).get("titleShort") or ""),
    ]
    return split_tags(tags)


def extract_opn_category(raw: dict[str, Any], title: str, description: str) -> str:
    explicit = _first_non_empty(
        raw.get("category"),
        raw.get("group"),
        *(str(value) for value in raw.get("labelName", []) if value),
    )
    return explicit or detect_category_from_text(title, description)


def extract_opn_closes_at(raw: dict[str, Any]) -> datetime | None:
    current = raw.get("collectionMarketDetail", {}).get("current", {})
    return (
        parse_datetime(str(raw.get("closesAt") or ""))
        or parse_datetime(str(raw.get("endDate") or ""))
        or parse_datetime(str(raw.get("endTime") or ""))
        or parse_datetime(str(raw.get("expiresAt") or ""))
        or parse_datetime(str(raw.get("cutoffAt") or ""))
        or parse_datetime(str(raw.get("cutoffTime") or ""))
        or parse_datetime(str(current.get("endTime") or ""))
    )


def extract_opn_resolves_at(raw: dict[str, Any], closes_at: datetime | None) -> datetime | None:
    return (
        parse_datetime(str(raw.get("resolvesAt") or ""))
        or parse_datetime(str(raw.get("resolveTime") or ""))
        or parse_datetime(str(raw.get("resolvedTime") or ""))
        or closes_at
    )


def extract_opn_resolution_source(raw: dict[str, Any], description: str) -> str | None:
    return (
        _first_non_empty(raw.get("resolutionSource"), raw.get("oracleUrl"), raw.get("resultUrl")) or
        _resolution_url_from_text(description, str(raw.get("rules") or ""))
    ) or None


def extract_opn_outcomes(raw: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes_payload = raw.get("outcomes") if isinstance(raw.get("outcomes"), list) else []
    if outcomes_payload:
        outcomes: list[dict[str, Any]] = []
        for index, outcome in enumerate(outcomes_payload):
            if not isinstance(outcome, dict):
                continue
            outcomes.append(
                {
                    "outcome_id": str(outcome.get("id") or outcome.get("outcomeId") or index),
                    "label": _first_non_empty(outcome.get("title"), outcome.get("label"), outcome.get("name"), f"Outcome {index + 1}"),
                    "price": (
                        safe_float(outcome.get("price"))
                        if outcome.get("price") not in (None, "")
                        else safe_float(outcome.get("buyPrice"))
                    )
                    if outcome.get("price") not in (None, "") or outcome.get("buyPrice") not in (None, "")
                    else safe_float(outcome.get("probability")) or safe_float(outcome.get("lastPrice")),
                }
            )
        if outcomes:
            return outcomes

    child_list = raw.get("childList") if isinstance(raw.get("childList"), list) else []
    if child_list:
        outcomes = []
        for index, child in enumerate(child_list):
            if not isinstance(child, dict):
                continue
            outcomes.append(
                {
                    "outcome_id": str(child.get("topicId") or child.get("id") or child.get("slug") or index),
                    "label": _first_non_empty(child.get("title"), child.get("yesLabel"), child.get("slug"), f"Outcome {index + 1}"),
                    "price": (
                        safe_float(child.get("yesBuyPrice"))
                        if child.get("yesBuyPrice") not in (None, "")
                        else safe_float(child.get("yesMarketPrice"))
                    ),
                }
            )
        if outcomes:
            return outcomes

    outcomes = []
    for side, default_label in (("yes", "Yes"), ("no", "No")):
        label = _first_non_empty(raw.get(f"{side}Label"), default_label)
        buy_price = raw.get(f"{side}BuyPrice")
        market_price = raw.get(f"{side}MarketPrice")
        if label:
            outcomes.append(
                {
                    "outcome_id": f"{raw.get('topicId') or raw.get('id') or raw.get('slug') or 'market'}:{side}",
                    "label": label,
                    "price": safe_float(buy_price) if buy_price not in (None, "") else safe_float(market_price),
                }
            )
    if outcomes:
        return outcomes

    options = raw.get("options") if isinstance(raw.get("options"), list) else []
    return [
        {
            "outcome_id": f"{raw.get('topicId') or raw.get('id') or raw.get('slug') or 'market'}:{index}",
            "label": str(option),
            "price": None,
        }
        for index, option in enumerate(options)
        if option
    ]


def extract_opn_chain(raw: dict[str, Any]) -> str:
    chain_value = _first_non_empty(raw.get("chain"), raw.get("network"), raw.get("chainId"))
    mapping = {"56": "bsc", "bsc": "bsc"}
    return mapping.get(chain_value.lower(), chain_value or "unknown")


def extract_opn_token(raw: dict[str, Any]) -> str:
    token = _first_non_empty(raw.get("token"), raw.get("collateralSymbol"))
    if token:
        return token
    address = str(raw.get("currencyAddress") or "").lower()
    return USDT_ADDRESSES.get(address, "USDC")


def extract_opn_liquidity(raw: dict[str, Any]) -> float:
    for key in ("liquidity", "totalPrice", "volume"):
        value = safe_float(raw.get(key))
        if value is not None:
            return value
    return 0.0


def extract_opn_volume(raw: dict[str, Any]) -> float:
    for key in ("volume", "volume24h", "totalPrice"):
        value = safe_float(raw.get(key))
        if value is not None:
            return value
    return 0.0
