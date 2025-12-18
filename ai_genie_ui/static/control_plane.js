const byId = (id) => document.getElementById(id);

const fmtTs = (ts) => {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString();
};

const escapeHtml = (s) =>
  String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

const statusPill = (status) => {
  const cls = `status-pill status-${status}`;
  return `<span class="${cls}">${escapeHtml(status)}</span>`;
};

const progressBar = (completed, total) => {
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
  return `
    <div class="progress">
      <div class="progress-bar" style="width:${pct}%"></div>
    </div>
  `;
};

async function apiGet(path) {
  const res = await fetch(path);
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}

async function apiPost(path, payload) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}

function getQueryParam(name) {
  const url = new URL(window.location.href);
  return url.searchParams.get(name);
}

async function renderJobs() {
  const table = byId("jobs-table-body");
  const empty = byId("jobs-empty");
  if (!table) return;
  // Avoid replacing DOM while the user is interacting with the table (prevents "stale click" glitches).
  const wrap = table.closest(".table-wrap");
  if (wrap && wrap.matches(":hover")) return;

  const { jobs } = await apiGet("/api/jobs");
  if (!jobs || jobs.length === 0) {
    empty?.classList.remove("hidden");
    table.innerHTML = "";
    return;
  }
  empty?.classList.add("hidden");

  table.innerHTML = jobs
    .map((j) => {
      const completed = j.progress?.completed_chunks ?? 0;
      const total = j.progress?.total_chunks ?? 0;
      const detailsHref = `/job?job_id=${encodeURIComponent(j.job_id)}`;
      return `
        <tr>
          <td class="mono"><a href="${detailsHref}">${escapeHtml(j.job_id)}</a></td>
          <td>${statusPill(j.status)}</td>
          <td>${progressBar(completed, total)}</td>
          <td class="mono">${completed}/${total}</td>
          <td>${escapeHtml(fmtTs(j.created_at))}</td>
        </tr>
      `;
    })
    .join("");
}

async function handleJobSubmit() {
  const form = byId("job-submit-form");
  if (!form) return;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const statusEl = byId("job-submit-status");
    const payload = {
      job_id: byId("job_id")?.value?.trim() || undefined,
      input_uri: byId("input_uri")?.value?.trim(),
      output_uri: byId("output_uri")?.value?.trim(),
      container_image: byId("container_image")?.value?.trim(),
      worker_label: byId("worker_label")?.value?.trim() || undefined,
      num_chunks: Number(byId("num_chunks")?.value || 1),
      chunk_range: {
        start: Number(byId("range_start")?.value || 0),
        end: Number(byId("range_end")?.value || 0),
      },
    };
    statusEl.textContent = "Submitting…";
    statusEl.className = "status";
    try {
      const job = await apiPost("/api/jobs", payload);
      statusEl.textContent = `Created job ${job.job_id}`;
      statusEl.classList.add("ok");
      await renderJobs();
    } catch (err) {
      statusEl.textContent = `Failed: ${err.message}`;
      statusEl.classList.add("bad");
    }
  });
}

async function renderJobDetail() {
  const jobId = getQueryParam("job_id");
  const header = byId("job-id");
  const meta = byId("job-meta");
  const chunksBody = byId("chunks-table-body");
  const missing = byId("job-missing");
  if (!header || !meta || !chunksBody) return;

  if (!jobId) {
    missing?.classList.remove("hidden");
    return;
  }

  try {
    const job = await apiGet(`/api/jobs/${encodeURIComponent(jobId)}`);
    header.textContent = job.job_id;
    meta.innerHTML = `
      <div class="kv"><div>Input URI</div><div class="mono">${escapeHtml(job.input_uri || "—")}</div></div>
      <div class="kv"><div>Output URI</div><div class="mono">${escapeHtml(job.output_uri || "—")}</div></div>
      <div class="kv"><div>Container Image</div><div class="mono">${escapeHtml(job.container_image || "—")}</div></div>
      <div class="kv"><div>Job State</div><div>${statusPill(job.status || "queued")}</div></div>
    `;

    chunksBody.innerHTML = job.chunks
      .map((c) => {
        return `
          <tr>
            <td class="mono">${escapeHtml(c.chunk_id)}</td>
            <td>${statusPill(c.status)}</td>
            <td class="mono">${escapeHtml(c.retries ?? 0)}</td>
            <td>${escapeHtml(c.failure_reason || "—")}</td>
            <td>${statusPill(c.verification || "pending")}</td>
          </tr>
        `;
      })
      .join("");
  } catch (err) {
    missing?.classList.remove("hidden");
  }
}

async function renderWorkers() {
  const body = byId("workers-table-body");
  if (!body) return;
  const wrap = body.closest(".table-wrap");
  if (wrap && wrap.matches(":hover")) return;
  const { workers } = await apiGet("/api/workers");
  body.innerHTML = workers
    .map((w) => {
      return `
        <tr>
          <td class="mono">${escapeHtml(w.worker_id)}</td>
          <td>${escapeHtml(w.label || "—")}</td>
          <td>${statusPill(w.state)}</td>
          <td>${escapeHtml(fmtTs(w.last_seen))}</td>
        </tr>
      `;
    })
    .join("");
}

async function initControlPlane() {
  try {
    // In BOINC DB mode, this UI is intentionally read-only.
    await handleJobSubmit();
    await renderJobs();
    await renderJobDetail();
    await renderWorkers();
    setInterval(() => {
      renderJobs().catch(() => {});
      renderJobDetail().catch(() => {});
      renderWorkers().catch(() => {});
    }, 5000);
  } catch (err) {
    // silent; page will show stale content
  }
}

window.addEventListener("DOMContentLoaded", initControlPlane);


