from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


async def run_worker_pool(
    items: Sequence[T],
    worker: Callable[[T], Awaitable[R]],
    concurrency: int,
    *,
    progress_every: int = 0,
    progress_label: str = "",
) -> list[R]:
    if not items:
        return []
    queue: asyncio.Queue[tuple[int, T] | None] = asyncio.Queue()
    results: list[R | None] = [None] * len(items)
    completed = 0
    lock = asyncio.Lock()

    for index, item in enumerate(items):
        queue.put_nowait((index, item))

    worker_count = min(max(1, int(concurrency)), len(items))
    for _ in range(worker_count):
        queue.put_nowait(None)

    async def consume() -> None:
        nonlocal completed
        while True:
            queued = await queue.get()
            try:
                if queued is None:
                    return
                index, item = queued
                results[index] = await worker(item)
                if progress_every:
                    async with lock:
                        completed += 1
                        if completed % progress_every == 0 or completed == len(items):
                            print(f"[{progress_label}] {completed}/{len(items)}", flush=True)
            finally:
                queue.task_done()

    tasks = [asyncio.create_task(consume()) for _ in range(worker_count)]
    await queue.join()
    await asyncio.gather(*tasks)
    return [result for result in results if result is not None]
