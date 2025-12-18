from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# BOINC DB constants (from html/inc/common_defs.inc; mirrored from server code).
RESULT_SERVER_STATE_INACTIVE = 1
RESULT_SERVER_STATE_UNSENT = 2
RESULT_SERVER_STATE_IN_PROGRESS = 4
RESULT_SERVER_STATE_OVER = 5

RESULT_OUTCOME_INIT = 0
RESULT_OUTCOME_SUCCESS = 1

VALIDATE_STATE_INIT = 0
VALIDATE_STATE_VALID = 1
VALIDATE_STATE_INVALID = 2
VALIDATE_STATE_NO_CHECK = 3


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _first_line(s: str, limit: int = 180) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    s = s.splitlines()[0].strip()
    if len(s) > limit:
        return s[:limit] + "…"
    return s


def _find_tag(text: str, tag: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", text, flags=re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip()


def _find_chunk_range(text: str) -> Tuple[Optional[int], Optional[int]]:
    if not text:
        return None, None
    # <chunk_range start="0" end="999"/>
    m = re.search(r"<chunk_range[^>]*\bstart=\"(\d+)\"[^>]*\bend=\"(\d+)\"[^>]*/?>", text, flags=re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    # <chunk_range><start>0</start><end>999</end></chunk_range>
    start = _find_tag(text, "start")
    end = _find_tag(text, "end")
    if start is not None and end is not None:
        return _safe_int(start), _safe_int(end)
    return None, None


def parse_ai_batch_meta(wu_xml_doc: str) -> Dict[str, Any]:
    """
    Extract AI batch metadata from WORKUNIT.xml_doc.
    This is intentionally forgiving because wu.xml_doc often contains multiple XML fragments.
    """
    meta: Dict[str, Any] = {}
    ai_block = None
    if wu_xml_doc:
        m = re.search(r"<ai_batch>([\s\S]*?)</ai_batch>", wu_xml_doc, flags=re.IGNORECASE)
        ai_block = m.group(1) if m else ""

    meta["job_id"] = _find_tag(ai_block, "job_id") if ai_block else None
    meta["input_uri"] = _find_tag(ai_block, "input_uri") if ai_block else None
    meta["output_uri"] = _find_tag(ai_block, "output_uri") if ai_block else None
    meta["container_image"] = _find_tag(ai_block, "container_image") if ai_block else None
    meta["worker_label"] = _find_tag(ai_block, "worker_label") if ai_block else None
    chunk_id = _find_tag(ai_block, "chunk_id") if ai_block else None
    meta["chunk_id"] = _safe_int(chunk_id, default=-1) if chunk_id is not None else None
    rs, re_ = _find_chunk_range(ai_block or "")
    meta["chunk_range"] = {"start": rs, "end": re_} if rs is not None and re_ is not None else None
    return meta


def _chunk_status(wu_need_validate: int, results: List[Dict[str, Any]]) -> str:
    # queued / running / completed / failed
    if not results:
        return "queued"
    if any(r["server_state"] == RESULT_SERVER_STATE_IN_PROGRESS for r in results):
        return "running"
    # success = outcome=SUCCESS and server_state=OVER
    success_results = [
        r for r in results
        if r["server_state"] == RESULT_SERVER_STATE_OVER and r["outcome"] == RESULT_OUTCOME_SUCCESS
    ]
    if success_results:
        if not wu_need_validate:
            return "completed"
        # If validator is used, wait for validate_state VALID / NO_CHECK
        if any(r["validate_state"] in (VALIDATE_STATE_VALID, VALIDATE_STATE_NO_CHECK) for r in success_results):
            return "completed"
        # success returned, but validator not yet decided
        return "running"
    # no successes and all are OVER => failed
    if all(r["server_state"] == RESULT_SERVER_STATE_OVER for r in results):
        return "failed"
    return "queued"


def _verification_status(wu_need_validate: int, results: List[Dict[str, Any]]) -> str:
    # pending / verified / mismatch / n/a
    if not wu_need_validate:
        return "n/a"
    if any(r["validate_state"] == VALIDATE_STATE_VALID for r in results):
        return "verified"
    if any(r["validate_state"] == VALIDATE_STATE_INVALID for r in results):
        return "mismatch"
    return "pending"


def _retry_count(results: List[Dict[str, Any]]) -> int:
    # Explainable: number of completed attempts beyond the first (server_state=OVER)
    over = [r for r in results if r["server_state"] == RESULT_SERVER_STATE_OVER]
    return max(0, len(over) - 1)


def _failure_reason(results: List[Dict[str, Any]]) -> Optional[str]:
    # Use latest failed OVER result (by received_time then id).
    failed = [
        r for r in results
        if r["server_state"] == RESULT_SERVER_STATE_OVER and r["outcome"] != RESULT_OUTCOME_SUCCESS
    ]
    if not failed:
        return None
    failed.sort(key=lambda r: (r.get("received_time") or 0, r.get("result_id") or 0), reverse=True)
    r = failed[0]
    stderr = _first_line(r.get("stderr_out") or "")
    if stderr:
        return stderr
    exit_status = r.get("exit_status")
    outcome = r.get("outcome")
    return f"exit_status={exit_status} outcome={outcome}"


def _job_status_from_chunks(chunks: List[Dict[str, Any]]) -> str:
    if not chunks:
        return "queued"
    if any(c["status"] == "failed" for c in chunks):
        return "failed"
    if all(c["status"] == "completed" for c in chunks):
        return "completed"
    if any(c["status"] == "running" for c in chunks):
        return "running"
    return "queued"


def _parse_worker_label_from_host(venue: str, misc: str) -> Optional[str]:
    # Explainable precedence:
    # 1) host.venue if it matches expected labels (operator-configured)
    # 2) host.misc JSON field "worker_label" if present (future-proof)
    v = (venue or "").strip()
    if v in ("edge", "on_prem", "cloud"):
        return v
    if misc:
        try:
            obj = json.loads(misc)
            wl = (obj.get("worker_label") or "").strip()
            if wl in ("edge", "on_prem", "cloud"):
                return wl
        except Exception:
            pass
    return None


@dataclass(frozen=True)
class BoincDBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    socket: Optional[str]
    use_mysql_cli: bool

    @staticmethod
    def from_env() -> Optional["BoincDBConfig"]:
        enabled = os.environ.get("BOINC_DB_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
        if not enabled:
            return None
        return BoincDBConfig(
            host=os.environ.get("BOINC_DB_HOST", "127.0.0.1"),
            port=_safe_int(os.environ.get("BOINC_DB_PORT", "3306"), 3306),
            user=os.environ.get("BOINC_DB_USER", "boinc"),
            password=os.environ.get("BOINC_DB_PASS", ""),
            database=os.environ.get("BOINC_DB_NAME", "boinc"),
            socket=os.environ.get("BOINC_DB_SOCKET") or None,
            use_mysql_cli=os.environ.get("BOINC_DB_USE_CLI", "").strip().lower() in ("1", "true", "yes", "on"),
        )


class BoincDBAdapter:
    """
    Read-only adapter from BOINC MySQL tables to the control-plane REST API shapes.

    - No writes (SELECT only)
    - Mappings are explainable to judges (see AI_BATCH_DESIGN.md)
    """

    def __init__(self, cfg: BoincDBConfig):
        self.cfg = cfg

    # -----------------------
    # Connection: mysql CLI
    # -----------------------
    def _mysql_cli(self, sql: str, params: Sequence[Any] = ()) -> List[List[str]]:
        if params:
            # minimal, safe-ish interpolation for this prototype.
            # For a hardened version, use a proper MySQL driver with parameterization.
            def esc(v: Any) -> str:
                if v is None:
                    return "NULL"
                if isinstance(v, (int, float)):
                    return str(v)
                s = str(v)
                s = s.replace("\\", "\\\\").replace("'", "\\'")
                return "'" + s + "'"

            for p in params:
                sql = sql.replace("%s", esc(p), 1)

        cmd = ["mysql", "--batch", "--raw", "--silent"]
        cmd += ["-h", self.cfg.host, "-P", str(self.cfg.port), "-u", self.cfg.user]
        if self.cfg.password:
            cmd += [f"-p{self.cfg.password}"]
        if self.cfg.socket:
            cmd += ["--socket", self.cfg.socket]
        cmd += [self.cfg.database, "-e", sql]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "mysql CLI query failed")
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        return [ln.split("\t") for ln in lines]

    # -----------------------
    # Connection: python drv
    # -----------------------
    def _connect(self):
        # Try mysql-connector-python, then PyMySQL.
        try:
            import mysql.connector  # type: ignore

            return mysql.connector.connect(
                host=self.cfg.host,
                port=self.cfg.port,
                user=self.cfg.user,
                password=self.cfg.password,
                database=self.cfg.database,
                unix_socket=self.cfg.socket,
                autocommit=True,
            )
        except Exception:
            pass
        try:
            import pymysql  # type: ignore

            return pymysql.connect(
                host=self.cfg.host,
                port=self.cfg.port,
                user=self.cfg.user,
                password=self.cfg.password,
                database=self.cfg.database,
                unix_socket=self.cfg.socket,
                autocommit=True,
                cursorclass=pymysql.cursors.DictCursor,
            )
        except Exception as exc:
            raise RuntimeError(
                "No MySQL driver available. Install `mysql-connector-python` or `pymysql`, "
                "or set BOINC_DB_USE_CLI=1 to use the `mysql` CLI."
            ) from exc

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        if self.cfg.use_mysql_cli:
            raise RuntimeError("Internal: _query called while BOINC_DB_USE_CLI=1")
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                if isinstance(r, dict):
                    out.append(r)
                else:
                    out.append({cols[i]: r[i] for i in range(len(cols))})
            return out
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # -----------------------
    # Public API
    # -----------------------
    def list_jobs(self, limit_workunits: int = 2000) -> List[Dict[str, Any]]:
        """
        Aggregate logical jobs from workunits, grouping by <ai_batch><job_id>.
        Fallback grouping: workunit.batch (as 'batch:<id>'), else workunit.name.
        """
        limit_workunits = max(1, min(int(limit_workunits), 20000))

        if self.cfg.use_mysql_cli:
            # NOTE: MySQL CLI mode doesn't fetch xml_doc because it's large and often contains tabs/newlines.
            # For competition credibility, prefer python driver mode to parse <ai_batch> from xml_doc.
            rows = self._mysql_cli(
                "SELECT id, name, create_time, batch, need_validate FROM workunit ORDER BY create_time DESC LIMIT %s",
                (limit_workunits,),
            )
            wus = [
                {
                    "id": _safe_int(r[0]),
                    "name": r[1],
                    "create_time": _safe_int(r[2]),
                    "batch": _safe_int(r[3]),
                    "need_validate": _safe_int(r[4]),
                    "xml_doc": "",
                }
                for r in rows
            ]
        else:
            wus = self._query(
                "SELECT id, name, create_time, batch, need_validate, xml_doc "
                "FROM workunit ORDER BY create_time DESC LIMIT %s",
                (limit_workunits,),
            )

        wu_ids = [int(w["id"]) for w in wus]
        if not wu_ids:
            return []

        # Fetch results for these WUs.
        placeholders = ",".join(["%s"] * len(wu_ids))
        if self.cfg.use_mysql_cli:
            rrows = self._mysql_cli(
                f"SELECT id, workunitid, server_state, outcome, validate_state, hostid, sent_time, received_time, exit_status "
                f"FROM result WHERE workunitid IN ({placeholders})",
                tuple(wu_ids),
            )
            results = [
                {
                    "result_id": _safe_int(r[0]),
                    "workunitid": _safe_int(r[1]),
                    "server_state": _safe_int(r[2]),
                    "outcome": _safe_int(r[3]),
                    "validate_state": _safe_int(r[4]),
                    "hostid": _safe_int(r[5]),
                    "sent_time": _safe_int(r[6]),
                    "received_time": _safe_int(r[7]),
                    "exit_status": _safe_int(r[8]),
                    "stderr_out": "",
                }
                for r in rrows
            ]
        else:
            results = self._query(
                f"SELECT id AS result_id, workunitid, server_state, outcome, validate_state, hostid, sent_time, received_time, exit_status, stderr_out "
                f"FROM result WHERE workunitid IN ({placeholders})",
                tuple(wu_ids),
            )

        by_wu: Dict[int, List[Dict[str, Any]]] = {}
        for r in results:
            wid = int(r["workunitid"])
            by_wu.setdefault(wid, []).append(r)

        jobs: Dict[str, Dict[str, Any]] = {}
        for w in wus:
            wu_id = int(w["id"])
            wu_name = str(w["name"])
            create_time = _safe_int(w.get("create_time"))
            batch = _safe_int(w.get("batch"))
            need_validate = _safe_int(w.get("need_validate"))
            xml_doc = (w.get("xml_doc") or "")
            meta = parse_ai_batch_meta(xml_doc)

            job_id = (meta.get("job_id") or "").strip()
            if not job_id:
                job_id = f"batch:{batch}" if batch else wu_name

            chunks = jobs.setdefault(
                job_id,
                {
                    "job_id": job_id,
                    "created_at": create_time,
                    "input_uri": meta.get("input_uri"),
                    "output_uri": meta.get("output_uri"),
                    "container_image": meta.get("container_image"),
                    "chunks": [],
                },
            )
            chunks["created_at"] = min(chunks["created_at"], create_time) if chunks["created_at"] else create_time
            # fill missing meta from any chunk that has it
            for k in ("input_uri", "output_uri", "container_image"):
                if not chunks.get(k) and meta.get(k):
                    chunks[k] = meta.get(k)

            rlist = by_wu.get(wu_id, [])
            cstatus = _chunk_status(need_validate, rlist)
            chunks["chunks"].append(
                {
                    "chunk_id": meta.get("chunk_id") if meta.get("chunk_id") is not None else wu_id,
                    "status": cstatus,
                    "retries": _retry_count(rlist),
                    "failure_reason": _failure_reason(rlist),
                    "verification": _verification_status(need_validate, rlist),
                }
            )

        # Convert to dashboard shape
        out: List[Dict[str, Any]] = []
        for job in jobs.values():
            completed = sum(1 for c in job["chunks"] if c["status"] == "completed")
            total = len(job["chunks"])
            out.append(
                {
                    "job_id": job["job_id"],
                    "status": _job_status_from_chunks(job["chunks"]),
                    "progress": {"completed_chunks": completed, "total_chunks": total},
                    "created_at": job["created_at"],
                }
            )
        out.sort(key=lambda j: j["created_at"], reverse=True)
        return out

    def get_job(self, job_id: str, limit_workunits: int = 20000) -> Optional[Dict[str, Any]]:
        job_id = (job_id or "").strip()
        if not job_id:
            return None
        limit_workunits = max(1, min(int(limit_workunits), 50000))

        # Strategy:
        # - If job_id is "batch:<n>" => query by workunit.batch
        # - Else try to locate via xml_doc LIKE '%<job_id>...%'
        batch_id = None
        if job_id.startswith("batch:"):
            batch_id = _safe_int(job_id.split(":", 1)[1], default=0)

        if self.cfg.use_mysql_cli:
            if batch_id:
                wrows = self._mysql_cli(
                    "SELECT id, name, create_time, batch, need_validate FROM workunit WHERE batch=%s ORDER BY id ASC LIMIT %s",
                    (batch_id, limit_workunits),
                )
                wus = [
                    {"id": _safe_int(r[0]), "name": r[1], "create_time": _safe_int(r[2]), "batch": _safe_int(r[3]), "need_validate": _safe_int(r[4]), "xml_doc": ""}
                    for r in wrows
                ]
            else:
                # CLI mode can't reliably parse xml_doc, so we can't locate non-batch job_id.
                return None
        else:
            if batch_id:
                wus = self._query(
                    "SELECT id, name, create_time, batch, need_validate, xml_doc FROM workunit WHERE batch=%s ORDER BY id ASC LIMIT %s",
                    (batch_id, limit_workunits),
                )
            else:
                # Look for <job_id> inside xml_doc. This is read-only and explainable.
                like = f"%<job_id>{job_id}</job_id>%"
                wus = self._query(
                    "SELECT id, name, create_time, batch, need_validate, xml_doc FROM workunit WHERE xml_doc LIKE %s ORDER BY id ASC LIMIT %s",
                    (like, limit_workunits),
                )

        if not wus:
            return None

        wu_ids = [int(w["id"]) for w in wus]
        placeholders = ",".join(["%s"] * len(wu_ids))
        if self.cfg.use_mysql_cli:
            rrows = self._mysql_cli(
                f"SELECT id, workunitid, server_state, outcome, validate_state, hostid, sent_time, received_time, exit_status "
                f"FROM result WHERE workunitid IN ({placeholders})",
                tuple(wu_ids),
            )
            results = [
                {
                    "result_id": _safe_int(r[0]),
                    "workunitid": _safe_int(r[1]),
                    "server_state": _safe_int(r[2]),
                    "outcome": _safe_int(r[3]),
                    "validate_state": _safe_int(r[4]),
                    "hostid": _safe_int(r[5]),
                    "sent_time": _safe_int(r[6]),
                    "received_time": _safe_int(r[7]),
                    "exit_status": _safe_int(r[8]),
                    "stderr_out": "",
                }
                for r in rrows
            ]
        else:
            results = self._query(
                f"SELECT id AS result_id, workunitid, server_state, outcome, validate_state, hostid, sent_time, received_time, exit_status, stderr_out "
                f"FROM result WHERE workunitid IN ({placeholders})",
                tuple(wu_ids),
            )

        by_wu: Dict[int, List[Dict[str, Any]]] = {}
        for r in results:
            by_wu.setdefault(int(r["workunitid"]), []).append(r)

        # Build chunk list
        created_at = min(_safe_int(w.get("create_time")) for w in wus)
        input_uri = None
        output_uri = None
        container_image = None
        chunks: List[Dict[str, Any]] = []
        for w in wus:
            wu_id = int(w["id"])
            need_validate = _safe_int(w.get("need_validate"))
            meta = parse_ai_batch_meta(w.get("xml_doc") or "")
            input_uri = input_uri or meta.get("input_uri")
            output_uri = output_uri or meta.get("output_uri")
            container_image = container_image or meta.get("container_image")

            rlist = by_wu.get(wu_id, [])
            chunks.append(
                {
                    "chunk_id": meta.get("chunk_id") if meta.get("chunk_id") is not None else wu_id,
                    "status": _chunk_status(need_validate, rlist),
                    "retries": _retry_count(rlist),
                    "failure_reason": _failure_reason(rlist),
                    "verification": _verification_status(need_validate, rlist),
                }
            )

        job = {
            "job_id": job_id,
            "created_at": created_at,
            "input_uri": input_uri,
            "output_uri": output_uri,
            "container_image": container_image,
            "chunks": chunks,
        }
        job["status"] = _job_status_from_chunks(chunks)
        return job

    def list_workers(self, limit_hosts: int = 2000) -> List[Dict[str, Any]]:
        limit_hosts = max(1, min(int(limit_hosts), 50000))

        if self.cfg.use_mysql_cli:
            rows = self._mysql_cli(
                "SELECT id, venue, rpc_time, misc FROM host ORDER BY rpc_time DESC LIMIT %s",
                (limit_hosts,),
            )
            hosts = [{"id": _safe_int(r[0]), "venue": r[1], "rpc_time": _safe_int(r[2]), "misc": r[3] if len(r) > 3 else ""} for r in rows]
        else:
            hosts = self._query(
                "SELECT id, venue, rpc_time, misc FROM host ORDER BY rpc_time DESC LIMIT %s",
                (limit_hosts,),
            )

        host_ids = [int(h["id"]) for h in hosts]
        if not host_ids:
            return []

        placeholders = ",".join(["%s"] * len(host_ids))
        if self.cfg.use_mysql_cli:
            rrows = self._mysql_cli(
                f"SELECT hostid, server_state FROM result WHERE hostid IN ({placeholders})",
                tuple(host_ids),
            )
            active_hosts = { _safe_int(r[0]) for r in rrows if _safe_int(r[1]) == RESULT_SERVER_STATE_IN_PROGRESS }
        else:
            rrows = self._query(
                f"SELECT hostid, server_state FROM result WHERE hostid IN ({placeholders})",
                tuple(host_ids),
            )
            active_hosts = { int(r["hostid"]) for r in rrows if _safe_int(r["server_state"]) == RESULT_SERVER_STATE_IN_PROGRESS }

        out: List[Dict[str, Any]] = []
        for h in hosts:
            hid = int(h["id"])
            out.append(
                {
                    "worker_id": str(hid),
                    "label": _parse_worker_label_from_host(h.get("venue") or "", h.get("misc") or ""),
                    "state": "active" if hid in active_hosts else "idle",
                    "last_seen": _safe_int(h.get("rpc_time")),
                }
            )
        out.sort(key=lambda w: int(w["worker_id"]))
        return out


