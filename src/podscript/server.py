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

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, notify, pipeline, upload
from .resolvers import ResolveError, resolve

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
            "stage": self.stage,
            "message": self.message,
            "percent": self.percent,
            "done": self.done,
            "error": self.error,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "stages": ["resolve", *pipeline.STAGES],
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
    url: str
    force: str | None = None
    num_speakers: int | None = None
    queue_id: str | None = None  # 由待處理清單觸發時帶入，供回填標題與結案


class SpeakersRequest(BaseModel):
    speakers: dict[str, str]


class HashtagDecisionRequest(BaseModel):
    # {新標籤: 要保留的標籤}，值為合併前的新標籤或建議的既有標籤。
    decisions: dict[str, str]


@app.post("/api/process")
def start_process(req: ProcessRequest) -> dict:
    """解析網址並在背景開始處理。"""
    try:
        episode = resolve(req.url)
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
        job = Job(guid=guid, title=episode.title, url=req.url)
        job.save()
        _jobs[guid] = job

    thread = threading.Thread(
        target=_run,
        args=(job, req.url, req.force, req.num_speakers),
        daemon=True,
    )
    thread.start()
    return job.to_dict()


def _run(job: Job, url: str, force: str | None, num_speakers: int | None) -> None:
    """背景執行整條流程，把進度寫回 job。"""

    def on_progress(stage: str, message: str, percent: int | None = None) -> None:
        job.stage = stage
        job.message = message
        job.percent = percent
        # 百分比每 1% 更新一次，寫檔過於頻繁；僅在階段切換或每 5% 落地。
        if percent is None or percent % 5 == 0:
            job.save()

    try:
        pipeline.process(
            url,
            model=os.environ.get("WHISPER_MODEL", "large-v2"),
            force=force,
            num_speakers=num_speakers,
            on_progress=on_progress,
        )
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
    if not previous.url:
        raise HTTPException(
            status_code=400, detail="這筆紀錄沒有原始網址，請重新貼上網址"
        )

    with _lock:
        running = _jobs.get(guid)
        if running and not running.done:
            return running.to_dict()
        job = Job(guid=guid, title=previous.title, url=previous.url)
        job.save()
        _jobs[guid] = job

    thread = threading.Thread(
        target=_run, args=(job, previous.url, None, None), daemon=True
    )
    thread.start()
    return job.to_dict()


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
    # 本機尚未上傳者的上傳時間需另外查詢。
    missing = [e["guid"] for e in items if not e.get("uploaded_at")]
    times = upload.uploaded_times(missing)
    for item in items:
        item.setdefault("uploaded_at", None)
        if not item["uploaded_at"]:
            item["uploaded_at"] = times.get(item["guid"])

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
        "hashtags": summary_data.get("hashtags", []),
        "has_summary": bool(summary_data.get("summary")),
        "ready": result is not None,
        "processing": bool(job and not job.done),
        "has_audio": bool(directory and (directory / "source.mp3").exists()),
        "stage": job.stage if job else "",
        "message": job.message if job else "",
        "error": job.error if job else "",
    }


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
        "known_speakers": _known_speakers(result.episode.podcast_name),
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
