<script setup>
import { computed, ref } from "vue";

// 压缩卡片：实时压缩结束和历史回放共用。数据形状见后端 dto.py::compaction_dto
const props = defineProps({ data: { type: Object, required: true } });
const open = ref(false);

const REASONS = { threshold: "自动", manual: "手动", overflow: "超窗口" };
const reason = computed(() => REASONS[props.data.reason] || props.data.reason || "");
const saved = computed(() => {
  const { tokensBefore: b, tokensAfter: a } = props.data;
  return b && a != null ? Math.max(0, Math.round((1 - a / b) * 100)) : null;
});
const llm = computed(() => props.data.llm || {});
const progress = computed(() => llm.value.progress || {});
const ledger = computed(() => props.data.fileLedger || {});

function k(n) {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n ?? "?"}`;
}
</script>

<template>
  <div class="compaction" :class="{ degraded: data.degraded }">
    <div class="head" @click="open = !open">
      <span class="caret">{{ open ? "▾" : "▸" }}</span>
      <span class="label">上下文已压缩<template v-if="data.seq">（第 {{ data.seq }} 次）</template></span>
      <span class="chip">{{ reason }}</span>
      <span class="chip">{{ k(data.tokensBefore) }} → {{ k(data.tokensAfter) }} tokens</span>
      <span v-if="saved != null" class="chip">省 {{ saved }}%</span>
      <span v-if="data.degraded" class="chip warn">摘要降级</span>
      <span v-if="data.rehydratedFiles?.length" class="chip" :title="data.rehydratedFiles.join('\n')">
        补回 {{ data.rehydratedFiles.length }} 个文件
      </span>
      <span v-if="data.restored?.length" class="chip" :title="data.restored.join(', ')">
        补回 {{ data.restored.length }} 条
      </span>
    </div>

    <div v-if="open" class="body">
      <template v-if="data.llm">
        <section v-if="llm.task">
          <h4>目标</h4>
          <p>{{ llm.task.goal }}</p>
          <p v-if="llm.task.status" class="muted">当前：{{ llm.task.status }}</p>
        </section>
        <section v-if="llm.constraints?.length">
          <h4>约束与偏好</h4>
          <ul><li v-for="c in llm.constraints" :key="c.id">{{ c.text }}</li></ul>
        </section>
        <section v-if="progress.done?.length || progress.inProgress?.length || progress.blocked?.length">
          <h4>进展</h4>
          <ul>
            <li v-for="p in progress.done || []" :key="p.id">✓ {{ p.text }}</li>
            <li v-for="p in progress.inProgress || []" :key="p.id">… {{ p.text }}</li>
            <li v-for="p in progress.blocked || []" :key="p.id" class="warn">
              ✗ {{ p.text }}<template v-if="p.reason">（{{ p.reason }}）</template>
            </li>
          </ul>
        </section>
        <section v-if="llm.decisions?.length">
          <h4>关键决策</h4>
          <ul>
            <li v-for="d in llm.decisions" :key="d.id">
              {{ d.decision }}<span v-if="d.reason" class="muted"> —— {{ d.reason }}</span>
            </li>
          </ul>
        </section>
        <section v-if="llm.knowledge?.length">
          <h4>关键认知</h4>
          <ul>
            <li v-for="item in llm.knowledge" :key="item.id">
              {{ item.fact || item.text }}<code v-if="item.where">{{ item.where }}</code>
            </li>
          </ul>
        </section>
        <section v-if="ledger.modified?.length || ledger.read?.length">
          <h4>文件</h4>
          <ul>
            <li v-for="f in ledger.modified || []" :key="'m' + f.path">
              <code>{{ f.path }}</code> <span class="muted">修改×{{ f.edits }}</span>
            </li>
            <li v-if="ledger.read?.length" class="muted">读取 {{ ledger.read.length }} 个文件</li>
          </ul>
        </section>
        <section v-if="llm.nextSteps?.length">
          <h4>下一步</h4>
          <ol><li v-for="(step, i) in llm.nextSteps" :key="i">{{ step }}</li></ol>
        </section>
        <p v-if="data.userMessages" class="muted">另有 {{ data.userMessages }} 条用户原话逐字保留</p>
      </template>
      <!-- 旧格式会话：只有一段文字摘要 -->
      <pre v-else class="raw">{{ data.summary }}</pre>
    </div>
  </div>
</template>

<style scoped>
.compaction {
  border: 1px dashed var(--line); border-radius: var(--radius-sm);
  margin: 14px 0; font-size: 13px; background: color-mix(in srgb, var(--accent-bg) 40%, transparent);
}
.compaction.degraded { border-color: var(--err); }
.head {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  padding: 8px 12px; cursor: pointer; color: var(--text-2);
}
.head .label { font-weight: 500; }
.caret { color: var(--muted); width: 10px; }
.warn { color: var(--err); }
.muted { color: var(--muted); }
.body { padding: 4px 16px 12px 30px; border-top: 1px dashed var(--line); }
.body h4 { margin: 10px 0 4px; font-size: 12px; font-weight: 500; color: var(--muted); }
.body p { margin: 2px 0; }
.body ul, .body ol { margin: 2px 0; padding-left: 18px; }
.body code { margin-left: 6px; font-size: 12px; }
.raw { white-space: pre-wrap; font-size: 12px; max-height: 360px; overflow: auto; }
</style>
