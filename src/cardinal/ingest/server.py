"""`POST /v1/records`: running apps send LogRecords here. Each record is validated alone, so one
bad record never costs the rest of its batch. Only configured repositories may report."""

import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydantic import ValidationError

from cardinal.config.models import Config
from cardinal.logs.record import LogRecord
from cardinal.logs.sink import Sink

log = logging.getLogger(__name__)
MAX_BODY = 5_000_000


def parse(body: bytes) -> list:
    text = body.decode("utf-8").strip()
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def accept(items: list, repos: set[str], sink: Sink) -> dict:
    accepted, rejected = 0, []
    for index, item in enumerate(items):
        try:
            record = LogRecord.model_validate(item)
        except ValidationError as exc:
            rejected.append({"index": index, "error": exc.errors(include_url=False, include_input=False)})
            continue
        if record.source.repo not in repos:
            rejected.append({"index": index, "error": f"{record.source.repo} is not a configured repository"})
            continue
        sink.write(record)
        accepted += 1
    return {"accepted": accepted, "rejected": rejected}


def handler_for(token: str, repos: set[str], sink: Sink):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            if self.path != "/v1/records":
                return self.reply(404, {"error": "POST /v1/records"})
            given = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not hmac.compare_digest(given.encode(), token.encode()):
                return self.reply(401, {"error": "missing or wrong bearer token"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self.reply(413, {"error": f"body over {MAX_BODY} bytes"})
            try:
                items = parse(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return self.reply(400, {"error": f"body is not JSON or NDJSON: {exc}"})
            if not isinstance(items, list):
                return self.reply(400, {"error": "send a JSON array or NDJSON"})
            result = accept(items, repos, sink)
            if result["rejected"]:
                log.warning("ingest rejected %s of %s records", len(result["rejected"]), len(items),
                            extra={"event": "ingest_rejected", "data": {"rejected": result["rejected"][:20]}})
            self.reply(200, result)

        def reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args) -> None:  # noqa: A002, ANN001 - keep request lines off stderr
            pass

    return Handler


def serve(config: Config, sink: Sink) -> None:
    if config.ingest is None:
        raise ValueError("No [ingest] section in cardinal.toml")
    token = os.environ.get(config.ingest.token_env)
    if not token:
        raise ValueError(f"{config.ingest.token_env} is not set; the ingest endpoint will not run without a token")
    host, _, port = config.ingest.bind.rpartition(":")
    server = ThreadingHTTPServer((host, int(port)), handler_for(token, {repo.slug for repo in config.repos}, sink))
    print(json.dumps({"listening": f"http://{host}:{server.server_port}/v1/records"}), flush=True)
    log.info("ingest listening on %s:%s", host, server.server_port, extra={"event": "ingest_started"})
    try:
        server.serve_forever()
    finally:
        server.server_close()
