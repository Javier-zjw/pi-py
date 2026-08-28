<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  meta: { type: Object, required: true },
  draft: { type: Object, required: true },
  history: { type: Array, default: () => [] },
  files: { type: Object, default: null },
  session: { type: Object, default: null },
  workspaces: { type: Array, default: () => [] },
});
const emit = defineEmits([
  "new", "resume", "browse", "open-file", "switch-workspace", "hide-history",
]);

const tab = ref("chat");
const picking = ref(false);
const dirs = ref(null);
const manual = ref("");

async function walk(path) {
  dirs.value = await fetch(`/api/workspaces/browse?path=${encodeURIComponent(path)}`)
    .then((r) => r.json());
  manual.value = dirs.value.path;
}
async function openPicker() {
  picking.value = true;
  await walk(props.meta.cwd);
}
function choose(path) {
  picking.value = false;
  emit("switch-workspace", path);
}
const TABS = [
  { id: "chat", label: "会话" },
  { id: "files", label: "文件" },
  { id: "config", label: "配置" },
];

const availableModels = computed(() => props.meta.models.filter((m) => m.available));

function toggleAll(kind) {
  const all = kind === "tools"
    ? props.meta.tools.map((t) => t.name)
    : props.meta.skills.map((s) => s.name);
  props.draft[kind] = allOn(kind) ? [] : all;
}
function allOn(kind) {
  const all = kind === "tools" ? props.meta.tools : props.meta.skills;
  return all.length > 0 && props.draft[kind].length === all.length;
}
function timeAgo(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}
</script>

<template>
  <aside class="sidebar">
    <div class="brand">
      <h1><span class="dot" /> pi</h1>
      <button class="cwd" :title="meta.cwd" @click="openPicker">
        <span class="name">{{ meta.cwd.split("/").pop() || meta.cwd }}</span>
        <span class="caret">▾</span>
      </button>
    </div>

    <!-- 工作目录选择器：面包屑随便跳，左侧常用位置一步到位，
         底部还能直接粘路径——不用一层层往上点 -->
    <div v-if="picking" class="modal" @click.self="picking = false">
      <div class="dialog">
        <div class="d-head">
          <strong>选择工作目录</strong>
          <span class="spacer" />
          <button @click="picking = false">✕</button>
        </div>

        <div class="crumbs">
          <button
            v-for="(c, i) in dirs?.breadcrumb || []" :key="c.path"
            class="crumb" :class="{ last: i === (dirs?.breadcrumb?.length || 0) - 1 }"
            @click="walk(c.path)"
          >{{ c.name }}</button>
        </div>

        <div class="d-body">
          <div class="side">
            <div class="grp">常用</div>
            <button v-for="q in dirs?.quick || []" :key="q.path" @click="walk(q.path)">
              {{ q.name }}
            </button>
            <template v-if="workspaces.length">
              <div class="grp">最近</div>
              <button
                v-for="w in workspaces" :key="w.cwd" :disabled="!w.exists"
                :title="w.cwd" @click="w.exists ? choose(w.cwd) : null"
              >{{ w.name }}</button>
            </template>
          </div>

          <div class="dirs">
            <div v-if="!dirs?.entries?.length" class="empty">这个目录下没有子目录</div>
            <div v-for="d in dirs?.entries || []" :key="d.path" class="d-item">
              <span class="t" @dblclick="walk(d.path)" @click="walk(d.path)">
                📁 {{ d.name }}
              </span>
              <button class="use" @click.stop="choose(d.path)">选择</button>
            </div>
          </div>
        </div>

        <div class="d-foot">
          <input
            v-model="manual" class="field" spellcheck="false"
            placeholder="也可以直接粘贴路径" @keydown.enter="walk(manual)"
          />
          <button class="primary" @click="choose(manual || dirs?.path)">
            使用当前目录
          </button>
        </div>
      </div>
    </div>

    <button class="new-btn" @click="emit('new')">＋ 新建会话</button>

    <nav class="tabs">
      <button v-for="t in TABS" :key="t.id" :class="{ on: tab === t.id }" @click="tab = t.id">
        {{ t.label }}
      </button>
    </nav>

    <div class="scroll">
      <!-- 会话历史 -->
      <template v-if="tab === 'chat'">
        <div class="section">
          <div class="section-head"><span>历史会话</span></div>
          <div class="list">
            <div
              v-for="h in history" :key="h.file" class="item history"
              :class="{ on: session && session.file === h.file }" @click="emit('resume', h)"
            >
              <span class="body">
                <span class="t">{{ h.title }}</span>
                <span class="s">{{ timeAgo(h.mtime) }} · {{ h.turns }} 轮</span>
              </span>
              <button
                class="del" title="从列表移除（不删除本地文件）"
                @click.stop="emit('hide-history', h)"
              >✕</button>
            </div>
            <div v-if="!history.length" class="empty">还没有会话记录</div>
          </div>
        </div>
      </template>

      <!-- 文件浏览 -->
      <template v-else-if="tab === 'files'">
        <div class="section">
          <div class="section-head">
            <span class="path" :title="files?.path">{{ files?.path || "." }}</span>
            <button v-if="files?.parent !== null && files" @click="emit('browse', files.parent)">
              ↑ 上级
            </button>
          </div>
          <div class="list">
            <div
              v-for="f in files?.entries || []" :key="f.path" class="item file"
              @click="f.dir ? emit('browse', f.path) : emit('open-file', f.path)"
            >
              <span class="ico">{{ f.dir ? "📁" : "📄" }}</span>
              <span class="t">{{ f.name }}</span>
              <span v-if="!f.dir" class="s">{{ (f.size / 1024).toFixed(1) }}K</span>
            </div>
            <div v-if="!files?.entries?.length" class="empty">空目录</div>
          </div>
        </div>
      </template>

      <!-- 模型 / 技能 / 工具 -->
      <template v-else>
        <div class="section">
          <div class="section-head"><span>模型</span></div>
          <div class="row" style="gap: 8px">
            <select class="field" v-model="draft.model">
              <option v-for="m in availableModels" :key="m.key" :value="m.key">{{ m.name }}</option>
            </select>
            <select class="field" v-model="draft.thinking" style="max-width: 92px">
              <option v-for="l in ['off','minimal','low','medium','high','xhigh','max']"
                      :key="l" :value="l">{{ l }}</option>
            </select>
          </div>
          <p class="hint" v-if="!availableModels.length">
            没有可用模型，检查 ~/.pi/agent/models.json 和 auth.json
          </p>
        </div>

        <div class="section">
          <div class="section-head">
            <span>技能 {{ draft.skills.length ? "· " + draft.skills.length : "" }}</span>
            <button @click="toggleAll('skills')">{{ allOn("skills") ? "全不选" : "全选" }}</button>
          </div>
          <div class="picker">
            <label v-for="s in meta.skills" :key="s.name" class="pick"
                   :class="{ on: draft.skills.includes(s.name) }">
              <input type="checkbox" :value="s.name" v-model="draft.skills" />
              <span class="body">
                <span class="name">{{ s.name }}</span>
                <span class="desc" :title="s.description">{{ s.description }}</span>
              </span>
            </label>
            <div v-if="!meta.skills.length" class="empty">.pi/skills/ 下还没有技能</div>
          </div>
        </div>

        <div class="section">
          <div class="section-head">
            <span>工具 {{ draft.tools.length ? "· " + draft.tools.length : "" }}</span>
            <button @click="toggleAll('tools')">{{ allOn("tools") ? "全不选" : "全选" }}</button>
          </div>
          <div class="picker">
            <label v-for="t in meta.tools" :key="t.name" class="pick"
                   :class="{ on: draft.tools.includes(t.name) }">
              <input type="checkbox" :value="t.name" v-model="draft.tools" />
              <span class="body">
                <span class="name">{{ t.label }}</span>
                <span class="desc" :title="t.description">{{ t.description }}</span>
              </span>
              <span v-if="t.source === 'extension'" class="tag">扩展</span>
            </label>
          </div>
        </div>
      </template>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  position: relative;
  background: var(--panel); border-right: 1px solid var(--line);
  display: flex; flex-direction: column; overflow: hidden;
  min-height: 0; height: 100%;
}
.brand { padding: 18px 18px 12px; }
.brand h1 {
  margin: 0; font-size: 17px; font-weight: 600;
  display: flex; align-items: center; gap: 8px;
}
.brand .dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-bg);
}
.brand .cwd {
  margin-top: 6px; padding: 4px 8px; border: 1px solid var(--line);
  background: var(--surface); border-radius: var(--radius-sm);
  display: flex; align-items: center; gap: 6px; max-width: 100%;
  font-size: 12px; color: var(--text-2);
}
.brand .cwd:hover { border-color: var(--accent); color: var(--accent); }
.brand .cwd .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.brand .cwd .caret { font-size: 9px; color: var(--muted); }

/* 目录选择器：居中弹窗，比侧栏里的小浮层好用得多 */
.modal {
  position: fixed; inset: 0; z-index: 50; display: flex;
  align-items: center; justify-content: center;
  background: rgba(0, 0, 0, 0.35); backdrop-filter: blur(2px);
}
.dialog {
  width: min(720px, 90vw); height: min(560px, 82vh);
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); box-shadow: var(--shadow);
  display: flex; flex-direction: column; overflow: hidden;
}
.d-head {
  display: flex; align-items: center; padding: 12px 16px;
  border-bottom: 1px solid var(--line-soft); font-size: 14px;
}
.d-head button { border: 0; background: none; color: var(--muted); font-size: 14px; }
.d-head button:hover { color: var(--accent); }

.crumbs {
  display: flex; flex-wrap: wrap; gap: 2px; padding: 8px 14px;
  border-bottom: 1px solid var(--line-soft); font-size: 12px;
}
.crumb {
  border: 0; background: none; color: var(--muted); padding: 3px 6px;
  border-radius: 4px; font-family: var(--mono);
}
.crumb:hover { color: var(--accent); background: var(--panel); }
.crumb.last { color: var(--text); font-weight: 500; }
.crumb + .crumb::before { content: "/"; margin-right: 6px; color: var(--line); }

.d-body { flex: 1; min-height: 0; display: flex; }
.side {
  width: 150px; flex-shrink: 0; border-right: 1px solid var(--line-soft);
  padding: 8px; overflow-y: auto; display: flex; flex-direction: column; gap: 1px;
}
.side .grp {
  font-size: 10px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.08em; padding: 8px 8px 4px;
}
.side button {
  border: 0; background: none; color: var(--text-2); text-align: left;
  font-size: 12.5px; padding: 6px 8px; border-radius: var(--radius-sm);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.side button:hover:not(:disabled) { background: var(--panel); color: var(--accent); }
.side button:disabled { opacity: 0.4; cursor: not-allowed; }

.dirs { flex: 1; min-width: 0; overflow-y: auto; padding: 8px; }
.d-item {
  display: flex; align-items: center; gap: 8px; padding: 7px 10px;
  border-radius: var(--radius-sm); font-size: 13px; cursor: pointer;
}
.d-item:hover { background: var(--panel); }
.d-item .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.d-item .use {
  border: 0; background: var(--accent-bg); color: var(--accent);
  font-size: 11px; padding: 3px 9px; border-radius: 4px; flex-shrink: 0;
  opacity: 0; transition: opacity 0.15s;
}
.d-item:hover .use { opacity: 1; }

.d-foot {
  display: flex; gap: 8px; padding: 12px 14px;
  border-top: 1px solid var(--line-soft); flex-shrink: 0;
}
.d-foot .field { flex: 1; font-family: var(--mono); font-size: 12px; cursor: text; }
.d-foot .primary {
  border: 0; background: var(--accent); color: #fff; font-size: 13px;
  padding: 7px 16px; border-radius: var(--radius-sm); flex-shrink: 0;
}
.new-btn {
  margin: 0 14px 12px; padding: 9px 12px; width: calc(100% - 28px);
  border: 1px solid var(--line); background: var(--surface); color: var(--text);
  border-radius: var(--radius-sm); font-size: 13.5px; font-weight: 500;
  transition: all 0.16s ease;
}
.new-btn:hover { border-color: var(--accent); color: var(--accent); transform: translateY(-1px); }

.tabs { display: flex; gap: 2px; padding: 0 14px 10px; }
.tabs button {
  flex: 1; padding: 6px 0; font-size: 12.5px; border: 0; border-radius: var(--radius-sm);
  background: transparent; color: var(--muted);
}
.tabs button.on { background: var(--surface); color: var(--accent); font-weight: 500; }

.scroll { flex: 1; overflow-y: auto; padding-bottom: 16px; }
.section { padding: 0 14px; margin-bottom: 18px; }
.section-head {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 11px; font-weight: 600; letter-spacing: 0.09em; text-transform: uppercase;
  color: var(--muted); padding: 0 4px 7px;
}
.section-head .path {
  font-family: var(--mono); text-transform: none; letter-spacing: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.section-head button {
  border: 0; background: none; color: var(--muted); font-size: 11px;
  padding: 2px 4px; border-radius: 4px; flex-shrink: 0;
}
.section-head button:hover { color: var(--accent); background: var(--accent-bg); }

.list { display: flex; flex-direction: column; gap: 2px; }
.item {
  padding: 8px 9px; border-radius: var(--radius-sm); font-size: 13px;
  border: 1px solid transparent; cursor: pointer; transition: background 0.14s;
}
.item:hover { background: var(--surface); }
.item.on { background: var(--surface); border-color: var(--line-soft); }
.item .t {
  display: block; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; color: var(--text-2);
}
.item.on .t { color: var(--accent); font-weight: 500; }
.item .s { display: block; font-size: 11px; color: var(--muted); margin-top: 2px; }
.item.history { display: flex; align-items: center; gap: 6px; }
.item.history .body { flex: 1; min-width: 0; }
.item.history .del {
  border: 0; background: none; color: var(--muted); font-size: 12px;
  padding: 2px 5px; border-radius: 4px; opacity: 0; flex-shrink: 0; transition: opacity 0.15s;
}
.item.history:hover .del { opacity: 1; }
.item.history .del:hover { color: var(--err); background: var(--panel); }
.item.file { display: flex; align-items: center; gap: 8px; }
.item.file .t { flex: 1; min-width: 0; }
.item.file .s { margin: 0; flex-shrink: 0; }
.item .ico { font-size: 12px; flex-shrink: 0; }

.picker { display: flex; flex-direction: column; gap: 2px; }
.pick {
  display: flex; align-items: flex-start; gap: 9px; padding: 7px 8px;
  border-radius: var(--radius-sm); font-size: 13px; cursor: pointer;
  border: 1px solid transparent; transition: background 0.14s;
}
.pick:hover { background: var(--surface); }
.pick.on { background: var(--surface); border-color: var(--line-soft); }
.pick input { margin: 3px 0 0; accent-color: var(--accent); }
.pick .body { flex: 1; min-width: 0; }
.pick .name { display: block; font-weight: 500; }
.pick.on .name { color: var(--accent); }
.pick .desc {
  display: block; font-size: 11.5px; color: var(--muted); line-height: 1.45;
  margin-top: 1px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pick .tag {
  font-size: 10px; padding: 1px 5px; border-radius: 4px;
  background: var(--accent-bg); color: var(--accent); font-weight: 500; flex-shrink: 0;
}
.hint { font-size: 11.5px; color: var(--err); margin: 8px 4px 0; line-height: 1.5; }
</style>
