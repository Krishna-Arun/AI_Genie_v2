## AI Genie Control Plane (Phase 1: Read-only BOINC DB)

This UI/control plane is designed to be **truthful**: when BOINC DB mode is enabled, the REST API is a **read-only projection** of BOINC scheduler state.

### Enable BOINC DB mode

Set env vars and run:

```bash
BOINC_DB_ENABLED=1 \
BOINC_DB_HOST=127.0.0.1 \
BOINC_DB_PORT=3306 \
BOINC_DB_NAME=boinc \
BOINC_DB_USER=boinc_ro \
BOINC_DB_PASS=... \
python3 ai_genie_ui/server.py
```

Optional:
- **`BOINC_DB_SOCKET`**: use a local MySQL socket path instead of TCP
- **`BOINC_DB_USE_CLI=1`**: use the `mysql` CLI (fallback if no Python MySQL driver is installed)

### What the API reads

- **`workunit`**: provides job/chunk identity and create time
- **`result`**: provides state and retry/diagnostic signals
- **`host`**: provides worker fleet state and last seen time

### Status mapping (explainable)

Chunk status (queued/running/completed/failed) is derived from BOINC fields:
- `result.server_state` (UNSENT / IN_PROGRESS / OVER)
- `result.outcome` (SUCCESS vs error outcomes)
- `result.validate_state` (VALID / INVALID / INIT), when `workunit.need_validate=1`

Worker state (idle/active) is derived from whether the host has any results in `server_state=IN_PROGRESS`.

### Deferred by design (Phase 3)

This phase does **not** implement:
- Container execution hooks in the BOINC client
- SHA-256 receipt generation on workers
- Server-side requeue logic beyond existing BOINC resend/validation mechanisms

Those will be added behind feature flags later.


