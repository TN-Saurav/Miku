import pytest

from miku.models import Track
from miku.queue import QueueManager


@pytest.mark.asyncio
async def test_guild_queue_operations_are_ordered_and_positions_are_one_based():
    queue = QueueManager().for_guild(1)
    first = Track("local", "1", "First")
    second = Track("local", "2", "Second")
    third = Track("local", "3", "Third")
    await queue.enqueue(first)
    await queue.enqueue(second)
    await queue.enqueue(third)
    assert await queue.start_next() == first
    assert (await queue.snapshot()) == (first, (second, third))
    assert await queue.remove(2) == third
    assert await queue.clear_upcoming() == 1
    assert await queue.snapshot() == (first, ())
    await queue.clear_all()
    assert await queue.snapshot() == (None, ())


@pytest.mark.asyncio
async def test_queue_invalid_positions_are_rejected():
    queue = QueueManager().for_guild(1)
    with pytest.raises(IndexError):
        await queue.remove(1)
    await queue.enqueue(Track("local", "1", "First"))
    with pytest.raises(IndexError):
        await queue.remove(0)


@pytest.mark.asyncio
async def test_previous_replays_last_completed_track_and_puts_current_back_at_front():
    queue = QueueManager().for_guild(10)
    first = Track("local", "1", "First")
    second = Track("local", "2", "Second")
    third = Track("local", "3", "Third")
    for track in (first, second, third):
        await queue.enqueue(track)

    assert await queue.start_next() == first
    assert await queue.start_next() == second
    assert await queue.prepare_previous() is True
    assert await queue.snapshot() == (None, (first, second, third))
    assert await queue.start_next() == first
    assert await queue.start_next() == second


@pytest.mark.asyncio
async def test_queue_history_is_isolated_between_guilds():
    queues = QueueManager()
    first_guild = queues.for_guild(1)
    second_guild = queues.for_guild(2)
    await first_guild.enqueue(Track("local", "1", "Guild one"))
    await first_guild.start_next()

    assert await first_guild.snapshot() == (Track("local", "1", "Guild one"), ())
    assert await second_guild.snapshot() == (None, ())
