<template>
  <!-- highlight() escapes the code -->
  <pre class="code-block hljs-tokens" :data-testid="testid"><code v-html="html" /></pre>
</template>

<script setup lang="ts">
import { computed } from 'vue';

import { highlight, type Language } from './highlight';

const props = withDefaults(defineProps<{ code: string; language?: Language; testid?: string }>(), {
  language: 'python',
  testid: undefined,
});

const html = computed(() => highlight(props.code, props.language));
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
