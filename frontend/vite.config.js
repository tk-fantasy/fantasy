import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    vue(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.svg', 'pwa-192.png', 'pwa-512.png'],
      manifest: {
        name: 'Aether Home — 智能家居',
        short_name: 'Aether',
        description: 'Aether 智能家居 AI 助手',
        theme_color: '#0a0a0a',
        background_color: '#0a0a0a',
        display: 'standalone',
        scope: '/',
        start_url: '/',
        icons: [
          { src: 'pwa-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512.png', sizes: '512x512', type: 'image/png' },
          { src: 'pwa-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        // 预缓存构建产物（JS/CSS/HTML/图标/字体），不含 mp4
        globPatterns: ['**/*.{js,css,html,svg,png,woff2}'],
        // 不拦截 /api/* 和 /ws/* —— 保持 401→refresh 流程不被 SW 吞掉
        navigateFallbackDenylist: [/^\/api\//, /^\/ws\//],
        maximumFileSizeToCacheInBytes: 5 * 1024 * 1024,
      },
      devOptions: { enabled: false },
    }),
  ],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api/startup-progress': {
        target: 'http://localhost:8011',
        changeOrigin: true,
      },
      '/api': {
        target: 'http://localhost:8010',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8010',
        ws: true,
      },
      '/search': {
        target: 'http://localhost:8010',
        changeOrigin: true,
      },
      '/doc/content': {
        target: 'http://localhost:8010',
        changeOrigin: true,
      },
    },
  },
})
