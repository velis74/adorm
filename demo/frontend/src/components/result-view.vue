<template>
  <div data-testid="result">
    <div
      v-if="table"
      class="result-grid"
      :style="{ '--columns': table.columns.length, height: `min(260px, ${table.records.length + 1} * 1.9em + 1em)` }"
    >
      <df-grid :columns="table.columns" :records="table.records" key-field="__row" />
    </div>
    <pre v-else class="result-json">{{ JSON.stringify(value, null, 2) }}</pre>
  </div>
</template>

<script setup lang="ts">
import { createColumn } from '@dynamicforms/vue-grid';
import { computed } from 'vue';

const props = defineProps<{ value: unknown }>();

type Row = Record<string, unknown>;

function isScalar(value: unknown): boolean {
  return value === null || ['string', 'number', 'boolean'].includes(typeof value);
}

/** A list of flat objects is shown in a grid; anything else as JSON. */
const table = computed(() => {
  const value = props.value;
  if (!Array.isArray(value) || !value.length) return null;
  if (!value.every((row) => row && typeof row === 'object' && !Array.isArray(row))) return null;
  const rows = value as Row[];
  if (!rows.every((row) => Object.values(row).every(isScalar))) return null;
  const names = Object.keys(rows[0]);
  const columns = names.map((name) => {
    const sample = rows.find((row) => row[name] !== null)?.[name];
    if (typeof sample === 'boolean') return createColumn(name, name, 'checkbox');
    if (typeof sample === 'number') return createColumn(name, name, 'plain', { cssClass: 'text-right' });
    return createColumn(name, name, 'plain');
  });
  return { columns, records: rows.map((row, index) => ({ ...row, __row: index })) };
});
</script>

<style scoped>
.result-grid {
  display: flex;
  flex-direction: column;
}
.result-grid :deep(.df-record-grid) {
  display: grid;
  grid-template-columns: repeat(var(--columns), minmax(min-content, auto));
  column-gap: 1em;
  font-size: 0.85rem;
}
.result-json {
  font-size: 0.8rem;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 260px;
  overflow: auto;
}
</style>
