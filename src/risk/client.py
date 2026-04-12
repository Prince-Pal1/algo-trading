"""Risk client — ZMQ REQ client and inline client for backtests.

RiskClient: connects to the separate risk server process via ZMQ.
            Fail-closed: if server unreachable, rejects all new entries.

InlineRiskClient: wraps RiskManager directly (no ZMQ).
                  Same interface for polymorphic use in backtests.
"""

from __future__ import annotations

import time

import msgspec
import zmq

from src.utils.logger import get_logger
from src.utils.types import Fill, RiskDecision, Signal, SignalAction

log = get_logger("risk_client")


def _numpy_enc_hook(obj: object) -> object:
    """Convert numpy scalars to Python builtins for msgspec serialization."""
    if hasattr(obj, "item"):
        return obj.item()
    raise NotImplementedError(f"Cannot serialize {type(obj)}")


class RiskClient:
    """ZMQ REQ client — connects to risk server."""

    def __init__(self, server_addr: str = "tcp://127.0.0.1:5555",
                 timeout_ms: int = 2000, max_failures: int = 3):
        self._addr = server_addr
        self._timeout_ms = timeout_ms
        self._max_failures = max_failures
        self._consecutive_failures = 0
        self._last_failure_time: float = 0
        self._ctx: zmq.Context | None = None
        self._socket: zmq.Socket | None = None
        self._encoder = msgspec.json.Encoder(enc_hook=_numpy_enc_hook)

    def connect(self) -> None:
        """Connect to risk server."""
        self._ctx = zmq.Context()
        self._socket = self._ctx.socket(zmq.REQ)
        self._socket.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
        self._socket.setsockopt(zmq.SNDTIMEO, self._timeout_ms)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.connect(self._addr)
        log.info("risk_client_connected", addr=self._addr)

    def check_signal(self, signal: Signal) -> RiskDecision:
        """Send signal to risk server, get decision back.

        Fail-closed: if server unreachable, reject all new entries.
        CLOSE signals always pass through even on failure.
        """
        if self._socket is None:
            if signal.action == SignalAction.CLOSE:
                return RiskDecision(approved=True, reason="CLOSE_FAILSAFE")
            return RiskDecision(approved=False, reason="RISK_CLIENT_NOT_CONNECTED")

        # Fail-closed: too many consecutive failures (with cooldown-based retry)
        if self._consecutive_failures >= self._max_failures:
            if time.time() - self._last_failure_time > 60:
                self._consecutive_failures = 0
                log.info("risk_client_retry", reason="cooldown_expired")
            else:
                if signal.action == SignalAction.CLOSE:
                    return RiskDecision(approved=True, reason="CLOSE_FAILSAFE")
                return RiskDecision(
                    approved=False,
                    reason=f"RISK_SERVER_UNREACHABLE: {self._consecutive_failures} consecutive failures",
                )

        try:
            self._socket.send(self._encoder.encode(signal))
            raw = self._socket.recv()
            decision = msgspec.json.decode(raw, type=RiskDecision)
            self._consecutive_failures = 0
            return decision

        except zmq.ZMQError as e:
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            log.error("risk_server_error", error=str(e),
                      failures=self._consecutive_failures)

            # Reconnect socket for next attempt
            self._reconnect()

            if signal.action == SignalAction.CLOSE:
                return RiskDecision(approved=True, reason="CLOSE_FAILSAFE")
            return RiskDecision(
                approved=False,
                reason=f"RISK_SERVER_TIMEOUT: {e}",
            )

    def report_fill(self, fill: Fill, strategy: str = "") -> None:
        """Notify risk server of an executed fill."""
        if self._socket is None:
            return
        try:
            msg = {
                "cmd": "UPDATE_FILL",
                "fill": {
                    "order_id": fill.order_id,
                    "symbol": fill.symbol,
                    "side": fill.side.value,
                    "price": fill.price,
                    "quantity": fill.quantity,
                    "commission": fill.commission,
                    "timestamp": fill.timestamp,
                    "exchange": fill.exchange,
                },
                "strategy": strategy,
            }
            self._socket.send(msgspec.json.encode(msg))
            self._socket.recv()  # Consume reply
        except zmq.ZMQError:
            pass

    def report_trade_close(self, strategy: str, pnl: float, symbol: str) -> None:
        """Notify risk server of a closed trade."""
        if self._socket is None:
            return
        try:
            msg = {"cmd": "TRADE_CLOSE", "strategy": strategy, "pnl": pnl, "symbol": symbol}
            self._socket.send(msgspec.json.encode(msg))
            self._socket.recv()
        except zmq.ZMQError:
            pass

    def kill(self, activate: bool, reason: str = "manual") -> None:
        """Toggle kill switch."""
        if self._socket is None:
            return
        try:
            cmd = "KILL_ON" if activate else "KILL_OFF"
            self._socket.send(msgspec.json.encode({"cmd": cmd, "reason": reason}))
            self._socket.recv()
        except zmq.ZMQError:
            pass

    def status(self) -> dict:
        """Get current risk status."""
        if self._socket is None:
            return {"error": "not connected"}
        try:
            self._socket.send(msgspec.json.encode({"cmd": "STATUS"}))
            raw = self._socket.recv()
            return msgspec.json.decode(raw)
        except zmq.ZMQError:
            return {"error": "unreachable"}

    def set_mode(self, mode: str, custom: dict[str, float] | None = None) -> dict:
        """Change operating mode."""
        if self._socket is None:
            return {"error": "not connected"}
        try:
            msg: dict = {"cmd": "SET_MODE", "mode": mode}
            if custom:
                msg["custom_multipliers"] = custom
            self._socket.send(msgspec.json.encode(msg))
            raw = self._socket.recv()
            return msgspec.json.decode(raw)
        except zmq.ZMQError:
            return {"error": "unreachable"}

    def close(self) -> None:
        """Disconnect and cleanup."""
        if self._socket:
            self._socket.close()
        if self._ctx:
            self._ctx.term()

    def _reconnect(self) -> None:
        """Reconnect socket after failure."""
        try:
            if self._socket:
                self._socket.close()
            if self._ctx:
                self._socket = self._ctx.socket(zmq.REQ)
                self._socket.setsockopt(zmq.RCVTIMEO, self._timeout_ms)
                self._socket.setsockopt(zmq.SNDTIMEO, self._timeout_ms)
                self._socket.setsockopt(zmq.LINGER, 0)
                self._socket.connect(self._addr)
        except zmq.ZMQError:
            pass


class InlineRiskClient:
    """Wraps RiskManager directly — same interface, no ZMQ.

    Used by BacktestEngine when risk checking is enabled.
    """

    def __init__(self, manager):
        """Args: manager is a RiskManager instance."""
        self._manager = manager

    def connect(self) -> None:
        pass  # No-op for inline

    def check_signal(self, signal: Signal) -> RiskDecision:
        """Evaluate signal directly via RiskManager."""
        return self._manager.evaluate(signal)

    def report_fill(self, fill: Fill, strategy: str = "") -> None:
        """Update state directly."""
        self._manager.update_fill(fill, strategy)

    def report_trade_close(self, strategy: str, pnl: float, symbol: str) -> None:
        """Update state directly."""
        self._manager.update_trade_close(strategy, pnl, symbol)

    def kill(self, activate: bool, reason: str = "manual") -> None:
        if activate:
            self._manager.kill_switch.activate(reason)
        else:
            self._manager.kill_switch.deactivate(reason)

    def set_mode(self, mode: str, custom: dict[str, float] | None = None) -> dict:
        self._manager.set_mode(mode, custom)
        return {"status": "mode_set", "mode": mode}

    def status(self) -> dict:
        return self._manager.get_status()

    def close(self) -> None:
        self._manager.state.persist()
