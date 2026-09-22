"""Live monitor page, served by the monitor process over one port.

Why not Streamlit: Streamlit re-runs the whole script on every refresh, which
costs 1-3 seconds per update. For a view you watch while price is inside your
level, that lag is the difference between information and decoration. This
serves a static page once and pushes state over a WebSocket, so updates cost a
JSON parse and a DOM write.

The server runs on its own asyncio task and is never touched from the tick
handler. Snapshots are published at a fixed UI cadence (default 5 Hz) — the hot
path just updates state, and a separate task samples it. A slow or absent
browser cannot apply backpressure to ingestion.

LAG IS THE HEADLINE NUMBER. Every other figure on the page is a lie if the
process has fallen behind the tape, so it is rendered large and colour-coded
rather than tucked in a corner.

Dark-only by design: this is a local operator console that sits beside Bookmap,
not a document. Colours are the dark-mode steps of the validated diverging
palette (blue #3987e5 / red #e66767 on #1a1a19).

INBOUND COMMANDS
----------------
The socket is two-way: the page sends level edits and settings changes back
(`on_command`). That turns a read-only display into something that mutates a
file on disk, which is why the handshake checks Origin.

WebSockets are exempt from the same-origin policy — any page you happen to have
open can open a socket to 127.0.0.1 and start talking. Binding to loopback stops
the network, not the browser. So a handshake carrying an Origin that is not this
server is refused. A missing Origin (curl, a script) is allowed: that is a local
process, which already has the file.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import orjson
import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from src.utils.logger import get_logger

log = get_logger("flow_live")

DEFAULT_PORT = 8760
DEFAULT_HZ = 5.0

_PAGE_PATH = Path(__file__).with_name("live_page.html")
_PAGE_CACHE: bytes | None = None


def _page_bytes() -> bytes:
    """Read the page once and hold it. Edits need a monitor restart."""
    global _PAGE_CACHE
    if _PAGE_CACHE is None:
        _PAGE_CACHE = _PAGE_PATH.read_bytes()
    return _PAGE_CACHE



@dataclass
class LiveServer:
    """Serves the page on GET / and pushes state snapshots on /ws."""

    get_snapshot: object                      # callable -> dict
    port: int = DEFAULT_PORT
    hz: float = DEFAULT_HZ
    host: str = "127.0.0.1"
    on_command: object = None                 # callable(dict) -> dict, or None
    get_chart: object = None                  # callable -> dict, or None
    _clients: set = field(default_factory=set)
    _pending_signals: list = field(default_factory=list)
    # Signals fire whether or not a browser is attached. Without a buffer, a
    # page opened five minutes into a session shows an empty signal table
    # beside a level card reading CONFIRMED — contradictory, and it hides
    # exactly what you opened the page to see.
    _recent_signals: deque = field(default_factory=lambda: deque(maxlen=50))
    _sent_chart_seq: int = -1

    def publish_signals(self, records: list[dict]) -> None:
        """Queue signals for the next broadcast. Cheap; safe from the tick path."""
        self._pending_signals.extend(records)
        for record in records:
            self._recent_signals.appendleft(record)

    async def _handler(self, ws: ServerConnection) -> None:
        self._clients.add(ws)
        log.info("flow_ui_connected", clients=len(self._clients))
        try:
            await self._send_backlog(ws)
            async for message in ws:
                await self._on_message(ws, message)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)

    async def _on_message(self, ws: ServerConnection, message) -> None:
        """Handle one inbound command. A bad command answers, never raises.

        The page is the only caller, but it is still the outside: a malformed
        or unknown command must not take down the socket the operator is
        watching price through.
        """
        try:
            payload = orjson.loads(message)
        except Exception:
            await self._reply(ws, {"ok": False, "error": "malformed json"})
            return
        if self.on_command is None:
            await self._reply(ws, {"ok": False, "error": "this monitor is read-only"})
            return
        try:
            result = self.on_command(payload)
        except Exception as e:
            log.error("flow_ui_command_failed",
                      cmd=str(payload.get("cmd")), error=str(e))
            result = {"ok": False, "error": str(e)}
        result.setdefault("cmd", payload.get("cmd"))
        await self._reply(ws, result)
        if result.get("ok"):
            await self._broadcast_now()

    async def _reply(self, ws: ServerConnection, result: dict) -> None:
        result["ack"] = True
        try:
            await ws.send(orjson.dumps(result).decode())
        except Exception:
            self._clients.discard(ws)

    async def _broadcast_now(self) -> None:
        """Push state immediately rather than waiting out the tick interval —
        an edit that takes 200 ms to appear reads as an edit that did not
        register, and the operator clicks it again."""
        try:
            snapshot = self.get_snapshot()
            snapshot["new_signals"] = []
        except Exception as e:
            log.error("flow_ui_snapshot_failed", error=str(e))
            return
        await self._send_all(orjson.dumps(snapshot).decode())

    async def _send_backlog(self, ws: ServerConnection) -> None:
        """First frame for a new page: recent signals AND the full chart history.

        A page that opened mid-session must not start with an empty chart and
        fill in one column every five seconds — the history is what makes the
        current column mean anything.
        """
        try:
            snapshot = self.get_snapshot()
            snapshot["new_signals"] = list(self._recent_signals)
            self._attach_chart(snapshot, force=True)
            await ws.send(orjson.dumps(snapshot).decode())
        except Exception as e:
            log.warning("flow_ui_backlog_failed", error=str(e))

    def _attach_chart(self, snapshot: dict, force: bool = False) -> None:
        """Add the closed-column history only when it actually changed.

        The forming column rides along in every frame because it changes every
        frame. The other 179 do not, and re-sending them five times a second
        would be ~99% of the payload carrying no news.
        """
        if self.get_chart is None:
            return
        seq = (snapshot.get("tape") or {}).get("seq", 0)
        if force or seq != self._sent_chart_seq:
            try:
                snapshot["chart"] = self.get_chart()
                self._sent_chart_seq = seq
            except Exception as e:
                log.warning("flow_ui_chart_failed", error=str(e))

    def _allowed_origin(self, origin: str | None) -> bool:
        """Only this server's own page may open a socket.

        A browser attaches Origin automatically and cannot be talked out of it,
        so an absent Origin means a non-browser client on this machine — which
        can already edit the file directly, and is allowed.
        """
        if not origin:
            return True
        return origin in (
            f"http://{self.host}:{self.port}",
            f"http://localhost:{self.port}",
            f"http://127.0.0.1:{self.port}",
        )

    def _process_request(self, connection, request):
        """Serve the page for anything that is not the WebSocket path."""
        if request.path.rstrip("/") in ("/ws",):
            origin = request.headers.get("Origin")
            if not self._allowed_origin(origin):
                log.warning("flow_ui_origin_rejected", origin=origin)
                return Response(403, "Forbidden", Headers({"Content-Length": "0"}), b"")
            return None  # let the handshake proceed
        body = _page_bytes()
        return Response(
            200, "OK",
            Headers({
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": str(len(body)),
                "Cache-Control": "no-store",
            }),
            body,
        )

    async def run(self) -> None:
        """Serve until cancelled. Broadcast failures never stop the loop."""
        interval = 1.0 / max(0.5, self.hz)
        async with serve(
            self._handler, self.host, self.port,
            process_request=self._process_request,
        ):
            log.info("flow_ui_listening", url=f"http://{self.host}:{self.port}")
            while True:
                await asyncio.sleep(interval)
                if not self._clients:
                    self._pending_signals.clear()
                    continue
                try:
                    snapshot = self.get_snapshot()
                    snapshot["new_signals"] = self._pending_signals
                    self._pending_signals = []
                    self._attach_chart(snapshot)
                    payload = orjson.dumps(snapshot).decode()
                except Exception as e:
                    log.error("flow_ui_snapshot_failed", error=str(e))
                    continue
                await self._send_all(payload)

    async def _send_all(self, payload: str) -> None:
        for client in list(self._clients):
            try:
                await client.send(payload)
            except websockets.ConnectionClosed:
                self._clients.discard(client)
            except Exception as e:
                log.warning("flow_ui_send_failed", error=str(e))
                self._clients.discard(client)
