"""Runtime objects belong to an event loop, never to a request or SQLAlchemy session."""
import asyncio
import weakref
from agent_system.config.settings import get_settings
from .scheduler import Scheduler
from .resilience import GuardRegistry
from .threads import ThreadRunner

_runtimes = weakref.WeakKeyDictionary()

class Runtime:
    def __init__(self, settings):
        self.scheduler = Scheduler(settings)
        self.guards = GuardRegistry(settings)
        self.catalog_lock = asyncio.Lock()
        self.catalog = {}
        self.http_requests = 0
        self.persistence_failures = 0
        self.runs = {}
        self.search_threads = ThreadRunner(settings.SEARCH_MAX_CONCURRENT)


def get_runtime():
    loop = asyncio.get_running_loop()
    if loop not in _runtimes:
        _runtimes[loop] = Runtime(get_settings())
    return _runtimes[loop]
