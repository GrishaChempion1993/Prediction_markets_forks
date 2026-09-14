from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from arb_engine.config import Settings
from arb_engine.io import latest_snapshot_files, read_snapshot_file
from arb_engine.market_db import MarketCache
from arb_engine.normalize import normalize_snapshots


def parse_args() -> object:
    parser = ArgumentParser(description="Rebuild normalized market cache from existing ingest snapshots.")
    parser.add_argument(
        "--exchanges",
        default="polymarket,opn,azuro,sxbet,kalshi",
        help="Comma-separated exchanges to rebuild from latest ingest files.",
    )
    parser.add_argument("--ingest-root", default="data/ingest", help="Ingest root relative to repo root.")
    parser.add_argument("--db", default="data/market_cache.sqlite3", help="SQLite path relative to repo root.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    exchanges = [value.strip() for value in str(args.exchanges).split(",") if value.strip()]
    settings = Settings(
        repo_root=REPO_ROOT,
        ingest_root=Path(args.ingest_root),
        market_db_path=Path(args.db),
    )

    ingest_root = settings.repo_root / settings.ingest_root
    latest_files = latest_snapshot_files(ingest_root, exchanges)
    if not latest_files:
        print("No ingest files found.")
        return

    cache = MarketCache(settings)
    try:
        for path in latest_files:
            snapshots = read_snapshot_file(path)
            if not snapshots:
                print(f"skip path={path} reason=no_snapshots")
                continue
            normalized = normalize_snapshots(snapshots, settings)
            exchange = snapshots[0].exchange
            cache.upsert_discovery(exchange, path, snapshots, normalized)
            print(f"rebuilt exchange={exchange} snapshots={len(snapshots)} normalized={len(normalized)} path={path}")
        print(cache.counts())
    finally:
        cache.close()


if __name__ == "__main__":
    main()
