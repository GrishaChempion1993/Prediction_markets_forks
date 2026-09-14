from __future__ import annotations

from argparse import ArgumentParser
from collections import Counter, defaultdict
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from arb_engine.config import Settings
from arb_engine.market_db import MarketCache
from arb_engine.matcher import MarketMatcher, TfidfIndex
from arb_engine.features import fuzzy_similarity


def parse_args() -> object:
    parser = ArgumentParser(description="Analyze matcher quality on cached normalized markets.")
    parser.add_argument("--db", default="data/market_cache.sqlite3", help="Path to the SQLite market cache.")
    parser.add_argument("--category", default="", help="Optional category_family filter.")
    parser.add_argument("--top", type=int, default=10, help="How many examples to print per bucket.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings(repo_root=REPO_ROOT, market_db_path=Path(args.db))
    cache = MarketCache(settings)
    try:
        markets = cache.load_active_markets()
    finally:
        cache.close()

    if args.category:
        markets = [market for market in markets if market.category_family == args.category]

    matcher = MarketMatcher(settings)
    tfidf = TfidfIndex.build(markets)

    entity_index: defaultdict[str, set[int]] = defaultdict(set)
    participant_index: defaultdict[str, set[int]] = defaultdict(set)
    tag_index: defaultdict[str, set[int]] = defaultdict(set)
    number_index: defaultdict[str, set[int]] = defaultdict(set)
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

    entity_df = {key: len(value) for key, value in entity_index.items()}
    participant_df = {key: len(value) for key, value in participant_index.items()}
    tag_df = {key: len(value) for key, value in tag_index.items()}
    number_df = {key: len(value) for key, value in number_index.items()}

    pair_seen: set[tuple[str, str]] = set()
    vetoed: list[dict[str, object]] = []
    below: list[dict[str, object]] = []
    mapping_failed: list[dict[str, object]] = []
    reason_counts: Counter[str] = Counter()

    for index, market in enumerate(markets):
        candidate_ids: set[int] = set()
        for token in {str(value).lower() for value in market.features.get("participants", [])}:
            if 1 < participant_df.get(token, 0) <= 40:
                candidate_ids.update(participant_index[token])
        for token in {str(value).lower() for value in market.features.get("entities", [])}:
            if 1 < entity_df.get(token, 0) <= 30:
                candidate_ids.update(entity_index[token])
        for token in {str(value).lower() for value in market.tags}:
            if 1 < tag_df.get(token, 0) <= 20:
                candidate_ids.update(tag_index[token])
        for token in {str(value).lower() for value in market.features.get("numbers", [])}:
            if 1 < number_df.get(token, 0) <= 50:
                candidate_ids.update(number_index[token])

        for other_index in candidate_ids:
            if other_index <= index:
                continue
            left = market
            right = markets[other_index]
            if left.exchange == right.exchange:
                continue
            pair_key = tuple(sorted((f"{left.exchange}:{left.market_id}", f"{right.exchange}:{right.market_id}")))
            if pair_key in pair_seen:
                continue
            pair_seen.add(pair_key)
            if not matcher._is_candidate(left, right, tfidf):
                continue

            base_fuzzy = max(fuzzy_similarity(left.title, right.title), tfidf.cosine(left, right))
            shared_entities = sorted(set(left.features.get("entities", [])) & set(right.features.get("entities", [])))
            vetoes = matcher._hard_vetoes(left, right)
            if vetoes:
                reason_counts.update(vetoes)
                vetoed.append(
                    {
                        "left": f"{left.exchange} | {left.title}",
                        "right": f"{right.exchange} | {right.title}",
                        "shared_entities": shared_entities,
                        "base_fuzzy": round(base_fuzzy, 3),
                        "vetoes": vetoes,
                    }
                )
                continue

            mapping = matcher._build_outcome_mapping(left, right)
            if not mapping:
                reason_counts["outcome_mapping_failed"] += 1
                mapping_failed.append(
                    {
                        "left": f"{left.exchange} | {left.title}",
                        "right": f"{right.exchange} | {right.title}",
                        "shared_entities": shared_entities,
                        "base_fuzzy": round(base_fuzzy, 3),
                    }
                )
                continue

            score = matcher._score(left, right, mapping, tfidf)
            confidence = (
                score["entities_numeric"] * 0.30
                + score["resolution"] * 0.20
                + score["fuzzy_text"] * 0.15
                + score["date"] * 0.15
                + score["tags_category"] * 0.10
                + score["outcome_set"] * 0.10
            )
            if confidence < settings.min_match_confidence:
                reason_counts["below_threshold"] += 1
                below.append(
                    {
                        "left": f"{left.exchange} | {left.title}",
                        "right": f"{right.exchange} | {right.title}",
                        "shared_entities": shared_entities,
                        "confidence": round(confidence, 3),
                        "score": {key: round(value, 3) for key, value in score.items()},
                        "mapping": mapping,
                    }
                )

    vetoed.sort(key=lambda item: (len(item["shared_entities"]), item["base_fuzzy"]), reverse=True)
    mapping_failed.sort(key=lambda item: (len(item["shared_entities"]), item["base_fuzzy"]), reverse=True)
    below.sort(key=lambda item: item["confidence"], reverse=True)

    print(f"markets={len(markets)} pair_candidates={len(pair_seen)}")
    print("reason_counts=", reason_counts.most_common(20))
    print("\nTOP_VETOED")
    for item in vetoed[: args.top]:
        print(item)
    print("\nTOP_MAPPING_FAILED")
    for item in mapping_failed[: args.top]:
        print(item)
    print("\nTOP_BELOW_THRESHOLD")
    for item in below[: args.top]:
        print(item)


if __name__ == "__main__":
    main()
