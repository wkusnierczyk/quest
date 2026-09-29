import hashlib
import io
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from quest import downloader
from quest.common import Console

PAYLOAD = os.urandom(3 * 1024 * 1024 + 12345)


class _Handler(BaseHTTPRequestHandler):
    honor_ranges = True
    advertise_ranges = True
    truncate_first_range = False
    truncate_first_full = False
    _truncated = False

    def log_message(self, *args):
        pass

    def _headers(self, status, body_len, extra=None):
        self.send_response(status)
        self.send_header("Content-Length", str(body_len))
        if self.advertise_ranges:
            self.send_header("Accept-Ranges", "bytes")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def do_HEAD(self):
        self._headers(200, len(PAYLOAD))

    def do_GET(self):
        rng = self.headers.get("Range")
        if rng and self.honor_ranges:
            start, end = (int(x) for x in rng.split("=")[1].split("-"))
            body = PAYLOAD[start:end + 1]
            cls = type(self)
            if cls.truncate_first_range and not cls._truncated:
                cls._truncated = True
                self._headers(206, len(body), {"Content-Range": f"bytes {start}-{end}/{len(PAYLOAD)}"})
                self.wfile.write(body[: len(body) // 2])  # then drop the connection
                self.close_connection = True
                return
            self._headers(206, len(body), {"Content-Range": f"bytes {start}-{end}/{len(PAYLOAD)}"})
            self.wfile.write(body)
        else:
            cls = type(self)
            self._headers(200, len(PAYLOAD))
            if cls.truncate_first_full and not cls._truncated:
                cls._truncated = True
                self.wfile.write(PAYLOAD[: len(PAYLOAD) // 3])  # then drop the connection
                self.close_connection = True
                return
            self.wfile.write(PAYLOAD)


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # Clients abort connections on purpose in these tests.


class DownloaderTests(unittest.TestCase):
    def serve(self, **attrs):
        handler = type("H", (_Handler,), dict(attrs))
        server = _QuietServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/file.zip"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.console = Console(stream=io.StringIO())
        self.expected = hashlib.sha256(PAYLOAD).hexdigest()
        self.orig_min = downloader.PARALLEL_MIN_SIZE
        downloader.PARALLEL_MIN_SIZE = 1024  # force the parallel path for a small payload
        self.orig_sleep = downloader.time.sleep
        downloader.time.sleep = lambda _: None

    def tearDown(self):
        downloader.PARALLEL_MIN_SIZE = self.orig_min
        downloader.time.sleep = self.orig_sleep

    def test_parallel_download_verifies_and_renames(self):
        dest = self.tmp / "a.zip"
        downloader.download(self.serve(), dest, self.console, expected_sha256=self.expected)
        self.assertEqual(dest.read_bytes(), PAYLOAD)
        self.assertFalse((self.tmp / "a.zip.part").exists())

    def test_resumes_truncated_segment(self):
        dest = self.tmp / "b.zip"
        downloader.download(self.serve(truncate_first_range=True, _truncated=False), dest, self.console,
                            expected_sha256=self.expected)
        self.assertEqual(dest.read_bytes(), PAYLOAD)

    def test_server_ignoring_ranges_falls_back(self):
        dest = self.tmp / "c.zip"
        downloader.download(self.serve(honor_ranges=False), dest, self.console, expected_sha256=self.expected)
        self.assertEqual(dest.read_bytes(), PAYLOAD)

    def test_no_range_support_uses_single_stream(self):
        dest = self.tmp / "d.zip"
        downloader.download(self.serve(advertise_ranges=False, honor_ranges=False), dest, self.console,
                            expected_sha256=self.expected)
        self.assertEqual(dest.read_bytes(), PAYLOAD)

    def test_single_stream_retries_truncated_response(self):
        dest = self.tmp / "f.zip"
        downloader.download(self.serve(advertise_ranges=False, honor_ranges=False, truncate_first_full=True,
                                       _truncated=False),
                            dest, self.console, expected_sha256=self.expected, threads=1)
        self.assertEqual(dest.read_bytes(), PAYLOAD)

    def test_hash_mismatch_leaves_nothing_behind(self):
        dest = self.tmp / "e.zip"
        with self.assertRaises(downloader.DownloadError):
            downloader.download(self.serve(), dest, self.console, expected_sha256="0" * 64)
        self.assertFalse(dest.exists())
        self.assertFalse((self.tmp / "e.zip.part").exists())


if __name__ == "__main__":
    unittest.main()
