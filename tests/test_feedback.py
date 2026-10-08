"""PR-04: thumbs up / down per request, the online quality signal."""

import json

import pytest

from dwarpal.feedback import FeedbackLog


def test_record_and_summary_keep_the_latest_rating(tmp_path):
    log = FeedbackLog(tmp_path / "f.jsonl")
    log.record("abc123", "up")
    log.record("def456", "down")
    log.record("abc123", "down")  # changed their mind
    assert log.summary() == {"rated": 2, "up": 0, "down": 2, "up_rate": 0.0}
    lines = [json.loads(line) for line in (tmp_path / "f.jsonl").read_text().splitlines()]
    assert set(lines[0]) == {"time", "request_id", "rating"}  # no message text is stored


@pytest.mark.parametrize("request_id, rating", [("abc", "meh"), ("", "up"), ("a b", "up")])
def test_bad_input_is_rejected(tmp_path, request_id, rating):
    with pytest.raises(ValueError):
        FeedbackLog(tmp_path / "f.jsonl").record(request_id, rating)


def test_empty_summary(tmp_path):
    assert FeedbackLog(tmp_path / "f.jsonl").summary()["up_rate"] is None


def test_feedback_endpoint(make_client):
    client = make_client()
    ok = client.post("/v1/dwarpal/feedback", json={"request_id": "abc123", "rating": "up"})
    assert ok.status_code == 200 and ok.json()["rating"] == "up"
    bad = client.post("/v1/dwarpal/feedback", json={"request_id": "abc123", "rating": "meh"})
    assert bad.status_code == 400
    assert client.app.state.feedback.summary()["up"] == 1


def test_feedback_endpoint_needs_the_api_key(make_client):
    client = make_client(api_keys="k1")
    body = {"request_id": "abc123", "rating": "up"}
    assert client.post("/v1/dwarpal/feedback", json=body).status_code == 401
    ok = client.post("/v1/dwarpal/feedback", json=body, headers={"Authorization": "Bearer k1"})
    assert ok.status_code == 200
