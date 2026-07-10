#!/usr/bin/env node
/**
 * 从 favicon.svg 生成 PWA 图标 + favicon.ico（透明背景）。
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
const BG_T = { r: 0, g: 0, b: 0, alpha: 0 } // 透明

async function genTransparent(size, file, scale) {
  const inner = Math.round(size * scale)
  const off = Math.round((size - inner) / 2)
  const resized = await sharp(svgBuffer).resize(inner, inner).toBuffer()
  await sharp({ create: { width: size, height: size, channels: 4, background: BG_T } })
    .composite([{ input: resized, top: off, left: off }])
    .png()
    .toFile(path.join(publicDir, file))
  console.log(`✓ ${file} (${size}x${size}, transparent)`)
}

async function genMaskable(size, file, scale) {
  // maskable 按规范需要不透明背景（系统会裁剪 safe zone）
  const inner = Math.round(size * scale)
  const off = Math.round((size - inner) / 2)
  const resized = await sharp(svgBuffer).resize(inner, inner).toBuffer()
  await sharp({ create: { width: size, height: size, channels: 4, background: { r: 10, g: 10, b: 10, alpha: 1 } } })
    .composite([{ input: resized, top: off, left: off }])
    .png()
    .toFile(path.join(publicDir, file))
  console.log(`✓ ${file} (${size}x${size}, dark bg for maskable)`)
}

async function genIco() {
  const sizes = [256, 128, 64, 48, 32, 16]
  const pngs = []
  for (const s of sizes) {
    const inner = Math.round(s * 0.85)
    const off = Math.round((s - inner) / 2)
    const resized = await sharp(svgBuffer).resize(inner, inner).toBuffer()
    const buf = await sharp({ create: { width: s, height: s, channels: 4, background: BG_T } })
      .composite([{ input: resized, top: off, left: off }])
      .png()
      .toBuffer()
    pngs.push({ size: s, data: buf })
  }
  const n = pngs.length, hs = 6, es = 16, ob = hs + es * n
  let off = ob
  const entries = []
  for (const p of pngs) { entries.push({ ...p, offset: off }); off += p.data.length }
  const buf = Buffer.alloc(off)
  buf.writeUInt16LE(0, 0); buf.writeUInt16LE(1, 2); buf.writeUInt16LE(n, 4)
  entries.forEach((e, i) => {
    const b = hs + i * es
    const w = e.size === 256 ? 0 : e.size
    buf.writeUInt8(w, b); buf.writeUInt8(w, b + 1)
    buf.writeUInt8(0, b + 2); buf.writeUInt8(0, b + 3)
    buf.writeUInt16LE(1, b + 4); buf.writeUInt16LE(32, b + 6)
    buf.writeUInt32LE(e.data.length, b + 8); buf.writeUInt32LE(e.offset, b + 12)
  })
  entries.forEach(e => e.data.copy(buf, e.offset))
  fs.writeFileSync(path.join(publicDir, 'favicon.ico'), buf)
  console.log(`✓ favicon.ico (${buf.length} bytes, transparent)`)
}

await genTransparent(192, 'pwa-192.png', 0.85)
await genTransparent(512, 'pwa-512.png', 0.85)
await genMaskable(512, 'pwa-maskable-512.png', 0.7)
await genIco()
console.log('Done.')
