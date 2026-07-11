<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import SidebarNav from './components/SidebarNav.vue'
import WeatherWidget from './components/WeatherWidget.vue'

const route = useRoute()
const isLanding = computed(() => route.name === 'Landing')
// 过渡页（landing/loading/login/setup）不挂载侧栏与天气组件，
// 避免它们在认证确认前就发起 /api/* 请求造成 401 风暴
const CHROME_HIDDEN_ROUTES = ['Landing', 'Loading', 'Login', 'Setup']
const showChrome = computed(() => !CHROME_HIDDEN_ROUTES.includes(route.name))
const hideWeather = computed(() => !showChrome.value || route.name === 'KGraph')

const saved = localStorage.getItem('aether-theme')
if (saved === 'light') {
  document.documentElement.classList.add('light-mode')
}
</script>

<template>
  <div class="app-layout" :class="{ landing: isLanding }">
    <SidebarNav v-if="showChrome" />
    <main class="main-content">
      <WeatherWidget v-if="!hideWeather" />
      <router-view />
    </main>
  </div>
</template>

<style>
@property --g1 {
  syntax: '<color>';
  inherits: false;
  initial-value: rgba(45, 90, 78, 0.2);
}
@property --g2 {
  syntax: '<color>';
  inherits: false;
  initial-value: rgba(74, 124, 112, 0.15);
}
@property --g3 {
  syntax: '<color>';
  inherits: false;
  initial-value: rgba(30, 60, 110, 0.2);
}
@property --g4 {
  syntax: '<color>';
  inherits: false;
  initial-value: rgba(232, 168, 124, 0.25);
}
@property --g5 {
  syntax: '<color>';
  inherits: false;
  initial-value: rgba(183, 128, 176, 0.2);
}
.app-layout {
  display: flex;
  min-height: 100vh;
  background: var(--color-bg);
}

.main-content {
  flex: 1;
  margin-left: var(--sidebar-width);
  min-height: 100vh;
  position: relative;
  overflow: hidden;
  transform: translateZ(0);
  background: var(--color-bg-app);
}

.main-content::before {
  content: '';
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  will-change: background-position;
  background:
    radial-gradient(ellipse 50% 40% at 20% 25%, var(--g1) 0%, transparent 45%),
    radial-gradient(ellipse 40% 35% at 75% 20%, var(--g2) 0%, transparent 40%),
    radial-gradient(ellipse 50% 40% at 40% 75%, var(--g3) 0%, transparent 40%),
    radial-gradient(ellipse 45% 40% at 55% 45%, var(--g4) 0%, transparent 40%),
    radial-gradient(ellipse 40% 35% at 35% 55%, var(--g5) 0%, transparent 35%);
  background-size: 300% 300%, 300% 300%, 300% 300%, 300% 300%, 300% 300%;
  animation: bgFlow 20s cubic-bezier(0.45, 0, 0.55, 1) infinite;
}

.main-content::after {
  content: '';
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  opacity: 0;
  background:
    radial-gradient(circle at 50% 50%, rgba(255, 200, 100, 0.25) 0%, rgba(255, 180, 80, 0.12) 20%, rgba(255, 160, 60, 0.04) 40%, transparent 55%);
  background-size: 250% 250%;
  will-change: background-position;
}

.light-mode .main-content::after {
  opacity: 1;
  animation: sunOrbit 24s cubic-bezier(0.45, 0, 0.55, 1) infinite;
}

@keyframes sunOrbit {
  0%   { background-position: 10% 10%; }
  25%  { background-position: 85% 15%; }
  50%  { background-position: 75% 85%; }
  75%  { background-position: 15% 75%; }
  100% { background-position: 10% 10%; }
}

@keyframes bgFlow {
  0% {
    --g1: rgba(45, 90, 78, 0.25);
    --g2: rgba(74, 124, 112, 0.2);
    --g3: rgba(30, 60, 110, 0.2);
    --g4: rgba(232, 168, 124, 0.25);
    --g5: rgba(183, 128, 176, 0.25);
    background-position: 10% 20%, 80% 25%, 35% 80%, 50% 40%, 30% 60%;
  }
  25% {
    --g1: rgba(183, 128, 176, 0.2);
    --g2: rgba(45, 90, 78, 0.25);
    --g3: rgba(74, 124, 112, 0.2);
    --g4: rgba(30, 60, 110, 0.25);
    --g5: rgba(232, 168, 124, 0.2);
    background-position: 30% 35%, 60% 45%, 55% 60%, 35% 55%, 45% 40%;
  }
  50% {
    --g1: rgba(232, 168, 124, 0.2);
    --g2: rgba(183, 128, 176, 0.2);
    --g3: rgba(45, 90, 78, 0.25);
    --g4: rgba(74, 124, 112, 0.2);
    --g5: rgba(30, 60, 110, 0.25);
    background-position: 20% 15%, 45% 55%, 70% 45%, 60% 35%, 20% 50%;
  }
  75% {
    --g1: rgba(30, 60, 110, 0.25);
    --g2: rgba(232, 168, 124, 0.2);
    --g3: rgba(183, 128, 176, 0.2);
    --g4: rgba(45, 90, 78, 0.25);
    --g5: rgba(74, 124, 112, 0.2);
    background-position: 40% 30%, 55% 15%, 25% 70%, 25% 25%, 55% 50%;
  }
  100% {
    --g1: rgba(45, 90, 78, 0.25);
    --g2: rgba(74, 124, 112, 0.2);
    --g3: rgba(30, 60, 110, 0.2);
    --g4: rgba(232, 168, 124, 0.25);
    --g5: rgba(183, 128, 176, 0.25);
    background-position: 10% 20%, 80% 25%, 35% 80%, 50% 40%, 30% 60%;
  }
}

.main-content > * {
  position: relative;
  z-index: 1;
}

.light-mode .main-content::before {
  background:
    radial-gradient(ellipse 50% 40% at 20% 25%, rgba(45, 90, 78, 0.04) 0%, transparent 45%),
    radial-gradient(ellipse 40% 35% at 75% 20%, rgba(74, 124, 112, 0.03) 0%, transparent 40%),
    radial-gradient(ellipse 50% 40% at 40% 75%, rgba(30, 60, 110, 0.04) 0%, transparent 40%),
    radial-gradient(ellipse 45% 40% at 55% 45%, rgba(232, 168, 124, 0.06) 0%, transparent 40%),
    radial-gradient(ellipse 40% 35% at 35% 55%, rgba(183, 128, 176, 0.04) 0%, transparent 35%);
  background-size: 300% 300%;
  animation: bgFlowLight 25s cubic-bezier(0.45, 0, 0.55, 1) infinite;
}

@keyframes bgFlowLight {
  0%, 100% {
    background-position: 10% 20%, 80% 25%, 35% 80%, 50% 40%, 30% 60%;
  }
  33% {
    background-position: 30% 35%, 60% 45%, 55% 60%, 35% 55%, 45% 40%;
  }
  66% {
    background-position: 20% 15%, 45% 55%, 70% 45%, 60% 35%, 20% 50%;
  }
}

.app-layout.landing {
  background: var(--color-bg);
}

.app-layout.landing .main-content {
  margin-left: 0;
  background: var(--color-bg);
}

.app-layout.landing .main-content::before,
.app-layout.landing .main-content::after {
  display: none;
}

@media (max-width: 768px) {
  .main-content {
    margin-left: var(--sidebar-width-collapsed);
  }
}
</style>
