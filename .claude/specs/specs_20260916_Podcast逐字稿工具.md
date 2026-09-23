# Podcast 逐字稿工具（podscript）需求規格

> **開始時間**：2026-09-16 14:20
> **更新時間**：（需求尚未完成，待使用者通知告一段落時填寫）

---

## 1. 概述

### 目的
貼上一個 Podcast 單集網址，自動產生**區分說話者、帶時間戳的逐字稿**，加上**內容摘要**與**架構心智圖**，在本機網頁檢視與下載；輕量結果可上傳 Supabase，供手機搜尋、瀏覽。

### 架構

```
┌─────────────── 本機 M1 Mac mini（重運算，localhost）──────────────┐
│  Apple Podcast URL                                                │
│    ├─ 1. 解析 → iTunes Lookup API → RSS → mp3 直連               │
│    ├─ 2. 下載 mp3                                                 │
│    ├─ 3. whisper.cpp（Metal GPU）→ 文字 + 時間戳                 │
│    ├─ 4. pyannote.audio → 說話者時段                             │
│    ├─ 5. 時間軸對齊合併 → 分人逐字稿                              │
│    ├─ 6. OpenCC → 強制轉繁體                                     │
│    └─ 7. 本機 claude -p → 摘要 + Mermaid 心智圖                  │
│                  │                                                │
│         web/app.js（瀏覽器） ⇄ src/podscript/server.py            │
│         檢視 / 改說話者名稱 / 重新生成 / 下載                      │
│         （GET /api/jobs/{guid} 每 2 秒輪詢進度 —— 純 localhost     │
│          內部往返，不經過網際網路，與下方 Railway 服務無關）        │
└──────────────────┼─────────────────────────────────────────────────┘
                   │ 手動按「上傳」才送出（約 80 KB/集）
                   ▼
              ┌─────────────────────┐
              │  Supabase Postgres  │  ← 兩套服務唯一的交集
              │  逐字稿·摘要·心智圖  │
              └──────────┬───────────┘
                          │ 唯讀 SELECT（psycopg，同步）
                          ▼
┌──────── 雲端：手機端唯讀查詢（獨立部署）────────┐
│  mobile-web/（Firebase Hosting）                │
│    Google 登入 · 列表搜尋 · 詳細頁              │
│           │ HTTPS                               │
│           ▼                                      │
│  mobile-backend/（Railway）                      │
│    Google id_token 驗證 → JWT                   │
│    /api/auth/google · /api/episodes             │
└───────────────────────────────────────────────────┘
```

**原則：重運算留本機，輕量結果上雲。**
**兩套服務不互相 import、各自獨立部署（各自的 requirements.txt），唯一交集是 Supabase。**
**視覺化版本（含色彩區分、資料流箭頭）：`.claude/architecture/architecture_20260918.html`，用瀏覽器開啟即可檢視**

### 範圍內
- 接收 **Apple Podcast** 單集網址
- 本機轉錄、說話者分離、摘要、心智圖
- 本機網頁：檢視、改說話者名稱、重新生成、下載
- 手動上傳結果至 Supabase
- 手機網頁：搜尋、瀏覽（唯讀）

### 不在範圍內
- 其他 Podcast 平台（Spotify、KKBOX、YouTube…）→ 架構預留擴充點，本階段不實作
- 手機端觸發轉錄
- 音檔上雲（mp3 只留本機）
- 權限分級

---

## 2. 需求來源

使用者口述需求（2026-09-16），四項畫面要求：
1. 本集內容摘要（最多 100 字）
2. 本集架構心智圖
3. 本集逐字稿：區分不同說話者、標出音檔秒數
4. 下載鈕：可勾選項目（摘要／心智圖／逐字稿，預設全勾），格式可選 Markdown / PDF

追加：結果上傳 Supabase，提供手機搜尋瀏覽介面。

---

## 3. 技術棧

| 層 | 選用 | 說明 |
|---|---|---|
| 語音轉文字 | **whisper.cpp** | `brew install whisper-cpp`，Metal GPU 加速 |
| 說話者分離 | **pyannote.audio** | 需 HuggingFace token（見 §7） |
| 繁體轉換 | **OpenCC** | `s2tw` 模式（簡體→台灣標準字形，**不含**用詞轉換） |
| 摘要／心智圖 | **本機 `claude -p`** | 吃訂閱額度。CLI v2.1.273，支援 `-p` / `--model` / `--output-format json` |
| 後端 | **FastAPI** | 本機服務 |
| 前端 | **原生 HTML + Mermaid CDN** | 無建置步驟、無 node_modules |
| 本機儲存 | **檔案系統** | 一集一目錄，見 §4.4。不使用 SQLite |
| 雲端儲存 | **Supabase** Postgres | 免費層 500 MB |
| 手機端託管 | **Firebase Hosting** | 本機已有 `firebase` CLI |
| 手機端登入 | **Google OAuth + DB 白名單** | 見 §6.4 |

**執行方式：原生，不使用 Docker。**
（Docker 容器在 macOS 上取不到 Apple Silicon GPU，會失去 Metal 加速。）

---

## 4. 資料流程規格

### 4.1 URL 解析

流程（已於 2026-09-16 用 EP242 實測成功）：

```
輸入：https://podcasts.apple.com/tw/podcast/...../id1605731163?i=1000789515808
  │
  ├─ 取 podcast id（1605731163）與單集 id（i=1000789515808）
  │
  ├─ GET https://itunes.apple.com/lookup?id=<podcast_id>&entity=podcast
  │     → feedUrl
  │
  └─ GET RSS → 比對單集 → <enclosure url="...mp3">
        實測結果：56 MB、3683 秒（61 分鐘）、支援 range 下載
```

**擴充點：** 解析層做成 `PlatformResolver` 介面，輸入 URL 輸出 `{title, mp3_url, duration, published_at}`。新增平台只實作一個 resolver，不動下游流程。

**注意：** mp3 URL 可能帶時效性參數（Firstory 為 `?v=<timestamp>`），日後可能失效。逐字稿必須存**完整文字**，不可依賴回頭讀音檔。

### 4.2 說話者分離與對齊

```
Whisper 輸出：  [12.3s-18.7s] "我覺得這個價格可以再談"
pyannote 輸出： [10.1s-19.2s] SPEAKER_00
                      │
                      ▼ 時間軸交集比對
結果：          [12.3s] SPEAKER_00: 我覺得這個價格可以再談
```

**pyannote 的限制（需在介面呈現，不可隱藏）：**
1. 只輸出編號（`SPEAKER_00`、`SPEAKER_01`），不知道名字
2. 搶話、疊音、笑聲會分錯 → 每段標信心度，低信心段落淡色標記，提示回聽原音

### 4.3 說話者命名規則

**預設顯示原始編號，使用者改名則顯示自訂名稱。**

```
speakers = {}                          → 顯示 SPEAKER_00 / SPEAKER_01
speakers = {"SPEAKER_00": "主持人"}    → 顯示 主持人 / SPEAKER_01
```

**不做跨集自動辨識說話者。**
pyannote 每次執行的編號不保證一致（本集 `SPEAKER_00` 是主持人，下一集可能是嘉賓）；自動辨識需儲存聲紋特徵做跨集比對，與效益不成比例。

### 4.4 階段性快取

**用「檔案是否存在」判斷階段完成，不引入任務佇列或狀態機。**

```
audio/<episode_guid>/
├── source.mp3        ← 存在則跳過下載
├── audio.wav         ← 16kHz 單聲道，whisper.cpp 與 pyannote 共用
├── whisper.json      ← 存在則跳過轉錄（最耗時的階段）
├── diarize.json      ← 存在則跳過說話者分離
├── transcript.json   ← 合併後的逐字稿與中繼資料
├── transcript.txt    ← 可讀版本（供 claude CLI 讀取與下載）
├── result.json       ← 摘要 + 心智圖 + 標籤（可重複覆寫）
└── job.json          ← 處理進度與原始網址
```

每階段開始前檢查對應檔案，存在就跳過。`process()` 的 `force` 參數可指定從哪個階段起重跑。

**本機不使用資料庫。** 目錄即紀錄，`/api/episodes` 掃描 `audio/` 取得清單；
唯一的資料庫是 Supabase（§7），只存手動上傳的輕量結果。

滿足三項需求：
- 中途失敗重跑時，已完成的轉錄不會重做
- 重新生成摘要／心智圖／標籤只跑最後階段（約 20-30 秒）
- **中斷後可接續**：`job.json` 存有原始網址，不需使用者重貼

### 4.5 摘要與心智圖的產生

**採用本機 `claude -p`**，費用計入既有訂閱額度。

```bash
claude -p "請讀取 transcript.txt，產生：(1) 100 字左右的繁體中文摘要
           (2) Mermaid mindmap 語法的架構心智圖
           (3) 5 個主題標籤（不得為人名）。以 JSON 格式輸出。" \
       --model opus \
       --output-format json
```

**⚠️ 逐字稿不可作為命令列參數傳入** —— 2-3 萬字會超過 `ARG_MAX`。必須寫成 `transcript.txt` 讓 CLI 自行讀取。

**⚠️ JSON 解析需容錯** —— 模型可能在 JSON 前後附加說明文字，需擷取 JSON 區塊而非直接 parse 整個輸出。

**抽象層（保留改用 API 的彈性）：**

```
SummaryProvider（介面）
├── ClaudeCliProvider   ← 本階段使用
└── ClaudeApiProvider   ← 未來可切換，僅改設定值
```

兩者失敗模式不同，需分別處理：

| Provider | 主要失敗模式 |
|---|---|
| CLI | 訂閱額度用盡、CLI 版本更新導致參數變動、回傳夾雜非 JSON 文字 |
| API | HTTP 錯誤碼、rate limit、token 超限 |

### 4.6 Whisper 中文處理（必做，否則輸出品質不可用）

| 問題 | 表現 | 對策 |
|---|---|---|
| **繁簡不穩** | Whisper 只有單一 `zh` 標籤，會隨機輸出簡體或繁簡混雜 | **OpenCC 後處理強制轉繁**（唯一有效手段）<br>⚠️ 實測 whisper-cli 的 `--prompt` 對中文輸出無作用，不可依賴 |
| **幻覺／跳針** | 靜音或雜音段落憑空生成「謝謝觀看」「請訂閱」等字幕常見語，或無限重複同一句 | ① `--max-context 0`（whisper.cpp 無 `condition_on_previous_text`）<br>② `--suppress-nst`<br>③ 後處理偵測異常重複並標記 |
| **中英夾雜** | 台灣華語常見「這個 case 要 confirm」，英文常拼錯或被硬轉中文 | ⚠️ 無有效對策（`--prompt` 實測無作用）；large-v2 表現尚可 |

---

## 5. 畫面規格

### 5.1 本機主畫面

```
┌────────────────────────────────────────────────────────┐
│  podscript                              [⚙ 設定]       │
├────────────────────────────────────────────────────────┤
│  ┌──────────────────────────────────────┐ ┌─────────┐ │
│  │ 貼上 Apple Podcast 單集網址…          │ │  開始   │ │
│  └──────────────────────────────────────┘ └─────────┘ │
├────────────────────────────────────────────────────────┤
│  處理中：EP242 怎麼讓房仲親口說出你要的數字             │
│  ●───────●───────●───────○───────○                     │
│  解析    下載    轉錄    分離    摘要                   │
│  轉錄中… 32%（預估剩餘 6 分鐘）                         │
└────────────────────────────────────────────────────────┘
```

**進度需逐階段顯示** —— 總耗時數分鐘至十餘分鐘，無回饋的等待體驗不可接受。

### 5.2 結果畫面

```
┌────────────────────────────────────────────────────────┐
│ EP242 | 怎麼讓房仲親口說出你要的數字                    │
│ 博音 · 2026-09-14 · 61 分鐘                             │
│                          [⬇ 下載]  [☁ 上傳到 Supabase] │
├────────────────────────────────────────────────────────┤
│ 📝 摘要 · 心智圖 · 標籤              [🔄 重新生成全部]  │
├────────────────────────────────────────────────────────┤
│ 📝 摘要                                                 │
│ （100 字左右）                                          │
│ #房地產 #談判技巧 #買房殺價 #投資理財 #房貸             │
├────────────────────────────────────────────────────────┤
│ 🧠 心智圖      [展開全部] [收合全部]                    │
│ （Mermaid mindmap，可折疊）                             │
├────────────────────────────────────────────────────────┤
│ 💬 逐字稿      說話者： [SPEAKER_00 ✏️] [SPEAKER_01 ✏️] │
│                                                         │
│ [00:12] SPEAKER_00                                      │
│   今天要聊的是房仲談判…                                  │
│                                                         │
│ [00:34] SPEAKER_01                                      │
│   其實關鍵在於你先不要開價…                              │
│                                                         │
│ [01:02] SPEAKER_00  ⚠️（低信心）                        │
│   （淡色顯示，提示回聽原音）                             │
└────────────────────────────────────────────────────────┘
```

### 5.3 重新生成流程

**單一 [🔄 重新生成全部] 按鈕，三項一併重生。**

摘要、心智圖、標籤由同一次 `claude -p` 呼叫產生（見 §4.5）。
分開設按鈕會讓每次點擊都重送整份逐字稿（約 3 萬 token），
三個按鈕即三倍額度，且產出彼此不一致。

```
生成 → 不滿意 → 重新生成全部 → 再看 → 滿意 → 按「上傳到 Supabase」
         ↑______________________|
                （可重複多次）
```

- 重新生成**只跑 §4.4 的最後階段**，不重新轉錄（約 20-30 秒）
- **上傳為手動觸發**，轉錄完成不會自動上傳
- 未上傳的結果留在本機，隨時可再刷

### 5.4 下載對話框

```
┌──────────────────────────────┐
│  下載                         │
│  ☑ 摘要                       │  ← 三項預設全勾
│  ☑ 心智圖                     │
│  ☑ 逐字稿                     │
│  ─────────────────────────    │
│  格式： ⦿ Markdown  ○ PDF     │
│         [取消]  [下載]        │
└──────────────────────────────┘
```

**PDF 產生方式：瀏覽器列印樣式**（`@media print`）。
零依賴、Mermaid 心智圖能正確呈現、中文直接用系統字型。

### 5.5 手機瀏覽介面（唯讀）

2026-09-18 改版，理由：純線性列表無法快速依頻道或時間找到內容。

```
┌──────────────────────┬─┐
│ 🔍 搜尋…               │ │
│ [所有][博音][The Real…]│ │ ← 頻道 chip，橫向滑動
│ 排序: 建檔日期 ▾        │ │
├──────────────────────┤9│ ← 右側 scrubber：
│ ── 2026年9月 ──        │月│   縱向年月標籤，
│ EP242 怎麼讓房仲…      │ │   拖曳快速跳轉
│  博音 · 09-14          │8│   （仿 iOS 相簿）
│ 現代版地下電台…         │月│
│  報導者 · 09-09         │ │
└──────────────────────┴─┘
```

- **頻道篩選**：橫向 chip 列，對應 `podcast_name`，資料來自 `GET /api/channels`
- **排序**：預設「建檔日期」（`created_at`），可切換「發佈日期」（`published_at`），
  分組（年/月 sticky header）依目前排序基準的欄位
- **scrubber**：右側常駐縱向年月標籤，拖曳時捲動列表到對應區塊

點入後顯示摘要／心智圖／逐字稿（版面同 §5.2），**不含**改名、重新生成與上傳功能。

標籤可點選，篩選出同標籤的其他集數（`where hashtags @> array['房地產']`）。

**資料載入方式：一次性全拉，不分頁。**
目前集數少，前端在記憶體中做分組／篩選／排序，讓 scrubber 能正確對應到
「畫面上真實存在」的年月區塊。

**⚠️ 未來優化項目（集數變多後才需要處理，本階段不實作）：**
集數成長到需要分頁時，一次性全拉會變慢，且 scrubber 只能對應「已載入」的
年月會體驗不一致。屆時可改為：後端先回傳「年月統計」（count by year-month），
scrubber 依統計資料渲染，再依使用者捲動/跳轉的位置分段載入該區間的集數。

---

## 6. 手機端登入

**設計：任何人都能點 Google 登入，但登入後必須在 DB 白名單內才放行。**

沿用既有實作（可直接參考）：
- `~/Project/exercise-together/exercise-together-backend/app/routers/auth.py`
- `~/Project/exercise-together/exercise-together-backend/app/auth.py`
- `~/Project/exercise-together/exercise-together-backend/app/dependencies.py`

### 6.1 流程

```
1. 前端 Google 登入 → 取得 id_token
2. 後端 GET https://oauth2.googleapis.com/tokeninfo?id_token=<token>
3. 檢查 aud == 自己的 GOOGLE_CLIENT_ID
4. 取出 sub / email / name
5. 查 DB：先用 google_sub 查 → 查不到再用 email 查
6. 都查不到 → 403「帳號未註冊」        ← 白名單在這裡
7. 首次登入（google_sub 為空）→ 把 sub 寫回該筆資料
8. 簽發自己的 JWT（HS256，30 天）
```

### 6.2 email fallback 的作用（不可省略）

新增使用者時無法事先得知其 Google `sub`（只有登入後才拿得到）：

```
① 在 DB 用 email 先建一筆（email 是已知的）
② 對方首次登入 → email 比對成功 → 同時把 sub 寫回 DB
③ 之後一律用 sub 比對
```

最終以 `sub` 比對的原因：`sub` 是 Google 帳號的不可變唯一 ID，email 可被使用者變更。

### 6.3 白名單資料表

**`users` 表本身就是白名單**，不另建白名單表：

```sql
create table users (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  email       text,                    -- 新增成員時先填這個
  google_sub  text unique,             -- 首次登入後自動寫入
  created_at  timestamptz default now()
);
```

**新增成員：** 在 Supabase 後台 insert 一筆（填 name + email），不需改設定檔或重啟服務。

---

## 7. Supabase 資料結構

```sql
create table episodes (
  id              uuid primary key default gen_random_uuid(),
  platform        text not null default 'apple',   -- 預留多平台
  source_url      text not null,
  episode_guid    text unique not null,            -- 防重複轉錄
  podcast_name    text not null,
  title           text not null,
  published_at    timestamptz,
  duration_sec    int,
  summary         text,
  mindmap_mermaid text,                            -- Mermaid 原始碼
  hashtags        text[] default '{}',             -- 5 個主題標籤，不含人名
  transcript      jsonb not null,
  speakers        jsonb,                           -- {"SPEAKER_00": "主持人"}
  provenance      jsonb,                           -- 各階段的模型與版本，見下方
  created_at      timestamptz default now()
);

create index episodes_guid_idx on episodes (episode_guid);

-- 標籤篩選：where hashtags @> array['房地產']
create index episodes_hashtags_idx on episodes using gin (hashtags);
```

`transcript` 格式：
```json
[
  {"start": 12.3, "end": 18.7, "speaker": "SPEAKER_00",
   "text": "我覺得這個價格可以再談", "confidence": 0.94}
]
```

`hashtags` 格式與規則：
```json
["房地產", "談判技巧", "買房殺價", "投資理財", "房貸"]
```

- 與摘要、心智圖同一次 `claude -p` 呼叫產生
- 固定 5 個，每個 2-6 字，不含 `#` 符號
- **不得為人名**（主持人、來賓、第三人皆不可）—— 以主題、領域、概念為準
- 供手機端依標籤篩選；標籤缺漏不影響摘要與心智圖，不視為錯誤

`provenance` 格式：
```json
{
  "transcribe_model": "large-v2",
  "transcribe_engine": "whisper.cpp 1.9.4",
  "diarize_model": "pyannote/speaker-diarization-3.1",
  "diarize_engine": "pyannote.audio 4.0.7",
  "summary_model": "claude-opus-5",
  "generated_at": "2026-09-17T11:30:00Z"
}
```

**分三個階段記錄，因為它們可各自獨立更換與重跑：**

| 欄位 | 來源 |
|---|---|
| `transcribe_*` | 逐字稿的模型與 whisper.cpp 版本 |
| `diarize_*` | 說話者分離的 pipeline 與 pyannote 版本 |
| `summary_model` | 摘要、心智圖與標籤的模型 ID（三者同一次呼叫產生） |

- **模型 ID 取自 CLI 回傳的 `modelUsage` 鍵**（如 `claude-opus-5`），
  比呼叫時傳入的別名（`opus`）精確，模型改版後仍可回溯是哪一版產生的
- **摘要、心智圖、標籤三項由同一次 `claude -p` 呼叫產生**，
  故共用一個 `summary_model` 欄位；逐字稿只需送一次，最省額度
- `generated_at` 為最近一次生成時間（可重複重新生成）

### ⚠️ 中文全文搜尋

**Postgres 預設的 `to_tsvector` 對中文無效** —— 不會斷詞，整句被當成一個 token，搜尋必定失敗。

**須啟用 `pg_trgm` 做三元組模糊搜尋：**

```sql
create extension if not exists pg_trgm;
create index episodes_title_trgm on episodes using gin (title gin_trgm_ops);
```

逐字稿內文搜尋改用 RPC function 配 `ILIKE`，或對攤平後的純文字欄位建 trgm 索引。

**此項若不處理，手機搜尋功能等於不能用。**

---

## 8. 環境變數（`.env`）

專案需附 `.env.example`（內容同下但留空）。`.env` 已列入 `.gitignore`。

```bash
# ── 本機轉錄（必填）───────────────────────────────
# pyannote 說話者分離模型權重
# 取得：https://huggingface.co/settings/tokens
# 注意：需先在以下兩個頁面同意條款，否則 token 無效：
#   https://huggingface.co/pyannote/speaker-diarization-3.1
#   https://huggingface.co/pyannote/segmentation-3.0
HUGGINGFACE_TOKEN=

# ── 摘要與心智圖 ─────────────────────────────────
SUMMARY_PROVIDER=claude_cli
# §9.2 實測後定案
CLAUDE_CLI_MODEL=opus

# 未來若切換為 API，改 SUMMARY_PROVIDER=claude_api 並填入：
ANTHROPIC_API_KEY=

# ── Supabase（留空則僅本機運作，不影響轉錄）──────
# 上傳走 Postgres 直連；手機端經 FastAPI 存取，不需 anon key
DATABASE_URL=

# ── 手機端登入（白名單存 users 表，不放這裡）─────
GOOGLE_CLIENT_ID=
JWT_SECRET=
JWT_SALT=
```

---

## 9. 實測計畫（動工第一步）

模型選擇不憑規格表，用實際音檔決定。**兩組實測皆由使用者本人判斷品質後定案。**

### 9.1 語音辨識模型對照（large-v2 vs large-v3）

取 EP242 **第 10-15 分鐘**（避開開場音樂，取正常對談段落）跑對照。

| 測項 | 驗證什麼 |
|---|---|
| large-v2 vs large-v3 | 中文準確度、幻覺是否出現、繁簡輸出狀況 |
| pyannote | 兩人對談分離準確度、換手邊界誤差 |
| 實際速度 | M1 + Metal 真實吞吐，用以校準進度條預估 |
| 中英夾雜 | 台灣華語 code-switching 表現 |

**備案：** 若中英夾雜或口音表現不佳，改用 **Breeze-ASR**（聯發創新基地，針對台灣華語微調）。

**產出：** 兩份逐字稿並列對照。

### 9.2 摘要／心智圖模型對照（Haiku vs Sonnet）

兩者都透過 `claude -p` 執行、都吃訂閱額度，因此比較重點是「Sonnet 的品質提升是否值得多花的額度」。

用 §9.1 定案後產生的**同一份完整逐字稿**，分別餵給兩個模型，同一組 prompt。

```bash
claude -p "$(cat prompt.txt)" --model haiku  --output-format json > result_haiku.json
claude -p "$(cat prompt.txt)" --model sonnet --output-format json > result_sonnet.json
```

> prompt 內只放指示與檔案路徑，逐字稿本身讓 CLI 讀檔（見 §4.5 的 ARG_MAX 限制）。

| 測項 | 驗證什麼 |
|---|---|
| **摘要品質** | 100 字內是否抓到本集真正重點，或只是開場幾句的複述 |
| **心智圖結構** | 層次是否反映節目實際脈絡；有無把並列概念錯置為上下層 |
| **Mermaid 語法正確性** | 是否可直接渲染。小模型在結構化語法上較易出錯，為主要風險項 |
| **長輸入穩定度** | 2-3 萬字輸入下是否漏掉後半段（只摘要前段是常見失敗模式） |
| **額度消耗** | 見下方 |
| **耗時** | `duration_api_ms` 欄位 |

#### 額度量測方式

`claude -p` 每次啟動會載入系統提示與工具定義，產生**與輸入長度無關的固定開銷**。
（2026-09-16 實測：輸入僅 10 token 的呼叫，`cache_creation_input_tokens` 即達 17,311，`total_cost_usd` 約 0.038。）

**因此不可只比 `total_cost_usd`，須分開看：**

| 欄位 | 意義 |
|---|---|
| `input_tokens` | 逐字稿本身的實際輸入量 |
| `cache_creation_input_tokens` / `cache_read_input_tokens` | CLI 固定開銷，兩模型都有 |
| `output_tokens`（含 `thinking_tokens`） | 模型實際產出量，**模型間差異主要在此** |
| `total_cost_usd` | 參考值，已被固定開銷稀釋 |

**產出：** 兩份摘要與心智圖並列對照（心智圖需**實際渲染**確認語法無誤），據以決定 `.env` 的 `CLAUDE_CLI_MODEL`。
三項由同一次呼叫產生，共用同一個模型設定。

---

## 10. 安裝清單（使用者已於 2026-09-16 同意）

| 項目 | 來源 | 位置 | 大小 |
|---|---|---|---|
| whisper.cpp | `brew install whisper-cpp`（官方 formula） | Homebrew | ~50 MB |
| 模型 large-v2 + large-v3 | HuggingFace `ggerganov/whisper.cpp`（官方） | `~/Project/podscript/models/` | ~3 GB |
| Python venv | PyPI：`pyannote.audio`、`opencc`、`fastapi`、`uvicorn`、`supabase` | `~/Project/podscript/venv/` | ~2.5 GB |
| **合計** | | | **~6 GB** |

**環境：** M1 Mac mini / 16 GB RAM / 磁碟可用 44 GB。
**隔離：** 全新 venv，不動 `~/Project/markitdown/venv`。

---

## 11. 已確認決策（2026-09-16）

| # | 項目 | 決定 |
|---|---|---|
| 1 | 執行方式 | **原生執行，不用 Docker**（容器取不到 Metal GPU） |
| 2 | 前端技術 | **原生 HTML + Mermaid CDN**，不用 React |
| 3 | mp3 處理 | **保留，不自動刪除**。用途：① 回聽低信心段落 ② 重新生成摘要／心智圖 |
| 4 | 手機介面部署 | **Firebase Hosting** |
| 5 | 手機端登入 | **Google 登入 + DB `users` 表白名單**，含 email fallback（見 §6） |
| 6 | 摘要字數 | **100 字寫在 prompt 即可**，稍微超過可接受，不做硬性截斷或重新生成 |
| 7 | 說話者命名 | **預設顯示 `SPEAKER_00`**，使用者改名則顯示自訂名稱；不做跨集自動辨識 |
| 8 | 中途失敗處理 | **階段性檔案快取**，不引入佇列或狀態機（見 §4.4） |
| 9 | 摘要／心智圖引擎 | **本機 `claude -p`**，吃訂閱額度；保留 `SummaryProvider` 抽象層以便未來換 API |
| 10 | 上傳時機 | **手動觸發**，可反覆重新生成到滿意再上傳 |
| 13 | 摘要生成方式 | **摘要、心智圖、標籤由同一次 `claude -p` 呼叫產生**，單一按鈕一併重生；分開呼叫會重複送入 3 萬 token 的逐字稿 |
| 11 | PDF 產生 | **瀏覽器列印樣式**（`@media print`） |
| 12 | 實作方式 | 使用者另開 session 在專案目錄執行；本 session 僅產出規格 |
| 14 | 手機端後端部署 | **Railway**（獨立子目錄 `mobile-backend/`），與本機重運算服務完全分離，走 Postgres 直連（psycopg，同步，與本機 `upload.py` 一致） |
| 15 | 手機端前端部署 | **Firebase Hosting**（`mobile-web/`），前後端分離故需設定 CORS（`ALLOWED_ORIGINS`） |

## 11.1 尚待確認

| # | 項目 | 說明 |
|---|---|---|
| A | ~~**Whisper 模型版本**~~ | ✅ **已定案：large-v2**（2026-09-17，見 §9.1 devlog） |
| B | ~~**摘要／心智圖模型**~~ | ✅ **已定案：Opus**（2026-09-17，見 §9.2 devlog；Haiku／Sonnet／Opus 三方對照） |
| C | **超長節目的分段處理** | 本規格假設節目在 2 小時內（逐字稿約 5 萬字，context 可容納）。更長節目需設計分段摘要機制，**本階段不實作** |

---

## 12. 驗收標準

### 轉錄品質
- [ ] 貼上 Apple Podcast 網址能正確解析出 mp3
- [ ] 轉錄完成時間在 15 分鐘內（61 分鐘音檔）
- [ ] 逐字稿輸出**全繁體**，無簡體殘留
- [ ] 逐字稿**無幻覺句**（「請訂閱」等）與無限重複
- [ ] 說話者分離正確區分兩位講者
- [ ] 每段顯示秒數時間戳
- [ ] 低信心段落有視覺標記

### 介面與互動
- [ ] 說話者**預設顯示 `SPEAKER_00` / `SPEAKER_01`**
- [ ] 改名後全篇同步顯示自訂名稱
- [ ] 摘要約 100 字
- [ ] 心智圖正確渲染且可折疊展開
- [ ] **選定模型產出的 Mermaid 語法可直接渲染**
- [ ] **摘要涵蓋全集重點，非僅前段內容**
- [ ] **摘要、心智圖、標籤可一併重新生成，且不重跑轉錄**（驗證：耗時 < 1 分鐘）
- [ ] 標籤為 5 個主題詞，**不含人名**
- [ ] 下載鈕三項預設全勾，Markdown 與 PDF 皆可正常輸出
- [ ] PDF 中心智圖未被截斷、中文未亂碼

### 階段性快取
- [ ] 中途中斷後重跑，**已完成的轉錄不會重做**
- [ ] `--force` 參數能強制重跑指定階段

### 上傳與手機端
- [ ] **上傳為手動觸發**，轉錄完成不會自動上傳
- [ ] 按下上傳後結果正確寫入 Supabase
- [ ] 手機能搜尋到集數（**含中文關鍵字搜尋**，驗證 `pg_trgm` 生效）
- [ ] 手機端 Google 登入正常
- [ ] **非白名單的 Google 帳號登入被拒絕**（需用另一個帳號實測）
- [ ] 同一集重複貼上時，偵測並提示已存在

---

## 13. 參考資料

- 實測樣本：`EP242 | 怎麼讓房仲親口說出你要的數字 ft. 陳侯勳 談判大叔`（博音，2026-09-14，61 分鐘，56 MB）
- iTunes Lookup API：`https://itunes.apple.com/lookup?id=<podcast_id>&entity=podcast`
- 登入實作參考：`~/Project/exercise-together/exercise-together-backend/app/`

## 14. 版本紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1 | 2026-09-16 | 初版 |
| v2 | 2026-09-17 | §9.1／§9.2 實測定案（large-v2、Opus）；新增 hashtags 欄位；修正 OpenCC 模式為 `s2tw`；修正 whisper.cpp 參數（`--max-context 0`）；記錄 `--prompt` 實測無效 |
| v3 | 2026-09-17 | 手機端動工：新增 §11 決策 14/15（Railway + Firebase Hosting，前後端分離） |
| v4 | 2026-09-18 | §1 架構圖補上手機端唯讀查詢區塊（mobile-web/mobile-backend/Railway），並附視覺化 Artifact 連結；補充「兩套服務唯一交集是 Supabase」「本機輪詢與 Railway 無關」說明 |
