<script setup>
defineProps({ extensions: { type: Array, default: () => [] } });
const emit = defineEmits(["toggle"]);

function kinds(registered) {
  const count = { tool: 0, command: 0, mode: 0, hook: 0 };
  for (const item of registered || []) {
    const key = item.split(":")[0];
    if (key in count) count[key] += 1;
  }
  return Object.entries(count)
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${n} 个${{ tool: "工具", command: "命令", mode: "模式", hook: "钩子" }[k]}`);
}
</script>

<template>
  <div class="section">
    <div class="section-head"><span>扩展</span></div>
    <div class="plugins">
      <div v-for="e in extensions" :key="e.key" class="plugin" :class="{ off: !e.enabled }">
        <div class="row">
          <label class="sw">
            <input
              type="checkbox" :checked="e.enabled"
              @change="emit('toggle', e.key, $event.target.checked)"
            />
            <span class="track"><span class="knob" /></span>
          </label>
          <span class="name">{{ e.name }}</span>
          <span v-if="e.builtin" class="tag">内置</span>
        </div>
        <p v-if="e.description" class="desc">{{ e.description }}</p>
        <p v-if="e.error" class="err">加载失败：{{ e.error }}</p>
        <p v-else-if="e.enabled && kinds(e.registered).length" class="caps">
          {{ kinds(e.registered).join(" · ") }}
        </p>
      </div>
      <div v-if="!extensions.length" class="empty">没有发现扩展</div>
    </div>
    <p class="tip">
      扩展放在 <code>.pi/extensions/</code> 或 <code>~/.pi/agent/extensions/</code>，
      导出 <code>activate(pi)</code> 即可。停用会立刻移除它注册的工具和命令。
    </p>
  </div>
</template>

<style scoped>
.plugins { display: flex; flex-direction: column; gap: 4px; }
.plugin {
  padding: 9px 10px; border-radius: var(--radius-sm);
  border: 1px solid var(--line-soft); background: var(--surface);
}
.plugin.off { opacity: 0.55; }
.plugin .row { display: flex; align-items: center; gap: 8px; }
.plugin .name { font-size: 13px; font-weight: 500; flex: 1; min-width: 0; }
.plugin .tag {
  font-size: 10px; padding: 1px 5px; border-radius: 4px;
  background: var(--panel); color: var(--muted);
}
.plugin .desc { margin: 5px 0 0; font-size: 11.5px; color: var(--muted); line-height: 1.5; }
.plugin .caps { margin: 4px 0 0; font-size: 11px; color: var(--accent); }
.plugin .err { margin: 4px 0 0; font-size: 11px; color: var(--err); line-height: 1.5; }

/* 开关：比复选框更能表达"启用/停用"这种持久状态 */
.sw { position: relative; display: inline-flex; flex-shrink: 0; cursor: pointer; }
.sw input { position: absolute; opacity: 0; width: 0; height: 0; }
.sw .track {
  width: 30px; height: 17px; border-radius: 9px; background: var(--line);
  display: block; transition: background 0.18s;
}
.sw .knob {
  width: 13px; height: 13px; border-radius: 50%; background: #fff;
  display: block; margin: 2px; transition: transform 0.18s;
}
.sw input:checked + .track { background: var(--accent); }
.sw input:checked + .track .knob { transform: translateX(13px); }

.tip { margin: 10px 4px 0; font-size: 11px; color: var(--muted); line-height: 1.6; }
.tip code {
  font-family: var(--mono); font-size: 10.5px;
  background: var(--panel); padding: 1px 4px; border-radius: 3px;
}
</style>
