from __future__ import annotations

from datetime import UTC, datetime
from gzip import open as gzip_open
from pathlib import Path
from typing import Any
import json

from .models import MarketSnapshot
from .utils import dump_json, ensure_parent, parse_datetime


def read_snapshot_file(path: Path) -> list[MarketSnapshot]:
    snapshots: list[MarketSnapshot] = []
    opener = gzip_open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            payload = json.loads(line)
            snapshots.append(
                MarketSnapshot(
                    schema_version=str(payload["schema_version"]),
                    exchange=str(payload["exchange"]),
                    market_id=str(payload["market_id"]),
                    fetched_at=parse_datetime(payload["fetched_at"]) or datetime.min.replace(tzinfo=UTC),
                    source=dict(payload["source"]),
                    raw=dict(payload["raw"]),
                )
            )
    return snapshots


def latest_snapshot_files(ingest_root: Path, exchanges: list[str]) -> list[Path]:
    files: list[Path] = []
    for exchange in exchanges:
        exchange_dir = ingest_root / exchange
        if not exchange_dir.exists():
            continue
        candidates = sorted(exchange_dir.glob("*.jsonl.gz"))
        if candidates:
            files.append(candidates[-1])
    return files


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(f"{dump_json(row)}\n")
