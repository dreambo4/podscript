# podscript

Podcast 逐字稿工具：貼上 Apple Podcast 單集或 YouTube 影片網址，產生**區分說話者、帶時間戳的逐字稿**、
內容摘要與心智圖。新聞或文章網址（或直接貼上全文）則跳過轉錄，只產生摘要、心智圖與標籤；論文 PDF 也可以上傳。本機網頁檢視與下載，結果可手動上傳 Supabase，供手機搜尋瀏覽。

## 架構

**重運算留本機，輕量結果上雲。** 兩套服務不互相 import，唯一交集是 Supabase。

![podscript 架構圖](docs/architecture.svg)

本機處理流程：解析網址（iTunes Lookup API / RSS）→ 下載 mp3 → whisper.cpp 轉錄（Metal GPU）
→ OpenCC 轉繁體 → pyannote.audio 說話者分離 → 時間軸對齊 → `claude -p` 產生摘要、心智圖、標籤
→ 手動按「上傳」寫入 Supabase。

手機端可把網址加入待處理清單，實際下載與轉錄仍在本機執行。
完整規格見 `.claude/specs/specs_20260916_Podcast逐字稿工具.md`，
含說明卡片的完整版架構圖見 `.claude/architecture/architecture_20260918.html`。

## 需求

- **Apple Silicon Mac**：whisper.cpp 與 pyannote 依賴 Metal 加速。
  **不要用 Docker**，容器在 macOS 取不到 GPU。
- Homebrew、Python 3.12
- [Claude Code CLI](https://claude.com/claude-code)（`claude -p` 產生摘要，使用訂閱額度）
- HuggingFace 帳號（下載 pyannote 模型權重）
- 磁碟空間約 4GB（Python 套件約 2.5GB，轉錄模型 1–3GB）

## 本機安裝

### 1. 系統工具

```bash
brew install whisper-cpp ffmpeg
```

### 2. Python 環境

venv 建在專案根目錄的 `venv/`：

```bash
python3.12 -m venv venv
./venv/bin/pip install pyannote.audio fastapi uvicorn python-dotenv requests \
  opencc-python-reimplemented "psycopg[binary]" "yt-dlp[default]" trafilatura \
  httpx pypdfium2==5.9.0
```

YouTube 下載另需 JavaScript 執行環境（node 或 deno 擇一，本機有就好）。
YouTube 改版常讓舊版 yt-dlp 失效，下載失敗時先執行 `./venv/bin/pip install -U "yt-dlp[default]"`。

### 3. 環境變數

```bash
cp .env.example .env
```

必填 `HUGGINGFACE_TOKEN`：至 <https://huggingface.co/settings/tokens> 建立，
並**先同意**以下兩個模型的條款，否則 token 無效：

- <https://huggingface.co/pyannote/speaker-diarization-3.1>
- <https://huggingface.co/pyannote/segmentation-3.0>

`DATABASE_URL` 留空時僅本機運作，不影響轉錄；要上傳才需要填，
取得方式見 `.env.example` 內的說明（須用 Session pooler）。

Telegram 推播（轉錄完成或失敗時傳訊息到手機）的 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`
存在資料庫 `app_settings` 表，有 `DATABASE_URL` 就會自動讀到，換電腦不必重填；
`.env` 填了同名變數則以 `.env` 為準。取得方式見 `.env.example`。

### 4. 選定轉錄模型

```bash
./venv/bin/python scripts/setup-model.py
```

依這台機器的記憶體選定模型、寫入 `.env` 的 `WHISPER_MODEL`，並只下載該模型。
**每台機器執行一次即可**，換電腦時再執行。詳見下一節。

## 轉錄模型

whisper 模型轉錄時整份載入記憶體，且 Metal 緩衝區無法換出。
選了超出機器負荷的模型，會吃光記憶體讓整台電腦卡死。
機器規格是固定的，所以每台機器固定使用一種模型，在安裝時決定，執行時不再判斷。

| 模型 | 檔案大小 | 記憶體峰值 | 適用 |
|---|---|---|---|
| `large-v2` | 2.9GB | 約 4.0GB | 實體記憶體 12GB 以上 |
| `large-v2-q5_0` | 1.0GB | 約 2.0GB | 實體記憶體 12GB 以下（如 8GB） |

- 兩者都是 large-v2。§9.1 實測 large-v2 的中文辨識比 large-v3 好，故不使用 v3 系列。
- `q5_0` 是量化版。同一段 60 秒音檔比對，差異只有少數語尾助詞（如「啦」），其餘一字不差。
- 記憶體峰值以 `/usr/bin/time -l` 實測 peak memory footprint。

手動指定模型（會覆寫 `.env`，缺檔時自動下載）：

```bash
./venv/bin/python scripts/setup-model.py large-v2
```

新增其他模型時，須先實測記憶體峰值，再填入 `src/podscript/hardware.py` 的 `MODEL_PEAK_GB`。

轉錄的執行緒數固定為效能核心數（如 M1 為 4），不排到節能核心。
說話者分離在獨立子行程執行，結束後記憶體完整歸還，不會與下一集的轉錄疊加。

## 使用

```bash
./run.sh
```

開啟 <http://127.0.0.1:8420>，貼上 Apple Podcast **單集**網址
（形如 `https://podcasts.apple.com/tw/podcast/.../id1856553936?i=1000787805573`，
需含 `?i=` 單集 id），或 YouTube 影片網址（`youtu.be/...`、`youtube.com/watch?v=...` 等皆可，
只下載音軌，不下載影像）。

也可以貼新聞或文章網址：輸入框旁的類型預設「自動」，Apple Podcast 與 YouTube 以外的網址都當成文章，
判斷錯時可手動切換。付費文章、需要登入或抓不到正文時，在最左邊的下拉選單選「貼上全文」直接貼內容。
文章不轉錄、沒有說話者，約 1 到 2 分鐘完成；原文會一併上傳，手機可搜尋內文。

論文在最左邊的下拉選單選「上傳論文」，再選 PDF 檔。全文擷取在本機進行（掃描版 PDF 抽不到文字，不支援），
章節直接用論文原有的章標題，表格逐列保留，參考文獻保留在原文但不送摘要模型。
摘要依序說明研究問題、方法、主要發現與限制，並讀出期刊名稱作為來源。
按「上傳」時 PDF 原檔存到 Supabase Storage 的 `papers` bucket（需 `.env` 的 `SUPABASE_URL`、
`SUPABASE_SECRET_KEY`），兩台電腦都能從結果頁的「開啟 PDF 原檔」取得；手機只看全文與摘要。
英文論文可在結果頁按「翻譯成中文」（模型為 `.env` 的 `TRANSLATE_MODEL`，預設 sonnet，約需數分鐘），
參考文獻不翻；翻譯後原文預設顯示中文、可切回英文，清單在原標題下方顯示中文標題，手機同樣可切換。

研究專案把 Podcast、文章、論文歸入自訂主題：頁首「研究專案」（`/projects`）可新增專案，
並把左側的單集拖進右側的專案格子歸類；單集頁的「研究專案」區塊也能歸類；
開始處理前按「開始」旁的「專案」勾選，處理完成後自動歸入。點專案格子進入專案頁（`/project`）。
專案頁有研究問題（可排序、各自寫筆記）、篇目時間軸、
專案內全文搜尋、標籤相近的「可能相關」內容，以及專案筆記（Markdown）。
「產生 AI 整理」以各篇摘要產生主張對照表、專案心智圖與研究問題的缺口（只整理各篇說了什麼，不下結論），
「AI 建議問題」提出 3 個問題供勾選加入；兩者都在本機以 `claude -p` 產生，會消耗額度。
手機可看專案頁、編輯研究問題與筆記，AI 整理只能在本機產生。

- 一集約 1 小時的節目，處理時間約數十分鐘；網頁會顯示各階段進度，重新整理後可接回。
- 一次只處理一集：同時送出多集時依序排隊，前一集連摘要都完成才開始下一集
  （轉錄與說話者分離會把模型整份載入記憶體，同時跑多集可能讓機器卡住）。
- 各階段的產出存於 `audio/<guid>/`，以「檔案是否存在」判斷是否已完成，
  中斷後重跑會從未完成的階段接續。
- 說話者預設顯示 `SPEAKER_00` 等編號，可在結果頁改名。
- 摘要與心智圖可反覆重新生成，滿意後再手動按「上傳」。
- 服務以 `--reload` 監看 `src/`，改程式會自動重載；改 `.env` 則需重啟。

## 手機端

兩套服務不互相 import，各自部署，唯一交集是 Supabase。

| 部分 | 位置 | 部署 |
|---|---|---|
| 資料表 | `supabase/schema.sql` | 於 Supabase SQL Editor 手動執行 |
| 前端 | `mobile-web/` | push 到 main 後由 GitHub Actions 部署至 Firebase Hosting |
| 後端 | `mobile-backend/` | Railway（`Procfile`，依賴見該目錄的 `requirements.txt`） |

- 前端設定：複製 `mobile-web/config.example.js` 為 `config.js`（不進版控）；
  正式環境由 workflow 以 GitHub Secrets 產生。
- 本機預覽前端：`cd mobile-web && python3 -m http.server 5173 --bind 127.0.0.1`
- 修改前端資源後、部署前執行 `python3 scripts/stamp-assets.py` 更新快取版本號。
- 登入採 Google OAuth，白名單存於 `users` 資料表。

### Telegram 分享加入待處理

在 Podcast App 按分享 → Telegram → 選 bot 的私聊，網址就會加入待處理，bot 回覆結果。
只寫入待處理清單，不觸發轉錄；本機服務不必開著，回家再手動處理。
沿用本機推播的同一個 bot，只接受 `TELEGRAM_CHAT_ID` 那個聊天室的訊息。

| 傳給 bot 的內容 | 加入為 |
|---|---|
| 網址（可夾帶短文字） | 網址項目，網址以外的文字存成備註 |
| 超過 200 字的文字 | 文章全文。超過 4096 字時 Telegram 會拆成幾則送出，15 秒內接著來的會併成同一篇；連續貼兩篇要隔一下 |
| `.txt`／`.md` 檔 | 文章全文（須為 UTF-8） |
| PDF 檔 | 論文，上限 20 MB（Telegram bot 下載上限），更大的請用本機網頁上傳 |

全文與 PDF 暫存在 `queue` 表，本機上傳該篇後隨項目刪除。
需要先執行 `supabase/schema.sql` 最後一段（待處理加入全文與 PDF）再部署後端。

設定（一次即可）：

1. Railway Variables 加上 `TELEGRAM_WEBHOOK_SECRET`（自訂隨機字串，只能用 `A-Z a-z 0-9 _ -`）。
   bot token 與 chat id 會從資料庫 `app_settings` 讀，不必重填。
2. 部署後把 bot 指到 Railway：

   ```bash
   curl -sS "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
     -d url=https://<railway 網域>/api/telegram/webhook \
     -d secret_token=<TELEGRAM_WEBHOOK_SECRET> \
     -d allowed_updates='["message"]'
   ```

   確認：`curl -sS "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"`
3. 在 Telegram 把 bot 的私聊置頂，分享選單才會一直排在前面。

## 目錄結構

```
src/podscript/      本機服務（FastAPI）與處理流程
  resolvers/        平台網址解析（Apple Podcast、YouTube、文章正文擷取、論文 PDF 擷取）
  pipeline.py       串接各階段
  transcribe.py     whisper.cpp 轉錄
  diarize.py        pyannote 說話者分離與對齊
  hardware.py       硬體偵測與模型推薦
  summary.py        claude -p 產生摘要、心智圖、標籤
  upload.py         上傳 Supabase、待處理清單
  projects.py       研究專案、研究問題、專案筆記（資料庫）
  project_ai.py     研究專案的專案內搜尋與 AI 整理
  storage.py        Supabase Storage（論文 PDF 原檔、思辨練習紀錄）
web/                本機網頁（index.html 單集、project.html 研究專案、project-manage.html 專案管理）
mobile-web/         手機前端
mobile-backend/     手機後端
supabase/           資料表定義
docs/               架構圖（architecture.svg）
scripts/            setup-model.py、stamp-assets.py
bruno/              API 測試集（本機與手機後端）
models/             轉錄模型（不進版控）
audio/              各集音檔與中間產物（不進版控）
```
