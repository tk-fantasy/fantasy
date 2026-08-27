import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  resolve: {
    alias: {
      '@': '/src',
    },
  },
  plugins: [
    vue({
      // 测试环境禁用模板资源路径改写：<video>/<source> 的相对 mp4 会被
      // 编译成 import，vitest 无法按文件解析（file:/// 崩溃）。
      // 运行时应用走 vite dev/public 目录，不受此开关影响。
      template: { transformAssetUrls: false },
    }),
  ],
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['tests/**/*.test.js'],
  },
})
