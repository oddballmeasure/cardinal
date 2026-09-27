"""Independent HTTP checks for the tag statistics endpoint. The empty case runs first, on the
fresh stack, before any note exists."""

import json

from test_notes_service import create, http


def test_tag_stats_with_no_notes_do_not_fail(service: str) -> None:
    status, content_type, body = http(service, "/stats/tags")
    assert status == 200, body
    assert content_type.startswith("application/json")
    summary = json.loads(body)
    assert summary["notes"] == 0
    assert summary["tags"] == 0


def test_tag_stats_summarise_stored_notes(service: str) -> None:
    create(service, "Stats one", ["a", "b", "c"])
    create(service, "Stats two", [])
    notes = json.loads(http(service, "/notes")[2])
    total = sum(len(note["tags"]) for note in notes)
    status, _, body = http(service, "/stats/tags")
    assert status == 200
    assert json.loads(body) == {"notes": len(notes), "tags": total,
                                "average_tags_per_note": round(total / len(notes), 2)}
