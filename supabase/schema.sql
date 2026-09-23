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
