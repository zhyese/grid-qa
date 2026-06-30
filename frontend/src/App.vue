<template>
  <router-view />
</template>

<script setup>
import { useDark } from '@vueuse/core'
// 初始化主题（读 localStorage / 跟随系统偏好，应用 html.dark）；切换按钮在 AppLayout 侧栏
useDark()
</script>
