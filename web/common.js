// 本機網頁共用的工具：單集頁（app.js）、研究專案頁（project.js）與專案管理頁（project-manage.js）。
// 先於各頁腳本載入；頁面需有 svg 圖示定義（ic-audio 等），用到心智圖全螢幕的頁面另需 #mindmap-overlay。

const STAGE_LABELS = {
  queued: "排隊", // 一次只處理一集，其他集排隊等前一集（含摘要）完成
  resolve: "解析",
  download: "下載",
  transcribe: "轉錄",
  diarize: "分離",
  merge: "對齊",
  summarize: "摘要",
  done: "完成",
};

const $ = (id) => document.getElementById(id);

// 未寫的屬性沿用這組預設（線條插圖風格），被過濾掉屬性的元素才不會變成黑色實心
const COVER_SVG_DEFAULTS = 'viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"';

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
  const status = e.processing && e.stage === "queued"
    ? `<span class="ep-status">排隊中…</span>`
    : e.processing
    ? `<span class="ep-status"><span class="spinner"></span> ${escapeHtml(
        STAGE_LABELS[e.stage] || e.stage
      )}中${e.percent != null ? ` ${e.percent}%` : "…"}</span>`
    : e.cancelled
      ? `<span class="ep-status muted">已取消排隊，點擊可重新排入</span>`
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

// ── 心智圖全螢幕 ──────────────────────────────────
let fullMindmap = null;

/** 以全螢幕顯示心智圖（mermaid mindmap 語法），可拖曳縮放。 */
function openMindmapFull(code) {
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

// 專案管理頁沒有心智圖，不綁定
if ($("mindmap-overlay")) {
  $("btn-mindmap-close").addEventListener("click", closeMindmapFull);
  $("btn-mindmap-reset").addEventListener("click", () => fullMindmap?.fit());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeMindmapFull();
  });
  window.addEventListener("resize", () => fullMindmap?.fit());
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
