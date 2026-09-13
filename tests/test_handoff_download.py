import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("handoff_download", Path(__file__).parents[1] / "scripts/sync_cloud_handoff.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DownloadTests(unittest.TestCase):
    def test_real_slow_server_is_killed_and_next_url_succeeds(self):
        content = b"complete pool"
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path == "/slow":
                    time.sleep(1.5)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(content)
                except OSError:
                    pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "pool.gz"
                base = f"http://127.0.0.1:{server.server_port}"
                started = time.monotonic()
                module.download(path, hashlib.sha256(content).hexdigest(), [base + "/slow", base + "/ok"], timeout=0.4, emit=lambda _: None)
                self.assertLess(time.monotonic() - started, 1.4)
                self.assertEqual(path.read_bytes(), content)
        finally:
            server.shutdown()
            server.server_close()

    def test_timeout_and_bad_hash_switch_before_accepting_verified_file(self):
        content = b"verified candidates"
        calls = []
        def execute(command, **kwargs):
            calls.append(command[-1])
            self.assertEqual(kwargs["timeout"], 30)
            self.assertEqual(command[command.index("--max-time") + 1], "30")
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(command, 30)
            Path(command[command.index("--output") + 1]).write_bytes(b"stale" if len(calls) == 2 else content)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        with tempfile.TemporaryDirectory() as folder, patch.object(module.subprocess, "run", side_effect=execute):
            path = Path(folder) / "pool.gz"
            path.write_bytes(b"previous")
            module.download(path, hashlib.sha256(content).hexdigest(), ["mirror1", "mirror2", "official"], emit=lambda _: None)
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(calls, ["mirror1", "mirror2", "official"])
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_failure_preserves_old_pool(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module.subprocess, "run", side_effect=subprocess.TimeoutExpired("curl", 30)):
            path = Path(folder) / "pool.gz"
            path.write_bytes(b"previous")
            with self.assertRaises(RuntimeError):
                module.download(path, "0" * 64, ["bad"], emit=lambda _: None)
            self.assertEqual(path.read_bytes(), b"previous")

    def test_mirror_first_and_official_fallback(self):
        urls = module.sources("owner/repo", "commit")
        self.assertTrue(urls[0].startswith("https://ghfast.top/"))
        self.assertEqual(len(urls), len(set(urls)))
        self.assertTrue(any(u.startswith("https://raw.githubusercontent.com/") for u in urls))
