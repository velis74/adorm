import process from 'node:process';

import vue from '@vitejs/plugin-vue';
import { defineConfig } from 'vite';

// demo.py passes the ports; the API serves /api and its documentation.
const api = `http://127.0.0.1:${process.env.DEMO_API_PORT ?? 8000}`;
// Set in GitHub Codespaces, where the page is served from `<codespace>-5173.<domain>`.
const forwardingDomain = process.env.GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN;
// Set in the dev container, where `localhost` resolves to `::1` only and `demo.py` prints `127.0.0.1`.
const listenOnAllInterfaces = Boolean(process.env.ADORM_DEMO_VITE_HOST);

export default defineConfig({
  plugins: [vue()],
  server: {
    port: Number(process.env.DEMO_FE_PORT ?? 5173),
    strictPort: true,
    ...((listenOnAllInterfaces || forwardingDomain) && { host: true }),
    allowedHosts: forwardingDomain ? [`.${forwardingDomain}`] : undefined,
    proxy: {
      '/api': api,
      '/docs': api,
      '/redoc': api,
      '/openapi.json': api,
    },
  },
});
