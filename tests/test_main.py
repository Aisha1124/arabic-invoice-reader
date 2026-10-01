import hashlib
import logging
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, store
from app.schema import CallMetadata, Invoice, LineItem
from app.store import read_resolver_events
from app.validate import validate

SELLER_NAME = "شركة الخليج للمعدات الطبية"
SELLER_VAT = "315839799889833"
DESCRIPTION = "استشارة هندسية"
PNG = b"\x89PNG\r\n\x1a\ninvoice one"
OTHER_PNG = b"\x89PNG\r\n\x1a\ninvoice two"
META = CallMetadata(
    model="test-model",
    prompt_version="v3",
    latency_ms=10,
    prompt_tokens=1,
    completion_tokens=1,
    estimated_cost_usd=None,
    cache_hit=True,
    temperature_zero=True,
)


def _invoice(unit_price: str = "13.43", **overrides: object) -> Invoice:
    """3 x 13.43 = 40.29, VAT 6.04, total 46.33; unit_price overrides inject a misread."""
    values: dict[str, object] = {
        "invoice_type": "standard",
        "seller_name": SELLER_NAME,
        "seller_vat_number": SELLER_VAT,
        "buyer_vat_number": "310000000000003",
        "line_items": [
            LineItem(
                description=DESCRIPTION,
                quantity="3",
                unit_price=unit_price,
                line_total="40.29",
                vat_rate="0.15",
                vat_amount="6.04",
            )
        ],
        "subtotal": "40.29",
        "vat_total": "6.04",
        "total": "46.33",
    }
    values.update(overrides)
    return Invoice(**values)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "app.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return TestClient(main.app)


def _serve(monkeypatch: pytest.MonkeyPatch, invoice: Invoice) -> None:
    """Stands in for the model call: no API key, no spend."""
    monkeypatch.setattr(main, "extract", lambda image: (validate(invoice, {}), META))


def _upload(client: TestClient, image: bytes = PNG) -> dict:
    response = client.post(
        "/extract", content=image, headers={"Content-Type": "image/png"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _reviews(client: TestClient) -> list[dict]:
    response = client.get("/reviews")
    assert response.status_code == 200
    return response.json()


# --- /extract wiring ---------------------------------------------------------


def test_arithmetic_misread_is_queued_with_a_reference(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _invoice(unit_price="12.43"))

    body = _upload(client)

    assert set(body) == {
        "invoice",
        "confidences",
        "findings",
        "status",
        "checks",
        "review",
    }
    assert body["review"] == {
        "status": "queued",
        "reference": "R-0001",
        "resolver_status": "suggested",
    }
    (item,) = _reviews(client)
    assert item["reference"] == "R-0001"
    assert item["status"] == "suggested"
    assert item["image_sha256"] == hashlib.sha256(PNG).hexdigest()
    assert item["created_utc"]
    top = item["candidates"][0]
    assert top == {
        "field": "line_items[0].unit_price",
        "read_value": "12.43",
        "value": "13.43",
        "edit": "known_substitution",
        "rank": 1,
    }
    (event,) = read_resolver_events(10)
    assert (event.event, event.reference) == ("suggested", "R-0001")


def test_valid_invoice_is_not_queued(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _invoice())
    body = _upload(client)
    assert body["review"] is None
    assert {c["outcome"] for c in body["checks"]} == {"pass"}
    assert _reviews(client) == []


def test_structural_warning_alone_is_not_queued(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _invoice(seller_vat_number=None))
    body = _upload(client)
    assert [f["severity"] for f in body["findings"]] == ["warning"]
    assert body["review"] is None
    assert _reviews(client) == []


def test_date_mismatch_alone_is_not_queued(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error, but not arithmetic: the resolver has nothing to work with."""
    _serve(
        monkeypatch,
        _invoice(
            invoice_date=date(2026, 7, 31),
            invoice_timestamp=datetime(2026, 8, 1, 9, 0),  # noqa: DTZ001
        ),
    )
    body = _upload(client)
    assert "invoice_date_matches_timestamp" in [f["rule"] for f in body["findings"]]
    assert body["review"] is None
    assert _reviews(client) == []


def test_reupload_of_the_same_image_keeps_one_review(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _invoice(unit_price="12.43"))
    first = _upload(client)
    again = _upload(client)

    assert again["review"] == first["review"]
    assert again["review"]["reference"] == "R-0001"
    (item,) = _reviews(client)
    assert item["reference"] == "R-0001"
    assert len(read_resolver_events(10)) == 1


def test_references_are_sequential_across_images(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _invoice(unit_price="12.43"))
    _upload(client, PNG)
    _upload(client, OTHER_PNG)
    assert [i["reference"] for i in _reviews(client)] == ["R-0001", "R-0002"]


def test_unresolvable_invoice_is_queued_with_the_values_involved(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _invoice(unit_price="12.43", total="99.99"))
    body = _upload(client)

    assert body["review"]["resolver_status"] == "unresolvable"
    (item,) = _reviews(client)
    assert item["status"] == "unresolvable"
    assert item["candidates"] == []
    involved = {r["field"]: r["read_value"] for r in item["involved"]}
    assert involved["line_items[0].unit_price"] == "12.43"
    assert involved["total"] == "99.99"


def test_resolver_failure_does_not_fail_the_extraction(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _serve(monkeypatch, _invoice(unit_price="12.43"))

    def broken(invoice: Invoice) -> None:
        raise RuntimeError("resolver bug quoting 12.43")

    monkeypatch.setattr(main, "resolve", broken)

    with caplog.at_level(logging.ERROR):
        body = _upload(client)

    assert body["status"] == "needs_review"
    assert body["review"] == {
        "status": "error",
        "reference": None,
        "resolver_status": None,
    }
    assert _reviews(client) == []
    assert "resolver failed" in caplog.text
    assert "12.43" not in caplog.text


def test_reviews_carry_no_names_vat_numbers_or_descriptions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _invoice(unit_price="12.43"))
    _upload(client)
    text = client.get("/reviews").text
    assert SELLER_NAME not in text
    assert SELLER_VAT not in text
    assert DESCRIPTION not in text


def test_review_card_carries_the_same_check_outcomes_as_the_extraction(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _invoice(unit_price="12.43"))
    body = _upload(client)

    (item,) = _reviews(client)
    without_amounts = [
        {k: v for k, v in c.items() if k not in ("computed", "difference")}
        for c in body["checks"]
    ]
    assert {k for c in item["checks"] for k in c} == {
        "rule",
        "line",
        "outcome",
        "reason",
    }
    assert item["checks"] == without_amounts
    qty = body["checks"][0]
    assert (qty["computed"], qty["difference"]) == ("37.29", "3.00")  # 3 x 12.43
    failed = [(c["rule"], c["line"]) for c in item["checks"] if c["outcome"] == "fail"]
    assert failed == [("line_total_equals_quantity_times_unit_price", 0)]


# --- decisions ---------------------------------------------------------------


def _queue_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, **overrides: str
) -> dict:
    _serve(monkeypatch, _invoice(**{"unit_price": "12.43", **overrides}))
    _upload(client)
    (item,) = _reviews(client)
    return item


def _decide(client: TestClient, item_id: str, **body: object) -> httpx.Response:
    return client.post(f"/reviews/{item_id}/decision", json=body)


def test_accepting_records_the_decision_and_nothing_else(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _queue_one(client, monkeypatch)
    audit_before = client.get("/audit").json()

    response = _decide(client, item["id"], decision="accepted", rank=1)

    assert response.status_code == 200
    assert response.json() == {"reference": "R-0001", "decision": "accepted"}
    assert _reviews(client) == []
    assert client.get("/audit").json() == audit_before
    decided = read_resolver_events(1)[0]
    assert (decided.event, decided.reference, decided.rank) == ("accepted", "R-0001", 1)


def test_rejecting_records_the_decision(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _queue_one(client, monkeypatch)
    response = _decide(client, item["id"], decision="rejected")
    assert response.status_code == 200
    assert read_resolver_events(1)[0].event == "rejected"


def test_unresolvable_takes_checked_manually_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _queue_one(client, monkeypatch, total="99.99")

    wrong = _decide(client, item["id"], decision="rejected")
    assert wrong.status_code == 400
    assert "checked_manually" in wrong.json()["detail"]

    right = _decide(client, item["id"], decision="checked_manually")
    assert right.status_code == 200
    assert read_resolver_events(1)[0].event == "checked_manually"


def test_suggestion_cannot_be_marked_checked_manually(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _queue_one(client, monkeypatch)
    response = _decide(client, item["id"], decision="checked_manually")
    assert response.status_code == 400
    assert len(_reviews(client)) == 1


def test_deciding_twice_or_an_unknown_id_is_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _queue_one(client, monkeypatch)
    assert _decide(client, item["id"], decision="rejected").status_code == 200
    assert _decide(client, item["id"], decision="rejected").status_code == 404
    assert _decide(client, "no-such-id", decision="rejected").status_code == 404


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"decision": "accepted", "rank": 99}, 400),
        ({"decision": "accepted"}, 400),
        ({"decision": "rejected", "rank": 1}, 400),
        ({"decision": "maybe"}, 422),
        ({}, 422),
    ],
)
def test_invalid_decisions_change_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, body: dict, status: int
) -> None:
    item = _queue_one(client, monkeypatch)
    response = client.post(f"/reviews/{item['id']}/decision", json=body)
    assert response.status_code == status
    assert [i["id"] for i in _reviews(client)] == [item["id"]]


def test_page_has_the_review_tab(client: TestClient) -> None:
    page = client.get("/").text
    assert 'id="tab-review"' in page
    assert "Checked manually" in page
    # Misreads can sit outside the failed checks (eval/results.md, "Resolver on
    # real model output"), so an unresolvable card must not imply the list is complete.
    assert "check every number on the invoice" in page
