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
    _clients: set = field(default_factory=set)
    _pending_signals: list = field(default_factory=list)
    # Signals fire whether or not a browser is attached. Without a buffer, a
    # page opened five minutes into a session shows an empty signal table
    # beside a level card reading CONFIRMED — contradictory, and it hides
    # exactly what you opened the page to see.
    _recent_signals: deque = field(default_factory=lambda: deque(maxlen=50))

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
            await ws.wait_closed()
        finally:
            self._clients.discard(ws)

    async def _send_backlog(self, ws: ServerConnection) -> None:
        """Replay recent signals so a late-opened page is self-consistent."""
        if not self._recent_signals:
            return
        try:
            snapshot = self.get_snapshot()
            snapshot["new_signals"] = list(self._recent_signals)
            await ws.send(orjson.dumps(snapshot).decode())
        except Exception as e:
            log.warning("flow_ui_backlog_failed", error=str(e))

    @staticmethod
    def _process_request(connection, request):
        """Serve the page for anything that is not the WebSocket path."""
        if request.path.rstrip("/") in ("/ws",):
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
                    payload = orjson.dumps(snapshot).decode()
                except Exception as e:
                    log.error("flow_ui_snapshot_failed", error=str(e))
                    continue
                for client in list(self._clients):
                    try:
                        await client.send(payload)
                    except websockets.ConnectionClosed:
                        self._clients.discard(client)
                    except Exception as e:
                        log.warning("flow_ui_send_failed", error=str(e))
                        self._clients.discard(client)
