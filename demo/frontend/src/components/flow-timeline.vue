<template>
  <div class="timeline" :style="{ '--flows': lanes.length }" data-testid="timeline">
    <div class="cell head">ms</div>
    <div v-for="lane in lanes" :key="lane" class="cell head">
      <v-chip size="x-small" label :color="sessionColor(lane)">{{ lane === 'demo' ? 'observer' : `flow ${lane}` }}</v-chip>
    </div>
    <template v-for="(event, index) in run.events" :key="index">
      <div class="cell time">{{ event.at_ms.toFixed(1) }}</div>
      <div
        v-for="lane in lanes"
        :key="lane"
        class="cell"
        :class="lane === event.flow ? `event ${event.kind}` : ''"
        :data-testid="lane === event.flow ? `event-${lane}` : undefined"
      >
        <template v-if="lane === event.flow">{{ event.text }}</template>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

import type { ScenarioRun } from '../api';

import { sessionColor } from './colors';

const props = defineProps<{ run: ScenarioRun }>();

/** A column per flow, then the observer's. */
const lanes = computed(() => [...props.run.flows, 'demo']);
</script>

<style scoped>
.timeline {
  display: grid;
  grid-template-columns: 4.5em repeat(var(--flows), minmax(0, 1fr));
  font-size: 0.8rem;
  border-top: 1px solid rgba(var(--v-theme-on-surface), 0.12);
}
.cell {
  padding: 0.2em 0.4em;
  border-bottom: 1px solid rgba(var(--v-theme-on-surface), 0.06);
  word-break: break-word;
}
.head {
  font-weight: 600;
}
.time {
  text-align: right;
  color: rgba(var(--v-theme-on-surface), 0.6);
  font-variant-numeric: tabular-nums;
}
.event.ok {
  color: rgb(var(--v-theme-success));
}
.event.error {
  color: rgb(var(--v-theme-error));
}
.event.observe {
  font-style: italic;
  color: rgba(var(--v-theme-on-surface), 0.7);
}
</style>
