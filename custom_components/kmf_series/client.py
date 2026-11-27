"""Asyncio TCP client for the Junctek KM-F WiFi module. By Pexic.

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
# Long enough for a module to answer the check write, short enough not to
# double the wait of a failed command, so it is capped by the write timeout.
DEFAULT_POST_CHECK_TIMEOUT = 4.0
MAX_DIAGNOSTIC_LINES = 3
MAX_DIAGNOSTIC_LENGTH = 60

# The manual specifies CRLF, but that is the RS-485 and Bluetooth link. The
# WiFi module only ever sends LF, and a bridge that splits its incoming
# lines on LF alone would see a stray carriage return at the end of the
# last field and drop the command without a word. Both are sent, CRLF
# first, since that is the documented one.
WRITE_TERMINATORS = (b"\r\n", b"\n")

READBACK = "readback"
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


def build_write_frame(
    function: int,
    address: int,
    parameter: int,
    terminator: bytes = b"\r\n",
) -> bytes:
    """Build one Juntek write line.

    Layout: SOM, function code W, function number, =, address, comma,
    checksum, comma, data, comma, terminator. The manual asks for CRLF, the
    TCP bridge itself is only known to speak LF, so both are offered.
    """
    head = f":W{function:02d}={address},{checksum(parameter)},{parameter},"
    return head.encode("ascii") + terminator


def build_set_capacity_frame(
    capacity_ah: int,
    address: int = DEFAULT_ADDRESS,
    terminator: bytes = b"\r\n",
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
        SET_CAPACITY_FUNCTION,
        address,
        capacity_ah * CAPACITY_PARAM_SCALE,
        terminator,
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
        self._any_write_waiter: asyncio.Future | None = None
        self._frame_event: asyncio.Event | None = None
        self._unparsed: list[str] = []
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
                        self._note_unparsed(text)
                        self._dispatch_reply(text)
                        continue
                    self._last_frame.update(data)
                    if self._frame_event is not None:
                        self._frame_event.set()
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
        """Write the battery capacity and prove the meter stored it.

        The write goes out on the connection the reader already owns. Two
        kinds of proof are accepted, whichever arrives first:

        1. A reply naming function 28, either a return code of ``0`` or a
           repeat of the command with the value echoed back.
        2. The next ``:A=`` frame, which carries the stored capacity in
           field 5. The meter pushes those about every second, so a module
           that forwards the write without echoing it is still confirmed,
           and this is the same proof the read only sensor relies on.

        Raises KmfcWriteError when the link is down, the meter refuses, or
        nothing confirms the new value. The failed write is then probed
        with a frame that changes nothing, and the answer decides the text:
        a link that answers writes points at the Monitor function, a link
        that answers none is a readings only port and says where the value
        has to be set instead. The unparsed lines are quoted either way.
        """
        writer = self._writer
        if writer is None:
            raise KmfcWriteError(
                f"No open link to KM-F at {self.host}:{self.port}"
            )
        wanted = capacity_ah * CAPACITY_PARAM_SCALE
        already = self._reports_capacity(wanted)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._write_timeout
        self._unparsed = []
        frames: list[bytes] = []
        for index, terminator in enumerate(WRITE_TERMINATORS):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            left = len(WRITE_TERMINATORS) - index
            stop = (
                deadline if left == 1 else loop.time() + remaining / left
            )
            frame = build_set_capacity_frame(
                capacity_ah, address, terminator
            )
            frames.append(frame)
            kind, detail = await self._write_and_watch(
                writer, frame, wanted, already, stop
            )
            if kind == "refused":
                raise KmfcWriteError(
                    f"KM-F at {self.host}:{self.port} refused the "
                    f"capacity, it answered {detail}"
                )
            if kind == "reply":
                _LOGGER.debug("KM-F accepted %r with %r", frame, detail)
                return detail
            if kind == READBACK:
                confirm = (
                    f"KM-F at {self.host}:{self.port} confirmed the "
                    f"capacity by reporting {capacity_ah} Ah back, "
                    f"without answering {frame!r}"
                )
                _LOGGER.debug("%s", confirm)
                return confirm
            if already:
                # The meter held this value before the write, so a
                # matching frame would prove nothing, and a second
                # terminator proves nothing either. Nothing to confirm.
                note = (
                    f"KM-F at {self.host}:{self.port} already reported "
                    f"{capacity_ah} Ah and sent no reply to {frame!r}"
                )
                _LOGGER.debug("%s", note)
                return note
        answered = await self._writes_are_answered()
        raise KmfcWriteError(
            self._no_proof_message(frames, capacity_ah, answered)
        )

    async def _writes_are_answered(self) -> bool:
        """Ask whether this link answers a ``:W`` command at all.

        Run only after a write has failed. The parameter sent is the
        capacity the meter reports right now, read back in tenths so the
        value cannot drift by a rounding step, which leaves the stored
        setting exactly as it was and makes the frame safe to fire. A
        reply naming any function means this socket does forward writes
        and only the requested value went unanswered. Silence, heard
        while the pushed frames keep arriving on the same connection,
        means the port carries readings only.
        """
        writer = self._writer
        if writer is None:
            return False
        reported = self._last_frame.get("reported_capacity")
        if reported is None:
            return False
        tenths = round(float(reported) * CAPACITY_PARAM_SCALE)
        limit = MAX_CAPACITY_AH * CAPACITY_PARAM_SCALE
        if not CAPACITY_PARAM_SCALE <= tenths <= limit:
            return False
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        budget = min(DEFAULT_POST_CHECK_TIMEOUT, self._write_timeout)
        deadline = loop.time() + budget
        self._any_write_waiter = waiter
        try:
            for index, terminator in enumerate(WRITE_TERMINATORS):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                left = len(WRITE_TERMINATORS) - index
                frame = build_write_frame(
                    SET_CAPACITY_FUNCTION, DEFAULT_ADDRESS, tenths, terminator
                )
                try:
                    await self._send(writer, frame)
                except ConnectionError as err:
                    raise KmfcWriteError(str(err)) from err
                done, _ = await asyncio.wait(
                    [waiter],
                    timeout=remaining / left,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if waiter in done:
                    return True
        finally:
            self._any_write_waiter = None
        return False

    async def _write_and_watch(
        self,
        writer: asyncio.StreamWriter,
        frame: bytes,
        wanted: int,
        already: bool,
        stop: float,
    ) -> tuple[str | None, str | None]:
        """Send one write frame and watch for proof until the stop time.

        Returns ``(kind, detail)``: ``("reply", line)`` when the module
        answered and accepted, ``("refused", line)`` when it answered and
        did not, ``(READBACK, None)`` when a pushed frame reported the
        value, and ``(None, None)`` when nothing confirmed in time.
        """
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        self._frame_event = asyncio.Event()
        self._reply_waiter = waiter
        try:
            try:
                await self._send(writer, frame)
            except ConnectionError as err:
                raise KmfcWriteError(str(err)) from err
            outcome = await self._wait_for_proof(
                waiter, wanted, already, stop
            )
        finally:
            self._reply_waiter = None
            self._frame_event = None
        if outcome is None:
            return None, None
        if outcome == READBACK:
            return READBACK, None
        if not reply_accepted(outcome, wanted):
            return "refused", outcome
        return "reply", outcome

    async def _wait_for_proof(
        self,
        waiter: asyncio.Future,
        wanted: int,
        already: bool,
        deadline: float,
    ) -> str | None:
        """First proof that arrived: a reply line, READBACK, or None.

        Both proofs are watched at the same time so a module that stays
        silent but obeys is not punished for it, and a module that answers
        is not made to wait for the next frame. Every pushed frame carries
        the capacity, so a frame reporting the written value is the read
        back; frames that report something else are ignored until the
        deadline. ``already`` skips that proof, the meter held the value
        before the write so a matching frame would prove nothing.
        """
        loop = asyncio.get_running_loop()
        event = self._frame_event
        while True:
            if not already and self._reports_capacity(wanted):
                return READBACK
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            frame_seen = asyncio.ensure_future(event.wait())
            done, _ = await asyncio.wait(
                [waiter, frame_seen],
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter in done:
                frame_seen.cancel()
                return waiter.result()
            if frame_seen in done:
                event.clear()
            else:
                frame_seen.cancel()

    def _reports_capacity(self, wanted: int) -> bool:
        """True when the newest frame carries the capacity we just wrote."""
        reported = self._last_frame.get("reported_capacity")
        if reported is None:
            return False
        return round(float(reported) * CAPACITY_PARAM_SCALE) == wanted

    def _no_proof_message(
        self, frames: list[bytes], capacity_ah: int, answered: bool
    ) -> str:
        """Error shown when neither a reply nor a read back arrived.

        ``answered`` comes from _writes_are_answered, so the text can
        separate the two failures that look identical from the outside.
        A link that answers other writes has a setting to fix, and the
        Monitor function is the one that silences commands. A link that
        answers no write at all is a property of the module, so the
        message stops sending the user hunting through menus and names
        the front panel setting that stores the same value.
        """
        sent = " or ".join(repr(frame) for frame in frames)
        detail = (
            " The only lines it sent were: " + "; ".join(self._unparsed)
            if self._unparsed
            else ""
        )
        head = (
            f"KM-F at {self.host}:{self.port} did not answer {sent} "
            f"and did not report {capacity_ah} Ah back within "
            f"{self._write_timeout:g} s. "
        )
        if answered:
            return head + (
                "It did answer another write on the same connection, so "
                "this link takes commands and only this one went "
                "unanswered. Switch the Monitor function on at the meter, "
                "under system settings; no protocol command can enable "
                "it, and while it is off the meter ignores writes. Then "
                "close the vendor app, since the module serves one TCP "
                f"client at a time.{detail}"
            )
        return head + (
            "It stayed silent about the capacity it already stores, on "
            "this connection and on a second one with a different line "
            "ending, while its measurements kept arriving. This network "
            "port carries readings only, so the capacity has to be set "
            "at the meter, under system settings as \"AH. preset\"; it "
            "shows up here on the next frame. If the vendor app writes "
            "the capacity for you, its Bluetooth link answers writes "
            "while this one does not." + detail
        )

    def _note_unparsed(self, line: str) -> None:
        """Keep a short trail of what the device said that we did not know.

        Only kept while a write is in flight, so a chatty or unfamiliar
        firmware can be described in the error message instead of being
        dropped silently.
        """
        if self._reply_waiter is None:
            return
        text = line.strip()
        if not text or len(self._unparsed) >= MAX_DIAGNOSTIC_LINES:
            return
        if len(text) > MAX_DIAGNOSTIC_LENGTH:
            text = text[:MAX_DIAGNOSTIC_LENGTH] + "..."
        if text not in self._unparsed:
            self._unparsed.append(text)

    def _dispatch_reply(self, line: str) -> None:
        """Hand a write reply to whoever is waiting for one.

        A reply naming any function also wakes the post check, since it
        only wants to learn whether this link answers writes at all.
        """
        text = line.strip()
        any_waiter = self._any_write_waiter
        if (
            any_waiter is not None
            and not any_waiter.done()
            and WRITE_REPLY_RE.match(text) is not None
        ):
            any_waiter.set_result(text)
        waiter = self._reply_waiter
        if waiter is None or waiter.done() or not is_reply_for(
            text, SET_CAPACITY_FUNCTION
        ):
            return
        waiter.set_result(text)

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
