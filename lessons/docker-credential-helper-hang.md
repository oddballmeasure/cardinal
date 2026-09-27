A hung Docker Desktop credential helper looks like a failing test suite: builds die with `DeadlineExceeded` on the `FROM` line.

On 2026-09-26 a live smoke run's coder passed the notes repo's tests, then minutes later the
verifier's run of the same commit failed: every HTTP test errored because
`docker compose up --build` exited 1. By hand the build showed
`failed to solve: DeadlineExceeded` on `FROM python:3.13-slim`. The host reached Docker Hub fine
(token 0.09s, manifest 0.12s, layer 0.24s); even `docker pull hello-world` hung. The cause was
`docker-credential-desktop get` never returning. Ten stuck copies had piled up.

The verifier correctly refused to approve, then the repair round sent a coder chasing an
environment fault. Before blaming code for a Compose failure, run `docker pull hello-world`
with a bound. Until Docker Desktop is restarted, the harness can run with `DOCKER_CONFIG`
pointing at a config with no `credsStore` (public images pull anonymously).
