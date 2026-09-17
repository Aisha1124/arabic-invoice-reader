import logging
from pathlib import Path

import pytest

from app import cache

IMAGE = b"fake png bytes"
RESPONSE = {"invoice_number": "INV-1", "total": "115.00"}


@pytest.fixture(autouse=True)
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    return tmp_path


def test_miss_then_hit() -> None:
    assert cache.get(IMAGE, "model-a", "v1") is None
    cache.set(IMAGE, "model-a", "v1", RESPONSE)
    assert cache.get(IMAGE, "model-a", "v1") == RESPONSE


def test_different_image_is_different_key() -> None:
    cache.set(IMAGE, "model-a", "v1", RESPONSE)
    assert cache.get(b"other bytes", "model-a", "v1") is None


def test_different_model_is_different_key() -> None:
    cache.set(IMAGE, "model-a", "v1", RESPONSE)
    assert cache.get(IMAGE, "model-b", "v1") is None


def test_different_prompt_version_is_different_key() -> None:
    cache.set(IMAGE, "model-a", "v1", RESPONSE)
    assert cache.get(IMAGE, "model-a", "v2") is None


def test_corrupt_file_is_miss_with_warning(
    cache_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache.set(IMAGE, "model-a", "v1", RESPONSE)
    (path,) = cache_dir.glob("*.json")
    path.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.cache"):
        assert cache.get(IMAGE, "model-a", "v1") is None

    assert "Corrupt cache file" in caplog.text


def test_non_object_file_is_miss_with_warning(
    cache_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache.set(IMAGE, "model-a", "v1", RESPONSE)
    (path,) = cache_dir.glob("*.json")
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.cache"):
        assert cache.get(IMAGE, "model-a", "v1") is None

    assert "expected object" in caplog.text
