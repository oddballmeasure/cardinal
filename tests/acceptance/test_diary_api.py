"""Independent API checks for Redis-backed dated diary entries."""

import json

from test_notes_service import http


def test_post_diary_returns_created_dated_entry(service: str) -> None:
    entry = {"date": "2026-09-24", "title": "First", "body": "Café"}
    status, content_type, body = http(service, "/diaries", entry)
    assert status == 201
    assert content_type.startswith("application/json")
    created = json.loads(body)
    assert isinstance(created["id"], int)
    assert {key: created[key] for key in entry} == entry


def test_get_diaries_returns_entries_in_creation_order(service: str) -> None:
    before = json.loads(http(service, "/diaries")[2])
    first = {"date": "2026-09-25", "title": "First", "body": "Café"}
    second = {"date": "2026-09-24", "title": "Second", "body": "An earlier date, later entry"}
    first_status, _, first_body = http(service, "/diaries", first)
    second_status, _, second_body = http(service, "/diaries", second)
    assert first_status == second_status == 201
    after = json.loads(http(service, "/diaries")[2])
    assert after == [*before, json.loads(first_body), json.loads(second_body)]


def test_invalid_diary_fields_are_rejected_without_mutation(service: str) -> None:
    before = json.loads(http(service, "/diaries")[2])
    for entry in (
        {"date": "2026-02-30", "title": "Impossible", "body": "No"},
        {"date": "2026-09-25", "title": " ", "body": "No"},
        {"date": "2026-09-25", "title": "Missing body"},
    ):
        status, content_type, body = http(service, "/diaries", entry)
        assert status == 400
        assert content_type.startswith("application/json")
        assert "error" in json.loads(body)
    assert json.loads(http(service, "/diaries")[2]) == before


def test_diaries_survive_api_container_restart(stack) -> None:
    before = json.loads(http(stack.api_url, "/diaries")[2])
    entry = {"date": "2026-09-26", "title": "Persisted", "body": "Redis keeps this"}
    status, _, body = http(stack.api_url, "/diaries", entry)
    assert status == 201
    stack.restart_api()
    after = json.loads(http(stack.api_url, "/diaries")[2])
    assert after == [*before, {**after[-1], **entry}]
