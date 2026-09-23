# 20260923 前端 GitHub Actions 自動部署 Firebase Hosting

## 目標
後端已做到 push GitHub → Railway 自動部署。
本次把前端也接上：push `mobile-web/**` 到 main → 自動部署到 `https://podscript.web.app`。

## 前置現況
- GitHub repo：`git@github.com:dreambo4/podscript.git`
- Firebase 專案 `podscript-bc3d5`，兩個 site：
  - `podscript` → https://podscript.web.app（要用這個）
  - `podscript-bc3d5` → https://podscript-bc3d5.web.app（預設 site，不用）
- `firebase.json` 已存在，`.firebaserc` 與 `.github/workflows/` 皆不存在

## 關鍵問題：config.js 被 gitignore
`mobile-web/config.js` 列在 `.gitignore`，CI checkout 後不會有這個檔，
`app.js:3-4` 會 fallback 到 `http://localhost:8000/api`，部署上去的網站直接不可用。

裡面兩個值（API 網址、Google Client ID）前端本來就會暴露，非密鑰
（`config.example.js` 自己也註明）。

**採用方案 B**（使用者決定）：config.js 維持不進版控，由 workflow 從
GitHub Secrets 產生。好處是本機 `127.0.0.1:8010` 的設定與正式值互不干擾。

## 本次異動

### 新增 `.firebaserc`
綁定 default project = `podscript-bc3d5`，並建立 hosting target `web` → site `podscript`。
不用 target 的話 `firebase deploy` 會部到預設 site `podscript-bc3d5`，網址就錯了。

### 修改 `firebase.json`
`hosting` 加上 `"target": "web"`，讓 `.firebaserc` 的 target 綁定生效。
驗證：`firebase deploy --only hosting:web --dry-run` → Hosting URL 正確顯示
`https://podscript.web.app`。

### 新增 `.github/workflows/firebase-hosting-deploy.yml`
- 觸發：push 到 main 且異動落在 `mobile-web/**`、`firebase.json`、`.firebaserc`
  或本 workflow 自身；另保留 `workflow_dispatch` 可手動觸發
- `paths` 過濾避免只改後端也跑前端部署
- `concurrency` 群組 `firebase-hosting-live`，連續 push 時取消前一次
- 步驟：checkout → 由 Secrets 產生 config.js → 檢查 Secrets 是否真的有值
  → `FirebaseExtended/action-hosting-deploy@v0`（`target: web`、`channelId: live`）
- 「檢查 Secrets」這步用 grep 驗 config.js 內容，Secret 忘了設會在此 fail，
  而不是默默部署一個連不到 API 的網站

### 新增 `.github/workflows/firebase-hosting-preview.yml`
PR 觸發，部到 7 天後過期的預覽頻道。
`if` 條件擋掉 fork 來的 PR —— fork 拿不到 Secrets，跑了只會產生壞掉的預覽。

## 待使用者執行（Claude 無法代跑）

1. **補 gh token 的 workflow scope**（現有 scopes 沒有 `workflow`，推 workflow 檔會被 GitHub 擋）
   ```
   gh auth refresh -h github.com -s workflow
   ```

2. **建立 Firebase 部署憑證**（互動式、會開瀏覽器）
   ```
   firebase init hosting:github
   ```
   會自動建 service account 並寫入 GitHub Secret。
   Secret 名稱須為 `FIREBASE_SERVICE_ACCOUNT_PODSCRIPT_BC3D5`（workflow 依此讀取）。
   它詢問「是否覆寫既有 workflow」一律選 **No**，本次的 workflow 已寫好。

3. **設定兩個 Secrets**
   ```
   gh secret set PODSCRIPT_API_BASE        # https://<railway>.up.railway.app/api
   gh secret set PODSCRIPT_GOOGLE_CLIENT_ID
   ```
   Client ID 現值見本機 `mobile-web/config.js`。

4. **確認 Railway 的 `ALLOWED_ORIGINS` 含 `https://podscript.web.app`**
   否則前端呼叫 API 會被 CORS 擋（`mobile-backend/app/main.py:14-26`）。

5. **commit 並 push**（commit 由使用者執行）

## 異動摘要
- 新增：`.firebaserc`、`.github/workflows/firebase-hosting-deploy.yml`、
  `.github/workflows/firebase-hosting-preview.yml`
- 修改：`firebase.json`（hosting 加 `target: web`）
- 未動：`mobile-web/` 下的程式碼（該目錄的既有未提交異動屬前次作業）

## 影響範圍
- 前端部署流程：由手動 `firebase deploy` 改為 push 觸發
- `firebase.json` 加 target 後，本機手動部署指令要改成
  `firebase deploy --only hosting:web`（原本的 `--only hosting` 仍可用）
- 後端不受影響

## 建議 commit 訊息
```
feat: 前端 GitHub Actions 自動部署 Firebase Hosting

- 新增 .firebaserc，綁定 hosting target web → site podscript
- firebase.json 加 target，避免部署到預設 site
- push mobile-web/** 到 main 自動部署 podscript.web.app
- PR 自動建立 7 天期限的預覽頻道
- config.js 不進版控，由 GitHub Secrets 於 CI 產生
```
