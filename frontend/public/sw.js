/* PWA 基础 Service Worker：静态资源 cache-first、/api 永不缓存、index 网络优先。
   版本号变更（SW_CACHE_v2…）即触发重新缓存与旧缓存清理。 */
const CACHE = 'grid-qa-static-v1'
const PRECACHE = ['/', '/index.html', '/manifest.webmanifest', '/icon.svg']

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()))
})

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url)
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api')) return  // API/写请求直连
  // hash 资产 immutable：cache-first；入口 HTML：network-first 兜底缓存
  const isAsset = url.pathname.startsWith('/assets/')
  e.respondWith(
    caches.match(e.request).then((hit) => {
      if (hit && isAsset) return hit
      return fetch(e.request).then((resp) => {
        if (resp.ok && (isAsset || url.pathname === '/' || url.pathname === '/index.html')) {
          const clone = resp.clone()
          caches.open(CACHE).then((c) => c.put(e.request, clone))
        }
        return resp
      }).catch(() => hit || caches.match('/index.html'))
    })
  )
})
