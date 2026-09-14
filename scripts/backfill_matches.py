from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from arb_engine.config import Settings
from arb_engine.market_db import MarketCache
from arb_engine.matcher import MarketMatcher
from arb_engine.scanner import OpportunityScanner


def parse_args() -> object:
    parser = ArgumentParser(description="Match active cached markets and persist matched pairs/opportunities.")
    parser.add_argument("--db", default="data/market_cache.sqlite3", help="SQLite path relative to repo root.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings(repo_root=REPO_ROOT, market_db_path=Path(args.db))
    cache = MarketCache(settings)
    try:
        active_markets = cache.load_active_markets()
        matcher = MarketMatcher(settings)
        pairs, events = matcher.match(active_markets)
        stored_pairs = cache.store_pairs(pairs)
        monitored_pairs = cache.load_pairs()
        scanner = OpportunityScanner(settings)
        opportunities = scanner.scan(monitored_pairs)
        alerts = cache.upsert_opportunities(opportunities)
        print(
            {
                "active_markets": len(active_markets),
                "matched_pairs_found": len(pairs),
                "matched_pairs_stored": len(stored_pairs),
                "canonical_events": len(events),
                "monitored_pairs": len(monitored_pairs),
                "opportunities": len(opportunities),
                "alerts": len(alerts),
            }
        )
        for pair in sorted(stored_pairs, key=lambda item: item.confidence, reverse=True)[:10]:
            print(
                round(pair.confidence, 3),
                "|",
                pair.market_a.exchange,
                "|",
                pair.market_a.title,
                "||",
                pair.market_b.exchange,
                "|",
                pair.market_b.title,
            )
    finally:
        cache.close()


if __name__ == "__main__":
    main()
