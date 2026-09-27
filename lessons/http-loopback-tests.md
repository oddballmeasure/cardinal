Local HTTP and Compose E2E checks need loopback access, isolated projects, and fresh port discovery after restarts.

The new service checks failed before reaching application behavior when the
sandbox denied a bind to `127.0.0.1`. Running the same checks with approved
loopback access under the project's Python 3.14 completed them. This matters
because a socket permission error is harness-environment evidence, not a
service failure. Docker-backed acceptance runs also need a unique Compose
project and `down --volumes` cleanup so test invocations do not share Redis
state. Restarting a port-randomized API can invalidate its host URL, so the
fixture waits for Compose health and asks Compose for the mapped port again.
