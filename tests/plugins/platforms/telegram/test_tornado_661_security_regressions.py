"""Security regressions for the Tornado webhook dependency used by Telegram.

These exercise Tornado's real HTTP stack rather than Hermes mocks.  The
Telegram adapter reaches this stack through ``Updater.start_webhook``.
"""

from __future__ import annotations

import tracemalloc
import urllib.parse

import pytest
from tornado.escape import parse_qs_bytes
from tornado.testing import AsyncHTTPTestCase
from tornado.web import (
    Application,
    RedirectHandler,
    RequestHandler,
    addslash,
    removeslash,
)


class _RemoveSlashHandler(RequestHandler):
    @removeslash
    def get(self) -> None:
        self.write("ok")


class _AddSlashHandler(RequestHandler):
    @addslash
    def get(self) -> None:
        self.write("ok")


class _RawRedirectHandler(RequestHandler):
    def get(self) -> None:
        self.set_status(302)
        self.set_header("Location", self.get_argument("url"))


class TestTornadoRedirectSecurity(AsyncHTTPTestCase):
    def get_app(self) -> Application:
        return Application(
            [
                (r"/+rem.*", _RemoveSlashHandler),
                (r"/+add.*", _AddSlashHandler),
                (r"/a/(.*)/b", RedirectHandler, {"url": "/{0}/done"}),
                (r"/raw-redirect", _RawRedirectHandler),
            ]
        )

    def test_addslash_and_removeslash_reject_protocol_relative_redirects(
        self,
    ) -> None:
        for path in [
            "//add.example.com",
            "///add.example.com",
            "//rem.example.com/",
            "//rem.example.com//",
            "///rem.example.com/x/",
        ]:
            with self.subTest(path=path):
                response = self.fetch(path, follow_redirects=False)
                assert response.code == 403
                assert "Location" not in response.headers

    def test_redirect_handler_rejects_triple_slash_substitutions(self) -> None:
        for path in [
            "/a///example.com/b",
            "/a////example.com/b",
            "/a/%2F%2F%5Cexample.com/b",
        ]:
            with self.subTest(path=path):
                response = self.fetch(path, follow_redirects=False)
                assert response.code == 400
                assert "Location" not in response.headers

    def test_client_rejects_non_http_redirect_without_hanging(self) -> None:
        for location in ["ftp://example.com/", "h:5"]:
            with self.subTest(location=location):
                target = self.get_url(
                    "/raw-redirect?url=" + urllib.parse.quote(location)
                )
                with pytest.raises(ValueError, match="Unsupported url scheme"):
                    self.io_loop.run_sync(
                        lambda: self.http_client.fetch(target), timeout=1.0
                    )


def test_long_percent_encoded_query_has_bounded_memory() -> None:
    encoded = b"%41" * 100_000
    tracemalloc.start()
    try:
        parsed = parse_qs_bytes(b"value=" + encoded)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert parsed == {"value": [b"A" * 100_000]}
    assert peak <= len(encoded) * 12
