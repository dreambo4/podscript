# 20260921 後端 Railway 部署與前端 Firebase Hosting 上線

目標：把 `mobile-backend`（唯讀 API）部署到 Railway，`mobile-web`（靜態前端）
部署到 Firebase Hosting `podscript.web.app`，讓手機可實際使用。

承接 `20260917_手機端後端骨架與Railway部署規劃.md`。

## 部署前現況

- Firebase 專案 `podscript-bc3d5` 已建立，site `podscript`（https://podscript.web.app）已存在
- `firebase.json` 已設定 `public: "mobile-web"`，但**缺 `.firebaserc`**（未綁 project）
- 專案**尚未納入版控**（非 git repo）
- `mobile-web/config.js` 指向 `http://127.0.0.1:8010/api`（本機位址）
- Railway CLI 未安裝

## 使用者定案

| 項目 | 決定 |
|---|---|
| 後端平台 | Railway（沿用 devlog 原規劃，非我建議的 Render） |
| 版控 | `git init` + 推到個人 GitHub private repo |
| 順序 | 先部署後端，取得網址後再改 `config.js` 部署前端 |

## 執行紀錄

（作業中持續更新）
