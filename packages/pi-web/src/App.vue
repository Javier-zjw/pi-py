<script setup>
import { computed, onMounted, nextTick, reactive, ref, watch } from "vue";
import { api, subscribe } from "./api";
import { applyPrefs, loadPrefs, savePrefs } from "./prefs";
import SideBar from "./components/SideBar.vue";
import TurnBlock from "./components/TurnBlock.vue";
import Composer from "./components/Composer.vue";
import FileViewer from "./components/FileViewer.vue";
import CompactionCard from "./components/CompactionCard.vue";

const meta = reactive({
  cwd: "", models: [], tools: [], skills: [], diagnostics: [],
  extensions: [], modes: [], commands: [],
});
const draft = reactive({ model: "", thinking: "off", tools: [], skills: [] });

const history = ref([]);
const workspaces = ref([]);
const mode = ref("chat");
const plan = ref(null);
const notices = ref([]);
// 压缩之后，之前那些轮次的用量不再代表当前上下文占用
const contextBase = ref(0);
// 压缩刚结束、还没有新的模型回复时，用后端算好的压缩后大小
const contextAfterCompaction = ref(0);
// 正在进行的压缩：{ reason, tokensBefore, phase, chunk, chunks, preview }
const compaction = ref(null);
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
  // 只看压缩检查点之后的轮次——压缩过的部分已经不在上下文里了，
  // 继续用旧数字会让占用一直虚高
  for (let i = turns.value.length - 1; i >= contextBase.value; i--) {
    const u = turns.value[i].usage;
    if (u && u.input) return u.input + (u.cacheRead || 0) + (u.output || 0);
  }
  return contextAfterCompaction.value;
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
        // 计划提交后在输入框上方显示执行入口
        if (frame.name === "submit_plan" && frame.ok && frame.plan) plan.value = frame.plan;
      }
      break;
    }
    case "compaction_start":
      compaction.value = {
        reason: frame.reason, tokensBefore: frame.tokensBefore,
        phase: "summarizing", chunk: 1, chunks: 1, preview: "",
      };
      break;
    case "compaction_progress":
      if (!compaction.value) return;
      Object.assign(compaction.value, { phase: frame.phase, chunk: frame.chunk, chunks: frame.chunks });
      // 只留最后一段，流式预览不需要整份 JSON
      compaction.value.preview = (compaction.value.preview + frame.text).slice(-600);
      return;                       // 进度帧很密，不必每次都滚动
    case "compaction_end":
      compaction.value = null;
      if (frame.ok) {
        // 压缩卡片插在这一轮的用户消息之前：压缩先于这一轮发生
        const card = { ...newTurn(""), kind: "compaction", compaction: frame, phase: "done" };
        const pending = turn && turn.phase === "waiting" && !turn.text && !turn.tools.length;
        if (pending) turns.value.splice(turns.value.length - 1, 0, card);
        else turns.value.push(card);
        contextBase.value = turns.value.length - (pending ? 1 : 0);
        contextAfterCompaction.value = frame.tokensAfter || 0;
      } else if (frame.error) {
        notices.value.push(`压缩未执行：${frame.error}`);
        error.value = `压缩未执行：${frame.error}`;
      } else if (frame.cancelled) {
        error.value = "已取消压缩，上下文保持原样";
      }
      break;
    case "mode":
      mode.value = frame.mode;
      break;
    case "notice":
      notices.value.push(frame.text);
      turns.value.push({ ...newTurn(""), phase: "done", folded: true, text: frame.text });
      break;
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
  if (mode.value !== "chat") await api.setMode(created.id, mode.value).catch(() => {});
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

async function switchMode(id) {
  mode.value = id;
  if (!session.value) return;      // 还没会话，等新建时带上
  try {
    const snapshot = await api.setMode(session.value.id, id);
    session.value = { ...session.value, ...snapshot };
  } catch (e) {
    error.value = e.message;
  }
}

async function runCommand(name, rest) {
  try {
    const s = await ensureSession();
    const r = await api.runCommand(s.id, name, rest);
    if (r.mode) mode.value = r.mode;
  } catch (e) {
    // 不是已知命令就当普通提问发出去，别让用户白打一遍
    if (String(e.message).includes("未知命令")) {
      await send(`/${name}${rest ? " " + rest : ""}`);
    } else {
      error.value = e.message;
    }
  }
}

async function buildPlan() {
  await runCommand("build", "");
  plan.value = null;
}

async function toggleExtension(key, enabled) {
  try {
    const r = await api.toggleExtension(key, enabled);
    meta.extensions = r.extensions;
    Object.assign(meta, await api.meta(meta.cwd));   // 工具/命令/模式都可能变了
  } catch (e) {
    error.value = e.message;
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
  plan.value = null;
  contextBase.value = 0;
  contextAfterCompaction.value = 0;
  compaction.value = null;
}

async function resume(item) {
  try {
    newSession();
    const detail = await api.historyDetail(item.file);
    turns.value = detail.turns.map((t) => ({ ...newTurn(t.user), ...t }));
    // 历史里最后一次压缩之前的轮次不再占用上下文
    const last = turns.value.map((t) => t.kind).lastIndexOf("compaction");
    if (last >= 0) {
      contextBase.value = last + 1;
      contextAfterCompaction.value = turns.value[last].compaction?.tokensAfter || 0;
    }
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
  if (!session.value || compaction.value) return;
  error.value = "";
  try {
    // 过程和结果都走 SSE 的 compaction_* 帧，这里不用再自己插卡片
    await api.compact(session.value.id);
  } catch (e) {
    error.value = e.message;
  }
}

const compactionLabel = computed(() => {
  const c = compaction.value;
  if (!c) return "";
  const step = c.phase === "retrying" ? "校验未通过，重试中" : "生成摘要";
  return c.chunks > 1 ? `${step}（第 ${c.chunk}/${c.chunks} 块）` : step;
});

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
      @toggle-extension="toggleExtension"
    />

    <div class="main">
      <header class="topbar">
        <span class="title">{{ session?.title || "新会话" }}</span>
        <span v-if="session" class="chip">{{ session.model }}</span>
        <span v-if="session && session.thinking !== 'off'" class="chip">
          think {{ session.thinking }}
        </span>
        <span v-if="mode !== 'chat'" class="chip accent">{{
          meta.modes.find((m) => m.id === mode)?.label || mode
        }}</span>
        <span v-if="contextMax" class="meter" :title="`${contextUsed} / ${contextMax}`">
          <span class="bar"><i :style="{ width: contextPct + '%' }" /></span>
          <span class="chip">{{ contextPct.toFixed(0) }}%</span>
        </span>
        <span v-if="totalTokens" class="chip">{{ totalTokens }} tok</span>
        <span v-if="totalCost" class="chip">¥{{ totalCost.toFixed(4) }}</span>
        <button v-if="session" class="ghost" :disabled="!!compaction || busy" @click="compact">
          压缩
        </button>
        <span v-if="error" class="chip warn" :title="error">{{ error }}</span>
      </header>

      <div class="chat" ref="chat">
        <div class="inner">
          <div v-if="!turns.length" class="welcome">
            <h2>开始一个新会话</h2>
            <p>左侧勾选技能和工具，然后在下面输入你的任务</p>
          </div>
          <template v-for="(turn, i) in turns" :key="i">
            <CompactionCard v-if="turn.kind === 'compaction'" :data="turn.compaction" />
            <TurnBlock v-else :turn="turn" @toggle="(what) => toggle(turn, what)" />
          </template>
        </div>
      </div>

      <!-- 压缩是同步的：期间不执行任务，必须让用户看得到、能取消 -->
      <div v-if="compaction" class="compacting">
        <div class="line">
          <span class="spin">⟳</span>
          <span>正在压缩上下文 · {{ compactionLabel }}</span>
          <span class="chip">压缩前 {{ compaction.tokensBefore }} tokens</span>
          <button class="ghost" @click="abort">取消</button>
        </div>
        <pre v-if="compaction.preview" class="preview">{{ compaction.preview }}</pre>
      </div>

      <Composer
        :busy="busy || !!compaction" :has-session="!!session"
        :modes="meta.modes" :mode="mode" :commands="meta.commands" :plan="plan"
        @send="send" @abort="abort" @switch-mode="switchMode"
        @run-command="runCommand" @build="buildPlan"
      />
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
.topbar .accent { color: var(--accent); background: var(--accent-bg); }
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

.compacting {
  flex-shrink: 0; max-width: 820px; width: calc(100% - 48px); margin: 0 auto 8px;
  border: 1px solid var(--accent); border-radius: var(--radius-sm);
  background: var(--accent-bg); padding: 8px 12px; font-size: 13px;
}
.compacting .line { display: flex; align-items: center; gap: 10px; }
.compacting .line > span:nth-child(2) { flex: 1; }
.compacting .spin { display: inline-block; animation: spin 1s linear infinite; color: var(--accent); }
.compacting .preview {
  margin: 6px 0 0; max-height: 72px; overflow: hidden; font-size: 11.5px;
  color: var(--muted); white-space: pre-wrap; word-break: break-all;
}
@keyframes spin { to { transform: rotate(360deg); } }
.chat .inner { max-width: 820px; margin: 0 auto; padding: 28px 24px 40px; }
.welcome { text-align: center; padding: 90px 20px; color: var(--muted); }
.welcome h2 { font-size: 19px; font-weight: 500; color: var(--text-2); margin: 0 0 8px; }
.welcome p { font-size: 13.5px; margin: 0; }

@media (max-width: 900px) {
  .app, .app.with-viewer { grid-template-columns: 1fr; }
}
</style>
