# Cardinal notes test application

This repository is a Docker Compose application with a FastAPI API, Redis
persistent storage, and a production React frontend served by Nginx. Redis is
the source of truth for notes and future dated diary entries; its append-only
file is stored in the named `redis_data` volume.

Start the full stack and inspect the published web and API ports:

```sh
docker compose up --build --wait
docker compose port web 80
docker compose port api 8000
```

The web container proxies `/api/*` to the API. Existing endpoints are:

```sh
curl http://localhost:<api-port>/healthz
curl -X POST http://localhost:<api-port>/notes \
  -H 'Content-Type: application/json' \
  -d '{"title":"Plan","tags":["work"]}'
curl http://localhost:<api-port>/notes
```

`GET /stats/tags` summarises stored notes as
`{"notes": <count>, "tags": <total tags>, "average_tags_per_note": <tags / notes, rounded to 2 places>}`.

`GET /stats/top-tag` returns the most used tag across all notes as
`{"tag": <most used tag or null>, "count": <uses>}`. `tag` is `null` when no
note has a tag. The response follows the JSON Schema in
`docs/schemas/top-tag.schema.json`.

`POST /notes` accepts a nonempty `title` and a list of nonempty string `tags`.
It returns the created note with a numeric ID. `GET /notes` returns notes in
creation order as JSON. API health depends on Redis being reachable.

Run the repository's container and browser E2E checks with:

```sh
python -m pip install 'playwright>=1.50,<2' 'pytest>=8,<10'
python -m playwright install chromium
python -m pytest -q tests
```

The tests build and start a unique Compose project, then remove its containers
and Redis volume. CI keeps the required check name `http-e2e` and builds all
images before running these tests.

The `deploy/` pair remains a fake-host validation fixture. Its script records
the configured host, and its health command checks that record and a fake host
readiness marker; it does not deploy to a production host.

## Error reporting

Unhandled API errors return `500 {"error": "internal error"}`. When
`CARDINAL_INGEST_URL` and `CARDINAL_INGEST_TOKEN` are set in the environment
Compose runs in, each one is also sent to Cardinal's ingest endpoint as a
LogRecord (`api/app/reporting.py`). From a container, the host's Cardinal is
`http://host.docker.internal:<port>/v1/records`.
