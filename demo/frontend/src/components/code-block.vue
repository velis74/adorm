<template>
  <pre class="code-block" :data-testid="testid"><code>{{ code }}</code></pre>
</template>

<script setup lang="ts">
defineProps<{ code: string; testid?: string }>();
</script>

<style scoped>
.code-block {
  background: rgba(var(--v-theme-on-surface), 0.05);
  border-radius: 4px;
  padding: 0.75em 1em;
  overflow-x: auto;
  font-size: 0.82rem;
  line-height: 1.45;
}
</style>
