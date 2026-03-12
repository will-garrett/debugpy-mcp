# src/debugpy_mcp/dap.py
from __future__ import annotations

import json
import os
import queue
import socket
import threading
import uuid
from typing import Any, Literal


class PathMapping:
    """Maps a local source root to a remote (container) source root."""

    def __init__(self, local_root: str, remote_root: str) -> None:
        self.local_root = local_root.rstrip("/")
        self.remote_root = remote_root.rstrip("/")

    def to_remote(self, local_path: str) -> str:
        if local_path.startswith(self.local_root):
            return self.remote_root + local_path[len(self.local_root):]
        return local_path

    def to_local(self, remote_path: str) -> str:
        if remote_path.startswith(self.remote_root):
            return self.local_root + remote_path[len(self.remote_root):]
        return remote_path

    def to_dict(self) -> dict[str, str]:
        return {"local_root": self.local_root, "remote_root": self.remote_root}


class DAPBreakpoint:
    """Represents one registered breakpoint with a stable internal ID."""

    def __init__(self, file: str, line: int, condition: str | None = None) -> None:
        self.internal_id: str = str(uuid.uuid4())
        self.file = file          # local path (as given by caller)
        self.line = line
        self.condition = condition
        self.dap_id: int | None = None      # assigned by debugpy after setBreakpoints
        self.verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.internal_id,
            "file": self.file,
            "line": self.line,
            "condition": self.condition,
            "verified": self.verified,
            "dap_id": self.dap_id,
        }


class DAPSession:
    """Manages one DAP connection to a running debugpy process."""

    def __init__(self, host: str, port: int, method: Literal["persist", "ephemeral"]) -> None:
        self.host = host
        self.port = port
        self.method = method

        # Socket and reader thread
        self._sock: socket.socket | None = None
        self._buf: bytes = b""
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()

        # Response routing: seq → Queue that the caller blocks on
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()

        # Async event queue (stopped, output, breakpoint, etc.)
        self._event_queue: queue.Queue[dict[str, Any]] = queue.Queue()

        # Mutable state — always access under _state_lock
        self._state_lock = threading.Lock()
        self._connected: bool = False
        self._seq: int = 0
        self.stopped_thread_id: int | None = None
        self.stopped_frame_id: int | None = None

        # Persistent state (survives ephemeral reconnects)
        self.breakpoints: list[DAPBreakpoint] = []
        self.path_mappings: list[PathMapping] = []

    # ------------------------------------------------------------------
    # Public state accessors (thread-safe)
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    def _set_connected(self, value: bool) -> None:
        with self._state_lock:
            self._connected = value

    def _next_seq(self) -> int:
        with self._state_lock:
            self._seq += 1
            return self._seq

    # ------------------------------------------------------------------
    # Message framing
    # ------------------------------------------------------------------

    def _send_msg(self, msg: dict[str, Any]) -> None:
        """Encode and send one DAP message. Called from tool-call threads."""
        body = json.dumps(msg).encode()
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        assert self._sock is not None
        self._sock.sendall(frame)

    def _read_msg_raw(self) -> dict[str, Any]:
        """Read one complete DAP message from the socket. Called ONLY from reader thread."""
        assert self._sock is not None
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("DAP socket closed")
            self._buf += chunk
        sep = self._buf.index(b"\r\n\r\n")
        header_bytes, self._buf = self._buf[:sep], self._buf[sep + 4:]
        length = int(
            next(
                line.split(":", 1)[1].strip()
                for line in header_bytes.decode().split("\r\n")
                if line.lower().startswith("content-length")
            )
        )
        while len(self._buf) < length:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("DAP socket closed")
            self._buf += chunk
        body, self._buf = self._buf[:length], self._buf[length:]
        return json.loads(body)

    # ------------------------------------------------------------------
    # Background reader thread
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """Runs in a background thread. Routes responses to callers and events to queue."""
        try:
            while not self._reader_stop.is_set():
                try:
                    msg = self._read_msg_raw()
                except (ConnectionError, OSError, StopIteration, ValueError):
                    break
                msg_type = msg.get("type")
                if msg_type == "response":
                    seq = msg.get("request_seq")
                    with self._pending_lock:
                        q = self._pending.get(seq)
                    if q is not None:
                        q.put(msg)
                elif msg_type == "event":
                    event = msg.get("event")
                    if event == "stopped":
                        thread_id = msg.get("body", {}).get("threadId")
                        with self._state_lock:
                            self.stopped_thread_id = thread_id
                    # Skip internal DAP housekeeping events that are not useful to tools
                    if event not in ("initialized", "process", "module", "loadedSource"):
                        self._event_queue.put(msg)
        finally:
            self._set_connected(False)

    def _start_reader(self) -> None:
        self._reader_stop.clear()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True, name=f"dap-reader-{self.host}:{self.port}")
        self._reader.start()

    # ------------------------------------------------------------------
    # Request / response
    # ------------------------------------------------------------------

    def _request(self, command: str, arguments: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        """Send a DAP request and block until the matching response arrives."""
        seq = self._next_seq()
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._pending_lock:
            self._pending[seq] = response_queue
        msg: dict[str, Any] = {"seq": seq, "type": "request", "command": command}
        if arguments is not None:
            msg["arguments"] = arguments
        self._send_msg(msg)
        try:
            return response_queue.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"DAP '{command}' request timed out after {timeout}s")
        finally:
            with self._pending_lock:
                self._pending.pop(seq, None)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> None:
        """Open the TCP socket and start the reader thread."""
        self._buf = b""
        self._sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._sock.settimeout(None)  # blocking reads in reader thread
        self._start_reader()
        self._set_connected(True)

    def disconnect(self, terminate_debuggee: bool = False) -> None:
        """Send DAP disconnect and close the socket."""
        if self._sock is not None:
            try:
                self._request("disconnect", {"restart": False, "terminateDebuggee": terminate_debuggee}, timeout=5.0)
            except Exception:
                pass
        self._reader_stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._set_connected(False)

    def ensure_connected(self, timeout: float = 30.0) -> None:
        """Ensure a live connection exists. In ephemeral mode, always reconnects."""
        if self.method == "ephemeral":
            if self._sock is not None:
                self.disconnect()
            self.connect()
            self.handshake(timeout=timeout)
            return
        # persist mode: reconnect if dropped
        if not self.connected:
            self.connect()
            self.handshake(timeout=timeout)
            self._resync_breakpoints(timeout=timeout)

    def ensure_disconnected_if_ephemeral(self) -> None:
        """In ephemeral mode, disconnect after each tool call."""
        if self.method == "ephemeral":
            self.disconnect()

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    def handshake(self, timeout: float = 30.0) -> None:
        """Perform the DAP initialize → attach → configurationDone sequence."""
        init_resp = self._request("initialize", {
            "clientID": "debugpy-mcp",
            "clientName": "debugpy-mcp",
            "adapterID": "debugpy",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "pathFormat": "path",
            "supportsVariableType": True,
            "supportsEvaluateForHovers": True,
        }, timeout=timeout)
        if not init_resp.get("success"):
            raise ConnectionError(f"DAP initialize failed: {init_resp.get('message', init_resp)}")

        attach_resp = self._request("attach", {
            "justMyCode": False,
            "subProcess": False,
        }, timeout=timeout)
        if not attach_resp.get("success"):
            raise ConnectionError(f"DAP attach failed: {attach_resp.get('message', attach_resp)}")

        self._request("configurationDone", timeout=timeout)

    # ------------------------------------------------------------------
    # Stopped state resolution
    # ------------------------------------------------------------------

    def _sync_stopped_state(self, timeout: float = 5.0) -> None:
        """Drain any pending stopped events and update stopped_thread_id / stopped_frame_id."""
        # Drain events without blocking if the queue is empty
        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break
            if event.get("event") == "stopped":
                thread_id = event.get("body", {}).get("threadId")
                if thread_id is not None:
                    with self._state_lock:
                        self.stopped_thread_id = thread_id

        # If we have a stopped thread, fetch the top frame
        with self._state_lock:
            thread_id = self.stopped_thread_id
        if thread_id is not None:
            try:
                resp = self._request("stackTrace", {"threadId": thread_id, "startFrame": 0, "levels": 1}, timeout=timeout)
                frames = resp.get("body", {}).get("stackFrames", [])
                if frames:
                    with self._state_lock:
                        self.stopped_frame_id = frames[0]["id"]
            except Exception:
                pass

    def wait_for_stop(self, timeout: float = 30.0) -> dict[str, Any] | None:
        """Block until a stopped event arrives (used by step/pause tools).

        Collects non-stopped events in a local buffer and re-enqueues them after
        the loop exits, avoiding a busy-wait spin on a non-empty queue.
        """
        import time
        deadline = time.monotonic() + timeout
        non_stopped: list[dict[str, Any]] = []
        result: dict[str, Any] | None = None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    event = self._event_queue.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if event.get("event") == "stopped":
                    result = event
                    break
                # Not a stopped event — buffer it to re-enqueue later
                non_stopped.append(event)
        finally:
            for e in non_stopped:
                self._event_queue.put(e)

        if result is not None:
            thread_id = result.get("body", {}).get("threadId")
            with self._state_lock:
                self.stopped_thread_id = thread_id
            self._sync_stopped_state(timeout=5.0)
        return result

    # ------------------------------------------------------------------
    # Path mapping helpers
    # ------------------------------------------------------------------

    def to_remote_path(self, local_path: str) -> str:
        for m in self.path_mappings:
            remote = m.to_remote(local_path)
            if remote != local_path:
                return remote
        return local_path

    def to_local_path(self, remote_path: str) -> str:
        for m in self.path_mappings:
            local = m.to_local(remote_path)
            if local != remote_path:
                return local
        return remote_path

    # ------------------------------------------------------------------
    # Breakpoint re-sync (called after reconnect)
    # ------------------------------------------------------------------

    def _resync_breakpoints(self, timeout: float = 30.0) -> None:
        """Re-send all registered breakpoints to DAP after reconnect."""
        files: dict[str, list[DAPBreakpoint]] = {}
        for bp in self.breakpoints:
            files.setdefault(bp.file, []).append(bp)
        for local_file, bps in files.items():
            remote_file = self.to_remote_path(local_file)
            source_bps = [{"line": bp.line, **({"condition": bp.condition} if bp.condition else {})} for bp in bps]
            try:
                resp = self._request("setBreakpoints", {
                    "source": {"path": remote_file},
                    "breakpoints": source_bps,
                }, timeout=timeout)
                dap_bps = resp.get("body", {}).get("breakpoints", [])
                for bp, dap_bp in zip(bps, dap_bps):
                    bp.dap_id = dap_bp.get("id")
                    bp.verified = dap_bp.get("verified", False)
            except Exception:
                pass
