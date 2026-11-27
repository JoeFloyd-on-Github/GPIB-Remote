"""Bus worker: serialises every operation on one GPIB board through a single thread.

GPIB is half-duplex and linux-gpib calls block, so each board gets one worker thread and
asyncio code awaits its results. A write+read pair submitted as one call is atomic.
"""

import asyncio
import concurrent.futures


class Bus:
    def __init__(self, backend):
        self.backend = backend
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="gpib%d" % backend.board)

    async def call(self, method, *args, **kwargs):
        fn = getattr(self.backend, method)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: fn(*args, **kwargs))

    def close(self):
        self._pool.submit(self.backend.close).result(timeout=10)
        self._pool.shutdown(wait=True)
