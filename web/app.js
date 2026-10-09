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

// 論文：選 PDF 檔後直接上傳，全文擷取與摘要在本機進行。
$("btn-pdf").addEventListener("click", () => $("pdf-file").click());

$("pdf-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = ""; // 同一個檔案再選一次也要觸發
  if (!file) return;

  const btn = $("btn-pdf");
  btn.disabled = true;
  btn.textContent = "上傳中…";
  try {
    const res = await fetch(`/api/papers?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/pdf" },
      body: file,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `上傳失敗（${res.status}）`);
    }
    const job = await res.json();
    location.hash = job.guid;
    await loadLibrary();
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "上傳論文";
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
  resume.hidden = !failed || !canResume(job);
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

/** 論文沒有網址，由本機的 PDF 接續；其他內容要有原始網址才能接續。 */
function canResume(job) {
  return Boolean(job.url) || job.kind === "paper";
}

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
  if (job.error && canResume(job) && !resumed.has(guid)) {
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
  // 換到另一集時原文回到預設的中文
  if (current?.guid !== guid) transcriptLang = "zh";
  current = { guid, ...data };

  $("empty").hidden = true;
  $("progress").hidden = true;
  $("episode").hidden = false;
  $("ep-title").textContent = data.episode.title;
  $("ep-title-zh").textContent = data.episode.title_translated || "";
  $("ep-title-zh").hidden = !data.episode.title_translated;
  renderUploadState();

  const text = isText(data.episode);
  $("transcript-heading").textContent = text ? "原文" : "逐字稿";
  $("download-transcript-label").textContent = text ? "原文" : "逐字稿";

  renderSummary(data.summary);
  renderEpisodeProjects();
  renderTranslateControls();
  if (isPaper(data.episode)) watchTranslation(guid);
  if (text) $("speaker-controls").innerHTML = "";
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
  const article = isText(data.episode);
  const paper = isPaper(data.episode);
  const chapters = localizedChapters(data);
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
    // 論文的章標題就是章節標題，上面已顯示，不重複
    if (paper && index !== undefined && s.kind === "h1") return;
    html.push(paper ? paperSegmentHtml(s, segmentText(s)) : article ? `<p>${escapeHtml(s.text)}</p>` : segmentHtml(data, s));
  });
  if (chapters.length) html.push("</section>");
  $("transcript").innerHTML = article
    ? `<div class="article-text">${html.join("")}</div>`
    : html.join("");

  $("transcript").querySelectorAll("span.seg-time").forEach((el) => {
    el.addEventListener("click", () => playAt(Number(el.dataset.at)));
  });
}

/** 表格：第一行是表格標題時獨立成段落正常換行，表格列才用等寬字逐列顯示。rows 已跳脫。 */
function paperTableHtml(rows) {
  const caption = /^(table|tab\.|表)\s*\S/i.test(rows[0]) ? rows.shift() : "";
  return `${caption ? `<p class="paper-table-caption">${caption}</p>` : ""}${
    rows.length ? `<pre class="paper-table">${rows.join("\n")}</pre>` : ""
  }`;
}

/** 論文段落依類型顯示：章節標題、表格（每列一行）、參考文獻。text 為目前語言的文字。 */
function paperSegmentHtml(s, text) {
  if (s.kind === "h1") return `<h4 class="paper-heading">${escapeHtml(text)}</h4>`;
  if (s.kind === "h2") return `<h5 class="paper-subheading">${escapeHtml(text)}</h5>`;
  if (s.kind === "table") return paperTableHtml(text.split("\n").map(escapeHtml));
  if (s.kind === "ref") return `<p class="paper-ref">${escapeHtml(text)}</p>`;
  return `<p>${escapeHtml(text)}</p>`;
}

// ── 論文翻譯 ────────────────────────────────────────
// 有譯文時原文預設顯示中文，可切回英文；沒有譯文的段落（參考文獻、未翻到的）顯示原文。

let transcriptLang = "zh";

function hasTranslation(data) {
  return (data?.segments || []).some((s) => s.translation);
}

/** 目前語言下這一段要顯示的文字。 */
function segmentText(s) {
  return transcriptLang === "zh" && s.translation ? s.translation : s.text;
}

/** 章節標題跟著語言切換：論文章節即章標題，譯文在該段的 translation。 */
function localizedChapters(data) {
  const chapters = data.summary?.chapters || [];
  if (!isPaper(data.episode) || transcriptLang !== "zh") return chapters;
  return chapters.map((c) => {
    const seg = data.segments[c.paragraph];
    return seg?.translation ? { ...c, title: seg.translation } : c;
  });
}

function renderTranslateControls() {
  const paper = isPaper(current.episode);
  $("translate-controls").hidden = !paper;
  if (!paper) return;
  const translated = hasTranslation(current);
  $("lang-switch").hidden = !translated;
  $("lang-switch").querySelectorAll("button").forEach((b) => {
    b.classList.toggle("on", b.dataset.lang === transcriptLang);
  });
  const status = translationStatus.get(current.guid);
  const btn = $("btn-translate");
  btn.disabled = Boolean(status?.running);
  btn.textContent = status?.running
    ? `翻譯中 ${status.done}／${status.total || "…"}`
    : translated
      ? "補翻／重新翻譯"
      : "翻譯成中文";
  btn.title = translated ? "補上沒翻到的段落；按住 Shift 點擊則全部重新翻譯" : "以 Sonnet 翻譯全文，約需數分鐘";
}

$("lang-switch").addEventListener("click", (e) => {
  const lang = e.target.closest("button")?.dataset.lang;
  if (!lang || lang === transcriptLang) return;
  transcriptLang = lang;
  renderTranslateControls();
  renderTranscript(current);
});

// 各集的翻譯進度；翻譯在背景跑，切到別集再回來仍看得到進度
const translationStatus = new Map();
const translationTimers = new Map();

$("btn-translate").addEventListener("click", async (e) => {
  const guid = current.guid;
  const force = e.shiftKey;
  if (force && !confirm("全部重新翻譯？已有的譯文會被覆蓋。")) return;
  try {
    const status = await api(`/api/episodes/${guid}/translate${force ? "?force=true" : ""}`, { method: "POST" });
    translationStatus.set(guid, status);
    renderTranslateControls();
    watchTranslation(guid);
  } catch (err) {
    alert(err.message);
  }
});

/** 追蹤翻譯進度，結束時重新載入這一集並顯示譯文。 */
async function watchTranslation(guid) {
  clearTimeout(translationTimers.get(guid));
  let status;
  try {
    status = await api(`/api/episodes/${guid}/translate`);
  } catch {
    return;
  }
  const wasRunning = translationStatus.get(guid)?.running;
  translationStatus.set(guid, status);
  if (current?.guid === guid) renderTranslateControls();

  if (status.running) {
    translationTimers.set(guid, setTimeout(() => watchTranslation(guid), 5000));
    return;
  }
  if (!wasRunning) return; // 開啟單集時查到的舊狀態，不重複提示
  if (status.error) alert(status.error);
  if (current?.guid === guid) {
    transcriptLang = "zh";
    await showEpisode(guid);
  }
  await loadLibrary();
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
  const chapters = localizedChapters(data);
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
        // 文章與論文的章節以段落定位，沒有時間
        const time = isText(data.episode) ? "" : `<span class="chapter-time">${formatTime(c.start)}</span>`;
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

function isPaper(episode) {
  return episode?.platform === "paper";
}

/** 文章與論文：沒有音檔與說話者，以段落定位。 */
function isText(episode) {
  return isArticle(episode) || isPaper(episode);
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
  if (isPaper(episode)) {
    link.hidden = false;
    link.href = `/api/episodes/${encodeURIComponent(episode.episode_guid)}/pdf`;
    link.textContent = "開啟 PDF 原檔 ↗";
    return;
  }
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

// ── 進行中的動作 ──────────────────────────────────────
// 按鈕在各集之間共用，進行中的動作以「單集＋動作」記錄：
// 切到別集時按鈕顯示該集自己的狀態，回應到達時只有仍在同一集才更新畫面。
const BUSY_BUTTONS = {
  regen: { id: "btn-regen", idle: "重新生成全部", busy: "生成中…" },
  cover: { id: "btn-regen-cover", idle: "重新生成封面", busy: "生成中…" },
  upload: { id: "btn-upload", busy: "上傳中…" },
  discard: { id: "btn-discard-regen", idle: "放棄重新生成", busy: "處理中…" },
};
const busyActions = new Set();

function isBusy(guid, action) {
  return busyActions.has(`${guid}:${action}`);
}

function setBusy(guid, action, on) {
  busyActions[on ? "add" : "delete"](`${guid}:${action}`);
  if (current?.guid === guid) renderBusyButtons();
}

/** 依目前這集進行中的動作更新按鈕；上傳鈕的平時文字由 renderUploadState 決定。 */
function renderBusyButtons() {
  if (!current) return;
  for (const [action, { id, idle, busy }] of Object.entries(BUSY_BUTTONS)) {
    const btn = $(id);
    const on = isBusy(current.guid, action);
    if (on) btn.textContent = busy;
    else if (idle) btn.textContent = idle;
    btn.disabled = on || (action === "upload" && pendingMerges(current.summary).length > 0);
  }
}

// ── 重新生成 ────────────────────────────────────────

$("btn-regen").addEventListener("click", async () => {
  const guid = current.guid;
  setBusy(guid, "regen", true);
  try {
    const summary = await api(`/api/episodes/${guid}/regenerate`, { method: "POST" });
    if (current?.guid === guid) {
      current.summary = summary;
      // 已上傳的單集重新生成後只存在本機，需再次上傳
      current.needs_reupload = Boolean(current.uploaded_at);
      renderSummary(summary);
      renderTranscript(current);
      renderUploadState();
    }
    loadLibrary();
  } catch (err) {
    alert(err.message);
  } finally {
    setBusy(guid, "regen", false);
  }
});

$("btn-regen-cover").addEventListener("click", async () => {
  if (!current.summary?.summary) {
    alert("請先產生摘要");
    return;
  }
  const guid = current.guid;
  setBusy(guid, "cover", true);
  try {
    const { cover } = await api(`/api/episodes/${guid}/cover`, { method: "POST" });
    if (current?.guid === guid) {
      current.summary = { ...current.summary, cover };
      renderCover(cover);
    }
    loadLibrary();
  } catch (err) {
    alert(err.message);
  } finally {
    setBusy(guid, "cover", false);
  }
});

// ── 上傳 Supabase ───────────────────────────────────

$("btn-upload").addEventListener("click", async () => {
  const guid = current.guid;
  setBusy(guid, "upload", true);
  try {
    const res = await api(`/api/episodes/${guid}/upload`, { method: "POST" });
    // 後端在上傳成功時把對應的待處理項目標記為完成，這裡刷新讓它消失
    await Promise.all([loadLibrary(), loadQueue()]);
    setBusy(guid, "upload", false);
    if (current?.guid !== guid) return;

    current.uploaded_at = new Date().toISOString();
    current.has_audio = false; // 上傳成功後音檔已自動清除
    current.needs_reupload = false;
    // 合併建議只存在本機 result.json，上傳後已隨本機目錄清除、無法再修改；
    // 收起標籤比較，與重新開啟這集時一致
    if (current.summary) current.summary.hashtag_merges = [];
    renderTagReview(current.summary);
    renderUploadState();
    // 上傳後按鈕會隱藏，先短暫顯示結果
    const freed = res.freed_bytes ? `，釋出 ${(res.freed_bytes / 1048576).toFixed(0)} MB` : "";
    const btn = $("btn-upload");
    $("upload-group").hidden = false;
    btn.hidden = false;
    btn.disabled = true;
    btn.textContent = `${res.inserted ? "已上傳" : "已更新"}${freed}`;
    setTimeout(() => { if (current?.guid === guid) renderUploadState(); }, 3000);
  } catch (err) {
    alert(err.message);
    setBusy(guid, "upload", false);
    if (current?.guid === guid) renderUploadState();
  }
});

// ── 刪除單集 ────────────────────────────────────────

$("btn-discard-regen").addEventListener("click", async () => {
  if (!confirm(`放棄「${current.episode.title}」重新生成的內容？\n本機的摘要、心智圖、標籤、章節與封面會刪除，回到已上傳的版本。\n此動作無法復原。`)) return;

  const guid = current.guid;
  setBusy(guid, "discard", true);
  try {
    await api(`/api/episodes/${guid}/regenerated`, { method: "DELETE" });
    await Promise.all([current?.guid === guid ? showEpisode(guid) : null, loadLibrary()]);
  } catch (err) {
    alert(err.message);
  } finally {
    setBusy(guid, "discard", false);
  }
});

$("btn-delete").addEventListener("click", async (e) => {
  const uploaded = Boolean(current.uploaded_at);
  const warning = uploaded
    ? "資料庫與本機檔案都會刪除，手機端也會看不到，所有人的收藏一併移除。"
    : isPaper(current.episode)
      ? "本機檔案（PDF、原文與摘要）都會刪除。"
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
    current.needs_reupload ? "重新生成的內容尚未上傳" : "",
    current.has_audio === false && !isText(current.episode) ? "音檔已刪除" : "",
  ].filter(Boolean);
  $("ep-meta").textContent = meta.join(" · ");
  renderSourceLink(current.episode);

  // 已上傳且沒有重新生成的內容時不需要上傳；「再次上傳」與「放棄重新生成」成組出現
  $("btn-upload").hidden = uploaded && !current.needs_reupload;
  $("btn-upload").textContent = uploaded ? "再次上傳" : "上傳";
  $("btn-discard-regen").hidden = !current.needs_reupload;
  $("upload-group").classList.toggle("paired", Boolean(current.needs_reupload));
  $("upload-group").hidden = $("btn-upload").hidden;
  const pending = pendingMerges(current.summary).length > 0;
  $("btn-upload").title = pending ? "標籤合併尚未確認" : "";
  renderBusyButtons();
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
  const startsAt = chapterStarts(d.segments, chapters, isText(d.episode));
  const heading = (i) => (startsAt.has(i) ? [`### ${chapters[startsAt.get(i)].title}`, ""] : []);
  if (parts.includes("transcript") && isPaper(d.episode)) {
    // 依目前顯示的語言輸出
    const zhChapters = localizedChapters(d);
    const paperHeading = (i) => (startsAt.has(i) ? [`### ${zhChapters[startsAt.get(i)].title}`, ""] : []);
    lines.push("## 原文", "");
    d.segments.forEach((s, i) => {
      const text = segmentText(s);
      if (startsAt.has(i) && s.kind === "h1") return lines.push(...paperHeading(i));
      if (s.kind === "h1") return lines.push(`### ${text}`, "");
      if (s.kind === "h2") return lines.push(`#### ${text}`, "");
      if (s.kind === "table") return lines.push("```", text, "```", "");
      lines.push(...paperHeading(i), text, "");
    });
  } else if (parts.includes("transcript") && isArticle(d.episode)) {
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
  const project = $("project-filter").value;
  const shown = allEpisodes.filter(
    (e) =>
      (!project || (e.projects || []).includes(project)) &&
      (!keyword ||
        [e.title, e.podcast_name, ...(e.hashtags || [])]
          .join(" ")
          .toLowerCase()
          .includes(keyword))
  );

  const selected = location.hash.slice(1);
  const groups = [
    ["處理中", shown.filter((e) => e.processing)],
    // 已上傳但本機有重新生成的內容，需再次上傳才會更新資料庫與手機端
    ["待重新上傳", shown.filter((e) => !e.processing && e.needs_reupload)],
    ["未上傳", shown.filter((e) => !e.processing && !e.uploaded_at)],
    ["已上傳", shown.filter((e) => !e.processing && e.uploaded_at && !e.needs_reupload)],
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
    html || `<li class="muted">${keyword || project ? "沒有符合的單集" : "還沒有處理過的單集"}</li>`;

  $("episode-list").querySelectorAll("li[data-guid]").forEach((li) => {
    li.addEventListener("click", () => {
      location.hash = li.dataset.guid;
      closeSidebar();
    });
  });
}

$("filter").addEventListener("input", renderLibrary);
$("project-filter").addEventListener("change", renderLibrary);

// ── 研究專案 ────────────────────────────────────────
// 專案存在資料庫，兩台電腦共用；未上傳的單集也能歸類。

let allProjects = [];

async function loadProjects() {
  try {
    allProjects = await api("/api/projects");
  } catch (err) {
    // 資料庫連不上或尚未建表時，專案功能停用，不影響其他功能
    console.error(err);
    allProjects = [];
  }
  renderProjectFilter();
  if (current) renderEpisodeProjects();
}

function renderProjectFilter() {
  const select = $("project-filter");
  const selected = select.value;
  select.innerHTML =
    `<option value="">全部專案</option>` +
    allProjects
      .map((p) => `<option value="${p.id}">${escapeHtml(p.name)}（${p.item_count}）</option>`)
      .join("");
  // 篩選中的專案被刪除時回到全部
  select.value = allProjects.some((p) => p.id === selected) ? selected : "";
}

/** 單集頁的專案區塊：已歸入的標為選取，點一下切換。 */
function renderEpisodeProjects() {
  const box = $("episode-projects");
  const mine = new Set(current.projects || []);
  box.innerHTML = allProjects.length
    ? allProjects
        .map(
          (p) =>
            `<button type="button" class="${mine.has(p.id) ? "on" : ""}" data-id="${p.id}" aria-pressed="${mine.has(p.id)}">${escapeHtml(p.name)}</button>`
        )
        .join("")
    : `<p class="muted">還沒有專案，可在下方新增</p>`;
  box.querySelectorAll("button[data-id]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const ids = new Set(current.projects || []);
      if (ids.has(btn.dataset.id)) ids.delete(btn.dataset.id);
      else ids.add(btn.dataset.id);
      saveEpisodeProjects([...ids]);
    });
  });
}

async function saveEpisodeProjects(ids) {
  const guid = current.guid;
  try {
    const res = await api(`/api/episodes/${guid}/projects`, {
      method: "PUT",
      body: { project_ids: ids },
    });
    if (current?.guid === guid) current.projects = res.projects;
    await Promise.all([loadProjects(), loadLibrary()]);
  } catch (err) {
    alert(err.message);
  }
}

$("new-project-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("new-project-name").value.trim();
  if (!name) return;
  try {
    const project = await api("/api/projects", { method: "POST", body: { name } });
    $("new-project-name").value = "";
    allProjects.push(project);
    await saveEpisodeProjects([...(current.projects || []), project.id]);
  } catch (err) {
    alert(err.message);
  }
});

$("btn-manage-projects").addEventListener("click", async () => {
  await loadProjects();
  renderProjectManager();
  $("projects-dialog").showModal();
});

/** 管理對話框：改名、說明與刪除；新增在單集頁進行（建立時就歸入該集）。 */
function renderProjectManager() {
  const list = $("project-manage-list");
  list.innerHTML = allProjects.length
    ? allProjects
        .map(
          (p) => `<li data-id="${p.id}">
            <input type="text" name="name" value="${escapeHtml(p.name)}" maxlength="100" aria-label="專案名稱">
            <button type="button" class="danger" data-action="delete">刪除</button>
            <textarea name="description" rows="2" maxlength="2000" placeholder="說明（選填）">${escapeHtml(p.description)}</textarea>
            <span class="count">${p.item_count} 筆內容</span>
          </li>`
        )
        .join("")
    : `<li class="muted">還沒有專案；在單集頁的「研究專案」區塊新增</li>`;

  list.querySelectorAll("li[data-id]").forEach((li) => {
    const id = li.dataset.id;
    li.querySelectorAll("input, textarea").forEach((field) => {
      field.addEventListener("change", async () => {
        try {
          await api(`/api/projects/${id}`, { method: "PUT", body: { [field.name]: field.value } });
          await loadProjects();
        } catch (err) {
          alert(err.message);
          renderProjectManager();
        }
      });
    });
    li.querySelector("[data-action=delete]").addEventListener("click", async () => {
      const name = allProjects.find((p) => p.id === id)?.name || "";
      if (!confirm(`刪除專案「${name}」？\n只會移除歸類，單集不受影響。`)) return;
      try {
        await api(`/api/projects/${id}`, { method: "DELETE" });
        await Promise.all([loadProjects(), loadLibrary()]);
        if (current) current.projects = (current.projects || []).filter((p) => p !== id);
        renderProjectManager();
        if (current) renderEpisodeProjects();
      } catch (err) {
        alert(err.message);
      }
    });
  });
}

// ── 側邊欄（窄螢幕） ────────────────────────────────

// 寬螢幕：清單常駐左側，可收合讓內容撐滿；窄螢幕：清單是抽屜，☰ 開關
const NARROW = window.matchMedia("(max-width: 800px)");
const SIDEBAR_COLLAPSED_KEY = "podscript_sidebar_collapsed";

function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  try {
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "");
  } catch {
    // 無法記住時只影響重新整理後的狀態
  }
}

try {
  if (localStorage.getItem(SIDEBAR_COLLAPSED_KEY)) document.body.classList.add("sidebar-collapsed");
} catch {
  // 讀不到就維持展開
}

$("btn-menu").addEventListener("click", () => {
  if (NARROW.matches) $("sidebar").classList.toggle("open");
  else setSidebarCollapsed(false);
});

$("btn-collapse-sidebar").addEventListener("click", () => {
  if (NARROW.matches) closeSidebar();
  else setSidebarCollapsed(true);
});

function closeSidebar() {
  $("sidebar").classList.remove("open");
}

// 內容類型：YouTube 為影片、文章與論文各自一類，其餘平台為音檔（與手機卡片相同）
const EP_KINDS = {
  audio: { icon: "ic-audio", label: "音檔" },
  video: { icon: "ic-video", label: "影片" },
  article: { icon: "ic-article", label: "文章" },
  paper: { icon: "ic-paper", label: "論文" },
};

function episodeKind(e) {
  if (e.kind === "article" || e.kind === "paper") return e.kind;
  return e.platform === "youtube" ? "video" : "audio";
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
      ${e.title_translated ? `<span class="ep-name-zh">${escapeHtml(e.title_translated)}</span>` : ""}
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
loadProjects();
