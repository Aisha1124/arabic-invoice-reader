"""Model response cache keyed by image hash, model name and prompt version.

One JSON file per key under ``.cache/``. No eviction, no TTL, no locking.
"""

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_DIR = Path(".cache")


def _key(image_bytes: bytes, model: str, prompt_version: str) -> str:
    digest = hashlib.sha256(image_bytes).hexdigest()
    return f"{digest}_{model}_{prompt_version}"


def _path(image_bytes: bytes, model: str, prompt_version: str) -> Path:
    return CACHE_DIR / f"{_key(image_bytes, model, prompt_version)}.json"


def get(image_bytes: bytes, model: str, prompt_version: str) -> dict | None:
    path = _path(image_bytes, model, prompt_version)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Corrupt cache file %s treated as miss: %s", path, exc)
        return None
    if not isinstance(value, dict):
        logger.warning(
            "Cache file %s holds %s, expected object; treated as miss",
            path,
            type(value).__name__,
        )
        return None
    return value


def set(image_bytes: bytes, model: str, prompt_version: str, value: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _path(image_bytes, model, prompt_version).write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8"
    )
