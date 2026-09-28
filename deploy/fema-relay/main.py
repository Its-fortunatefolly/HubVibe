"""FEMA flood-map relay for property.context.

hazards.fema.gov refuses connections from the VPS's address range, while it
answers Google Cloud. This Cloud Run service forwards exactly one kind of
request -- a point query on the National Flood Hazard Layer's flood-zone
layer -- and only for callers presenting the shared key. It stores
nothing and serves nothing else.
"""

import hmac
import json
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
KEY = os.environ.get("RELAY_KEY", "")
ALLOWED = {"geometry", "geometryType", "inSR", "spatialRel", "returnGeometry", "outFields", "f"}


class Relay(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/healthz":
            return self._send(200, b'{"ok":true}')
        if url.path != "/nfhl":
            return self._send(404, b'{"error":"not found"}')
        if not KEY or not hmac.compare_digest(self.headers.get("X-Relay-Key", ""), KEY):
            return self._send(403, b'{"error":"forbidden"}')
        params = {k: v for k, v in urllib.parse.parse_qsl(url.query) if k in ALLOWED}
        if params.get("geometryType") != "esriGeometryPoint" or "geometry" not in params:
            return self._send(400, b'{"error":"point query only"}')
        request = urllib.request.Request(f"{UPSTREAM}?{urllib.parse.urlencode(params)}",
                                         headers={"User-Agent": "HubVibe Hubvibe@hubvibe-io.com"})
        try:
            with urllib.request.urlopen(request, timeout=20) as upstream:
                return self._send(upstream.status, upstream.read())
        except Exception as exc:  # noqa: BLE001 - report, never crash the relay
            return self._send(502, json.dumps({"error": f"upstream: {type(exc).__name__}"}).encode())

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("", int(os.environ.get("PORT", "8080"))), Relay).serve_forever()
