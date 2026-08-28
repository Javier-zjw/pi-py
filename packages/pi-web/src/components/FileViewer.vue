<script setup>
import { computed, ref, watch } from "vue";

const props = defineProps({
  file: { type: Object, default: null },
  sessionId: { type: String, default: "" },
});
const emit = defineEmits(["close"]);

const zoom = ref(13);           // 正文字号，px
const MIN = 9;
const MAX = 24;

// 换文件时把缩放重置，避免上一个文件的设置带过来
watch(() => props.file?.path, () => { zoom.value = 13; });

const rawUrl = computed(() => {
  if (!props.file) return "";
  const q = props.sessionId ? `&session=${props.sessionId}` : "";
  return `/api/files/raw?path=${encodeURIComponent(props.file.path)}${q}`;
});
const sizeText = computed(() =>
  props.file ? (props.file.size / 1024).toFixed(1) + " KB" : ""
);
</script>

<template>
  <div v-if="file" class="viewer">
    <div class="head">
      <span class="path mono" :title="file.path">{{ file.name || file.path }}</span>
      <span class="chip">{{ file.language || file.kind }}</span>
      <span class="chip">{{ sizeText }}</span>
      <span class="spacer" />
      <template v-if="file.kind === 'text'">
        <button title="缩小" @click="zoom = Math.max(MIN, zoom - 1)">A−</button>
        <button title="重置" @click="zoom = 13">{{ zoom }}</button>
        <button title="放大" @click="zoom = Math.min(MAX, zoom + 1)">A+</button>
      </template>
      <a class="btn" :href="rawUrl" download :title="'下载 ' + (file.name || '')">↓</a>
      <button title="关闭" @click="emit('close')">✕</button>
    </div>

    <!-- 文本：可滚动 + 可缩放 -->
    <pre
      v-if="file.kind === 'text'" class="body text"
      :style="{ fontSize: zoom + 'px' }"
    ><code>{{ file.content }}</code></pre>

    <!-- 图片：交给浏览器渲染，点击可在新标签打开原图 -->
    <div v-else-if="file.kind === 'image'" class="body center">
      <a :href="rawUrl" target="_blank" rel="noreferrer">
        <img :src="rawUrl" :alt="file.name" />
      </a>
    </div>

    <!-- PDF：浏览器自带阅读器，翻页缩放都有 -->
    <iframe v-else-if="file.kind === 'pdf'" class="body frame" :src="rawUrl" />

    <div v-else class="body center note">
      <p>{{ file.note || "这个文件无法在网页中预览" }}</p>
      <a class="btn big" :href="rawUrl" download>下载文件</a>
    </div>

    <div v-if="file.truncated" class="foot">内容过长，仅显示前 500 KB</div>
  </div>
</template>

<style scoped>
/* min-height:0 是能滚动的前提：flex 子元素默认 min-height:auto，
   不置零的话 .body 会被内容撑开，overflow 根本不生效 */
.viewer {
  border-left: 1px solid var(--line); background: var(--surface);
  display: flex; flex-direction: column; min-width: 0; min-height: 0; height: 100%;
}
.head {
  display: flex; align-items: center; gap: 8px; padding: 0 12px; height: 52px;
  border-bottom: 1px solid var(--line); flex-shrink: 0;
}
.head .path {
  font-size: 12.5px; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; flex: 1; min-width: 0;
}
.head button, .head .btn {
  border: 0; background: none; color: var(--muted); font-size: 12px;
  padding: 3px 7px; border-radius: 4px; text-decoration: none; flex-shrink: 0;
}
.head button:hover, .head .btn:hover { color: var(--accent); background: var(--panel); }

.body { flex: 1; min-height: 0; overflow: auto; }
.text {
  margin: 0; padding: 14px 16px; font-family: var(--mono);
  line-height: 1.7; color: var(--text-2);
  white-space: pre; tab-size: 4;
}
.center {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 14px; padding: 24px;
}
.center img { max-width: 100%; height: auto; border-radius: var(--radius-sm); }
.frame { border: 0; width: 100%; }
.note { color: var(--muted); font-size: 13.5px; text-align: center; }
.note p { margin: 0; }
.btn.big {
  border: 1px solid var(--line); padding: 7px 16px; border-radius: var(--radius-sm);
  color: var(--accent); font-size: 13px;
}
.foot {
  padding: 8px 16px; font-size: 11.5px; color: var(--muted);
  border-top: 1px solid var(--line-soft); flex-shrink: 0;
}
</style>
