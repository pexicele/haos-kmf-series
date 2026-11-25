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
KmfcWriteError = client_module.KmfcWriteError
build_set_capacity_frame = client_module.build_set_capacity_frame
checksum = client_module.checksum
reply_accepted = client_module.reply_accepted

A_FRAME = b":A=4810,567,0,421,3986,100,\n"
C_FRAME = b":C=12,41,\n"

# Same frame with field 5 moved to 11000 tenths, the capacity a 1100 Ah
# battery reports after a successful write.
A_FRAME_1100 = b":A=4810,567,0,421,3986,11000,\n"

# The documented frame for 200.0 Ah: :W28=1,216,2000,
DOC_FRAME = b":W28=1,216,2000,\r\n"


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


def test_frame_matches_the_manual() -> None:
    """The written frame is the one the Juntek manual documents."""
    assert build_set_capacity_frame(200) == DOC_FRAME, (
        build_set_capacity_frame(200)
    )
    # 1100 Ah -> 11000 tenths, 11000 mod 255 is 35, plus one is 36.
    assert build_set_capacity_frame(1100) == b":W28=1,36,11000,\r\n", (
        build_set_capacity_frame(1100)
    )
    assert build_set_capacity_frame(200, 3) == b":W28=3,216,2000,\r\n"
    assert checksum(2000) == 216, checksum(2000)
    print("ok   frame matches the manual: :W28=1,216,2000,")


def test_frame_rejects_bad_values() -> None:
    """Only whole Ah from 1 to 9999 on an address from 1 to 99."""
    for bad in (0, -5, 10000, 2.5, "200", True):
        try:
            build_set_capacity_frame(bad)
        except KmfcWriteError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")
    try:
        build_set_capacity_frame(100, 100)
    except KmfcWriteError:
        pass
    else:
        raise AssertionError("address 100 should have been rejected")
    assert build_set_capacity_frame(1) == b":W28=1,11,10,\r\n"
    assert build_set_capacity_frame(9999).startswith(b":W28=1,")
    print("ok   out of range capacity and address are rejected")


def test_reply_shapes() -> None:
    """Both documented replies count as accepted, a refusal does not."""
    # The module repeats the command: address, checksum, parameter.
    assert reply_accepted(":W28=1,36,11000,", 11000)
    # The module answers with a return code, 0 means accepted.
    assert reply_accepted(":W28=1,0,", 11000)
    assert reply_accepted(":W28=0,0,", 11000)
    # A non zero code, or a parameter that is not ours, is a refusal.
    assert not reply_accepted(":W28=1,2,", 11000)
    assert not reply_accepted(":W28=1,36,2000,", 11000)
    assert not reply_accepted(":W28=1,", 11000)
    assert not reply_accepted("junk", 11000)
    print("ok   reply shapes: echo and return code both accepted")


async def test_set_capacity_round_trip() -> None:
    """set_capacity() writes on the live socket and waits for the echo."""

    async def module(reader, writer):
        writer.write(A_FRAME)
        await writer.drain()
        try:
            while True:
                request = await reader.readuntil(b"\n")
                if b":W28=" in request:
                    writer.write(request)
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(module, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = KmfcClient("127.0.0.1", port, read_timeout=1.0)
    async with server:
        task = asyncio.create_task(client.listen())
        try:
            for _ in range(40):
                await asyncio.sleep(0.05)
                if client.connected:
                    break
            reply = await client.set_capacity(1100)
            assert reply == ":W28=1,36,11000,", reply
            print(f"ok   set_capacity confirmed by the device: {reply}")
        finally:
            # Stop the client even when the assertions failed, otherwise
            # the reader keeps the server from closing and the run hangs.
            await client.stop()
            try:
                await task
            except (ConnectionError, KmfcConnectionError):
                pass
    server.close()
    await server.wait_closed()


async def test_set_capacity_times_out() -> None:
    """A module that ignores the write is reported as a failure."""

    async def silent(reader, writer):
        writer.write(A_FRAME)
        await writer.drain()
        try:
            await reader.read()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = KmfcClient(
        "127.0.0.1", port, read_timeout=1.0, write_timeout=0.5
    )
    async with server:
        task = asyncio.create_task(client.listen())
        try:
            for _ in range(40):
                await asyncio.sleep(0.05)
                if client.connected:
                    break
            try:
                await client.set_capacity(300)
            except KmfcWriteError as err:
                print(f"ok   a silent device raises: {err}")
                assert "Monitor function" in str(err), err
            else:
                raise AssertionError("set_capacity should have raised")
        finally:
            await client.stop()
            try:
                await task
            except (ConnectionError, KmfcConnectionError):
                pass
    server.close()
    await server.wait_closed()


async def test_write_without_reply_is_confirmed_by_the_meter() -> None:
    """A module that forwards the write but never echoes still counts.

    The command is documented over RS-485 and Bluetooth, so a WiFi module
    that passes it through without answering is possible. The meter pushes
    :A= frames every second, and the capacity in them proves the write
    landed.
    """

    async def forward_only(reader, writer):
        writer.write(A_FRAME)
        await writer.drain()
        stored = 100
        try:
            while True:
                request = await reader.readuntil(b"\n")
                if b":W28=" in request:
                    # Accept the value but stay silent about it.
                    stored = int(request.split(b",")[-2])
                if b":A" in request:
                    writer.write(f":A=4810,567,0,421,3986,{stored},\n".encode())
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(forward_only, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = KmfcClient(
        "127.0.0.1",
        port,
        read_timeout=1.0,
        counters_interval=0.3,
        write_timeout=2.0,
    )
    async with server:
        task = asyncio.create_task(client.listen())
        try:
            for _ in range(40):
                await asyncio.sleep(0.05)
                if client.connected:
                    break
            reply = await client.set_capacity(1100)
            assert "1100 Ah" in reply, reply
            print(f"ok   write without a reply confirmed by :A=: {reply}")
        finally:
            await client.stop()
            try:
                await task
            except (ConnectionError, KmfcConnectionError):
                pass
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
        if name.startswith("test_") and callable(func):
            try:
                if asyncio.iscoroutinefunction(func):
                    await func()
                else:
                    func()
            except AssertionError as err:
                failures += 1
                print(f"FAIL {name}: {err}")
    print(f"\n{'FAILED' if failures else 'ALL PASSED'} ({failures} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
