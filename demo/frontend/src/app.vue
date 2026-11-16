<template>
  <v-app>
    <v-app-bar density="compact" color="primary">
      <v-app-bar-title>adorm demo · library</v-app-bar-title>
      <template #append>
        <v-chip
          v-for="driver in drivers"
          :key="driver.name"
          class="mr-2"
          size="small"
          variant="elevated"
          :color="driver.available ? 'success' : 'grey'"
          :title="driver.available ? driver.description : driver.reason ?? ''"
          :data-testid="`driver-${driver.name}`"
        >
          {{ driver.name }}<template v-if="driver.mode"> · {{ driver.mode }}</template>
        </v-chip>
        <v-btn variant="text" size="small" href="/docs" target="_blank" rel="noopener">API</v-btn>
      </template>
    </v-app-bar>
    <v-main>
      <v-container fluid>
        <div v-if="error" class="text-error" data-testid="error">{{ error }}</div>
        <v-tabs v-model="tab" color="primary" class="mb-3">
          <v-tab value="examples" data-testid="tab-examples">Examples</v-tab>
          <v-tab value="scenarios" data-testid="tab-scenarios">Concurrency</v-tab>
        </v-tabs>
        <v-window v-model="tab">
          <v-window-item value="examples"><examples-view /></v-window-item>
          <v-window-item value="scenarios"><scenarios-view /></v-window-item>
        </v-window>
      </v-container>
    </v-main>
  </v-app>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';

import { api, type DriverInfo } from './api';
import ExamplesView from './components/examples-view.vue';
import ScenariosView from './components/scenarios-view.vue';

const tab = ref('examples');
const drivers = ref<DriverInfo[]>([]);
const error = ref<string | null>(null);

onMounted(async () => {
  try {
    drivers.value = await api.drivers();
  } catch (e) {
    error.value = String(e);
  }
});
</script>
