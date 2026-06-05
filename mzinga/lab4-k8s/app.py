import json
import os
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer

VERSION = os.getenv("APP_VERSION", "1.0.0")
APP_COLOR = os.getenv("APP_COLOR", "blue")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default Apache-style access log

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        elif self.path == "/":
            self._respond(200, {
                "version": VERSION,
                "color": APP_COLOR,
                "hostname": socket.gethostname(),
                "message": f"Hello from version {VERSION}",
            })
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("", port), Handler)
    print(f"Listening on :{port} — version={VERSION} color={APP_COLOR}", flush=True)
    server.serve_forever()