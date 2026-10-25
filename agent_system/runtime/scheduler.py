"""Bounded per-process admission and round-robin scheduling. One event-loop owner."""
import asyncio
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from .errors import ServiceError


@dataclass(eq=False)
class Ticket:
    owner: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ready: bool = False
    running: bool = False
    released: bool = False
    created: float = field(default_factory=time.monotonic)
    grant: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class Scheduler:
    def __init__(self, settings):
        self.settings = settings
        self.tickets = []
        self.rotation = deque()
        self.active = Counter()
        self.rates = {}
        self.tasks = set()
        self.closing = False
        self.last_owner = None
        self.accepted = self.rejected = self.released = 0

    def admit(self, owner):
        if self.closing:
            raise ServiceError('SERVICE_DRAINING', '服务正在关闭，请稍后重试。', retryable=True, retry_after=5)
        s = self.settings
        count = sum(t.owner == owner for t in self.tickets)
        if count >= s.USER_MAX_CONCURRENT + s.USER_MAX_QUEUED:
            self.rejected += 1
            raise ServiceError('USER_CAPACITY_EXCEEDED', '你的运行及排队任务已达上限。', 429, True, 5)
        if len(self.tickets) >= s.AGENT_MAX_CONCURRENT + s.AGENT_MAX_QUEUED:
            self.rejected += 1
            raise ServiceError('QUEUE_FULL', '任务队列已满，请稍后重试。', 429, True, 5)
        now = time.monotonic()
        self.rates = {k:v for k,v in self.rates.items() if now-v[0] < 60}
        start, used = self.rates.get(owner, (now, 0))
        if used >= s.USER_REQUESTS_PER_MINUTE:
            self.rejected += 1
            raise ServiceError('USER_RATE_LIMITED', '请求过于频繁，请稍后重试。', 429, True, max(1,int(60-now+start)))
        self.rates[owner] = (start, used+1)
        ticket = Ticket(owner)
        self.tickets.append(ticket)
        if owner not in self.rotation:
            self.rotation.append(owner)
        self.accepted += 1
        return ticket

    def _pump(self):
        while not self.closing and sum(self.active.values()) < self.settings.AGENT_MAX_CONCURRENT:
            selected = None
            if self.last_owner in self.rotation and len(self.rotation)>1:
                index=self.rotation.index(self.last_owner)
                self.rotation.rotate(-index-1)
            for _ in range(len(self.rotation)):
                owner = self.rotation[0]
                self.rotation.rotate(-1)
                if self.active[owner] >= self.settings.USER_MAX_CONCURRENT:
                    continue
                selected = next((t for t in self.tickets if t.owner == owner and t.ready and not t.running), None)
                if selected:
                    break
            if selected is None:
                return
            self.last_owner = selected.owner
            selected.running = True
            self.active[selected.owner] += 1
            selected.grant.set_result(None)

    async def wait(self, ticket):
        ticket.ready = True
        self._pump()
        remaining = self.settings.AGENT_QUEUE_TIMEOUT_SECONDS-(time.monotonic()-ticket.created)
        try:
            async with asyncio.timeout(max(0,remaining)):
                await asyncio.shield(ticket.grant)
        except TimeoutError:
            raise ServiceError('QUEUE_TIMEOUT', '排队等待超时，请稍后重试。', 503, True, 5) from None

    def release(self, ticket):
        if ticket.released:
            return
        ticket.released = True
        if ticket.running:
            self.active[ticket.owner] -= 1
            if not self.active[ticket.owner]:
                del self.active[ticket.owner]
        if ticket in self.tickets:
            self.tickets.remove(ticket)
        if not ticket.grant.done():
            ticket.grant.cancel()
        if not any(t.owner == ticket.owner for t in self.tickets) and ticket.owner in self.rotation:
            self.rotation.remove(ticket.owner)
        self.released += 1
        self._pump()

    def track(self, task):
        self.tasks.add(task)
        def done(value):
            self.tasks.discard(value)
            # Always consume unexpected failures, even if the HTTP client disconnected.
            if not value.cancelled():
                value.exception()
        task.add_done_callback(done)
        return task

    def snapshot(self):
        return {'active':sum(self.active.values()), 'queued':sum(not t.running for t in self.tickets),
                'capacity':self.settings.AGENT_MAX_CONCURRENT, 'queue_capacity':self.settings.AGENT_MAX_QUEUED,
                'accepted':self.accepted, 'rejected':self.rejected, 'released':self.released,
                'draining':self.closing}

    async def close(self):
        self.closing = True
        if self.tasks:
            _,pending = await asyncio.wait(self.tasks, timeout=self.settings.AGENT_SHUTDOWN_GRACE_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
