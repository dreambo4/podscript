
## 執行紀錄

### 1. 納入版控前的敏感內容清查

`git init` 後先以 `git status --porcelain -uall` 列出實際會被納管的檔案
（比讀 `.gitignore` 推論可靠），再逐項檢查內容，而非只看檔名。

初次掃描 100 個檔案，以正則搜尋所有待納管檔案中的連線字串、JWT、
私鑰、API key 等樣式，確認 `.env.example` 內全為空值範本、無真實密鑰。

使用者另外判斷排除四類（最終 61 個檔案）：

| 排除項 | 原因 |
|---|---|
| `full_transcript.md`／`sample_transcript.md` | 他人 Podcast 著作內容，且僅為測試產物（使用者自行刪除） |
| `.agents/skills/`、`.claude/skills/` | 第三方 skill 副本，可依 `skills-lock.json` 重裝 |
| `.mcp.json` | 含 Supabase `project_ref`，使用者不希望暴露專案識別 |

**踩到的坑：** `.mcp.json` 加進 `.gitignore` 後仍出現在清單中——
`.gitignore` 對**已進索引**的檔案無效，需 `git rm --cached` 移出索引。
最初的檢查指令用 `grep "\.mcp\.json$"` 也誤判，因為 `.mcp.example.json`
的結尾同樣符合該樣式，改用 `git check-ignore -v` 才確認到真正的規則命中。

### 2. 新增的範本檔

- `.mcp.example.json`：說明 `project_ref` 的取得路徑與格式
- 兩份 `.env.example`：補上 `DATABASE_URL` 須使用 **Session pooler**
  （Direct connection 的 `db.<ref>.supabase.co` 為 IPv6-only，一般環境解析不到），
  以及 `ALLOWED_ORIGINS` 設錯時的症狀（後端收得到請求，前端收不到回應）

### 3. Railway 後端部署

平台由使用者定案 Railway（我原建議 Render，理由為免綁卡）。
採 **Railway 原生 GitHub 整合**而非 GitHub Actions：Railway 監聽 push 後
自動 build，不需維護設定檔與 token。

**踩到的坑：** 首次部署失敗，Railpack 回報
「could not determine how to build the app」。原因是 **Root Directory 未設定**，
Railway 掃描 repo 根目錄而該處沒有 `requirements.txt`。設為 `mobile-backend` 後正常。

**預先處理的坑：** 新增 `mobile-backend/.python-version` 釘住 3.12。
Nixpacks 未指定時不保證版本，而 `app/` 多處使用 3.10+ 才支援的 `str | None`，
版本過舊會在 build 階段 SyntaxError（此坑於 20260917 devlog 已記載過一次）。

Public Networking 產生網域時需填 port，填 `8080`——Railway 注入的 `$PORT`
預設即為 8080，與 Procfile 的 `--port $PORT` 相符。

### 4. Firebase Hosting 前端部署

`.firebaserc` 綁定 `podscript-bc3d5`，hosting target `web` 對應 site `podscript`
（該專案有 `podscript` 與 `podscript-bc3d5` 兩個 site，不指定 target 會無法判斷）。

前端改由 GitHub Actions 自動部署，`config.js` 不進版控、於 CI 從 Secrets 產生，
並在部署前驗證值存在，避免帶著空的 API 位址上線。

**踩到的坑：** `firebase init hosting:github` 失敗，錯誤為
`Service account ... does not exist`（HTTP 404）——CLI 嘗試為服務帳號產生金鑰，
但帳號本身未被成功建立。改為在 Firebase Console 手動產生私密金鑰，
以 `gh secret set` 寫入後 `rm -P` 刪除本機檔案。
該指令在產生金鑰前就中斷，既有 workflow 未被覆寫。

## 驗證結果（2026-09-23）

| 項目 | 結果 |
|---|---|
| 後端健康檢查 | `{"status":"ok"}` HTTP 200 |
| API 鑑權 | 未帶 token 回 401，DB 連線與 JWT 驗證皆正常 |
| CORS | `access-control-allow-origin: https://podscript.web.app` |
| 前端首頁 | HTTP 200 |
| 線上 `config.js` | 確認為 CI 產生（無本機版註解），證明部署鏈路運作 |
| `sw.js` 快取標頭 | `cache-control: no-cache` |
| Actions workflow | 全步驟通過 |

未驗證：Google 登入實際流程、集數列表與待處理佇列的端到端操作，
需由使用者在瀏覽器實測。

## 異動摘要

- 專案納入版控並推送至 `github.com/dreambo4/podscript`（private）
- 新增 `.firebaserc`、`.mcp.example.json`、`mobile-backend/.python-version`
- `.gitignore` 增加 `.mcp.json` 與第三方 skill 目錄
- 兩份 `.env.example` 補上連線字串格式與 CORS 說明
- 前端 GitHub Actions 自動部署 workflow（另一 session 撰寫，於本次設定 Secrets 並驗證）

## 影響範圍

- **服務**：後端 `podscript-production-9d53.up.railway.app`、
  前端 `podscript.web.app`，兩者皆首次上線
- **部署流程**：push 到 `main` 後，異動 `mobile-backend/` 觸發 Railway 重建，
  異動 `mobile-web/` 觸發 Actions 部署 Firebase

  > **2026-09-23 更正**：上面這句在當時是錯的。Railway 起初**沒有**路徑過濾，
  > 任何 push 到 `main` 都會重建後端。
  >
  > 誤判來源：把 **Root Directory** 當成觸發過濾。實際上兩者是分開的設定 ——
  > Root Directory 只決定 build 的工作目錄（去哪找 `requirements.txt`），
  > 決定「哪些異動才觸發」的是 **Watch Paths**，而當時未設。
  >
  > 發現經過：favicon commit（`37d5ed9`）完全沒碰 `mobile-backend/`，
  > Railway 仍照樣部署，由使用者從 Railway 後台截圖發現。
  > 當時 Claude 只查 `gh run list` 就斷言「Railway 沒動」——
  > 但 Railway 走自己的 GitHub 整合，根本不會出現在 Actions 清單裡，
  > 等於拿看不到的東西當作沒發生。
  >
  > 已修正：Watch Paths 設為 `/mobile-backend/**`，並新增 repo 根目錄的
  > `railway.json` 讓此設定可版控（見下方「Railway 設定可版控化」）。
- **本機開發**：`mobile-web/config.js` 現指向 Railway，本機測試需改回
  `http://127.0.0.1:8010/api`
- **git 身分**：本專案設 local 身分為個人 noreply 信箱，不影響其他專案

## 建議 commit 訊息

本次異動已分三次 commit 完成並推送：
`170dabe` init、`0c48357` feat 待處理佇列與 PWA、`78112d5` ci 前端自動部署。
本 devlog 可併入下次 commit。

---

## 追記（2026-09-23）：Railway 設定可版控化

### 問題

Railway 的 Root Directory 與 Watch Paths 都是**後台設定，不在 repo 裡**。
造成：

- `git clone` 下來看不到實際觸發條件
- 有人在後台改動，code review 看不出來
- Claude 判斷部署行為時無從查證（本次誤判的根因）

### 處置

新增 repo 根目錄的 `railway.json`：

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "build": {
    "watchPatterns": ["/mobile-backend/**"]
  }
}
```

### 三個查證過的重點（來自官方文件，非推測）

1. **watchPatterns 從 repo 根目錄起算，不隨 Root Directory 位移。**
   官方原文：「if a Root Directory is provided, patterns still operate from `/`」。
   故路徑寫 `/mobile-backend/**` 而非 `/**`。語法為 gitignore 風格。

2. **config-as-code 覆寫後台，但只覆寫「有寫的欄位」。**
   未列出的欄位仍沿用後台值，不會被重設為預設。
   因此本檔**刻意只寫 `watchPatterns`** —— 不寫 `startCommand`、
   `healthcheckPath` 等，避免覆蓋掉後台已調好且運作正常的設定。

3. **`rootDirectory` 不在 railway.json 的 schema 內，無法版控。**
   Root Directory（`mobile-backend`）仍只能留在後台。
   且**設定檔路徑本身不跟隨 Root Directory** —— 放在 repo 根目錄
   才會被自動偵測；若放進 `mobile-backend/`，還需另在後台指定絕對路徑，
   反而多一層後台依賴，違背此次版控化的目的。

### 待驗證

`railway.json` 生效與否，需下次 push 觀察：
- 只動 `.claude/**` 或 `web/**` → Railway **不應**部署
- 動 `mobile-backend/**` → Railway 應部署

### 參考

- https://docs.railway.com/reference/config-as-code
- https://docs.railway.com/guides/build-configuration
- https://docs.railway.com/guides/deploying-a-monorepo
