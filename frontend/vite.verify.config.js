// 临时验证配置——浏览器布局验证用，验证完即删除，不进 git。
// 全量 mock /api（无后端、无真实凭据），并生成一张假视频帧供 <img> 显示。
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { VitePWA } from 'vite-plugin-pwa'

const CAMERAS = [
  { id: 'cam_a', name: '客厅摄像头', enabled: true, ptz_enabled: true },
  { id: 'cam_b', name: '门口摄像头', enabled: false, ptz_enabled: false },
  { id: 'cam_c', name: '书房摄像头', enabled: false, ptz_enabled: false },
]

let feedPromise = null
function getFeed() {
  if (!feedPromise) {
    feedPromise = (async () => {
      const sharp = (await import('sharp')).default
      const svg = `<svg width="1280" height="720" xmlns="http://www.w3.org/2000/svg">
        <rect width="1280" height="720" fill="#16302a"/>
        <rect x="40" y="40" width="1200" height="640" fill="none" stroke="#e8b93c" stroke-width="6"/>
        <circle cx="640" cy="380" r="190" fill="#e8b93c"/>
        <text x="640" y="120" font-size="56" fill="#ffffff" text-anchor="middle">FAKE FEED 1280x720</text>
      </svg>`
      return sharp(Buffer.from(svg)).jpeg({ quality: 80 }).toBuffer()
    })()
  }
  return feedPromise
}

function json(res, payload) {
  res.setHeader('Content-Type', 'application/json')
  res.end(JSON.stringify(payload))
}

async function handleApi(req, res, next) {
  const url = (req.url || '').split('?')[0]
  if (url === '/health') return json(res, { status: 'ok' })
  if (url === '/auth/login') return json(res, {
    data: { user: { id: 'u1', username: 'layout-verify', display_name: '布局验证', is_admin: true } },
  })
  if (url === '/auth/me') return json(res, {
    data: { id: 'u1', username: 'layout-verify', display_name: '布局验证', is_admin: true },
  })
  if (url === '/cameras') return json(res, { data: CAMERAS })
  let m = url.match(/^\/cameras\/([^/]+)\/state$/)
  if (m) {
    return json(res, {
      data: {
        camera_opened: true,
        motion_distance: '0.42m',
        infer_count: 128,
        model_fps: 24.6,
        feedback: '（布局验证用假数据）检测到 1 个目标。',
      },
    })
  }
  m = url.match(/^\/cameras\/([^/]+)\/video_feed$/)
  if (m) {
    res.setHeader('Content-Type', 'image/jpeg')
    res.end(await getFeed())
    return
  }
  // 其余 /api/*（display enable/disable、ptz、sessions、weather…）一律 200 空数据，
  // 避免 401/4xx 触发前端全局登出拦截。
  return json(res, { data: {} })
}

export default defineConfig({
  plugins: [
    vue(),
    // 生产同款插件仅用于解析 virtual:pwa-register;dev 下 devOptions.enabled=false
    VitePWA({
      registerType: 'autoUpdate',
      devOptions: { enabled: false },
    }),
    { name: 'mock-api', configureServer(server) { server.middlewares.use('/api', handleApi) } },
  ],
  server: {
    port: 5174,
    strictPort: true,
    fs: { allow: ['..'] },
  },
})
