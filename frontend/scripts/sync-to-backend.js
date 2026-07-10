#!/usr/bin/env node
/**
 * 前端构建产物同步到后端静态目录
 * 在 npm run build 后自动执行
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const src = path.join(__dirname, '../dist');
const dest = path.join(__dirname, '../../app/static/frontend');

// 检查源目录是否存在
if (!fs.existsSync(src)) {
  console.error('✗ dist/ 目录不存在，请先运行 npm run build');
  process.exit(1);
}

// 清空目标目录
if (fs.existsSync(dest)) {
  fs.rmSync(dest, { recursive: true, force: true });
}

// 创建目标目录
fs.mkdirSync(path.dirname(dest), { recursive: true });

// 复制 dist 到 app/static/frontend
fs.cpSync(src, dest, { recursive: true });

console.log('✓ Frontend build synced to app/static/frontend/');
