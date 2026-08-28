<script setup>
import { computed, ref } from "vue";

const props = defineProps({ tool: { type: Object, required: true } });
const open = ref(false);

const icon = computed(() =>
  props.tool.state === "run" ? "◐" : props.tool.state === "ok" ? "✓" : "✗"
);
const argsText = computed(() => {
  const a = props.tool.arguments || {};
  for (const key of ["path", "command", "pattern", "url", "query"]) {
    if (a[key]) return String(a[key]);
  }
  return JSON.stringify(a);
});
const expandable = computed(() => Boolean(props.tool.patch || props.tool.preview));

function renderDiff(patch) {
  return patch.split("\n").map((line) => {
    const cls = line.startsWith("+++") || line.startsWith("---") ? "c"
      : line.startsWith("@@") ? "h"
      : line.startsWith("+") ? "p"
      : line.startsWith("-") ? "m" : "c";
    const safe = line.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
    return `<div class="${cls}">${safe}</div>`;
  }).join("");
}
</script>

<template>
  <div class="tool" :class="{ open }">
    <div class="head" @click="expandable && (open = !open)">
      <span class="ico" :class="tool.state">{{ icon }}</span>
      <span class="name">{{ tool.name }}</span>
      <span class="args" :title="argsText">{{ argsText }}</span>
      <span v-if="expandable" class="caret">{{ open ? "▾" : "▸" }}</span>
    </div>
    <div v-if="open" class="body">
      <div v-if="tool.patch" class="diff" v-html="renderDiff(tool.patch)"></div>
      <pre v-else class="preview">{{ tool.preview }}</pre>
    </div>
  </div>
</template>

<style scoped>
/* 工具用等宽字体 + 面板底色，和自然语言明确区分 */
.tool {
  background: var(--panel); border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm); font-size: 12.5px; font-family: var(--mono);
  overflow: hidden;
}
.head {
  display: flex; align-items: center; gap: 9px; padding: 7px 11px;
  cursor: default;
}
.tool.open .head, .head:hover { background: var(--surface); }
.ico { width: 13px; text-align: center; flex-shrink: 0; }
.ico.run { color: var(--accent); animation: spin 1s linear infinite; }
.ico.ok { color: var(--ok); }
.ico.bad { color: var(--err); }
.name { font-weight: 600; color: var(--text-2); flex-shrink: 0; }
.args {
  color: var(--muted); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; flex: 1; min-width: 0;
}
.caret { color: var(--muted); font-size: 10px; }
.body { border-top: 1px solid var(--line-soft); }
.preview {
  margin: 0; padding: 9px 11px; font-size: 11.5px; color: var(--muted);
  white-space: pre-wrap; max-height: 260px; overflow: auto;
}
.diff {
  padding: 9px 11px; font-size: 11.5px; line-height: 1.55;
  max-height: 320px; overflow: auto;
}
.diff :deep(.p) { color: var(--ok); }
.diff :deep(.m) { color: var(--err); }
.diff :deep(.h) { color: var(--think); }
.diff :deep(.c) { color: var(--muted); }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
