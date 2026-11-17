<template>
  <v-row>
    <v-col cols="12" md="3">
      <v-list density="compact" nav data-testid="example-list">
        <v-list-item
          v-for="example in examples"
          :key="example.id"
          :active="selected?.id === example.id"
          :title="example.title"
          :data-testid="`example-${example.id}`"
          @click="select(example)"
        >
          <template #append>
            <v-icon v-if="example.writes" size="small" icon="mdi-pencil" title="writes; reverted after each run" />
          </template>
        </v-list-item>
      </v-list>
    </v-col>
    <v-col v-if="selected" cols="12" md="9">
      <h2 class="text-h6" data-testid="example-title">{{ selected.title }}</h2>
      <p class="text-body-2 mb-3">{{ selected.description }}</p>
      <code-block :code="selected.code" testid="example-code" />
      <div class="d-flex flex-wrap align-center ga-3 mt-3">
        <div v-for="param in selected.params" :key="param.name" class="param">
          <df-select
            v-if="param.kind === 'choice'"
            :control="fields[param.name]"
            :choices="param.choices.map((choice) => ({ id: choice, text: choice === '' ? '(any)' : String(choice) }))"
            :label="param.label"
            :data-testid="`param-${param.name}`"
          />
          <df-input
            v-else
            :control="fields[param.name]"
            :input-type="param.kind === 'int' ? 'number' : 'text'"
            :label="param.label"
            :data-testid="`param-${param.name}`"
          />
        </div>
        <v-btn color="primary" :loading="running" :disabled="!valid" data-testid="run-example" @click="run">
          Run on every driver
        </v-btn>
      </div>
      <div v-if="error" class="text-error mt-2" data-testid="error">{{ error }}</div>
      <p v-if="selected.writes" class="text-caption text-medium-emphasis mt-2">
        The example commits its writes; the demo reverts them after each driver's run.
      </p>
      <v-row v-if="runs" class="mt-2" data-testid="example-runs">
        <v-col v-for="driverRun in runs.runs" :key="driverRun.driver" cols="12" lg="6">
          <v-card variant="outlined" :data-testid="`run-${driverRun.driver}`">
            <v-card-title class="d-flex align-center ga-2 text-subtitle-1">
              <span class="font-weight-bold">{{ driverRun.driver }}</span>
              <v-chip size="small" data-testid="run-time" title="Time of this run, SQL included; not a benchmark">
                {{ driverRun.ms.toFixed(1) }} ms
              </v-chip>
              <v-chip size="small" variant="tonal">
                {{ driverRun.statements.length }} {{ driverRun.statements.length === 1 ? 'statement' : 'statements' }}
              </v-chip>
            </v-card-title>
            <v-card-text>
              <div class="text-overline">Result</div>
              <div v-if="driverRun.error" class="text-error" data-testid="run-error">
                {{ driverRun.error.error }}: {{ driverRun.error.message }}
              </div>
              <result-view v-else :value="driverRun.result" />
              <div class="text-overline mt-2">SQL</div>
              <sql-list :statements="driverRun.statements" />
            </v-card-text>
          </v-card>
        </v-col>
      </v-row>
    </v-col>
  </v-row>
</template>

<script setup lang="ts">
import { Field, Validators } from '@dynamicforms/vue-forms';
import { computed, onMounted, ref, shallowRef } from 'vue';

import { api, type ExampleInfo, type ExampleRuns, type ParamInfo } from '../api';

import CodeBlock from './code-block.vue';
import ResultView from './result-view.vue';
import SqlList from './sql-list.vue';

const examples = ref<ExampleInfo[]>([]);
const selected = ref<ExampleInfo | null>(null);
const fields = shallowRef<Record<string, Field>>({});
const runs = ref<ExampleRuns | null>(null);
const running = ref(false);
const error = ref<string | null>(null);
const valid = computed(() => Object.values(fields.value).every((field) => field.valid));
let request = 0;

function validators(param: ParamInfo) {
  const out = [];
  if (param.minimum !== null) out.push(new Validators.MinValue(param.minimum));
  if (param.maximum !== null) out.push(new Validators.MaxValue(param.maximum));
  return out;
}

function select(example: ExampleInfo) {
  request += 1;
  selected.value = example;
  runs.value = null;
  error.value = null;
  running.value = false;
  fields.value = Object.fromEntries(
    example.params.map((param) => [param.name, new Field({ value: param.default, validators: validators(param) })]),
  );
}

async function run() {
  if (!selected.value) return;
  const current = ++request;
  running.value = true;
  error.value = null;
  try {
    const params = Object.fromEntries(Object.entries(fields.value).map(([name, field]) => [name, field.value ?? '']));
    const answer = await api.runExample(selected.value.id, params);
    if (current === request) runs.value = answer;
  } catch (e) {
    if (current === request) error.value = String(e);
  } finally {
    if (current === request) running.value = false;
  }
}

onMounted(async () => {
  try {
    examples.value = await api.examples();
    if (examples.value.length) select(examples.value[0]);
  } catch (e) {
    error.value = String(e);
  }
});
</script>

<style scoped>
.param {
  min-width: 220px;
}
</style>
