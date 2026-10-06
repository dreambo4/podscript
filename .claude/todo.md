# 待辦

2026-09-28 與 Codex 各自發想後討論，由使用者決定取捨。

| # | 項目 | 狀態 | 範圍 | DDL |
|---|---|---|---|---|
| 1 | 說話者名稱建議清單 | 已完成（2026-10-05） | 本機 | 不需要 |
| 2 | 搜尋命中片段與定位 | 已完成（2026-10-05） | 手機 | 不需要 |
| 3 | 章節導覽 | 優先 | 先本機試行，再上手機 | 手機階段需要 |
| 4 | 逐字稿修正 | 做法待討論 | 本機 | 不需要 |
| 5 | Telegram 推播 | 已完成（2026-10-05） | 本機 | 新增 `app_settings` 表 |
| 6 | 文章／新聞摘要 | 已上線（2026-10-06），待使用者驗收 | 本機＋手機 | 不需要 |

共通規則：改到任何端點，同一輪要補 `bruno/` 的 `.bru`；DDL 只提供 SQL 由使用者執行。

---

## 1. 說話者名稱建議清單（已完成）

2026-10-05 完成，使用者驗收通過（commit `dcd9040`）。

同一個節目的講者通常固定。改名時，輸入框可以按往下的箭頭，列出這個節目以前用過的人名；也保留自行輸入。

**現況**
- 改名輸入框在本機結果頁 `web/app.js` `renderSpeakers`，一位說話者一個 `<input>`，`change` 時呼叫 `PUT /api/episodes/{guid}/speakers`
- 手機端沒有改名功能，不在範圍內
- 各集的名稱存在 `episodes.speakers`（jsonb）；尚未上傳的集數存在本機 `transcript.json`

**做法**
- 前端用原生 `<datalist>`：輸入框綁同一個 datalist，Chrome 會顯示下拉箭頭，按往下鍵或點擊就會展開，打字時自動篩選，也能輸入清單外的名字
- 後端在 `GET /api/episodes/{guid}` 的回應加上 `known_speakers: list[str]`
  - 來源：資料庫 `select speakers from episodes where podcast_name = %s`，加上本機目錄中同節目、尚未上傳的集數
  - 以 `podcast_name` 判斷是否同一節目；YouTube 用頻道名
  - 依出現次數由多到少排序，去除重複與空字串，不包含 `SPEAKER_00` 這類預設編號
  - 沒設 `DATABASE_URL` 或查詢失敗時只回傳本機結果，不讓整頁載入失敗
- Bruno：更新 `bruno/本機/單集內容.bru` 的回應說明

**驗收**
- 同一節目第二集開啟時，說話者輸入框的下拉清單有第一集設定過的名字
- 選取清單項目後會存檔，和手打的效果相同
- 不同節目的名字不會混在一起
- 可以輸入清單外的新名字

---

## 2. 搜尋命中片段與定位（手機）（已完成）

2026-10-05 完成並部署（commit `140c008`）。另加搜尋框「大小寫須相符」「全字拼寫須相符」開關；
全字的「字」只算英數與底線，且只在關鍵字頭（尾）是英數時才檢查邊界（中英文常直接相連）。

**現況**
- `mobile-backend/app/routers/episodes.py` `list_episodes`：`q` 以 `ilike` 比對 `title` 與 `transcript_text`，回傳的每一集只有標題等摘要欄位
- 手機列表 `mobile-web/app.js` `loadList` 一次拉 500 筆；詳細頁路由是 `#/ep/{guid}`，有「摘要／心智圖」「逐字稿」兩個分頁

**做法：後端**
- 有 `q` 時，在 SQL 裡算出命中片段，不要把整份 `transcript_text` 拉回 Python（500 集 × 數萬字）
  - 以 `strpos(lower(transcript_text), lower(q))` 找第一個命中位置，`substr` 取前後各約 40 字
  - 另外回傳命中次數 `match_count`（以 `(length(text) - length(replace(lower(text), lower(q), ''))) / length(q)` 計算）
- 回應新增 `snippet: str | null`、`match_count: int`；只命中標題時 `snippet` 為 null
- Bruno：更新 `bruno/手機後端/集數/集數列表.bru`

**做法：前端**
- 列表：標題下方顯示片段，命中字用 `<mark>` 標示（先跳脫 HTML 再加標記），附「N 處」
- 點進去的網址帶關鍵字：`#/ep/{guid}?q=...`
- 詳細頁收到 `q` 時：
  - 自動切到「逐字稿」分頁
  - 標亮所有命中處，捲到第一處
  - 底部浮出一列「第 1／N 處 ↑ ↓ ×」，可以切換上一處、下一處，按 × 取消標亮
- 注意：手機 `renderTranscript` 目前把說話者名稱與段落文字直接放進 `innerHTML`，都沒有跳脫（已有 `escapeHtml` 可用）；加標記時一併改成先跳脫

**驗收**
- 搜尋一個只出現在內文的詞，列表看得到命中句與次數
- 點進去直接在逐字稿分頁，跳到第一處，可以逐處切換
- 關鍵字含 `<`、`&` 等字元時畫面正常、沒有注入
- 不帶 `q` 進詳細頁，行為和現在一樣

---

## 3. 章節導覽

**現況**
- 摘要、心智圖、標籤由同一次 `claude -p` 產生（`src/podscript/summary.py` 的 `PROMPT`），讀的是 `transcript.txt`；每段都有 `[hh:mm:ss] 說話者` 開頭，模型看得到時間
- 「重新生成」會同時重產這三項

**第一階段：本機試行（不需 DDL）**
- 在 `PROMPT` 輸出多加 `chapters: [{"start": "00:12:30", "title": "..."}]`，約 5 到 12 章，不多開一次模型
- `_parse_cli_output` 檢查章節：
  - 時間格式正確，而且落在節目長度內
  - 時間依序遞增
  - 把時間對齊到「不晚於該時間的最近一個段落開頭」
  - 章節不合格時丟掉章節但保留摘要，不要讓整次生成失敗
- `Summary` 資料類別加 `chapters` 欄位，存進本機摘要檔
- 本機結果頁：逐字稿上方列出章節目錄，點了捲到對應段落；YouTube 集數另附開影片跳秒的連結
- 用幾集實際節目檢查章節時間準不準、標題是否有用，**由使用者判斷品質**

**第二階段：上手機（確認品質後）**
- DDL（由使用者執行）：`alter table episodes add column chapters jsonb default '[]'::jsonb;`
- `upload.py` 的上傳與 `update_episode` 加上 `chapters`
- 手機後端 `DETAIL_COLUMNS` 加上 `chapters`，手機詳細頁的逐字稿分頁顯示章節目錄
- Bruno：更新本機「單集內容」「重新生成摘要」、手機「單集詳細內容」

**驗收**
- 新處理或重新生成的集數有章節，點擊能跳到正確段落
- 模型回傳的章節格式錯誤時，摘要照常產生
- 舊集數沒有章節時，頁面照常顯示

---

## 4. 逐字稿修正（做法待討論）

在本機結果頁直接修正錯字，順便處理低信心段落的逐一檢查。

**現況**
- 本機逐字稿以 `confidence < 0.6` 淡色標示低信心段，點時間戳可以回聽本機音檔
- 說話者改名已經有「本機改檔，已上傳就同步寫回資料庫」的做法（`server.py` `update_speakers`），可以照這個模式
- `upload.update_episode` 目前能更新 `transcript_text`，還不能更新 `transcript`（jsonb）

**初步做法**
- 低信心導覽：逐字稿上方加「下一處低信心（剩 N 處）」，捲到該段並自動播放
- 點段落文字進入編輯，Enter 或失焦時儲存，Esc 取消
- 新增 `PUT /api/episodes/{guid}/segments/{index}`，內容 `{text}`
  - 本機有檔案：更新 `transcript.json`、`transcript.txt`
  - 已上傳：擴充 `update_episode`，寫回 `transcript` 與 `transcript_text`
  - 同步新增 Bruno `.bru`
- 改過的段落視為已校對，不再列為低信心

**需要使用者決定**
1. 已上傳的集數改字後，是否像說話者改名一樣立刻寫回資料庫？
   建議：是。這只是修錯字；如果要等手動「再次上傳」，容易忘記。
2. 要不要保留原始辨識文字，提供「還原」？
   建議：要。第一次修改時把原文存在該段的 `original_text`，成本很低。
3. 能不能改說話者歸屬（這段其實是另一個人講的）？
   建議：第一版只改文字；說話者歸屬用下拉選單，放第二版。
4. 改字後要不要自動重新生成摘要？
   建議：不要。錯字很少影響摘要，需要時使用者自己按「重新生成」。
5. 之後如果強制重跑轉錄，手動修正會被覆蓋。要擋下來，還是只提示？
   建議：有修改過的集數，重跑前跳確認框。

---

## 5. Telegram 推播：轉錄完成或失敗時通知（已完成）

2026-10-05 完成。token 與 chat_id 改存資料庫 `app_settings` 表（RLS 開啟、無 policy），換電腦不必重填；
`.env` 有同名變數時以 `.env` 為準。

長時間轉錄時人常不在電腦前，完成或中途失敗都要能在手機得知。

- 使用者先做：在 BotFather 建 bot 取得 token，傳一句話給 bot 以便查出 chat_id，兩者填入 `.env`（`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`）
- 新增 `src/podscript/notify.py`：未設定就略過；發送失敗只記 log，不影響轉錄
- 在 `server.py` 的 `_run` 成功與失敗處各發一則
  - 成功：「轉錄完成：{集名}（耗時 N 分）」
  - 失敗：「處理失敗：{集名}：{錯誤訊息}」
- 訊息只帶標題與狀態，不帶逐字稿內容
- 用現有的 `requests`，不需安裝套件；README 與 spec §8 環境變數要一併補上

不採用的方案：
- Claude app：沒有對外推播的 API
- LINE：LINE Notify 已停止服務；Messaging API 設定繁瑣，而且有則數上限
- 手機網頁推播：要產生推播金鑰、新增資料表存訂閱，成本高

---

## 6. 文章／新聞摘要

除了影音，也能丟新聞或文章進來：不轉錄、沒有說話者，只產生摘要、心智圖、標籤。

**現況**
- `summary.generate()` 只吃一個文字檔路徑，不在乎文字來源；`PROMPT` 寫死「Podcast 逐字稿、無標點、SPEAKER_00」
- `resolvers.resolve()` 只認 Apple、YouTube，其他網址丟 `ResolveError`
- `episodes.platform` 本來就預留多平台；`duration_sec`、`published_at` 可為 null；`transcript` 是 `not null`
- 手機 `queue` 只驗證 http(s)，解析交給本機 resolver，文章網址可直接入列

**已定案（2026-10-05，使用者決定）**
1. 類型判斷：輸入框旁加「自動／Podcast／文章」切換，預設自動（Apple、YouTube 以外視為文章），判斷錯可手動改
2. 支援直接貼上全文（付費牆、電子報等抓不到正文時用）
3. 原文全文上傳資料庫，存進 `transcript_text`，沿用現有內文搜尋
4. 標籤與 Podcast 共用同一標籤庫，照常走標籤收斂
5. 短文章也照樣產生心智圖
6. 摘要模型與 Podcast 相同（`CLAUDE_CLI_MODEL`）

**已知限制（2026-10-06 決定維持現狀）**
- 自動模式只認得 Apple、YouTube，Spotify、SoundOn 等尚未支援的 Podcast 平台網址會被當成文章：
  頁面靠 JS 載入的會回「正文太短」，有長篇節目介紹的則會拿介紹文去產生摘要
- 之後若要處理：先加已知 Podcast 平台網域名單直接回錯誤；漏網再加頁面特徵（`og:audio`、`<audio>`、RSS 連結）判斷
- 新聞網站正文結尾常夾帶相關新聞標題等雜訊，`favor_precision` 實測無效（聯合報整篇抓不到），改由 prompt 請模型忽略

**做法：本機**
- 新增 `resolvers/article.py`：抓網頁、擷取正文／標題／媒體名稱／發布時間（`trafilatura`，**新套件，安裝前須使用者同意**）
  - `platform = "article"`，`podcast_name` 填媒體名稱（抓不到用網域），`duration_sec = None`
  - `episode_guid`：去掉 `utm_*`、`fbclid` 等追蹤參數後的網址取雜湊，前綴 `article-`
  - 正文少於 300 字視為擷取失敗，錯誤訊息提示改用「貼上全文」
- 貼上全文：`ProcessRequest` 加 `text`、`title`、`kind`；有 `text` 時不抓網頁，`guid` 以內文雜湊產生，`source_url` 可空
- `pipeline.py` 新增 `process_article()`：寫出 `article.txt`，跳過下載／轉錄／分離，直接呼叫 `summarize()`
  - `summary.py` 新增文章版 `ARTICLE_PROMPT`，依 `platform` 選用；`_parse_cli_output` 與標籤收斂共用
- 上傳：每段正文存成 `transcript` 的一個片段（`start`／`end` 為 0、`speaker` 為空字串），
  `transcript_text` 以換行串接各段；上傳、重新生成、搜尋定位都沿用逐字稿的格式，不需 DDL
- 本機結果頁：文章隱藏說話者、音檔、低信心；「逐字稿」區改顯示「原文」段落
- 輸入框 placeholder 與錯誤訊息更新；Bruno 更新「開始處理」的 `.bru`

**做法：手機**
- 列表：文章類以 SVG 圖示區分，meta 顯示媒體名稱，不顯示時長
- 詳細頁：分頁標題「逐字稿」改為「原文」，以 `transcript_text` 分段顯示；搜尋命中定位照常可用
- 手機後端：確認 `DETAIL_COLUMNS` 有回傳 `transcript_text` 與 `platform`，沒有就補上；Bruno 同步更新

**驗收**
- 貼一般新聞網址（自動模式）能產出摘要、心智圖、標籤，上傳後手機看得到
- 付費牆網址顯示擷取失敗並提示貼全文；貼全文後正常產出
- 同一篇帶不同 `utm` 參數只產生一筆
- 手機搜尋文章內文的字能命中並定位
- 既有 Podcast／YouTube 流程不受影響
- 從手機待處理清單貼入的文章網址，本機能正常處理

---

## 決定不做（2026-09-28）

| 項目 | 原因 |
|---|---|
| 手機回聽原音 | 目前沒有需求；而且 mp3 網址帶時效參數，YouTube 串流網址也會過期 |
| 專有名詞詞表（依頻道自動替換） | 暫時不做，擔心統一替換造成更多錯誤；whisper `--prompt` 已實測無效 |
| SRT／WebVTT 字幕匯出 | 沒有字幕需求 |
| 訂閱頻道自動輪詢、跨集提問、超長節目分段摘要 | 等需求出現再說 |
| 局部重新轉錄 | 成本高，價值待驗證 |

之後可再考慮：手機段落筆記、閱讀進度同步（都需要新表）、批次匯出備份、待處理清單自動依序處理。
