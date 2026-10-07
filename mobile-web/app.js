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

  const dark = isDarkMode();
  document.querySelector("#theme-icon use").setAttribute("href", dark ? "#ic-sun" : "#ic-moon");
  document.querySelector("#menu-theme-icon use").setAttribute("href", dark ? "#ic-sun" : "#ic-moon");
  document.querySelector("#menu-theme-label").textContent = dark ? "淺色模式" : "深色模式";
}

function toggleTheme() {
  const next = isDarkMode() ? "light" : "dark";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
  // 心智圖 root 色是渲染時依主題算定的，切換主題需重繪才會更新（有心智圖時才做）。
  if (currentMindmapCode) renderMindmap();
}

// 心智圖改用 markmap 渲染（見 mindmap-render.js），透過 window.renderMarkmap 呼叫。
// 資料仍存 mermaid 語法，由該模組轉譯，故此處不再需要 mermaid.initialize。

applyTheme(getStoredTheme());
document.querySelector("#btn-theme").addEventListener("click", toggleTheme);

// ── 使用者選單（名稱下拉：深淺色切換、登出）──────────────
const userMenuBtn = document.querySelector("#btn-user-menu");
const userMenu = document.querySelector("#user-menu");

function setUserMenuOpen(open) {
  userMenu.hidden = !open;
  userMenuBtn.setAttribute("aria-expanded", String(open));
}

userMenuBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  setUserMenuOpen(userMenu.hidden);
});
document.addEventListener("click", (e) => {
  if (!userMenu.hidden && !userMenu.contains(e.target)) setUserMenuOpen(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !userMenu.hidden) setUserMenuOpen(false);
});
document.querySelector("#btn-menu-theme").addEventListener("click", () => {
  toggleTheme();
  setUserMenuOpen(false);
});

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
  document.querySelector("#user-name").textContent = name;
  // 登入後深淺色切換收進名稱下拉選單；未登入時保留 topbar 上的按鈕
  document.querySelector("#btn-theme").hidden = true;
  document.querySelector("#btn-tags-entry").hidden = false;
  document.querySelector("#btn-favorites-entry").hidden = false;
  return true;
}

function hideUserInfo() {
  document.querySelector("#g_id_signin").hidden = false;
  document.querySelector("#user-info").hidden = true;
  document.querySelector("#btn-theme").hidden = false;
  setUserMenuOpen(false);
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
    const err = new Error(detail);
    err.status = res.status;  // 供呼叫端分辨錯誤種類，例如 404 代表後端尚未提供該 API
    throw err;
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

// 搜尋結果的命中片段：逐字稿第一個命中處前後幾句，附命中次數
function snippetHtml(ep) {
  if (!ep.snippet) return "";
  return `<span class="ep-snippet">${highlightHtml(ep.snippet, ep.query, ep.searchOptions)}<span class="ep-match-count">${ep.match_count} 處</span></span>`;
}

function renderEpisodeCard(ep) {
  const div = document.createElement("div");
  div.className = "ep-card";
  // 從搜尋結果點進去時帶上關鍵字，詳細頁據此跳到逐字稿命中處
  const href = `#/ep/${encodeURIComponent(ep.episode_guid)}${ep.query ? `?${searchHashParams(ep.query, ep.searchOptions)}` : ""}`;
  // 顯示目前排序依據的日期，與年月分組標題一致
  const date = ep[getSort()];
  div.innerHTML = `
    <a href="${href}" class="ep-link">
      ${renderCoverHtml(ep.podcast_name)}
      <div class="ep-body">
        <span class="ep-title">${escapeHtml(ep.title)}</span>
        <span class="ep-meta">${isArticle(ep) ? `<svg class="icon icon-xs ep-kind-icon" aria-label="文章"><use href="#ic-article"/></svg>` : ""}${escapeHtml(ep.podcast_name)}${date ? " · " + date.slice(0, 10) : ""}${ep.duration_sec ? " · " + formatDuration(ep.duration_sec) : ""}</span>
        ${snippetHtml(ep)}
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

// ── 快速捲動把手（仿 Google 相簿）────────────────────
// 列表（依年月）與逐字稿（依章節）共用。
// 把手位置 = 目前捲動量在「第一個區段 ~ 頁尾」之間的比例；拖曳把手時反向換算成捲動量。
// 目前區段以一條「判斷線」決定：平常貼齊畫面頂端（固定標題下方），進入最後一個畫面高度的
// 捲動量後逐漸移到畫面底部，因此捲不到頂端的末段區段在拖到底時仍會被選到。軌道刻度用同一套換算。
// 拖曳期間把手直接跟著手指，換算基準（geo）在按下時固定；頁面不捲動，
// 以 transform 位移內容預覽，放開時才捲到目的地。
const SCRUBBER_HIDE_DELAY = 1500;
const SCRUBBER_LABEL_MIN_GAP = 26; // 軌道刻度間距小於此值（px）時略過，避免互相重疊
const SCRUBBER_HEADING_ROOM = 48;  // 判斷線在畫面底部時保留的高度，讓該區段標題仍在畫面內

/**
 * @param {object} config
 * @param {HTMLElement} config.el            把手容器（.scrubber）
 * @param {string} config.sectionSelector    可跳轉的區段，依文件順序
 * @param {(section: HTMLElement) => string} config.bubbleLabel  拖曳時把手旁顯示的文字
 * @param {(section: HTMLElement) => string|null} config.trackLabel  軌道刻度文字；與前一個刻度相同或為 null 時不顯示
 * @param {() => number} config.stickyOffset 區段捲到頂端時，其上方被固定元素佔去的高度（clientY）
 * @param {string} config.movingSelector     拖曳期間以 transform 位移預覽的內容
 */
function createScrubber(config) {
  const { el } = config;
  const thumb = el.querySelector(".scrubber-thumb");
  const bubble = el.querySelector(".scrubber-bubble");
  const labelsEl = el.querySelector(".scrubber-years");
  const state = {
    enabled: false,
    dragging: false,
    hideTimer: null,
    geo: null,      // 拖曳期間固定的換算基準，見 measureGeo
    sections: [],   // 拖曳開始時快取 [{ top, bubble, track }]，top 為該區段捲到頂端時的 scrollY
  };

  // start ~ end：第一個區段捲到頂端 ~ 頁尾的捲動量；travel：判斷線從頂端移到底部的最大位移；
  // track：把手可移動的高度
  function measureGeo() {
    const offset = config.stickyOffset();
    const first = document.querySelector(config.sectionSelector);
    const start = first ? first.getBoundingClientRect().top + window.scrollY - offset : 0;
    const end = document.documentElement.scrollHeight - window.innerHeight;
    const travel = Math.max(0, window.innerHeight - offset - SCRUBBER_HEADING_ROOM);
    const track = el.clientHeight - thumb.offsetHeight;
    return { start, end, travel, track };
  }

  function ratioOf(scrollY, { start, end }) {
    return end > start ? Math.min(1, Math.max(0, (scrollY - start) / (end - start))) : 0;
  }

  // 判斷線開始往下移的捲動量
  function tailStart({ start, end, travel }) {
    return Math.max(start, end - travel);
  }

  // 捲動量 scrollY 對應的判斷線位置（與 section.top 同一座標）
  function lineAt(scrollY, geo) {
    const tail = tailStart(geo);
    if (scrollY <= tail || geo.end <= tail) return scrollY;
    return scrollY + geo.travel * Math.min(1, (scrollY - tail) / (geo.end - tail));
  }

  // lineAt 的反函數：判斷線剛好碰到 top 時的捲動量
  function scrollForLine(top, geo) {
    const tail = tailStart(geo);
    if (top <= tail || geo.end <= tail) return top;
    const tailLen = geo.end - tail;
    return Math.min(geo.end, (top * tailLen + geo.travel * tail) / (tailLen + geo.travel));
  }

  function isShown() {
    return state.enabled && el.getClientRects().length > 0;
  }

  function setThumb(ratio, geo) {
    thumb.style.transform = `translateY(${ratio * geo.track}px)`;
  }

  function cacheSections() {
    const offset = config.stickyOffset();
    state.sections = [...document.querySelectorAll(config.sectionSelector)].map(section => ({
      top: section.getBoundingClientRect().top + window.scrollY - offset,
      bubble: config.bubbleLabel(section),
      track: config.trackLabel(section),
    }));
  }

  function renderTrackLabels(geo) {
    const thumbHalf = thumb.offsetHeight / 2;
    let lastText = null;
    let lastY = -Infinity;
    const html = [];

    state.sections.forEach(({ top, track }) => {
      if (!track || track === lastText) return;
      lastText = track;
      const y = ratioOf(scrollForLine(top, geo), geo) * geo.track + thumbHalf;
      if (y - lastY < SCRUBBER_LABEL_MIN_GAP) return;
      lastY = y;
      html.push(`<span class="scrubber-year" style="top:${y}px">${escapeHtml(track)}</span>`);
    });

    labelsEl.innerHTML = html.join("");
  }

  function updateBubble(scrollY, geo) {
    const line = lineAt(scrollY, geo);
    let current = state.sections[0];
    for (const section of state.sections) {
      if (section.top <= line + 1) current = section;
      else break;
    }
    bubble.textContent = current ? current.bubble : "";

    // 與泡泡垂直重疊的刻度先隱藏，避免文字疊在一起
    const bubbleRect = bubble.getBoundingClientRect();
    labelsEl.querySelectorAll(".scrubber-year").forEach(label => {
      const rect = label.getBoundingClientRect();
      label.classList.toggle("covered", rect.bottom > bubbleRect.top - 4 && rect.top < bubbleRect.bottom + 4);
    });
  }

  function showBriefly() {
    el.classList.add("visible");
    clearTimeout(state.hideTimer);
    state.hideTimer = setTimeout(() => {
      if (!state.dragging) el.classList.remove("visible");
    }, SCRUBBER_HIDE_DELAY);
  }

  let startClientY = 0;
  let startThumbY = 0;   // 按下時把手在軌道內的位置
  let startScrollY = 0;  // 按下時的捲動量，拖曳期間頁面維持在此
  let targetScrollY = 0; // 放開時要捲到的位置
  let movingEls = [];

  window.addEventListener("scroll", () => {
    if (state.dragging || !isShown()) return;
    const geo = measureGeo();
    if (geo.end <= geo.start) return;
    setThumb(ratioOf(window.scrollY, geo), geo);
    showBriefly();
  }, { passive: true });

  // 手指在把手上移動時不可觸發頁面的原生捲動
  thumb.addEventListener("touchstart", e => e.preventDefault(), { passive: false });
  thumb.addEventListener("touchmove", e => e.preventDefault(), { passive: false });

  thumb.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    thumb.setPointerCapture(e.pointerId);
    state.dragging = true;
    state.geo = measureGeo();
    startClientY = e.clientY;
    startThumbY = thumb.getBoundingClientRect().top - el.getBoundingClientRect().top;
    startScrollY = window.scrollY;
    targetScrollY = startScrollY;
    // 先量完區段位置再取消固定標題，避免量到固定狀態下的位置
    cacheSections();
    movingEls = [...document.querySelectorAll(config.movingSelector)];
    movingEls.forEach(node => { node.style.willChange = "transform"; });
    document.documentElement.classList.add("scrubbing");
    renderTrackLabels(state.geo);
    updateBubble(startScrollY, state.geo);
    el.classList.add("dragging");
    clearTimeout(state.hideTimer);
  });

  thumb.addEventListener("pointermove", (e) => {
    if (!state.dragging) return;
    const geo = state.geo;
    const ratio = geo.track > 0 ? Math.min(1, Math.max(0, (startThumbY + e.clientY - startClientY) / geo.track)) : 0;
    targetScrollY = geo.start + ratio * (geo.end - geo.start);
    setThumb(ratio, geo);
    updateBubble(targetScrollY, geo);
    const offset = startScrollY - targetScrollY;
    movingEls.forEach(node => { node.style.transform = `translateY(${offset}px)`; });
  });

  const endDrag = () => {
    if (!state.dragging) return;
    state.dragging = false;
    state.geo = null;
    window.scrollTo(0, targetScrollY);
    movingEls.forEach(node => { node.style.transform = ""; node.style.willChange = ""; });
    movingEls = [];
    document.documentElement.classList.remove("scrubbing");
    el.classList.remove("dragging");
    showBriefly();
  };
  thumb.addEventListener("pointerup", endDrag);
  thumb.addEventListener("pointercancel", endDrag);

  return {
    /** 內容重新渲染後呼叫；enabled 為 false 時不顯示把手。 */
    refresh(enabled) {
      state.enabled = enabled;
      el.hidden = !enabled;
      el.classList.remove("visible", "dragging");
    },
  };
}

function topbarBottom() {
  return document.querySelector(".topbar")?.getBoundingClientRect().bottom ?? 0;
}

const listScrubber = createScrubber({
  el: document.querySelector("#scrubber"),
  sectionSelector: "#episode-groups > section",
  bubbleLabel: section => section.dataset.group === "unknown"
    ? "日期不明"
    : YEAR_MONTH_LABEL(...section.dataset.group.split("-").map(Number)),
  trackLabel: section => section.dataset.group === "unknown" ? null : `${section.dataset.group.slice(0, 4)}年`,
  stickyOffset: topbarBottom,
  movingSelector: "#list-view > :not(.scrubber)",
});

function chapterTitleOf(section) {
  return section.querySelector(".chapter-title > span:last-child")?.textContent ?? "";
}

const chapterScrubber = createScrubber({
  el: document.querySelector("#chapter-scrubber"),
  sectionSelector: "#transcript .chapter[id]",
  bubbleLabel: chapterTitleOf,
  trackLabel: chapterTitleOf,
  stickyOffset: () => topbarBottom() + (document.querySelector("#detail-view .subtabbar")?.offsetHeight ?? 0),
  movingSelector: "#detail-view > .detail-hero, #detail-view > .subtabbar, #sub-transcript > .block",
});

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
  listScrubber.refresh(groups.size > 1);
  document.querySelector("#list-empty").hidden = episodes.length > 0;
}

async function ensureChannelsLoaded() {
  if (channelsLoaded) return;
  allChannels = await api("/channels");
  channelsLoaded = true;
}

async function loadList({ quiet = false } = {}) {
  const requestId = ++listRequestId;

  // 待處理清單與集數列表互不相依，平行拉取，失敗各自處理
  loadQueue();

  await ensureChannelsLoaded();
  renderSheetChannelChips();
  renderActiveFilters();
  updateFilterDot();

  const q = document.querySelector("#search").value.trim();
  const query = new URLSearchParams();
  if (q) query.set("q", q);
  if (searchOptions.caseSensitive) query.set("case_sensitive", "true");
  if (searchOptions.wholeWord) query.set("whole_word", "true");
  currentTags.forEach(t => query.append("tags", t));
  query.set("tag_mode", currentTagMode);
  if (currentChannel) query.set("channel", currentChannel);
  if (currentFavoritesOnly) query.set("favorites_only", "true");
  query.set("sort", getSort());
  query.set("limit", 500); // 一次性全拉，見 spec §5.5 未來優化項目

  if (!quiet) setListLoading(true);
  let episodes;
  try {
    episodes = await api(`/episodes?${query}`);
  } finally {
    if (!quiet && requestId === listRequestId) setListLoading(false);
  }

  // 若期間又觸發了更新的請求，這次結果已過時，不渲染避免畫面閃回舊資料
  if (requestId !== listRequestId) return;

  const options = { ...searchOptions };
  episodes.forEach(ep => { ep.query = q; ep.searchOptions = options; });
  renderFilteredList(episodes);
}

function setListLoading(loading) {
  document.querySelector("#list-loading").hidden = !loading;
  document.querySelector("#episode-groups").hidden = loading;
}

document.querySelector("#search").addEventListener("input", debounce(() => loadList(), 300));

// ── 搜尋選項：大小寫須相符、全字拼寫須相符（仿 VS Code 搜尋框）──
const SEARCH_OPTIONS_KEY = "podscript_search_options";
const searchOptions = { caseSensitive: false, wholeWord: false };
try {
  Object.assign(searchOptions, JSON.parse(localStorage.getItem(SEARCH_OPTIONS_KEY)) || {});
} catch { /* 讀不到就用預設 */ }

/** 詳細頁網址的搜尋參數；只帶有開的選項 */
function searchHashParams(query, options = {}) {
  const params = new URLSearchParams({ q: query });
  if (options.caseSensitive) params.set("case", "1");
  if (options.wholeWord) params.set("word", "1");
  return params.toString();
}

function bindSearchOption(id, key) {
  const btn = document.querySelector(id);
  const sync = () => {
    btn.classList.toggle("on", searchOptions[key]);
    btn.setAttribute("aria-pressed", String(searchOptions[key]));
  };
  sync();
  btn.addEventListener("click", () => {
    searchOptions[key] = !searchOptions[key];
    sync();
    try { localStorage.setItem(SEARCH_OPTIONS_KEY, JSON.stringify(searchOptions)); } catch { /* 存不了也不影響本次搜尋 */ }
    if (document.querySelector("#search").value.trim()) loadList();
  });
}
bindSearchOption("#btn-match-case", "caseSensitive");
bindSearchOption("#btn-whole-word", "wholeWord");

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// ── 待處理區 ──────────────────────────────────────
// 待處理的 url 與 note 是使用者直接輸入的內容，
// 透過 innerHTML 顯示前必須跳脫，否則貼入含標籤的字串即可注入。
function escapeHtml(text) {
  return String(text ?? "").replace(
    /[&<>"']/g,
    ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]
  );
}

/**
 * 搜尋比對規則，須與後端 episodes.py 的 _regex_pattern 一致：
 * 關鍵字只比對字面；全字相符時「字」只算英數與底線，
 * 且只在關鍵字頭（尾）是英數時才檢查前（後）一字（中文與英文常直接相連）。
 */
function searchRegex(query, { caseSensitive = false, wholeWord = false } = {}) {
  let source = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (wholeWord) {
    if (/^\w/.test(query)) source = "(?<![A-Za-z0-9_])" + source;
    if (/\w$/.test(query)) source += "(?![A-Za-z0-9_])";
  }
  return new RegExp(source, caseSensitive ? "g" : "gi");
}

/** 跳脫 HTML 後以 <mark> 標出所有命中處；先切段再各自跳脫，標記不會被當成內容。 */
function highlightHtml(text, query, options) {
  text = String(text ?? "");
  if (!query) return escapeHtml(text);
  let html = "";
  let from = 0;
  for (const match of text.matchAll(searchRegex(query, options))) {
    html += escapeHtml(text.slice(from, match.index)) + `<mark>${escapeHtml(match[0])}</mark>`;
    from = match.index + match[0].length;
  }
  return html + escapeHtml(text.slice(from));
}

// 手機端只把網址存進資料庫，實際下載與轉錄回本機端再跑。
const QUEUE_OPEN_KEY = "podscript_queue_open";

let queueItems = [];

function queueMessage(text, ok = false) {
  const el = document.querySelector("#queue-msg");
  el.textContent = text;
  el.classList.toggle("ok", ok);
  el.hidden = !text;
}

function renderQueue() {
  const container = document.querySelector("#queue-items");
  const badge = document.querySelector("#queue-count");

  badge.textContent = queueItems.length;
  badge.hidden = queueItems.length === 0;
  document.querySelector("#queue-empty").hidden = queueItems.length > 0;

  container.innerHTML = "";
  queueItems.forEach(item => {
    const div = document.createElement("div");
    div.className = "queue-item";
    div.innerHTML = `
      <div class="queue-item-body">
        <span class="queue-item-url">${escapeHtml(item.title || item.url)}</span>
        ${item.note ? `<span class="queue-item-note">${escapeHtml(item.note)}</span>` : ""}
      </div>
      <button type="button" class="queue-item-del" aria-label="移除">
        <svg class="icon icon-sm"><use href="#ic-trash"/></svg>
      </button>
    `;
    div.querySelector(".queue-item-del").addEventListener("click", async () => {
      try {
        await api(`/queue/${item.id}`, { method: "DELETE" });
        queueItems = queueItems.filter(i => i.id !== item.id);
        renderQueue();
      } catch (err) {
        queueMessage(`移除失敗：${err.message}`);
      }
    });
    container.appendChild(div);
  });
}

async function loadQueue() {
  const section = document.querySelector("#queue-section");
  try {
    queueItems = await api("/queue?status=pending");
  } catch (err) {
    // 404 代表後端還沒部署這個 API，使用者對此無能為力，整區靜默隱藏。
    // 其餘錯誤（連線失敗、500）才提示，且只在待處理區內，不影響集數列表。
    console.error(err);
    if (err.status === 404) {
      section.hidden = true;
    } else {
      queueMessage(`待處理清單讀取失敗：${err.message}`);
    }
    return;
  }
  section.hidden = false;
  renderQueue();
}

function setQueueOpen(open) {
  document.querySelector("#queue-toggle").setAttribute("aria-expanded", String(open));
  document.querySelector("#queue-body").hidden = !open;
  localStorage.setItem(QUEUE_OPEN_KEY, open ? "1" : "0");
}

document.querySelector("#queue-toggle").addEventListener("click", () => {
  const open = document.querySelector("#queue-toggle").getAttribute("aria-expanded") !== "true";
  setQueueOpen(open);
});

document.querySelector("#queue-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const urlInput = document.querySelector("#queue-url");
  const noteInput = document.querySelector("#queue-note");
  const btn = e.currentTarget.querySelector(".queue-add-btn");

  const url = urlInput.value.trim();
  if (!url) return;

  btn.disabled = true;
  queueMessage("");
  try {
    const item = await api("/queue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, note: noteInput.value.trim() || null }),
    });
    urlInput.value = "";
    noteInput.value = "";
    // 重複貼同一網址時後端回傳既有那筆，這裡去重避免列表出現兩筆
    queueItems = [item, ...queueItems.filter(i => i.id !== item.id)];
    renderQueue();
    queueMessage("已加入待處理，回家開本機端處理。", true);
  } catch (err) {
    queueMessage(err.message);
  } finally {
    btn.disabled = false;
  }
});

setQueueOpen(localStorage.getItem(QUEUE_OPEN_KEY) === "1");

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
async function loadFavorites({ quiet = false } = {}) {
  const loading = document.querySelector("#favorites-loading");
  const container = document.querySelector("#favorites-cards");
  const empty = document.querySelector("#favorites-empty");

  if (!quiet) {
    loading.hidden = false;
    container.innerHTML = "";
  }
  empty.hidden = true;

  let episodes;
  try {
    episodes = await api(`/episodes?favorites_only=true&sort=${getSort()}&limit=500`);
  } finally {
    loading.hidden = true;
  }

  // 安靜刷新時舊卡片仍在畫面上，取得新資料後才換掉
  if (quiet) container.innerHTML = "";

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

const PLATFORM_LABELS = { apple: "Apple Podcasts", youtube: "YouTube", article: "原網站" };

function isArticle(ep) {
  return ep?.platform === "article";
}

/**
 * 只接受 https 網址。source_url 會放進 href，
 * 擋掉 javascript: 等 scheme（本機端 Apple 的網址判斷只看是否含網域字串）。
 */
function safeSourceUrl(url) {
  return /^https:\/\//i.test(url || "") ? url : null;
}

/** YouTube 單集指定秒數的影片連結；非 YouTube 回傳 null。 */
function youtubeTimeUrl(ep, seconds) {
  if (ep.platform !== "youtube") return null;
  const source = safeSourceUrl(ep.source_url);
  if (!source) return null;
  const url = new URL(source);
  url.searchParams.set("t", `${Math.floor(seconds)}s`);
  return url.href;
}

function renderSourceLink(ep) {
  const link = document.querySelector("#ep-source");
  const url = safeSourceUrl(ep.source_url);
  link.hidden = !url;
  if (!url) return;
  link.href = url;
  link.querySelector("span").textContent = `在 ${PLATFORM_LABELS[ep.platform] || "原平台"} 開啟`;
}

function renderTranscript(ep, query = "", options = {}) {
  const container = document.querySelector("#transcript");
  renderChapters(ep);
  const article = isArticle(ep);
  const segments = ep.transcript || [];
  const speakers = ep.speakers || {};
  const chapters = ep.chapters || [];
  const startsAt = chapterStarts(segments, chapters, article);
  // 每章包成一個 section：標題 sticky 只在所屬 section 內固定，捲到下一章時被推走。
  const html = [];
  segments.forEach((seg, i) => {
    const index = startsAt.get(i);
    if (index !== undefined) {
      if (i > 0) html.push("</section>");
      // 文章章節以段落定位，沒有時間
      const time = article ? "" : `<span class="chapter-time">${formatTime(seg.start)}</span>`;
      html.push(`<section class="chapter" id="chapter-${index}">
        <h4 class="chapter-title">
          ${time}
          <span>${escapeHtml(chapters[index].title)}</span>
        </h4>`);
    } else if (i === 0 && chapters.length) {
      html.push(`<section class="chapter">`);
    }

    // 文章只有段落，沒有時間與說話者
    if (article) {
      html.push(`<p>${highlightHtml(seg.text, query, options)}</p>`);
      return;
    }
    const name = speakers[seg.speaker] || seg.speaker;
    // 只有 YouTube 集數的時間戳可點，開影片跳到該處。
    const youtube = youtubeTimeUrl(ep, seg.start);
    const time = youtube
      ? `<a class="seg-time" href="${escapeHtml(youtube)}" target="_blank" rel="noopener noreferrer">${formatTime(seg.start)}</a>`
      : `<span class="seg-time">${formatTime(seg.start)}</span>`;
    html.push(`<div class="seg${seg.confidence < 0.6 ? " low" : ""}">
      <div class="seg-head">
        <span class="seg-speaker">${escapeHtml(name)}</span>
        ${time}
      </div>
      <p>${highlightHtml(seg.text, query, options)}</p>
    </div>`);
  });
  if (chapters.length) html.push("</section>");
  container.innerHTML = article ? `<div class="article-text">${html.join("")}</div>` : html.join("");
  chapterScrubber.refresh(container.querySelectorAll(".chapter[id]").length >= 2);
}

/**
 * 各章從第幾段開始：{段落索引: 章節索引}。
 * 本機端已對齊，Podcast 章節的 start 等於某一段的 start；文章章節的 paragraph 即段落索引。
 */
function chapterStarts(segments, chapters, article) {
  const bySegment = new Map();
  chapters.forEach((c, i) => {
    const seg = article ? c.paragraph : segments.findIndex(s => s.start === c.start);
    if (seg >= 0 && !bySegment.has(seg)) bySegment.set(seg, i);
  });
  return bySegment;
}

/** 逐字稿上方的章節目錄；沒有章節時不顯示。 */
function renderChapters(ep) {
  const el = document.querySelector("#chapters");
  const chapters = ep.chapters || [];
  el.hidden = !chapters.length;
  if (el.hidden) {
    el.innerHTML = "";
    return;
  }

  const article = isArticle(ep);
  el.innerHTML = `<details class="chapters-toc" open>
    <summary>章節（${chapters.length}）</summary>
    <ol>${chapters
      .map((c, i) => `<li>
        ${article ? "" : `<span class="chapter-time">${formatTime(c.start)}</span>`}
        <a href="#" data-chapter="${i}">${escapeHtml(c.title)}</a>
      </li>`)
      .join("")}</ol>
  </details>`;

  el.querySelectorAll("a[data-chapter]").forEach(a => {
    a.addEventListener("click", e => {
      e.preventDefault();
      document.getElementById(`chapter-${a.dataset.chapter}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
}

// ── 逐字稿命中導覽 ──────────────────────────────────
// 從搜尋結果進來時標亮所有命中處，底部浮出「第 i／N 處」切換列。
let detailEp = null;
let hitMarks = [];
let hitIndex = 0;

function showHits(ep, query, options) {
  renderTranscript(ep, query, options);
  hitMarks = [...document.querySelectorAll("#transcript mark")];
  const bar = document.querySelector("#hit-nav");
  bar.hidden = hitMarks.length === 0;
  if (!hitMarks.length) return false; // 只命中標題：維持一般顯示
  selectSubtab("transcript");
  goToHit(0);
  return true;
}

function goToHit(index) {
  hitMarks[hitIndex]?.classList.remove("current");
  hitIndex = (index + hitMarks.length) % hitMarks.length;
  const mark = hitMarks[hitIndex];
  mark.classList.add("current");
  mark.scrollIntoView({ block: "center", behavior: "smooth" });
  document.querySelector("#hit-count").textContent = `第 ${hitIndex + 1}／${hitMarks.length} 處`;
}

function clearHits() {
  hitMarks = [];
  document.querySelector("#hit-nav").hidden = true;
  if (detailEp) renderTranscript(detailEp);
  // 網址拿掉關鍵字，重新整理才不會又標亮；replaceState 不觸發 hashchange
  history.replaceState(null, "", `#/ep/${encodeURIComponent(currentDetailGuid)}`);
}

document.querySelector("#btn-hit-prev").addEventListener("click", () => goToHit(hitIndex - 1));
document.querySelector("#btn-hit-next").addEventListener("click", () => goToHit(hitIndex + 1));
document.querySelector("#btn-hit-close").addEventListener("click", clearHits);

function setDetailFavoriteIcon(isFavorite) {
  const btn = document.querySelector("#btn-favorite-detail");
  btn.classList.toggle("on", isFavorite);
  btn.dataset.favorite = isFavorite ? "1" : "0";
  document.querySelector("#favorite-detail-icon use").setAttribute("href", isFavorite ? "#ic-heart-fill" : "#ic-heart");
}

let currentDetailGuid = null;
let currentMindmapCode = null;

// 深色模式下 root 用較亮的灰，避免在深底糊掉；分支色為 HSL 高明度、深底仍清晰。
function mindmapRootColor() {
  return isDarkMode() ? "#cbd5e1" : "#475569";
}

// 頁面內是唯讀縮圖：不攔手勢，手指滑過去照常捲頁面；要拖曳縮放時點進全螢幕。
function renderMindmap() {
  const wrap = document.querySelector("#mindmap-wrap");
  const empty = document.querySelector("#mindmap-empty");
  const message = !currentMindmapCode ? "（尚無心智圖）"
    : typeof window.renderMarkmap !== "function" ? "（心智圖元件尚未載入）" : null;
  wrap.hidden = message !== null;
  empty.hidden = message === null;
  if (message) {
    empty.textContent = message;
    return;
  }
  window.renderMarkmap(currentMindmapCode, document.querySelector("#mindmap"),
    { rootColor: mindmapRootColor(), interactive: false });
  if (fullMindmap) openMindmapFull(); // 全螢幕中切換主題時一併重繪
}

// ── 心智圖全螢幕 ──────────────────────────────────
let fullMindmap = null;
const mindmapOverlay = document.querySelector("#mindmap-overlay");

function openMindmapFull() {
  if (!currentMindmapCode || typeof window.renderMarkmap !== "function") return;
  mindmapOverlay.hidden = false;
  document.body.classList.add("scroll-locked");
  fullMindmap?.destroy();
  // 必須在 overlay 顯示後才渲染，markmap 依容器實際尺寸 fit。
  fullMindmap = window.renderMarkmap(currentMindmapCode, document.querySelector("#mindmap-full"),
    { rootColor: mindmapRootColor() });
}

function closeMindmapFull() {
  if (mindmapOverlay.hidden) return;
  mindmapOverlay.hidden = true;
  document.body.classList.remove("scroll-locked");
  fullMindmap?.destroy();
  fullMindmap = null;
}

document.querySelector("#btn-mindmap-full").addEventListener("click", (e) => {
  e.stopPropagation();
  openMindmapFull();
});
// 點縮圖任一處也進全螢幕；點節點的圓點仍是展開/收合。
document.querySelector("#mindmap").addEventListener("click", (e) => {
  if (!e.target.closest("circle")) openMindmapFull();
});
document.querySelector("#btn-mindmap-close").addEventListener("click", closeMindmapFull);
document.querySelector("#btn-mindmap-reset").addEventListener("click", () => fullMindmap?.fit());
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeMindmapFull();
});
// 換頁（含返回鍵）時關掉，避免全螢幕蓋在別的頁面上。
window.addEventListener("hashchange", closeMindmapFull);
window.addEventListener("markmap-ready", () => {
  if (currentMindmapCode) renderMindmap();
});
// 轉橫/轉直後依新尺寸重新置中。
window.addEventListener("resize", () => fullMindmap?.fit());

async function loadDetail(guid, query = "", options = {}) {
  currentDetailGuid = guid;
  const ep = await api(`/episodes/${encodeURIComponent(guid)}`);
  detailEp = ep;

  document.querySelector("#ep-title").textContent = ep.title;
  document.querySelector("#ep-meta").textContent =
    `${ep.podcast_name}${ep.published_at ? " · " + ep.published_at.slice(0, 10) : ""}${ep.duration_sec ? " · " + formatDuration(ep.duration_sec) : ""}`;
  renderSourceLink(ep);
  document.querySelector('.subtab[data-sub="transcript"]').textContent = isArticle(ep) ? "原文" : "逐字稿";
  document.querySelector("#summary-text").textContent = ep.summary || "（尚無摘要）";
  document.querySelector("#hashtags").innerHTML =
    (ep.hashtags || []).map(t => `<a href="#/?tag=${encodeURIComponent(t)}"><span><svg class="icon icon-xs"><use href="#ic-tag"/></svg>${t}</span></a>`).join("");

  setDetailFavoriteIcon(ep.is_favorite);

  currentMindmapCode = ep.mindmap_mermaid || null;
  await renderMindmap();

  selectSubtab("summary");
  if (!showHits(ep, query, options)) renderTranscript(ep);
}

function selectSubtab(name) {
  document.querySelectorAll(".subtab").forEach(t => t.classList.toggle("active", t.dataset.sub === name));
  document.querySelectorAll(".subpanel").forEach(p => p.classList.toggle("active", p.id === `sub-${name}`));
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
  tab.addEventListener("click", () => selectSubtab(tab.dataset.sub));
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
      await loadDetail(decodeURIComponent(epMatch[1]), params.get("q") || "", {
        caseSensitive: params.get("case") === "1",
        wholeWord: params.get("word") === "1",
      });
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
      // manifest 的「加入待處理」捷徑會帶 queue=open，直接展開該區
      if (params.get("queue") === "open") setQueueOpen(true);
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
  setUserMenuOpen(false);
  clearToken();
  location.hash = "#/";
  route();
});

route();

// ── 下拉刷新 ──────────────────────────────────────
// 手機端不輪詢，資料只在載入時抓一次；下拉是使用者主動更新的入口。
// 整頁滾動，故在 document 上監聽，並只在捲到頂端時才接管手勢。
const PTR_THRESHOLD = 70;   // 觸發刷新的下拉距離
const PTR_MAX = 110;        // 指示器最多跟到這裡，再拉也不會更遠
const PTR_RESISTANCE = .5;  // 阻尼：手指位移打對折，避免一拉就到底

let ptrStartY = null;
let ptrDistance = 0;
let ptrRefreshing = false;

function ptrElement() {
  return document.querySelector("#ptr");
}

function ptrSetPosition(distance, animating) {
  const el = ptrElement();
  el.classList.toggle("animating", animating);
  if (distance <= 0) {
    el.style.transform = "translateY(-40px)";
    el.style.opacity = "0";
    return;
  }
  const clamped = Math.min(distance, PTR_MAX);
  el.style.transform = `translateY(${clamped - 34}px) rotate(${clamped * 3}deg)`;
  el.style.opacity = String(Math.min(clamped / PTR_THRESHOLD, 1));
}

// 可刷新的頁面與其載入函式；詳情頁與登入頁不支援下拉。
// quiet：下拉已有自己的轉圈指示器，不再顯示頁內的載入狀態。
function ptrCurrentLoader() {
  if (!document.querySelector("#list-view").hidden) return () => loadList({ quiet: true });
  if (!document.querySelector("#favorites-view").hidden) return () => loadFavorites({ quiet: true });
  if (!document.querySelector("#tags-view").hidden) return () => loadTagsView();
  return null;
}

function ptrCanStart() {
  if (ptrRefreshing) return false;
  // 篩選 sheet 展開時，下拉是關閉 sheet 的手勢，不該被刷新攔截
  if (!document.querySelector("#filter-sheet").hidden) return false;
  if (!getToken()) return false;
  return window.scrollY <= 0 && ptrCurrentLoader() !== null;
}

document.addEventListener("touchstart", (e) => {
  // 拖曳日期把手是捲動手勢，不可被當成下拉刷新
  const isOnScrubber = e.target instanceof Element && e.target.closest(".scrubber-thumb");
  ptrStartY = ptrCanStart() && e.touches.length === 1 && !isOnScrubber ? e.touches[0].clientY : null;
  ptrDistance = 0;
}, { passive: true });

document.addEventListener("touchmove", (e) => {
  if (ptrStartY === null) return;

  const delta = e.touches[0].clientY - ptrStartY;
  if (delta <= 0) {
    // 往上滑代表使用者要捲動頁面，放棄這次手勢
    ptrStartY = null;
    ptrSetPosition(0, true);
    return;
  }

  ptrDistance = delta * PTR_RESISTANCE;
  ptrSetPosition(ptrDistance, false);

  // 接管手勢後要擋掉瀏覽器自己的彈性捲動，否則畫面會一起被拉開。
  // 監聽器必須是非被動的才擋得住，故下方註冊時指定 passive: false。
  if (ptrDistance > 2 && e.cancelable) e.preventDefault();
}, { passive: false });

async function ptrFinish() {
  if (ptrStartY === null) return;
  ptrStartY = null;

  if (ptrDistance < PTR_THRESHOLD) {
    ptrSetPosition(0, true);
    return;
  }

  const loader = ptrCurrentLoader();
  if (!loader) {
    ptrSetPosition(0, true);
    return;
  }

  ptrRefreshing = true;
  const el = ptrElement();
  el.classList.add("spinning");
  ptrSetPosition(PTR_THRESHOLD, true);

  try {
    await loader();
  } catch (err) {
    console.error(err);
  } finally {
    ptrRefreshing = false;
    el.classList.remove("spinning");
    ptrSetPosition(0, true);
  }
}

document.addEventListener("touchend", ptrFinish, { passive: true });
document.addEventListener("touchcancel", ptrFinish, { passive: true });

// ── PWA ───────────────────────────────────────────
// Service Worker 只快取靜態外殼，API 一律走網路（見 sw.js）。
// 註冊失敗不影響任何功能，僅代表無法安裝到主畫面與離線開啟。
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js").catch(err => {
      console.warn("Service Worker 註冊失敗：", err);
    });
  });
}
