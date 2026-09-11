from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from comparebot.calibration.labels import delete_label, load_labels, save_label


def serve(cases_dir: Path, labels_path: Path, *, host: str, port: int) -> None:
    handler = _handler(cases_dir, labels_path)
    print(f"Calibration panel: http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), handler).serve_forever()


def _handler(cases_dir: Path, labels_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                content = (
                    files("comparebot.interfaces").joinpath("static/calibration.html").read_bytes()
                )
                self._send(HTTPStatus.OK, content, "text/html; charset=utf-8")
                return
            if path == "/api/cases":
                self._json(HTTPStatus.OK, _cases(cases_dir))
                return
            if path == "/api/labels":
                self._json(HTTPStatus.OK, load_labels(labels_path))
                return
            if path == "/api/candidate-image":
                try:
                    params = parse_qs(parsed.query)
                    url = _candidate_image_url(
                        cases_dir,
                        params.get("product_id", [""])[0],
                        int(params.get("rank", ["0"])[0]),
                    )
                    content, content_type = _download_image(url)
                except (OSError, ValueError, KeyError, IndexError, httpx.HTTPError) as error:
                    self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
                    return
                self._send(HTTPStatus.OK, content, content_type)
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

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            prefix = "/api/labels/"
            if not path.startswith(prefix):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            delete_label(labels_path, unquote(path[len(prefix) :]).strip())
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
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def _candidate_image_url(cases_dir: Path, product_id: str, rank: int) -> str:
    if not product_id or not product_id.isdigit() or not 1 <= rank <= 5:
        raise ValueError("invalid candidate image request")
    payload = json.loads((cases_dir / f"{product_id}.json").read_text(encoding="utf-8"))
    return str(payload["search_and_rank"]["candidates"][rank - 1]["candidate"]["image_url"])


def _download_image(url: str) -> tuple[bytes, str]:
    with httpx.Client(timeout=30, follow_redirects=True, trust_env=False) as client:
        response = client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.1688.com/"},
        )
        response.raise_for_status()
    content_type = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
    if not content_type.startswith("image/") or not response.content:
        raise ValueError("candidate URL did not return an image")
    return response.content, content_type


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
