// ── 設定 ──────────────────────────────────────────
// Railway 部署後的 API 網址，與本機 8420 不同服務。
const API_BASE = window.PODSCRIPT_API_BASE || "http://localhost:8000/api";
const GOOGLE_CLIENT_ID = window.PODSCRIPT_GOOGLE_CLIENT_ID || "";

const TOKEN_KEY = "podscript_token";
const NAME_KEY = "podscript_name";
const THEME_KEY = "podscript_theme";

// ── 主題（深/淺色）──────────────────────────────────
// 三態：跟隨系統（不存值）／light／dark，符合 artifact-design 慣例的 data-theme 機制。
function getStoredTheme() {
  return localStorage.getItem(THEME_KEY); // null＝跟隨系統
}

function isDarkMode() {
  const stored = getStoredTheme();
  return stored ? stored === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme) root.setAttribute("data-theme", theme);
  else root.removeAttribute("data-theme");

  document.querySelector("#theme-icon use").setAttribute("href", isDarkMode() ? "#ic-sun" : "#ic-moon");
}

function toggleTheme() {
  const next = isDarkMode() ? "light" : "dark";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
}

// 心智圖配色跟隨深/淺色模式；maxNodeWidth 縮窄節點寬度換取分支間距，避免節點多時交疊。
// 分支色不透過 themeVariables 的 primaryColor 自動推算（該推算以 primaryColor
// 明度為基準，深色系種子會讓所有分支色階塌陷成同一種近黑色），
// 改用 CSS 直接指定 mermaid 產生的 .section-N / .section-edge-N，見 style.css。
function initMindmapTheme() {
  mermaid.initialize({
    startOnLoad: false,
    theme: "base",
    themeVariables: {
      fontFamily: '"PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif',
    },
    mindmap: { padding: 16, maxNodeWidth: 120 },
    fontSize: 14,
  });
}

initMindmapTheme();

applyTheme(getStoredTheme());
document.querySelector("#btn-theme").addEventListener("click", toggleTheme);

// ── 登入 ──────────────────────────────────────────
function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(NAME_KEY);
}

function showUserInfo() {
  const name = localStorage.getItem(NAME_KEY);
  if (!name) {
    // 舊版本存的 token 沒有附帶名字，視為登入資訊不完整，要求重新登入
    clearToken();
    hideUserInfo();
    return false;
  }
  document.querySelector("#g_id_signin").hidden = true;
  document.querySelector("#user-info").hidden = false;
  document.querySelector("#user-name").textContent = `你好，${name}`;
  document.querySelector("#btn-tags-entry").hidden = false;
  document.querySelector("#btn-favorites-entry").hidden = false;
  return true;
}

function hideUserInfo() {
  document.querySelector("#g_id_signin").hidden = false;
  document.querySelector("#user-info").hidden = true;
  document.querySelector("#btn-tags-entry").hidden = true;
  document.querySelector("#btn-favorites-entry").hidden = true;
}

async function handleGoogleCredential(response) {
  const res = await fetch(`${API_BASE}/auth/google`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id_token: response.credential }),
  });

  if (!res.ok) {
    const detail = (await res.json().catch(() => ({}))).detail || "登入失敗";
    document.querySelector("#login p").textContent =
      res.status === 403 ? "此帳號未在白名單內，請聯絡管理員。" : detail;
    return;
  }

  const data = await res.json();
  setToken(data.access_token);
  localStorage.setItem(NAME_KEY, data.name);
  document.querySelector("#login").hidden = true;
  showUserInfo();
  route();
}

function initGoogleSignIn() {
  if (!window.google || !GOOGLE_CLIENT_ID) return;
  google.accounts.id.initialize({
    client_id: GOOGLE_CLIENT_ID,
    callback: handleGoogleCredential,
  });
  google.accounts.id.renderButton(document.getElementById("g_id_signin"), {
    theme: "outline",
    size: "medium",
  });
  if (getToken()) showUserInfo();
}

// ── API ───────────────────────────────────────────
async function api(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { Authorization: `Bearer ${getToken()}`, ...(options.headers || {}) },
  });
  if (res.status === 401) {
    clearToken();
    showLogin();
    throw new Error("未登入或憑證過期");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || res.status);
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

async function setFavorite(guid, on) {
  await api(`/episodes/${encodeURIComponent(guid)}/favorite`, { method: on ? "PUT" : "DELETE" });
}

// ── 畫面切換 ──────────────────────────────────────
const VIEWS = ["login", "list-view", "favorites-view", "tags-view", "detail-view"];

function showView(id) {
  VIEWS.forEach(v => {
    document.querySelector(`#${v}`).hidden = v !== id;
  });
  document.querySelector("#btn-back").hidden = id === "login" || id === "list-view";
}

function showLogin() {
  showView("login");
  hideUserInfo();
}

function showList() {
  showView("list-view");
}

function showFavorites() {
  showView("favorites-view");
}

function showTags() {
  showView("tags-view");
}

function showDetail() {
  showView("detail-view");
}

// ── 卡片頭像（頻道色塊＋首字，尚無真實封面圖）──────────────
const COVER_COLORS = ["#3a6b52", "#a3672f", "#5c6bb0", "#b0555c", "#4d8b8b", "#8a5ab0"];

function coverColorFor(podcastName) {
  let hash = 0;
  for (let i = 0; i < podcastName.length; i++) hash = (hash * 31 + podcastName.charCodeAt(i)) >>> 0;
  return COVER_COLORS[hash % COVER_COLORS.length];
}

// 優先序：中文字 > 英文字母 > 字串第一個字元 > 都不符合則空字串
function coverInitial(podcastName) {
  const name = (podcastName || "").trim();
  const cjk = name.match(/[一-鿿]/);
  if (cjk) return cjk[0];
  const alpha = name.match(/[a-zA-Z]/);
  if (alpha) return alpha[0];
  return name.charAt(0) || "";
}

function renderCoverHtml(podcastName) {
  return `<div class="ep-cover" style="background:${coverColorFor(podcastName || "")}">${coverInitial(podcastName)}</div>`;
}

// ── 列表頁 ────────────────────────────────────────
const SORT_KEY = "podscript_sort";
const YEAR_MONTH_LABEL = (y, m) => `${y}年${m}月`;

let currentChannel = "";
let currentTags = [];
let currentTagMode = "any";
let currentFavoritesOnly = false;
let channelsLoaded = false;
let allChannels = [];

function formatDuration(sec) {
  if (!sec) return "";
  const m = Math.round(sec / 60);
  return `${m} 分鐘`;
}

function getSort() {
  return localStorage.getItem(SORT_KEY) || "created_at";
}

function heartButtonHtml(isFavorite) {
  return `
    <button type="button" class="heart-btn${isFavorite ? " on" : ""}" data-favorite="${isFavorite ? "1" : "0"}">
      <svg class="icon icon-sm"><use href="#${isFavorite ? "ic-heart-fill" : "ic-heart"}"/></svg>
    </button>
  `;
}

function cardTagsHtml(hashtags) {
  return (hashtags || [])
    .map(t => `<button type="button" class="tag-btn" data-tag="${encodeURIComponent(t)}">${t}</button>`)
    .join("");
}

function renderEpisodeCard(ep) {
  const div = document.createElement("div");
  div.className = "ep-card";
  div.innerHTML = `
    <a href="#/ep/${encodeURIComponent(ep.episode_guid)}" class="ep-link">
      ${renderCoverHtml(ep.podcast_name)}
      <div class="ep-body">
        <span class="ep-title">${ep.title}</span>
        <span class="ep-meta">${ep.podcast_name}${ep.published_at ? " · " + ep.published_at.slice(0, 10) : ""}${ep.duration_sec ? " · " + formatDuration(ep.duration_sec) : ""}</span>
        <span class="tags">${cardTagsHtml(ep.hashtags)}</span>
      </div>
    </a>
    ${heartButtonHtml(ep.is_favorite)}
  `;

  div.querySelector(".heart-btn").addEventListener("click", async (e) => {
    e.preventDefault();
    const btn = e.currentTarget;
    const nextOn = btn.dataset.favorite !== "1";
    btn.dataset.favorite = nextOn ? "1" : "0";
    btn.classList.toggle("on", nextOn);
    btn.querySelector("use").setAttribute("href", nextOn ? "#ic-heart-fill" : "#ic-heart");
    try {
      await setFavorite(ep.episode_guid, nextOn);
    } catch (err) {
      // 失敗時還原按鈕狀態，避免畫面與伺服器不同步
      btn.dataset.favorite = nextOn ? "0" : "1";
      btn.classList.toggle("on", !nextOn);
      btn.querySelector("use").setAttribute("href", !nextOn ? "#ic-heart-fill" : "#ic-heart");
      alert(`收藏失敗：${err.message}`);
    }
  });

  div.querySelectorAll(".tag-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      location.hash = `#/?tag=${btn.dataset.tag}`;
    });
  });

  return div;
}

// 依排序基準的時間欄位分組成 {"2026-09": [...]}，維持後端已排好的順序
function groupByYearMonth(episodes, sortField) {
  const groups = new Map();
  episodes.forEach(ep => {
    const raw = ep[sortField];
    const key = raw ? raw.slice(0, 7) : "unknown";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(ep);
  });
  return groups;
}

function renderScrubber(groupKeys) {
  const scrubber = document.querySelector("#scrubber");
  if (groupKeys.length <= 1) {
    scrubber.hidden = true;
    scrubber.innerHTML = "";
    return;
  }

  scrubber.hidden = false;
  scrubber.innerHTML = groupKeys
    .map(key => {
      if (key === "unknown") return `<span class="scrubber-tick" data-key="unknown">−</span>`;
      const [y, m] = key.split("-");
      return `<span class="scrubber-tick" data-key="${key}">${m}</span>`;
    })
    .join("");
}

function bindScrubberDrag() {
  const scrubber = document.querySelector("#scrubber");
  if (scrubber.dataset.bound) return;
  scrubber.dataset.bound = "1";

  function jumpToKey(key) {
    const section = document.querySelector(`[data-group="${key}"]`);
    if (section) section.scrollIntoView({ block: "start" });
  }

  function handlePoint(clientY) {
    const el = document.elementFromPoint(scrubber.getBoundingClientRect().left + 10, clientY);
    const tick = el && el.closest(".scrubber-tick");
    if (tick) jumpToKey(tick.dataset.key);
  }

  let dragging = false;
  scrubber.addEventListener("pointerdown", e => { dragging = true; handlePoint(e.clientY); });
  window.addEventListener("pointermove", e => { if (dragging) handlePoint(e.clientY); });
  window.addEventListener("pointerup", () => { dragging = false; });
}

function renderGroups(groups) {
  const container = document.querySelector("#episode-groups");
  container.innerHTML = "";

  for (const [key, episodes] of groups) {
    const section = document.createElement("section");
    section.dataset.group = key;

    const heading = key === "unknown"
      ? "日期不明"
      : YEAR_MONTH_LABEL(...key.split("-").map(Number));

    const list = document.createElement("div");
    list.className = "cards";
    episodes.forEach(ep => list.appendChild(renderEpisodeCard(ep)));

    section.innerHTML = `<h2 class="group-heading">${heading}</h2>`;
    section.appendChild(list);
    container.appendChild(section);
  }
}

function renderActiveFilters() {
  const container = document.querySelector("#active-filters");
  const chips = [];

  if (currentChannel) {
    chips.push({ label: currentChannel, onRemove: () => { currentChannel = ""; loadList(); } });
  }
  currentTags.forEach(tag => {
    chips.push({
      label: tag,
      icon: "ic-tag",
      onRemove: () => { currentTags = currentTags.filter(t => t !== tag); loadList(); },
    });
  });
  if (currentFavoritesOnly) {
    chips.push({ label: "只看收藏", icon: "ic-heart-fill", onRemove: () => { currentFavoritesOnly = false; loadList(); } });
  }

  if (currentTags.length > 1) {
    chips.push({ label: currentTagMode === "all" ? "AND" : "OR", badge: true });
  }

  container.innerHTML = "";
  container.hidden = chips.length === 0;

  chips.forEach((chip) => {
    const el = document.createElement(chip.onRemove ? "button" : "span");
    if (chip.onRemove) el.type = "button";
    el.className = "filter-chip" + (chip.badge ? " mode-badge" : "");
    el.innerHTML = `${chip.icon ? `<svg class="icon icon-xs"><use href="#${chip.icon}"/></svg>` : ""}${chip.label}${chip.onRemove ? ` <span class="remove">✕</span>` : ""}`;
    if (chip.onRemove) el.addEventListener("click", chip.onRemove);
    container.appendChild(el);
  });
}

function updateFilterDot() {
  const active = !!(currentChannel || currentTags.length || currentFavoritesOnly);
  document.querySelector("#filter-dot").hidden = !active;
  document.querySelector("#btn-open-filter").classList.toggle("active", active);
}

let listRequestId = 0;

function renderFilteredList(episodes) {
  const groups = groupByYearMonth(episodes, getSort());
  renderGroups(groups);
  renderScrubber([...groups.keys()]);
  bindScrubberDrag();
  document.querySelector("#list-empty").hidden = episodes.length > 0;
}

async function ensureChannelsLoaded() {
  if (channelsLoaded) return;
  allChannels = await api("/channels");
  channelsLoaded = true;
}

async function loadList() {
  const requestId = ++listRequestId;

  await ensureChannelsLoaded();
  renderSheetChannelChips();
  renderActiveFilters();
  updateFilterDot();

  const q = document.querySelector("#search").value.trim();
  const query = new URLSearchParams();
  if (q) query.set("q", q);
  currentTags.forEach(t => query.append("tags", t));
  query.set("tag_mode", currentTagMode);
  if (currentChannel) query.set("channel", currentChannel);
  if (currentFavoritesOnly) query.set("favorites_only", "true");
  query.set("sort", getSort());
  query.set("limit", 500); // 一次性全拉，見 spec §5.5 未來優化項目

  setListLoading(true);
  let episodes;
  try {
    episodes = await api(`/episodes?${query}`);
  } finally {
    if (requestId === listRequestId) setListLoading(false);
  }

  // 若期間又觸發了更新的請求，這次結果已過時，不渲染避免畫面閃回舊資料
  if (requestId !== listRequestId) return;

  renderFilteredList(episodes);
}

function setListLoading(loading) {
  document.querySelector("#list-loading").hidden = !loading;
  document.querySelector("#episode-groups").hidden = loading;
}

document.querySelector("#search").addEventListener("input", debounce(() => loadList(), 300));

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// ── 篩選 Bottom Sheet ────────────────────────────────
let sheetSort = getSort();
let sheetChannel = currentChannel;
let sheetFavoritesOnly = currentFavoritesOnly;

function renderSheetChannelChips() {
  const container = document.querySelector("#sheet-channel-chips");
  const allChip = `<button type="button" class="sheet-chip${sheetChannel === "" ? " active" : ""}" data-channel="">全部</button>`;
  const chips = allChannels.map(c =>
    `<button type="button" class="sheet-chip${sheetChannel === c.podcast_name ? " active" : ""}" data-channel="${encodeURIComponent(c.podcast_name)}">${c.podcast_name}（${c.count}）</button>`
  );
  container.innerHTML = allChip + chips.join("");

  container.querySelectorAll(".sheet-chip").forEach(btn => {
    btn.addEventListener("click", () => {
      sheetChannel = decodeURIComponent(btn.dataset.channel);
      container.querySelectorAll(".sheet-chip").forEach(b => b.classList.toggle("active", b === btn));
    });
  });
}

function openFilterSheet() {
  sheetSort = getSort();
  sheetChannel = currentChannel;
  sheetFavoritesOnly = currentFavoritesOnly;

  renderSheetChannelChips();
  document.querySelectorAll("#filter-sheet [data-sort]").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.sort === sheetSort);
  });
  document.querySelector("#sheet-favorites-only").classList.toggle("active", sheetFavoritesOnly);

  document.querySelector("#filter-sheet").hidden = false;
}

function closeFilterSheet() {
  document.querySelector("#filter-sheet").hidden = true;
}

document.querySelector("#btn-open-filter").addEventListener("click", openFilterSheet);
document.querySelector("#filter-sheet").addEventListener("click", e => {
  if (e.target.id === "filter-sheet") closeFilterSheet();
});

document.querySelectorAll("#filter-sheet [data-sort]").forEach(btn => {
  btn.addEventListener("click", () => {
    sheetSort = btn.dataset.sort;
    document.querySelectorAll("#filter-sheet [data-sort]").forEach(b => b.classList.toggle("active", b === btn));
  });
});

document.querySelector("#sheet-favorites-only").addEventListener("click", (e) => {
  sheetFavoritesOnly = !sheetFavoritesOnly;
  e.currentTarget.classList.toggle("active", sheetFavoritesOnly);
});

document.querySelector("#sheet-clear").addEventListener("click", () => {
  sheetChannel = "";
  sheetSort = "created_at";
  sheetFavoritesOnly = false;
  currentTags = [];
  renderSheetChannelChips();
  document.querySelectorAll("#filter-sheet [data-sort]").forEach(btn => btn.classList.toggle("active", btn.dataset.sort === sheetSort));
  document.querySelector("#sheet-favorites-only").classList.remove("active");
});

document.querySelector("#sheet-apply").addEventListener("click", () => {
  currentChannel = sheetChannel;
  currentFavoritesOnly = sheetFavoritesOnly;
  localStorage.setItem(SORT_KEY, sheetSort);
  closeFilterSheet();
  loadList();
});

// ── 我的收藏 ──────────────────────────────────────
async function loadFavorites() {
  const loading = document.querySelector("#favorites-loading");
  const container = document.querySelector("#favorites-cards");
  const empty = document.querySelector("#favorites-empty");

  loading.hidden = false;
  container.innerHTML = "";
  empty.hidden = true;

  let episodes;
  try {
    episodes = await api(`/episodes?favorites_only=true&sort=${getSort()}&limit=500`);
  } finally {
    loading.hidden = true;
  }

  if (episodes.length === 0) {
    empty.hidden = false;
    return;
  }
  episodes.forEach(ep => container.appendChild(renderEpisodeCard(ep)));
}

document.querySelector("#btn-favorites-entry").addEventListener("click", () => {
  location.hash = "#/favorites";
});

// ── 標籤總覽 ──────────────────────────────────────
let allTags = [];
let tagSelection = new Set();

function tagFontSize(count, maxCount) {
  const min = 12, max = 17;
  if (maxCount <= 1) return max;
  return Math.round(min + (max - min) * (count / maxCount));
}

function renderTagCloud() {
  const container = document.querySelector("#tag-cloud");
  const maxCount = Math.max(...allTags.map(t => t.count), 1);
  container.innerHTML = allTags
    .map(t => `
      <button type="button" class="tag-pill${tagSelection.has(t.tag) ? " selected" : ""}" data-tag="${encodeURIComponent(t.tag)}" style="font-size:${tagFontSize(t.count, maxCount)}px;">
        ${t.tag} <span class="n">${t.count}</span>
      </button>
    `)
    .join("");
}

function renderTagList() {
  const container = document.querySelector("#tag-list");
  const sorted = [...allTags].sort((a, b) => a.tag.localeCompare(b.tag, "zh-Hant"));
  container.innerHTML = sorted
    .map(t => `
      <button type="button" class="tag-row${tagSelection.has(t.tag) ? " selected" : ""}" data-tag="${encodeURIComponent(t.tag)}">
        <span class="tag-row-main"><span class="tag-row-check">✓</span><span class="tag-row-name">${t.tag}</span></span>
        <span class="tag-row-count">${t.count} 集</span>
      </button>
    `)
    .join("");
}

function bindTagSelectionHandlers() {
  document.querySelectorAll("#tag-cloud .tag-pill, #tag-list .tag-row").forEach(el => {
    el.addEventListener("click", () => {
      const tag = decodeURIComponent(el.dataset.tag);
      if (tagSelection.has(tag)) tagSelection.delete(tag);
      else tagSelection.add(tag);
      renderTagCloud();
      renderTagList();
      bindTagSelectionHandlers();
      updateTagSelectionUi();
    });
  });
}

function updateTagSelectionUi() {
  document.querySelector("#tag-selected-count").textContent = tagSelection.size;
  document.querySelector("#tag-apply-btn").disabled = tagSelection.size === 0;
}

async function loadTagsView() {
  allTags = await api("/tags");
  tagSelection = new Set(currentTags);
  renderTagCloud();
  renderTagList();
  bindTagSelectionHandlers();
  updateTagSelectionUi();

  const isAnd = currentTagMode === "all";
  document.querySelector("#tag-mode-switch").classList.toggle("on", isAnd);
  document.querySelector("#tag-mode-switch").setAttribute("aria-checked", String(isAnd));
  updateTagModeSub(isAnd);
}

function updateTagModeSub(isAnd) {
  document.querySelector("#tag-mode-sub").textContent = isAnd
    ? "目前：AND（須同時符合所選標籤）"
    : "目前：OR（符合任一標籤即列出）";
}

document.querySelector("#tag-mode-switch").addEventListener("click", (e) => {
  const on = e.currentTarget.classList.toggle("on");
  e.currentTarget.setAttribute("aria-checked", String(on));
  updateTagModeSub(on);
});

document.querySelector("#tag-search").addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll("#tag-cloud .tag-pill, #tag-list .tag-row").forEach(el => {
    const name = decodeURIComponent(el.dataset.tag).toLowerCase();
    el.hidden = q.length > 0 && !name.includes(q);
  });
});

document.querySelector("#tag-apply-btn").addEventListener("click", () => {
  currentTags = [...tagSelection];
  currentTagMode = document.querySelector("#tag-mode-switch").classList.contains("on") ? "all" : "any";
  location.hash = "#/";
});

document.querySelector("#btn-tags-entry").addEventListener("click", () => {
  location.hash = "#/tags";
});

// ── 詳細頁 ────────────────────────────────────────
function formatTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function renderTranscript(segments, speakers) {
  const container = document.querySelector("#transcript");
  container.innerHTML = "";
  segments.forEach(seg => {
    const name = (speakers || {})[seg.speaker] || seg.speaker;
    const div = document.createElement("div");
    div.className = "seg" + (seg.confidence < 0.6 ? " low" : "");
    div.innerHTML = `
      <div class="seg-head">
        <span class="seg-speaker">${name}</span>
        <span class="seg-time">${formatTime(seg.start)}</span>
      </div>
      <p>${seg.text}</p>
    `;
    container.appendChild(div);
  });
}

function setDetailFavoriteIcon(isFavorite) {
  const btn = document.querySelector("#btn-favorite-detail");
  btn.classList.toggle("on", isFavorite);
  btn.dataset.favorite = isFavorite ? "1" : "0";
  document.querySelector("#favorite-detail-icon use").setAttribute("href", isFavorite ? "#ic-heart-fill" : "#ic-heart");
}

let currentDetailGuid = null;
let currentMindmapCode = null;

async function renderMindmap() {
  const mindmapEl = document.querySelector("#mindmap");
  if (!currentMindmapCode) {
    mindmapEl.textContent = "（尚無心智圖）";
    return;
  }
  mindmapEl.innerHTML = "";
  const { svg } = await mermaid.render("mindmap-svg" + Date.now(), currentMindmapCode);
  mindmapEl.innerHTML = svg;
}

async function loadDetail(guid) {
  currentDetailGuid = guid;
  const ep = await api(`/episodes/${encodeURIComponent(guid)}`);

  document.querySelector("#ep-title").textContent = ep.title;
  document.querySelector("#ep-meta").textContent =
    `${ep.podcast_name}${ep.published_at ? " · " + ep.published_at.slice(0, 10) : ""}${ep.duration_sec ? " · " + formatDuration(ep.duration_sec) : ""}`;
  document.querySelector("#summary-text").textContent = ep.summary || "（尚無摘要）";
  document.querySelector("#hashtags").innerHTML =
    (ep.hashtags || []).map(t => `<a href="#/?tag=${encodeURIComponent(t)}"><span><svg class="icon icon-xs"><use href="#ic-tag"/></svg>${t}</span></a>`).join("");

  setDetailFavoriteIcon(ep.is_favorite);

  currentMindmapCode = ep.mindmap_mermaid || null;
  await renderMindmap();

  renderTranscript(ep.transcript || [], ep.speakers || {});

  document.querySelectorAll(".subtab").forEach(t => t.classList.toggle("active", t.dataset.sub === "summary"));
  document.querySelectorAll(".subpanel").forEach(p => p.classList.toggle("active", p.id === "sub-summary"));
}

document.querySelector("#btn-favorite-detail").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const nextOn = btn.dataset.favorite !== "1";
  setDetailFavoriteIcon(nextOn);
  try {
    await setFavorite(currentDetailGuid, nextOn);
  } catch (err) {
    setDetailFavoriteIcon(!nextOn);
    alert(`收藏失敗：${err.message}`);
  }
});

document.querySelectorAll(".subtab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".subtab").forEach(t => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".subpanel").forEach(p => p.classList.toggle("active", p.id === `sub-${tab.dataset.sub}`));
  });
});

// ── 路由 ──────────────────────────────────────────
async function route() {
  if (!getToken() || !showUserInfo()) {
    showLogin();
    return;
  }

  const hash = location.hash || "#/";
  const path = hash.split("?")[0];
  const params = new URLSearchParams(hash.split("?")[1] || "");
  const epMatch = path.match(/^#\/ep\/(.+)$/);

  try {
    if (epMatch) {
      showDetail();
      await loadDetail(decodeURIComponent(epMatch[1]));
    } else if (path === "#/favorites") {
      showFavorites();
      await loadFavorites();
    } else if (path === "#/tags") {
      showTags();
      await loadTagsView();
    } else {
      const tagParam = params.get("tag");
      if (tagParam) {
        currentTags = [tagParam];
        currentTagMode = "any";
      }
      showList();
      await loadList();
    }
  } catch (err) {
    console.error(err);
    document.querySelector("#list-empty").hidden = false;
    document.querySelector("#list-empty").textContent = `讀取失敗：${err.message}`;
  }
}

window.addEventListener("hashchange", route);
document.querySelector("#btn-back").addEventListener("click", () => {
  history.back();
});
document.querySelector("#btn-logout").addEventListener("click", () => {
  clearToken();
  location.hash = "#/";
  route();
});

route();
