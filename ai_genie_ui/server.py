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
import subprocess
import threading
import time
import tempfile
import errno
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

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

        if route.startswith("/static/"):
            relative = route[len("/static/") :]
            target = (STATIC_DIR / relative).resolve()
            if STATIC_DIR in target.parents or target == STATIC_DIR:
                self._serve_static(target, head_only=True)
            else:
                self.send_error(403, "Forbidden")
            return

        # For API routes, respond with a lightweight OK so HEAD doesn't error.
        if route in ("/api/state", "/api/logs", "/api/toggle", "/api/run", "/logs/server", "/logs/client"):
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
    append_log("server", "Executing provided Python snippet.")
    fd, path = tempfile.mkstemp(suffix=".py", text=True)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(code)
        proc = subprocess.run(
            ["python3", path], capture_output=True, text=True, timeout=60, check=False
        )
    except subprocess.TimeoutExpired:
        msg = "Python script timed out after 60s."
        append_log("server", msg)
        return msg, False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    append_log("server", stdout or "(no STDOUT)")
    if stderr:
        append_log("server", f"STDERR: {stderr}")

    if proc.returncode != 0:
        msg = stderr or f"Python exited with status {proc.returncode}"
        return msg, False

    msg = stdout or "Python script finished without output."
    return msg, True


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

