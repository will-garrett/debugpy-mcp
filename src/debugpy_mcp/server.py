from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from debugpy_mcp.dap import DAPSession, DAPBreakpoint, PathMapping, detect_path_mappings


mcp = FastMCP("debugpy-docker-mcp")


class ContainerSummary(BaseModel):
    id: str
    name: str
    image: str
    status: str
    ports: str


class AutodiscoverResult(BaseModel):
    ok: bool
    candidates: list[ContainerSummary] = Field(default_factory=list)
    selected: ContainerSummary | None = None
    notes: list[str] = Field(default_factory=list)


class ProcessInfo(BaseModel):
    pid: int
    ppid: int
    cmd: str
    kind: Literal["uvicorn", "gunicorn-master", "gunicorn-worker", "python", "other"]


class DebugpyStatusResult(BaseModel):
    ok: bool
    container: str
    port: int
    host: str
    container_running: bool
    debugpy_installed: bool
    debugpy_listening: bool
    mapped_port: str | None = None
    candidate_processes: list[ProcessInfo] = Field(default_factory=list)
    suggested_pid: int | None = None
    notes: list[str] = Field(default_factory=list)


class DebugpyAttachResult(BaseModel):
    ok: bool
    container: str
    port: int
    host: str
    pid: int | None = None
    already_listening: bool = False
    attached: bool = False
    mapped_port: str | None = None
    command: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    notes: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class DebugContextResult(BaseModel):
    ok: bool
    container: str
    working_dir: str | None = None
    python_version: str | None = None
    debugpy_version: str | None = None
    debugpy_listening: bool = False
    mapped_port: str | None = None
    processes: list[ProcessInfo] = Field(default_factory=list)
    suggested_pid: int | None = None
    ports_snapshot: str | None = None
    env_subset: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class LogsResult(BaseModel):
    ok: bool
    container: str
    tail: int
    logs: str


class BreakpointTarget(BaseModel):
    file_hint: str
    rationale: str
    breakpoint_kind: Literal["route", "dependency", "middleware", "startup", "exception", "worker", "service"]


class BreakpointPlanResult(BaseModel):
    ok: bool
    container: str
    inferred_endpoint: str | None = None
    inferred_modules: list[str] = Field(default_factory=list)
    targets: list[BreakpointTarget] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ToolError(RuntimeError):
    pass


DEFAULT_TIMEOUT = int(os.getenv("DEBUGPY_MCP_TIMEOUT", "30"))
DEFAULT_SHELL = os.getenv("DEBUGPY_MCP_SHELL", "sh")
DEFAULT_PORT = int(os.getenv("DEBUGPY_MCP_PORT", "5678"))
DEFAULT_HOST = os.getenv("DEBUGPY_MCP_HOST", "0.0.0.0")
DEFAULT_DEBUGPY_LOG_DIR = os.getenv("DEBUGPY_MCP_DEBUGPY_LOG_DIR", "/tmp/debugpy-logs")

SESSION_METHOD: Literal["persist", "ephemeral"] = os.getenv("DEBUGPY_MCP_METHOD", "persist")  # type: ignore[assignment]
_sessions: dict[tuple[str, int], DAPSession] = {}


def _get_session(host: str, port: int) -> DAPSession:
    """Return existing session or raise ToolError."""
    key = (host, port)
    if key not in _sessions:
        raise ToolError(
            f"No active DAP session for {host}:{port}. "
            "Call debugpy_session_start first."
        )
    return _sessions[key]


def _require_session(host: str, port: int) -> DAPSession:
    """Return session after syncing stopped state."""
    session = _get_session(host, port)
    session.ensure_connected()
    session._sync_stopped_state()
    return session


def run(cmd: list[str], *, timeout: int = DEFAULT_TIMEOUT, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise ToolError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc


def docker_exec(container: str, shell_cmd: str, *, timeout: int = DEFAULT_TIMEOUT, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["docker", "exec", container, DEFAULT_SHELL, "-lc", shell_cmd], timeout=timeout, check=check)


def docker_inspect_running(container: str) -> bool:
    proc = run(["docker", "inspect", "-f", "{{.State.Running}}", container], timeout=10, check=False)
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def docker_port_mapping(container: str, container_port: int) -> str | None:
    proc = run(["docker", "port", container, str(container_port)], timeout=10, check=False)
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def list_containers() -> list[ContainerSummary]:
    proc = run([
        "docker", "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
    ], timeout=10)
    items: list[ContainerSummary] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        items.append(ContainerSummary(id=parts[0], name=parts[1], image=parts[2], status=parts[3], ports=parts[4]))
    return items


def autodiscover_target(service_hint: str | None = None, image_hint: str | None = None) -> AutodiscoverResult:
    containers = list_containers()
    notes: list[str] = []
    scored: list[tuple[int, ContainerSummary]] = []

    for c in containers:
        score = 0
        haystacks = [c.name.lower(), c.image.lower(), c.ports.lower()]
        if service_hint:
            hint = service_hint.lower()
            if hint in c.name.lower():
                score += 10
            if hint in c.image.lower():
                score += 6
        if image_hint:
            hint = image_hint.lower()
            if hint in c.image.lower():
                score += 10
            if hint in c.name.lower():
                score += 3
        if "5678" in c.ports:
            score += 4
        if any(token in c.name.lower() or token in c.image.lower() for token in ["api", "fastapi", "uvicorn", "backend", "web"]):
            score += 2
        if score > 0:
            scored.append((score, c))

    if not scored:
        notes.append("No strong autodiscovery match found; returning all running containers.")
        return AutodiscoverResult(ok=True, candidates=containers, selected=containers[0] if containers else None, notes=notes)

    scored.sort(key=lambda t: (-t[0], t[1].name))
    candidates = [c for _, c in scored]
    selected = candidates[0] if candidates else None
    if selected:
        notes.append(f"Selected container '{selected.name}' as the strongest match.")
    return AutodiscoverResult(ok=True, candidates=candidates, selected=selected, notes=notes)


def detect_debugpy_installed(container: str, python_bin: str = "python") -> tuple[bool, str | None]:
    cmd = (
        f"{shlex.quote(python_bin)} - <<'PY'\n"
        "import importlib.util\n"
        "spec = importlib.util.find_spec('debugpy')\n"
        "print('YES' if spec else 'NO')\n"
        "PY"
    )
    proc = docker_exec(container, cmd, timeout=20, check=False)
    if proc.returncode != 0:
        return False, None
    installed = proc.stdout.strip() == "YES"
    if not installed:
        return False, None
    ver_cmd = (
        f"{shlex.quote(python_bin)} - <<'PY'\n"
        "import debugpy\n"
        "print(getattr(debugpy, '__version__', 'unknown'))\n"
        "PY"
    )
    ver_proc = docker_exec(container, ver_cmd, timeout=20, check=False)
    version = ver_proc.stdout.strip() if ver_proc.returncode == 0 else None
    return True, version


def get_process_table(container: str) -> list[ProcessInfo]:
    proc = docker_exec(container, "ps -eo pid,ppid,args", timeout=20, check=False)
    if proc.returncode != 0:
        raise ToolError(f"Unable to read process table in container {container}:\n{proc.stderr}")
    results: list[ProcessInfo] = []
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, ppid_s, cmd = parts
        try:
            pid = int(pid_s)
            ppid = int(ppid_s)
        except ValueError:
            continue
        lowered = cmd.lower()
        kind: Literal["uvicorn", "gunicorn-master", "gunicorn-worker", "python", "other"] = "other"
        if "gunicorn" in lowered and "master" in lowered:
            kind = "gunicorn-master"
        elif "gunicorn" in lowered and "worker" in lowered:
            kind = "gunicorn-worker"
        elif "uvicorn" in lowered:
            kind = "uvicorn"
        elif "python" in lowered:
            kind = "python"
        if any(token in lowered for token in ["python", "uvicorn", "gunicorn", "fastapi"]):
            results.append(ProcessInfo(pid=pid, ppid=ppid, cmd=cmd, kind=kind))
    return results


def choose_pid(processes: list[ProcessInfo]) -> tuple[int | None, list[str]]:
    notes: list[str] = []
    if not processes:
        return None, ["No Python-like process candidates found."]
    uvicorn = [p for p in processes if p.kind == "uvicorn"]
    if uvicorn:
        if len(uvicorn) > 1:
            notes.append("Multiple uvicorn-like processes found; selecting the first one.")
        return uvicorn[0].pid, notes
    workers = [p for p in processes if p.kind == "gunicorn-worker"]
    if workers:
        if len(workers) > 1:
            notes.append("Multiple gunicorn workers found; selecting the first worker.")
        return workers[0].pid, notes
    masters = [p for p in processes if p.kind == "gunicorn-master"]
    if masters:
        notes.append("Only gunicorn master detected; a worker may be a better debug target if one exists.")
        return masters[0].pid, notes
    py = [p for p in processes if p.kind == "python"]
    if py:
        if len(py) > 1:
            notes.append("Multiple generic python processes found; selecting the first one.")
        return py[0].pid, notes
    return processes[0].pid, ["Fell back to the first candidate process."]


def port_is_listening(container: str, port: int) -> bool:
    cmd = f"(ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null) | grep -E '[:]{port}([[:space:]]|$)' >/dev/null 2>&1"
    proc = docker_exec(container, cmd, timeout=15, check=False)
    return proc.returncode == 0


def capture_ports_snapshot(container: str) -> str:
    proc = docker_exec(container, "(ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null || true)", timeout=15, check=False)
    return (proc.stdout or proc.stderr).strip()


def get_working_dir(container: str, pid: int) -> str | None:
    proc = docker_exec(container, f"readlink /proc/{pid}/cwd", timeout=10, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def get_python_version(container: str, python_bin: str = "python") -> str | None:
    proc = docker_exec(container, f"{shlex.quote(python_bin)} --version", timeout=10, check=False)
    if proc.returncode != 0:
        return None
    out = (proc.stdout or proc.stderr).strip()
    return out or None


def get_env_subset(container: str) -> dict[str, str]:
    keys = ["PYTHONPATH", "PYTHONUNBUFFERED", "UVICORN_HOST", "UVICORN_PORT", "HOSTNAME"]
    env_map: dict[str, str] = {}
    for k in keys:
        proc = docker_exec(container, f"printf '%s' \"${k}\"", timeout=5, check=False)
        val = proc.stdout if proc.returncode == 0 else ""
        if val:
            env_map[k] = val
    return env_map


def build_debugpy_attach_cmd(*, python_bin: str, host: str, port: int, pid: int, wait_for_client: bool, log_to: str | None, configure_subprocess: bool) -> str:
    parts = [
        shlex.quote(python_bin), "-m", "debugpy", "--listen", f"{shlex.quote(host)}:{port}",
        "--configure-subProcess", "true" if configure_subprocess else "false"
    ]
    if wait_for_client:
        parts.append("--wait-for-client")
    if log_to:
        parts.extend(["--log-to", shlex.quote(log_to)])
    parts.extend(["--pid", str(pid)])
    return " ".join(parts)


def list_debugpy_log_files(container: str, log_dir: str) -> list[str]:
    proc = docker_exec(container, f"find {shlex.quote(log_dir)} -maxdepth 1 -type f 2>/dev/null | sort", timeout=15, check=False)
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def read_debugpy_logs(container: str, log_dir: str, tail: int) -> str:
    cmd = (
        f"if [ -d {shlex.quote(log_dir)} ]; then "
        f"for f in $(find {shlex.quote(log_dir)} -maxdepth 1 -type f | sort); do "
        f"echo '===== '"'$f'"' ====='; tail -n {tail} \"$f\"; echo; done; "
        f"fi"
    )
    proc = docker_exec(container, cmd, timeout=30, check=False)
    return (proc.stdout or proc.stderr).strip()


def infer_modules_from_logs(logs: str) -> tuple[list[str], str | None]:
    inferred_modules: list[str] = []
    endpoint: str | None = None
    for pattern in [r"File \"([^\"]+)\"", r"(/[^\s:'\"]+\.py)", r"([A-Za-z0-9_./-]+\.py)"]:
        for match in re.findall(pattern, logs):
            if match not in inferred_modules:
                inferred_modules.append(match)
    endpoint_match = re.search(r'\b(GET|POST|PUT|PATCH|DELETE)\s+([^\s]+)', logs)
    if endpoint_match:
        endpoint = f"{endpoint_match.group(1)} {endpoint_match.group(2)}"
    return inferred_modules[:10], endpoint


def build_breakpoint_plan(logs: str, processes: list[ProcessInfo], working_dir: str | None) -> BreakpointPlanResult:
    inferred_modules, endpoint = infer_modules_from_logs(logs)
    notes: list[str] = []
    targets: list[BreakpointTarget] = []

    if endpoint:
        targets.append(BreakpointTarget(
            file_hint="app/routes/* or api/routes/*",
            rationale=f"Recent logs suggest endpoint activity around {endpoint}; route handler is the first high-value breakpoint.",
            breakpoint_kind="route",
        ))

    if any("middleware" in p.cmd.lower() for p in processes) or "middleware" in logs.lower():
        targets.append(BreakpointTarget(
            file_hint="app/main.py or app/middleware/*",
            rationale="Logs or process metadata suggest middleware involvement; break before request dispatch and on exception wrapping.",
            breakpoint_kind="middleware",
        ))

    if "traceback" in logs.lower() or "exception" in logs.lower() or "error" in logs.lower():
        targets.append(BreakpointTarget(
            file_hint="file from top traceback frame",
            rationale="Container logs contain an error signal; set a breakpoint on the first application frame above framework internals.",
            breakpoint_kind="exception",
        ))

    if not targets:
        targets.append(BreakpointTarget(
            file_hint="app/main.py",
            rationale="Default fallback: break in FastAPI app setup to inspect router registration, dependencies, and middleware.",
            breakpoint_kind="startup",
        ))
        targets.append(BreakpointTarget(
            file_hint="app/api/* or app/routes/*",
            rationale="Default fallback: break in the route handler for the suspect endpoint or resource.",
            breakpoint_kind="route",
        ))
        targets.append(BreakpointTarget(
            file_hint="app/services/*",
            rationale="Default fallback: break inside the business logic layer called by the route.",
            breakpoint_kind="service",
        ))

    for mod in inferred_modules[:3]:
        targets.append(BreakpointTarget(
            file_hint=mod,
            rationale="This file path appeared in recent logs and is a likely code path for the failure.",
            breakpoint_kind="exception" if mod.endswith(".py") else "service",
        ))

    if working_dir:
        notes.append(f"Likely remote source root is near {working_dir}.")
    if inferred_modules:
        notes.append("Inferred modules were extracted from container logs and stack traces.")
    else:
        notes.append("No explicit file paths were found in logs; plan is heuristic.")

    deduped: list[BreakpointTarget] = []
    seen: set[tuple[str, str]] = set()
    for t in targets:
        key = (t.file_hint, t.breakpoint_kind)
        if key not in seen:
            seen.add(key)
            deduped.append(t)

    return BreakpointPlanResult(ok=True, container="", inferred_endpoint=endpoint, inferred_modules=inferred_modules, targets=deduped[:6], notes=notes)


@mcp.tool()
def debugpy_list_containers() -> dict[str, Any]:
    return {"ok": True, "containers": [c.model_dump() for c in list_containers()]}


@mcp.tool()
def debugpy_autodiscover_target(service_hint: str | None = None, image_hint: str | None = None) -> dict[str, Any]:
    return autodiscover_target(service_hint=service_hint, image_hint=image_hint).model_dump()


@mcp.tool()
def debugpy_status(container: str, port: int = DEFAULT_PORT, host: str = DEFAULT_HOST, python_bin: str = "python") -> dict[str, Any]:
    running = docker_inspect_running(container)
    if not running:
        return DebugpyStatusResult(ok=False, container=container, port=port, host=host, container_running=False, debugpy_installed=False, debugpy_listening=False, notes=["Container is not running or does not exist."]).model_dump()
    installed, _version = detect_debugpy_installed(container, python_bin=python_bin)
    mapped_port = docker_port_mapping(container, port)
    notes: list[str] = []
    if mapped_port is None:
        notes.append(f"No docker port mapping was found for container port {port}. Your IDE may still connect if the network path is otherwise reachable.")
    processes = get_process_table(container)
    suggested_pid, pid_notes = choose_pid(processes)
    notes.extend(pid_notes)
    listening = port_is_listening(container, port)
    notes.append(f"Port {port} is {'already' if listening else 'not'} listening inside the container.")
    if not installed:
        notes.append("debugpy is not importable inside the container.")
    return DebugpyStatusResult(ok=True, container=container, port=port, host=host, container_running=True, debugpy_installed=installed, debugpy_listening=listening, mapped_port=mapped_port, candidate_processes=processes, suggested_pid=suggested_pid, notes=notes).model_dump()


@mcp.tool()
def debugpy_attach(container: str, pid: int | None = None, port: int = DEFAULT_PORT, host: str = DEFAULT_HOST, python_bin: str = "python", wait_for_client: bool = False, log_to: str | None = DEFAULT_DEBUGPY_LOG_DIR, configure_subprocess: bool = False) -> dict[str, Any]:
    notes: list[str] = []
    if not docker_inspect_running(container):
        return DebugpyAttachResult(ok=False, container=container, port=port, host=host, notes=["Container is not running or does not exist."]).model_dump()
    installed, version = detect_debugpy_installed(container, python_bin=python_bin)
    if not installed:
        return DebugpyAttachResult(ok=False, container=container, port=port, host=host, notes=["debugpy is not installed inside the container.", "Install it in the image or running container before attach."]).model_dump()
    if version:
        notes.append(f"Detected debugpy {version} inside the container.")
    processes = get_process_table(container)
    if pid is None:
        pid, pid_notes = choose_pid(processes)
        notes.extend(pid_notes)
    if pid is None:
        return DebugpyAttachResult(ok=False, container=container, port=port, host=host, notes=["No candidate Python process was found to attach to."]).model_dump()
    if port_is_listening(container, port):
        mapped = docker_port_mapping(container, port)
        return DebugpyAttachResult(ok=True, container=container, port=port, host=host, pid=pid, already_listening=True, attached=False, mapped_port=mapped, notes=[f"Port {port} is already listening inside the container.", "Skipping injection because debugpy likely already attached."], next_steps=["Start your existing Attach configuration in Cursor.", "Verify your pathMappings match the container source path."]).model_dump()
    if log_to:
        docker_exec(container, f"mkdir -p {shlex.quote(log_to)}", timeout=10, check=False)
    cmd = build_debugpy_attach_cmd(python_bin=python_bin, host=host, port=port, pid=pid, wait_for_client=wait_for_client, log_to=log_to, configure_subprocess=configure_subprocess)
    proc = docker_exec(container, cmd, timeout=60, check=False)
    mapped_port = docker_port_mapping(container, port)
    listening = port_is_listening(container, port)
    if not listening:
        notes.append("debugpy did not appear to open the listening port after attach.")
        notes.append("Common causes: ptrace restrictions, wrong PID, or missing process privileges.")
        if "operation not permitted" in (proc.stderr or "").lower():
            notes.append("The container likely lacks ptrace permission for PID attach.")
        next_steps = [
            "Inspect stderr from the command output.",
            "Check container capabilities such as SYS_PTRACE and seccomp settings.",
            "Verify you attached to the worker process rather than a supervisor or master process.",
        ]
    else:
        notes.append(f"debugpy is now listening on {host}:{port} inside the container.")
        next_steps = [
            "Start your existing Attach configuration in Cursor.",
            "Set breakpoints in the relevant FastAPI route, dependency, or middleware path.",
            "If breakpoints do not bind, verify localRoot and remoteRoot path mappings.",
        ]
    return DebugpyAttachResult(ok=listening, container=container, port=port, host=host, pid=pid, attached=listening, mapped_port=mapped_port, command=cmd, stdout=(proc.stdout or "").strip() or None, stderr=(proc.stderr or "").strip() or None, notes=notes, next_steps=next_steps).model_dump()


@mcp.tool()
def debugpy_context(container: str, port: int = DEFAULT_PORT, python_bin: str = "python") -> dict[str, Any]:
    if not docker_inspect_running(container):
        return DebugContextResult(ok=False, container=container, notes=["Container is not running or does not exist."]).model_dump()
    installed, version = detect_debugpy_installed(container, python_bin=python_bin)
    processes = get_process_table(container)
    suggested_pid, pid_notes = choose_pid(processes)
    working_dir = get_working_dir(container, suggested_pid) if suggested_pid else None
    listening = port_is_listening(container, port)
    mapped_port = docker_port_mapping(container, port)
    py_ver = get_python_version(container, python_bin=python_bin)
    ports_snapshot = capture_ports_snapshot(container)
    env_subset = get_env_subset(container)
    notes = list(pid_notes)
    if not installed:
        notes.append("debugpy is not currently installed inside the container.")
    notes.append(f"Port {port} is {'already' if listening else 'not'} listening inside the container.")
    return DebugContextResult(ok=True, container=container, working_dir=working_dir, python_version=py_ver, debugpy_version=version, debugpy_listening=listening, mapped_port=mapped_port, processes=processes, suggested_pid=suggested_pid, ports_snapshot=ports_snapshot, env_subset=env_subset, notes=notes).model_dump()


@mcp.tool()
def debugpy_logs(container: str, tail: int = 250) -> dict[str, Any]:
    proc = run(["docker", "logs", "--tail", str(tail), container], timeout=30, check=False)
    combined = ""
    if proc.stdout:
        combined += proc.stdout
    if proc.stderr:
        if combined:
            combined += "\n"
        combined += proc.stderr
    return LogsResult(ok=proc.returncode == 0, container=container, tail=tail, logs=combined.strip()).model_dump()


@mcp.tool()
def debugpy_debugpy_logs(container: str, log_dir: str = DEFAULT_DEBUGPY_LOG_DIR, tail: int = 200) -> dict[str, Any]:
    files = list_debugpy_log_files(container, log_dir)
    contents = read_debugpy_logs(container, log_dir, tail)
    return {
        "ok": True,
        "container": container,
        "log_dir": log_dir,
        "files": files,
        "logs": contents,
        "notes": [
            "These are debugpy-generated logs if attach used --log-to.",
            "An empty file list usually means attach has not run with logging enabled yet.",
        ],
    }


@mcp.tool()
def debugpy_breakpoint_plan(container: str, tail: int = 250, python_bin: str = "python") -> dict[str, Any]:
    if not docker_inspect_running(container):
        return BreakpointPlanResult(ok=False, container=container, notes=["Container is not running or does not exist."]).model_dump()
    logs_resp = debugpy_logs(container=container, tail=tail)
    ctx_resp = debugpy_context(container=container, python_bin=python_bin)
    logs = str(logs_resp.get("logs", ""))
    processes = [ProcessInfo(**p) for p in ctx_resp.get("processes", [])]
    working_dir = ctx_resp.get("working_dir")
    plan = build_breakpoint_plan(logs, processes, working_dir)
    plan.container = container
    return plan.model_dump()


class ConnectResult(BaseModel):
    ok: bool
    host: str
    port: int
    listening: bool
    notes: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


def tcp_is_listening(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@mcp.tool()
def debugpy_connect(host: str = "localhost", port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Check whether debugpy is already listening at host:port and return IDE connection details.

    Use this when debugpy is already running (e.g. the process was started with --wait-for-client
    or you injected it manually) and you just need to verify connectivity and get the attach config.
    No Docker access is required.
    """
    listening = tcp_is_listening(host, port)
    notes: list[str] = []
    next_steps: list[str] = []

    if listening:
        notes.append(f"debugpy is listening at {host}:{port}.")
        next_steps = [
            f'Use "host": "{host}", "port": {port} in your IDE attach configuration.',
            "Ensure your pathMappings map your local source root to the remote container path.",
            "Start the Attach debug configuration in Cursor / VS Code.",
        ]
    else:
        notes.append(f"Nothing is listening at {host}:{port}.")
        next_steps = [
            "Verify the process was started with debugpy (e.g. python -m debugpy --listen 0.0.0.0:5678 ...).",
            "Check that the port is exposed / forwarded if the process is inside a container.",
            "If you need to inject debugpy into a running process, use debugpy_attach instead.",
        ]

    return ConnectResult(ok=listening, host=host, port=port, listening=listening, notes=notes, next_steps=next_steps).model_dump()


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

class SessionStatusResult(BaseModel):
    ok: bool
    host: str
    port: int
    method: str
    connected: bool
    stopped_thread_id: int | None = None
    stopped_frame_id: int | None = None
    breakpoints: list[dict] = Field(default_factory=list)
    path_mappings: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


@mcp.tool()
def debugpy_session_start(
    host: str = "localhost",
    port: int = DEFAULT_PORT,
    path_mappings: list[dict] | None = None,
    container: str | None = None,
) -> dict[str, Any]:
    """Start a DAP session with a running debugpy process.

    path_mappings: list of {"local_root": "...", "remote_root": "..."} dicts.
    container: Docker container name for Docker-based path auto-detection.
    If path_mappings is omitted, auto-detection is attempted.
    IMPORTANT: debugpy accepts only one DAP client at a time.
    Disconnect any IDE before calling this tool.
    """
    key = (host, port)
    notes: list[str] = []

    # Close any existing session for this key
    if key in _sessions:
        try:
            _sessions[key].disconnect()
        except Exception:
            pass
        del _sessions[key]

    session = DAPSession(host=host, port=port, method=SESSION_METHOD)

    try:
        session.connect()
    except ConnectionRefusedError:
        return SessionStatusResult(
            ok=False, host=host, port=port, method=SESSION_METHOD, connected=False,
            notes=[
                f"Connection refused at {host}:{port}.",
                "debugpy accepts only one DAP client at a time. Disconnect your IDE first.",
                "Or verify debugpy is listening: use debugpy_connect to check.",
            ],
        ).model_dump()
    except OSError as exc:
        return SessionStatusResult(
            ok=False, host=host, port=port, method=SESSION_METHOD, connected=False,
            notes=[f"Connection error: {exc}"],
        ).model_dump()

    try:
        session.handshake()
    except ConnectionError as exc:
        session.disconnect()
        return SessionStatusResult(
            ok=False, host=host, port=port, method=SESSION_METHOD, connected=False,
            notes=[f"DAP handshake failed: {exc}"],
        ).model_dump()

    # Path mappings
    if path_mappings:
        session.path_mappings = [PathMapping(m["local_root"], m["remote_root"]) for m in path_mappings]
        notes.append(f"Using {len(session.path_mappings)} provided path mapping(s).")
    else:
        detected, detect_notes = detect_path_mappings(session, container=container)
        session.path_mappings = detected
        notes.extend(detect_notes)

    _sessions[key] = session
    session._sync_stopped_state()
    notes.append(f"Session started in '{SESSION_METHOD}' mode.")

    return SessionStatusResult(
        ok=True, host=host, port=port, method=SESSION_METHOD, connected=True,
        stopped_thread_id=session.stopped_thread_id,
        stopped_frame_id=session.stopped_frame_id,
        breakpoints=[bp.to_dict() for bp in session.breakpoints],
        path_mappings=[m.to_dict() for m in session.path_mappings],
        notes=notes,
    ).model_dump()


@mcp.tool()
def debugpy_session_stop(host: str = "localhost", port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Cleanly disconnect the DAP session. Does not terminate the debuggee."""
    key = (host, port)
    if key not in _sessions:
        return {"ok": False, "notes": [f"No active session for {host}:{port}."]}
    try:
        _sessions[key].disconnect(terminate_debuggee=False)
    except Exception as exc:
        return {"ok": False, "notes": [f"Disconnect error: {exc}"]}
    finally:
        del _sessions[key]
    return {"ok": True, "notes": [f"Session {host}:{port} stopped."]}


@mcp.tool()
def debugpy_session_status(host: str = "localhost", port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Return the current state of a DAP session: connection, stopped position, breakpoints, mappings."""
    key = (host, port)
    if key not in _sessions:
        return SessionStatusResult(
            ok=False, host=host, port=port, method=SESSION_METHOD, connected=False,
            notes=[f"No active session for {host}:{port}. Call debugpy_session_start first."],
        ).model_dump()
    session = _sessions[key]
    session._sync_stopped_state()
    return SessionStatusResult(
        ok=True, host=host, port=port, method=SESSION_METHOD,
        connected=session.connected,
        stopped_thread_id=session.stopped_thread_id,
        stopped_frame_id=session.stopped_frame_id,
        breakpoints=[bp.to_dict() for bp in session.breakpoints],
        path_mappings=[m.to_dict() for m in session.path_mappings],
        notes=[],
    ).model_dump()


# ---------------------------------------------------------------------------
# Execution control
# ---------------------------------------------------------------------------

class ExecControlResult(BaseModel):
    ok: bool
    host: str
    port: int
    command: str
    stopped: bool = False
    stopped_thread_id: int | None = None
    stopped_frame_id: int | None = None
    stopped_reason: str | None = None
    notes: list[str] = Field(default_factory=list)


def _exec_control(session: DAPSession, host: str, port: int, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Shared implementation for execution control tools. Caller is responsible for obtaining the session."""
    try:
        resp = session._request(command, arguments)
    except Exception as exc:
        session.ensure_disconnected_if_ephemeral()
        return ExecControlResult(ok=False, host=host, port=port, command=command, notes=[f"DAP error: {exc}"]).model_dump()

    if not resp.get("success"):
        session.ensure_disconnected_if_ephemeral()
        return ExecControlResult(
            ok=False, host=host, port=port, command=command,
            notes=[resp.get("message", "DAP request failed"), "Thread may no longer be stopped."],
        ).model_dump()

    # Wait for stopped event (step/pause commands produce one)
    stopped_event = session.wait_for_stop(timeout=30.0)
    session.ensure_disconnected_if_ephemeral()

    if stopped_event:
        reason = stopped_event.get("body", {}).get("reason", "unknown")
        return ExecControlResult(
            ok=True, host=host, port=port, command=command, stopped=True,
            stopped_thread_id=session.stopped_thread_id,
            stopped_frame_id=session.stopped_frame_id,
            stopped_reason=reason,
        ).model_dump()
    return ExecControlResult(
        ok=True, host=host, port=port, command=command, stopped=False,
        notes=["Command sent; no stopped event received within timeout. Process may still be running."],
    ).model_dump()


@mcp.tool()
def debugpy_pause(host: str = "localhost", port: int = DEFAULT_PORT, thread_id: int | None = None) -> dict[str, Any]:
    """Pause execution. Defaults to all threads."""
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return ExecControlResult(ok=False, host=host, port=port, command="pause", notes=[str(exc)]).model_dump()
    args: dict[str, Any] = {}
    if thread_id is not None:
        args["threadId"] = thread_id
    return _exec_control(session, host, port, "pause", args)


@mcp.tool()
def debugpy_continue(host: str = "localhost", port: int = DEFAULT_PORT, thread_id: int | None = None) -> dict[str, Any]:
    """Resume execution."""
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}
    args: dict[str, Any] = {}
    if thread_id is not None:
        args["threadId"] = thread_id
    elif session.stopped_thread_id is not None:
        args["threadId"] = session.stopped_thread_id
    try:
        resp = session._request("continue", args)
        with session._state_lock:
            session.stopped_thread_id = None
            session.stopped_frame_id = None
        session.ensure_disconnected_if_ephemeral()
        return {"ok": resp.get("success", True), "host": host, "port": port, "notes": ["Resumed."]}
    except Exception as exc:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"DAP error: {exc}"]}


def _step_tool(host: str, port: int, command: str, thread_id: int | None) -> dict[str, Any]:
    """Shared wrapper for step_over / step_in / step_out."""
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return ExecControlResult(ok=False, host=host, port=port, command=command, notes=[str(exc)]).model_dump()
    tid = thread_id if thread_id is not None else session.stopped_thread_id
    if tid is None:
        session.ensure_disconnected_if_ephemeral()
        return ExecControlResult(
            ok=False, host=host, port=port, command=command,
            notes=["No stopped thread. Use debugpy_pause first or wait for a breakpoint."],
        ).model_dump()
    return _exec_control(session, host, port, command, {"threadId": tid})


@mcp.tool()
def debugpy_step_over(host: str = "localhost", port: int = DEFAULT_PORT, thread_id: int | None = None) -> dict[str, Any]:
    """Step over the current line (DAP 'next')."""
    return _step_tool(host, port, "next", thread_id)


@mcp.tool()
def debugpy_step_in(host: str = "localhost", port: int = DEFAULT_PORT, thread_id: int | None = None) -> dict[str, Any]:
    """Step into the next function call (DAP 'stepIn')."""
    return _step_tool(host, port, "stepIn", thread_id)


@mcp.tool()
def debugpy_step_out(host: str = "localhost", port: int = DEFAULT_PORT, thread_id: int | None = None) -> dict[str, Any]:
    """Step out of the current function (DAP 'stepOut')."""
    return _step_tool(host, port, "stepOut", thread_id)


# ---------------------------------------------------------------------------
# Breakpoints
# ---------------------------------------------------------------------------

def _send_breakpoints_for_file(session: DAPSession, local_file: str, timeout: float = 30.0) -> list[dict]:
    """Send the full setBreakpoints list for one file. Returns raw DAP breakpoint dicts."""
    remote_file = session.to_remote_path(local_file)
    bps_for_file = [bp for bp in session.breakpoints if bp.file == local_file]
    source_bps = [
        {"line": bp.line, **({"condition": bp.condition} if bp.condition else {})}
        for bp in bps_for_file
    ]
    resp = session._request("setBreakpoints", {
        "source": {"path": remote_file},
        "breakpoints": source_bps,
    }, timeout=timeout)
    dap_bps = resp.get("body", {}).get("breakpoints", [])
    for bp, dap_bp in zip(bps_for_file, dap_bps):
        bp.dap_id = dap_bp.get("id")
        bp.verified = dap_bp.get("verified", False)
    return dap_bps


@mcp.tool()
def debugpy_set_breakpoint(
    file: str,
    line: int,
    host: str = "localhost",
    port: int = DEFAULT_PORT,
    condition: str | None = None,
) -> dict[str, Any]:
    """Set a breakpoint at file:line. file should be a local path; path mapping is applied automatically.

    Returns a breakpoint ID (use with debugpy_remove_breakpoint).
    """
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}

    bp = DAPBreakpoint(file=file, line=line, condition=condition)
    session.breakpoints.append(bp)

    try:
        _send_breakpoints_for_file(session, file)
    except Exception as exc:
        session.breakpoints.remove(bp)
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"setBreakpoints failed: {exc}"]}

    session.ensure_disconnected_if_ephemeral()
    notes = []
    if not bp.verified:
        notes.append("Breakpoint not yet verified by debugpy. It may resolve when the module is loaded.")
    remote_file = session.to_remote_path(file)
    if remote_file != file:
        notes.append(f"Path mapped to remote: {remote_file}")
    return {
        "ok": True, "id": bp.internal_id, "file": file, "line": line,
        "verified": bp.verified, "condition": condition, "notes": notes,
    }


@mcp.tool()
def debugpy_remove_breakpoint(
    breakpoint_id: str,
    host: str = "localhost",
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Remove a breakpoint by its ID (returned from debugpy_set_breakpoint)."""
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}

    bp = next((b for b in session.breakpoints if b.internal_id == breakpoint_id), None)
    if bp is None:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"No breakpoint with id '{breakpoint_id}'."]}

    affected_file = bp.file
    session.breakpoints.remove(bp)

    try:
        _send_breakpoints_for_file(session, affected_file)
    except Exception as exc:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"Failed to update breakpoints after removal: {exc}"]}

    session.ensure_disconnected_if_ephemeral()
    return {"ok": True, "notes": [f"Breakpoint {breakpoint_id} removed."]}


@mcp.tool()
def debugpy_list_breakpoints(host: str = "localhost", port: int = DEFAULT_PORT) -> dict[str, Any]:
    """List all registered breakpoints for this session."""
    try:
        session = _get_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}
    return {
        "ok": True,
        "breakpoints": [bp.to_dict() for bp in session.breakpoints],
        "notes": [f"{len(session.breakpoints)} breakpoint(s) registered."],
    }


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

@mcp.tool()
def debugpy_threads(host: str = "localhost", port: int = DEFAULT_PORT) -> dict[str, Any]:
    """List active threads and stack frames. Stopped threads include full stack traces."""
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}

    try:
        threads_resp = session._request("threads")
    except Exception as exc:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"DAP threads error: {exc}"]}

    threads_out = []
    for t in threads_resp.get("body", {}).get("threads", []):
        tid = t["id"]
        entry: dict[str, Any] = {"id": tid, "name": t.get("name", ""), "frames": []}
        # Attempt stackTrace; will fail for running threads — that's fine
        try:
            st_resp = session._request("stackTrace", {"threadId": tid, "startFrame": 0, "levels": 20}, timeout=5.0)
            for frame in st_resp.get("body", {}).get("stackFrames", []):
                src = frame.get("source", {})
                remote_path = src.get("path", "")
                entry["frames"].append({
                    "id": frame["id"],
                    "name": frame.get("name", ""),
                    "file": session.to_local_path(remote_path),
                    "line": frame.get("line"),
                })
        except Exception:
            entry["frames"] = []
        threads_out.append(entry)

    session.ensure_disconnected_if_ephemeral()
    return {"ok": True, "threads": threads_out, "notes": []}


@mcp.tool()
def debugpy_variables(
    host: str = "localhost",
    port: int = DEFAULT_PORT,
    frame_id: int | None = None,
    scope: str = "locals",
) -> dict[str, Any]:
    """Inspect variables in a stopped frame.

    scope: 'locals' or 'globals' (case-insensitive match against DAP scope names).
    frame_id defaults to the current stopped frame.
    """
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}

    fid = frame_id
    if fid is None:
        with session._state_lock:
            fid = session.stopped_frame_id
    if fid is None:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": ["No stopped frame. Pause the process or hit a breakpoint first."]}

    try:
        scopes_resp = session._request("scopes", {"frameId": fid})
        scopes = scopes_resp.get("body", {}).get("scopes", [])
        matched = next(
            (s for s in scopes if s.get("name", "").lower() == scope.lower()),
            None,
        )
        if matched is None:
            available = [s.get("name") for s in scopes]
            session.ensure_disconnected_if_ephemeral()
            return {"ok": False, "notes": [f"Scope '{scope}' not found. Available: {available}"]}

        vars_resp = session._request("variables", {"variablesReference": matched["variablesReference"]})
        variables = [
            {"name": v["name"], "value": v.get("value", ""), "type": v.get("type", "")}
            for v in vars_resp.get("body", {}).get("variables", [])
        ]
    except Exception as exc:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"DAP error: {exc}", "Frame ID may be stale — call debugpy_threads for fresh IDs."]}

    session.ensure_disconnected_if_ephemeral()
    return {"ok": True, "frame_id": fid, "scope": scope, "variables": variables, "notes": []}


@mcp.tool()
def debugpy_evaluate(
    expression: str,
    host: str = "localhost",
    port: int = DEFAULT_PORT,
    frame_id: int | None = None,
    context: str = "watch",
) -> dict[str, Any]:
    """Evaluate a watch expression or REPL snippet.

    context: 'watch' (read-only style), 'repl' (can have side effects), or 'hover'.
    frame_id defaults to the current stopped frame.
    """
    try:
        session = _require_session(host, port)
    except ToolError as exc:
        return {"ok": False, "notes": [str(exc)]}

    fid = frame_id
    if fid is None:
        with session._state_lock:
            fid = session.stopped_frame_id

    args: dict[str, Any] = {"expression": expression, "context": context}
    if fid is not None:
        args["frameId"] = fid

    try:
        resp = session._request("evaluate", args)
    except Exception as exc:
        session.ensure_disconnected_if_ephemeral()
        return {"ok": False, "notes": [f"DAP error: {exc}", "Frame ID may be stale — call debugpy_threads."]}

    session.ensure_disconnected_if_ephemeral()
    if not resp.get("success"):
        return {
            "ok": False,
            "expression": expression,
            "notes": [resp.get("message", "Evaluation failed."), "Ensure the process is stopped at a breakpoint."],
        }
    body = resp.get("body", {})
    return {
        "ok": True,
        "expression": expression,
        "result": body.get("result"),
        "type": body.get("type"),
        "notes": [],
    }


def main() -> None:
    mcp.run()
