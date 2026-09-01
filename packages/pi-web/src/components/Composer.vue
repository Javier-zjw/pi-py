<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  busy: Boolean,
  hasSession: Boolean,
  modes: { type: Array, default: () => [] },
  mode: { type: String, default: "chat" },
  commands: { type: Array, default: () => [] },
  plan: { type: Object, default: null },
});
const emit = defineEmits(["send", "abort", "switch-mode", "run-command", "build"]);
const text = ref("");
const box = ref(null);
const active = ref(0);

// 输入以 / 开头且还没打空格时，弹出命令补全
const suggestions = computed(() => {
  const value = text.value;
  if (!value.startsWith("/") || value.includes(" ")) return [];
  const prefix = value.slice(1).toLowerCase();
  return props.commands.filter((c) => c.name.toLowerCase().startsWith(prefix)).slice(0, 8);
});

function pick(cmd) {
  text.value = `/${cmd.name} `;
  active.value = 0;
  box.value?.focus();
}

function grow(e) {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 190) + "px";
}
function onKey(e) {
  const list = suggestions.value;
  if (list.length) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      active.value = (active.value + 1) % list.length;
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      active.value = (active.value - 1 + list.length) % list.length;
      return;
    }
    if (e.key === "Tab") {
      e.preventDefault();
      pick(list[active.value]);
      return;
    }
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (list.length && text.value.slice(1) !== list[active.value].name) {
      pick(list[active.value]);
      return;
    }
    send();
  }
}

function send() {
  const value = text.value.trim();
  if (!value) return;
  // 斜杠命令走命令通道，和终端的 /命令 是同一条后端路径
  if (value.startsWith("/")) {
    const [name, ...rest] = value.slice(1).split(" ");
    emit("run-command", name, rest.join(" "));
  } else {
    emit("send", value);
  }
  text.value = "";
  active.value = 0;
  if (box.value) box.value.style.height = "auto";
}
</script>

<template>
  <div class="composer">
    <!-- 计划就绪时的执行入口：计划模式不能只是提示词，要能一键落地 -->
    <div v-if="plan" class="plan-bar">
      <span class="p-title">📋 {{ plan.summary }}</span>
      <span class="p-meta">{{ (plan.steps || []).length }} 步</span>
      <span class="spacer" />
      <button class="p-run" @click="emit('build')">执行计划</button>
    </div>

    <div v-if="suggestions.length" class="suggest">
      <div
        v-for="(c, i) in suggestions" :key="c.name"
        class="s-item" :class="{ on: i === active }"
        @mousedown.prevent="pick(c)"
      >
        <span class="s-name">/{{ c.name }}</span>
        <span class="s-desc">{{ c.description }}</span>
        <span class="s-src">{{ c.source === "prompt" ? "模板" : "扩展" }}</span>
      </div>
    </div>

    <div class="inner">
      <textarea
        ref="box" v-model="text" rows="1" @input="grow" @keydown="onKey"
        :placeholder="busy ? '模型正在回答…' : '描述你的任务，Enter 发送'"
      />
      <button
        v-if="busy" class="icon-btn stop" title="停止生成" aria-label="停止生成"
        @click="emit('abort')"
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <rect x="3.5" y="3.5" width="9" height="9" rx="1.5" fill="currentColor" />
        </svg>
      </button>
      <button
        v-else class="icon-btn" :disabled="!text.trim()" title="发送（Enter）"
        aria-label="发送" @click="send"
      >
        <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
          <path
            d="M8 13.5V3M8 3L3.5 7.5M8 3l4.5 4.5" fill="none" stroke="currentColor"
            stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
          />
        </svg>
      </button>
    </div>

    <div class="bottom">
      <div class="modes">
        <button
          v-for="m in modes" :key="m.id" class="mode"
          :class="{ on: m.id === mode }" :title="m.description"
          @click="emit('switch-mode', m.id)"
        >{{ m.label }}</button>
      </div>
      <span class="spacer" />
      <span class="hint">
        Enter 发送 · Shift+Enter 换行 · / 唤出命令{{
          hasSession ? "" : " · 发送时自动新建会话"
        }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.composer { border-top: 1px solid var(--line); padding: 14px 24px 18px; background: var(--bg); }
.inner {
  max-width: 820px; margin: 0 auto; background: var(--surface);
  border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow);
  display: flex; align-items: flex-end; gap: 10px; padding: 10px 12px;
  transition: border-color 0.16s;
}
.inner:focus-within { border-color: var(--accent); }
textarea {
  flex: 1; border: 0; outline: none; resize: none; background: none;
  font-family: inherit; font-size: 14.5px; line-height: 1.6; color: var(--text);
  max-height: 190px; padding: 3px 0;
}
textarea::placeholder { color: var(--muted); }
/* 圆形图标按钮：↑ 发送、■ 停止。
   文字按钮在输入框旁边太占地方，图标更符合聊天界面的习惯 */
.icon-btn {
  border: 0; border-radius: 50%; width: 32px; height: 32px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  background: var(--accent); color: #fff;
  transition: opacity 0.15s, transform 0.15s, background 0.15s;
}
.icon-btn:disabled { opacity: 0.32; cursor: not-allowed; }
.icon-btn:not(:disabled):hover { transform: scale(1.06); }
.icon-btn:not(:disabled):active { transform: scale(0.96); }
.icon-btn.stop { background: var(--err); }
.bottom {
  max-width: 820px; margin: 8px auto 0; display: flex; align-items: center; gap: 10px;
}
.hint { font-size: 11px; color: var(--muted); }
.modes { display: flex; gap: 3px; }
.mode {
  border: 1px solid var(--line); background: var(--surface); color: var(--muted);
  font-size: 12px; padding: 3px 11px; border-radius: 20px; transition: all 0.15s;
}
.mode:hover { color: var(--accent); border-color: var(--accent); }
.mode.on {
  background: var(--accent-bg); color: var(--accent);
  border-color: var(--accent); font-weight: 500;
}

.plan-bar {
  max-width: 820px; margin: 0 auto 10px; display: flex; align-items: center; gap: 10px;
  padding: 9px 14px; border-radius: var(--radius);
  background: var(--accent-bg); border: 1px solid var(--accent);
  font-size: 13px;
}
.plan-bar .p-title {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--text);
}
.plan-bar .p-meta { font-size: 11.5px; color: var(--muted); flex-shrink: 0; }
.plan-bar .p-run {
  border: 0; background: var(--accent); color: #fff; font-size: 12.5px;
  padding: 5px 14px; border-radius: var(--radius-sm); flex-shrink: 0;
}

.suggest {
  max-width: 820px; margin: 0 auto 8px; background: var(--surface);
  border: 1px solid var(--line); border-radius: var(--radius);
  box-shadow: var(--shadow); overflow: hidden; max-height: 260px; overflow-y: auto;
}
.s-item {
  display: flex; align-items: center; gap: 10px; padding: 7px 14px;
  font-size: 13px; cursor: pointer;
}
.s-item.on, .s-item:hover { background: var(--accent-bg); }
.s-name { font-family: var(--mono); color: var(--accent); flex-shrink: 0; min-width: 92px; }
.s-desc {
  flex: 1; min-width: 0; color: var(--muted); font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.s-src { font-size: 10.5px; color: var(--muted); flex-shrink: 0; }
</style>
