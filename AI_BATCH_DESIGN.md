## BOINC → Stateless, Containerized Batch AI Inference (Prototype Design)

This document describes an **additive** approach for extending BOINC into a batch AI execution backend with:

- Chunked, stateless execution
- Container-based workers
- Output hashing + server-side verification
- Retries/fallback via existing BOINC resend/validation mechanisms
- A demo-grade UI driven by a minimal REST API

This is not production guidance (no hardening, no multi-tenant isolation guarantees).

---

## 1) Where to Modify BOINC (File/Component Map)

### Server-side (scheduler / workunit creation / validation)
- **Workunit creation / templates**
  - `tools/create_work.cpp`: CLI job submission; supports `--batch`.
  - `sched/sample_work_generator.cpp`: example program using `create_work()` to register WUs.
  - `sched/make_work.cpp`: clones WUs (useful to understand how `wu.xml_doc` is manipulated).
  - `tools/backend_lib.cpp`, `tools/backend_lib.h`: `create_work()` implementation and template processing.
- **Workunit metadata carrier**
  - `WORKUNIT::xml_doc` is a BLOB and is already used for structured tags.
  - Example of project-specific tags: `sched/buda.cpp` (`<buda_app_name>` in `wu.xml_doc`).
- **Scheduling / resend / retry behavior**
  - `sched/transitioner.cpp`: creates results for WUs and manages reissue behavior.
  - `sched/sched_resend.cpp`: resend logic for timed-out/failed results.
  - `sched/sched_send.cpp`: selects WUs to send to clients (hook for filtering by worker label if desired later).
- **Validation / output checking**
  - `sched/validator.cpp`: top-level validator loop and state transitions.
  - `sched/validate_util.cpp`, `sched/validate_util2.cpp`: helpers to get output file paths/infos.
  - `sched/script_validator.cpp`: runs project-provided scripts; ideal for prototype hash verification without touching core validator logic.
  - `sched/sample_bitwise_validator.cpp`: shows checksum-based comparison pattern (currently MD5).
- **DB structs**
  - `db/boinc_db_types.h`: defines `WORKUNIT`, `RESULT`, `BATCH` etc.
  - `db/boinc_db.h`: DB wrapper classes like `DB_WORKUNIT`, `DB_RESULT`, `DB_BATCH`.

### Client-side (task execution)
- **App/task launch**
  - `client/app_start.cpp`: `ACTIVE_TASK::start()` is the canonical spawn point for task execution.
- **Finish/upload path (where to compute hashes / generate receipts)**
  - `client/cs_apps.cpp`: `CLIENT_STATE::app_finished()` scans output files and computes MD5 today; good place to add SHA-256 bookkeeping or to generate a receipt file for upload.
- **Container runner precedent**
  - `client/Run_Podman.cpp`: existing helper for running Podman with BOINC’s sandbox/user model (useful reference for “container runtime glue” on macOS).

---

## 2) Job Model Extension (Structured Metadata)

**Do not hardcode “AI job” logic in many places.** Put job fields in a single structured blob, then parse where needed.

Recommended carrier: **`WORKUNIT::xml_doc`** under a single tag:

```xml
<workunit>
  ...
  <ai_batch>
    <job_id>job-123</job_id>
    <input_uri>s3://bucket/input.jsonl</input_uri>
    <output_uri>s3://bucket/output/</output_uri>
    <container_image>ghcr.io/org/model:tag</container_image>
    <chunk_id>7</chunk_id>
    <chunk_range start="7000" end="7999"/>
    <worker_label>edge</worker_label> <!-- optional: edge|on_prem|cloud -->
    <expected_output_sha256>...</expected_output_sha256> <!-- optional -->
  </ai_batch>
</workunit>
```

Notes:
- This keeps changes additive: DB schema need not change.
- You can still use BOINC’s **`batch`** field to group workunits for a logical job, while `job_id` remains a stable external identifier.

---

## 3) Stateless Container Execution (Client)

### Core idea
For each workunit:
- Create an ephemeral working directory (slot dir is already per-task)
- Start a container with:
  - read-only image
  - explicit input pull step (from `input_uri`)
  - explicit output push step (to `output_uri`)
  - no persistent host state outside the slot

### Minimal pseudocode (mechanism sketch)

Hook point: `client/app_start.cpp` inside `ACTIVE_TASK::start()`.

```cpp
// PSEUDOCODE: not drop-in C++

AI_META meta = parse_ai_batch_from_wu_xml(wup->xml_doc);

string slot = slot_dir; // already unique per task instance
string in_dir = slot + "/in";
string out_dir = slot + "/out";
mkdir(in_dir); mkdir(out_dir);

// Pass only minimal, explicit environment into the container.
map<string,string> env = {
  {"AI_JOB_ID", meta.job_id},
  {"AI_CHUNK_ID", to_string(meta.chunk_id)},
  {"AI_RANGE_START", to_string(meta.range_start)},
  {"AI_RANGE_END", to_string(meta.range_end)},
  {"AI_INPUT_URI", meta.input_uri},
  {"AI_OUTPUT_URI", meta.output_uri},
};

// Runtime contract:
// Container entrypoint must:
//  1) pull inputs from AI_INPUT_URI
//  2) run inference for the chunk range
//  3) write output artifact(s) into /out
//  4) compute sha256 of output artifact(s)
//  5) push artifact(s) to AI_OUTPUT_URI
//  6) write a small receipt file into /out/receipt.json with hashes and remote object keys

vector<string> cmd = {
  "docker", "run", "--rm",
  "--network=host",              // or none, depending on your pull/push model
  "--cpus=1", "--memory=4g",
  "-v", in_dir + ":/in:rw",
  "-v", out_dir + ":/out:rw",
  "--env", "AI_JOB_ID=" + meta.job_id,
  "--env", "AI_CHUNK_ID=" + ...,
  "--env", "AI_INPUT_URI=" + meta.input_uri,
  "--env", "AI_OUTPUT_URI=" + meta.output_uri,
  meta.container_image
};

int rc = exec_and_wait(cmd, /*timeout*/ meta.deadline);
if (rc != 0) mark_task_failed();
else mark_task_completed();

// BOINC still needs "something" to upload/verify:
// Upload /out/receipt.json as the BOINC result output file.
```

Why a receipt file?
- You satisfy “outputs pushed immediately” (to `output_uri`) without forcing BOINC’s upload server to carry the large artifact.
- BOINC still gets a deterministic, small result artifact for bookkeeping + validation.

---

## 4) Chunk-Based Scheduling (Server)

Model:
- **1 logical job** → **N workunits** (one per chunk)
- Each workunit carries:
  - `job_id`
  - `chunk_id`, `chunk_range`
  - `container_image`, `input_uri`, `output_uri`

Implementation guidance:
- On submission, create a `BATCH` record (`db/boinc_db_types.h` has `struct BATCH`) and set `workunit.batch = batch.id`.
- Use `tools/create_work.cpp` + `--batch` in the prototype submission path, or implement a small server-side generator based on `sched/sample_work_generator.cpp`.

Retries:
- Use BOINC’s existing result resend/validation loop:
  - If a chunk result errors or fails validation, BOINC will create additional results for the same WU until limits are hit (`max_total_results`, `max_error_results`).
- If you want “reassignment on failure” at chunk granularity, **keep each chunk as its own WU**.

---

## 5) Output Verification (SHA-256)

Prototype-friendly approach:
- Worker container writes `receipt.json` like:

```json
{
  "job_id": "job-123",
  "chunk_id": 7,
  "artifact": {
    "uri": "s3://bucket/output/job-123/chunk-7.jsonl",
    "sha256": "…"
  }
}
```

Server-side verification choices:

### Option A (fast prototype): script validator
Use `sched/script_validator.cpp`:
- init script validates receipt format
- compare script verifies:
  - download artifact from `artifact.uri` (or HEAD metadata)
  - compute sha256
  - compare to receipt `sha256`
- return nonzero to force INVALID/TRANSIENT outcomes and trigger resend.

### Option B (C++ validator): checksum-style
Follow the pattern in `sched/sample_bitwise_validator.cpp`:
- In `init_result()`, parse receipt, compute sha256 of the remote artifact (or local if BOINC stored it), store in per-result data.
- In `compare_results()`, mark match if hashes match.

Requeue on mismatch:
- Treat mismatch as “invalid”; BOINC’s transitioner/resend will reissue until success.

---

## 6) Worker Classification (edge/on_prem/cloud)

Minimal/additive approach:
- Have the client send a **worker label** in scheduler request as a custom tag.
- Store it server-side as project-specific metadata keyed by `hostid`.

Implementation sketch (additive):
- Client:
  - Extend `client/hostinfo_*.cpp` or scheduler request builder to include `<worker_label>edge</worker_label>`.
- Server:
  - Parse the tag in the scheduler request (see `sched/handle_request.cpp` → request parse flow).
  - Persist label in:
    - a new small table `worker_labels(hostid, label, updated_at)` (cleanest), OR
    - `msg_from_host`/`msg_to_host` project-specific messages (prototype), OR
    - `host.description` (not recommended; mixes concerns).

No optimization logic required; label is for observability + UI filtering.

---

## 7) Minimal REST API (UI / Control Plane)

The repo includes a prototype REST API in `ai_genie_ui/server.py`:
- `POST /api/jobs`
- `GET /api/jobs`
- `GET /api/jobs/{job_id}`
- `GET /api/workers`

The intended architecture is:
- REST API reads from BOINC internals (DB + logs + scheduler state) **without rewriting** BOINC.
- Prototype backend can be swapped from “in-memory” to “BOINC-backed”.

---

## 8) UI Component Structure

Demo screens:
- Jobs dashboard (`/jobs`)
  - list jobs, status, progress, container image, URIs
- Job detail (`/job?job_id=...`)
  - metadata + chunk table (status, retries, failure reasons, verification)
- Workers (`/workers`)
  - list workers, labels, active/idle

---

## 9) How This Differs From Traditional BOINC Workloads

Traditional BOINC:
- Assumes apps are distributed as BOINC app versions; binaries are managed by BOINC.
- Typically treats input/output as BOINC-managed files (download/upload via project servers).
- Validation often compares outputs across replicas (quorum) rather than verifying remote object-store receipts.

This batch AI design:
- Treats each unit as a **stateless container invocation**.
- Treats inputs/outputs as **URI-addressed objects**, not pre-staged BOINC files.
- Uses a **receipt + hash** path to make verification explicit and explainable in a demo.

---

## 10) Why Stateless + Chunked Fits Batch AI (and Tradeoffs)

Why it fits:
- Chunking matches how large inference datasets are processed (ranges/shards).
- Stateless execution enables easy retries, rescheduling, and heterogeneous workers.
- Container images standardize runtime dependencies without BOINC-specific app packaging.

Tradeoffs / technical debt:
- You are partly bypassing BOINC’s native file distribution model (inputs/outputs become “external”).
- Container execution reduces BOINC’s visibility into the true workload unless you explicitly surface metrics.
- Verification becomes “receipt-driven”; you must ensure receipt integrity and artifact availability.
- Worker labels add another classification dimension that can diverge from BOINC’s native “plan class / platform” model.


