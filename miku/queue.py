from __future__ import annotations

import asyncio
from collections import deque

from .models import Track


class GuildQueue:
    """Thread-safe per-guild queue with 1-based removal positions."""

    def __init__(self):
        self._upcoming: deque[Track] = deque()
        self._history: deque[Track] = deque(maxlen=100)
        self.current: Track | None = None
        self._lock = asyncio.Lock()

    async def enqueue(self, track: Track) -> int:
        async with self._lock:
            self._upcoming.append(track)
            return len(self._upcoming)

    async def enqueue_front(self, track: Track) -> int:
        async with self._lock:
            self._upcoming.appendleft(track)
            return len(self._upcoming)

    async def enqueue_front(self, track: Track) -> int:
        async with self._lock:
            self._upcoming.appendleft(track)
            return len(self._upcoming)

    async def shuffle_upcoming(self) -> int:
        async with self._lock:
            items = list(self._upcoming)
            import random

            random.shuffle(items)
            self._upcoming = deque(items)
            return len(items)

    async def start_next(self) -> Track | None:
        async with self._lock:
            if self.current is not None:
                self._history.append(self.current)
            self.current = self._upcoming.popleft() if self._upcoming else None
            return self.current

    async def prepare_previous(self) -> bool:
        """Replay the last completed track and put the current one at the queue front."""
        async with self._lock:
            if not self._history:
                return False
            previous = self._history.pop()
            if self.current is not None:
                self._upcoming.appendleft(self.current)
            self.current = None
            self._upcoming.appendleft(previous)
            return True

    async def finish_current(self) -> Track | None:
        """Record the active item as history without consuming the next queue item."""
        async with self._lock:
            current = self.current
            if current is not None:
                self._history.append(current)
                self.current = None
            return current

    async def snapshot(self) -> tuple[Track | None, tuple[Track, ...]]:
        async with self._lock:
            return self.current, tuple(self._upcoming)

    async def remove(self, position: int) -> Track:
        async with self._lock:
            if position < 1 or position > len(self._upcoming):
                raise IndexError("queue position is out of range")
            items = list(self._upcoming)
            removed = items.pop(position - 1)
            self._upcoming = deque(items)
            return removed

    async def clear_upcoming(self) -> int:
        async with self._lock:
            count = len(self._upcoming)
            self._upcoming.clear()
            return count

    async def clear_all(self) -> None:
        async with self._lock:
            self._upcoming.clear()
            self.current = None
            self._history.clear()


class QueueManager:
    def __init__(self):
        self._queues: dict[int, GuildQueue] = {}

    def for_guild(self, guild_id: int) -> GuildQueue:
        if guild_id not in self._queues:
            self._queues[guild_id] = GuildQueue()
        return self._queues[guild_id]

    def discard_guild(self, guild_id: int) -> None:
        self._queues.pop(guild_id, None)
