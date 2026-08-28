<script setup>
import { computed } from "vue";
import ToolItem from "./ToolItem.vue";

const props = defineProps({ turn: { type: Object, required: true } });
const emit = defineEmits(["toggle"]);

const thinkingLines = computed(() =>
  (props.turn.thinking || "").split("\n").filter((l) => l.trim()).length
);
const failed = computed(() => props.turn.tools.filter((t) => t.state === "bad").length);
const tailThinking = computed(() =>
  (props.turn.thinking || "").split("\n").slice(-3).join("\n")
);
const visibleTools = computed(() =>
  props.turn.toolsOpen || props.turn.phase === "done"
    ? props.turn.tools
    : props.turn.tools.slice(-3)
);

// 极简 markdown：先转义再渲染，绝不把模型输出当 HTML 执行
function render(src) {
  let html = src.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g,
    (_, lang, code) => `<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return html.split(/\n{2,}/).map((block) => {
    if (block.startsWith("<pre>")) return block;
    if (/^#{1,6}\s/.test(block)) return `<h3>${block.replace(/^#{1,6}\s*/, "")}</h3>`;
    if (/^\s*[-*]\s/m.test(block)) {
      const items = block.split("\n").filter((l) => l.trim())
        .map((l) => `<li>${l.replace(/^\s*[-*]\s*/, "")}</li>`).join("");
      return `<ul>${items}</ul>`;
    }
    return `<p>${block.replace(/\n/g, "<br>")}</p>`;
  }).join("");
}
</script>

<template>
  <div class="turn">
    <div v-if="turn.user" class="user-row">
      <div class="msg-user">{{ turn.user }}</div>
    </div>

    <div v-if="turn.phase === 'waiting'" class="waiting">
      <span class="dots"><i /><i /><i /></span>
      <span class="shimmer">正在思考中…</span>
    </div>

    <!-- 思考：流式时只留最后 3 行，正文出现后折叠 -->
    <div v-if="turn.thinking && !turn.folded && turn.thinkOpen" class="msg-think">
      {{ turn.thinkOpen ? turn.thinking : tailThinking }}
    </div>

    <div v-if="turn.tools.length && !turn.folded && turn.toolsOpen" class="tools">
      <ToolItem v-for="tool in visibleTools" :key="tool.id" :tool="tool" />
    </div>

    <!-- 折叠入口：可以来回切，不是单向展开 -->
    <div v-if="turn.thinking || turn.tools.length" class="folded">
      <span
        v-if="turn.thinking" class="fold-chip" :class="{ on: !turn.folded && turn.thinkOpen }"
        @click="emit('toggle', 'think')"
      >
        {{ !turn.folded && turn.thinkOpen ? "▾" : "▸" }} 思考过程 · {{ thinkingLines }} 行
      </span>
      <span
        v-if="turn.tools.length" class="fold-chip" :class="{ on: !turn.folded && turn.toolsOpen }"
        @click="emit('toggle', 'tools')"
      >
        {{ !turn.folded && turn.toolsOpen ? "▾" : "▸" }} {{ turn.tools.length }} 个工具调用
        <span v-if="failed" style="color: var(--err)">· {{ failed }} 失败</span>
      </span>
    </div>

    <div v-if="turn.injected && turn.injected.length" class="injected">
      <div v-for="(msg, i) in turn.injected" :key="i" class="msg-user inject">{{ msg }}</div>
    </div>

    <div v-if="turn.text" class="msg-text" v-html="render(turn.text)"></div>
    <span v-if="turn.phase === 'writing'" class="caret" />

    <div v-if="turn.phase === 'done' && (turn.usage.input || turn.elapsed)" class="meta">
      <span v-if="turn.elapsed">⏱ <b>{{ turn.elapsed.toFixed(1) }}s</b></span>
      <span>↑ <b>{{ turn.usage.input || 0 }}</b></span>
      <span>↓ <b>{{ turn.usage.output || 0 }}</b></span>
      <span v-if="turn.usage.cacheRead">⚡ <b>{{ turn.usage.cacheRead }}</b></span>
      <span v-if="turn.usage.cost">¥<b>{{ turn.usage.cost.toFixed(5) }}</b></span>
      <span v-if="turn.tools.length">🔧 <b>{{ turn.tools.length }}</b></span>
      <span v-if="turn.error" class="err">✗ {{ turn.error }}</span>
    </div>
  </div>
</template>

<style scoped>
.turn { margin-bottom: 30px; }

/* 四类内容的差异化：位置 + 底色 + 字号 + 字体
   用户靠右成气泡，模型靠左铺满——这是聊天界面的基本语法，
   比只靠底色区分直观得多 */
.user-row { display: flex; justify-content: flex-end; margin-bottom: 18px; }
.msg-user {
  background: var(--accent); color: #fff;
  border-radius: 16px 16px 4px 16px;
  padding: 10px 15px; font-size: 15px; line-height: 1.6;
  max-width: 78%; white-space: pre-wrap; word-break: break-word;
  box-shadow: var(--shadow); animation: rise 0.22s ease both;
}
.msg-user.inject {
  font-size: 14px; opacity: 0.9;
  background: var(--accent-bg); color: var(--text);
  border-radius: 14px 14px 4px 14px;
}
.msg-user.inject::before {
  content: "插话"; font-size: 10px; color: var(--accent); font-weight: 600;
  display: block; margin-bottom: 3px; letter-spacing: 0.06em;
}
.injected { display: flex; flex-direction: column; align-items: flex-end; margin-bottom: 14px; }
.msg-think {
  font-size: 12.5px; line-height: 1.65; color: var(--muted); font-style: italic;
  background: var(--think-bg); border-left: 2px solid var(--think);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  padding: 8px 12px; margin-bottom: 10px; white-space: pre-wrap;
  animation: rise 0.22s ease both;
}
.tools { display: flex; flex-direction: column; gap: 5px; margin-bottom: 12px; }
.msg-text { font-size: 15.5px; line-height: 1.82; word-break: break-word; }
.msg-text :deep(p) { margin: 0 0 0.85em; }
.msg-text :deep(p:last-child) { margin-bottom: 0; }
.msg-text :deep(h3) { font-size: 15.5px; font-weight: 600; margin: 1.1em 0 0.5em; }
.msg-text :deep(ul) { margin: 0 0 0.85em; padding-left: 1.3em; }
.msg-text :deep(code) {
  font-family: var(--mono); font-size: 0.87em; background: var(--panel);
  padding: 1.5px 5px; border-radius: 4px; color: var(--accent);
}
.msg-text :deep(pre) {
  background: var(--panel); border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm); padding: 12px 14px; overflow-x: auto;
  font-size: 12.5px; margin: 0 0 0.9em;
}
.msg-text :deep(pre code) { background: none; padding: 0; color: var(--text-2); }

.folded { display: flex; gap: 7px; flex-wrap: wrap; margin-bottom: 12px; }
.fold-chip {
  font-size: 11.5px; color: var(--muted); background: var(--panel);
  border: 1px solid var(--line-soft); border-radius: 20px; padding: 3px 11px;
  cursor: pointer; transition: all 0.15s;
}
.fold-chip:hover { color: var(--accent); border-color: var(--accent); }
.fold-chip.on { color: var(--accent); border-color: var(--accent); background: var(--accent-bg); }

.waiting {
  display: flex; align-items: center; gap: 9px; font-size: 13px;
  color: var(--muted); padding: 3px 0 12px;
}
.dots { display: inline-flex; gap: 4px; }
.dots i {
  width: 5px; height: 5px; border-radius: 50%; background: var(--accent);
  display: block; animation: bounce 1.25s ease-in-out infinite;
}
.dots i:nth-child(2) { animation-delay: 0.16s; }
.dots i:nth-child(3) { animation-delay: 0.32s; }
.shimmer {
  background: linear-gradient(90deg, var(--muted) 20%, var(--accent) 45%, var(--muted) 70%);
  background-size: 220% 100%; -webkit-background-clip: text; background-clip: text;
  color: transparent; animation: shimmer 2.1s linear infinite;
}
.caret {
  display: inline-block; width: 7px; height: 1.05em; background: var(--accent);
  vertical-align: text-bottom; margin-left: 2px; border-radius: 1px;
  animation: blink 1.05s steps(2) infinite;
}
.meta {
  display: flex; gap: 14px; flex-wrap: wrap; font-size: 11.5px; color: var(--muted);
  font-family: var(--mono); padding-top: 10px; margin-top: 12px;
  border-top: 1px solid var(--line-soft);
}
.meta b { color: var(--text-2); font-weight: 500; }
.meta .err { color: var(--err); font-family: var(--font); }

@keyframes rise { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: none; } }
@keyframes bounce { 0%, 70%, 100% { transform: translateY(0); opacity: 0.45; } 35% { transform: translateY(-5px); opacity: 1; } }
@keyframes shimmer { to { background-position: -220% 0; } }
@keyframes blink { 50% { opacity: 0; } }
</style>
