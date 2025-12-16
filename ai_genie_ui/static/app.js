const serverToggle = document.getElementById("server-toggle");
const clientToggle = document.getElementById("client-toggle");
const runForm = document.getElementById("run-form");
const dockerInput = document.getElementById("docker-ref");
const pythonInput = document.getElementById("python-code");
const runResult = document.getElementById("run-result");
const serverLogBox = document.getElementById("server-log-box");
const clientLogBox = document.getElementById("client-log-box");
let pollTimer;

const setToggleState = (button, isActive) => {
  if (!button) return;
  button.classList.toggle("inactive", !isActive);
  button.textContent = isActive ? "Deactivate" : "Activate";
};

const fetchState = async () => {
  try {
    const res = await fetch("/api/state");
    const json = await res.json();
    const { state } = json;
    setToggleState(serverToggle, state.server_active);
    setToggleState(clientToggle, state.client_active);
    if (state.selected_mode) {
      const radio = document.querySelector(
        `input[name="mode"][value="${state.selected_mode}"]`
      );
      if (radio) {
        radio.checked = true;
        syncModeInputs(state.selected_mode);
      }
    }
    if (dockerInput) dockerInput.value = state.docker_reference || "";
    if (pythonInput) pythonInput.value = state.python_code || "";
    if (runResult) runResult.textContent = state.last_run_result || "";
  } catch (err) {
    console.error("Failed to fetch state", err);
  }
};

const toggleTarget = async (target) => {
  const button = target === "server" ? serverToggle : clientToggle;
  if (!button) return;
  const isActive = !button.classList.contains("inactive");
  const action = isActive ? "deactivate" : "activate";
  try {
    const res = await fetch("/api/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, action }),
    });
    if (!res.ok) {
      throw new Error("Toggle failed");
    }
    setToggleState(button, action === "activate");
  } catch (err) {
    alert(`Unable to toggle ${target}: ${err.message}`);
  }
};

const syncModeInputs = (mode) => {
  if (!dockerInput || !pythonInput) return;
  if (mode === "docker") {
    dockerInput.disabled = false;
    pythonInput.disabled = true;
  } else {
    dockerInput.disabled = true;
    pythonInput.disabled = false;
  }
};

const submitRun = async (event) => {
  event.preventDefault();
  if (!runForm || !runResult) return;
  const formData = new FormData(runForm);
  const mode = formData.get("mode");
  const payload = {
    mode,
    docker_reference: formData.get("docker_reference"),
    python_code: formData.get("python_code"),
  };
  runResult.textContent = "Running Hello World test...";
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const json = await res.json();
    if (!res.ok) {
      throw new Error(json.error || "Unknown error");
    }
    runResult.textContent = json.result;
  } catch (err) {
    runResult.textContent = `Failed: ${err.message}`;
  }
};

const fetchLogsForTarget = async (target) => {
  const res = await fetch(`/api/logs?target=${target}`);
  if (!res.ok) {
    throw new Error("Failed to load logs");
  }
  const json = await res.json();
  return json.entries.join("\n") || "Awaiting events...";
};

const refreshLogs = async () => {
  try {
    const tasks = [];
    if (serverLogBox) tasks.push(fetchLogsForTarget("server"));
    if (clientLogBox) tasks.push(fetchLogsForTarget("client"));
    if (tasks.length === 0) return;

    const results = await Promise.all(tasks);
    let idx = 0;
    if (serverLogBox) serverLogBox.textContent = results[idx++];
    if (clientLogBox) clientLogBox.textContent = results[idx++];
  } catch (err) {
    if (serverLogBox) serverLogBox.textContent = "Unable to load logs.";
    if (clientLogBox) clientLogBox.textContent = "Unable to load logs.";
  }
};

const init = () => {
  fetchState();
  refreshLogs();
  pollTimer = setInterval(refreshLogs, 2000);

  const initialMode = document.querySelector('input[name="mode"]:checked')?.value;
  if (initialMode) {
    syncModeInputs(initialMode);
  }

  if (serverToggle) serverToggle.addEventListener("click", () => toggleTarget("server"));
  if (clientToggle) clientToggle.addEventListener("click", () => toggleTarget("client"));
  if (runForm) runForm.addEventListener("submit", submitRun);

  document.querySelectorAll('input[name="mode"]').forEach((radio) => {
    radio.addEventListener("change", (event) =>
      syncModeInputs(event.target.value)
    );
  });

};

window.addEventListener("DOMContentLoaded", init);

