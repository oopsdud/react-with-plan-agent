import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  return {
    plugins: [vue()],
    base: '/console/',
    server: {
      proxy: {
        // Swagger served through /api/docs fetches this absolute schema URL.
        '/openapi.json': { target: env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000', changeOrigin: true },
        '/api': { target: env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000', changeOrigin: true, rewrite: path => path.replace(/^\/api/, '') },
      },
    },
    test: { environment: 'jsdom', include: ['tests/**/*.test.ts'], restoreMocks: true },
  }
})
