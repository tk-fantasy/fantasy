<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const showContent = ref(false)
const isTransitioning = ref(false)
const videoRef = ref(null)

onMounted(() => {
  setTimeout(() => {
    showContent.value = true
  }, 400)
})

function onVideoLoaded(e) {
  e.target.playbackRate = 0.5
}

function enterApp() {
  isTransitioning.value = true
  setTimeout(() => {
    router.push('/loading')
  }, 800)
}
</script>

<template>
  <div class="landing">
    <div class="video-wrapper">
      <video
        ref="videoRef"
        class="bg-video"
        autoplay
        muted
        loop
        playsinline
        @loadedmetadata="onVideoLoaded"
      >
        <source src="/1_nowatermark_120fps.mp4" type="video/mp4" />
      </video>
      <div class="video-overlay"></div>
      <div class="video-gradient"></div>
    </div>

    <div class="content" :class="{ visible: showContent, exit: isTransitioning }">
      <div class="brand">
        <div class="brand-dot"></div>
        <div class="brand-line"></div>
      </div>

      <h1 class="title">
        <span class="title-sub">Aether Home</span>
        <span class="title-main">智能家居<br>重新定义</span>
      </h1>

      <p class="desc">
        以 AI 为核，融科技于生活。从灯光到安防，从温度到能耗，<br />
        家，比你想象的更懂你。
      </p>

      <button class="enter-btn" @click="enterApp">
        <span class="enter-btn-text">点击进入</span>
        <span class="enter-btn-arrow">&rarr;</span>
      </button>

      <div class="hint">
        <span class="hint-line"></span>
        <span class="hint-text">点击按钮开始体验</span>
        <span class="hint-line"></span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.landing {
  position: fixed;
  inset: 0;
  width: 100vw;
  height: 100vh;
  overflow: hidden;
  background: #0a0a0a;
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
}

.video-wrapper {
  position: absolute;
  inset: 0;
}

.bg-video {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.video-overlay {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.2);
}

.video-gradient {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    180deg,
    rgba(0, 0, 0, 0.05) 0%,
    rgba(0, 0, 0, 0.25) 50%,
    rgba(0, 0, 0, 0.5) 100%
  );
}

.content {
  position: relative;
  z-index: 10;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: 40px;
  text-align: center;
  opacity: 0;
  transform: translateY(30px);
  transition: opacity 1s ease, transform 1s ease;
}

.content.visible {
  opacity: 1;
  transform: translateY(0);
}

.content.exit {
  opacity: 0;
  transform: translateY(-30px) scale(0.98);
  transition: opacity 0.8s ease, transform 0.8s ease;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 32px;
}

.brand-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #4a7c70;
  animation: pulse-dot 2s ease-in-out infinite;
}

@keyframes pulse-dot {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(0.8); }
}

.brand-line {
  width: 40px;
  height: 1px;
  background: rgba(255, 255, 255, 0.3);
}

.title {
  margin-bottom: 20px;
}

.title-sub {
  display: block;
  font-size: 14px;
  font-weight: 500;
  letter-spacing: 6px;
  text-transform: uppercase;
  color: rgba(255, 255, 255, 0.5);
  margin-bottom: 12px;
}

.title-main {
  display: block;
  font-size: 56px;
  font-weight: 700;
  letter-spacing: -1.5px;
  line-height: 1.15;
  color: #fff;
  background: linear-gradient(135deg, #ffffff 0%, #c8e6d9 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

.desc {
  font-size: 15px;
  color: rgba(255, 255, 255, 0.55);
  line-height: 1.8;
  max-width: 520px;
  margin-bottom: 48px;
}

.enter-btn {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 16px 40px;
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 50px;
  background: rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  color: #fff;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.4s ease;
  overflow: hidden;
}

.enter-btn::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 50px;
  background: linear-gradient(135deg, #2d5a4e, #4a7c70);
  opacity: 0;
  transition: opacity 0.4s ease;
}

.enter-btn:hover::before {
  opacity: 1;
}

.enter-btn:hover {
  border-color: transparent;
  transform: translateY(-2px);
  box-shadow: 0 12px 32px rgba(45, 90, 78, 0.3);
}

.enter-btn-text,
.enter-btn-arrow {
  position: relative;
  z-index: 1;
}

.enter-btn-arrow {
  font-size: 18px;
  transition: transform 0.3s ease;
}

.enter-btn:hover .enter-btn-arrow {
  transform: translateX(4px);
}

.hint {
  position: absolute;
  bottom: 48px;
  display: flex;
  align-items: center;
  gap: 12px;
}

.hint-text {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.3);
  letter-spacing: 2px;
}

.hint-line {
  width: 24px;
  height: 1px;
  background: rgba(255, 255, 255, 0.15);
}

@media (max-width: 768px) {
  .title-main {
    font-size: 36px;
  }

  .desc {
    font-size: 14px;
    padding: 0 20px;
  }

  .enter-btn {
    padding: 14px 32px;
    font-size: 14px;
  }

  .hint {
    bottom: 32px;
  }
}
</style>
