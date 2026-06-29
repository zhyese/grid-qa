<template>
  <router-view />
  <button class="theme-toggle" @click="toggleDark()" :title="isDark ? '切换亮色' : '切换暗色'">
    {{ isDark ? '☀️' : '🌙' }}
  </button>
</template>

<script setup>
import { useDark, useToggle } from '@vueuse/core'

// useDark: 自动管理 html.dark 类 + localStorage 持久化 + 跟随系统偏好
const isDark = useDark()
const toggleDark = useToggle(isDark)
</script>
