/**
 * 特性开关前端镜像（后端 GET /system/feature-flags）。
 *
 * 用于导航层「提前隐藏」后端未启用的功能入口，避免点进去 404/403；
 * require_perm 仍是真相之源，这里不构成安全边界。
 *
 * 降级语义：flags 未加载/加载失败时保持 undefined —— 菜单侧一律用
 * `flag !== false` 判断（undefined=默认显示，false=隐藏），镜像仓库
 * "degrade, never crash" 的铁律：开关镜像挂了只退化为现状，不能把菜单全藏了。
 */
import { defineStore } from 'pinia'

export const useFeatureFlagsStore = defineStore('featureFlags', {
  state: () => ({ flags: {}, loaded: false }),
  actions: {
    async load() {
      if (this.loaded) return
      try {
        const { default: request } = await import('../api/request')
        const res = await request.get('/system/feature-flags')
        this.flags = res.data || {}
      } catch (e) {
        // 拉不到就保持空对象：undefined !== false，菜单维持全显现状
      } finally {
        this.loaded = true
      }
    },
  },
})
