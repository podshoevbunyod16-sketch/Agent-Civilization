import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// Backend проксируется через dev-сервер: браузер клиента никогда не
// обращается к localhost напрямую (важно для превью в браузере).
const API_TARGET = process.env.VITE_API_PROXY ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: false,
    // Ограничиваем Host локальной разработкой и доменом браузерного превью.
    // Разрешать любой Host опасно: это открывает возможность DNS rebinding.
    allowedHosts: ['.e2b.app'],
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true, ws: true },
    },
  },
  preview: { host: '0.0.0.0', allowedHosts: ['.e2b.app'] },
  build: { outDir: 'dist', sourcemap: false },
})
