"""The demo-only mock client-credentials token endpoint for the local demo stack.

This is not an identity provider and must never be used for any real
deployment. HAPI FHIR JPA runs open (no auth enforcement) in this setup, and
src/services/fhir.py never validates the token itself (see fetch_access_token) —
it only requires a 200 response with an "access_token" field. This satisfies
that contract without standing up a real OAuth2 provider (Keycloak, etc.)
for a synthetic local demo.

It binds 127.0.0.1:8081 by default. TOKEN_SERVER_HOST and TOKEN_SERVER_PORT
override the bind; the compose stack sets TOKEN_SERVER_HOST to 0.0.0.0 inside
the container only, and publishes the port on the host loopback.

Each connection runs on its own thread, and a connection that sends no request
line within 10 seconds is dropped. Successful requests are not logged. Errors
(an unsupported method, a timeout, a malformed request) go to stderr. The
server never logs a request body, because the client secret travels in it."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081


class TokenHandler(BaseHTTPRequestHandler):
    # A client that sends no request line within 10 s is dropped, so it cannot hold a worker (11-REVIEW WR-02).
    timeout = 10

    def do_POST(self) -> None:
        body = json.dumps(
            {"access_token": "local-dev-token", "token_type": "bearer", "expires_in": 3600}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # Successful requests, including the compose health probe every 10 s, stay silent.
        # log_error lines (unsupported method, timeout, malformed request) still reach stderr
        # and `docker compose logs token` (11-REVIEW IN-04).
        pass


def resolve_bind(environ: Mapping[str, str]) -> tuple[str, int]:
    host = environ.get("TOKEN_SERVER_HOST", DEFAULT_HOST)
    port = int(environ.get("TOKEN_SERVER_PORT", str(DEFAULT_PORT)))
    return host, port


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), TokenHandler)


def main() -> None:
    host, port = resolve_bind(os.environ)
    server = build_server(host, port)
    print(
        f"demo-only mock OAuth token server listening on {host}:{port} (not an identity provider)",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
