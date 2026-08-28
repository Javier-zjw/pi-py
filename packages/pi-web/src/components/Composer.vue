<script setup>
import { ref } from "vue";

defineProps({ busy: Boolean, hasSession: Boolean });
const emit = defineEmits(["send", "abort"]);
const text = ref("");
const box = ref(null);

function grow(e) {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 190) + "px";
}
function onKey(e) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
}
function send() {
  const value = text.value.trim();
  if (!value) return;
  emit("send", value);
  text.value = "";
  if (box.value) box.value.style.height = "auto";
}
</script>

<template>
  <div class="composer">
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
    <div class="hint">
      Enter 发送 · Shift+Enter 换行{{ hasSession ? "" : " · 发送时自动新建会话" }}
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
.hint { max-width: 820px; margin: 7px auto 0; font-size: 11px; color: var(--muted); }
</style>
