"""Dependency bulkheads and a generation-fenced closed/open/half-open circuit breaker."""
import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from .errors import ServiceError


class DependencyGuard:
    def __init__(self, limit, threshold, cooldown, slot_timeout):
        self.limit, self.threshold, self.cooldown = limit, threshold, cooldown
        self.slot_timeout = slot_timeout
        self.slots = asyncio.Semaphore(limit)
        self.failures = 0
        self.opened_at = None
        self.probing = False
        self.generation = 0
        self.inflight = 0
        self.waiters = 0

    @property
    def state(self):
        if self.opened_at is None:
            return 'closed'
        return 'half_open' if time.monotonic()-self.opened_at >= self.cooldown else 'open'

    def before(self):
        if self.opened_at is not None:
            if self.state == 'open' or self.probing:
                raise ServiceError('CIRCUIT_OPEN', '外部服务熔断保护中，请稍后重试。', 503, True, max(1,int(self.cooldown)))
            self.probing = True
        return self.generation

    def record(self, generation, failed):
        if generation != self.generation:
            return
        if failed:
            self.failures += 1
            if self.probing or self.failures >= self.threshold:
                self.opened_at = time.monotonic()
                self.probing = False
                self.generation += 1
        elif self.probing:
            self.failures = 0
            self.opened_at = None
            self.probing = False
            self.generation += 1
        elif self.opened_at is None:
            self.failures = 0

    @asynccontextmanager
    async def call(self):
        # Open circuits reject before waiting for a provider slot. Recheck after
        # acquisition as another in-flight request may trip it while we wait.
        if self.opened_at is not None and (self.state=='open' or self.probing):
            self.before()
        # Do not add another unbounded dependency wait queue.
        self.waiters += 1
        try:
            async with asyncio.timeout(self.slot_timeout):
                await self.slots.acquire()
        except TimeoutError:
            raise ServiceError('DEPENDENCY_BUSY', '外部服务并发已满，请稍后重试。', 503, True, 2) from None
        finally:
            self.waiters -= 1
        generation = None
        self.inflight += 1
        try:
            generation = self.before()
            try:
                yield
            except asyncio.CancelledError:
                # Cancellation and client errors are not infrastructure failures.
                if generation == self.generation:
                    self.probing = False
                raise
            except BaseException as error:
                self.record(generation, not isinstance(error,ServiceError) or error.retryable)
                raise
            else:
                self.record(generation, False)
        finally:
            self.inflight -= 1
            self.slots.release()


class GuardRegistry:
    def __init__(self, settings):
        self.settings = settings
        self.guards = {}

    def get(self, name, identity='default'):
        key = (name,hashlib.sha256(identity.encode()).hexdigest())
        if key not in self.guards:
            # Bounded registry. Never evict active guards and bypass their bulkheads.
            if len(self.guards) >= 64:
                idle = next((k for k,v in self.guards.items() if not v.inflight and not v.waiters and v.state=='closed'), None)
                if idle is None:
                    raise ServiceError('DEPENDENCY_BUSY','连接配置过多，请稍后重试。',503,True,5)
                del self.guards[idle]
            limit = self.settings.SEARCH_MAX_CONCURRENT if name=='search' else self.settings.LLM_MAX_CONCURRENT
            self.guards[key] = DependencyGuard(limit,self.settings.CIRCUIT_FAILURE_THRESHOLD,
                self.settings.CIRCUIT_OPEN_SECONDS,self.settings.DEPENDENCY_SLOT_TIMEOUT_SECONDS)
        return self.guards[key]

    def snapshot(self):
        return [{'dependency':name, 'identity':identity[:12], 'state':guard.state,
                 'failures':guard.failures, 'inflight':guard.inflight, 'waiters':guard.waiters, 'limit':guard.limit}
                for (name,identity),guard in self.guards.items()]
