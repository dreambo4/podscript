-- podscript 資料表
-- 由使用者本人在 Supabase SQL Editor 執行（Claude 不下 DDL）
-- 對應 spec §7

-- 中文全文搜尋：Postgres 的 to_tsvector 不會斷中文詞，
-- 整句會變成單一 token 導致搜尋失效，必須改用 pg_trgm 三元組模糊比對。
create extension if not exists pg_trgm;

create table if not exists episodes (
  id              uuid primary key default gen_random_uuid(),
  platform        text not null default 'apple',   -- 預留多平台
  source_url      text not null,
  episode_guid    text unique not null,            -- 防重複上傳
  podcast_name    text not null,
  title           text not null,
  published_at    timestamptz,                     -- Podcast 原始發布時間
  duration_sec    int,                             -- 音檔長度（秒）；論文存 PDF 頁數
  summary         text,
  mindmap_mermaid text,                            -- Mermaid 原始碼
  hashtags        text[] default '{}',             -- 最多 5 個主題標籤，不含人名、語意不重疊
  transcript      jsonb not null,
  speakers        jsonb default '{}'::jsonb,       -- {"SPEAKER_00": "主持人"}
  provenance      jsonb default '{}'::jsonb,       -- 各階段的模型與版本
  transcript_text text,                            -- 攤平的純文字，供內文搜尋
  chapters        jsonb not null default '[]'::jsonb, -- [{"start": 秒數, "title": "..."}]
  cover           jsonb,                           -- {"svg": 48x48 線條插圖的內部元素, "color": "#rrggbb"}
  created_at      timestamptz default now(),       -- 上傳時間
  updated_at      timestamptz default now()
);

-- 章節（2026-10-06 新增）：既有資料庫的 create table 不會補欄位，以此補上。
-- start 等於 transcript 中某一段的 start，前端以此定位段落；文章與舊集數為空陣列。
alter table episodes add column if not exists chapters jsonb not null default '[]'::jsonb;

-- 論文標題的中文譯文（2026-10-09 新增）：列表顯示在原標題下方。
-- 逐段譯文存在 transcript 每一段的 translation，不另開欄位。
alter table episodes add column if not exists title_translated text;

-- 內容封面（2026-10-07 新增）：摘要模型依內容畫的線條插圖，不含文字。
-- svg 只含白名單內的繪圖元素，線條與填色一律 currentColor，由前端依 color 上色；舊集數為 null。
alter table episodes add column if not exists cover jsonb;

create index if not exists episodes_guid_idx on episodes (episode_guid);
create index if not exists episodes_published_idx on episodes (published_at desc);

-- 標籤篩選：where hashtags @> array['房地產']
create index if not exists episodes_hashtags_idx on episodes using gin (hashtags);

-- 中文搜尋：標題與逐字稿內文
create index if not exists episodes_title_trgm
  on episodes using gin (title gin_trgm_ops);
create index if not exists episodes_text_trgm
  on episodes using gin (transcript_text gin_trgm_ops);

-- 白名單（spec §6.3）：users 表本身就是白名單，不另建表。
-- 新增成員只需 insert 一筆 name + email，不必改設定或重啟服務。
create table if not exists users (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  email       text,                    -- 新增成員時先填這個
  google_sub  text unique,             -- 首次登入後自動寫入
  created_at  timestamptz default now()
);

create index if not exists users_email_idx on users (email);

-- ────────────────────────────────────────────────
-- RLS（Row Level Security）
--
-- 本專案手機端不直連 Supabase，一律經 FastAPI（spec §6）：
--   手機網頁 → FastAPI（Google 驗證 + 白名單 + JWT）→ Postgres
-- 因此沒有 anon key 暴露在公開網頁，RLS 非必要。
--
-- 若日後改為前端直連 Supabase，則 RLS 必須啟用並設 policy，
-- 否則任何持有 anon key 者皆可讀寫全部資料。
--
-- 也可視為 DATABASE_URL 外流時的第二道防線，屆時再評估。

-- ────────────────────────────────────────────────
-- 待處理佇列（手機端貼網址暫存，回家在本機端處理）
--
-- 手機端只驗證是否為合法 http(s) 網址，不檢查平台：
-- 解析責任在本機端 resolver，平台擴充時不必同步改動手機端。
-- episode_guid 與 title 在本機端解析成功後才回填，故可為 null。
create table if not exists queue (
  id           uuid primary key default gen_random_uuid(),
  url          text not null check (url <> '' and length(url) <= 2048),
  episode_guid text,                               -- 本機端解析後回填
  title        text,                               -- 同上
  note         text check (note is null or length(note) <= 200),
  status       text not null default 'pending'
               check (status in ('pending', 'done', 'skipped')),
  -- 刪除使用者不該連帶刪掉佇列項目（別人也可能在等這集），
  -- 也不該被 FK 擋住，故留 null。
  added_by     uuid references users(id) on delete set null,
  created_at   timestamptz default now(),
  processed_at timestamptz
);

-- 同一網址只允許一筆待處理；標記 done 或 skipped 後可再次貼同一網址。
create unique index if not exists queue_url_pending_idx
  on queue (url) where status = 'pending';

-- 兩端的清單都只查 pending 並依時間排序，索引直接涵蓋該查詢。
create index if not exists queue_pending_idx
  on queue (created_at desc) where status = 'pending';

-- ────────────────────────────────────────────────
-- 本機端設定（換電腦時不必重填，如 Telegram 推播的 token 與 chat_id）
--
-- 新電腦仍需 .env 的 DATABASE_URL 才連得上，故只放「連上資料庫之後」才需要的設定；
-- .env 有同名變數時以 .env 為準。
-- 內含密鑰，啟用 RLS 且不設任何 policy：Supabase REST API（anon／authenticated）
-- 一律讀不到，只有本機以 DATABASE_URL 直連（資料表擁有者，不受 RLS 限制）才讀得到。
create table if not exists app_settings (
  key        text primary key,
  value      text not null,
  updated_at timestamptz default now()
);

alter table app_settings enable row level security;

-- ────────────────────────────────────────────────
-- 研究專案（2026-10-09 新增）：使用者自訂的研究主題，把相關的 Podcast、文章、論文歸在一起。
-- 與標籤分開：標籤由模型依內容產生，專案由使用者建立與歸類。
-- 一筆內容可屬於多個專案；尚未上傳的單集也能先歸類，故 project_items 只存 episode_guid、
-- 不設外鍵到 episodes。刪除單集時由本機服務一併刪除對照列。
-- 只由本機服務與手機後端以資料庫直連存取，開啟 RLS、不設 policy，擋下 REST API。
-- ────────────────────────────────────────────────
create table if not exists projects (
  id          uuid primary key default gen_random_uuid(),
  name        text not null unique check (name <> '' and length(name) <= 100),
  description text not null default '' check (length(description) <= 2000),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create table if not exists project_items (
  project_id   uuid not null references projects (id) on delete cascade,
  episode_guid text not null,
  added_at     timestamptz not null default now(),
  primary key (project_id, episode_guid)
);

-- 單集頁查「這集屬於哪些專案」
create index if not exists project_items_guid_idx on project_items (episode_guid);

alter table projects enable row level security;
alter table project_items enable row level security;

-- ────────────────────────────────────────────────
-- 研究專案頁（2026-10-09）：專案筆記、研究問題、AI 整理結果
-- spec：.claude/specs/specs_20261009_研究專案頁面.md §8
-- 研究問題與筆記本機、手機都能編輯；AI 整理只由本機 claude -p 產生，手機只讀。
-- 存筆記時比對「開始編輯時的內容」，另一台裝置改過就拒絕，避免互相覆蓋。
-- ────────────────────────────────────────────────

-- 1. 專案筆記
alter table projects add column if not exists note text not null default ''
  check (length(note) <= 50000);
alter table projects add column if not exists note_updated_at timestamptz;

-- 2. 研究問題
create table if not exists project_questions (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects (id) on delete cascade,
  text        text not null check (text <> '' and length(text) <= 200),
  status      text not null default 'open'
              check (status in ('open', 'partial', 'resolved')),  -- 還沒有答案／有初步想法／已釐清
  position    int  not null default 0,                           -- 手動排序，小的在前
  note        text not null default '' check (length(note) <= 20000),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index if not exists project_questions_project_idx
  on project_questions (project_id, position);

-- 3. AI 整理結果（本機 claude -p 產生，手機只讀）；重新產生時整列覆蓋
create table if not exists project_insights (
  project_id      uuid primary key references projects (id) on delete cascade,
  claims          jsonb,          -- 主張對照表 {"sources": [...], "rows": [{"claim", "marks": {guid: "agree"|"differ"}, "note"}]}
  mindmap         text,           -- Mermaid mindmap 語法，沿用單集的心智圖渲染
  gaps            jsonb,          -- {"<question_id>": {"covered": [guid...], "missing": ["..."]}}
  source_guids    text[] not null default '{}',  -- 產生時用到的篇目，用來提示「有新篇目未納入」
  generated_at    timestamptz,
  suggestions     jsonb not null default '[]'::jsonb,  -- 建議問題 [{"text": "...", "why": "..."}]
  suggested_at    timestamptz,
  provenance      jsonb not null default '{}'::jsonb   -- 模型與版本
);

alter table project_questions enable row level security;
alter table project_insights enable row level security;

-- 手機待處理：存網址時選的研究專案（可多選），本機處理完成後歸入（2026-10-09）
-- 不設外鍵：陣列無法設外鍵；專案已刪除時，歸入時略過
alter table queue add column if not exists project_ids uuid[] not null default '{}';

-- 待處理加入全文與 PDF（2026-10-10）：只能從 Telegram 分享入口加入，見 mobile-backend/app/routers/telegram.py
-- kind=text 的全文存 content；kind=pdf 的原檔暫存 pdf（Telegram 下載上限 20 MB），
-- 本機上傳該篇後整列刪除（resolve_queue_item），不長期佔用資料庫空間。
-- 這兩種沒有網址，url 改為可空；queue_url_pending_idx 對 null 不判重複，不受影響。
alter table queue add column if not exists kind text not null default 'url'
  check (kind in ('url', 'text', 'pdf'));
alter table queue add column if not exists content text;
alter table queue add column if not exists pdf bytea;
alter table queue alter column url drop not null;
alter table queue drop constraint if exists queue_url_check;
alter table queue add constraint queue_url_check
  check (url is null or (url <> '' and length(url) <= 2048));
alter table queue drop constraint if exists queue_kind_payload_check;
alter table queue add constraint queue_kind_payload_check check (
  (kind = 'url' and url is not null)
  or (kind = 'text' and content is not null)
  or (kind = 'pdf' and pdf is not null)
);
