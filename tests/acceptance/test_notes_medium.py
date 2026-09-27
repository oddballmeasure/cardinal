"""Independent HTTP checks for partial note updates."""

import json

from test_notes_service import create, http


def test_patch_title_updates_only_title(service: str) -> None:
    before = json.loads(http(service, "/notes")[2])
    note = create(service, "Before", ["work"])
    status, _, body = http(service, f"/notes/{note['id']}", {"title": "After"}, method="PATCH")
    assert status == 200
    assert json.loads(body) == {**note, "title": "After"}
    assert json.loads(http(service, "/notes")[2]) == [*before, {**note, "title": "After"}]


def test_patch_tags_preserves_id_and_list_order(service: str) -> None:
    before = json.loads(http(service, "/notes")[2])
    first = create(service, "Café", ["old"])
    second = create(service, "Other", [])
    status, _, body = http(service, f"/notes/{first['id']}", {"tags": ["new", "é"]}, method="PATCH")
    assert status == 200
    updated = {**first, "tags": ["new", "é"]}
    assert json.loads(body) == updated
    assert json.loads(http(service, "/notes")[2]) == [*before, updated, second]


def test_invalid_patch_is_atomic_and_missing_id_is_404(service: str) -> None:
    before = json.loads(http(service, "/notes")[2])
    note = create(service, "Keep", ["safe"])
    for payload in ({"title": ""}, {"tags": [1]}, {"title": "Changed", "tags": [1]}):
        status, _, body = http(service, f"/notes/{note['id']}", payload, method="PATCH")
        assert status == 400
        assert "error" in json.loads(body)
        assert json.loads(http(service, "/notes")[2]) == [*before, note]
    status, _, body = http(service, "/notes/999", {"title": "Missing"}, method="PATCH")
    assert status == 404
    assert "error" in json.loads(body)
