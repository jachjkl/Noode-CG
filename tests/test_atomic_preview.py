import tempfile
import unittest
import os
import threading
from pathlib import Path
from unittest.mock import patch

from core.io_utils import atomic_write_bytes
from core.handoff import _write_handoff


class AtomicPreviewTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows delete-sharing semantics")
    def test_real_windows_reader_lock_is_released_before_retry(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.gz"
            path.write_bytes(b"old")
            handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            release = threading.Timer(0.08, kernel.CloseHandle, args=(handle,))
            release.start()
            try:
                atomic_write_bytes(path, b"new complete preview")
                self.assertEqual(path.read_bytes(), b"new complete preview")
            finally:
                release.join()

    def test_transient_windows_lock_retries_without_losing_content(self):
        import os
        replace = os.replace
        calls = 0
        def locked_once(src, dst):
            nonlocal calls
            calls += 1
            if calls < 3:
                error = PermissionError("reader holds file")
                error.winerror = 5
                raise error
            return replace(src, dst)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview.gz"
            with patch("core.io_utils.os.replace", side_effect=locked_once):
                atomic_write_bytes(path, b"complete")
            self.assertEqual(path.read_bytes(), b"complete")
            self.assertEqual(calls, 3)

    def test_preview_failure_is_nonfatal_but_publish_payload_failure_is_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.gz"
            with patch("core.handoff.atomic_write_bytes", side_effect=PermissionError("locked")):
                self.assertFalse(_write_handoff(path, [], {}, best_effort=True))
                with self.assertRaises(PermissionError):
                    _write_handoff(path, [], {})
