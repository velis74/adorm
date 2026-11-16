import vue from '@vitejs/plugin-vue';
import { defineConfig } from 'vite';

// demo.py passes the ports; the API serves /api and its documentation.
const api = `http://127.0.0.1:${process.env.DEMO_API_PORT ?? 8000}`;

export default defineConfig({
  plugins: [vue()],
  server: {
    port: Number(process.env.DEMO_FE_PORT ?? 5173),
    strictPort: true,
    proxy: {
      '/api': api,
      '/docs': api,
      '/redoc': api,
      '/openapi.json': api,
    },
  },
});
