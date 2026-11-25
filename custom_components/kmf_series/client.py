"""Asyncio TCP client for the Junctek KM-F WiFi module.

The module does not answer a request/response exchange in a useful way: it
pushes :A= frames on its own and only emits :C= totals when it is asked. The
client therefore keeps one long lived connection open, asks for the counters
from time to time, and pushes a frame request whenever the socket goes quiet,
which doubles as a keep alive.

Observed on a KM-F unit and on the KM105F / KM140F reports published by the
community: a single client is served at a time, so Home Assistant must own
the connection. Reconnect is left to Home Assistant by raising
ConnectionError out of listen().
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable

from .parser import parse_line

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 8899
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 20.0
DEFAULT_COUNTERS_INTERVAL = 30.0
MAX_LINE_LENGTH = 512

REQUEST_A = b":A\n"
REQUEST_C = b":C\n"

# Function 28 sets the battery capacity. Same numbering as the RS-485 and
# Bluetooth protocols of the meter, which the WiFi module forwards.
SET_CAPACITY_FUNCTION = 28
CAPACITY_PARAM_SCALE = 10
MIN_CAPACITY_AH = 1
MAX_CAPACITY_AH = 9999
MIN_ADDRESS = 1
MAX_ADDRESS = 99
DEFAULT_ADDRESS = 1
DEFAULT_WRITE_TIMEOUT = 5.0

WRITE_REPLY_RE = re.compile(r"^:[wW](\d{2})=")

OnDataCallback = Callable[[dict], None]
OnConnectionChangeCallback = Callable[[bool], None]


class KmfcError(Exception):
    """Base error for the KM-F client."""


class KmfcConnectionError(KmfcError):
    """Raised when the socket cannot be established or drops out."""


class KmfcWriteError(KmfcError):
    """Raised when the device does not accept a written parameter."""


def checksum(parameter: int) -> int:
    """Checksum of the Juntek command line: remainder of 255, plus one."""
    return (parameter % 255) + 1


def build_write_frame(function: int, address: int, parameter: int) -> bytes:
    """Build one Juntek write line, CRLF terminated.

    Layout: SOM, function code W, function number, =, address, comma,
    checksum, comma, data, comma, CRLF.
    """
    return (
        f":W{function:02d}={address},{checksum(parameter)},{parameter},\r\n"
    ).encode("ascii")


def build_set_capacity_frame(
    capacity_ah: int, address: int = DEFAULT_ADDRESS
) -> bytes:
    """Build the function 28 frame that sets the battery capacity.

    The capacity travels in tenths of an Ah, so 200 Ah becomes 2000 and the
    documented frame ``:W28=1,216,2000,``. Whole Ah between 1 and 9999 only,
    which is what the meter itself accepts.
    """
    if isinstance(capacity_ah, bool) or not isinstance(capacity_ah, int):
        raise KmfcWriteError(
            f"Capacity must be a whole number of Ah, got {capacity_ah!r}"
        )
    if not MIN_CAPACITY_AH <= capacity_ah <= MAX_CAPACITY_AH:
        raise KmfcWriteError(
            f"Capacity must be between {MIN_CAPACITY_AH} and "
            f"{MAX_CAPACITY_AH} Ah, got {capacity_ah}"
        )
    if not MIN_ADDRESS <= address <= MAX_ADDRESS:
        raise KmfcWriteError(
            f"Address must be between {MIN_ADDRESS} and "
            f"{MAX_ADDRESS}, got {address}"
        )
    return build_write_frame(
        SET_CAPACITY_FUNCTION, address, capacity_ah * CAPACITY_PARAM_SCALE
    )


def is_reply_for(line: str, function: int) -> bool:
    """True when a stream line is the reply to one of our write commands."""
    match = WRITE_REPLY_RE.match(line.strip())
    return bool(match) and int(match.group(1)) == function


def _reply_fields(line: str) -> list[int | None]:
    """The comma separated numbers after the ``=``, None where not a number.

    A write reply is ``:W28=<address>,<code>,<echo>,``. The second field is
    the return code when the module answers with one and the checksum echo
    when it answers by repeating the command, so both readings are kept and
    the caller decides which one applies.
    """
    head, _, tail = line.strip().partition("=")
    fields: list[int | None] = []
    for chunk in tail.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            fields.append(int(chunk))
        except ValueError:
            fields.append(None)
    return fields


def reply_accepted(line: str, wanted_parameter: int) -> bool:
    """True when a write reply confirms the value that was written.

    Two reply shapes are documented and in the wild: the module answers with
    a return code, where ``0`` in the second field means accepted, or it
    repeats the command, where the second field is the checksum and the
    third is the parameter that was stored. Anything else, an error code or
    a different parameter, is a refusal.
    """
    fields = _reply_fields(line)
    if len(fields) < 2:
        return False
    if fields[1] == 0:
        return True
    return len(fields) >= 3 and fields[2] == wanted_parameter


class KmfcClient:
    """Long lived, push oriented reader for one KM-F device."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        counters_interval: float = DEFAULT_COUNTERS_INTERVAL,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
        on_data: OnDataCallback | None = None,
        on_connection_change: OnConnectionChangeCallback | None = None,
    ) -> None:
        """Store the endpoint and the callbacks used while streaming."""
        self.host = host
        self.port = port
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._counters_interval = counters_interval
        self._write_timeout = write_timeout
        self._reply_waiter: asyncio.Future | None = None
        self._on_data = on_data
        self._on_connection_change = on_connection_change
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._last_frame: dict = {}
        self._closing = False

    @property
    def connected(self) -> bool:
        """True while a socket is open and streaming."""
        return self._connected

    @property
    def last_frame(self) -> dict:
        """The most recent successfully parsed frame of this session."""
        return self._last_frame

    async def probe(self) -> dict:
        """Open, wait for the first frame, close. Used by the config flow.

        Returns the first parsed frame. Raises KmfcConnectionError when the
        device cannot be reached or stays silent.
        """
        reader, writer = await self._open()
        try:
            return await self._read_first_frame(reader)
        finally:
            await self._close_writer(writer)
            await self._notify(False)

    async def listen(self) -> None:
        """Stream frames until the device drops out or the task is cancelled.

        Raises ConnectionError when the link is lost so the caller, normally
        an update coordinator, can mark the device unavailable and retry on
        its own schedule.
        """
        self._closing = False
        reader, writer = await self._open()
        self._writer = writer
        await self._notify(True)
        loop = asyncio.get_running_loop()
        next_counters = loop.time() + self._counters_interval
        buffer = b""
        try:
            while True:
                timeout = self._read_timeout
                if self._counters_interval > 0:
                    timeout = min(
                        timeout, max(1.0, next_counters - loop.time())
                    )
                try:
                    chunk = await asyncio.wait_for(
                        reader.readuntil(b"\n"), timeout
                    )
                except asyncio.TimeoutError:
                    await self._send(writer, REQUEST_A)
                    continue
                except (
                    asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError,
                    ConnectionError,
                    OSError,
                ) as err:
                    raise ConnectionError(f"KM-F link lost: {err}") from err

                if self._counters_interval > 0 and loop.time() >= next_counters:
                    await self._send(writer, REQUEST_C)
                    next_counters = loop.time() + self._counters_interval

                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if len(line) > MAX_LINE_LENGTH:
                        _LOGGER.debug(
                            "Dropping oversized line: %d bytes", len(line)
                        )
                        continue
                    text = line.decode("ascii", errors="replace")
                    data = parse_line(text)
                    if not data:
                        self._dispatch_reply(text)
                        continue
                    self._last_frame.update(data)
                    if self._on_data is not None:
                        self._on_data(dict(self._last_frame))
        finally:
            self._writer = None
            await self._close_writer(writer)
            if not self._closing:
                await self._notify(False)

    async def set_capacity(
        self, capacity_ah: int, address: int = DEFAULT_ADDRESS
    ) -> str:
        """Write the battery capacity and wait for the device to confirm.

        The write goes out on the connection the reader already owns, the
        same way the vendor app writes over Bluetooth. The module answers a
        write with ``:W28=<address>,<checksum>,<value>,``, so the reply is
        matched on the function number and on the echoed value.

        Returns the reply line. Raises KmfcWriteError when the link is down,
        the module stays silent or it echoes something else than what was
        written.
        """
        writer = self._writer
        if writer is None:
            raise KmfcWriteError(
                f"No open link to KM-F at {self.host}:{self.port}"
            )
        frame = build_set_capacity_frame(capacity_ah, address)
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._reply_waiter = waiter
        try:
            try:
                await self._send(writer, frame)
            except ConnectionError as err:
                raise KmfcWriteError(str(err)) from err
            try:
                reply = await asyncio.wait_for(waiter, self._write_timeout)
            except asyncio.TimeoutError as err:
                raise KmfcWriteError(
                    f"KM-F at {self.host}:{self.port} did not answer "
                    f"{frame!r} within {self._write_timeout:g} s"
                ) from err
        finally:
            self._reply_waiter = None

        wanted = capacity_ah * CAPACITY_PARAM_SCALE
        if not reply_accepted(reply, wanted):
            raise KmfcWriteError(
                f"KM-F at {self.host}:{self.port} refused the capacity, "
                f"it answered {reply!r}"
            )
        _LOGGER.debug("KM-F accepted %r with %r", frame, reply)
        return reply

    def _dispatch_reply(self, line: str) -> None:
        """Hand a write reply to whoever is waiting for one."""
        waiter = self._reply_waiter
        if waiter is None or waiter.done() or not is_reply_for(
            line, SET_CAPACITY_FUNCTION
        ):
            return
        waiter.set_result(line.strip())

    async def stop(self) -> None:
        """Break the listen loop without reporting an unexpected drop."""
        self._closing = True
        writer = self._writer
        self._writer = None
        if writer is None:
            self._connected = False
            return
        await self._close_writer(writer)
        await self._notify(False)

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect to the WiFi module."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                self._connect_timeout,
            )
        except (TimeoutError, OSError, ValueError) as err:
            raise KmfcConnectionError(
                f"Cannot reach KM-F at {self.host}:{self.port}: {err}"
            ) from err
        self._connected = True
        return reader, writer

    async def _close_writer(self, writer: asyncio.StreamWriter) -> None:
        """Close the transport; teardown errors are not worth propagating."""
        self._connected = False
        try:
            writer.close()
        except (ConnectionError, OSError):
            return
        try:
            await asyncio.wait_for(writer.wait_closed(), 2.0)
        except (TimeoutError, ConnectionError, OSError):
            return

    async def _read_first_frame(self, reader: asyncio.StreamReader) -> dict:
        """Return the first parsed frame, asking for one if needed."""
        buffer = b""
        deadline = asyncio.get_running_loop().time() + self._read_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise KmfcConnectionError(
                    f"KM-F at {self.host}:{self.port} sent no data"
                )
            try:
                chunk = await asyncio.wait_for(
                    reader.readuntil(b"\n"), remaining
                )
                buffer += chunk
            except asyncio.TimeoutError as err:
                raise KmfcConnectionError(
                    f"KM-F at {self.host}:{self.port} sent no data"
                ) from err
            except (
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
                ConnectionError,
                OSError,
            ) as err:
                raise KmfcConnectionError(
                    f"KM-F at {self.host}:{self.port} closed the connection"
                ) from err
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                data = parse_line(line.decode("ascii", errors="replace"))
                if data:
                    self._last_frame.update(data)
                    return dict(self._last_frame)

    async def _send(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        """Best effort write, a failed keep alive must not kill the reader."""
        try:
            writer.write(payload)
            await writer.drain()
        except (ConnectionError, OSError) as err:
            raise ConnectionError(f"KM-F write failed: {err}") from err

    async def _notify(self, connected: bool) -> None:
        """Report a connection state change to the caller."""
        if self._on_connection_change is not None:
            self._on_connection_change(connected)
