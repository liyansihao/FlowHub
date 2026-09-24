import argparse
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from comparebot.interfaces import cli


def test_rank_source_image_uses_explicit_proxy(monkeypatch, tmp_path):
    seen = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"image-bytes")

        def log_message(self, *_args):
            pass

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("FLOWHUB_REVIEW_IMAGE_PROXY", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.setattr(cli, "DinoV2Ranker", lambda **_kwargs: object())
        monkeypatch.setattr(cli, "Alibaba1688ImageSearchAdapter", lambda: object())

        class CheckedImageRoute:
            def __init__(self, _search, image_loader, _ranker):
                self.image_loader = image_loader

            async def run(self, query, *, top_k):
                assert await self.image_loader.load(query.image_url) == b"image-bytes"
                raise StopAfterImage

        class StopAfterImage(Exception):
            pass

        monkeypatch.setattr(cli, "SearchAndRankService", CheckedImageRoute)
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps([{
            "product_id": "one",
            "title": "source",
            "image_url": "http://ir.ozone.invalid/source.jpg",
        }]))
        args = argparse.Namespace(manifest=manifest, product_id="one", device="cpu", top_k=1)
        with pytest.raises(StopAfterImage):
            asyncio.run(cli._run(args))
        assert seen == ["http://ir.ozone.invalid/source.jpg"]
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=2)
