"""本機 FastAPI 服務。

處理耗時數十分鐘，故 POST /api/process 立即回應並在背景執行，
前端以 GET /api/jobs/{guid} 輪詢進度。
"""
from __future__ import annotations

import json
import os
import re
import threading
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, notify, pipeline, projects, storage, upload
from .resolvers import ResolveError, article, is_media_url, paper, platform_of, resolve
from .summary import SummaryError

# 模型、摘要與資料庫設定皆來自 .env，須在建立 app 前載入。
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

STATIC_DIR = Path(__file__).resolve().parents[2] / "web"

app = FastAPI(title="podscript")


@dataclass
class Job:
    """一集的處理進度。

    狀態寫入該集目錄的 job.json，與 spec §4.4 的檔案式快取一致：
    前端刷新、開新分頁或服務重啟後，都能從檔案還原進度。
    """

    guid: str
    title: str
    url: str = ""
    kind: str = "podcast"  # podcast、article 或 paper，決定有哪些階段
    stage: str = "queued"
    message: str = "等待開始"
    percent: int | None = None
    done: bool = False
    error: str = ""
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "guid": self.guid,
            "title": self.title,
            "url": self.url,
            "kind": self.kind,
            "stage": self.stage,
            "message": self.message,
            "percent": self.percent,
            "done": self.done,
            "error": self.error,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "stages": [
                "resolve",
                *(pipeline.STAGES if self.kind == "podcast" else pipeline.ARTICLE_STAGES),
            ],
        }

    def save(self) -> None:
        """寫入該集目錄，供刷新或重啟後還原。"""
        self.updated_at = datetime.now(timezone.utc).isoformat()
        directory = audio.episode_dir(self.guid)
        (directory / JOB_FILE).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


JOB_FILE = "job.json"

# 記憶體中的任務供執行中的執行緒更新；讀取一律經 _read_job 以檔案為準。
_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def _read_job(guid: str) -> Job | None:
    """讀取任務狀態，優先取記憶體中執行中的任務。

    服務重啟後記憶體為空，此時從 job.json 還原；
    若該任務在重啟時仍在執行中，標記為中斷以免前端無限等待。
    """
    running = _jobs.get(guid)
    if running is not None:
        return running

    path = audio.AUDIO_ROOT / guid / JOB_FILE
    if not path.exists():
        return None

    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("stages", None)
    job = Job(**data)

    if not job.done:
        job.done = True
        job.error = "服務重啟，處理已中斷；可重新送出網址接續未完成的階段"
        job.message = "已中斷"
    return job


class ProcessRequest(BaseModel):
    url: str = ""
    # auto：Apple、YouTube 以外的網址視為文章
    kind: Literal["auto", "podcast", "article"] = "auto"
    text: str | None = None  # 貼上的文章全文；有值時不抓網頁
    title: str | None = None  # 貼上全文時的標題，留空取第一段開頭
    force: str | None = None
    num_speakers: int | None = None
    queue_id: str | None = None  # 由待處理清單觸發時帶入，供回填標題與結案


class SpeakersRequest(BaseModel):
    speakers: dict[str, str]


class ProjectRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class EpisodeProjectsRequest(BaseModel):
    project_ids: list[str]


class HashtagDecisionRequest(BaseModel):
    # {新標籤: 要保留的標籤}，值為合併前的新標籤或建議的既有標籤。
    decisions: dict[str, str]


@app.post("/api/process")
def start_process(req: ProcessRequest) -> dict:
    """解析網址（或貼上的全文）並在背景開始處理。

    文章在這裡就抓好正文，擷取失敗直接回 400，
    讓使用者當場改用貼上全文。
    """
    if not req.text and not req.url.strip():
        raise HTTPException(status_code=400, detail="請貼上網址或文章全文")

    try:
        if req.text or req.kind == "article" or (
            req.kind == "auto" and not is_media_url(req.url)
        ):
            parsed = (
                article.from_text(req.text, title=req.title or "")
                if req.text
                else article.resolve_url(req.url)
            )
            episode = parsed.episode
            kind = "article"
            task = _article_task(parsed, force=req.force == "summarize")
        else:
            episode = resolve(req.url)
            kind = "podcast"
            task = _podcast_task(req.url, req.force, req.num_speakers)
    except ResolveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    guid = episode.episode_guid
    # 手機端入列時不解析網址，這裡補上 guid 與標題讓待處理清單看得懂。
    if req.queue_id:
        upload.annotate_queue_item(
            req.queue_id, episode_guid=guid, title=episode.title
        )

    with _lock:
        running = _jobs.get(guid)
        if running and not running.done:
            return running.to_dict()
        job = Job(guid=guid, title=episode.title, url=episode.source_url, kind=kind)
        job.save()
        _jobs[guid] = job

    thread = threading.Thread(target=_run, args=(job, task), daemon=True)
    thread.start()
    return job.to_dict()


# Storage 單檔上限
MAX_PDF_BYTES = 50 * 1024 * 1024


@app.post("/api/papers")
async def start_paper(request: Request, filename: str = "") -> dict:
    """上傳論文 PDF（request body 為 PDF 原檔）並在背景產生摘要。

    擷取全文在這裡同步完成（每篇不到一秒），抽不到文字（掃描檔）直接回 400。
    PDF 原檔先存在本機，按「上傳」時才存到 Storage，見 upload.upload。
    """
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="沒有收到檔案")
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status_code=400, detail="PDF 超過 50 MB，無法上傳")

    try:
        parsed = await run_in_threadpool(paper.from_pdf, data, filename=filename)
    except ResolveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    guid = parsed.episode.episode_guid
    with _lock:
        running = _jobs.get(guid)
        if running and not running.done:
            return running.to_dict()
        job = Job(guid=guid, title=parsed.episode.title, kind="paper")
        job.save()
        _jobs[guid] = job

    task = _paper_task(parsed, filename)
    thread = threading.Thread(target=_run, args=(job, task), daemon=True)
    thread.start()
    return job.to_dict()


Task = Callable[[pipeline.ProgressFn], object]


def _podcast_task(url: str, force: str | None, num_speakers: int | None) -> Task:
    return lambda on_progress: pipeline.process(
        url,
        model=os.environ.get("WHISPER_MODEL", "large-v2"),
        force=force,
        num_speakers=num_speakers,
        on_progress=on_progress,
    )


def _article_task(parsed: article.Article, *, force: bool = False) -> Task:
    return lambda on_progress: pipeline.process_article(
        parsed, force=force, on_progress=on_progress
    )


def _paper_task(parsed: paper.Paper, filename: str) -> Task:
    return lambda on_progress: pipeline.process_paper(
        parsed, filename=filename, on_progress=on_progress
    )


def _run(job: Job, task: Task) -> None:
    """背景執行整條流程，把進度寫回 job。"""

    def on_progress(stage: str, message: str, percent: int | None = None) -> None:
        job.stage = stage
        job.message = message
        job.percent = percent
        # 百分比每 1% 更新一次，寫檔過於頻繁；僅在階段切換或每 5% 落地。
        if percent is None or percent % 5 == 0:
            job.save()

    try:
        task(on_progress)
        job.stage = "done"
        job.message = "完成"
        job.percent = None
        notify.job_done(job.title, _elapsed_minutes(job))
    except Exception as exc:  # 背景執行緒需攔下所有例外，否則錯誤不會傳到前端
        job.error = str(exc) or exc.__class__.__name__
        job.message = "處理失敗"
        traceback.print_exc()
        notify.job_failed(job.title, job.error)
    finally:
        job.done = True
        job.save()
        with _lock:
            _jobs.pop(job.guid, None)


def _elapsed_minutes(job: Job) -> int:
    started = datetime.fromisoformat(job.started_at)
    return round((datetime.now(timezone.utc) - started).total_seconds() / 60)


@app.get("/api/jobs/{guid}")
def get_job(guid: str) -> dict:
    job = _read_job(guid)
    if job is None:
        raise HTTPException(status_code=404, detail="查無此任務")
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    """列出進行中的任務，供前端刷新後接回進度。"""
    return [job.to_dict() for job in _jobs.values() if not job.done]


@app.post("/api/jobs/{guid}/resume")
def resume_job(guid: str) -> dict:
    """接續中斷的處理。

    沿用 job.json 記下的原始網址，不需使用者重貼；
    已完成的階段會因對應檔案存在而自動跳過（spec §4.4）。
    """
    previous = _read_job(guid)
    if previous is None:
        raise HTTPException(status_code=404, detail="查無此任務")

    task = _resume_task(guid, previous)
    if task is None:
        raise HTTPException(
            status_code=400, detail="這筆紀錄沒有原始網址，請重新貼上網址或全文"
        )

    with _lock:
        running = _jobs.get(guid)
        if running and not running.done:
            return running.to_dict()
        job = Job(guid=guid, title=previous.title, url=previous.url, kind=previous.kind)
        job.save()
        _jobs[guid] = job

    thread = threading.Thread(target=_run, args=(job, task), daemon=True)
    thread.start()
    return job.to_dict()


def _resume_task(guid: str, previous: Job) -> Task | None:
    """接續用的任務；文章與論文已有全文就只補摘要，不必重抓網頁或重新擷取。"""
    if previous.kind == "podcast":
        return _podcast_task(previous.url, None, None) if previous.url else None

    directory = _episode_dir(guid)
    result = pipeline.load_result(directory)
    if result is not None:

        def summarize_only(on_progress: pipeline.ProgressFn) -> None:
            on_progress("summarize", "產生摘要、心智圖與標籤", None)
            pipeline.summarize(directory, result=result)

        return summarize_only
    if previous.kind == "paper":
        pdf = directory / pipeline.PAPER_PDF
        if not pdf.exists():
            return None
        try:
            return _paper_task(paper.from_pdf(pdf.read_bytes()), "")
        except ResolveError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not previous.url:
        return None
    try:
        return _article_task(article.resolve_url(previous.url))
    except ResolveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/queue")
def list_queue() -> list[dict]:
    """手機端貼上的待處理網址。實際下載與轉錄由使用者手動觸發。"""
    return upload.fetch_queue()


@app.delete("/api/queue/{item_id}")
def remove_queue_item(item_id: str) -> dict:
    """從待處理清單移除（決定不處理這集）。"""
    try:
        upload.remove_queue_item(item_id)
    except upload.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"removed": item_id}


@app.get("/api/episodes")
def list_episodes() -> list[dict]:
    """列出所有單集：本機處理中的，加上已上傳至資料庫的。

    上傳後本機目錄即清除，已上傳的單集一律由資料庫還原，
    因此換機後只帶程式也能看到完整清單。
    """
    episodes: dict[str, dict] = {}

    for result, summary_data, uploaded, created in upload.fetch_all():
        guid = result.episode.episode_guid
        episodes[guid] = _episode_summary(
            guid, result=result, summary_data=summary_data, job=None
        ) | {"uploaded_at": uploaded, "created_at": created}

    # 本機檔案覆蓋資料庫版本：處理中或剛重新生成、尚未上傳者以本機為準。
    for directory in audio.AUDIO_ROOT.glob("*"):
        if not directory.is_dir():
            continue

        job = _read_job(directory.name)
        result = pipeline.load_result(directory)
        if result is None and job is None:
            continue

        item = _episode_summary(
            directory.name,
            result=result,
            summary_data=pipeline.load_summary(directory) or {},
            job=job,
            directory=directory,
        )
        previous = episodes.get(directory.name, {})
        # created_at 以資料庫為準；本機的 job.started_at 僅供尚未上傳者使用。
        merged = {**previous, **item}
        merged["created_at"] = previous.get("created_at") or item["created_at"]
        episodes[directory.name] = merged

    items = list(episodes.values())
    # 所屬專案供左側清單依專案篩選；查詢失敗時視為都沒有歸類
    by_episode = projects.projects_by_episode()
    for item in items:
        item["projects"] = by_episode.get(item["guid"], [])
    # 本機尚未上傳者的上傳時間需另外查詢。
    missing = [e["guid"] for e in items if not e.get("uploaded_at")]
    times = upload.uploaded_times(missing)
    for item in items:
        item.setdefault("uploaded_at", None)
        if not item["uploaded_at"]:
            item["uploaded_at"] = times.get(item["guid"])
        item["needs_reupload"] = bool(
            item["uploaded_at"] and item.get("has_local_summary") and not item["processing"]
        )

    # 依加入本工具的時間排序（非節目發布時間），新加入的在前。
    items.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return items


def _episode_summary(
    guid: str,
    *,
    result: pipeline.Result | None,
    summary_data: dict,
    job: Job | None,
    directory: Path | None = None,
) -> dict:
    """組出列表頁單集所需的欄位。

    created_at 為加入本工具的時間，供列表排序；本機來源取任務開始時間，
    已上傳者由呼叫端以資料庫的 created_at 覆寫。
    """
    episode = result.episode if result else None
    return {
        "guid": guid,
        "title": episode.title if episode else (job.title if job else guid),
        "podcast_name": episode.podcast_name if episode else "",
        "published_at": episode.to_dict()["published_at"] if episode else None,
        "created_at": job.started_at if job else "",
        "duration_sec": episode.duration_sec if episode else None,
        # 處理中尚未取得節目資訊時，依任務網址判斷平台
        "platform": episode.platform if episode else (platform_of(job.url) if job and job.url else None),
        "cover": summary_data.get("cover"),
        "kind": _content_kind(episode, job),
        "hashtags": summary_data.get("hashtags", []),
        "has_summary": bool(summary_data.get("summary")),
        "ready": result is not None,
        "processing": bool(job and not job.done),
        "has_audio": bool(directory and (directory / "source.mp3").exists()),
        # 上傳後本機目錄會清除；已上傳又出現本機摘要檔，表示重新生成過、尚未再次上傳
        "has_local_summary": bool(directory and (directory / "result.json").exists()),
        "stage": job.stage if job else "",
        "message": job.message if job else "",
        "error": job.error if job else "",
    }


def _content_kind(episode, job: Job | None) -> str:
    """podcast、article 或 paper；處理中尚未取得節目資訊時看任務類型。"""
    if episode is not None:
        if pipeline.is_paper(episode):
            return "paper"
        return "article" if pipeline.is_article(episode) else "podcast"
    return job.kind if job else "podcast"


def _load_episode(guid: str) -> tuple[pipeline.Result, dict, bool]:
    """載入單集，本機沒有就從資料庫還原。

    Returns:
        (Result, 摘要, 是否來自資料庫)。

    Raises:
        HTTPException: 兩處都查無此單集。
    """
    directory = _episode_dir(guid)
    result = pipeline.load_result(directory)
    if result is not None:
        return result, pipeline.load_summary(directory) or {}, False

    try:
        fetched = upload.fetch_episode(guid)
    except upload.UploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if fetched is None:
        raise HTTPException(status_code=404, detail="查無此單集")

    result, summary_data = fetched
    return result, summary_data, True


@app.get("/api/episodes/{guid}")
def get_episode(guid: str) -> dict:
    result, summary_data, from_db = _load_episode(guid)
    directory = _episode_dir(guid)

    return {
        **result.to_dict(),
        "summary": summary_data or None,
        "uploaded_at": upload.uploaded_at(guid),
        "has_audio": (directory / "source.mp3").exists(),
        "from_db": from_db,
        "needs_reupload": _needs_reupload(guid),
        "known_speakers": []
        if pipeline.is_text(result.episode)
        else _known_speakers(result.episode.podcast_name),
        "projects": projects.projects_of(guid),
    }


DEFAULT_SPEAKER = re.compile(r"SPEAKER_\d+")


def _known_speakers(podcast_name: str) -> list[str]:
    """列出同一節目以前用過的說話者名稱，依使用集數由多到少排序。

    來源為資料庫加上本機尚未上傳的集數；同一集兩邊都有時以本機為準。
    以 podcast_name 判斷是否同一節目（YouTube 為頻道名）。
    """
    if not podcast_name:
        return []

    by_guid = upload.fetch_speakers_by_podcast(podcast_name)
    for directory in audio.AUDIO_ROOT.glob("*"):
        if not directory.is_dir():
            continue
        result = pipeline.load_result(directory)
        if result is not None and result.episode.podcast_name == podcast_name:
            by_guid[directory.name] = result.speakers

    counts: Counter[str] = Counter()
    for speakers in by_guid.values():
        names = {v.strip() for v in speakers.values() if isinstance(v, str)}
        counts.update(n for n in names if n and not DEFAULT_SPEAKER.fullmatch(n))
    # 次數相同時依名稱排序，讓清單順序穩定。
    return [name for name, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


@app.put("/api/episodes/{guid}/speakers")
def update_speakers(guid: str, req: SpeakersRequest) -> dict:
    """更新說話者名稱。只影響顯示，不重跑任何階段。

    已上傳的單集本機無檔案，直接寫回資料庫。
    """
    result, _summary, from_db = _load_episode(guid)
    if not from_db:
        result = pipeline.rename_speakers(_episode_dir(guid), req.speakers)
        if upload.uploaded_at(guid):
            _sync_speakers(guid, result)
        return {"speakers": result.speakers}

    # 空字串視為取消自訂，與 pipeline.rename_speakers 一致。
    result.speakers = {k: v.strip() for k, v in req.speakers.items() if v.strip()}
    _sync_speakers(guid, result)
    return {"speakers": result.speakers}


def _sync_speakers(guid: str, result: pipeline.Result) -> None:
    """把說話者名稱寫回資料庫。

    transcript_text 不含說話者名稱，故不需一併更新。
    """
    try:
        upload.update_episode(guid, speakers=result.speakers)
    except upload.UploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/episodes/{guid}/regenerate")
def regenerate(guid: str) -> dict:
    """重新生成摘要、心智圖與標籤。三項一併重生，不重跑轉錄。

    結果只寫本機，不動資料庫：上傳為手動觸發，使用者可反覆重生到滿意
    再按「再次上傳」（spec §11 決策 10）。已上傳的單集本機已無檔案，
    重生時一併還原 transcript.json，上傳後再由 _remove_local 一起清除。
    """
    result, _summary, _from_db = _load_episode(guid)
    generated = pipeline.summarize(
        _episode_dir(guid), result=result, force=True
    )
    if generated is None:
        raise HTTPException(status_code=502, detail="摘要生成失敗，請稍後再試")
    return generated.to_dict()


@app.post("/api/episodes/{guid}/chapters")
def add_chapters(guid: str) -> dict:
    """只產生章節，摘要、心智圖與標籤不變。供已有摘要的舊集數與文章補上章節。

    本機有摘要檔時寫入摘要檔；已上傳的單集同時寫回資料庫，
    與說話者改名相同，不需再按「再次上傳」。
    """
    result, summary_data, _from_db = _load_episode(guid)
    if not summary_data.get("summary"):
        raise HTTPException(status_code=400, detail="請先產生摘要")

    try:
        chapters = pipeline.add_chapters(_episode_dir(guid), result)
    except SummaryError as exc:
        raise HTTPException(status_code=502, detail=f"章節生成失敗：{exc}") from exc

    if upload.uploaded_at(guid):
        try:
            upload.update_episode(guid, chapters=chapters)
        except upload.UploadError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"chapters": chapters}


@app.post("/api/episodes/{guid}/cover")
def regenerate_cover(guid: str) -> dict:
    """只重畫封面，摘要、心智圖、標籤與章節不變。依標題、摘要與標籤畫，不讀逐字稿。

    本機有摘要檔時寫入摘要檔；已上傳的單集同時寫回資料庫，
    與補章節相同，不需再按「再次上傳」。
    """
    result, summary_data, _from_db = _load_episode(guid)
    if not summary_data.get("summary"):
        raise HTTPException(status_code=400, detail="請先產生摘要")

    try:
        cover = pipeline.regenerate_cover(_episode_dir(guid), result, summary_data)
    except SummaryError as exc:
        raise HTTPException(status_code=502, detail=f"封面生成失敗：{exc}") from exc

    if upload.uploaded_at(guid):
        try:
            upload.update_episode(guid, cover=cover)
        except upload.UploadError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"cover": cover}


@app.put("/api/episodes/{guid}/hashtags")
def decide_hashtags(guid: str, req: HashtagDecisionRequest) -> dict:
    """確認標籤合併建議：逐項決定保留原標籤或改用既有標籤。

    合併建議只存在本機 result.json（重新生成後產生），
    全部決定完才能上傳，見 upload.upload。
    """
    try:
        return pipeline.decide_hashtag_merges(_episode_dir(guid), req.decisions)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="這集沒有待確認的標籤") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _needs_reupload(guid: str) -> bool:
    """已上傳、但本機有重新生成的摘要尚未再次上傳。

    上傳成功會清除整個本機目錄，之後只有重新生成會在本機寫出 result.json。
    """
    return (_episode_dir(guid) / "result.json").exists() and bool(upload.uploaded_at(guid))


@app.delete("/api/episodes/{guid}/regenerated")
def discard_regenerated(guid: str) -> dict:
    """放棄重新生成的內容：清除本機檔案，回到資料庫中已上傳的版本。

    只允許已上傳且本機有摘要檔的單集，避免誤刪尚未上傳的成果。
    """
    job = _read_job(guid)
    if job is not None and not job.done:
        raise HTTPException(status_code=409, detail="這集正在處理中，請等處理結束再放棄")
    if not _needs_reupload(guid):
        raise HTTPException(status_code=400, detail="這集沒有待重新上傳的內容")
    try:
        return upload.discard_audio(_episode_dir(guid))
    except upload.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/episodes/{guid}/upload")
def upload_episode(guid: str) -> dict:
    """上傳結果至 Supabase。手動觸發，不會自動執行（spec §11 決策 10）。

    上傳成功代表這集已完成，對應的待處理項目一併結案；
    手機端的待處理清單因此不需使用者手動清掉。
    """
    try:
        result = upload.upload(_episode_dir(guid))
    except upload.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    upload.resolve_queue_item(guid)
    return result


@app.delete("/api/episodes/{guid}")
def delete_episode(guid: str) -> dict:
    """永久刪除單集（資料庫與本機檔案）。處理中的單集不可刪，否則背景執行緒會把目錄寫回來。"""
    directory = _episode_dir(guid)
    job = _read_job(guid)
    if job is not None and not job.done:
        raise HTTPException(status_code=409, detail="這集正在處理中，請等處理結束再刪除")

    try:
        result = upload.delete_episode(directory)
    except upload.UploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not result["deleted_db"] and not result["freed_bytes"]:
        raise HTTPException(status_code=404, detail="查無此單集")
    projects.remove_episode(guid)
    return result


@app.delete("/api/episodes/{guid}/audio")
def discard_audio(guid: str) -> dict:
    """刪除音檔釋出空間。僅在已上傳後可用。"""
    try:
        return upload.discard_audio(_episode_dir(guid))
    except upload.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/episodes/{guid}/audio")
def get_audio(guid: str) -> FileResponse:
    """提供 mp3 供回聽低信心段落。"""
    path = _episode_dir(guid) / "source.mp3"
    if not path.exists():
        raise HTTPException(status_code=404, detail="找不到音檔")
    return FileResponse(path, media_type="audio/mpeg")


# ── 研究專案 ────────────────────────────────────────
# 專案只存在資料庫，兩台電腦共用；未上傳的單集也能歸類。


@app.get("/api/projects")
def list_projects() -> list[dict]:
    """所有專案與各自的內容數，依名稱排序。"""
    try:
        return projects.list_projects()
    except projects.ProjectError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/projects")
def create_project(req: ProjectRequest) -> dict:
    try:
        return projects.create_project(req.name or "", req.description or "")
    except projects.ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/projects/{project_id}")
def update_project(project_id: str, req: ProjectRequest) -> dict:
    """修改專案名稱或說明；未帶的欄位不變。"""
    try:
        projects.update_project(project_id, name=req.name, description=req.description)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": project_id}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict:
    """刪除專案；歸類關係連帶刪除，單集本身不受影響。"""
    try:
        projects.delete_project(project_id)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": project_id}


@app.put("/api/episodes/{guid}/projects")
def set_episode_projects(guid: str, req: EpisodeProjectsRequest) -> dict:
    """設定這集所屬的專案（整組取代）。直接寫入資料庫，不需再次上傳。"""
    _load_episode(guid)  # 查無此單集時回 404，避免歸類到打錯的 guid
    try:
        return {"projects": projects.set_episode_projects(guid, req.project_ids)}
    except projects.ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/episodes/{guid}/pdf")
def get_paper_pdf(guid: str) -> Response:
    """論文的 PDF 原檔：本機尚未上傳的讀本機檔案，已上傳的從 Storage 取得。"""
    path = _episode_dir(guid) / pipeline.PAPER_PDF
    if path.exists():
        return FileResponse(path, media_type="application/pdf")
    if not guid.startswith("paper-"):
        raise HTTPException(status_code=404, detail="找不到 PDF 原檔")
    try:
        data = storage.fetch_paper(guid)
    except storage.StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # inline：瀏覽器分頁直接開啟，不另存檔案
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": "inline"})


def _episode_dir(guid: str) -> Path:
    """取得單集目錄，並擋下跳脫 audio/ 的路徑。"""
    directory = (audio.AUDIO_ROOT / guid).resolve()
    if not directory.is_relative_to(audio.AUDIO_ROOT.resolve()):
        raise HTTPException(status_code=400, detail="無效的單集代碼")
    return directory


@app.get("/")
def index() -> HTMLResponse:
    """首頁。css/js 以檔案修改時間戳記，改版後瀏覽器必定重新抓取。

    手動維護 ?v=N 容易忘記更新，導致改了樣式卻看到舊畫面；
    改由伺服器在回應時填入 mtime，存檔即換網址。
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("style.css", "app.js"):
        stamp = int((STATIC_DIR / name).stat().st_mtime)
        html = html.replace(f'"{name}"', f'"{name}?v={stamp}"')
    return HTMLResponse(html)


# 靜態檔案掛在最後，才不會蓋掉上面的 /api 路由與首頁。
# html=True 會讓 "/" 也交給 StaticFiles，故上面的 index 需先註冊。
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="web")
