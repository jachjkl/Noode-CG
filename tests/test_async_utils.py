from __future__ import annotations

import asyncio
import unittest

from core.async_utils import run_worker_pool


class AsyncUtilsTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_exception_is_raised_without_queue_deadlock(self) -> None:
        async def worker(value: int) -> int:
            if value == 1:
                raise RuntimeError("probe failed")
            await asyncio.sleep(0)
            return value

        with self.assertRaisesRegex(RuntimeError, "probe failed"):
            await asyncio.wait_for(
                run_worker_pool([1, 2, 3], worker, concurrency=2),
                timeout=1,
            )


if __name__ == "__main__":
    unittest.main()
