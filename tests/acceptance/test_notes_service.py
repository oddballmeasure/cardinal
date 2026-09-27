"""Independent HTTP acceptance checks for the future notes-service issue."""

import csv
import io
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def http(base: str, path: str, payload: dict | None = None, method: str | None = None) -> tuple[int, str, str]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        base + path,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    try:
        response = urlopen(request, timeout=2)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers["Content-Type"], response.read().decode("utf-8")


def create(base: str, title: str, tags: list[str]) -> dict:
    status, content_type, body = http(base, "/notes", {"title": title, "tags": tags})
    assert status == 201
    assert content_type.startswith("application/json")
    return json.loads(body)


def test_tag_filter_is_exact_case_insensitive_and_ordered(service: str) -> None:
    first = create(service, "Plan", ["Cardinal-Exact-Only"])
    create(service, "Workout", ["Cardinal-Exact-Only-Extra"])
    third = create(service, "Review", ["cARDINAL-eXACT-oNLY", "urgent"])
    status, content_type, body = http(service, "/notes?tag=CARDINAL-EXACT-ONLY")
    assert status == 200
    assert content_type.startswith("application/json")
    assert [note["id"] for note in json.loads(body)] == [first["id"], third["id"]]


def test_csv_quotes_commas_and_preserves_unicode(service: str) -> None:
    note = create(service, "Café, mañana", ["travel", "é"])
    status, content_type, body = http(service, "/notes?format=csv")
    assert status == 200
    assert content_type.startswith("text/csv")
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == ["id", "title", "tags"]
    assert [row for row in rows if row[0] == str(note["id"])] == [
        [str(note["id"]), "Café, mañana", "travel;é"]
    ]
    assert '"Café, mañana"' in body


def test_empty_filtered_csv_has_header_only(service: str) -> None:
    create(service, "One", ["seen"])
    status, content_type, body = http(service, "/notes?tag=cardinal-no-match-7f9d&format=csv")
    assert status == 200
    assert content_type.startswith("text/csv")
    assert list(csv.reader(io.StringIO(body))) == [["id", "title", "tags"]]


def test_filter_and_csv_combine(service: str) -> None:
    first = create(service, "First, café", ["Csv-Only-Tag"])
    create(service, "Other", ["Csv-Only-Tag-extra"])
    third = create(service, "Last", ["csv-only-tag"])
    status, content_type, body = http(service, "/notes?tag=CSV-ONLY-TAG&format=csv")
    assert status == 200
    assert content_type.startswith("text/csv")
    rows = list(csv.reader(io.StringIO(body)))
    assert rows == [
        ["id", "title", "tags"],
        [str(first["id"]), "First, café", "Csv-Only-Tag"],
        [str(third["id"]), "Last", "csv-only-tag"],
    ]


def test_default_json_and_post_still_work(service: str) -> None:
    before = json.loads(http(service, "/notes")[2])
    note = create(service, "Plain", ["personal"])
    status, content_type, body = http(service, "/notes")
    assert status == 200
    assert content_type.startswith("application/json")
    assert json.loads(body) == [*before, note]
