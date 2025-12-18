#!/usr/bin/env python3
"""
Lightweight AI Genie UI control surface.

Exposes:
- Static UI (index.html, CSS/JS).
- JSON APIs for toggling server/client, running Hello World tests.
- Plain-text log links for server/client terminal feeds.
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
import threading
import time
import tempfile
import errno
import io
import contextlib
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from urllib.parse import parse_qs, urlparse

# Allow running as a script (python3 ai_genie_ui/server.py) while still importing
# ai_genie_ui.* as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_genie_ui.boinc_db_adapter import BoincDBAdapter, BoincDBConfig

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
HOST = os.environ.get("AI_GENIE_HOST", "0.0.0.0")
PORT = int(os.environ.get("AI_GENIE_PORT", "8080"))
LOG_LIMIT = 500

STATE = {
    "server_active": False,
    "client_active": False,
    # Default to Python mode so the UI works out-of-the-box on machines without Docker running.
    "selected_mode": "python",
    "docker_reference": "",
    "python_code": "print('Hello World from Python!')\n",
    "last_run_result": "",
}

LOGS: Dict[str, List[str]] = {"server": [], "client": []}
STATE_LOCK = threading.Lock()

# A persistent Python execution context so users can "actually run Python code"
# across multiple submissions (variables/imports persist until restart).
PY_SESSION_LOCK = threading.Lock()
PY_SESSION_GLOBALS: Dict[str, object] = {"__name__": "__main__"}
PY_OUTPUT_CHAR_LIMIT = 20000

def _now_ts() -> int:
    return int(time.time())


# -----------------------------------------------------------------------------
# Minimal "control plane" model (prototype).
# For demo purposes this is in-memory but intentionally shaped like something
# that could be backed by BOINC DB queries or GUI RPC output.
# -----------------------------------------------------------------------------

JobStatus = str  # queued | running | completed | failed
ChunkStatus = str  # queued | running | completed | failed


def _job_status_from_chunks(chunks: List[Dict[str, Any]]) -> JobStatus:
    if not chunks:
        return "queued"
    if any(c["status"] == "failed" for c in chunks):
        return "failed"
    if all(c["status"] == "completed" for c in chunks):
        return "completed"
    if any(c["status"] == "running" for c in chunks):
        return "running"
    return "queued"


CONTROL_LOCK = threading.Lock()
JOBS: Dict[str, Dict[str, Any]] = {}
WORKERS: Dict[str, Dict[str, Any]] = {
    "edge-1": {"worker_id": "edge-1", "label": "edge", "state": "idle", "active_job_id": None, "active_chunk_id": None, "last_seen": _now_ts()},
    "onprem-1": {"worker_id": "onprem-1", "label": "on_prem", "state": "idle", "active_job_id": None, "active_chunk_id": None, "last_seen": _now_ts()},
    "cloud-1": {"worker_id": "cloud-1", "label": "cloud", "state": "idle", "active_job_id": None, "active_chunk_id": None, "last_seen": _now_ts()},
}


def list_jobs() -> List[Dict[str, Any]]:
    with CONTROL_LOCK:
        out: List[Dict[str, Any]] = []
        for job in JOBS.values():
            chunks = job["chunks"]
            completed = sum(1 for c in chunks if c.get("status") == "completed")
            out.append(
                {
                    "job_id": job["job_id"],
                    "status": _job_status_from_chunks(chunks),
                    "created_at": job["created_at"],
                    "progress": {"completed_chunks": completed, "total_chunks": len(chunks)},
                }
            )
        out.sort(key=lambda j: j["created_at"], reverse=True)
        return out


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with CONTROL_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        job_copy = json.loads(json.dumps(job))
    chunks = []
    for c in job_copy.get("chunks", []):
        chunks.append(
            {
                "chunk_id": c.get("chunk_id"),
                "status": c.get("status", "queued"),
                "retries": c.get("retries", 0),
                "failure_reason": c.get("failure_reason"),
                "verification": (c.get("output_verification") or {}).get("status", "pending"),
            }
        )
    return {
        "job_id": job_copy.get("job_id"),
        "created_at": job_copy.get("created_at"),
        "input_uri": job_copy.get("input_uri"),
        "output_uri": job_copy.get("output_uri"),
        "container_image": job_copy.get("container_image"),
        "status": _job_status_from_chunks(job_copy.get("chunks", [])),
        "chunks": chunks,
    }


def create_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    job_id = (payload.get("job_id") or "").strip() or str(uuid.uuid4())
    input_uri = (payload.get("input_uri") or "").strip()
    output_uri = (payload.get("output_uri") or "").strip()
    container_image = (payload.get("container_image") or "").strip()
    worker_label = (payload.get("worker_label") or "").strip() or None
    num_chunks = int(payload.get("num_chunks") or 1)
    chunk_range = payload.get("chunk_range") or {}
    start = int(chunk_range.get("start") or 0)
    end = int(chunk_range.get("end") or max(start, num_chunks - 1))

    if not input_uri or not output_uri or not container_image:
        raise ValueError("input_uri, output_uri, and container_image are required")
    if num_chunks < 1 or num_chunks > 100000:
        raise ValueError("num_chunks must be between 1 and 100000")

    # Simple chunking: evenly partition [start, end] into num_chunks contiguous ranges.
    total = max(0, end - start + 1)
    if total == 0:
        ranges = [(0, -1)] * num_chunks
    else:
        base = total // num_chunks
        extra = total % num_chunks
        ranges = []
        cursor = start
        for i in range(num_chunks):
            size = base + (1 if i < extra else 0)
            r0 = cursor
            r1 = cursor + size - 1
            ranges.append((r0, r1))
            cursor = r1 + 1

    chunks: List[Dict[str, Any]] = []
    for i, (r0, r1) in enumerate(ranges):
        chunks.append(
            {
                "chunk_id": i,
                "chunk_range": {"start": r0, "end": r1},
                "status": "queued",
                "retries": 0,
                "failure_reason": None,
                # Prototype-only: in BOINC-backed mode this will come from validator state / receipt checks.
                "output_verification": {"status": "pending"},
                "assigned_worker_id": None,
                "updated_at": _now_ts(),
            }
        )

    job = {
        "job_id": job_id,
        "created_at": _now_ts(),
        "input_uri": input_uri,
        "output_uri": output_uri,
        "container_image": container_image,
        "worker_label": worker_label,
        "chunks": chunks,
    }

    with CONTROL_LOCK:
        if job_id in JOBS:
            raise ValueError("job_id already exists")
        JOBS[job_id] = job
    return get_job(job_id) or job


def list_workers() -> List[Dict[str, Any]]:
    with CONTROL_LOCK:
        workers = []
        for w in WORKERS.values():
            workers.append(
                {
                    "worker_id": w["worker_id"],
                    "label": w.get("label"),
                    "state": w.get("state", "idle"),
                    "last_seen": w.get("last_seen"),
                }
            )
        workers.sort(key=lambda w: w["worker_id"])
        return workers


DB_CFG = BoincDBConfig.from_env()
DB_ADAPTER: Optional[BoincDBAdapter] = BoincDBAdapter(DB_CFG) if DB_CFG else None


def _api_list_jobs() -> List[Dict[str, Any]]:
    if DB_ADAPTER:
        # Read-only projection from BOINC DB.
        return DB_ADAPTER.list_jobs()
    return list_jobs()


def _api_get_job(job_id: str) -> Optional[Dict[str, Any]]:
    if DB_ADAPTER:
        return DB_ADAPTER.get_job(job_id)
    return get_job(job_id)


def _api_list_workers() -> List[Dict[str, Any]]:
    if DB_ADAPTER:
        return DB_ADAPTER.list_workers()
    return list_workers()


def append_log(target: str, message: str) -> None:
    if target not in LOGS:
        return
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    LOGS[target].append(entry)
    if len(LOGS[target]) > LOG_LIMIT:
        LOGS[target] = LOGS[target][-LOG_LIMIT:]


class AIUIRequestHandler(BaseHTTPRequestHandler):
    server_version = "AI-Genie-UI/1.0"

    def _send_json(self, payload: Dict, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text: str, status: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, file_path: Path, *, head_only: bool = False) -> None:
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404, "File not found")
            return

        mime = "text/plain"
        if file_path.suffix == ".html":
            mime = "text/html; charset=utf-8"
        elif file_path.suffix == ".css":
            mime = "text/css; charset=utf-8"
        elif file_path.suffix == ".js":
            mime = "application/javascript; charset=utf-8"
        elif file_path.suffix in {".png", ".jpg", ".jpeg", ".gif"}:
            mime = f"image/{file_path.suffix.lstrip('.')}"
        elif file_path.suffix == ".svg":
            mime = "image/svg+xml; charset=utf-8"
        elif file_path.suffix == ".json":
            mime = "application/json; charset=utf-8"

        size = file_path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if not head_only:
            self.wfile.write(file_path.read_bytes())

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._serve_static(STATIC_DIR / "index.html")
            return

        if route == "/server":
            self._serve_static(STATIC_DIR / "server.html")
            return

        if route == "/client":
            self._serve_static(STATIC_DIR / "client.html")
            return

        if route == "/jobs":
            self._serve_static(STATIC_DIR / "jobs.html")
            return

        if route == "/job":
            self._serve_static(STATIC_DIR / "job.html")
            return

        if route == "/workers":
            self._serve_static(STATIC_DIR / "workers.html")
            return

        if route.startswith("/static/"):
            relative = route[len("/static/") :]
            target = (STATIC_DIR / relative).resolve()
            if STATIC_DIR in target.parents or target == STATIC_DIR:
                self._serve_static(target)
            else:
                self.send_error(403, "Forbidden")
            return

        if route == "/api/state":
            with STATE_LOCK:
                payload = {
                    "state": STATE,
                    "logs": {k: len(v) for k, v in LOGS.items()},
                }
            self._send_json(payload)
            return

        if route == "/api/jobs":
            try:
                self._send_json({"jobs": _api_list_jobs()})
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=503)
            return

        if route.startswith("/api/jobs/"):
            job_id = route[len("/api/jobs/") :]
            try:
                job = _api_get_job(job_id)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=503)
                return
            if not job:
                self._send_json({"error": "Job not found"}, status=404)
                return
            self._send_json(job)
            return

        if route == "/api/workers":
            try:
                self._send_json({"workers": _api_list_workers()})
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=503)
            return

        if route == "/api/logs":
            params = parse_qs(parsed.query)
            target = params.get("target", ["server"])[0]
            if target not in LOGS:
                self._send_json({"error": "Unknown log target"}, status=400)
                return
            with STATE_LOCK:
                entries = LOGS[target][-200:]
            self._send_json({"target": target, "entries": entries})
            return

        if route in ("/logs/server", "/logs/client"):
            target = route.split("/")[-1]
            with STATE_LOCK:
                text = "\n".join(LOGS[target]) or "No log entries yet."
            self._send_text(text)
            return

        self.send_error(404, "Not found")

    def do_HEAD(self) -> None:
        """
        Support HEAD requests (e.g. curl -I, load balancer / health checks).
        Python's BaseHTTPRequestHandler returns 501 if do_HEAD is missing.
        """
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._serve_static(STATIC_DIR / "index.html", head_only=True)
            return

        if route == "/server":
            self._serve_static(STATIC_DIR / "server.html", head_only=True)
            return

        if route == "/client":
            self._serve_static(STATIC_DIR / "client.html", head_only=True)
            return

        if route == "/jobs":
            self._serve_static(STATIC_DIR / "jobs.html", head_only=True)
            return

        if route == "/job":
            self._serve_static(STATIC_DIR / "job.html", head_only=True)
            return

        if route == "/workers":
            self._serve_static(STATIC_DIR / "workers.html", head_only=True)
            return

        if route.startswith("/static/"):
            relative = route[len("/static/") :]
            target = (STATIC_DIR / relative).resolve()
            if STATIC_DIR in target.parents or target == STATIC_DIR:
                self._serve_static(target, head_only=True)
            else:
                self.send_error(403, "Forbidden")
            return

        # For API routes, respond with a lightweight OK so HEAD doesn't error.
        if route in (
            "/api/state",
            "/api/logs",
            "/api/toggle",
            "/api/run",
            "/api/jobs",
            "/api/workers",
            "/logs/server",
            "/logs/client",
        ) or route.startswith("/api/jobs/"):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_error(404, "Not found")

    def _read_json_body(self) -> Dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/toggle":
            data = self._read_json_body()
            target = data.get("target")
            action = data.get("action")
            if target not in ("server", "client") or action not in ("activate", "deactivate"):
                self._send_json({"error": "Invalid toggle payload"}, status=400)
                return
            flag = action == "activate"
            with STATE_LOCK:
                STATE[f"{target}_active"] = flag
            append_log(target, f"{target.title()} {'activated' if flag else 'deactivated'}.")
            self._send_json({"ok": True, "state": STATE})
            return

        if parsed.path == "/api/run":
            data = self._read_json_body()
            mode = data.get("mode")
            docker_ref = (data.get("docker_reference") or "").strip()
            python_code = data.get("python_code") or ""
            if mode not in ("docker", "python"):
                self._send_json({"error": "Mode must be docker or python"}, status=400)
                return

            with STATE_LOCK:
                STATE["selected_mode"] = mode
                STATE["docker_reference"] = docker_ref
                STATE["python_code"] = python_code

            append_log("client", f"Client requested Hello World via {mode}.")
            append_log("server", "Server preparing Hello World test environment.")

            if mode == "docker":
                ref = docker_ref or "hello-world"
                result, success = run_docker_test(ref)
            else:
                code = python_code.strip() or "print('Hello World from Python!')"
                result, success = run_python_test(code)

            append_log("client", result)
            with STATE_LOCK:
                STATE["last_run_result"] = result

            status = 200 if success else 400
            self._send_json({"ok": success, "result": result}, status=status)
            return

        if parsed.path == "/api/jobs":
            # In DB mode, the control plane is read-only by design.
            if DB_ADAPTER:
                self._send_json({"error": "Read-only mode: BOINC DB adapter enabled"}, status=405)
                return
            data = self._read_json_body()
            try:
                job = create_job(data)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            self._send_json(job, status=201)
            return

        self.send_error(404, "Unknown endpoint")

    def log_message(self, format: str, *args) -> None:
        # Silence default console logging to avoid noise
        pass


def run_docker_test(image: str) -> Tuple[str, bool]:
    append_log("server", f"Running docker image '{image}' ...")
    cmd = ["docker", "run", "--rm", image]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90, check=False
        )
    except FileNotFoundError:
        msg = "Docker executable not found. Install Docker to use this mode."
        append_log("server", msg)
        return msg, False
    except subprocess.TimeoutExpired:
        msg = "Docker run timed out after 90s."
        append_log("server", msg)
        return msg, False

    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    append_log("server", stdout or "(no STDOUT)")
    if stderr:
        append_log("server", f"STDERR: {stderr}")

    if proc.returncode != 0:
        msg = stderr or f"Docker exited with status {proc.returncode}"
        return msg, False

    msg = stdout or "Docker run finished without output."
    return msg, True


def run_python_test(code: str) -> Tuple[str, bool]:
    append_log("server", "Executing provided Python snippet (persistent session).")
    output = io.StringIO()
    ok = True
    try:
        compiled = compile(code, "<ai-genie-python>", "exec")
        with PY_SESSION_LOCK:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                exec(compiled, PY_SESSION_GLOBALS, PY_SESSION_GLOBALS)
    except Exception:
        ok = False
        output.write(traceback.format_exc())

    text = output.getvalue().rstrip()
    if not text:
        text = "OK (no output)."

    if len(text) > PY_OUTPUT_CHAR_LIMIT:
        text = text[:PY_OUTPUT_CHAR_LIMIT] + "\n\n... (output truncated) ..."

    # Mirror the result into the server log for easy debugging.
    append_log("server", text if ok else f"Python error:\n{text}")
    return text, ok


def run_server() -> None:
    port_env_set = "AI_GENIE_PORT" in os.environ
    host = HOST
    port = PORT

    def _try_bind(bind_port: int) -> HTTPServer:
        return HTTPServer((host, bind_port), AIUIRequestHandler)

    try:
        httpd = _try_bind(port)
    except OSError as exc:
        # Common: [Errno 48] Address already in use (macOS) / [Errno 98] (Linux)
        addr_in_use = exc.errno in (errno.EADDRINUSE, 48, 98)
        if addr_in_use and not port_env_set:
            # If the default port is busy, fall back automatically so "python3 server.py" always works.
            # Try a small range first for predictable URLs, then finally ask the OS for any free port.
            httpd = None
            for candidate in range(port + 1, port + 21):
                try:
                    httpd = _try_bind(candidate)
                    port = candidate
                    break
                except OSError as exc2:
                    if exc2.errno in (errno.EADDRINUSE, 48, 98):
                        continue
                    raise
            if httpd is None:
                httpd = _try_bind(0)
                port = httpd.server_port
            print(
                "Port 8080 is already in use; started AI Genie UI on a free port instead.\n"
                f"- Bind address: {host}:{port}\n",
                flush=True,
            )
        else:
            print(
                "Failed to start AI Genie UI.\n"
                f"- Bind address: {host}:{port}\n"
                f"- Error: {exc}\n\n"
                "Try one of the following:\n"
                f"- Set a different port: AI_GENIE_PORT=8081 python3 {__file__}\n"
                "- Stop the process currently using that port.\n",
                flush=True,
            )
            raise

    # Print URLs that work for copy/paste regardless of HOST (0.0.0.0 isn't a browser destination).
    local_base = f"http://127.0.0.1:{port}"
    print(f"AI Genie UI running on {local_base}", flush=True)
    print(f"Server log feed: {local_base}/logs/server", flush=True)
    print(f"Client log feed: {local_base}/logs/client", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down AI Genie UI.", flush=True)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run_server()

