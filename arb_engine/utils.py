from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse
import json
import os
import re


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.isdigit():
        timestamp = int(candidate)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    return datetime.fromisoformat(candidate)


def json_hash(payload: object) -> str:
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def host_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower().strip()
    return host or None


def json_ready(value: object) -> object:
    if is_dataclass(value):
        return json_ready(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_ready(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_ready(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def dump_json(value: object) -> str:
    return json.dumps(json_ready(value), ensure_ascii=True, sort_keys=True)


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def safe_float(value: object, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, (float, int)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return default


def ordered_pairs(items: Iterable[str]) -> list[tuple[str, str]]:
    values = list(items)
    pairs: list[tuple[str, str]] = []
    for left_index, left in enumerate(values):
        for right in values[left_index + 1 :]:
            pairs.append((left, right))
    return pairs
