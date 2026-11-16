<template>
  <v-row>
    <v-col cols="12" md="3">
      <v-list density="compact" nav data-testid="scenario-list">
        <v-list-item
          v-for="scenario in scenarios"
          :key="scenario.id"
          :active="selected?.id === scenario.id"
          :title="scenario.title"
          :data-testid="`scenario-${scenario.id}`"
          @click="select(scenario)"
        />
      </v-list>
    </v-col>
    <v-col v-if="selected" cols="12" md="9">
      <h2 class="text-h6" data-testid="scenario-title">{{ selected.title }}</h2>
      <p class="text-body-2 mb-3">{{ selected.description }}</p>
      <div class="text-overline">Flows</div>
      <code-block :code="selected.code" testid="scenario-code" />
      <v-expansion-panels class="mt-2" variant="accordion">
        <v-expansion-panel title="How the demo steers the flows">
          <v-expansion-panel-text><code-block :code="selected.choreography" /></v-expansion-panel-text>
        </v-expansion-panel>
      </v-expansion-panels>
      <v-btn class="mt-3" color="primary" :loading="running" data-testid="run-scenario" @click="run">
        Run on every driver
      </v-btn>
      <div v-if="error" class="text-error mt-2" data-testid="error">{{ error }}</div>
      <v-row v-if="runs" class="mt-2" data-testid="scenario-runs">
        <v-col v-for="driverRun in runs.runs" :key="driverRun.driver" cols="12" xl="6">
          <v-card variant="outlined" :data-testid="`run-${driverRun.driver}`">
            <v-card-title class="d-flex flex-wrap align-center ga-2 text-subtitle-1">
              <span class="font-weight-bold">{{ driverRun.driver }}</span>
              <v-chip size="small">{{ driverRun.ms.toFixed(1) }} ms</v-chip>
              <v-chip
                v-for="flow in driverRun.flows"
                :key="flow"
                size="small"
                :color="sessionColor(flow)"
                :prepend-icon="outcomeIcon(driverRun.outcomes[flow])"
                :data-testid="`outcome-${flow}`"
                :data-status="driverRun.outcomes[flow].status"
              >
                {{ flow }}: {{ outcomeText(driverRun.outcomes[flow]) }}
              </v-chip>
            </v-card-title>
            <v-card-text>
              <div class="text-overline">What every flow saw</div>
              <flow-timeline :run="driverRun" />
              <div class="text-overline mt-2">SQL of every flow</div>
              <sql-list :statements="driverRun.statements" show-session />
            </v-card-text>
          </v-card>
        </v-col>
      </v-row>
    </v-col>
  </v-row>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';

import { api, type FlowOutcome, type ScenarioInfo, type ScenarioRuns } from '../api';

import CodeBlock from './code-block.vue';
import { sessionColor } from './colors';
import FlowTimeline from './flow-timeline.vue';
import SqlList from './sql-list.vue';

const scenarios = ref<ScenarioInfo[]>([]);
const selected = ref<ScenarioInfo | null>(null);
const runs = ref<ScenarioRuns | null>(null);
const running = ref(false);
const error = ref<string | null>(null);
let request = 0;

function select(scenario: ScenarioInfo) {
  request += 1;
  selected.value = scenario;
  runs.value = null;
  error.value = null;
  running.value = false;
}

function outcomeIcon(outcome: FlowOutcome): string {
  if (outcome.status === 'ok') return 'mdi-check';
  if (outcome.status === 'cancelled') return 'mdi-cancel';
  return 'mdi-alert-circle-outline';
}

function outcomeText(outcome: FlowOutcome): string {
  if (outcome.status === 'ok') return String(outcome.result);
  if (outcome.status === 'cancelled') return 'cancelled';
  if (outcome.held_sessions) return `${outcome.error}, holding ${outcome.held_sessions.join(', ')}`;
  return outcome.sqlstate ? `${outcome.error} ${outcome.sqlstate}` : outcome.error;
}

async function run() {
  if (!selected.value) return;
  const current = ++request;
  running.value = true;
  error.value = null;
  try {
    const answer = await api.runScenario(selected.value.id);
    if (current === request) runs.value = answer;
  } catch (e) {
    if (current === request) error.value = String(e);
  } finally {
    if (current === request) running.value = false;
  }
}

onMounted(async () => {
  try {
    scenarios.value = await api.scenarios();
    if (scenarios.value.length) select(scenarios.value[0]);
  } catch (e) {
    error.value = String(e);
  }
});
</script>
