from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
import json
import math

from .config import Settings
from .features import cosine_similarity, date_proximity_score, fuzzy_similarity, jaccard, overlap_coefficient
from .models import CanonicalEvent, MatchedPair, NormalizedMarket
from .resolve import resolve_profile_compatibility
from .utils import host_from_url, text_hash


def _document_tokens(market: NormalizedMarket) -> Counter[str]:
    tokens = Counter()
    for token in market.features.get("entities", []):
        tokens[token] += 1
    for token in market.features.get("numbers", []):
        tokens[token] += 2
    for token in market.tags:
        if token:
            tokens[token.lower()] += 1
    for outcome in market.outcomes:
        tokens[outcome.canonical_label] += 1
    return tokens


@dataclass(slots=True)
class TfidfIndex:
    vectors: dict[str, Counter[str]]
    idf: dict[str, float]

    @classmethod
    def build(cls, markets: list[NormalizedMarket]) -> "TfidfIndex":
        vectors = {market.market_id: _document_tokens(market) for market in markets}
        document_frequency: Counter[str] = Counter()
        for vector in vectors.values():
            for token in vector:
                document_frequency[token] += 1
        total_docs = max(len(vectors), 1)
        idf = {token: math.log((1 + total_docs) / (1 + df)) + 1 for token, df in document_frequency.items()}
        return cls(vectors=vectors, idf=idf)

    def cosine(self, left: NormalizedMarket, right: NormalizedMarket) -> float:
        return cosine_similarity(self.vectors[left.market_id], self.vectors[right.market_id], self.idf)


class MarketMatcher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.manual_mappings = self._load_manual_mappings(settings.repo_root / settings.manual_mappings_path)

    @staticmethod
    def _load_manual_mappings(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, list) else []

    def match(self, markets: list[NormalizedMarket]) -> tuple[list[MatchedPair], list[CanonicalEvent]]:
        return self._match_internal(markets)

    def match_frontier(
        self,
        frontier_markets: list[NormalizedMarket],
        universe_markets: list[NormalizedMarket],
    ) -> tuple[list[MatchedPair], list[CanonicalEvent]]:
        frontier_keys = {(market.exchange, market.market_id) for market in frontier_markets}
        combined: dict[tuple[str, str], NormalizedMarket] = {}
        for market in universe_markets:
            combined[(market.exchange, market.market_id)] = market
        for market in frontier_markets:
            combined[(market.exchange, market.market_id)] = market
        return self._match_internal(list(combined.values()), frontier_keys=frontier_keys)

    def _match_internal(
        self,
        markets: list[NormalizedMarket],
        frontier_keys: set[tuple[str, str]] | None = None,
    ) -> tuple[list[MatchedPair], list[CanonicalEvent]]:
        matched_pairs: list[MatchedPair] = []
        market_index = {(market.exchange, market.market_id): market for market in markets}
        manual_keys: set[tuple[str, str]] = set()

        for mapping in self.manual_mappings:
            market_refs = mapping.get("markets", {})
            selected = []
            for exchange, market_id in market_refs.items():
                market = market_index.get((str(exchange), str(market_id)))
                if market:
                    selected.append(market)
            if len(selected) != 2:
                continue
            market_a, market_b = selected
            pair_identity = tuple(sorted((f"{market_a.exchange}:{market_a.market_id}", f"{market_b.exchange}:{market_b.market_id}")))
            if frontier_keys and not (
                (market_a.exchange, market_a.market_id) in frontier_keys
                or (market_b.exchange, market_b.market_id) in frontier_keys
            ):
                continue
            manual_keys.add(pair_identity)
            matched_pairs.append(
                MatchedPair(
                    market_a=market_a,
                    market_b=market_b,
                    confidence=1.0,
                    match_method="manual",
                    outcome_mapping={str(key): str(value) for key, value in mapping.get("outcome_mapping", {}).items()},
                    score_breakdown={"manual_override": 1.0},
                    veto_reasons=[],
                )
            )

        tfidf = TfidfIndex.build(markets)
        for left, right in self._iter_candidates(markets):
            pair_identity = tuple(sorted((f"{left.exchange}:{left.market_id}", f"{right.exchange}:{right.market_id}")))
            if frontier_keys and not (
                (left.exchange, left.market_id) in frontier_keys
                or (right.exchange, right.market_id) in frontier_keys
            ):
                continue
            if pair_identity in manual_keys:
                continue
            if not self._is_candidate(left, right, tfidf):
                continue
            vetoes = self._hard_vetoes(left, right)
            if vetoes:
                continue
            outcome_mapping = self._build_outcome_mapping(left, right)
            if not outcome_mapping:
                continue
            score_breakdown = self._score(left, right, outcome_mapping, tfidf)
            resolve_compatibility, _, _ = resolve_profile_compatibility(left.resolution_profile, right.resolution_profile)
            confidence = (
                score_breakdown["entities_numeric"] * 0.30
                + score_breakdown["resolution"] * 0.20
                + score_breakdown["fuzzy_text"] * 0.15
                + score_breakdown["date"] * 0.15
                + score_breakdown["tags_category"] * 0.10
                + score_breakdown["outcome_set"] * 0.10
            )
            if confidence < self.settings.min_match_confidence:
                continue
            matched_pairs.append(
                MatchedPair(
                    market_a=left,
                    market_b=right,
                    confidence=confidence,
                    match_method="automatic_weighted",
                    outcome_mapping=outcome_mapping,
                    score_breakdown=score_breakdown,
                    resolve_compatibility=resolve_compatibility,
                    veto_reasons=[],
                )
            )

        return matched_pairs, self._build_canonical_events(matched_pairs)

    def _iter_candidates(self, markets: list[NormalizedMarket]) -> list[tuple[NormalizedMarket, NormalizedMarket]]:
        entity_index: defaultdict[str, set[int]] = defaultdict(set)
        participant_index: defaultdict[str, set[int]] = defaultdict(set)
        tag_index: defaultdict[str, set[int]] = defaultdict(set)
        number_index: defaultdict[str, set[int]] = defaultdict(set)
        event_title_index: defaultdict[str, set[int]] = defaultdict(set)
        date_bucket_index: defaultdict[tuple[str, str], set[int]] = defaultdict(set)

        for index, market in enumerate(markets):
            for token in {str(value).lower() for value in market.features.get("entities", [])}:
                if len(token) >= 3:
                    entity_index[token].add(index)
            for token in {str(value).lower() for value in market.features.get("participants", [])}:
                if len(token) >= 3:
                    participant_index[token].add(index)
            for token in {str(value).lower() for value in market.tags}:
                if 3 <= len(token) <= 40:
                    tag_index[token].add(index)
            for token in {str(value).lower() for value in market.features.get("numbers", [])}:
                number_index[token].add(index)
            for token in {str(value).lower() for value in market.features.get("event_title_tokens", [])}:
                if len(token) >= 3:
                    event_title_index[token].add(index)
            date_bucket = str(market.features.get("date_bucket") or "")
            if date_bucket:
                date_bucket_index[(market.category_family, date_bucket)].add(index)

        entity_df = {key: len(value) for key, value in entity_index.items()}
        participant_df = {key: len(value) for key, value in participant_index.items()}
        tag_df = {key: len(value) for key, value in tag_index.items()}
        number_df = {key: len(value) for key, value in number_index.items()}
        event_title_df = {key: len(value) for key, value in event_title_index.items()}

        seen_keys: set[tuple[int, int]] = set()
        candidates: list[tuple[NormalizedMarket, NormalizedMarket]] = []
        for index, market in enumerate(markets):
            candidate_indexes: set[int] = set()
            for token in {str(value).lower() for value in market.features.get("participants", [])}:
                if 1 < participant_df.get(token, 0) <= 40:
                    candidate_indexes.update(participant_index[token])
            for token in {str(value).lower() for value in market.features.get("entities", [])}:
                if 1 < entity_df.get(token, 0) <= 30:
                    candidate_indexes.update(entity_index[token])
            for token in {str(value).lower() for value in market.tags}:
                if 1 < tag_df.get(token, 0) <= 20:
                    candidate_indexes.update(tag_index[token])
            for token in {str(value).lower() for value in market.features.get("numbers", [])}:
                if 1 < number_df.get(token, 0) <= 50:
                    candidate_indexes.update(number_index[token])
            for token in {str(value).lower() for value in market.features.get("event_title_tokens", [])}:
                if 1 < event_title_df.get(token, 0) <= 50:
                    candidate_indexes.update(event_title_index[token])
            date_bucket = str(market.features.get("date_bucket") or "")
            if date_bucket:
                candidate_indexes.update(date_bucket_index.get((market.category_family, date_bucket), set()))

            for other_index in candidate_indexes:
                if other_index <= index:
                    continue
                left = market
                right = markets[other_index]
                if left.exchange == right.exchange:
                    continue
                pair_key = (index, other_index)
                if pair_key in seen_keys:
                    continue
                seen_keys.add(pair_key)
                candidates.append((left, right))
        return candidates

    def _is_candidate(self, left: NormalizedMarket, right: NormalizedMarket, tfidf: TfidfIndex) -> bool:
        if self._subject_context_similarity(left, right) >= 0.72:
            return True
        if self._event_title_similarity(left, right) >= 0.78:
            return True
        if left.category_family != right.category_family and "other" not in {left.category_family, right.category_family}:
            return False
        entity_overlap = set(left.features.get("entities", [])) & set(right.features.get("entities", []))
        number_overlap = set(left.features.get("numbers", [])) & set(right.features.get("numbers", []))
        tfidf_score = tfidf.cosine(left, right)
        if tfidf_score >= 0.20:
            return True
        if entity_overlap or number_overlap:
            return True
        return left.category_family == right.category_family and date_proximity_score(left.closes_at, right.closes_at) >= 0.5

    def _hard_vetoes(self, left: NormalizedMarket, right: NormalizedMarket) -> list[str]:
        reasons: list[str] = []
        subject_context_score = self._subject_context_similarity(left, right)
        sports_like = (
            left.category_family == "sports"
            or right.category_family == "sports"
            or left.event_kind.startswith("sports_")
            or right.event_kind.startswith("sports_")
        )
        left_numbers = set(left.features.get("numbers", []))
        right_numbers = set(right.features.get("numbers", []))
        if left_numbers and right_numbers and not (left_numbers & right_numbers):
            reasons.append("numeric_threshold_mismatch")
        if sports_like and left.features.get("championship_like") != right.features.get("championship_like"):
            reasons.append("championship_vs_single_match")
        left_participants = {value.lower() for value in left.features.get("participants", [])}
        right_participants = {value.lower() for value in right.features.get("participants", [])}
        if left.category_family == "sports" and right.category_family == "sports" and left_participants and right_participants:
            if not self._participant_overlap(left_participants, right_participants):
                reasons.append("participant_mismatch")
        if left.market_type != right.market_type:
            reasons.append("market_type_mismatch")
        resolve_compatibility, resolve_reasons, _ = resolve_profile_compatibility(left.resolution_profile, right.resolution_profile)
        if resolve_compatibility == "incompatible":
            reasons.extend(resolve_reasons)
        if left.closes_at and right.closes_at:
            gap_days = abs((left.closes_at - right.closes_at).total_seconds()) / 86_400
            if gap_days > self.settings.max_cross_date_gap_days and subject_context_score < 0.80:
                reasons.append("resolution_window_gap")
        left_host = host_from_url(left.resolution_source_url)
        right_host = host_from_url(right.resolution_source_url)
        if left_host and right_host and left_host != right_host:
            similarity = SequenceMatcher(None, left.description[:240], right.description[:240]).ratio()
            if similarity < 0.40 and left.market_type == right.market_type:
                reasons.append("resolution_source_mismatch")
        return reasons

    @staticmethod
    def _participant_overlap(left: set[str], right: set[str]) -> bool:
        for left_value in left:
            for right_value in right:
                if fuzzy_similarity(left_value, right_value) >= 0.75:
                    return True
        return False

    @staticmethod
    def _subject_entity(market: NormalizedMarket) -> str:
        return str(market.features.get("subject_entity") or "")

    @staticmethod
    def _context_key(market: NormalizedMarket) -> str:
        return str(market.features.get("context_key") or "")

    def _subject_context_similarity(self, left: NormalizedMarket, right: NormalizedMarket) -> float:
        left_subject = self._subject_entity(left)
        right_subject = self._subject_entity(right)
        left_context = self._context_key(left)
        right_context = self._context_key(right)
        if not left_subject or not right_subject or not left_context or not right_context:
            return 0.0
        subject_score = fuzzy_similarity(left_subject, right_subject)
        context_score = max(
            fuzzy_similarity(left_context, right_context),
            SequenceMatcher(None, left_context, right_context).ratio(),
        )
        return subject_score * 0.55 + context_score * 0.45

    @staticmethod
    def _event_title_key(market: NormalizedMarket) -> str:
        return str(market.features.get("event_title_key") or "")

    @staticmethod
    def _event_title_tokens(market: NormalizedMarket) -> set[str]:
        return {str(value).lower() for value in market.features.get("event_title_tokens", []) if value}

    def _event_title_similarity(self, left: NormalizedMarket, right: NormalizedMarket) -> float:
        left_key = self._event_title_key(left)
        right_key = self._event_title_key(right)
        left_tokens = self._event_title_tokens(left)
        right_tokens = self._event_title_tokens(right)
        token_score = max(jaccard(left_tokens, right_tokens), overlap_coefficient(left_tokens, right_tokens))
        if not left_key or not right_key:
            return token_score
        return max(token_score, SequenceMatcher(None, left_key, right_key).ratio())

    @staticmethod
    def _yes_no_labels(market: NormalizedMarket) -> tuple[str, str] | None:
        labels = {
            outcome.label: outcome.canonical_label
            for outcome in market.outcomes
        }
        yes_label = next((label for label, canonical in labels.items() if canonical == "yes"), None)
        no_label = next((label for label, canonical in labels.items() if canonical == "no"), None)
        if yes_label and no_label:
            return yes_label, no_label
        return None

    @staticmethod
    def _entity_vs_field_labels(market: NormalizedMarket) -> tuple[str, str] | None:
        other = next((outcome.label for outcome in market.outcomes if outcome.canonical_label == "other"), None)
        subject = next((outcome.label for outcome in market.outcomes if outcome.canonical_label != "other"), None)
        if subject and other:
            return subject, other
        return None

    def _special_outcome_mapping(self, left: NormalizedMarket, right: NormalizedMarket) -> dict[str, str]:
        subject_context_score = self._subject_context_similarity(left, right)
        if subject_context_score < 0.72:
            return {}

        left_yes_no = self._yes_no_labels(left)
        right_yes_no = self._yes_no_labels(right)
        left_field = self._entity_vs_field_labels(left)
        right_field = self._entity_vs_field_labels(right)

        if left_yes_no and right_field:
            return {
                left_yes_no[0]: right_field[0],
                left_yes_no[1]: right_field[1],
            }
        if left_field and right_yes_no:
            return {
                left_field[0]: right_yes_no[0],
                left_field[1]: right_yes_no[1],
            }
        if left_field and right_field:
            return {
                left_field[0]: right_field[0],
                left_field[1]: right_field[1],
            }
        return {}

    def _build_outcome_mapping(self, left: NormalizedMarket, right: NormalizedMarket) -> dict[str, str]:
        special_mapping = self._special_outcome_mapping(left, right)
        if special_mapping:
            return special_mapping
        if len(left.outcomes) != len(right.outcomes):
            return {}
        remaining = right.outcomes.copy()
        mapping: dict[str, str] = {}
        for left_outcome in left.outcomes:
            best_score = -1.0
            best_match = None
            for right_outcome in remaining:
                score = fuzzy_similarity(left_outcome.canonical_label, right_outcome.canonical_label)
                if score > best_score:
                    best_score = score
                    best_match = right_outcome
            if best_match is None:
                return {}
            if len(left.outcomes) > 2 and best_score < 0.55:
                return {}
            mapping[left_outcome.label] = best_match.label
            remaining = [candidate for candidate in remaining if candidate.label != best_match.label]
        return mapping

    def _score(
        self,
        left: NormalizedMarket,
        right: NormalizedMarket,
        outcome_mapping: dict[str, str],
        tfidf: TfidfIndex,
    ) -> dict[str, float]:
        left_entities = set(left.features.get("entities", []))
        right_entities = set(right.features.get("entities", []))
        left_numbers = set(left.features.get("numbers", []))
        right_numbers = set(right.features.get("numbers", []))
        entity_score = jaccard(left_entities, right_entities)
        subject_context_score = self._subject_context_similarity(left, right)
        if left_numbers and right_numbers:
            if left_numbers == right_numbers:
                entity_score = min(1.0, entity_score + 0.30)
            elif left_numbers & right_numbers:
                entity_score = min(1.0, entity_score + 0.15)
        entity_score = max(entity_score, subject_context_score)

        left_host = host_from_url(left.resolution_source_url)
        right_host = host_from_url(right.resolution_source_url)
        if left.resolution_rules_hash == right.resolution_rules_hash:
            resolution_score = 1.0
        else:
            resolution_score = SequenceMatcher(None, left.description[:300], right.description[:300]).ratio()
            if left_host and right_host and left_host == right_host:
                resolution_score = min(1.0, resolution_score + 0.20)
            if subject_context_score >= 0.80 and left.category_family == right.category_family:
                resolution_score = max(resolution_score, 0.55)
        resolve_status, _, resolve_profile_score = resolve_profile_compatibility(left.resolution_profile, right.resolution_profile)
        if resolve_status == "compatible":
            resolution_score = max(resolution_score, 1.0)
        elif resolve_status == "review":
            resolution_score = max(resolution_score, resolve_profile_score)
        else:
            resolution_score = min(resolution_score, 0.0)

        fuzzy_text_score = max(
            SequenceMatcher(None, left.title.lower(), right.title.lower()).ratio(),
            tfidf.cosine(left, right),
            self._event_title_similarity(left, right),
        )
        fuzzy_text_score = max(fuzzy_text_score, subject_context_score)
        tags_category_score = 0.5 if "other" in {left.category_family, right.category_family} else float(left.category_family == right.category_family)
        tags_category_score = min(1.0, tags_category_score + 0.5 * jaccard(set(left.tags), set(right.tags)))
        matched_right_labels = {value for value in outcome_mapping.values()}
        outcome_score = 1.0 if len(matched_right_labels) == len(right.outcomes) else 0.5
        date_score = date_proximity_score(left.closes_at, right.closes_at)
        if subject_context_score >= 0.80 and left.category_family == right.category_family:
            date_score = max(date_score, 0.5)

        return {
            "entities_numeric": entity_score,
            "resolution": resolution_score,
            "fuzzy_text": fuzzy_text_score,
            "date": date_score,
            "tags_category": tags_category_score,
            "outcome_set": outcome_score,
        }

    def _build_canonical_events(self, matched_pairs: list[MatchedPair]) -> list[CanonicalEvent]:
        parents: dict[str, str] = {}
        market_index: dict[str, NormalizedMarket] = {}

        def find(item: str) -> str:
            parents.setdefault(item, item)
            if parents[item] != item:
                parents[item] = find(parents[item])
            return parents[item]

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        for pair in matched_pairs:
            market_index[pair.market_a.market_id] = pair.market_a
            market_index[pair.market_b.market_id] = pair.market_b
            union(pair.market_a.market_id, pair.market_b.market_id)

        clusters: defaultdict[str, list[NormalizedMarket]] = defaultdict(list)
        for market_id, market in market_index.items():
            clusters[find(market_id)].append(market)

        events: list[CanonicalEvent] = []
        for markets in clusters.values():
            entities = sorted({entity for market in markets for entity in market.features.get("entities", [])})
            numbers = sorted({number for market in markets for number in market.features.get("numbers", [])})
            closes_at = min((market.closes_at for market in markets if market.closes_at is not None), default=None)
            title = max(markets, key=lambda market: len(market.title)).title
            event_kind = max(set(market.event_kind for market in markets), key=lambda value: sum(m.event_kind == value for m in markets))
            hash_input = "|".join(sorted(f"{market.exchange}:{market.market_id}" for market in markets))
            events.append(
                CanonicalEvent(
                    event_id=text_hash(hash_input),
                    title=title,
                    event_kind=event_kind,
                    entities=entities,
                    numbers=numbers,
                    closes_at=closes_at,
                    resolution_rules_hashes=sorted({market.resolution_rules_hash for market in markets}),
                    markets=sorted(markets, key=lambda market: (market.exchange, market.market_id)),
                )
            )
        return events
