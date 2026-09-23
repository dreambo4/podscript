const STAGE_LABELS = {
  resolve: "解析",
  download: "下載",
  transcribe: "轉錄",
  diarize: "分離",
  merge: "對齊",
  summarize: "摘要",
  done: "完成",
};

const $ = (id) => document.getElementById(id);
let current = null;
let audioEl = null;

// 分支色不透過 themeVariables 的 primaryColor 自動推算（該推算以 primaryColor
// 明度為基準，深色系種子會讓所有分支色階塌陷成同一種近黑色），
// 改用 CSS 直接指定 mermaid 產生的 .section-N / .section-edge-N，見 style.css。
// maxNodeWidth 縮窄節點寬度換取分支間距，避免節點多時彼此交疊。
mermaid.initialize({
  startOnLoad: false,
  theme: "base",
  themeVariables: {
    fontFamily: '"PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif',
  },
  mindmap: { padding: 16, maxNodeWidth: 120 },
  fontSize: 14,
});

// ── 開始處理 ────────────────────────────────────────

$("start-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("url").value.trim();
  if (!url) return;

  const btn = e.target.querySelector("button");
  btn.disabled = true;
  try {
    const job = await api("/api/process", { method: "POST", body: { url } });
    $("url").value = "";
    // 交給 hash 驅動：網址成為唯一狀態來源，刷新後仍停在這一集。
    location.hash = job.guid;
    await loadLibrary();
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
  }
});

function showProgress(job) {
  $("empty").hidden = true;
  $("progress").hidden = false;
  $("episode").hidden = true;
  $("progress-title").textContent = job.title;

  const failed = Boolean(job.error);
  const running = !job.done && !failed;

  $("progress-message").textContent = failed
    ? `處理失敗：${job.error}`
    : job.percent != null
      ? `${job.message} ${job.percent}%`
      : job.message;
  $("progress-message").className = failed ? "error" : "";
  $("spinner").hidden = !running;
  $("progress-elapsed").textContent = running ? elapsed(job.started_at) : "";

  // 中斷或失敗時可直接接續，已下載與已轉錄的階段會自動跳過。
  const resume = $("btn-resume");
  resume.hidden = !failed || !job.url;
  resume.dataset.guid = job.guid;

  const index = job.stages.indexOf(job.stage);
  $("stages").innerHTML = job.stages
    .map((s, i) => {
      const cls = failed ? "" : i < index ? "past" : i === index ? "active" : "";
      return `<li class="${cls}">${STAGE_LABELS[s] || s}</li>`;
    })
    .join("");

  // 轉錄有實際百分比；其他階段長短不一，以流動條紋表示進行中。
  const fill = $("bar-fill");
  if (failed) {
    fill.className = "bar-fill";
    fill.style.width = "0";
  } else if (job.percent != null) {
    fill.className = "bar-fill";
    fill.style.width = `${job.percent}%`;
  } else {
    fill.className = "bar-fill indeterminate";
    fill.style.width = "";
  }
}

$("btn-resume").addEventListener("click", async (e) => {
  const btn = e.target;
  const guid = btn.dataset.guid;
  btn.disabled = true;
  try {
    await api(`/api/jobs/${guid}/resume`, { method: "POST" });
    await poll(guid);
    await loadLibrary();
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
  }
});

/** 已經跑了多久，讓使用者判斷是否卡住。 */
function elapsed(startedAt) {
  if (!startedAt) return "";
  const seconds = Math.max(0, (Date.now() - new Date(startedAt)) / 1000);
  const m = Math.floor(seconds / 60);
  return m < 1 ? "剛開始" : `已經過 ${m} 分鐘`;
}

/** 追蹤一集的處理進度；同一時間只追一集，切換單集會自動停止前一個。 */
let pollTimer = null;

/** 每集只自動接續一次，避免真正的失敗造成無限重試。 */
const resumed = new Set();

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

async function poll(guid) {
  stopPolling();

  let job;
  try {
    job = await api(`/api/jobs/${guid}`);
  } catch {
    return false; // 沒有任務紀錄，代表這集是直接開啟的既有結果
  }

  // 使用者已切到別集，停止更新畫面。
  if (location.hash.slice(1) !== guid) return true;

  if (!job.done) {
    showProgress(job);
    pollTimer = setTimeout(() => poll(guid), 30000);
    return true;
  }

  // 中斷的任務（多半是服務重啟）直接接續，不必使用者介入；
  // 已完成的階段會因檔案存在而跳過，不會重跑。
  if (job.error && job.url && !resumed.has(guid)) {
    resumed.add(guid);
    try {
      await api(`/api/jobs/${guid}/resume`, { method: "POST" });
      return poll(guid);
    } catch {
      // 接續失敗就照常顯示錯誤，交給使用者決定
    }
  }

  if (job.error) {
    showProgress(job);
    return true;
  }

  $("progress").hidden = true;
  await showEpisode(guid);
  await loadLibrary();
  return true;
}

// ── 顯示單集 ────────────────────────────────────────

/** 決定該顯示進度還是結果：有進行中的任務就追進度，否則直接顯示內容。 */
async function openEpisode(guid) {
  const tracked = await poll(guid);
  if (!tracked) await showEpisode(guid);
}

async function showEpisode(guid) {
  const data = await api(`/api/episodes/${guid}`);
  current = { guid, ...data };

  $("empty").hidden = true;
  $("progress").hidden = true;
  $("episode").hidden = false;
  $("ep-title").textContent = data.episode.title;
  renderUploadState();

  renderSummary(data.summary);
  renderSpeakers(data);
  renderTranscript(data);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderSummary(summary) {
  if (!summary) {
    $("summary-text").textContent = "尚未生成";
    $("hashtags").innerHTML = "";
    $("mindmap").innerHTML = "";
    return;
  }

  $("summary-text").textContent = summary.summary;
  $("hashtags").innerHTML = (summary.hashtags || [])
    .map((t) => `<span>#${escapeHtml(t)}</span>`)
    .join("");
  renderMindmap(summary.mindmap);
}

async function renderMindmap(code) {
  const box = $("mindmap");
  if (!code) {
    box.innerHTML = "";
    return;
  }
  try {
    const { svg } = await mermaid.render("mm" + Date.now(), code);
    box.innerHTML = svg;
  } catch (err) {
    box.innerHTML = `<p class="error">心智圖語法錯誤，請重新生成</p>`;
  }
}

function renderSpeakers(data) {
  const ids = [...new Set(data.segments.map((s) => s.speaker))].sort();
  $("speaker-controls").innerHTML = ids
    .map(
      (id) =>
        `<input data-speaker="${id}" value="${escapeHtml(data.speakers[id] || "")}" placeholder="${id}">`
    )
    .join("");

  $("speaker-controls").querySelectorAll("input").forEach((input) => {
    input.addEventListener("change", saveSpeakers);
  });
}

async function saveSpeakers() {
  const speakers = {};
  $("speaker-controls").querySelectorAll("input").forEach((input) => {
    speakers[input.dataset.speaker] = input.value;
  });

  const res = await api(`/api/episodes/${current.guid}/speakers`, {
    method: "PUT",
    body: { speakers },
  });
  current.speakers = res.speakers;
  renderTranscript(current);
}

function renderTranscript(data) {
  $("transcript").innerHTML = data.segments
    .map((s) => {
      const name = data.speakers[s.speaker] || s.speaker;
      const low = s.confidence < 0.6 ? " low" : "";
      return `<div class="seg${low}">
        <div class="seg-head">
          <span class="seg-time" data-at="${s.start}">${formatTime(s.start)}</span>
          <span class="seg-speaker">${escapeHtml(name)}</span>
        </div>
        <p>${escapeHtml(s.text)}</p>
      </div>`;
    })
    .join("");

  $("transcript").querySelectorAll(".seg-time").forEach((el) => {
    el.addEventListener("click", () => playAt(Number(el.dataset.at)));
  });
}

/** 點時間戳回聽原音，用於確認低信心段落。 */
function playAt(seconds) {
  if (!audioEl) {
    audioEl = new Audio(`/api/episodes/${current.guid}/audio`);
  }
  audioEl.currentTime = seconds;
  audioEl.play();
}

// ── 重新生成 ────────────────────────────────────────

$("btn-regen").addEventListener("click", async (e) => {
  const btn = e.target;
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    const summary = await api(`/api/episodes/${current.guid}/regenerate`, {
      method: "POST",
    });
    current.summary = summary;
    renderSummary(summary);
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "🔄 重新生成全部";
  }
});

// ── 上傳 Supabase ───────────────────────────────────

$("btn-upload").addEventListener("click", async (e) => {
  const btn = e.target;
  btn.disabled = true;
  btn.textContent = "上傳中…";
  try {
    const res = await api(`/api/episodes/${current.guid}/upload`, {
      method: "POST",
    });
    current.uploaded_at = new Date().toISOString();
    current.has_audio = false; // 上傳成功後音檔已自動清除
    renderUploadState();
    await loadLibrary();

    const freed = res.freed_bytes
      ? `，釋出 ${(res.freed_bytes / 1048576).toFixed(0)} MB`
      : "";
    btn.textContent = `✅ ${res.inserted ? "已上傳" : "已更新"}${freed}`;
    setTimeout(renderUploadState, 3000);
  } catch (err) {
    alert(err.message);
    renderUploadState();
  } finally {
    btn.disabled = false;
  }
});

/** 依上傳與音檔狀態更新中繼資料列與按鈕。 */
function renderUploadState() {
  const uploaded = Boolean(current.uploaded_at);
  const meta = [
    current.episode.podcast_name,
    (current.episode.published_at || "").slice(0, 10),
    current.episode.duration_sec
      ? `${Math.round(current.episode.duration_sec / 60)} 分鐘`
      : "",
    current.provenance?.transcribe_model,
    uploaded ? `☁ 已上傳 ${current.uploaded_at.slice(0, 10)}` : "尚未上傳",
    current.has_audio === false ? "音檔已刪除" : "",
  ].filter(Boolean);
  $("ep-meta").textContent = meta.join(" · ");

  $("btn-upload").textContent = uploaded ? "☁ 再次上傳" : "☁ 上傳";
}

// ── 下載 ────────────────────────────────────────────

$("btn-download").addEventListener("click", () => $("download-dialog").showModal());

$("btn-do-download").addEventListener("click", () => {
  const dialog = $("download-dialog");
  const parts = [...dialog.querySelectorAll("[name=part]:checked")].map((i) => i.value);
  const format = dialog.querySelector("[name=format]:checked").value;
  dialog.close();

  if (format === "pdf") {
    window.print();
    return;
  }
  downloadMarkdown(parts);
});

function downloadMarkdown(parts) {
  const d = current;
  const lines = [`# ${d.episode.title}`, ""];
  lines.push(`${d.episode.podcast_name} · ${(d.episode.published_at || "").slice(0, 10)}`, "");

  if (parts.includes("summary") && d.summary) {
    lines.push("## 摘要", "", d.summary.summary, "");
    if (d.summary.hashtags?.length) {
      lines.push(d.summary.hashtags.map((t) => `#${t}`).join(" "), "");
    }
  }
  if (parts.includes("mindmap") && d.summary?.mindmap) {
    lines.push("## 心智圖", "", "```mermaid", d.summary.mindmap, "```", "");
  }
  if (parts.includes("transcript")) {
    lines.push("## 逐字稿", "");
    for (const s of d.segments) {
      const name = d.speakers[s.speaker] || s.speaker;
      lines.push(`**[${formatTime(s.start)}] ${name}**`, "", s.text, "");
    }
  }

  const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${d.episode.title.replace(/[/\\?%*:|"<>]/g, "-")}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
}

// ── 本機清單 ────────────────────────────────────────

let libraryTimer = null;
let allEpisodes = [];

async function loadLibrary() {
  allEpisodes = await api("/api/episodes");
  renderLibrary();

  // 有任務在跑就定期刷新清單，讓狀態自動更新。
  if (allEpisodes.some((e) => e.processing)) {
    clearTimeout(libraryTimer);
    libraryTimer = setTimeout(loadLibrary, 5000);
  }
}

function renderLibrary() {
  const keyword = $("filter").value.trim().toLowerCase();
  const shown = keyword
    ? allEpisodes.filter((e) =>
        [e.title, e.podcast_name, ...(e.hashtags || [])]
          .join(" ")
          .toLowerCase()
          .includes(keyword)
      )
    : allEpisodes;

  const selected = location.hash.slice(1);
  const groups = [
    ["處理中", shown.filter((e) => e.processing)],
    ["未上傳", shown.filter((e) => !e.processing && !e.uploaded_at)],
    ["已上傳", shown.filter((e) => !e.processing && e.uploaded_at)],
  ];

  const html = groups
    .filter(([, items]) => items.length)
    .map(
      ([label, items]) =>
        `<li class="group">${label} <span class="count">${items.length}</span></li>` +
        items.map((e) => episodeRow(e, e.guid === selected)).join("")
    )
    .join("");

  $("episode-list").innerHTML =
    html || `<li class="muted">${keyword ? "沒有符合的單集" : "還沒有處理過的單集"}</li>`;

  $("episode-list").querySelectorAll("li[data-guid]").forEach((li) => {
    li.addEventListener("click", () => {
      location.hash = li.dataset.guid;
      closeSidebar();
    });
  });
}

$("filter").addEventListener("input", renderLibrary);

// ── 側邊欄（窄螢幕） ────────────────────────────────

$("btn-menu").addEventListener("click", () => {
  $("sidebar").classList.toggle("open");
});

function closeSidebar() {
  $("sidebar").classList.remove("open");
}

function episodeRow(e, selected) {
  const meta = e.processing
    ? `<span class="ep-status"><span class="spinner"></span> ${escapeHtml(
        STAGE_LABELS[e.stage] || e.stage
      )}中${e.percent != null ? ` ${e.percent}%` : "…"}</span>`
    : `<span class="${e.error ? "ep-error" : "muted"}">${[
        (e.published_at || "").slice(0, 10),
        e.error
          ? "⚠️ 未完成，點擊繼續"
          : !e.has_summary
            ? "未生成摘要"
            : !e.has_audio
              ? "已釋出空間"
              : "",
      ]
        .filter(Boolean)
        .join(" · ")}</span>`;

  return `<li data-guid="${e.guid}" class="${selected ? "selected" : ""}">
    <span class="ep-name">${escapeHtml(e.title)}</span>
    ${meta}
  </li>`;
}

// ── 工具 ────────────────────────────────────────────

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `請求失敗（${res.status}）`);
  }
  return res.json();
}

function formatTime(seconds) {
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

/** 網址是唯一的狀態來源：#guid 決定顯示哪一集，重新整理後不變。 */
async function openFromHash() {
  const guid = location.hash.slice(1);
  renderLibrary(); // 更新選中高亮

  if (!guid) {
    stopPolling();
    $("empty").hidden = false;
    $("progress").hidden = true;
    $("episode").hidden = true;
    return;
  }

  $("empty").hidden = true;
  try {
    await openEpisode(guid);
  } catch {
    $("empty").hidden = false;
    $("progress").hidden = true;
    $("episode").hidden = true;
  }
}

window.addEventListener("hashchange", openFromHash);

loadLibrary().then(openFromHash);
