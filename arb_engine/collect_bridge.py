from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def resolve_tsx(repo_root: Path) -> Path | str:
    candidates = [
        repo_root / "node_modules" / ".bin" / ("tsx.cmd" if sys.platform.startswith("win") else "tsx"),
        repo_root / "node_modules" / ".bin" / "tsx",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    fallback = shutil.which("tsx.cmd") or shutil.which("tsx")
    if fallback:
        return fallback
    raise FileNotFoundError("Unable to locate tsx. Run `npm.cmd install` first.")


def collect_exchange(
    repo_root: Path,
    exchange: str,
    out_path: Path,
    max_markets: int,
    env_overrides: dict[str, str] | None = None,
) -> None:
    tsx = resolve_tsx(repo_root)
    command = [
        str(tsx),
        "collect",
        "--exchange",
        exchange,
        "--out",
        str(out_path.as_posix()),
        "--max-markets",
        str(max_markets),
    ]
    env = None
    if env_overrides:
        env = dict(os.environ)
        env.update(env_overrides)
    subprocess.run(command, check=True, cwd=repo_root, env=env)
