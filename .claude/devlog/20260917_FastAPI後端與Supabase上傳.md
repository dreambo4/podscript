# 20260917 FastAPI 後端、本機網頁與 Supabase 上傳

承接 `20260916_環境建置與解析層實作.md`。§9.1／§9.2 實測定案後，
完成本機服務、網頁介面與雲端上傳。**手機端尚未開始，另開 session 進行。**

## 完成項目

| 檔案 | 內容 |
|---|---|
| `src/podscript/summary.py` | 摘要／心智圖／標籤（三項同一次呼叫），含 `SummaryProvider` 抽象層 |
| `src/podscript/upload.py` | Supabase 上傳，Postgres 直連；上傳成功後自動刪音檔 |
| `src/podscript/server.py` | FastAPI，9 個 API 端點 |
| `web/*` | 原生 HTML 前端，側邊欄佈局 |
| `supabase/schema.sql` | 建表 SQL（已由使用者執行） |
| `run.sh` | 啟動腳本，預設 port 8420 |

## 定案的決策

### 1. 本機不使用資料庫

spec §3 原列 SQLite，實作後確認不需要。狀態全在 `audio/<guid>/` 的檔案：
`job.json` 存進度與原始網址，`/api/episodes` 掃描目錄取得清單。
**唯一的資料庫是 Supabase。**

### 2. 上傳走 Postgres 直連，不用 Supabase REST API

與 exercise-together 一致（`DATABASE_URL` + psycopg）。

**踩到的坑：** `p.txt` 提供的 `db.<ref>.supabase.co` 是 **IPv6-only**，
本機 IPv4 網路解析不到（`failed to resolve host`）。
須改用 **Session pooler**：`postgres.<ref>@aws-0-ap-southeast-1.pooler.supabase.com`。

### 3. RLS 非必要

手機端經 FastAPI 存取（spec §6），不會有 anon key 暴露在公開網頁，
故 `SUPABASE_ANON_KEY` 已從 `.env` 移除。

一度誤判為「必須開 RLS」——那是 Supabase 前端直連的典型情境，
但本專案的架構是 `手機 → FastAPI → Postgres`，前提不成立。

### 4. 上傳成功即自動刪除音檔

每集約 230 MB，實測兩集從 426 MB 降到 **7.9 MB**。
刪除發生在資料庫寫入成功之後，上傳失敗則音檔保留可重試。

**代價：** 不能重跑轉錄（mp3 網址帶時效性參數，刪除後無法重新下載）。
使用者已確認不需要「點時間戳回聽原音」功能。

## 過程中修正的問題

| 問題 | 原因 |
|---|---|
| 服務從未載入 `.env` | `server.py` 缺 `load_dotenv()`，所有設定都在用程式預設值 |
| 刷新頁面進度消失 | 進度只存前端遞迴，改為寫入 `job.json` |
| 處理中的單集不在清單 | `/api/episodes` 只認 `transcript.json` |
| 中斷後需重貼網址 | 加 `/api/jobs/{guid}/resume`，沿用 `job.json` 的網址 |
| 逐集查 DB 上傳時間 | 改為 `where episode_guid = any(...)` 一次查完 |
| 心智圖全變灰 | `theme: "neutral"` 讓 68 節點失去層次，改回預設彩色主題 |
| 轉錄無進度回報 | `capture_output=True` 吞掉 stderr，改為逐行讀取解析 `progress = N%` |

## 驗證結果

- 上傳 EP242：1.4 秒、**50 KB**（spec §1 估 80 KB）
- 重複上傳：upsert 正確，不產生重複列
- **中文搜尋**：`pg_trgm` 生效，標題「房仲」、內文「斡旋」「實價登錄」全部命中
- 標籤篩選：`hashtags @> array['房地產']` 正確
- 第二集為**三位說話者**，pyannote 正確分辨

## 異動摘要

新增 `summary.py`、`upload.py`、`server.py`、`web/`、`supabase/schema.sql`、`run.sh`；
`pipeline.py` 補 `summarize()`／`load_result()`／`rename_speakers()`；
`transcribe.py` 改為串流讀取以回報進度。

## 影響範圍

本機服務與網頁介面。spec §3、§4.4、§5.2、§5.3、§7、§8、§11、§12 已同步更新為 v2。

## 建議 commit 訊息

```
feat: 本機服務、網頁介面與 Supabase 上傳

- FastAPI 後端（9 端點）與原生 HTML 前端
- 摘要／心智圖／標籤三項單次呼叫產生
- Postgres 直連上傳，成功後自動清除音檔
- 狀態改存 job.json，支援刷新還原與中斷接續
```
