"""Bound synchronous work even when asyncio cancellation cannot stop its thread."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from .errors import ServiceError

class ThreadRunner:
    def __init__(self,limit):
        self.slots=threading.BoundedSemaphore(limit)
        self.pool=ThreadPoolExecutor(max_workers=limit,thread_name_prefix='agent-search')

    async def run(self,function,*args):
        if not self.slots.acquire(blocking=False):
            raise ServiceError('SEARCH_THREADS_BUSY','检索线程仍在处理请求，请稍后重试。',503,True,2)
        def work():
            try:return function(*args)
            finally:self.slots.release()
        try:
            future=self.pool.submit(work)
        except BaseException:
            self.slots.release();raise
        wrapped=asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        return await asyncio.shield(wrapped)

    def close(self):
        self.pool.shutdown(wait=False)
