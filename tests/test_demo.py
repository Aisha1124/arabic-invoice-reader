"""
Demo mode: saved model output on synthetic invoices, no model client, no
uploads, and a private review queue per visitor. Runs offline.
"""

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import demo, extract, main, store

GROUND_TRUTH = Path(__file__).resolve().parent.parent / "eval" / "ground_truth.json"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DEMO_MODE", "1")
    for name in ("OPENAI_API_KEY", "OPENAI_MODEL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "app.db")
    monkeypatch.setattr(main, "visitors", demo.Visitors())

    def no_model(*args: object, **kwargs: object) -> None:
        raise AssertionError("demo mode must not make a model client")

    monkeypatch.setattr(extract, "OpenAI", no_model)
    return TestClient(main.app)


def _visitor() -> dict[str, str]:
    return {"X-Demo-Visitor": str(uuid.uuid4())}


def _run(client: TestClient, sample: str, visitor: dict[str, str]) -> dict:
    response = client.post(f"/demo/run/{sample}", headers=visitor)
    assert response.status_code == 200, response.text
    return response.json()


def _reviews(client: TestClient, visitor: dict[str, str]) -> list[dict]:
    response = client.get("/reviews", headers=visitor)
    assert response.status_code == 200, response.text
    return response.json()


# --- the samples ---------------------------------------------------------------------


def test_samples_are_listed_with_their_images(client: TestClient) -> None:
    samples = client.get("/demo/samples").json()

    assert [s["label"] for s in samples] == [
        "Clean invoice",
        "Many misreads",
        "Warnings only",
        "QR code disagrees",
        "Single misread",
    ]
    for sample in samples:
        assert client.get(sample["image"]).status_code == 200
    (single,) = [s for s in samples if s["id"] == "single-misread"]
    assert (
        single["note"] == "One value altered on purpose to show how suggestions work."
    )


def test_every_sample_runs_with_no_model_and_no_api_key(client: TestClient) -> None:
    visitor = _visitor()
    outcomes = {s.id: _run(client, s.id, visitor) for s in demo.SAMPLES}

    assert outcomes["clean"]["status"] == "ok"
    assert outcomes["clean"]["review"] is None
    assert outcomes["many-misreads"]["review"]["resolver_status"] == "unresolvable"
    assert {f["severity"] for f in outcomes["warnings-only"]["findings"]} == {"warning"}
    assert outcomes["warnings-only"]["review"]["status"] == "queued"


def test_qr_sample_shows_its_disagreements(client: TestClient) -> None:
    body = _run(client, "qr-disagrees", _visitor())

    assert body["qr"]["status"] == "read"
    quoted = {
        f["rule"]: f["message"]
        for f in body["findings"]
        if f["rule"].endswith("_matches_qr")
    }
    assert set(quoted) == {
        "seller_vat_number_matches_qr",
        "timestamp_matches_qr",
        "total_matches_qr",
        "vat_total_matches_qr",
    }
    assert all(m.startswith("QR code says ") for m in quoted.values())


def test_single_misread_is_suggested_and_can_be_accepted(client: TestClient) -> None:
    visitor = _visitor()
    body = _run(client, "single-misread", visitor)
    assert body["review"]["resolver_status"] == "suggested"

    (item,) = _reviews(client, visitor)
    top = item["candidates"][0]
    assert (top["field"], top["read_value"], top["value"]) == (
        "line_items[0].quantity",
        "2",
        "3.000",
    )
    decided = client.post(
        f"/reviews/{item['id']}/decision",
        json={"decision": "accepted", "rank": 1},
        headers=visitor,
    )
    assert decided.status_code == 200
    assert _reviews(client, visitor) == []


# --- what demo mode refuses -----------------------------------------------------------


def test_uploads_are_refused_even_with_a_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    response = client.post(
        "/extract",
        content=b"\x89PNG\r\n\x1a\nimage",
        headers={"Content-Type": "image/png", **_visitor()},
    )
    assert response.status_code == 403
    assert "demo" in response.json()["detail"]


def test_health_reports_demo_mode_without_reading_the_key(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "demo_mode": True}


@pytest.mark.parametrize("headers", [{}, {"X-Demo-Visitor": "short"}])
def test_queue_and_log_need_a_visitor_id(client: TestClient, headers: dict) -> None:
    for method, path in (
        ("get", "/reviews"),
        ("get", "/audit"),
        ("post", "/demo/run/clean"),
    ):
        response = getattr(client, method)(path, headers=headers)
        assert response.status_code == 400, path


def test_unknown_sample_is_not_found(client: TestClient) -> None:
    assert client.post("/demo/run/nope", headers=_visitor()).status_code == 404


# --- visitors are isolated -------------------------------------------------------------


def test_each_visitor_has_a_private_queue_and_log(client: TestClient) -> None:
    alice, bob = _visitor(), _visitor()
    _run(client, "single-misread", alice)

    assert len(_reviews(client, alice)) == 1
    assert _reviews(client, bob) == []
    assert len(client.get("/audit", headers=alice).json()) == 1
    assert client.get("/audit", headers=bob).json() == []


def test_reset_empties_the_visitors_queue(client: TestClient) -> None:
    visitor = _visitor()
    _run(client, "single-misread", visitor)
    assert client.post("/demo/reset", headers=visitor).status_code == 200

    assert _reviews(client, visitor) == []
    assert client.get("/audit", headers=visitor).json() == []


def test_nothing_is_written_to_the_shared_database(client: TestClient) -> None:
    visitor = _visitor()
    for sample in demo.SAMPLES:
        _run(client, sample.id, visitor)
    for item in _reviews(client, visitor):
        client.post(
            f"/reviews/{item['id']}/decision",
            json={"decision": "checked_manually"}
            if item["status"] in ("unresolvable", "cross_check", "compliance")
            else {"decision": "rejected"},
            headers=visitor,
        )

    assert not store.DB_PATH.exists()


def test_least_recently_used_visitor_is_closed_first() -> None:
    visitors = demo.Visitors(limit=2)
    a, _ = visitors.get("a" * 16)
    b, _ = visitors.get("b" * 16)
    visitors.get("a" * 16)  # a is now the most recently used
    visitors.get("c" * 16)  # over the limit: b goes

    assert len(visitors) == 2
    a.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        b.execute("SELECT 1")


# --- the fixtures ----------------------------------------------------------------------


def _saved(name: str) -> dict:
    return json.loads((demo.DEMO_DIR / name).read_text(encoding="utf-8"))


def _answer(name: str) -> dict:
    return json.loads(_saved(name)["record"]["content"])


def test_single_misread_is_inv_1010_with_one_digit_changed() -> None:
    original, altered = _answer("INV-2026-1010.json"), _answer("single-misread.json")
    assert original["invoice"]["line_items"][0]["quantity"] == "٣"

    altered["invoice"]["line_items"][0]["quantity"] = "٣"
    assert altered == original


def test_demo_folder_holds_only_the_samples() -> None:
    expected = {s.image for s in demo.SAMPLES} | {s.response for s in demo.SAMPLES}
    expected.add("INV-2026-1010.json")  # the source of the single misread
    assert {p.name for p in demo.DEMO_DIR.iterdir()} == expected


def test_fixture_names_are_the_generators_invented_ones() -> None:
    """Every seller and buyer name in the saved answers is one eval/generate_invoices.py
    invented; VAT numbers there are random digits."""
    truth = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    invented = {g[k] for g in truth for k in ("seller_name", "buyer_name")}
    for sample in demo.SAMPLES:
        invoice = _answer(sample.response)["invoice"]
        for key in ("seller_name", "buyer_name"):
            assert invoice[key] is None or invoice[key] in invented, (sample.id, key)


# --- the deployment image ----------------------------------------------------------------

DOCKERFILE = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text("utf-8")


def test_image_runs_the_demo_on_the_hosts_port_with_7860_as_default() -> None:
    """Render sets PORT; a host that does not gets 7860."""
    assert "DEMO_MODE=1" in DOCKERFILE
    (cmd,) = [line for line in DOCKERFILE.splitlines() if line.startswith("CMD ")]
    assert json.loads(cmd[len("CMD ") :]) == [
        "sh",
        "-c",
        'exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-7860}"',
    ]


def test_image_copies_no_secrets_cache_or_data() -> None:
    copies = [
        line.split()[-2] for line in DOCKERFILE.splitlines() if line.startswith("COPY ")
    ]
    assert copies == ["requirements.txt", "app", "static"]
    instructions = "\n".join(
        line for line in DOCKERFILE.splitlines() if not line.lstrip().startswith("#")
    )
    for word in ("API_KEY", "TOKEN", "SECRET", ".env", "OPENAI"):
        assert word not in instructions, word


def test_every_sample_says_what_it_shows(client: TestClient) -> None:
    samples = client.get("/demo/samples").json()

    tips = [s["tooltip"] for s in samples]
    assert all(tip and len(tip) <= 120 for tip in tips), tips
    assert len(set(tips)) == len(tips)
