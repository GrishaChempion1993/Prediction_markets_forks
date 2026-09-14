from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any

from .config import Settings
from .features import jaccard, normalize_text, overlap_coefficient
from .sports_models import SportsCanonicalEvent, SportsLink, SportsMarket
from .utils import text_hash


@dataclass(slots=True)
class LocalEventGroup:
    local_event_id: str
    exchange: str
    base_lane: str
    sport_key: str
    competition_key: str
    season_key: str | None
    is_esports: bool
    participants_sorted: tuple[str, ...]
    participant_signature_keys: tuple[str, ...]
    start_time: object
    best_of: int | None
    title: str
    title_key: str
    title_tokens: tuple[str, ...]
    underlying_title: str
    underlying_title_key: str
    underlying_title_tokens: tuple[str, ...]
    strict_event_key: str | None
    soft_event_key: str | None
    competition_signature: str
    date_bucket: str | None
    markets: list[SportsMarket]


class SportsEventMatcher:
    def __init__(self, settings: Settings, aliases: dict[tuple[str, str], str] | None = None) -> None:
        self.settings = settings
        self.aliases = aliases or {}

    def link(
        self,
        markets: list[SportsMarket],
    ) -> tuple[list[SportsCanonicalEvent], list[dict[str, Any]], list[dict[str, Any]]]:
        local_groups = self._build_local_groups(markets)
        review_items: list[dict[str, Any]] = []
        parents: dict[str, str] = {group.local_event_id: group.local_event_id for group in local_groups}
        edge_confidence: dict[str, float] = {group.local_event_id: 1.0 for group in local_groups}

        def find(item: str) -> str:
            if parents[item] != item:
                parents[item] = find(parents[item])
            return parents[item]

        def union(left: str, right: str, confidence: float) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root == right_root:
                edge_confidence[left_root] = max(edge_confidence.get(left_root, 0.0), confidence)
                return
            if left_root > right_root:
                left_root, right_root = right_root, left_root
            parents[right_root] = left_root
            edge_confidence[left_root] = max(edge_confidence.get(left_root, 0.0), edge_confidence.get(right_root, 0.0), confidence)

        self._fast_path_link(local_groups, union)
        for left_group, right_group in self._candidate_group_pairs(local_groups):
            if find(left_group.local_event_id) == find(right_group.local_event_id):
                continue
            confidence, score_breakdown, veto_reasons = self._score_groups(left_group, right_group)
            if veto_reasons:
                if confidence >= self.settings.sports_review_threshold:
                    review_items.append(
                        self._review_row(left_group, right_group, confidence, score_breakdown, veto_reasons, "veto")
                    )
                continue
            if confidence >= self.settings.sports_linked_threshold:
                union(left_group.local_event_id, right_group.local_event_id, confidence)
            elif confidence >= self.settings.sports_review_threshold:
                review_items.append(
                    self._review_row(left_group, right_group, confidence, score_breakdown, [], "review")
                )

        clustered: dict[str, list[LocalEventGroup]] = {}
        for group in local_groups:
            clustered.setdefault(find(group.local_event_id), []).append(group)

        canonical_events: list[SportsCanonicalEvent] = []
        event_links: list[dict[str, Any]] = []
        for root_id, groups in clustered.items():
            groups = sorted(groups, key=lambda item: (item.exchange, item.local_event_id))
            primary = groups[0]
            all_markets = [market for group in groups for market in group.markets]
            exchange_count = len({group.exchange for group in groups})
            canonical_event_id = self._canonical_event_id(groups)
            canonical_events.append(
                SportsCanonicalEvent(
                    canonical_event_id=canonical_event_id,
                    title=max((group.underlying_title or group.title for group in groups), key=len),
                    event_class="future" if primary.base_lane == "future" else "match",
                    match_lane=primary.base_lane,
                    sport_key=primary.sport_key,
                    competition_key=primary.competition_key,
                    season_key=primary.season_key,
                    is_esports=primary.is_esports,
                    participants_sorted=primary.participants_sorted,
                    start_time=primary.start_time,
                    best_of=primary.best_of,
                    markets=all_markets,
                )
            )
            status = "linked" if exchange_count >= 2 else "unmatched"
            confidence = edge_confidence.get(root_id, 1.0)
            for market in all_markets:
                event_links.append(
                    {
                        "exchange": market.exchange,
                        "market_id": market.market_id,
                        "canonical_event_id": canonical_event_id,
                        "confidence": confidence,
                        "status": status,
                        "match_lane": primary.base_lane,
                        "manual_override": 0,
                        "resolve_compatibility": "event_only",
                        "score_json": {
                            "exchange_count": exchange_count,
                            "local_group_count": len(groups),
                        },
                    }
                )

        return canonical_events, event_links, review_items

    def _fast_path_link(
        self,
        groups: list[LocalEventGroup],
        union: Any,
    ) -> None:
        strict_buckets: dict[tuple[str, str, str], list[LocalEventGroup]] = defaultdict(list)
        soft_buckets: dict[tuple[str, str, str], list[LocalEventGroup]] = defaultdict(list)
        for group in groups:
            if group.strict_event_key:
                strict_buckets[(group.base_lane, group.sport_key, group.strict_event_key)].append(group)
            elif group.soft_event_key:
                soft_buckets[(group.base_lane, group.sport_key, group.soft_event_key)].append(group)

        for bucket_groups in strict_buckets.values():
            self._union_bucket(bucket_groups, union, confidence=0.995)

        for bucket_groups in soft_buckets.values():
            if len({group.exchange for group in bucket_groups}) < 2:
                continue
            for left_group, right_group in combinations(bucket_groups, 2):
                if left_group.exchange == right_group.exchange:
                    continue
                participant_score = self._participant_score(left_group, right_group)
                title_score = self._title_score(left_group, right_group)
                competition_score = self._competition_score(left_group.competition_key, right_group.competition_key, left_group.sport_key)
                start_score = self._start_time_score(left_group.start_time, right_group.start_time)
                if left_group.base_lane == "future":
                    if max(participant_score, title_score) >= 0.85 and competition_score >= 0.55:
                        union(left_group.local_event_id, right_group.local_event_id, 0.95)
                elif participant_score >= 0.90 and start_score >= 0.75 and max(title_score, competition_score) >= 0.60:
                    union(left_group.local_event_id, right_group.local_event_id, 0.96)

    @staticmethod
    def _union_bucket(bucket_groups: list[LocalEventGroup], union: Any, confidence: float) -> None:
        ordered = sorted(bucket_groups, key=lambda item: (item.exchange, item.local_event_id))
        for left_group, right_group in combinations(ordered, 2):
            if left_group.exchange == right_group.exchange:
                continue
            union(left_group.local_event_id, right_group.local_event_id, confidence)

    def _build_local_groups(self, markets: list[SportsMarket]) -> list[LocalEventGroup]:
        grouped: dict[str, list[SportsMarket]] = {}
        for market in markets:
            signature = self._local_signature(market)
            grouped.setdefault(signature, []).append(market)
        groups: list[LocalEventGroup] = []
        for signature, grouped_markets in grouped.items():
            primary = max(grouped_markets, key=lambda item: (item.liquidity_usd, item.volume_usd, len(item.title)))
            groups.append(
                LocalEventGroup(
                    local_event_id=text_hash(f"{primary.exchange}|{signature}"),
                    exchange=primary.exchange,
                    base_lane=self._base_lane(primary),
                    sport_key=primary.sport_key,
                    competition_key=self._canonical_competition(primary.competition_key, primary.sport_key),
                    season_key=primary.season_key,
                    is_esports=primary.is_esports,
                    participants_sorted=tuple(self._canonical_participant(item, primary.sport_key) for item in primary.participants_sorted),
                    participant_signature_keys=tuple(
                        str(value)
                        for value in primary.features.get("participant_signature_keys", [])
                        if value
                    ),
                    start_time=primary.start_time,
                    best_of=primary.best_of,
                    title=primary.title,
                    title_key=str(primary.features.get("event_title_key") or normalize_text(primary.title)),
                    title_tokens=tuple(str(value) for value in primary.features.get("event_title_tokens", [])),
                    underlying_title=str(primary.features.get("underlying_event_title") or primary.title),
                    underlying_title_key=str(
                        primary.features.get("underlying_event_title_key")
                        or primary.features.get("event_title_key")
                        or normalize_text(primary.title)
                    ),
                    underlying_title_tokens=tuple(
                        str(value)
                        for value in primary.features.get(
                            "underlying_event_title_tokens",
                            primary.features.get("event_title_tokens", []),
                        )
                    ),
                    strict_event_key=(
                        str(primary.features.get("strict_event_key")).strip()
                        if primary.features.get("strict_event_key")
                        else None
                    ),
                    soft_event_key=(
                        str(primary.features.get("soft_event_key")).strip()
                        if primary.features.get("soft_event_key")
                        else None
                    ),
                    competition_signature=str(
                        primary.features.get("competition_key_norm")
                        or self._canonical_competition(primary.competition_key, primary.sport_key)
                    ),
                    date_bucket=str(primary.features.get("date_bucket")) if primary.features.get("date_bucket") else None,
                    markets=sorted(grouped_markets, key=lambda item: (item.exchange, item.market_id)),
                )
            )
        return groups

    def _candidate_group_pairs(self, groups: list[LocalEventGroup]) -> list[tuple[LocalEventGroup, LocalEventGroup]]:
        bucketed: dict[tuple[str, str, str], list[LocalEventGroup]] = {}
        for group in groups:
            for bucket_key in self._bucket_keys(group):
                bucketed.setdefault(bucket_key, []).append(group)
        candidates: list[tuple[LocalEventGroup, LocalEventGroup]] = []
        seen: set[tuple[str, str]] = set()
        for bucket_groups in bucketed.values():
            for left_group, right_group in combinations(bucket_groups, 2):
                if left_group.exchange == right_group.exchange:
                    continue
                key = tuple(sorted((left_group.local_event_id, right_group.local_event_id)))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((left_group, right_group))
        return candidates

    def _bucket_keys(self, group: LocalEventGroup) -> list[tuple[str, str, str]]:
        buckets: list[tuple[str, str, str]] = []
        if group.strict_event_key:
            buckets.append((group.base_lane, group.sport_key, f"strict:{group.strict_event_key}"))
        if group.soft_event_key:
            buckets.append((group.base_lane, group.sport_key, f"soft:{group.soft_event_key}"))

        title_anchor = (
            "|".join(sorted(group.underlying_title_tokens)[:3])
            or "|".join(sorted(group.title_tokens)[:3])
            or (group.underlying_title_key[:48] if group.underlying_title_key else "unknown")
        )
        if group.base_lane == "future":
            if group.competition_signature and group.competition_signature != "unknown":
                buckets.append((group.base_lane, group.sport_key, f"comp:{group.competition_signature}"))
            for token in self._anchor_tokens(group):
                buckets.append((group.base_lane, group.sport_key, f"title_token:{token}"))
            for signature in group.participant_signature_keys[:8]:
                buckets.append((group.base_lane, group.sport_key, f"psig:{signature}"))
            buckets.extend(
                [
                    (group.base_lane, group.sport_key, group.season_key or "na"),
                    (group.base_lane, group.sport_key, f"title:{title_anchor}"),
                ]
            )
            return list(dict.fromkeys(buckets))
        day_buckets = self._day_buckets(group)
        buckets.append((group.base_lane, group.sport_key, f"title:{title_anchor}"))
        for day_bucket in day_buckets:
            buckets.append((group.base_lane, group.sport_key, f"day:{day_bucket}"))
            if group.competition_signature and group.competition_signature != "unknown":
                buckets.append((group.base_lane, group.sport_key, f"compday:{group.competition_signature}:{day_bucket}"))
            for token in self._anchor_tokens(group):
                buckets.append((group.base_lane, group.sport_key, f"title_token:{day_bucket}:{token}"))
        participants_anchor = "|".join(group.participants_sorted)
        if participants_anchor:
            buckets.append((group.base_lane, group.sport_key, f"participants:{participants_anchor}"))
        for signature in group.participant_signature_keys[:8]:
            for day_bucket in day_buckets:
                buckets.append((group.base_lane, group.sport_key, f"psigday:{day_bucket}:{signature}"))
        return list(dict.fromkeys(buckets))

    @staticmethod
    def _day_buckets(group: LocalEventGroup) -> tuple[str, ...]:
        if group.start_time:
            values = [
                (group.start_time + timedelta(days=offset)).date().isoformat()
                for offset in (-1, 0, 1)
            ]
            return tuple(dict.fromkeys(values))
        if group.date_bucket:
            return (group.date_bucket,)
        return ("unknown",)

    @staticmethod
    def _base_lane(market: SportsMarket) -> str:
        return "future" if market.event_class == "future" else "fixture"

    def _local_signature(self, market: SportsMarket) -> str:
        base_lane = self._base_lane(market)
        strict_event_key = str(market.features.get("strict_event_key") or "").strip()
        soft_event_key = str(market.features.get("soft_event_key") or "").strip()
        if strict_event_key:
            return "|".join([market.exchange, base_lane, strict_event_key, str(market.best_of or 0)])
        if soft_event_key:
            return "|".join([market.exchange, base_lane, soft_event_key, str(market.best_of or 0)])
        if base_lane == "future":
            return "|".join(
                [
                    market.exchange,
                    base_lane,
                    market.sport_key,
                    self._canonical_competition(market.competition_key, market.sport_key),
                    market.season_key or "na",
                ]
            )
        participant_key = "|".join(self._canonical_participant(item, market.sport_key) for item in market.participants_sorted) or "unknown"
        start_key = market.start_time.isoformat()[:13] if market.start_time else "unknown"
        return "|".join(
            [
                market.exchange,
                base_lane,
                market.sport_key,
                self._canonical_competition(market.competition_key, market.sport_key),
                participant_key,
                start_key,
                str(market.best_of or 0),
            ]
        )

    def _score_groups(
        self,
        left: LocalEventGroup,
        right: LocalEventGroup,
    ) -> tuple[float, dict[str, float], list[str]]:
        vetoes: list[str] = []
        if left.base_lane != right.base_lane:
            vetoes.append("base_lane_mismatch")
        if left.sport_key != right.sport_key:
            vetoes.append("sport_mismatch")
        strict_event_score = 1.0 if left.strict_event_key and left.strict_event_key == right.strict_event_key else 0.0
        soft_event_score = 1.0 if left.soft_event_key and left.soft_event_key == right.soft_event_key else 0.0
        title_score = self._title_score(left, right)
        competition_score = self._competition_score(left.competition_key, right.competition_key, left.sport_key)
        if left.base_lane == "future":
            participant_score = max(1.0 if soft_event_score or strict_event_score else 0.0, self._participant_score(left, right))
            start_score = 1.0
            if left.season_key and right.season_key and left.season_key != right.season_key:
                vetoes.append("season_mismatch")
            if strict_event_score:
                competition_score = max(competition_score, 1.0)
            elif soft_event_score:
                competition_score = max(competition_score, 0.75)
            elif title_score < 0.55 and competition_score < 0.55:
                vetoes.append("future_title_mismatch")
        else:
            participant_score = self._participant_score(left, right)
            start_score = self._start_time_score(left.start_time, right.start_time)
            if not left.participants_sorted or not right.participants_sorted:
                participant_score = max(participant_score, title_score * 0.75)
            if strict_event_score:
                participant_score = 1.0
                start_score = max(start_score, 0.95)
                competition_score = max(competition_score, 0.9)
            elif soft_event_score:
                participant_score = max(participant_score, 1.0)
                start_score = max(start_score, 0.75)
            if not strict_event_score and not soft_event_score and participant_score < 0.5 and title_score < 0.82:
                vetoes.append("participant_mismatch")
            if not strict_event_score and start_score == 0.0 and title_score < 0.80:
                vetoes.append("start_time_gap")

        if not strict_event_score and not soft_event_score and competition_score < 0.3 and title_score < 0.82:
            vetoes.append("competition_mismatch")

        lane_score = 1.0
        if left.is_esports != right.is_esports:
            lane_score = 0.5
        if left.best_of and right.best_of and left.best_of != right.best_of:
            vetoes.append("best_of_mismatch")

        score_breakdown = {
            "strict_event": strict_event_score,
            "soft_event": soft_event_score,
            "participants": participant_score,
            "competition": competition_score,
            "start_time": start_score,
            "event_lane": lane_score,
            "title": title_score,
        }
        if strict_event_score:
            confidence = 0.99
        elif left.base_lane == "future" and soft_event_score:
            confidence = 0.82 + title_score * 0.08 + competition_score * 0.05 + lane_score * 0.05
        elif left.base_lane != "future" and soft_event_score:
            confidence = (
                0.40
                + participant_score * 0.20
                + competition_score * 0.10
                + start_score * 0.15
                + lane_score * 0.05
                + title_score * 0.10
            )
        elif left.base_lane == "future":
            confidence = (
                participant_score * 0.10
                + competition_score * 0.25
                + start_score * 0.10
                + lane_score * 0.15
                + title_score * 0.40
            )
        else:
            confidence = (
                participant_score * 0.30
                + competition_score * 0.15
                + start_score * 0.20
                + lane_score * 0.10
                + title_score * 0.25
            )
        return confidence, score_breakdown, vetoes

    @staticmethod
    def _anchor_tokens(group: LocalEventGroup) -> tuple[str, ...]:
        tokens = set(group.underlying_title_tokens) | set(group.title_tokens)
        ranked = sorted((token for token in tokens if len(token) >= 4), key=lambda token: (-len(token), token))
        return tuple(ranked[:6])

    def _participant_score(self, left: LocalEventGroup, right: LocalEventGroup) -> float:
        if not left.participants_sorted or not right.participants_sorted:
            return 0.0
        if left.participants_sorted == right.participants_sorted:
            return 1.0
        signature_score = overlap_coefficient(set(left.participant_signature_keys), set(right.participant_signature_keys))
        if signature_score == 1.0:
            return 1.0
        right_values = list(right.participants_sorted)
        used_indexes: set[int] = set()
        pair_scores: list[float] = []
        for left_value in left.participants_sorted:
            best_score = 0.0
            best_index: int | None = None
            for index, right_value in enumerate(right_values):
                if index in used_indexes:
                    continue
                score = self._participant_name_score(left_value, right_value)
                if score > best_score:
                    best_score = score
                    best_index = index
            if best_index is not None:
                used_indexes.add(best_index)
                pair_scores.append(best_score)
        aligned_score = sum(pair_scores) / max(len(left.participants_sorted), len(right.participants_sorted)) if pair_scores else 0.0
        return max(signature_score, aligned_score)

    @staticmethod
    def _name_tokens(value: str) -> tuple[str, ...]:
        return tuple(token for token in normalize_text(value).split() if token)

    def _participant_name_score(self, left: str, right: str) -> float:
        left_tokens = self._name_tokens(left)
        right_tokens = self._name_tokens(right)
        if not left_tokens or not right_tokens:
            return 0.0
        if left_tokens == right_tokens:
            return 1.0
        token_overlap = overlap_coefficient(set(left_tokens), set(right_tokens))
        token_jaccard = jaccard(set(left_tokens), set(right_tokens))
        prefix_score = self._token_alignment_score(left_tokens, right_tokens)
        surname_score = 0.0
        if left_tokens[-1] == right_tokens[-1] and len(left_tokens[-1]) >= 4:
            surname_score = 0.92 if min(len(left_tokens), len(right_tokens)) >= 2 else 0.88
        raw_score = SequenceMatcher(None, " ".join(left_tokens), " ".join(right_tokens)).ratio()
        return max(token_overlap, token_jaccard, prefix_score, surname_score, raw_score)

    def _token_alignment_score(self, left_tokens: tuple[str, ...], right_tokens: tuple[str, ...]) -> float:
        used_indexes: set[int] = set()
        aligned: list[float] = []
        for left_token in left_tokens:
            best_score = 0.0
            best_index: int | None = None
            for index, right_token in enumerate(right_tokens):
                if index in used_indexes:
                    continue
                score = self._token_similarity(left_token, right_token)
                if score > best_score:
                    best_score = score
                    best_index = index
            if best_index is not None:
                used_indexes.add(best_index)
                aligned.append(best_score)
        return sum(aligned) / max(len(left_tokens), len(right_tokens)) if aligned else 0.0

    @staticmethod
    def _token_similarity(left: str, right: str) -> float:
        if left == right:
            return 1.0
        if len(left) >= 3 and right.startswith(left):
            return 0.92
        if len(right) >= 3 and left.startswith(right):
            return 0.92
        if len(left) == 1 and right.startswith(left):
            return 0.70
        if len(right) == 1 and left.startswith(right):
            return 0.70
        return SequenceMatcher(None, left, right).ratio()

    def _competition_score(self, left: str, right: str, sport_key: str) -> float:
        if left == right:
            return 1.0
        if "unknown" in {left, right}:
            return 0.5
        left_tokens = {token for token in normalize_text(left).split() if token}
        right_tokens = {token for token in normalize_text(right).split() if token}
        score = max(
            SequenceMatcher(None, left, right).ratio(),
            jaccard(left_tokens, right_tokens),
            overlap_coefficient(left_tokens, right_tokens),
        )
        if sport_key and sport_key in left and sport_key in right:
            score = max(score, 0.7)
        return score

    def _title_score(self, left: LocalEventGroup, right: LocalEventGroup) -> float:
        title_token_left = set(left.title_tokens)
        title_token_right = set(right.title_tokens)
        underlying_token_left = set(left.underlying_title_tokens)
        underlying_token_right = set(right.underlying_title_tokens)
        token_score = max(jaccard(title_token_left, title_token_right), overlap_coefficient(title_token_left, title_token_right))
        key_score = SequenceMatcher(None, left.title_key, right.title_key).ratio() if left.title_key and right.title_key else 0.0
        underlying_token_score = max(
            jaccard(underlying_token_left, underlying_token_right),
            overlap_coefficient(underlying_token_left, underlying_token_right),
        )
        underlying_key_score = (
            SequenceMatcher(None, left.underlying_title_key, right.underlying_title_key).ratio()
            if left.underlying_title_key and right.underlying_title_key
            else 0.0
        )
        raw_score = SequenceMatcher(None, normalize_text(left.title), normalize_text(right.title)).ratio()
        underlying_raw_score = SequenceMatcher(None, normalize_text(left.underlying_title), normalize_text(right.underlying_title)).ratio()
        return max(token_score, key_score, raw_score, underlying_token_score, underlying_key_score, underlying_raw_score)

    def _start_time_score(self, left, right) -> float:
        if left is None or right is None:
            return 0.5
        delta = abs(left - right)
        if delta == timedelta(0):
            return 1.0
        if delta <= timedelta(hours=1):
            return 0.95
        if delta <= timedelta(hours=self.settings.sports_match_window_hours):
            return 0.75
        if delta <= timedelta(hours=12):
            return 0.35
        return 0.0

    def _canonical_competition(self, value: str, sport_key: str) -> str:
        return self.aliases.get(("competition", f"{sport_key}:{value}"), value)

    def _canonical_participant(self, value: str, sport_key: str) -> str:
        return self.aliases.get(("participant", f"{sport_key}:{value}"), value)

    @staticmethod
    def _canonical_event_id(groups: list[LocalEventGroup]) -> str:
        strict_keys = {group.strict_event_key for group in groups if group.strict_event_key}
        if len(strict_keys) == 1:
            return text_hash(next(iter(strict_keys)))
        soft_keys = {group.soft_event_key for group in groups if group.soft_event_key}
        if len(soft_keys) == 1:
            return text_hash(next(iter(soft_keys)))
        primary = groups[0]
        participant_values = sorted({value for group in groups for value in group.participants_sorted if value})
        title_values = sorted({group.underlying_title_key or group.title_key for group in groups if group.underlying_title_key or group.title_key})
        date_values = sorted({group.date_bucket for group in groups if group.date_bucket})
        payload = "|".join(
            [
                primary.base_lane,
                primary.sport_key,
                sorted(group.competition_key for group in groups)[0],
                sorted(group.season_key or "na" for group in groups)[0],
                "|".join(participant_values) or "|".join(title_values) or "unknown",
                "|".join(date_values) or "unknown_date",
                str(sorted(group.best_of or 0 for group in groups)[0]),
            ]
        )
        return text_hash(payload)

    @staticmethod
    def _review_row(
        left: LocalEventGroup,
        right: LocalEventGroup,
        confidence: float,
        score_breakdown: dict[str, float],
        veto_reasons: list[str],
        review_status: str,
    ) -> dict[str, Any]:
        return {
            "exchange_a": left.exchange,
            "market_id_a": left.markets[0].market_id,
            "exchange_b": right.exchange,
            "market_id_b": right.markets[0].market_id,
            "confidence": confidence,
            "status": review_status,
            "match_lane": left.base_lane,
            "veto_reasons": veto_reasons,
            "score_json": score_breakdown,
            "payload_json": {
                "title_a": left.title,
                "title_b": right.title,
                "underlying_title_a": left.underlying_title,
                "underlying_title_b": right.underlying_title,
                "participants_a": left.participants_sorted,
                "participants_b": right.participants_sorted,
                "strict_event_key_a": left.strict_event_key,
                "strict_event_key_b": right.strict_event_key,
                "soft_event_key_a": left.soft_event_key,
                "soft_event_key_b": right.soft_event_key,
                "resolution_profile_signature_a": left.markets[0].features.get("resolution_profile_signature"),
                "resolution_profile_signature_b": right.markets[0].features.get("resolution_profile_signature"),
            },
        }
