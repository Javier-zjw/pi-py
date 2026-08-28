// 与 FastAPI 的全部交互都收在这里，组件不直接碰 fetch。

async function request(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

export const api = {
  meta: (cwd) => request("GET", `/api/meta${cwd ? `?cwd=${encodeURIComponent(cwd)}` : ""}`),

  workspaces: () => request("GET", "/api/workspaces"),
  browseDirs: (path = "~") =>
    request("GET", `/api/workspaces/browse?path=${encodeURIComponent(path)}`),
  setWorkspace: (cwd) => request("POST", "/api/workspaces", { cwd }),

  createSession: (payload) => request("POST", "/api/sessions", payload),
  getSession: (id) => request("GET", `/api/sessions/${id}`),
  closeSession: (id) => request("DELETE", `/api/sessions/${id}`),
  prompt: (id, text) => request("POST", `/api/sessions/${id}/prompt`, { text }),
  abort: (id) => request("POST", `/api/sessions/${id}/abort`),
  compact: (id) => request("POST", `/api/sessions/${id}/compact`),
  setModel: (id, spec) => request("POST", `/api/sessions/${id}/model`, { spec }),
  setThinking: (id, level) => request("POST", `/api/sessions/${id}/thinking`, { level }),
  setTools: (id, tools, skills) =>
    request("POST", `/api/sessions/${id}/tools`, { tools, skills }),

  history: (limit = 30, cwd) =>
    request("GET", `/api/history?limit=${limit}${cwd ? `&cwd=${encodeURIComponent(cwd)}` : ""}`),
  hideHistory: (file) =>
    request("DELETE", `/api/history?file=${encodeURIComponent(file)}`),
  restoreHistory: () => request("POST", "/api/history/restore"),
  historyDetail: (file) =>
    request("GET", `/api/history/detail?file=${encodeURIComponent(file)}`),

  files: (path = ".", session) =>
    request("GET", `/api/files?path=${encodeURIComponent(path)}${session ? `&session=${session}` : ""}`),
  fileContent: (path, session) =>
    request("GET", `/api/files/content?path=${encodeURIComponent(path)}${session ? `&session=${session}` : ""}`),
};

// SSE：浏览器自带重连，不用自己写心跳
export function subscribe(sessionId, onFrame) {
  const source = new EventSource(`/api/sessions/${sessionId}/events?replay=false`);
  source.onmessage = (e) => {
    try {
      onFrame(JSON.parse(e.data));
    } catch {
      /* 忽略解析失败的帧 */
    }
  };
  return () => source.close();
}
