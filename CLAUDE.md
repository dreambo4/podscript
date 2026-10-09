# podscript

Podcast 逐字稿工具：貼 Apple Podcast 網址 → 分人逐字稿 + 摘要 + 心智圖。

## 動工前必讀
`.claude/specs/specs_20260916_Podcast逐字稿工具.md`

## 核心架構
重運算留本機（Metal GPU 加速），輕量結果手動上傳 Supabase（手機唯讀瀏覽）。

## 關鍵技術陷阱（務必處理）
1. **不要用 Docker** → 容器在 macOS 取不到 Apple Silicon GPU，會失去 Metal 加速
2. **Whisper 中文會吐簡體** → OpenCC `s2tw` 後處理為必做步驟
   （勿改 `s2twp`：其片語表會破壞術語，如實價登錄→實價登入）
3. **Whisper 會幻覺**（「請訂閱」等字幕語）→ 需設 `condition_on_previous_text=False`
4. **Postgres `to_tsvector` 對中文無效** → 中文搜尋須用 `pg_trgm`，否則手機搜尋等於不能用
5. **逐字稿不可當命令列參數傳給 `claude -p`** → 2-3 萬字會爆 ARG_MAX，必須寫檔讓它讀
6. **`claude -p` 回傳可能夾雜非 JSON 文字** → 需擷取 JSON 區塊，不可直接 parse
7. **pyannote 每次執行的 SPEAKER 編號不保證一致** → 不做跨集自動辨識

## 已確認的設計決策（見 spec §11，不要改）
- 前端：原生 HTML + Mermaid CDN，不用 React
- 說話者：預設顯示 `SPEAKER_00`，使用者改名才顯示自訂名稱
- 摘要／心智圖：本機 `claude -p`，保留 `SummaryProvider` 抽象層
- **上傳是手動觸發**，可反覆重新生成到滿意再上傳
- 階段性快取用「檔案存在與否」判斷，不引入佇列或狀態機
- mp3 保留不自動刪
- PDF 用瀏覽器列印樣式

## 思辨練習
**使用者主動提出時才帶思辨練習；轉完一集不要主動詢問或提醒**（2026-10-10 使用者要求）。
流程、六個問題與紀錄格式見 `.claude/思辨練習.md`。Claude 負責提問，不給結論；紀錄存 `notes/<guid>.md`，不進 git（repo 公開），
以 `scripts/notes.py sync` 同步到 Supabase Storage 的 `notes` bucket 跨電腦共用

## 禁止使用 emoji
介面文字、程式碼、註解、文件、commit 訊息一律不用 emoji。
需要圖示時用 SVG（手機端見 `mobile-web/index.html` 的 `<symbol>`）；
→ ☰ 這類箭頭／選單符號不算 emoji，可以用。

## 第一步：兩組實測（spec §9）
1. **§9.1** large-v2 vs large-v3（語音辨識品質）
2. **§9.2** Haiku vs Sonnet（摘要／心智圖品質）

兩組都**由使用者本人判斷品質後定案**。Claude 不自行決定模型，也不自行在驗收清單打勾。

比較 Haiku/Sonnet 額度時注意：`claude -p` 每次啟動有約 1.7 萬 token 的固定 cache
開銷，`total_cost_usd` 會被稀釋 → 要看 `output_tokens`（見 spec §9.2）。

## 環境
需 Apple Silicon Mac（Metal 加速）；機型與記憶體不固定，**不要假設規格**。
- 要知道規格時實際查：`sysctl -n hw.memsize hw.perflevel0.physicalcpu`、`df -h .`
- 轉錄模型每台機器固定一種：換電腦時執行一次 `scripts/setup-model.py`，
  依記憶體選定後寫入 `.env` 的 `WHISPER_MODEL`，只下載該模型；執行時不再判斷
- 選模型邏輯在 `src/podscript/hardware.py`；新增模型時須實測記憶體峰值填入 `MODEL_PEAK_GB`
- whisper 模型整份載入記憶體且無法換出，選錯會吃光記憶體讓整台機器卡死
venv 一律建在**專案根目錄的 `./venv`**（`run.sh` 與 README 都用這個相對路徑），
不寫死絕對路徑，因為專案在各台電腦的位置不同。
只動本專案的 venv，**不要動其他專案的 venv**（如 markitdown）。
安裝清單見 spec §10，使用者已同意（約 6GB）。

## Supabase 分工約定（重要）
**Claude 只能 CRUD 資料，不能 DDL。**

- 可做：`SELECT` / `INSERT` / `UPDATE` / `DELETE`、查 schema、讀 log、除錯
- 不可做：`CREATE TABLE` / `DROP TABLE` / `ALTER TABLE` / `TRUNCATE`、
  建刪 index、改 RLS policy、改 migration —— **一律由使用者本人執行**

需要動結構時，Claude 只**提供 SQL 讓使用者自己貼上執行**，不透過 MCP 下 DDL。

MCP 連線設定在 `.mcp.json`（scope=project），
features 為 `docs,database,development`，**未開 `read_only`**
（開了連 `INSERT` 都會被擋，就無法 CRUD 資料）。
因此此約定是**靠遵守、非技術強制**。

**Why:** 使用者要保留資料表結構的完全掌控權；read_only 是連線層級的全有全無，
無法只擋 DDL 放行 DML，所以改用約定處理。
