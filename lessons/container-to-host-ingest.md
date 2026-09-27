A container reaches a host service bound to 127.0.0.1 through `host.docker.internal` on Docker Desktop, so ingest need not listen on every interface.

Checked on 2026-09-27: `cardinal ingest` bound to `127.0.0.1:0`, the test repo's API container posted
to `http://host.docker.internal:<port>/v1/records`, and the record arrived. The test repo's
`compose.yaml` also maps `host.docker.internal:host-gateway` for Linux Docker, where this has not
been checked. The live propagation run depends on this path.
