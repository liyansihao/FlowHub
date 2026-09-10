from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from comparebot.calibration.labels import load_labels, save_label


def serve(cases_dir: Path, labels_path: Path, *, host: str, port: int) -> None:
    handler = _handler(cases_dir, labels_path)
    print(f"Calibration panel: http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), handler).serve_forever()


def _handler(cases_dir: Path, labels_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                content = (
                    files("comparebot.interfaces")
                    .joinpath("static/calibration.html")
                    .read_bytes()
                )
                self._send(HTTPStatus.OK, content, "text/html; charset=utf-8")
                return
            if path == "/api/cases":
                self._json(HTTPStatus.OK, _cases(cases_dir))
                return
            if path == "/api/labels":
                self._json(HTTPStatus.OK, load_labels(labels_path))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            prefix = "/api/labels/"
            if not path.startswith(prefix):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            product_id = unquote(path[len(prefix) :]).strip()
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                save_label(labels_path, product_id, payload)
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            self._json(HTTPStatus.OK, {"ok": True})

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            self._send(
                status,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )

        def _send(self, status: HTTPStatus, content: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    return Handler


def _cases(cases_dir: Path) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(cases_dir.glob("*.json")):
        if path.name.endswith(".error.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            ranked = payload["search_and_rank"]["candidates"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if ranked:
            cases.append(payload)
    return cases
