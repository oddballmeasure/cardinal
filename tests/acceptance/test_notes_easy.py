"""Independent HTTP checks for note retrieval by ID."""

import json

from test_notes_service import create, http


def test_get_note_by_id_returns_created_note(service: str) -> None:
    first = create(service, "Café", ["personal"])
    create(service, "Later", ["work"])
    status, content_type, body = http(service, f"/notes/{first['id']}")
    assert status == 200
    assert content_type.startswith("application/json")
    assert json.loads(body) == first


def test_get_missing_and_invalid_note_ids_return_404(service: str) -> None:
    for path in ("/notes/999", "/notes/not-a-number"):
        status, _, body = http(service, path)
        assert status == 404
        assert "error" in json.loads(body)


def test_get_note_by_id_preserves_list_response(service: str) -> None:
    before = json.loads(http(service, "/notes")[2])
    first = create(service, "First", [])
    second = create(service, "Second", [])
    http(service, f"/notes/{first['id']}")
    status, _, body = http(service, "/notes")
    assert status == 200
    assert json.loads(body) == [*before, first, second]
