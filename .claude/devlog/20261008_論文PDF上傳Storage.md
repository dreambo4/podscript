# 論文 PDF 上傳 Storage

日期：2026-10-08

## 背景

待辦第 8 項「論文（PDF 上傳）」的第一步。使用者有兩台 Mac，論文 PDF 要能跨電腦取得；
已定案原檔放 Supabase Storage（見 `.claude/todo.md` 第 8 項）。
這次只做原檔的上傳、列出、下載，抽文字與摘要尚未開始。

## 做法

- 使用者建立 `papers` bucket（不公開、單檔 50 MB、只收 `application/pdf`），未設 RLS policy
- `.env` 新增 `SUPABASE_URL`、`SUPABASE_SECRET_KEY`：Storage 無法用 `DATABASE_URL` 直連存取，需走 Storage API；
  secret key 不受 RLS 限制，只放本機
- `src/podscript/storage.py`：
  - guid 為 PDF 內容的 sha256 前 16 碼，前綴 `paper-`；物件名稱 `<guid>.pdf`，重複上傳以 `x-upsert` 覆蓋
  - 原始檔名與下載來源以 `x-metadata`（base64 JSON）存進物件的 user metadata
  - 新版 `sb_secret_` 金鑰不是 JWT，只放 `apikey` header；舊版 service_role 另帶 `Authorization`
  - `list` API 不回傳 user metadata，列出時逐筆呼叫 `object/info` 取得
- `scripts/paper.py`：`upload`／`list`／`download` 三個子指令；下載預設存到 `papers/`（已加入 `.gitignore`）
- 用 httpx 直接呼叫 Storage API，venv 已有，未安裝新套件

## 驗證

- 上傳 `1743-7075-6-23.pdf`（Springer，1.5 MB）→ `paper-33d87255338f338b`
- SQL 查 `storage.objects`：`user_metadata` 有 `filename`、`source_url`
- `list` 顯示 guid、大小、檔名、來源
- `download` 下載後與原檔 `cmp` 一致
- 同一份 PDF 再上傳一次，guid 相同、物件仍只有一筆

## 異動摘要

- 新增 `src/podscript/storage.py`、`scripts/paper.py`
- `.env.example` 新增 Storage 區段；`.gitignore` 新增 `papers/`
- `.claude/todo.md` 新增第 8 項「論文」、第 9 項「研究專案」與已定案事項

## 影響範圍

- 本機：新增模組與腳本，不影響現有轉錄、文章、上傳流程；server 未引用 `storage.py`
- 手機：無
- 資料庫：無 DDL；Storage 新增 `papers` bucket（使用者建立）

## 建議 commit 訊息

```
feat: 論文 PDF 原檔上傳至 Supabase Storage，提供上傳、列出、下載指令
```
