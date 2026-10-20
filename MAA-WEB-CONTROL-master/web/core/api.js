// FastAPI 的错误体是 {"detail": ...}（HTTPException 为字符串，422 校验错误为数组）。
// 原样抛出去会让界面显示一整段 JSON，用户看不出到底哪个设置失败了。
function apiErrorMessage(status, raw, statusText) {
  let message = raw;
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed?.detail === "string") {
      message = parsed.detail;
    } else if (Array.isArray(parsed?.detail)) {
      message = parsed.detail.map((item) => (item && item.msg ? item.msg : JSON.stringify(item))).join("; ");
    }
  } catch {
    // 非 JSON 响应，保持原文
  }
  return `HTTP ${status}: ${message || statusText}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) throw new Error(apiErrorMessage(response.status, await response.text(), response.statusText));
  return response.json();
}

function statusMessage(status, unavailableText) {
  if (status?.message) return status.message;
  return status?.available ? "可用" : unavailableText;
}

let refreshStatusTimer = null;

function scheduleRefreshStatus() {
  if (refreshStatusTimer) return;
  refreshStatusTimer = setTimeout(() => {
    refreshStatusTimer = null;
    refreshStatus().catch(showError);
  }, 600);
}

async function refreshStatus() {
  const [status, adb] = await Promise.all([
    api("/api/status"),
    api("/api/adb/devices")
  ]);
  state.runnerState = status.state || "Idle";
  setText("runnerState", status.state);
  setText("sideRunnerState", status.state);
  setText("taskProgress", `${status.appended_tasks} / ${status.total_tasks}`);
  setText("sideTaskProgress", `${status.appended_tasks} / ${status.total_tasks}`);
  setText("adbStatus", statusMessage(adb, "未配置"));
  setText("toolsAdbStatus", statusMessage(adb, "未配置"));
  syncRunnerControls();
  if (typeof syncCopilotRunState === "function") syncCopilotRunState(state.runnerState);
}
