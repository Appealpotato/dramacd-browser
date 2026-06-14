"""Runtime-resizable async concurrency gates for pipeline jobs.

`asyncio.Semaphore` fixes its capacity at construction time, so it can't back
a user-tunable "max N jobs" setting without recreating the object (and orphaning
anyone already waiting on the old one). `DynamicSemaphore` instead reads its
limit fresh on every acquisition from a getter, so changing the setting takes
effect for the *next* job to start — no restart, no object swap.

Lowering the limit never preempts jobs that already hold a slot; it just blocks
new acquisitions until the active count falls back below the new limit. Raising
it immediately wakes waiters.
"""

import asyncio
import inspect
import logging

logger = logging.getLogger(__name__)


async def _resolve(value):
    """Await the value if the getter was async; otherwise return it as-is."""
    if inspect.isawaitable(value):
        return await value
    return value


class DynamicSemaphore:
    def __init__(self, limit_getter, *, name: str = "gate", default: int = 1):
        # limit_getter: callable returning an int (may be a coroutine function).
        self._limit_getter = limit_getter
        self._name = name
        self._default = max(1, int(default))
        self._active = 0
        # Created lazily so the object can be instantiated at import time
        # (before any event loop is running) and bind to the loop on first use.
        self._cond: asyncio.Condition | None = None

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    @property
    def active(self) -> int:
        return self._active

    async def current_limit(self) -> int:
        try:
            n = int(await _resolve(self._limit_getter()))
        except Exception:
            logger.warning("[%s] limit getter failed; falling back to %d", self._name, self._default)
            n = self._default
        return max(1, n)

    async def would_wait(self) -> bool:
        """True if an acquire() right now would block. Used to decide whether
        to mark a job 'queued' before it actually parks on the gate."""
        cond = self._condition()
        async with cond:
            return self._active >= await self.current_limit()

    async def acquire(self) -> None:
        cond = self._condition()
        async with cond:
            while self._active >= await self.current_limit():
                await cond.wait()
            self._active += 1

    async def release(self) -> None:
        cond = self._condition()
        async with cond:
            if self._active > 0:
                self._active -= 1
            # Wake everyone so raising the limit (or freeing a slot) is picked
            # up immediately; each waiter re-checks the current limit itself.
            cond.notify_all()

    async def __aenter__(self) -> "DynamicSemaphore":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()
