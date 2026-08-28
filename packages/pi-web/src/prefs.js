// 界面偏好持久化。
//
// 只存"用户的选择"，不存会话内容——会话本来就在服务端的 jsonl 里。
// 恢复时要对着服务端返回的 meta 校验一遍：工具可能被删了、模型可能不再可用，
// 直接照搬旧值会让界面显示一个根本用不了的配置。

const KEY = "pi-web-prefs-v1";

export function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(KEY)) || {};
  } catch {
    return {};
  }
}

export function savePrefs(patch) {
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...loadPrefs(), ...patch }));
  } catch {
    /* 隐私模式下 localStorage 会抛异常，忽略即可 */
  }
}

// 用保存的偏好覆盖默认值，但只保留服务端仍然认识的项
export function applyPrefs(draft, meta, prefs) {
  const toolNames = new Set(meta.tools.map((t) => t.name));
  const skillNames = new Set(meta.skills.map((s) => s.name));
  const modelKeys = new Set(meta.models.filter((m) => m.available).map((m) => m.key));

  if (Array.isArray(prefs.tools)) {
    draft.tools = prefs.tools.filter((n) => toolNames.has(n));
  }
  if (Array.isArray(prefs.skills)) {
    draft.skills = prefs.skills.filter((n) => skillNames.has(n));
  }
  if (prefs.model && modelKeys.has(prefs.model)) {
    draft.model = prefs.model;
  }
  if (typeof prefs.thinking === "string") {
    draft.thinking = prefs.thinking;
  }
}
