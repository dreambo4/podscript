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
  duration_sec    int,
  summary         text,
  mindmap_mermaid text,                            -- Mermaid 原始碼
  hashtags        text[] default '{}',             -- 5 個主題標籤，不含人名
  transcript      jsonb not null,
  speakers        jsonb default '{}'::jsonb,       -- {"SPEAKER_00": "主持人"}
  provenance      jsonb default '{}'::jsonb,       -- 各階段的模型與版本
  transcript_text text,                            -- 攤平的純文字，供內文搜尋
  created_at      timestamptz default now(),       -- 上傳時間
  updated_at      timestamptz default now()
);

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
