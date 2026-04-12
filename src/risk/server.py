"""ZMQ Risk Server — separate process that cannot be bypassed.

Listens on a ZMQ REP socket. Trading engine sends Signals via REQ,
risk server evaluates them and returns RiskDecisions.

Special commands (sent as JSON with "cmd" key):
  KILL_ON   — activate kill switch
  KILL_OFF  — deactivate kill switch
  STATUS    — return current risk state
  UPDATE_FILL — update state after trade execution

Usage:
    python -m src.risk.server
"""

from __future__ import annotations

import asyncio
import signal as signal_module
import sys

import msgspec
import zmq
import zmq.asyncio

from src.utils.logger import get_logger
from src.utils.types import Fill, RiskDecision, Signal

from .config import RiskConfig
from .manager import RiskManager
from .state import RiskState

log = get_logger("risk_server")

_DEFAULT_ADDR = "tcp://127.0.0.1:5555"


class RiskServer:
    """ZMQ REP server for risk management."""

    def __init__(self, bind_addr: str = _DEFAULT_ADDR, db_path: str = "data/trades.db"):
        self._addr = bind_addr
        self._db_path = db_path
        self._running = False
        self._manager: RiskManager | None = None

    async def run(self) -> None:
        """Main server loop."""
        # Load config and state
        config = RiskConfig.from_toml()
        state = RiskState(self._db_path)
        state.load_from_db()

        self._manager = RiskManager(config, state)
        self._running = True

        log.info("risk_server_starting", addr=self._addr)

        ctx = zmq.asyncio.Context()
        socket = ctx.socket(zmq.REP)
        socket.bind(self._addr)

        # Handle shutdown
        def shutdown(signum, frame):
            log.info("risk_server_shutdown_requested")
            self._running = False

        signal_module.signal(signal_module.SIGINT, shutdown)
        signal_module.signal(signal_module.SIGTERM, shutdown)

        log.info("risk_server_ready", addr=self._addr)

        try:
            while self._running:
                try:
                    if not await socket.poll(timeout=1000):
                        continue

                    raw = await socket.recv()
                    response = self._handle_message(raw)
                    await socket.send(response)
                except zmq.ZMQError as e:
                    log.error("zmq_error", error=str(e))
                    if not self._running:
                        break
        finally:
            state.persist()
            socket.close()
            ctx.term()
            log.info("risk_server_stopped")

    def _handle_message(self, raw: bytes) -> bytes:
        """Route message to handler, return serialized response."""
        encoder = msgspec.json.Encoder()
        decoder = msgspec.json.Decoder()

        try:
            data = decoder.decode(raw)
        except msgspec.DecodeError:
            decision = RiskDecision(approved=False, reason="INVALID_MESSAGE")
            return encoder.encode(decision)

        assert self._manager is not None

        # Check for special commands
        if isinstance(data, dict) and "cmd" in data:
            return self._handle_command(data, encoder)

        # Normal signal evaluation
        try:
            sig = msgspec.json.decode(raw, type=Signal)
            decision = self._manager.evaluate(sig)
            return encoder.encode(decision)
        except (msgspec.DecodeError, Exception) as e:
            log.error("evaluate_error", error=str(e))
            decision = RiskDecision(approved=False, reason=f"EVAL_ERROR: {e}")
            return encoder.encode(decision)

    def _handle_command(self, data: dict, encoder: msgspec.json.Encoder) -> bytes:
        """Handle special commands."""
        assert self._manager is not None
        cmd = data.get("cmd", "")

        if cmd == "KILL_ON":
            reason = data.get("reason", "manual")
            self._manager.kill_switch.activate(reason)
            return encoder.encode({"status": "kill_switch_activated"})

        elif cmd == "KILL_OFF":
            reason = data.get("reason", "manual")
            self._manager.kill_switch.deactivate(reason)
            return encoder.encode({"status": "kill_switch_deactivated"})

        elif cmd == "STATUS":
            status = self._manager.get_status()
            return encoder.encode(status)

        elif cmd == "UPDATE_FILL":
            try:
                fill = msgspec.convert(data.get("fill", {}), Fill)
                strategy = data.get("strategy", "")
                self._manager.update_fill(fill, strategy)
                return encoder.encode({"status": "fill_updated"})
            except Exception as e:
                return encoder.encode({"status": "error", "error": str(e)})

        elif cmd == "SET_MODE":
            mode = data.get("mode", "BALANCED")
            custom = data.get("custom_multipliers")
            try:
                self._manager.set_mode(mode, custom)
                return encoder.encode({"status": "mode_set", "mode": mode})
            except (ValueError, KeyError) as e:
                return encoder.encode({"status": "error", "error": str(e)})

        elif cmd == "UPDATE_EQUITY":
            equity = data.get("equity", 0.0)
            self._manager.state.update_equity(equity)
            return encoder.encode({"status": "equity_updated"})

        elif cmd == "TRADE_CLOSE":
            strategy = data.get("strategy", "")
            pnl = data.get("pnl", 0.0)
            symbol = data.get("symbol", "")
            self._manager.update_trade_close(strategy, pnl, symbol)
            return encoder.encode({"status": "trade_close_recorded"})

        return encoder.encode({"status": "unknown_command", "cmd": cmd})


async def main():
    server = RiskServer()
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())
