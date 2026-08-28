<script setup>
import { computed, onMounted, nextTick, reactive, ref, watch } from "vue";
import { api, subscribe } from "./api";
import { applyPrefs, loadPrefs, savePrefs } from "./prefs";
import SideBar from "./components/SideBar.vue";
import TurnBlock from "./components/TurnBlock.vue";
import Composer from "./components/Composer.vue";
import FileViewer from "./components/FileViewer.vue";

const meta = reactive({ cwd: "", models: [], tools: [], skills: [], diagnostics: [] });
const draft = reactive({ model: "", thinking: "off", tools: [], skills: [] });

const history = ref([]);
const workspaces = ref([]);
const files = ref(null);
const openFile = ref(null);
const session = ref(null);
const turns = ref([]);
const busy = ref(false);
const error = ref("");
const chat = ref(null);
let unsubscribe = null;

const totalCost = computed(() =>
  turns.value.reduce((sum, t) => sum + (t.usage.cost || 0), 0)
);
const contextUsed = computed(() => {
  // 优先取最后一轮上报的输入量——它就是"下一次请求要带多少上下文"
  for (let i = turns.value.length - 1; i >= 0; i--) {
    const u = turns.value[i].usage;
    if (u && u.input) return u.input + (u.output || 0);
  }
  return 0;
});
const totalTokens = computed(() =>
  turns.value.reduce((sum, t) => sum + (t.usage.input || 0) + (t.usage.output || 0), 0)
);
const contextMax = computed(() => {
  const model = meta.models.find((m) => m.key === (session.value?.model || draft.model));
  return model?.contextWindow || 0;
});
const contextPct = computed(() =>
  contextMax.value ? Math.min(100, (contextUsed.value / contextMax.value) * 100) : 0
);

// 用户的每一次勾选都记下来，下次打开网页恢复原样
watch(
  () => [draft.model, draft.thinking, draft.tools.join(","), draft.skills.join(",")],
  () => savePrefs({
    model: draft.model, thinking: draft.thinking,
    tools: [...draft.tools], skills: [...draft.skills],
  })
);

// 侧栏改了模型或思考档位，如果会话已经在跑，要立刻同步到后端。
// 之前只有新建会话时才带上这两个值，改了等于没改。
watch(() => draft.thinking, async (level) => {
  if (!session.value) return;
  try {
    session.value = { ...session.value, ...(await api.setThinking(session.value.id, level)) };
  } catch (e) {
    error.value = e.message;
  }
});

// 工具和技能的勾选也要能中途改——只在新建会话时用一次的话，
// 用户勾了新工具下一轮还是没有，非常反直觉
watch(
  () => [draft.tools.slice().sort().join(","), draft.skills.slice().sort().join(",")],
  async () => {
    if (!session.value) return;
    try {
      await api.setTools(session.value.id, draft.tools, draft.skills);
    } catch (e) {
      error.value = e.message;
    }
  }
);

watch(() => draft.model, async (spec) => {
  if (!session.value || !spec) return;
  try {
    session.value = { ...session.value, ...(await api.setModel(session.value.id, spec)) };
  } catch (e) {
    error.value = e.message;
  }
});

function newTurn(user) {
  return {
    user, thinking: "", text: "", tools: [], injected: [], usage: {}, elapsed: 0,
    phase: "waiting", folded: false, thinkOpen: false, toolsOpen: false, error: "",
  };
}
const current = () => turns.value[turns.value.length - 1];

function scrollDown() {
  nextTick(() => {
    const el = chat.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
}

// ── SSE 帧处理：契约定义在后端 dto.py，前端只认这一份 ──────────
function handle(frame) {
  const turn = current();
  switch (frame.type) {
    case "thinking_delta":
      if (!turn) return;
      turn.thinking += frame.text;
      turn.phase = "thinking";
      break;
    case "text_start":
      // 正文开始 = 过程结束，折叠起来让注意力回到结论
      if (turn) { turn.folded = true; turn.phase = "writing"; }
      break;
    case "text_delta":
      if (!turn) return;
      turn.text += frame.text;
      turn.phase = "writing";
      break;
    case "tool_start":
      if (!turn) return;
      turn.tools.push({ ...frame, state: "run", preview: "" });
      if (turn.phase === "waiting" || turn.phase === "thinking") turn.phase = "tools";
      break;
    case "tool_update": {
      const t = turn?.tools.find((x) => x.id === frame.id);
      if (t) t.preview = frame.preview;
      break;
    }
    case "tool_end": {
      const t = turn?.tools.find((x) => x.id === frame.id);
      if (t) {
        t.state = frame.ok ? "ok" : "bad";
        t.preview = frame.preview || "";
        t.patch = frame.patch;
      }
      break;
    }
    case "injected":
      if (turn) turn.injected.push(frame.text);
      break;
    case "message_end":
      if (!turn) return;
      turn.usage = frame.usage || {};
      turn.elapsed = frame.elapsed || 0;
      if (frame.error) turn.error = frame.error;
      break;
    case "done":
      if (turn) {
        turn.phase = "done";
        turn.folded = true;
        turn.elapsed = frame.elapsed || turn.elapsed;
      }
      busy.value = false;
      refreshHistory();
      break;
    case "aborted":
      if (turn) { turn.phase = "done"; turn.error = "已中断"; }
      busy.value = false;
      break;
    case "error":
      if (turn) { turn.phase = "done"; turn.error = frame.message; }
      busy.value = false;
      break;
  }
  scrollDown();
}

// ── 动作 ────────────────────────────────────────────────────
async function ensureSession() {
  if (session.value) return session.value;
  const created = await api.createSession({
    model: draft.model || null,
    thinking: draft.thinking,
    tools: draft.tools.length ? draft.tools : null,
    skills: draft.skills,
    cwd: meta.cwd,
  });
  session.value = created;
  unsubscribe = subscribe(created.id, handle);
  return created;
}

async function send(text) {
  if (busy.value) return;
  error.value = "";
  try {
    const s = await ensureSession();
    turns.value.push(newTurn(text));
    busy.value = true;
    scrollDown();
    await api.prompt(s.id, text);
  } catch (e) {
    error.value = e.message;
    busy.value = false;
  }
}

async function abort() {
  if (session.value) await api.abort(session.value.id).catch(() => {});
}

function newSession() {
  unsubscribe?.();
  unsubscribe = null;
  session.value = null;
  turns.value = [];
  error.value = "";
  busy.value = false;
  openFile.value = null;
}

async function resume(item) {
  try {
    newSession();
    const detail = await api.historyDetail(item.file);
    turns.value = detail.turns.map((t) => ({ ...newTurn(t.user), ...t }));
    const created = await api.createSession({
      model: draft.model || null,
      thinking: draft.thinking,
      tools: draft.tools.length ? draft.tools : null,
      skills: draft.skills,
      resume: item.file,
    });
    session.value = { ...created, file: item.file, title: detail.title };
    unsubscribe = subscribe(created.id, handle);
    scrollDown();
  } catch (e) {
    error.value = e.message;
  }
}

async function browse(path) {
  try {
    files.value = await api.files(path ?? ".", session.value?.id);
  } catch (e) {
    error.value = e.message;
  }
}

async function showFile(path) {
  try {
    openFile.value = await api.fileContent(path, session.value?.id);
  } catch (e) {
    error.value = e.message;
  }
}

async function compact() {
  if (!session.value) return;
  try {
    const r = await api.compact(session.value.id);
    if (r.ok) turns.value.push({ ...newTurn(""), phase: "done", text: "（上下文已压缩）" });
  } catch (e) {
    error.value = e.message;
  }
}

async function refreshHistory() {
  try {
    history.value = (await api.history()).sessions;
  } catch { /* 忽略 */ }
}

// 折叠是可来回切的开关，不是单向展开
function toggle(turn, what) {
  const key = what === "think" ? "thinkOpen" : "toolsOpen";
  const opening = turn.folded || !turn[key];
  turn[key] = opening;
  // folded 只是"两块都收起来"的快捷状态，任一块打开就取消它
  turn.folded = !(turn.thinkOpen || turn.toolsOpen);
}

async function switchWorkspace(cwd) {
  try {
    await api.setWorkspace(cwd);
    newSession();
    Object.assign(meta, await api.meta(cwd));
    draft.tools = meta.tools.filter((t) => t.default).map((t) => t.name);
    draft.skills = meta.skills.map((s) => s.name);
    // 新目录的技能和扩展工具都不一样，用偏好覆盖时要重新过滤
    applyPrefs(draft, meta, loadPrefs());
    savePrefs({ cwd });
    await Promise.all([refreshHistory(), browse("."), refreshWorkspaces()]);
  } catch (e) {
    error.value = e.message;
  }
}

async function hideHistory(item) {
  try {
    await api.hideHistory(item.file);
    history.value = history.value.filter((h) => h.file !== item.file);
    if (session.value?.file === item.file) newSession();
  } catch (e) {
    error.value = e.message;
  }
}

async function refreshWorkspaces() {
  try {
    workspaces.value = (await api.workspaces()).workspaces;
  } catch { /* 忽略 */ }
}

onMounted(async () => {
  const prefs = loadPrefs();
  try {
    // 上次用的工作目录优先，服务端确认不存在时会回退
    if (prefs.cwd) await api.setWorkspace(prefs.cwd).catch(() => {});
    Object.assign(meta, await api.meta());
    draft.tools = meta.tools.filter((t) => t.default).map((t) => t.name);
    draft.skills = meta.skills.map((s) => s.name);
    const first = meta.models.find((m) => m.available);
    if (first) draft.model = first.key;
    applyPrefs(draft, meta, prefs);          // 偏好覆盖默认值
    if (meta.diagnostics?.length) error.value = meta.diagnostics[0];
  } catch (e) {
    error.value = "无法连接后端：" + e.message;
  }
  refreshHistory();
  refreshWorkspaces();
  browse(".");
});
</script>

<template>
  <div class="app" :class="{ 'with-viewer': openFile }">
    <SideBar
      :meta="meta" :draft="draft" :history="history" :files="files" :session="session"
      :workspaces="workspaces"
      @new="newSession" @resume="resume" @browse="browse" @open-file="showFile"
      @switch-workspace="switchWorkspace" @hide-history="hideHistory"
    />

    <div class="main">
      <header class="topbar">
        <span class="title">{{ session?.title || "新会话" }}</span>
        <span v-if="session" class="chip">{{ session.model }}</span>
        <span v-if="session && session.thinking !== 'off'" class="chip">
          think {{ session.thinking }}
        </span>
        <span v-if="contextMax" class="meter" :title="`${contextUsed} / ${contextMax}`">
          <span class="bar"><i :style="{ width: contextPct + '%' }" /></span>
          <span class="chip">{{ contextPct.toFixed(0) }}%</span>
        </span>
        <span v-if="totalTokens" class="chip">{{ totalTokens }} tok</span>
        <span v-if="totalCost" class="chip">¥{{ totalCost.toFixed(4) }}</span>
        <button v-if="session" class="ghost" @click="compact">压缩</button>
        <span v-if="error" class="chip warn" :title="error">{{ error }}</span>
      </header>

      <div class="chat" ref="chat">
        <div class="inner">
          <div v-if="!turns.length" class="welcome">
            <h2>开始一个新会话</h2>
            <p>左侧勾选技能和工具，然后在下面输入你的任务</p>
          </div>
          <TurnBlock
            v-for="(turn, i) in turns" :key="i" :turn="turn"
            @toggle="(what) => toggle(turn, what)"
          />
        </div>
      </div>

      <Composer :busy="busy" :has-session="!!session" @send="send" @abort="abort" />
    </div>

    <FileViewer
      v-if="openFile" :file="openFile" :session-id="session?.id || ''"
      @close="openFile = null"
    />
  </div>
</template>

<style scoped>
/* height + overflow:hidden 双管齐下：grid 的行高默认由内容撑开，
   不锁死的话对话一长整个页面就跟着滚，侧栏会被推走 */
.app {
  display: grid; grid-template-columns: 272px 1fr;
  height: 100vh; max-height: 100vh; overflow: hidden;
}
.app.with-viewer { grid-template-columns: 272px 1fr minmax(320px, 40%); }
/* min-height:0 是关键：flex 子元素默认 min-height:auto，
   不置零的话 .chat 的 overflow 不会生效 */
.main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }

.topbar {
  height: 52px; flex-shrink: 0; border-bottom: 1px solid var(--line);
  display: flex; align-items: center; gap: 12px; padding: 0 24px;
  background: color-mix(in srgb, var(--bg) 85%, transparent);
  backdrop-filter: blur(8px); position: sticky; top: 0; z-index: 5;
}
.topbar .title {
  font-size: 14px; font-weight: 500; flex: 1; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.topbar .warn { color: var(--err); max-width: 240px; overflow: hidden; text-overflow: ellipsis; }
.ghost {
  border: 1px solid var(--line); background: transparent; color: var(--muted);
  font-size: 12px; padding: 4px 10px; border-radius: var(--radius-sm);
}
.ghost:hover { color: var(--accent); border-color: var(--accent); }

.meter { display: flex; align-items: center; gap: 6px; }
.meter .bar {
  width: 60px; height: 4px; border-radius: 2px; background: var(--line);
  overflow: hidden; display: block;
}
.meter .bar i { display: block; height: 100%; background: var(--accent); transition: width 0.3s; }

.chat { flex: 1; min-height: 0; overflow-y: auto; scroll-behavior: smooth; }
.chat .inner { max-width: 820px; margin: 0 auto; padding: 28px 24px 40px; }
.welcome { text-align: center; padding: 90px 20px; color: var(--muted); }
.welcome h2 { font-size: 19px; font-weight: 500; color: var(--text-2); margin: 0 0 8px; }
.welcome p { font-size: 13.5px; margin: 0; }

@media (max-width: 900px) {
  .app, .app.with-viewer { grid-template-columns: 1fr; }
}
</style>
