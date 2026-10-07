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

// 心智圖改用 markmap 渲染（見 mindmap-render.js），透過 window.renderMindmap 呼叫。
// 資料仍存 mermaid 語法，由該模組轉譯，故此處不再需要 mermaid.initialize。

// ── 開始處理 ────────────────────────────────────────

$("start-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("url").value.trim();
  if (!url) return;

  const btn = e.target.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    await startJob({ url, kind: $("kind").value });
    $("url").value = "";
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
  }
});

/** 送出處理並切到該集；交給 hash 驅動，刷新後仍停在這一集。 */
async function startJob(body) {
  const job = await api("/api/process", { method: "POST", body });
  location.hash = job.guid;
  await loadLibrary();
}

// 付費文章或抓不到正文時，直接貼上全文。
$("btn-paste").addEventListener("click", () => $("paste-dialog").showModal());

$("paste-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "ok") return;
  e.preventDefault();
  const text = $("paste-text").value.trim();
  if (!text) return;

  const btn = $("btn-paste-submit");
  btn.disabled = true;
  try {
    await startJob({ text, title: $("paste-title").value.trim() });
    $("paste-dialog").close();
    $("paste-title").value = "";
    $("paste-text").value = "";
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
  // 回應到達前使用者已切到別集：丟棄，否則舊集會蓋掉新集的畫面。
  if (location.hash.slice(1) !== guid) return;
  current = { guid, ...data };

  $("empty").hidden = true;
  $("progress").hidden = true;
  $("episode").hidden = false;
  $("ep-title").textContent = data.episode.title;
  renderUploadState();

  const article = isArticle(data.episode);
  $("transcript-heading").textContent = article ? "原文" : "逐字稿";
  $("download-transcript-label").textContent = article ? "原文" : "逐字稿";

  renderSummary(data.summary);
  if (article) $("speaker-controls").innerHTML = "";
  else renderSpeakers(data);
  renderTranscript(data);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// 未寫的屬性沿用這組預設（線條插圖風格），被過濾掉屬性的元素才不會變成黑色實心
const COVER_SVG_DEFAULTS = 'viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"';

/** 摘要中的封面；svg 已由後端以白名單過濾（見 cover.py）。 */
function renderCover(cover) {
  const el = $("cover");
  el.hidden = !cover;
  $("cover-empty").hidden = !!cover;
  if (!cover) {
    el.innerHTML = "";
    return;
  }
  el.style.setProperty("--cover-color", cover.color);
  el.innerHTML = `<svg ${COVER_SVG_DEFAULTS} aria-hidden="true">${cover.svg}</svg>`;
}

function renderSummary(summary) {
  renderTagReview(summary);
  if (!summary) {
    renderCover(null);
    $("summary-text").textContent = "尚未生成";
    $("hashtags").innerHTML = "";
    $("mindmap").innerHTML = "";
    return;
  }

  renderCover(summary.cover);
  $("summary-text").textContent = summary.summary;
  $("hashtags").innerHTML = (summary.hashtags || [])
    .map((t) => `<span>#${escapeHtml(t)}</span>`)
    .join("");
  renderMindmap(summary.mindmap);
}

// ── 標籤合併確認 ────────────────────────────────────

function pendingMerges(summary) {
  return (summary?.hashtag_merges || []).filter((m) => m.keep == null);
}

/**
 * AI 建議把新標籤合併為既有標籤時，逐項由使用者決定保留哪一個。
 * 未決定前標籤維持原樣，且不能上傳（後端同樣會擋）。
 */
function renderTagReview(summary) {
  const box = $("tag-review");
  const merges = summary?.hashtag_merges || [];
  box.hidden = merges.length === 0;
  if (!merges.length) {
    box.innerHTML = "";
    return;
  }

  const pending = pendingMerges(summary).length;
  const head = pending
    ? `標籤合併待確認（剩 ${pending} 項），全部確認後才能上傳`
    : "標籤合併已確認，可再點選修改";
  const cell = (i, m, side) => {
    const chosen = m.keep === m[side] ? ' class="chosen"' : "";
    return `<td><button type="button"${chosen} data-i="${i}" data-side="${side}">${escapeHtml(m[side])}</button></td>`;
  };

  box.innerHTML = `<p class="tag-review-head${pending ? " pending" : ""}">${head}</p>
    <table>
      <thead><tr><th>保留原標籤</th><th>改用既有標籤</th></tr></thead>
      <tbody>${merges
        .map((m, i) => `<tr>${cell(i, m, "from")}${cell(i, m, "to")}</tr>`)
        .join("")}</tbody>
    </table>`;

  box.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const m = merges[Number(btn.dataset.i)];
      decideMerge(m.from, m[btn.dataset.side]);
    });
  });
}

async function decideMerge(from, keep) {
  try {
    const summary = await api(`/api/episodes/${current.guid}/hashtags`, {
      method: "PUT",
      body: { decisions: { [from]: keep } },
    });
    current.summary = summary;
    renderSummary(summary);
    renderUploadState();
  } catch (err) {
    alert(err.message);
  }
}

function renderMindmap(code) {
  const box = $("mindmap");
  if (typeof window.renderMarkmap !== "function") {
    box.innerHTML = `<p class="error">心智圖元件尚未載入</p>`;
    return;
  }
  try {
    // window.renderMarkmap 由 index.html 的 module 腳本注入（markmap 渲染）。
    // 頁面內是唯讀縮圖：不攔滾輪與拖曳，要縮放時點進全螢幕。
    window.renderMarkmap(code, box, { interactive: false });
  } catch (err) {
    box.innerHTML = `<p class="error">心智圖語法錯誤，請重新生成</p>`;
  }
}

// ── 心智圖全螢幕 ──────────────────────────────────
let fullMindmap = null;

function openMindmapFull() {
  const code = current?.summary?.mindmap;
  if (!code || typeof window.renderMarkmap !== "function") return;
  $("mindmap-overlay").hidden = false;
  document.body.classList.add("scroll-locked");
  fullMindmap?.destroy();
  // 必須在 overlay 顯示後才渲染，markmap 依容器實際尺寸 fit。
  try {
    fullMindmap = window.renderMarkmap(code, $("mindmap-full"));
  } catch (err) {
    closeMindmapFull();
  }
}

function closeMindmapFull() {
  if ($("mindmap-overlay").hidden) return;
  $("mindmap-overlay").hidden = true;
  document.body.classList.remove("scroll-locked");
  fullMindmap?.destroy();
  fullMindmap = null;
  $("mindmap-full").innerHTML = "";
}

$("btn-mindmap-full").addEventListener("click", (e) => {
  e.stopPropagation();
  openMindmapFull();
});
// 點縮圖任一處也進全螢幕；點節點的圓點仍是展開/收合。
$("mindmap").addEventListener("click", (e) => {
  if (!e.target.closest("circle")) openMindmapFull();
});
$("btn-mindmap-close").addEventListener("click", closeMindmapFull);
$("btn-mindmap-reset").addEventListener("click", () => fullMindmap?.fit());
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeMindmapFull();
});
window.addEventListener("resize", () => fullMindmap?.fit());

function renderSpeakers(data) {
  const ids = [...new Set(data.segments.map((s) => s.speaker))].sort();
  // 輸入框共用同一個 datalist：列出這個節目以前用過的人名，也可自行輸入。
  $("speaker-controls").innerHTML =
    ids
      .map(
        (id) =>
          `<input data-speaker="${id}" list="known-speakers" autocomplete="off" value="${escapeHtml(data.speakers[id] || "")}" placeholder="${id}">`
      )
      .join("") + `<datalist id="known-speakers"></datalist>`;
  renderKnownSpeakers(data.known_speakers || []);

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
  // 剛取的新名字也加進清單，其他說話者不必等重新載入就能選。
  const known = current.known_speakers || [];
  current.known_speakers = [
    ...known,
    ...Object.values(res.speakers).filter((name) => !known.includes(name)),
  ];
  renderKnownSpeakers(current.known_speakers);
  renderTranscript(current);
}

function renderKnownSpeakers(names) {
  $("known-speakers").innerHTML = names
    .map((name) => `<option value="${escapeHtml(name)}"></option>`)
    .join("");
}

function renderTranscript(data) {
  renderChapters(data);
  const article = isArticle(data.episode);
  const chapters = data.summary?.chapters || [];
  const startsAt = chapterStarts(data.segments, chapters, article);
  // 每章包成一個 section：標題 sticky 只在所屬 section 內固定，捲到下一章時被推走。
  const html = [];
  data.segments.forEach((s, i) => {
    const index = startsAt.get(i);
    if (index !== undefined) {
      if (i > 0) html.push("</section>");
      const time = article ? "" : `<span class="chapter-time">${formatTime(s.start)}</span>`;
      html.push(`<section class="chapter" id="chapter-${index}">
        <h4 class="chapter-title">
          ${time}
          <span>${escapeHtml(chapters[index].title)}</span>
        </h4>`);
    } else if (i === 0 && chapters.length) {
      html.push(`<section class="chapter">`);
    }
    html.push(article ? `<p>${escapeHtml(s.text)}</p>` : segmentHtml(data, s));
  });
  if (chapters.length) html.push("</section>");
  $("transcript").innerHTML = article
    ? `<div class="article-text">${html.join("")}</div>`
    : html.join("");

  $("transcript").querySelectorAll("span.seg-time").forEach((el) => {
    el.addEventListener("click", () => playAt(Number(el.dataset.at)));
  });
}

/**
 * 各章從第幾段開始：{段落索引: 章節索引}。
 * 後端已對齊，Podcast 章節的 start 等於某一段的 start；文章章節的 paragraph 即段落索引。
 */
function chapterStarts(segments, chapters, article) {
  const bySegment = new Map();
  chapters.forEach((c, i) => {
    const seg = article ? c.paragraph : segments.findIndex((s) => s.start === c.start);
    if (seg >= 0 && !bySegment.has(seg)) bySegment.set(seg, i);
  });
  return bySegment;
}

function segmentHtml(data, s) {
  const name = data.speakers[s.speaker] || s.speaker;
  const low = s.confidence < 0.6 ? " low" : "";
  // YouTube 集數的時間戳直接開影片跳到該處；其他平台維持回聽本機音檔。
  const youtube = youtubeTimeUrl(data.episode, s.start);
  const time = youtube
    ? `<a class="seg-time" href="${escapeHtml(youtube)}" target="_blank" rel="noopener noreferrer" title="在 YouTube 從這裡播放">${formatTime(s.start)}</a>`
    : `<span class="seg-time" data-at="${s.start}">${formatTime(s.start)}</span>`;
  return `<div class="seg${low}">
    <div class="seg-head">
      ${time}
      <span class="seg-speaker">${escapeHtml(name)}</span>
    </div>
    <p>${escapeHtml(s.text)}</p>
  </div>`;
}

// ── 章節 ────────────────────────────────────────────

/** 逐字稿上方的章節目錄；沒有章節時（含文章）不顯示。 */
function renderChapters(data) {
  const el = $("chapters");
  const chapters = data.summary?.chapters || [];
  el.hidden = !chapters.length;
  if (el.hidden) return;

  el.innerHTML = `<details class="chapters-toc" open>
    <summary>章節（${chapters.length}）</summary>
    <ol>${chapters
      .map((c, i) => {
        const youtube = youtubeTimeUrl(data.episode, c.start);
        const play = youtube
          ? `<a class="chapter-play" href="${escapeHtml(youtube)}" target="_blank" rel="noopener noreferrer">YouTube ↗</a>`
          : "";
        // 文章章節以段落定位，沒有時間
        const time = isArticle(data.episode) ? "" : `<span class="chapter-time">${formatTime(c.start)}</span>`;
        return `<li>
          ${time}
          <a href="#" data-chapter="${i}">${escapeHtml(c.title)}</a>
          ${play}
        </li>`;
      })
      .join("")}</ol>
  </details>`;

  el.querySelectorAll("a[data-chapter]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      $(`chapter-${a.dataset.chapter}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
}

const PLATFORM_LABELS = { apple: "Apple Podcasts", youtube: "YouTube", article: "原網站" };

function isArticle(episode) {
  return episode?.platform === "article";
}

/**
 * 只接受 https 網址。source_url 會放進 href，
 * 擋掉 javascript: 等 scheme（Apple 的網址判斷只看是否含網域字串）。
 */
function safeSourceUrl(url) {
  return /^https:\/\//i.test(url || "") ? url : null;
}

/** YouTube 單集指定秒數的影片連結；非 YouTube 回傳 null。 */
function youtubeTimeUrl(episode, seconds) {
  if (episode.platform !== "youtube") return null;
  const source = safeSourceUrl(episode.source_url);
  if (!source) return null;
  const url = new URL(source);
  url.searchParams.set("t", `${Math.floor(seconds)}s`);
  return url.href;
}

function renderSourceLink(episode) {
  const link = $("ep-source");
  const url = safeSourceUrl(episode.source_url);
  link.hidden = !url;
  if (!url) return;
  link.href = url;
  link.textContent = `在 ${PLATFORM_LABELS[episode.platform] || "原平台"} 開啟 ↗`;
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
    renderTranscript(current);
    renderUploadState();
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "重新生成全部";
  }
});

$("btn-regen-cover").addEventListener("click", async (e) => {
  const btn = e.target;
  if (!current.summary?.summary) {
    alert("請先產生摘要");
    return;
  }
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    const { cover } = await api(`/api/episodes/${current.guid}/cover`, { method: "POST" });
    current.summary = { ...current.summary, cover };
    renderCover(cover);
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "重新生成封面";
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
    // 後端在上傳成功時把對應的待處理項目標記為完成，這裡刷新讓它消失
    await Promise.all([loadLibrary(), loadQueue()]);

    const freed = res.freed_bytes
      ? `，釋出 ${(res.freed_bytes / 1048576).toFixed(0)} MB`
      : "";
    btn.textContent = `${res.inserted ? "已上傳" : "已更新"}${freed}`;
    setTimeout(renderUploadState, 3000);
  } catch (err) {
    alert(err.message);
    renderUploadState();
  } finally {
    btn.disabled = pendingMerges(current?.summary).length > 0;
  }
});

// ── 刪除單集 ────────────────────────────────────────

$("btn-delete").addEventListener("click", async (e) => {
  const uploaded = Boolean(current.uploaded_at);
  const warning = uploaded
    ? "資料庫與本機檔案都會刪除，手機端也會看不到，所有人的收藏一併移除。"
    : isArticle(current.episode)
      ? "本機檔案（原文與摘要）都會刪除。"
      : "本機檔案（含音檔與轉錄結果）都會刪除。";
  if (!confirm(`確定要刪除「${current.episode.title}」？\n${warning}\n此動作無法復原。`)) return;

  const btn = e.target;
  btn.disabled = true;
  try {
    await api(`/api/episodes/${current.guid}`, { method: "DELETE" });
    current = null;
    location.hash = "";
    await loadLibrary();
  } catch (err) {
    alert(err.message);
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
    uploaded ? `已上傳 ${current.uploaded_at.slice(0, 10)}` : "尚未上傳",
    current.has_audio === false && !isArticle(current.episode) ? "音檔已刪除" : "",
  ].filter(Boolean);
  $("ep-meta").textContent = meta.join(" · ");
  renderSourceLink(current.episode);

  $("btn-upload").textContent = uploaded ? "再次上傳" : "上傳";
  const pending = pendingMerges(current.summary).length > 0;
  $("btn-upload").disabled = pending;
  $("btn-upload").title = pending ? "標籤合併尚未確認" : "";
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
  const chapters = d.summary?.chapters || [];
  const startsAt = chapterStarts(d.segments, chapters, isArticle(d.episode));
  const heading = (i) => (startsAt.has(i) ? [`### ${chapters[startsAt.get(i)].title}`, ""] : []);
  if (parts.includes("transcript") && isArticle(d.episode)) {
    lines.push("## 原文", "");
    if (safeSourceUrl(d.episode.source_url)) lines.push(d.episode.source_url, "");
    d.segments.forEach((s, i) => lines.push(...heading(i), s.text, ""));
  } else if (parts.includes("transcript")) {
    lines.push("## 逐字稿", "");
    d.segments.forEach((s, i) => {
      const name = d.speakers[s.speaker] || s.speaker;
      lines.push(...heading(i), `**[${formatTime(s.start)}] ${name}**`, "", s.text, "");
    });
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

// ── 手機待處理 ──────────────────────────────────────
// 手機端只存網址，解析、下載與轉錄都在這裡手動觸發。

// null 代表尚未載入；loadLibrary 先於 loadQueue 完成時據此跳過重畫，
// 避免整區先隱藏再出現的閃動。
let queueItems = null;

async function loadQueue() {
  try {
    queueItems = await api("/api/queue");
  } catch (err) {
    // 待處理是附屬功能，資料庫連不上時不該讓左側清單整個掛掉
    console.error(err);
    return;
  }
  renderQueue();
}

function renderQueue() {
  if (queueItems === null) return;  // 尚未載入，交給 loadQueue 完成時再畫

  $("queue-box").hidden = queueItems.length === 0;
  $("queue-count").textContent = queueItems.length;

  const list = $("queue-list");
  list.innerHTML = "";

  queueItems.forEach((item) => {
    // 開始處理時後端已回填 episode_guid，據此比對該集目前的狀態。
    // 已在處理中就不該能再按一次，否則會重複送出同一集。
    const episode = item.episode_guid
      ? allEpisodes.find((e) => e.guid === item.episode_guid)
      : null;
    const processing = Boolean(episode && episode.processing);
    const failed = Boolean(episode && episode.error);
    // 已處理完成但尚未上傳：項目要到上傳成功才結案，這段期間不能再開始處理，
    // 改成「查看」直接跳到該集。
    const done = Boolean(episode && episode.ready && !processing && !failed);

    const li = document.createElement("li");
    li.innerHTML = `
      <div class="queue-item-body">
        <span class="queue-item-title">${escapeHtml(item.title || item.url)}</span>
        ${item.note ? `<span class="queue-item-note">${escapeHtml(item.note)}</span>` : ""}
        ${processing ? `<span class="queue-item-status">處理中…${escapeHtml(episode.message || "")}</span>` : ""}
        ${failed ? `<span class="queue-item-status failed">處理失敗，可再試一次</span>` : ""}
        ${done ? `<span class="queue-item-status">已處理完成，待上傳</span>` : ""}
        <div class="queue-actions">
          ${
            done
              ? `<button type="button" class="view">查看</button>`
              : `<button type="button" class="go"${processing ? " disabled" : ""}>${failed ? "▶ 重新處理" : "▶ 開始處理"}</button>`
          }
          <button type="button" class="del"${processing ? " disabled" : ""}>移除</button>
        </div>
      </div>
    `;

    li.querySelector(".view")?.addEventListener("click", () => {
      location.hash = item.episode_guid;
      closeSidebar();
    });

    li.querySelector(".go")?.addEventListener("click", async (e) => {
      const buttons = li.querySelectorAll("button");
      buttons.forEach((b) => (b.disabled = true));
      try {
        // 帶 queue_id 讓後端回填解析出的標題；結案在上傳成功時才做。
        const job = await api("/api/process", {
          method: "POST",
          body: { url: item.url, queue_id: item.id },
        });
        location.hash = job.guid;
        await Promise.all([loadLibrary(), loadQueue()]);
      } catch (err) {
        alert(err.message);
        buttons.forEach((b) => (b.disabled = false));
      }
    });

    li.querySelector(".del").addEventListener("click", async () => {
      try {
        await api(`/api/queue/${item.id}`, { method: "DELETE" });
        queueItems = queueItems.filter((i) => i.id !== item.id);
        renderQueue();
      } catch (err) {
        alert(err.message);
      }
    });

    list.appendChild(li);
  });
}

async function loadLibrary() {
  allEpisodes = await api("/api/episodes");
  renderLibrary();
  // 待處理項目的「處理中」狀態取自 allEpisodes，故一併重畫；
  // 轉錄期間靠下面的輪詢，進度會跟著更新。
  renderQueue();

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

// 內容類型：YouTube 為影片、文章為文章，其餘平台為音檔（與手機卡片相同）
const EP_KINDS = {
  audio: { icon: "ic-audio", label: "音檔" },
  video: { icon: "ic-video", label: "影片" },
  article: { icon: "ic-article", label: "文章" },
};

function episodeKind(e) {
  return e.kind === "article" ? "article" : e.platform === "youtube" ? "video" : "audio";
}

/** 清單卡片左側封面；svg 已由後端過濾。沒有封面時顯示灰色類型 icon。 */
function listCoverHtml(e) {
  if (!e.cover?.svg) {
    return `<div class="ep-cover ep-cover-empty"><svg class="kind-icon"><use href="#${EP_KINDS[episodeKind(e)].icon}"/></svg></div>`;
  }
  return `<div class="ep-cover" style="--cover-color:${escapeHtml(e.cover.color)}"><svg ${COVER_SVG_DEFAULTS} aria-hidden="true">${e.cover.svg}</svg></div>`;
}

function episodeRow(e, selected) {
  const { icon, label } = EP_KINDS[episodeKind(e)];
  const meta = [
    escapeHtml(e.podcast_name),
    (e.published_at || "").slice(0, 10),
    e.duration_sec ? `${Math.round(e.duration_sec / 60)} 分鐘` : "",
  ]
    .filter(Boolean)
    // 每段不斷行，窄欄換行時只在「·」之間換，不會把「61 分鐘」拆開
    .map((part) => `<span class="nowrap">${part}</span>`)
    .join(" · ");

  // 處理進度與待辦提示另起一行，與手機卡片的版面一致
  const status = e.processing
    ? `<span class="ep-status"><span class="spinner"></span> ${escapeHtml(
        STAGE_LABELS[e.stage] || e.stage
      )}中${e.percent != null ? ` ${e.percent}%` : "…"}</span>`
    : e.error
      ? `<span class="ep-status ep-error">未完成，點擊繼續</span>`
      : !e.has_summary
        ? `<span class="ep-status muted">未生成摘要</span>`
        : "";

  const tags = (e.hashtags || []).map((t) => `<span>#${escapeHtml(t)}</span>`).join("");
  return `<li data-guid="${e.guid}" class="ep-card${selected ? " selected" : ""}">
    ${listCoverHtml(e)}
    <div class="ep-body">
      <span class="ep-name">${escapeHtml(e.title)}</span>
      ${meta ? `<span class="ep-meta"><svg class="kind-icon" role="img" aria-label="${label}"><use href="#${icon}"/></svg>${meta}</span>` : ""}
      ${status}
      ${tags ? `<span class="ep-tags">${tags}</span>` : ""}
    </div>
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
    if (location.hash.slice(1) !== guid) return; // 已切到別集，錯誤不影響目前畫面
    $("empty").hidden = false;
    $("progress").hidden = true;
    $("episode").hidden = true;
  }
}

window.addEventListener("hashchange", openFromHash);

loadLibrary().then(openFromHash);
loadQueue();
