import { DynamicFormsVueGrid } from '@dynamicforms/vue-grid';
import { DynamicFormsInputs } from '@dynamicforms/vuetify-inputs';
import { createApp } from 'vue';
import { createVuetify } from 'vuetify';
import * as components from 'vuetify/components';
import * as directives from 'vuetify/directives';

import '@mdi/font/css/materialdesignicons.css';
import 'vuetify/styles';
import '@dynamicforms/vuetify-inputs/styles.css';
import '@dynamicforms/vue-grid/styles.css';

import App from './app.vue';
import { initialTheme } from './preferences';
import './highlight.css';

const vuetify = createVuetify({ components, directives, theme: { defaultTheme: initialTheme() } });

createApp(App)
  .use(vuetify)
  .use(DynamicFormsInputs, { registerComponents: true, registerVuetifyComponents: false })
  .use(DynamicFormsVueGrid, { registerComponents: true })
  .mount('#app');
