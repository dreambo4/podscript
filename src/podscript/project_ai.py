"""研究專案的跨篇整理與專案內搜尋（只在本機執行）。

AI 整理只用各篇摘要當輸入（spec §6 第 9 點）：一個專案十集 Podcast 的逐字稿有數十萬字，
整份送入會超出額度與上下文。輸入寫檔讓 claude -p 讀，不放進命令列參數（ARG_MAX）。

兩種產出各自一次呼叫：
- 對照表、心智圖、缺口：同一份輸入，合併為一次最省額度
- 建議問題：另一個按鈕觸發，不必每次跟著重產
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import psycopg

from . import audio, pipeline, summary

CONNECT_TIMEOUT = 20

# 對照表標記：有此主張、說法不同；沒提到的不列
MARKS = ("agree", "differ")

INSIGHTS_PROMPT = """請讀取 {path}，這是一個研究專案的資料，JSON 格式：
- project：專案名稱與說明（使用者想研究的主題）
- questions：使用者的研究問題，id 為 Q1、Q2…
- sources：專案內各篇內容（Podcast、影片、文章、論文）的摘要，id 為 S1、S2…

這是使用者自己的研究，使用者要自己判斷誰對。你的角色是整理「哪篇說了什麼」，
**不判斷哪篇正確、不下結論、不回答研究問題本身、不加入資料以外的知識**。

產生以下三項，並以 JSON 格式輸出：

1. claims：主張對照表。列出各篇提出的主要主張，每列一個主張：
   - claim：主張內容，20 到 50 字的繁體中文，寫成中立的陳述句
   - marks：哪些篇目與這個主張有關，鍵為篇目 id，值為 "agree"（這篇提出或支持此主張）
     或 "differ"（這篇的說法不同或相反）；沒提到的篇目不要列
   - note：選填，說法不同時簡述差異在哪（30 字以內），例如「S2 認為漲幅假設偏高」；沒有就省略
   - 優先列出多篇都提到的主張、各篇說法不同的主張；只有一篇提到的主張挑重要的列，總共 6 到 15 列
   - 同一篇內部就有不同看法（例如主持人與來賓意見不同）也可以標 "differ"，並在 note 說明

2. mindmap：專案層級的心智圖，Mermaid mindmap 語法。根節點為專案名稱，
   第一層依主題分支（不要依篇目分支），葉節點後以全形括號註明有幾篇提到，例如「信用管制影響房價（3 篇）」；
   節點文字不要用半形括號、方括號或大括號

3. gaps：每個研究問題的缺口，鍵為問題 id：
   - covered：有談到這個問題的篇目 id 陣列（沒有就空陣列）
   - missing：2 到 3 項「還缺哪類來源」，例如缺第一手資料、缺反方觀點、缺長期數據、缺某一側的比較；
     每項 15 到 40 字，具體指出可以找哪類資料或單位，但不回答問題本身

輸出格式（只輸出 JSON，不要任何說明文字）：
{{"claims": [{{"claim": "...", "marks": {{"S1": "agree", "S3": "differ"}}, "note": "..."}}],
  "mindmap": "mindmap\\n  root((專案名稱))\\n    主題一\\n      細項（2 篇）",
  "gaps": {{"Q1": {{"covered": ["S1"], "missing": ["...", "..."]}}}}}}

規則：
- 篇目 id、問題 id 一律照抄輸入，不可自創
- mindmap 須為可直接渲染的合法 Mermaid 語法，階層以縮排表示
- 沒有研究問題時 gaps 輸出空物件"""

SUGGESTIONS_PROMPT = """請讀取 {path}，這是一個研究專案的資料，JSON 格式：
- project：專案名稱與說明（使用者想研究的主題）
- questions：使用者已經列出的研究問題
- sources：專案內各篇內容的摘要，id 為 S1、S2…

請替使用者想 3 個新的研究問題，以 JSON 格式輸出。

規則：
- 問題要能幫使用者更了解這個主題，或幫助做決定；不要與已列出的問題重複或只是換句話說
- 從現有資料看得出來、但還沒被回答的地方切入，例如各篇說法不同之處、只有一篇提到的說法、
  缺少的數據或比較
- text：問句，10 到 40 字的繁體中文
- why：一句話說明為什麼建議這個問題，指出是從哪些篇目或哪個落差看出來的（15 到 40 字），
  篇目以標題簡稱表示，不要寫 S1 這類代號
- 只提問，不給答案

輸出格式（只輸出 JSON，不要任何說明文字）：
{{"suggestions": [{{"text": "...", "why": "..."}}, {{"text": "...", "why": "..."}}, {{"text": "...", "why": "..."}}]}}"""

KIND_LABELS = {"podcast": "Podcast", "video": "影片", "article": "文章", "paper": "論文"}


class ProjectAIError(Exception):
    """AI 整理失敗。訊息需可直接顯示給使用者。"""


@dataclass
class Source:
    guid: str
    kind: str
    title: str
    source: str
    published: str
    summary: str


# ── 篇目資料 ─────────────────────────────────────────


def collect_sources(guids: list[str]) -> tuple[list[Source], list[str]]:
    """取得各篇的標題與摘要：本機有檔案的以本機為準（含尚未上傳、重新生成過的），其餘查資料庫。

    Returns:
        (有摘要的篇目依發布時間排序, 沒有摘要而略過的 guid)。
    """
    found: dict[str, Source] = {}
    for guid in guids:
        directory = audio.AUDIO_ROOT / guid
        result = pipeline.load_result(directory)
        data = pipeline.load_summary(directory) or {}
        if result is None or not data.get("summary"):
            continue
        episode = result.episode
        found[guid] = Source(
            guid=guid,
            kind=_kind(episode.platform),
            title=episode.title,
            source=episode.podcast_name,
            published=episode.to_dict()["published_at"] or "",
            summary=data["summary"],
        )

    remaining = [g for g in guids if g not in found]
    url = os.environ.get("DATABASE_URL")
    if remaining and url:
        try:
            with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "select episode_guid, platform, title, podcast_name, published_at, summary"
                        " from episodes where episode_guid = any(%s) and coalesce(summary, '') <> ''",
                        (remaining,),
                    )
                    rows = cur.fetchall()
        except psycopg.Error as exc:
            raise ProjectAIError(f"讀取資料庫失敗：{exc}") from exc
        for guid, platform, title, source, published, text in rows:
            found[guid] = Source(
                guid=guid,
                kind=_kind(platform),
                title=title,
                source=source,
                published=published.isoformat() if published else "",
                summary=text,
            )

    sources = sorted(found.values(), key=lambda s: s.published or "9999")
    skipped = [g for g in guids if g not in found]
    return sources, skipped


def _kind(platform: str | None) -> str:
    if platform in ("article", "paper"):
        return platform
    return "video" if platform == "youtube" else "podcast"


# ── 專案內搜尋 ───────────────────────────────────────

SNIPPET_RADIUS = 30
MAX_HITS_PER_ITEM = 3


def search(guids: list[str], query: str) -> list[dict]:
    """在專案篇目的逐字稿或全文裡找關鍵字（不分大小寫、只比對字面）。

    Returns:
        每篇一筆 {guid, count, hits: [{start, snippet}]}，依命中次數排序；
        start 為該段的秒數，文章與論文為 None。
    """
    needle = query.strip().lower()
    if not needle:
        return []

    segments_by_guid: dict[str, tuple[str, list[dict]]] = {}
    for guid in guids:
        result = pipeline.load_result(audio.AUDIO_ROOT / guid)
        if result is not None:
            segments_by_guid[guid] = (
                result.episode.platform,
                [{"start": s.start, "text": s.text} for s in result.segments],
            )

    remaining = [g for g in guids if g not in segments_by_guid]
    url = os.environ.get("DATABASE_URL")
    if remaining and url:
        try:
            with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
                with conn.cursor() as cur:
                    # transcript_text 有 pg_trgm 索引，先篩出有命中的再取整份逐字稿
                    cur.execute(
                        "select episode_guid, platform, transcript from episodes"
                        " where episode_guid = any(%s) and transcript_text ilike %s",
                        (remaining, "%" + _escape_like(query.strip()) + "%"),
                    )
                    rows = cur.fetchall()
        except psycopg.Error as exc:
            raise ProjectAIError(f"讀取資料庫失敗：{exc}") from exc
        for guid, platform, transcript in rows:
            segments = transcript if isinstance(transcript, list) else json.loads(transcript or "[]")
            segments_by_guid[guid] = (platform, segments)

    results = []
    for guid, (platform, segments) in segments_by_guid.items():
        timed = platform not in ("article", "paper")
        count, hits = 0, []
        for seg in segments:
            text = seg.get("text") or ""
            lowered = text.lower()
            n = lowered.count(needle)
            if not n:
                continue
            count += n
            if len(hits) < MAX_HITS_PER_ITEM:
                pos = lowered.find(needle)
                start, end = max(pos - SNIPPET_RADIUS, 0), pos + len(needle) + SNIPPET_RADIUS
                snippet = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
                hits.append({"start": seg.get("start") if timed else None, "snippet": snippet})
        if count:
            results.append({"guid": guid, "count": count, "hits": hits})
    results.sort(key=lambda r: -r["count"])
    return results


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ── AI 整理 ──────────────────────────────────────────


def generate_insights(project: dict, sources: list[Source]) -> dict:
    """產生對照表、心智圖與缺口。

    Returns:
        {claims, mindmap, gaps, source_guids, provenance}；篇目與問題代號已換回 guid 與問題 id。

    Raises:
        ProjectAIError: 沒有可用的篇目、生成失敗或回傳格式不正確。
    """
    if not sources:
        raise ProjectAIError("專案內還沒有已生成摘要的篇目")
    payload, model, sid, qid = _run(INSIGHTS_PROMPT, project, sources)

    claims = []
    for row in payload.get("claims") or []:
        if not isinstance(row, dict) or not isinstance(row.get("claim"), str):
            continue
        marks = {
            sid[k]: v
            for k, v in (row.get("marks") or {}).items()
            if k in sid and v in MARKS
        }
        if not marks:
            continue
        item = {"claim": row["claim"].strip(), "marks": marks}
        if isinstance(row.get("note"), str) and row["note"].strip():
            item["note"] = _replace_ids(row["note"].strip(), sid, sources)
        claims.append(item)

    mindmap = payload.get("mindmap")
    if not claims or not isinstance(mindmap, str) or not mindmap.strip():
        raise ProjectAIError("模型回傳的對照表或心智圖不完整，請再試一次")

    gaps = {}
    for key, value in (payload.get("gaps") or {}).items():
        if key not in qid or not isinstance(value, dict):
            continue
        gaps[qid[key]] = {
            "covered": [sid[s] for s in value.get("covered") or [] if s in sid],
            "missing": [m.strip() for m in value.get("missing") or [] if isinstance(m, str) and m.strip()],
        }

    return {
        "claims": {
            "sources": [{"guid": s.guid, "title": s.title, "kind": s.kind, "published": s.published[:10]} for s in sources],
            "rows": claims,
        },
        "mindmap": mindmap.strip(),
        "gaps": gaps,
        "source_guids": [s.guid for s in sources],
        "provenance": {"model": model},
    }


def generate_suggestions(project: dict, sources: list[Source]) -> tuple[list[dict], dict]:
    """產生 3 個建議研究問題。

    Returns:
        ([{text, why}], provenance)。

    Raises:
        ProjectAIError: 沒有可用的篇目、生成失敗或回傳格式不正確。
    """
    if not sources:
        raise ProjectAIError("專案內還沒有已生成摘要的篇目")
    payload, model, _sid, _qid = _run(SUGGESTIONS_PROMPT, project, sources)
    suggestions = [
        {"text": item["text"].strip(), "why": (item.get("why") or "").strip()}
        for item in payload.get("suggestions") or []
        if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip()
    ][:3]
    if not suggestions:
        raise ProjectAIError("模型沒有回傳建議問題，請再試一次")
    return suggestions, {"model": model}


def _run(prompt: str, project: dict, sources: list[Source]) -> tuple[dict, str, dict, dict]:
    """寫出輸入檔並呼叫模型。

    Returns:
        (模型回傳的 JSON, 實際模型 ID, 篇目代號→guid, 問題代號→問題 id)。
    """
    sid = {f"S{i}": s.guid for i, s in enumerate(sources, 1)}
    qid = {f"Q{i}": q["id"] for i, q in enumerate(project.get("questions") or [], 1)}
    data = {
        "project": {"name": project["name"], "description": project.get("description") or ""},
        "questions": [
            {"id": key, "text": q["text"]}
            for key, q in zip(qid, project.get("questions") or [])
        ],
        "sources": [
            {
                "id": key,
                "type": KIND_LABELS[s.kind],
                "title": s.title,
                "source": s.source,
                "published": s.published[:10],
                "summary": s.summary,
            }
            for key, s in zip(sid, sources)
        ],
    }

    model = os.environ.get("CLAUDE_CLI_MODEL", "opus")
    provider = summary.get_provider(os.environ.get("SUMMARY_PROVIDER", "claude_cli"))
    # audio/ 不進 git，與單集的暫存檔放在一起；結束即刪
    workdir = Path(tempfile.mkdtemp(prefix=".project-", dir=audio.AUDIO_ROOT))
    try:
        path = workdir / "project.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        payload, model_id = provider.generate_json(prompt.format(path=path), model=model)
    except summary.SummaryError as exc:
        raise ProjectAIError(str(exc)) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return payload, model_id, sid, qid


def _replace_ids(text: str, sid: dict, sources: list[Source]) -> str:
    """把 S1 這類代號換成「篇目標題簡稱」，使用者才看得懂。"""
    titles = {key: _short_title(s.title) for key, s in zip(sid, sources)}
    return re.sub(r"(?<![A-Za-z0-9])S\d+(?![0-9])", lambda m: titles.get(m.group(0), m.group(0)), text)


def _short_title(title: str, limit: int = 12) -> str:
    return title if len(title) <= limit else title[:limit] + "…"
