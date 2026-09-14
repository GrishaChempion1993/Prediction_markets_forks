from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from arb_engine.config import Settings
from arb_engine.io import latest_snapshot_files, read_snapshot_file
from arb_engine.sports_db import SportsCache
from arb_engine.sports_matcher import SportsEventMatcher
from arb_engine.sports_normalize import normalize_sports_snapshots
from arb_engine.sports_scanner import SportsArbitrageScanner


def _parse_snapshot_override(value: str) -> tuple[str, Path]:
    exchange, separator, raw_path = value.partition("=")
    if not separator or not exchange.strip() or not raw_path.strip():
        raise ValueError(f"Expected --snapshot exchange=path, got: {value}")
    return exchange.strip(), Path(raw_path.strip())


def parse_args() -> object:
    parser = ArgumentParser(description="Rebuild sports cache and event links from latest ingest snapshots.")
    parser.add_argument(
        "--exchanges",
        default="polymarket,opn,azuro,sxbet,myriad,kalshi",
        help="Comma-separated exchanges to rebuild from latest ingest files.",
    )
    parser.add_argument("--ingest-root", default="data/ingest", help="Ingest root relative to repo root.")
    parser.add_argument("--db", default="data/sports_cache.sqlite3", help="SQLite path relative to repo root.")
    parser.add_argument(
        "--snapshot",
        action="append",
        default=[],
        help="Override latest ingest file for one exchange. Format: exchange=path",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    exchanges = [value.strip() for value in str(args.exchanges).split(",") if value.strip()]
    snapshot_overrides = dict(_parse_snapshot_override(value) for value in args.snapshot)
    settings = Settings(
        repo_root=REPO_ROOT,
        ingest_root=Path(args.ingest_root),
        sports_db_path=Path(args.db),
    )

    ingest_root = settings.repo_root / settings.ingest_root
    latest_files = {path.parent.name: path for path in latest_snapshot_files(ingest_root, exchanges)}
    selected_files: list[Path] = []
    for exchange in exchanges:
        override = snapshot_overrides.get(exchange)
        if override is not None:
            path = override if override.is_absolute() else settings.repo_root / override
            if not path.exists():
                raise FileNotFoundError(f"Override snapshot not found for {exchange}: {path}")
            selected_files.append(path)
            continue
        path = latest_files.get(exchange)
        if path is not None:
            selected_files.append(path)

    if not selected_files:
        print("No ingest files found.")
        return

    cache = SportsCache(settings)
    try:
        for path in selected_files:
            snapshots = read_snapshot_file(path)
            if not snapshots:
                print(f"skip path={path} reason=no_snapshots")
                continue
            normalized = normalize_sports_snapshots(snapshots, settings)
            exchange = snapshots[0].exchange
            cache.upsert_discovery(exchange, path, snapshots, normalized)
            print(f"rebuilt exchange={exchange} snapshots={len(snapshots)} normalized={len(normalized)} path={path}")

        matcher = SportsEventMatcher(settings, aliases=cache.load_aliases())
        canonical_events, event_links, review_items = matcher.link(cache.load_active_markets())
        cache.replace_links(canonical_events, event_links, review_items)
        opportunities = SportsArbitrageScanner(settings).scan(canonical_events)
        cache.upsert_opportunities(opportunities)
        linked = sum(1 for row in event_links if row["status"] == "linked")
        print(
            {
                **cache.counts(),
                "linked_rows": linked,
                "review_items": len(review_items),
                "sports_opportunities": len(opportunities),
            }
        )
    finally:
        cache.close()


if __name__ == "__main__":
    main()
