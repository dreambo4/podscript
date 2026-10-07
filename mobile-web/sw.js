// Service Worker：讓 podscript 可安裝為 PWA，並在離線時仍能開啟外殼。
//
// 只快取靜態資源。API 回應一律不快取：
//   1. 集數、標籤、待處理清單隨時會變，舊資料比沒資料更容易誤導
//   2. 回應內容依 JWT 而異，快取後可能把某人的收藏狀態給到另一個帳號
//
// CACHE_VERSION 由 scripts/stamp-assets.py 依外殼內容的雜湊填入，
// 內容一變版本就變，activate 會清掉舊版快取。
const CACHE_VERSION = "podscript-v0bae07e2c1a2";

// 外殼檔案。config.js 不列入：它含 API 位址與 Google Client ID，
// 部署環境不同，快取住會讓換環境後連到舊後端。
const SHELL = [
  "./",
  "./index.html",
  "./style.css",
  "./app.js",
  "./mindmap-render.js",
  "./manifest.json",
  "./favicon.svg",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) =>
      // 單一檔案失敗不該讓整個安裝失敗，逐一加入並忽略錯誤
      Promise.allSettled(SHELL.map((url) => cache.add(url)))
    )
  );
  // 新版 SW 立即接手，使用者不必關掉 App 再開
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);

  // 跨網域一律直接走網路：API（Railway）、Google 登入、Mermaid CDN
  // 都不該被這裡的快取介入。
  if (url.origin !== self.location.origin) return;

  // 同網域下的 API 路徑（本機開發時前後端同源）同樣不快取
  if (url.pathname.includes("/api/")) return;

  // 導覽請求（開啟 App／重新整理）：優先走網路取得最新版，
  // 離線時退回快取的 index.html，這是 standalone 模式下不變白畫面的關鍵。
  // cache: "no-cache" 要求向伺服器確認，不直接用瀏覽器 HTTP 快取裡的 index.html；
  // index.html 內含各資源的版本號，拿到舊的就會載入整套舊版。
  // navigate 模式的 Request 不能帶 RequestInit，故以網址重新發出。
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request.url, { cache: "no-cache", credentials: "same-origin" })
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_VERSION).then((cache) => cache.put("./index.html", copy));
          return response;
        })
        .catch(() => caches.match("./index.html"))
    );
    return;
  }

  // 靜態資源：快取優先，取不到再走網路並順手存起來
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        // opaque 或錯誤回應不入快取，避免把失敗結果固定下來
        if (response.ok && response.type === "basic") {
          const copy = response.clone();
          caches.open(CACHE_VERSION).then((cache) => cache.put(request, copy));
        }
        return response;
      });
    })
  );
});
