<template>
  <div class="sql-list" data-testid="sql-list">
    <div v-if="!statements.length" class="text-medium-emphasis text-caption">no SQL</div>
    <div v-for="(statement, index) in statements" :key="index" class="statement" data-testid="sql-statement">
      <div class="d-flex align-center ga-2 text-caption text-medium-emphasis">
        <v-chip v-if="showSession" size="x-small" :color="sessionColor(statement.session)" label>
          {{ statement.session ?? 'connection setup' }}
        </v-chip>
        <span>at {{ statement.at_ms.toFixed(1) }} ms</span>
        <span>· {{ statement.duration_ms.toFixed(2) }} ms</span>
      </div>
      <pre class="sql">{{ statement.sql.trim() }}</pre>
      <div v-if="statement.params.length" class="text-caption text-medium-emphasis params">
        params: {{ statement.params.map((p) => JSON.stringify(p)).join(', ') }}
      </div>
      <div v-if="statement.error" class="text-caption text-error">{{ statement.error }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Statement } from '../api';

import { sessionColor } from './colors';

defineProps<{ statements: Statement[]; showSession?: boolean }>();
</script>

<style scoped>
.statement {
  border-left: 3px solid rgba(var(--v-theme-on-surface), 0.12);
  padding: 0.25em 0 0.25em 0.6em;
  margin-bottom: 0.4em;
}
.sql {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.78rem;
  margin: 0.15em 0;
}
.params {
  word-break: break-word;
}
</style>
