from __future__ import annotations

from math import isfinite
from typing import Any
import json

from .config import Settings
from .features import (
    canonicalize_label,
    category_family,
    classify_event_kind,
    classify_market_type,
    detect_category_from_text,
    event_title_key,
    event_title_tokens,
    extract_entities,
    extract_subject_context,
    extract_participants_from_title,
    extract_numbers,
    is_championship_like,
    split_tags,
)
from .models import MarketSnapshot, NormalizedMarket, NormalizedOutcome, OutcomeQuote
from .opn_adapter import (
    extract_opn_category,
    extract_opn_chain,
    extract_opn_closes_at,
    extract_opn_description,
    extract_opn_liquidity,
    extract_opn_outcomes,
    extract_opn_resolution_source,
    extract_opn_resolves_at,
    extract_opn_tags,
    extract_opn_token,
    extract_opn_title,
    extract_opn_volume,
)
from .resolve import build_generic_resolution_profile
from .utils import host_from_url, parse_datetime, safe_float, text_hash


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
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
    quote_type: str,
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
        quote_type=quote_type,
        best_bid=best_bid,
        best_ask=price,
        quoted_cost_for_size=quoted_cost,
        fee_bps=fee_bps,
        slippage_bps_est=slippage_bps,
        max_fill_usd=max_fill_usd,
        quote_timestamp=fetched_at,
        metadata=metadata or {},
    )


def _empty_quote(
    outcome_id: str,
    label: str,
    quote_type: str,
    fee_bps: int,
    slippage_bps: int,
    fetched_at,
    metadata: dict[str, Any] | None = None,
) -> OutcomeQuote:
    return OutcomeQuote(
        outcome_id=outcome_id,
        label=label,
        quote_type=quote_type,
        best_bid=None,
        best_ask=None,
        quoted_cost_for_size=None,
        fee_bps=fee_bps,
        slippage_bps_est=slippage_bps,
        max_fill_usd=0.0,
        quote_timestamp=fetched_at,
        metadata=metadata or {},
    )


def _top_of_book_quote(
    outcome_id: str,
    label: str,
    quote_type: str,
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
        quote_type=quote_type,
        best_bid=best_bid,
        best_ask=best_ask,
        quoted_cost_for_size=quoted_cost,
        fee_bps=fee_bps,
        slippage_bps_est=slippage_bps,
        max_fill_usd=max_fill_usd,
        quote_timestamp=fetched_at,
        metadata=metadata or {},
    )


def _build_common_features(
    title: str,
    description: str,
    tags: list[str],
    outcome_labels: list[str],
    closes_at,
    category: str,
    participants: list[str] | None = None,
) -> dict[str, Any]:
    participants = participants or extract_participants_from_title(title)
    entities = extract_entities(" ".join([title, description, *tags, *participants]))
    numbers = extract_numbers(f"{title} {description}")
    subject_entity, context_key, market_lane = extract_subject_context(title, description, tags, outcome_labels)
    if subject_entity:
        entities.add(subject_entity)
    return {
        "title_norm": canonicalize_label(title),
        "description_norm": canonicalize_label(description),
        "event_title_key": event_title_key(title, participants),
        "event_title_tokens": sorted(event_title_tokens(title, participants)),
        "entities": sorted(entities),
        "numbers": sorted(numbers),
        "participants": participants,
        "has_start_time": closes_at is not None,
        "date_bucket": closes_at.date().isoformat() if closes_at else None,
        "outcome_labels": [canonicalize_label(label) for label in outcome_labels],
        "championship_like": is_championship_like(title, description),
        "category_family": category_family(category),
        "subject_entity": subject_entity,
        "context_key": context_key,
        "market_lane": market_lane or "generic",
    }


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


def _normalize_polymarket(snapshot: MarketSnapshot, settings: Settings) -> NormalizedMarket:
    raw = snapshot.raw
    title = str(raw.get("question") or raw.get("title") or snapshot.market_id)
    description = str(raw.get("description") or "")
    outcome_labels = [str(item) for item in _as_list(raw.get("outcomes"))]
    outcome_prices = [safe_float(item) for item in _as_list(raw.get("outcomePrices"))]
    clob_token_ids = [str(item) for item in _as_list(raw.get("clobTokenIds"))]
    order_books = raw.get("orderBooks") if isinstance(raw.get("orderBooks"), dict) else {}
    category = detect_category_from_text(title, description)
    tags = split_tags(list(extract_entities(title + " " + description)))

    outcomes: list[NormalizedOutcome] = []
    for index, label in enumerate(outcome_labels):
        token_id = clob_token_ids[index] if index < len(clob_token_ids) else f"{snapshot.market_id}:{index}"
        book = order_books.get(token_id) if isinstance(order_books, dict) else None
        price = outcome_prices[index] if index < len(outcome_prices) else None
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
                quote_type="clob_top_of_book_fallback",
                price=price,
                payout_size=settings.quote_size_usd,
                fee_bps=settings.polymarket_fee_bps,
                slippage_bps=0,
                max_fill_usd=safe_float(raw.get("liquidityNum"), settings.quote_size_usd) or settings.quote_size_usd,
                fetched_at=snapshot.fetched_at,
                metadata={"token_id": token_id},
            )
        outcomes.append(
            NormalizedOutcome(
                outcome_id=token_id,
                label=label,
                canonical_label=canonicalize_label(label),
                quote=quote,
            )
        )

    resolution_source_url = str(raw.get("resolutionSource") or "") or None
    closes_at = parse_datetime(str(raw.get("endDate") or raw.get("endDateIso") or "")) or parse_datetime(
        str(raw.get("acceptingOrdersTimestamp") or "")
    )
    rules_hash = text_hash(f"{description}|{resolution_source_url or ''}")
    features = _attach_source_metadata(_build_common_features(title, description, tags, outcome_labels, closes_at, category), snapshot)
    resolution_profile = build_generic_resolution_profile(
        title=title,
        description=description,
        category_family=category_family(category),
        market_type=classify_market_type(outcome_labels),
        outcome_labels=outcome_labels,
        raw=raw,
        closes_at=closes_at,
        resolves_at=closes_at,
        resolution_source_url=resolution_source_url,
        features=features,
    )

    return NormalizedMarket(
        exchange="polymarket",
        market_id=snapshot.market_id,
        market_type=classify_market_type(outcome_labels),
        event_kind=classify_event_kind(category, title, description, outcome_labels, tags),
        title=title,
        description=description,
        category=category,
        category_family=category_family(category),
        tags=tags,
        chain="polygon",
        token="USDC",
        closes_at=closes_at,
        resolves_at=closes_at,
        resolution_source_url=resolution_source_url,
        resolution_rules_hash=rules_hash,
        outcomes=outcomes,
        volume_usd=safe_float(raw.get("volumeNum"), 0.0) or 0.0,
        liquidity_usd=safe_float(raw.get("liquidityNum"), 0.0) or 0.0,
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


def _normalize_azuro(snapshot: MarketSnapshot, settings: Settings) -> NormalizedMarket:
    raw = snapshot.raw
    environment = str(raw.get("environment") or "unknown")
    game = dict(raw.get("game") or {})
    condition = dict(raw.get("condition") or {})
    chain, token = _azuro_chain(environment)
    title = str(condition.get("title") or game.get("title") or snapshot.market_id)
    description = ""
    outcome_labels = _azuro_outcome_labels(game, condition)
    category = str(game.get("sport", {}).get("name") or "sports")
    tags = split_tags(
        [
            str(game.get("sport", {}).get("slug") or ""),
            str(game.get("league", {}).get("slug") or ""),
            str(game.get("country", {}).get("slug") or ""),
            *[str(item.get("name") or "") for item in game.get("participants", [])],
        ]
    )
    turnover = safe_float(game.get("turnover"), 0.0) or 0.0
    margin = int((safe_float(condition.get("margin"), 0.0) or 0.0) * 100)
    closes_at = parse_datetime(str(game.get("startsAt") or ""))
    rules_hash = text_hash(f"{title}|{category}|{environment}")
    features = _attach_source_metadata(_build_common_features(
        title=title,
        description=description,
        tags=tags,
        outcome_labels=outcome_labels,
        closes_at=closes_at,
        category=category,
        participants=[str(item.get("name")) for item in game.get("participants", []) if item.get("name")],
    ), snapshot)
    features.update({"league": str(game.get("league", {}).get("name") or ""), "environment": environment})
    resolution_profile = build_generic_resolution_profile(
        title=title,
        description=description,
        category_family=category_family(category),
        market_type=classify_market_type(outcome_labels),
        outcome_labels=outcome_labels,
        raw=raw,
        closes_at=closes_at,
        resolves_at=closes_at,
        resolution_source_url=None,
        features=features,
    )

    outcomes: list[NormalizedOutcome] = []
    for index, outcome in enumerate(condition.get("outcomes", [])):
        label = outcome_labels[index] if index < len(outcome_labels) else f"Outcome {index + 1}"
        odds = safe_float(outcome.get("odds"))
        price = (1 / odds) if odds and odds > 0 else None
        slippage_bps = settings.azuro_slippage_bps + margin
        quote = _fallback_quote(
            outcome_id=str(outcome.get("outcomeId") or index),
            label=label,
            quote_type="amm_implied_probability",
            price=price,
            payout_size=settings.quote_size_usd,
            fee_bps=0,
            slippage_bps=slippage_bps,
            max_fill_usd=max(100.0, turnover * 0.05),
            fetched_at=snapshot.fetched_at,
            metadata={"odds": odds, "environment": environment},
        )
        outcomes.append(
            NormalizedOutcome(
                outcome_id=str(outcome.get("outcomeId") or index),
                label=label,
                canonical_label=canonicalize_label(label),
                quote=quote,
            )
        )

    return NormalizedMarket(
        exchange="azuro",
        market_id=snapshot.market_id,
        market_type=classify_market_type(outcome_labels),
        event_kind=classify_event_kind(category, title, description, outcome_labels, tags),
        title=title,
        description=description,
        category=category,
        category_family=category_family(category),
        tags=tags,
        chain=chain,
        token=token,
        closes_at=closes_at,
        resolves_at=closes_at,
        resolution_source_url=None,
        resolution_rules_hash=rules_hash,
        outcomes=outcomes,
        volume_usd=turnover,
        liquidity_usd=turnover,
        fetched_at=snapshot.fetched_at,
        raw_data=raw,
        resolution_profile=resolution_profile,
        features=features,
    )


def _myriad_chain(network_id: object) -> str:
    mapping = {274133: "abstract", "274133": "abstract"}
    return mapping.get(network_id, f"network_{network_id}")


def _normalize_opn(snapshot: MarketSnapshot, settings: Settings) -> NormalizedMarket:
    raw = snapshot.raw
    title = extract_opn_title(raw) or snapshot.market_id
    description = extract_opn_description(raw)
    category = extract_opn_category(raw, title, description)
    tags = extract_opn_tags(raw)
    closes_at = extract_opn_closes_at(raw)
    resolves_at = extract_opn_resolves_at(raw, closes_at)
    resolution_source_url = extract_opn_resolution_source(raw, description)
    rules_hash = text_hash(f"{description}|{raw.get('rules') or ''}|{resolution_source_url or ''}")

    outcome_specs = extract_opn_outcomes(raw)
    outcome_labels = [str(item["label"]) for item in outcome_specs]

    features = _attach_source_metadata(_build_common_features(title, description, tags, outcome_labels, closes_at, category), snapshot)
    features["resolution_host"] = host_from_url(resolution_source_url)
    resolution_profile = build_generic_resolution_profile(
        title=title,
        description=description,
        category_family=category_family(category),
        market_type=classify_market_type(outcome_labels),
        outcome_labels=outcome_labels,
        raw=raw,
        closes_at=closes_at,
        resolves_at=resolves_at,
        resolution_source_url=resolution_source_url,
        features=features,
    )

    outcomes: list[NormalizedOutcome] = []
    for index, outcome in enumerate(outcome_specs):
        label = str(outcome["label"])
        price = safe_float(outcome.get("price"))
        outcome_id = str(outcome.get("outcome_id") or f"{snapshot.market_id}:{index}")
        quote = (
            _fallback_quote(
                outcome_id=outcome_id,
                label=label,
                quote_type="opn_price",
                price=price,
                payout_size=settings.quote_size_usd,
                fee_bps=settings.opn_fee_bps,
                slippage_bps=settings.opn_slippage_bps,
                max_fill_usd=max(extract_opn_liquidity(raw), settings.quote_size_usd),
                fetched_at=snapshot.fetched_at,
            )
            if price is not None
            else _empty_quote(
                outcome_id=outcome_id,
                label=label,
                quote_type="opn_missing_price",
                fee_bps=settings.opn_fee_bps,
                slippage_bps=settings.opn_slippage_bps,
                fetched_at=snapshot.fetched_at,
            )
        )
        outcomes.append(
            NormalizedOutcome(
                outcome_id=outcome_id,
                label=label,
                canonical_label=canonicalize_label(label),
                quote=quote,
            )
        )

    return NormalizedMarket(
        exchange="opn",
        market_id=snapshot.market_id,
        market_type=classify_market_type(outcome_labels),
        event_kind=classify_event_kind(category, title, description, outcome_labels, tags),
        title=title,
        description=description,
        category=category,
        category_family=category_family(category),
        tags=tags,
        chain=extract_opn_chain(raw),
        token=extract_opn_token(raw),
        closes_at=closes_at,
        resolves_at=resolves_at,
        resolution_source_url=resolution_source_url,
        resolution_rules_hash=rules_hash,
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


def _kalshi_outcome_labels(raw: dict[str, Any]) -> tuple[str, str, str, str]:
    display_yes = str(raw.get("yes_title") or raw.get("yesLabel") or raw.get("yes_sub_title") or "Yes").strip() or "Yes"
    display_no = str(raw.get("no_title") or raw.get("noLabel") or raw.get("no_sub_title") or "No").strip() or "No"
    if canonicalize_label(display_yes) == canonicalize_label(display_no):
        return "Yes", "No", display_yes, display_no
    return display_yes, display_no, display_yes, display_no


def _normalize_kalshi(snapshot: MarketSnapshot, settings: Settings) -> NormalizedMarket:
    raw = snapshot.raw
    title = str(raw.get("title") or raw.get("event_title") or snapshot.market_id)
    description = " ".join(
        part
        for part in [
            str(raw.get("rules_primary") or ""),
            str(raw.get("rules_secondary") or ""),
            str(raw.get("subtitle") or ""),
            str(raw.get("event_sub_title") or ""),
            str(raw.get("event_category") or ""),
        ]
        if part
    )
    yes_label, no_label, display_yes, display_no = _kalshi_outcome_labels(raw)
    outcome_labels = [yes_label, no_label]
    category_hint = " ".join(
        str(value or "")
        for value in [
            description,
            raw.get("event_ticker"),
            raw.get("event_category"),
            raw.get("event_series_ticker"),
            raw.get("event_title"),
        ]
    ).lower()
    category = "sports" if any(token in category_hint for token in ("basketball", "baseball", "soccer", "football", "hockey", "boxing", "mma", "nba", "nfl", "mlb", "nhl")) else detect_category_from_text(title, f"{description} {raw.get('event_ticker') or ''}")
    tags = split_tags(
        [
            str(raw.get("event_ticker") or ""),
            str(raw.get("market_type") or ""),
            str(raw.get("strike_type") or ""),
            display_yes,
            display_no,
        ]
    )
    closes_at = parse_datetime(str(raw.get("close_time") or raw.get("expiration_time") or ""))
    resolves_at = parse_datetime(str(raw.get("expiration_time") or raw.get("close_time") or ""))
    rules_hash = text_hash(f"{description}|{raw.get('event_ticker') or ''}|{raw.get('ticker') or ''}")
    features = _attach_source_metadata(_build_common_features(title, description, tags, outcome_labels, closes_at, category), snapshot)
    if display_yes:
        features["kalshi_display_yes"] = canonicalize_label(display_yes)
    if display_no:
        features["kalshi_display_no"] = canonicalize_label(display_no)
    resolution_profile = build_generic_resolution_profile(
        title=title,
        description=description,
        category_family=category_family(category),
        market_type="binary",
        outcome_labels=outcome_labels,
        raw=raw,
        closes_at=closes_at,
        resolves_at=resolves_at,
        resolution_source_url=None,
        features=features,
    )

    yes_quote = _top_of_book_quote(
        outcome_id=f"{snapshot.market_id}:yes",
        label=yes_label,
        quote_type="kalshi_top_of_book",
        best_bid=_kalshi_price(raw, "yes_bid_dollars", "yes_bid"),
        best_ask=_kalshi_price(raw, "yes_ask_dollars", "yes_ask"),
        payout_size=settings.quote_size_usd,
        fee_bps=settings.kalshi_fee_bps,
        slippage_bps=settings.kalshi_slippage_bps,
        max_fill_usd=safe_float(raw.get("yes_ask_size_fp"), settings.quote_size_usd) or settings.quote_size_usd,
        fetched_at=snapshot.fetched_at,
        metadata={"ticker": raw.get("ticker"), "side": "yes", "display_label": display_yes},
    )
    no_quote = _top_of_book_quote(
        outcome_id=f"{snapshot.market_id}:no",
        label=no_label,
        quote_type="kalshi_top_of_book",
        best_bid=_kalshi_price(raw, "no_bid_dollars", "no_bid"),
        best_ask=_kalshi_price(raw, "no_ask_dollars", "no_ask"),
        payout_size=settings.quote_size_usd,
        fee_bps=settings.kalshi_fee_bps,
        slippage_bps=settings.kalshi_slippage_bps,
        max_fill_usd=safe_float(raw.get("no_ask_size_fp"), settings.quote_size_usd) or settings.quote_size_usd,
        fetched_at=snapshot.fetched_at,
        metadata={"ticker": raw.get("ticker"), "side": "no", "display_label": display_no},
    )

    return NormalizedMarket(
        exchange="kalshi",
        market_id=snapshot.market_id,
        market_type="binary",
        event_kind=classify_event_kind(category, title, description, outcome_labels, tags),
        title=title,
        description=description,
        category=category,
        category_family=category_family(category),
        tags=tags,
        chain="kalshi",
        token="USD",
        closes_at=closes_at,
        resolves_at=resolves_at,
        resolution_source_url=None,
        resolution_rules_hash=rules_hash,
        outcomes=[
            NormalizedOutcome(
                outcome_id=f"{snapshot.market_id}:yes",
                label=yes_label,
                canonical_label=canonicalize_label(yes_label),
                quote=yes_quote,
            ),
            NormalizedOutcome(
                outcome_id=f"{snapshot.market_id}:no",
                label=no_label,
                canonical_label=canonicalize_label(no_label),
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


def _sxbet_order_quote(
    market_hash: str,
    label: str,
    orders: list[dict[str, Any]],
    settings: Settings,
    fetched_at,
) -> OutcomeQuote:
    prices: list[float] = []
    max_fill_units = 0.0
    for order in orders:
        percentage_odds = safe_float(order.get("percentageOdds"))
        if percentage_odds is None:
            continue
        prices.append(percentage_odds / 10_000_000_000_000_000_000)
        max_fill_units += (safe_float(order.get("totalBetSize"), 0.0) or 0.0) / 1_000_000
    if not prices:
        return _empty_quote(
            outcome_id=f"{market_hash}:{canonicalize_label(label)}",
            label=label,
            quote_type="sxbet_orderbook_missing",
            fee_bps=settings.sxbet_fee_bps,
            slippage_bps=settings.sxbet_slippage_bps,
            fetched_at=fetched_at,
        )
    best_ask = min(prices)
    return _fallback_quote(
        outcome_id=f"{market_hash}:{canonicalize_label(label)}",
        label=label,
        quote_type="sxbet_orders_probability",
        price=best_ask,
        payout_size=settings.quote_size_usd,
        fee_bps=settings.sxbet_fee_bps,
        slippage_bps=settings.sxbet_slippage_bps,
        max_fill_usd=max_fill_units,
        fetched_at=fetched_at,
        metadata={"orders": len(orders)},
    )


def _normalize_sxbet(snapshot: MarketSnapshot, settings: Settings) -> NormalizedMarket:
    raw = snapshot.raw
    title = str(raw.get("sportXeventId") or snapshot.market_id)
    team_one = str(raw.get("teamOneName") or raw.get("outcomeOneName") or "Outcome 1")
    team_two = str(raw.get("teamTwoName") or raw.get("outcomeTwoName") or "Outcome 2")
    sport_label = str(raw.get("sportLabel") or "other")
    league_label = str(raw.get("leagueLabel") or raw.get("group1") or "")
    description = league_label
    category = sport_label
    tags = split_tags(
        [
            sport_label,
            league_label,
            str(raw.get("group1") or ""),
            str(raw.get("group2") or ""),
            team_one,
            team_two,
        ]
    )
    closes_at = parse_datetime(str(raw.get("gameTime") or ""))
    features = _attach_source_metadata(_build_common_features(
        title=f"{team_one} vs {team_two}",
        description=description,
        tags=tags,
        outcome_labels=[team_one, team_two],
        closes_at=closes_at,
        category=category,
        participants=[team_one, team_two],
    ), snapshot)
    features.update(
        {
            "league": league_label,
            "sportx_event_id": str(raw.get("sportXeventId") or ""),
            "line": safe_float(raw.get("line")),
        }
    )
    if canonicalize_label(team_two) == "other":
        features["market_lane"] = "entity_vs_field"
        features["subject_entity"] = canonicalize_label(team_one)
        features["context_key"] = " ".join(
            part
            for part in [
                canonicalize_label(league_label) if league_label else "",
                canonicalize_label(description) if description else "",
            ]
            if part
        ).strip()

    orders_payload = raw.get("orders") if isinstance(raw.get("orders"), list) else []
    orders_one = [order for order in orders_payload if bool(order.get("isMakerBettingOutcomeOne"))]
    orders_two = [order for order in orders_payload if not bool(order.get("isMakerBettingOutcomeOne"))]
    market_hash = str(raw.get("marketHash") or snapshot.market_id)
    line_value = safe_float(raw.get("line"))
    quote_one = _sxbet_order_quote(market_hash, team_one, orders_one, settings, snapshot.fetched_at)
    quote_two = _sxbet_order_quote(market_hash, team_two, orders_two, settings, snapshot.fetched_at)
    resolution_profile = build_generic_resolution_profile(
        title=f"{team_one} vs {team_two}",
        description=description,
        category_family=category_family(category),
        market_type="binary",
        outcome_labels=[team_one, team_two],
        raw=raw,
        closes_at=closes_at,
        resolves_at=closes_at,
        resolution_source_url=None,
        features=features,
    )

    return NormalizedMarket(
        exchange="sxbet",
        market_id=snapshot.market_id,
        market_type="binary",
        event_kind=classify_event_kind(category, f"{team_one} vs {team_two}", description, [team_one, team_two], tags),
        title=f"{team_one} vs {team_two}",
        description=description,
        category=category,
        category_family=category_family(category),
        tags=tags,
        chain=str(raw.get("chainVersion") or "base"),
        token="USDC",
        closes_at=closes_at,
        resolves_at=closes_at,
        resolution_source_url=None,
        resolution_rules_hash=text_hash(f"{league_label}|{raw.get('type')}|{line_value}"),
        outcomes=[
            NormalizedOutcome(
                outcome_id=f"{market_hash}:1",
                label=team_one,
                canonical_label=canonicalize_label(team_one),
                quote=quote_one,
            ),
            NormalizedOutcome(
                outcome_id=f"{market_hash}:2",
                label=team_two,
                canonical_label=canonicalize_label(team_two),
                quote=quote_two,
            ),
        ],
        volume_usd=0.0,
        liquidity_usd=max(quote_one.max_fill_usd, quote_two.max_fill_usd),
        fetched_at=snapshot.fetched_at,
        raw_data=raw,
        resolution_profile=resolution_profile,
        features=features,
    )


def _normalize_myriad(snapshot: MarketSnapshot, settings: Settings) -> NormalizedMarket:
    raw = snapshot.raw
    title = str(raw.get("title") or snapshot.market_id)
    description = str(raw.get("description") or "")
    raw_category = str(raw.get("category") or "other")
    category = raw_category.split(";;", maxsplit=1)[0]
    topics = [str(item) for item in raw.get("topics", []) if item]
    tags = split_tags(topics)
    resolution_source_url = str(raw.get("resolutionSource") or "") or None
    rules_hash = text_hash(f"{description}|{resolution_source_url or ''}")
    closes_at = parse_datetime(str(raw.get("expiresAt") or raw.get("resolvesAt") or ""))
    outcomes_payload = raw.get("outcomes") if isinstance(raw.get("outcomes"), list) else []
    outcome_labels = [str(item.get("title") or f"Outcome {index + 1}") for index, item in enumerate(outcomes_payload)]
    fee_bps = int(
        (safe_float(raw.get("fees", {}).get("buy", {}).get("fee"), safe_float(raw.get("fee"), 0.0)) or 0.0) * 10_000
    )
    liquidity = safe_float(raw.get("liquidity"), 0.0) or 0.0
    token = str(raw.get("token", {}).get("symbol") or "USDC")
    chain = _myriad_chain(raw.get("networkId"))
    features = _attach_source_metadata(_build_common_features(title, description, tags, outcome_labels, closes_at, category), snapshot)
    features["resolution_host"] = host_from_url(resolution_source_url)
    resolution_profile = build_generic_resolution_profile(
        title=title,
        description=description,
        category_family=category_family(category),
        market_type=classify_market_type(outcome_labels),
        outcome_labels=outcome_labels,
        raw=raw,
        closes_at=closes_at,
        resolves_at=closes_at,
        resolution_source_url=resolution_source_url,
        features=features,
    )

    outcomes: list[NormalizedOutcome] = []
    for index, outcome in enumerate(outcomes_payload):
        label = outcome_labels[index]
        price = safe_float(outcome.get("price"))
        quote = _fallback_quote(
            outcome_id=str(outcome.get("id") or index),
            label=label,
            quote_type="amm_price",
            price=price,
            payout_size=settings.quote_size_usd,
            fee_bps=fee_bps or settings.myriad_default_fee_bps,
            slippage_bps=settings.myriad_slippage_bps,
            max_fill_usd=max(50.0, liquidity * 0.50),
            fetched_at=snapshot.fetched_at,
            metadata={"holders": outcome.get("holders"), "shares": outcome.get("shares")},
        )
        outcomes.append(
            NormalizedOutcome(
                outcome_id=str(outcome.get("id") or index),
                label=label,
                canonical_label=canonicalize_label(label),
                quote=quote,
            )
        )

    return NormalizedMarket(
        exchange="myriad",
        market_id=snapshot.market_id,
        market_type=classify_market_type(outcome_labels),
        event_kind=classify_event_kind(category, title, description, outcome_labels, tags),
        title=title,
        description=description,
        category=category,
        category_family=category_family(category),
        tags=tags,
        chain=chain,
        token=token,
        closes_at=closes_at,
        resolves_at=parse_datetime(str(raw.get("resolvesAt") or "")) or closes_at,
        resolution_source_url=resolution_source_url,
        resolution_rules_hash=rules_hash,
        outcomes=outcomes,
        volume_usd=safe_float(raw.get("volume"), 0.0) or 0.0,
        liquidity_usd=liquidity,
        fetched_at=snapshot.fetched_at,
        raw_data=raw,
        resolution_profile=resolution_profile,
        features=features,
    )


def normalize_snapshots(snapshots: list[MarketSnapshot], settings: Settings) -> list[NormalizedMarket]:
    normalized: list[NormalizedMarket] = []
    for snapshot in snapshots:
        if snapshot.exchange == "polymarket":
            normalized.append(_normalize_polymarket(snapshot, settings))
        elif snapshot.exchange == "opn":
            normalized.append(_normalize_opn(snapshot, settings))
        elif snapshot.exchange == "azuro":
            normalized.append(_normalize_azuro(snapshot, settings))
        elif snapshot.exchange == "sxbet":
            normalized.append(_normalize_sxbet(snapshot, settings))
        elif snapshot.exchange == "myriad":
            normalized.append(_normalize_myriad(snapshot, settings))
        elif snapshot.exchange == "kalshi":
            normalized.append(_normalize_kalshi(snapshot, settings))
    return normalized
