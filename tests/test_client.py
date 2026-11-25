"""Checks for the KM-F asyncio client.

Runs against a fake WiFi module listening on localhost, no Home Assistant
needed:

    python3 tests/test_client.py
"""

from __future__ import annotations

import asyncio

import _loader

client_module = _loader.load("client")
KmfcClient = client_module.KmfcClient
KmfcConnectionError = client_module.KmfcConnectionError

A_FRAME = b":A=4810,567,0,421,3986,100,\n"
C_FRAME = b":C=12,41,\n"


async def fake_module(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Push one frame, then answer the :C request, then hang up."""
    writer.write(A_FRAME)
    await writer.drain()
    try:
        while True:
            request = await reader.read(64)
            if not request:
                break
            if b":C" in request:
                writer.write(C_FRAME)
                await writer.drain()
            if b":A" in request:
                writer.write(A_FRAME)
                await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def test_probe() -> None:
    """probe() returns the first frame and frees the socket."""
    server = await asyncio.start_server(fake_module, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = KmfcClient("127.0.0.1", port, read_timeout=5.0)
    async with server:
        frame = await client.probe()
        assert frame["voltage"] == 48.1, frame
        assert frame["current"] == -0.567, frame
        print(f"ok   probe: {frame}")
        assert not client.connected
        # A second client can still connect after probe released the socket.
        frame2 = await KmfcClient("127.0.0.1", port, read_timeout=5.0).probe()
        assert frame2["voltage"] == 48.1, frame2
        print("ok   probe releases the socket for the next client")
    server.close()
    await server.wait_closed()


async def test_listen() -> None:
    """listen() keeps merging pushed frames and answers :C polls."""
    server = await asyncio.start_server(fake_module, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    seen: list[dict] = []
    states: list[bool] = []
    client = KmfcClient(
        "127.0.0.1",
        port,
        read_timeout=1.0,
        counters_interval=1.0,
        on_data=seen.append,
        on_connection_change=states.append,
    )
    async with server:
        task = asyncio.create_task(client.listen())
        for _ in range(80):
            await asyncio.sleep(0.05)
            if "energy_charged" in client.last_frame:
                break
        await client.stop()
        try:
            await task
        except (ConnectionError, KmfcConnectionError):
            pass

    assert client.last_frame.get("voltage") == 48.1, client.last_frame
    assert client.last_frame.get("energy_charged") == 0.012, client.last_frame
    assert len(seen) >= 2, seen
    assert states[0] is True, states
    print(f"ok   listen merged frames: {client.last_frame}")
    print(f"ok   callbacks: {len(seen)} data, {len(states)} state changes")
    server.close()
    await server.wait_closed()


async def test_refuses_dead_host() -> None:
    """A host nobody listens on raises, it does not hang."""
    client = KmfcClient("127.0.0.1", 1, connect_timeout=2.0, read_timeout=2.0)
    try:
        await client.probe()
    except KmfcConnectionError as err:
        print(f"ok   dead host raises: {type(err).__name__}")
        return
    raise AssertionError("probe() should have raised KmfcConnectionError")


async def main() -> int:
    """Run every client check."""
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and asyncio.iscoroutinefunction(func):
            try:
                await func()
            except AssertionError as err:
                failures += 1
                print(f"FAIL {name}: {err}")
    print(f"\n{'FAILED' if failures else 'ALL PASSED'} ({failures} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
