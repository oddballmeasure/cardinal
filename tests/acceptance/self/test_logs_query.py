"""Independent checks for `cardinal logs query`."""

import json

import jsonschema

from conftest import record


def lines(result) -> list[dict]:
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def ids(rows: list[dict]) -> list[str]:
    return [row["record"]["id"] for row in rows]


def seeded(send) -> list[dict]:
    posted = [record(level="info"), record(level="warning"), record(level="error"),
              record(level="error"), record(level="critical", repo="example/other")]
    send(posted)
    return posted


def test_query_prints_every_record_oldest_first_in_the_published_shape(cardinal, send) -> None:
    posted = seeded(send)
    rows = lines(cardinal("logs", "query"))
    schema = json.loads(cardinal("logs", "schema").stdout)
    seen = ids(rows)
    assert [item for item in seen if item in {p["id"] for p in posted}] == [p["id"] for p in posted]
    assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)
    assert len({row["seq"] for row in rows}) == len(rows)
    for row in rows:
        assert set(row) == {"seq", "record"}
        jsonschema.validate(row["record"], schema)


def test_query_filters_by_level_repo_and_fingerprint(cardinal, send) -> None:
    posted = seeded(send)
    app = [item["id"] for item in posted if item["source"]["repo"] == "example/app"]
    severe = ids(lines(cardinal("logs", "query", "--level", "warning", "--repo", "example/app")))
    assert severe == app[1:]
    assert ids(lines(cardinal("logs", "query", "--repo", "example/other"))) == [posted[4]["id"]]
    errors = lines(cardinal("logs", "query", "--repo", "example/app", "--level", "error"))
    fingerprint = errors[0]["record"]["fingerprint"]
    same = lines(cardinal("logs", "query", "--fingerprint", fingerprint))
    assert same and all(row["record"]["fingerprint"] == fingerprint for row in same)
    assert {posted[2]["id"], posted[3]["id"]} <= set(ids(same))
    assert posted[4]["id"] not in ids(same)


def test_query_pages_with_after_seq_and_limit(cardinal, send) -> None:
    posted = seeded(send)
    app = lines(cardinal("logs", "query", "--repo", "example/app"))
    assert ids(lines(cardinal("logs", "query", "--repo", "example/app", "--limit", "1"))) == [posted[0]["id"]]
    after = lines(cardinal("logs", "query", "--repo", "example/app", "--after-seq", str(app[0]["seq"])))
    assert ids(after) == [item["id"] for item in posted[1:4]]
    assert all(row["seq"] > app[0]["seq"] for row in after)


def test_query_refuses_an_unknown_level(cardinal, send) -> None:
    seeded(send)
    result = cardinal("logs", "query", "--level", "loud")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "error" in json.loads(result.stderr.strip().splitlines()[-1])
