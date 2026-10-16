import asyncio

from zip_service.pipeline import OutputBuffer


def test_put_blocks_when_full_and_resumes_after_get() -> None:
    async def scenario() -> None:
        buffer = OutputBuffer(max_bytes=10)
        await buffer.put(b"0123456789")  # fills the buffer exactly
        blocked = asyncio.create_task(buffer.put(b"more"))
        await asyncio.sleep(0.01)
        assert not blocked.done(), "put must apply backpressure when the buffer is full"
        assert await buffer.get() == b"0123456789"
        await asyncio.wait_for(blocked, 1)
        assert buffer.size == 4

    asyncio.run(scenario())


def test_single_chunk_may_exceed_limit_when_below_it() -> None:
    async def scenario() -> None:
        buffer = OutputBuffer(max_bytes=10)
        await buffer.put(b"abc")
        await asyncio.wait_for(buffer.put(b"x" * 50), 1)
        assert buffer.size == 53

    asyncio.run(scenario())


def test_get_returns_none_after_close_and_drain() -> None:
    async def scenario() -> None:
        buffer = OutputBuffer(max_bytes=10)
        await buffer.put(b"a")
        await buffer.put(b"")  # ignored
        await buffer.close()
        assert await buffer.get() == b"a"
        assert await buffer.get() is None

    asyncio.run(scenario())


def test_get_waits_for_data() -> None:
    async def scenario() -> None:
        buffer = OutputBuffer(max_bytes=10)
        waiting = asyncio.create_task(buffer.get())
        await asyncio.sleep(0.01)
        assert not waiting.done()
        await buffer.put(b"z")
        assert await asyncio.wait_for(waiting, 1) == b"z"

    asyncio.run(scenario())
