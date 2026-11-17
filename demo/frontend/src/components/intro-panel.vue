<template>
  <v-expansion-panels v-model="open" class="mb-3" data-testid="intro">
    <v-expansion-panel value="intro">
      <v-expansion-panel-title data-testid="intro-toggle">
        <span class="font-weight-medium">About this demo</span>
      </v-expansion-panel-title>
      <v-expansion-panel-text class="text-body-2" data-testid="intro-text">
        <p class="mb-2">
          A library: authors, books, editions, tags, readers and loans. The schema is a Django app; the adorm models
          are generated from it with <code>adorm generate</code>. The examples and scenarios are coroutine functions
          on those models, called by this page through a FastAPI API.
        </p>
        <v-row dense class="mb-1">
          <v-col cols="12" md="6">
            <div class="text-subtitle-2">Examples</div>
            <p>
              The query API as Django developers know it, with <code>await</code> and <code>async for</code>. Each
              example shows its code; a run shows, per driver, the SQL sent to the database, the result and the time
              the run took.
            </p>
          </v-col>
          <v-col cols="12" md="6">
            <div class="text-subtitle-2">Concurrency</div>
            <p>
              Several flows in concurrent tasks, each in a session of its own: row locks, a lock timeout, cancelling
              a statement, a deadlock in the database, a deadlock between pool slots, an exhausted pool. A run shows
              when each flow did what, what it saw, its SQL and how it ended.
            </p>
          </v-col>
        </v-row>
        <p class="mb-2">
          Every run executes on each available driver ({{ driverNames }}). The same code gives the same result on
          each; the times show that the drivers are comparable, not which one is faster under load.
        </p>
        <p class="mb-0">
          Setting adorm up in a project: the quick start, <code>docs/intro/quickstart.md</code> in the adorm
          repository. This demo: <code>docs/intro/demo.md</code>. The API behind this page:
          <a href="/docs" target="_blank" rel="noopener">/docs</a>.
        </p>
      </v-expansion-panel-text>
    </v-expansion-panel>
  </v-expansion-panels>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';

import type { DriverInfo } from '../api';
import { loadPreference, savePreference } from '../preferences';

const props = defineProps<{ drivers: DriverInfo[] }>();

// Open on the first visit and closed on later ones, unless the reader opened it last time.
const stored = loadPreference('intro');
const open = ref<string[]>(stored === null || stored === 'open' ? ['intro'] : []);
if (stored === null) savePreference('intro', 'closed');
watch(open, (value) => savePreference('intro', value.length ? 'open' : 'closed'));

const driverNames = computed(() => props.drivers.map((driver) => driver.name).join(', ') || 'none yet');
</script>
