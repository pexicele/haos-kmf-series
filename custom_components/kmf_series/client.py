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

OnDataCallback = Callable[[dict], None]
OnConnectionChangeCallback = Callable[[bool], None]


class KmfcError(Exception):
    """Base error for the KM-F client."""


class KmfcConnectionError(KmfcError):
    """Raised when the socket cannot be established or drops out."""


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
        on_data: OnDataCallback | None = None,
        on_connection_change: OnConnectionChangeCallback | None = None,
    ) -> None:
        """Store the endpoint and the callbacks used while streaming."""
        self.host = host
        self.port = port
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._counters_interval = counters_interval
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
                    data = parse_line(line.decode("ascii", errors="replace"))
                    if not data:
                        continue
                    self._last_frame.update(data)
                    if self._on_data is not None:
                        self._on_data(dict(self._last_frame))
        finally:
            self._writer = None
            await self._close_writer(writer)
            if not self._closing:
                await self._notify(False)

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
