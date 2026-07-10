#!/usr/bin/env node
/**
 * 一次性脚本：从 public/favicon.svg 生成 PWA 所需的 PNG 图标。
 *  - pwa-192.png          (192x192, 普通图标)
 *  - pwa-512.png          (512x512, 普通图标)
 *  - pwa-maskable-512.png (512x512, maskable: 带安全边距 + 背景)
 *
 * 用法: node scripts/gen-pwa-icons.mjs
 */
import sharp from 'sharp'
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const svgPath = path.join(__dirname, '../public/favicon.svg')
const publicDir = path.join(__dirname, '../public')

const svgBuffer = fs.readFileSync(svgPath)
const BG = { r: 10, g: 10, b: 10, alpha: 1 } // #0a0a0a，与 theme_color 一致

async function genIcon(size, file, scale = 0.85) {
  const inner = Math.round(size * scale)
  const offset = Math.round((size - inner) / 2)
  const resized = await sharp(svgBuffer).resize(inner, inner).toBuffer()
  await sharp({
    create: { width: size, height: size, channels: 4, background: BG },
  })
    .composite([{ input: resized, top: offset, left: offset }])
    .png()
    .toFile(path.join(publicDir, file))
  console.log(`✓ ${file} (${size}x${size}, scale=${scale})`)
}

// 普通图标: SVG 占 85%，留少量边距
await genIcon(192, 'pwa-192.png', 0.85)
await genIcon(512, 'pwa-512.png', 0.85)
// maskable: SVG 占 70%，留出 safe zone（系统裁剪区域）
await genIcon(512, 'pwa-maskable-512.png', 0.7)
console.log('Done.')
