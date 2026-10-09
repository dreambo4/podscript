// ── 研究專案頁（#/project/<id>）──────────────────────────
// 研究問題、篇目、AI 整理（對照表、心智圖、缺口）與筆記。spec：specs_20261009_研究專案頁面.md
// 手機可編輯研究問題與筆記；AI 整理只在本機產生，這裡只顯示結果，也不能產生建議問題。
// 筆記不用編輯器：檢視時把 Markdown 轉成畫面，編輯是一般文字框。
// 沿用 app.js 的全域工具：api、jsonBody、escapeHtml、renderEpisodeCard、EP_KINDS、episodeKind、
// getSort、debounce、mindmapRootColor、openMindmapFull。

const PV_STATES = {
  open: { label: "還沒有答案", next: "partial" },
  partial: { label: "有初步想法", next: "resolved" },
  resolved: { label: "已釐清", next: "open" },
};

let pv = null; // GET /projects/{id} 的回應
let pvEpisodes = []; // 專案內已上傳的集數（GET /episodes?project=）
let pvCandidates = null; // 可能相關；null 表示尚未載入
let pvTab = "items";
let pvSort = "published_at";
let pvKind = ""; // "" 全部、EP_KINDS 的鍵，或 "candidates"
const pvOpen = new Set();
const pvEditing = new Map(); // 編輯中的筆記：key → { base, value, save }
let pvSorter = null;
let pvRequest = 0;

async function loadProjectView(id) {
  const request = ++pvRequest;
  if (pv?.id !== id) {
    pv = null;
    pvTab = "items";
    pvKind = "";
    pvCandidates = null;
    pvOpen.clear();
    pvEditing.clear();
    document.querySelector("#pv-search").value = "";
  }
  document.querySelector("#pv-loading").hidden = false;
  try {
    const [data, episodes] = await Promise.all([api(`/projects/${id}`), fetchProjectEpisodes(id)]);
    if (request !== pvRequest) return;
    pv = data;
    pvEpisodes = episodes;
  } catch (err) {
    if (request !== pvRequest) return;
    document.querySelector("#pv-name").textContent = "無法載入研究專案";
    document.querySelector("#pv-desc").textContent = err.message;
    return;
  } finally {
    if (request === pvRequest) document.querySelector("#pv-loading").hidden = true;
  }
  renderProjectView();
  loadCandidates(id);
}

async function fetchProjectEpisodes(id, q = "") {
  const query = new URLSearchParams({ project: id, sort: "published_at", limit: "1000" });
  if (q) query.set("q", q);
  const episodes = await api(`/episodes?${query}`);
  episodes.forEach(ep => { ep.query = q; ep.searchOptions = {}; });
  return episodes;
}

async function loadCandidates(id) {
  try {
    const items = await api(`/projects/${id}/candidates`);
    if (pv?.id !== id) return;
    pvCandidates = items;
  } catch (err) {
    console.error(err); // 附屬功能，失敗時只是不顯示
    pvCandidates = [];
  }
  if (pvTab === "items") renderPvItems();
}

function renderProjectView() {
  document.querySelector("#pv-name").textContent = pv.name;
  const desc = document.querySelector("#pv-desc");
  desc.textContent = pv.description || "";
  desc.hidden = !pv.description;
  document.querySelector("#pv-meta").textContent = `${pv.items.length} 篇 · 建立於 ${pv.created_at.slice(0, 10)}`;
  renderPvAi();
  renderPvQuestions();
  renderPvSuggestions();
  renderPvTabs();
}

function renderPvAi() {
  const ins = pv.insights;
  const box = document.querySelector("#pv-ai");
  if (!ins?.generated_at) {
    box.innerHTML = `<span class="muted">AI 整理（對照表、心智圖、研究問題缺口）尚未產生，請在本機產生。</span>`;
    return;
  }
  const used = new Set(ins.source_guids);
  const fresh = pv.items.filter(i => !used.has(i.guid)).length;
  box.innerHTML = `
    <span class="pv-ai-tag"><svg class="icon icon-xs"><use href="#ic-spark"/></svg>AI 整理 ${ins.generated_at.slice(0, 10)} 依 ${used.size} 篇摘要</span>
    ${fresh ? `<span class="pv-warn">${fresh} 篇新篇目尚未納入，到本機重新產生</span>` : ""}`;
}

// ── 研究問題 ──────────────────────────────────────────

function pvStateSvg(status) {
  if (status === "open") {
    return `<svg viewBox="0 0 22 22"><circle cx="11" cy="11" r="8" fill="none" stroke="var(--muted)" stroke-width="2"/></svg>`;
  }
  if (status === "partial") {
    return `<svg viewBox="0 0 22 22"><circle cx="11" cy="11" r="8" fill="none" stroke="var(--warn)" stroke-width="2"/><path d="M11 3a8 8 0 0 1 0 16z" fill="var(--warn)"/></svg>`;
  }
  return `<svg viewBox="0 0 22 22"><circle cx="11" cy="11" r="9" fill="var(--accent)"/><path d="M7 11.5l2.5 2.5L15 8.5" fill="none" stroke="var(--accent-ink)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

function pvTitle(guid) {
  return pvEpisodes.find(e => e.episode_guid === guid)?.title
    || pv.insights?.claims?.sources?.find(s => s.guid === guid)?.title
    || "（尚未上傳的篇目）";
}

function renderPvQuestions() {
  const list = document.querySelector("#pv-questions");
  const qs = pv.questions;
  document.querySelector("#pv-q-hint").hidden = !qs.length;
  list.innerHTML = qs.length
    ? qs.map(pvQuestionHtml).join("")
    : `<li class="muted pv-empty">還沒有研究問題。寫下你想搞清楚的事。</li>`;

  list.querySelectorAll("li[data-id]").forEach(li => {
    const q = qs.find(x => x.id === li.dataset.id);
    li.querySelector(".pv-q-state").addEventListener("click", () =>
      updatePvQuestion(q, { status: PV_STATES[q.status].next }));
    li.querySelectorAll("[data-toggle]").forEach(el => el.addEventListener("click", () => {
      if (pvOpen.has(q.id)) {
        flushPvNote(`q:${q.id}`);
        pvOpen.delete(q.id);
      } else pvOpen.add(q.id);
      renderPvQuestions();
    }));
    const box = li.querySelector(".md-box");
    if (box) mountPvNote(box, {
      key: `q:${q.id}`,
      value: () => q.note,
      empty: "還沒有筆記。按「編輯」寫下對這個問題的想法、待查事項。",
      rows: 6,
      save: async (note, base) => {
        Object.assign(q, await api(`/projects/${pv.id}/questions/${q.id}`, {
          method: "PUT", ...jsonBody({ note, base_note: base }),
        }));
      },
    });
    li.querySelector("[data-action=rename]")?.addEventListener("click", async () => {
      const text = (prompt("修改研究問題", q.text) || "").trim();
      if (text && text !== q.text) updatePvQuestion(q, { text });
    });
    li.querySelector("[data-action=delete]")?.addEventListener("click", () => deletePvQuestion(q));
  });

  pvSorter?.destroy();
  pvSorter = window.Sortable && qs.length > 1
    ? Sortable.create(list, {
      handle: ".pv-q-handle",
      animation: 150,
      ghostClass: "pv-q-ghost",
      // 長按才開始拖，手指滑過握把時仍能捲動頁面
      delay: 150,
      delayOnTouchOnly: true,
      onEnd: savePvOrder,
    })
    : null;
}

function pvQuestionHtml(q) {
  const open = pvOpen.has(q.id);
  const gap = pv.insights?.gaps?.[q.id] || null;
  return `<li class="pv-q" data-id="${q.id}">
    <div class="pv-q-row">
      <span class="pv-q-handle" aria-label="拖曳調整順序"><svg class="icon icon-sm"><use href="#ic-grip"/></svg></span>
      <button type="button" class="pv-q-state" aria-label="狀態：${PV_STATES[q.status].label}，點一下切換">${pvStateSvg(q.status)}</button>
      <div class="pv-q-body" data-toggle>
        <div class="pv-q-text">${escapeHtml(q.text)}</div>
        <div class="pv-q-sub">
          <span class="pv-q-status ${q.status}">${PV_STATES[q.status].label}</span>
          ${gap ? `<span>${gap.covered.length} 篇有談到</span>` : ""}
          ${q.note.trim() ? `<span class="pv-has-note"><svg class="icon icon-xs"><use href="#ic-note"/></svg>有筆記</span>` : ""}
        </div>
      </div>
      <button type="button" class="pv-q-open" data-toggle aria-expanded="${open}" aria-label="展開筆記與缺口">
        <svg class="icon icon-sm"><use href="#ic-chevron"/></svg>
      </button>
    </div>
    ${open ? `<div class="pv-q-detail">
      <span class="sheet-label">我的筆記</span>
      <div class="md-box small"></div>
      <div class="pv-gap"><span class="pv-ai-tag"><svg class="icon icon-xs"><use href="#ic-spark"/></svg>AI 整理的缺口</span>
        ${gap ? `<h4>有談到的篇目</h4>
          <ul>${gap.covered.length ? gap.covered.map(g => `<li>${escapeHtml(pvTitle(g))}</li>`).join("") : "<li>還沒有</li>"}</ul>
          <h4>還缺哪類來源</h4>
          <ul>${gap.missing.map(m => `<li>${escapeHtml(m)}</li>`).join("")}</ul>`
        : `<p class="muted">${pv.insights?.generated_at ? "這題在上次產生之後才新增，下次在本機產生 AI 整理時才會分析。" : "在本機產生 AI 整理後，會列出哪些篇有談到、還缺哪類來源。"}</p>`}
      </div>
      <div class="pv-q-actions">
        <button type="button" class="pv-link" data-action="rename">修改問題</button>
        <button type="button" class="pv-link danger" data-action="delete">刪除</button>
      </div>
    </div>` : ""}
  </li>`;
}

async function updatePvQuestion(q, body) {
  try {
    Object.assign(q, await api(`/projects/${pv.id}/questions/${q.id}`, { method: "PUT", ...jsonBody(body) }));
  } catch (err) {
    alert(err.message);
    if (err.status === 409) loadProjectView(pv.id);
    return;
  }
  renderPvQuestions();
}

async function deletePvQuestion(q) {
  if (!confirm(`刪除研究問題「${q.text}」？\n這題的筆記也會一併刪除。`)) return;
  try {
    await api(`/projects/${pv.id}/questions/${q.id}`, { method: "DELETE" });
  } catch (err) {
    alert(err.message);
    return;
  }
  pvEditing.delete(`q:${q.id}`);
  pv.questions = pv.questions.filter(x => x.id !== q.id);
  renderPvQuestions();
}

async function savePvOrder() {
  const ids = [...document.querySelectorAll("#pv-questions li[data-id]")].map(li => li.dataset.id);
  const byId = new Map(pv.questions.map(q => [q.id, q]));
  pv.questions = ids.map(id => byId.get(id));
  try {
    await api(`/projects/${pv.id}/questions/order`, { method: "PUT", ...jsonBody({ ids }) });
  } catch (err) {
    alert(err.message);
    loadProjectView(pv.id);
  }
}

document.querySelector("#pv-add-question").addEventListener("submit", async e => {
  e.preventDefault();
  const input = document.querySelector("#pv-new-question");
  const text = input.value.trim();
  if (!text) return;
  try {
    await withBusyButton(e.target.querySelector("button[type=submit]"), "新增中…", async () => {
      pv.questions.push(...await api(`/projects/${pv.id}/questions`, {
        method: "POST", ...jsonBody({ texts: [text] }),
      }));
    });
    input.value = "";
  } catch (err) {
    alert(err.message);
    return;
  }
  renderPvQuestions();
});

// 建議問題由本機產生；手機可以勾選加入或全部不要
function renderPvSuggestions() {
  const box = document.querySelector("#pv-suggestions");
  const items = pv.insights?.suggestions || [];
  box.hidden = !items.length;
  if (!items.length) return;
  box.innerHTML = `
    <div class="pv-sugg-head"><span class="pv-ai-tag"><svg class="icon icon-xs"><use href="#ic-spark"/></svg>AI 建議</span>
      <span class="muted">勾選要加入研究問題清單的</span></div>
    ${items.map((s, i) => `<label class="pv-sugg-item"><input type="checkbox" data-i="${i}">
      <span>${escapeHtml(s.text)}${s.why ? `<span class="muted">${escapeHtml(s.why)}</span>` : ""}</span></label>`).join("")}
    <div class="pv-sugg-foot">
      <button type="button" class="pv-primary" id="pv-sugg-add" disabled>加入所選</button>
      <button type="button" class="pv-link" id="pv-sugg-dismiss">都不要</button>
    </div>`;
  const checked = () => [...box.querySelectorAll("input[data-i]:checked")].map(c => items[+c.dataset.i]);
  box.querySelectorAll("input[data-i]").forEach(c => c.addEventListener("change", () => {
    const n = checked().length;
    const btn = document.querySelector("#pv-sugg-add");
    btn.disabled = !n;
    btn.textContent = n ? `加入所選（${n}）` : "加入所選";
  }));
  document.querySelector("#pv-sugg-add").addEventListener("click", async e => {
    try {
      await withBusyButton(e.currentTarget, "加入中…", async () => {
        pv.questions.push(...await api(`/projects/${pv.id}/questions`, {
          method: "POST", ...jsonBody({ texts: checked().map(s => s.text), from_suggestions: true }),
        }));
      });
      pv.insights.suggestions = [];
    } catch (err) {
      alert(err.message);
      return;
    }
    renderPvQuestions();
    renderPvSuggestions();
  });
  document.querySelector("#pv-sugg-dismiss").addEventListener("click", async () => {
    try {
      await api(`/projects/${pv.id}/suggestions`, { method: "DELETE" });
      pv.insights.suggestions = [];
    } catch (err) {
      alert(err.message);
      return;
    }
    renderPvSuggestions();
  });
}

// ── 分頁 ──────────────────────────────────────────────

document.querySelectorAll("[data-pv-tab]").forEach(tab => tab.addEventListener("click", () => {
  if (pvTab === "notes") flushPvNote("project");
  pvTab = tab.dataset.pvTab;
  renderPvTabs();
}));

function renderPvTabs() {
  document.querySelectorAll("[data-pv-tab]").forEach(t => t.classList.toggle("active", t.dataset.pvTab === pvTab));
  for (const name of ["items", "claims", "mindmap", "notes"]) {
    document.querySelector(`#pv-tab-${name}`).hidden = name !== pvTab;
  }
  if (pvTab === "items") renderPvItems();
  else if (pvTab === "claims") renderPvClaims();
  else if (pvTab === "mindmap") renderPvMindmap();
  else renderPvNote();
}

// ── 內容 ──────────────────────────────────────────────

document.querySelectorAll("[data-pv-sort]").forEach(btn => btn.addEventListener("click", () => {
  pvSort = btn.dataset.pvSort;
  document.querySelectorAll("[data-pv-sort]").forEach(b => b.setAttribute("aria-pressed", b === btn));
  renderPvItems();
}));

document.querySelector("#pv-search").addEventListener("input", debounce(async () => {
  if (!pv) return;
  const id = pv.id;
  const q = document.querySelector("#pv-search").value.trim();
  try {
    const episodes = await fetchProjectEpisodes(id, q);
    if (pv?.id !== id || document.querySelector("#pv-search").value.trim() !== q) return;
    pvEpisodes = episodes;
  } catch (err) {
    alert(err.message);
    return;
  }
  renderPvItems();
}, 300));

function renderPvKinds() {
  const count = k => pvEpisodes.filter(e => !k || episodeKind(e) === k).length;
  const kinds = Object.keys(EP_KINDS).filter(k => count(k));
  const cands = pvCandidates || [];
  if (pvKind && pvKind !== "candidates" && !kinds.includes(pvKind)) pvKind = "";
  if (pvKind === "candidates" && !cands.length) pvKind = "";
  const box = document.querySelector("#pv-kinds");
  box.innerHTML = [["", "全部"], ...kinds.map(k => [k, EP_KINDS[k].label])]
    .map(([k, label]) => `<button type="button" data-kind="${k}" aria-pressed="${pvKind === k}">${label} ${count(k)}</button>`)
    .join("")
    + (cands.length
      ? `<button type="button" class="pv-chip-cand" data-kind="candidates" aria-pressed="${pvKind === "candidates"}">
          <svg class="icon icon-xs"><use href="#ic-link"/></svg>可能相關 ${cands.length}</button>`
      : "");
  box.querySelectorAll("[data-kind]").forEach(btn => btn.addEventListener("click", () => {
    pvKind = btn.dataset.kind;
    renderPvItems();
  }));
}

function renderPvItems() {
  if (!pv) return;
  renderPvKinds();
  const box = document.querySelector("#pv-items");
  box.innerHTML = "";
  const searching = document.querySelector("#pv-search").value.trim();

  if (pvKind === "candidates") {
    box.insertAdjacentHTML("beforeend",
      `<p class="muted pv-cand-note">還沒歸入這個專案，但標籤與專案內容相近；依共同標籤數排序。</p>`);
    pvCandidates.forEach(ep => {
      const card = renderEpisodeCard({ ...ep, [getSort()]: ep.published_at });
      card.classList.add("pv-cand");
      card.querySelector(".tags").innerHTML =
        ep.shared_tags.map(t => `<span class="pv-shared">#${escapeHtml(t)}</span>`).join("");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pv-adopt";
      btn.textContent = "歸入";
      btn.addEventListener("click", () => adoptPvCandidate(ep, btn));
      card.querySelector(".heart-btn").replaceWith(btn);
      box.appendChild(card);
    });
    return;
  }

  const added = new Map(pv.items.map(i => [i.guid, i.added_at]));
  let shown = pvEpisodes
    .filter(e => !pvKind || episodeKind(e) === pvKind)
    .map(e => ({ ...e, added_at: added.get(e.episode_guid) || "" }));
  if (!searching) shown.sort((a, b) => (b[pvSort] || "").localeCompare(a[pvSort] || ""));

  if (!shown.length) {
    box.innerHTML = `<p class="muted pv-empty">${searching ? `這個專案裡沒有提到「${escapeHtml(searching)}」的內容`
      : pvEpisodes.length ? "這個類型沒有內容" : "還沒有已上傳的內容。到單集頁把內容歸入這個專案。"}</p>`;
    return;
  }
  let group = "";
  shown.forEach(ep => {
    if (!searching) {
      const label = (ep[pvSort] || "").slice(0, 4) || "日期不明";
      if (label !== group) {
        group = label;
        box.insertAdjacentHTML("beforeend",
          `<h3 class="pv-group">${pvSort === "added_at" ? `${label} 歸入` : label}</h3>`);
      }
    }
    // 卡片上的日期跟著目前的排序基準
    box.appendChild(renderEpisodeCard({ ...ep, [getSort()]: ep[pvSort] }));
  });
}

async function adoptPvCandidate(ep, btn) {
  btn.disabled = true;
  btn.textContent = "歸入中…";
  try {
    await api(`/episodes/${encodeURIComponent(ep.episode_guid)}/projects`, {
      method: "PUT",
      ...jsonBody({ project_ids: [...ep.project_ids, pv.id] }),
    });
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "歸入";
    alert(err.message);
    return;
  }
  projectsLoaded = false; // 專案篇數變了，列表頁下次重抓
  await loadProjectView(pv.id);
}

// ── 對照表 ────────────────────────────────────────────

// 手機版對照表：主張文字獨占一行，下一行是可左右滑的標記列；
// 所有標記列與頂端的篇目列連動，滑任何一列其他列跟著滑（使用者 2026-10-10 指定的版面）
function renderPvClaims() {
  const box = document.querySelector("#pv-tab-claims");
  const claims = pv.insights?.claims;
  if (!claims?.rows?.length) {
    box.innerHTML = `<p class="muted pv-empty">尚未產生。在本機的專案頁按「產生 AI 整理」，會依各篇摘要整理出主張對照表。</p>`;
    return;
  }
  const sources = claims.sources;
  const groups = [
    ["說法不同", r => Object.values(r.marks).includes("differ")],
    ["多篇提到", r => Object.values(r.marks).filter(m => m === "agree").length >= 2],
    ["只有一篇提到", () => true],
  ];
  const strip = cells => `<div class="pv-strip"><div class="pv-strip-inner">${cells}</div></div>`;
  const used = new Set();
  const body = groups.map(([label, test]) => {
    const rows = claims.rows.filter(r => !used.has(r) && test(r));
    rows.forEach(r => used.add(r));
    if (!rows.length) return "";
    return `<h4 class="pv-cmp-group">${label}</h4>`
      + rows.map(r => `<div class="pv-cmp-row">
          <p class="pv-cmp-claim">${escapeHtml(r.claim)}${r.note ? `<span class="muted">${escapeHtml(r.note)}</span>` : ""}</p>
          ${strip(sources.map(s => `<span class="pv-cell">${pvMark(r.marks[s.guid])}</span>`).join(""))}
        </div>`).join("");
  }).join("");
  box.innerHTML = `
    <p class="muted">${pvGenerated()}只整理各篇「說了什麼」，不判斷誰對。</p>
    <div class="pv-legend muted">
      <span><i class="pv-mark agree"></i>有此主張</span>
      <span><i class="pv-mark differ"></i>說法不同</span>
      <span><i class="pv-mark none"></i>沒提到</span>
    </div>
    <div class="pv-cmp">
      <div class="pv-cmp-head">${strip(sources.map(s => `<span class="pv-cell pv-cell-src" title="${escapeHtml(s.title)}">
        <span class="pv-src-title">${escapeHtml(s.title)}</span><span class="muted">${(s.published || "").slice(0, 4)}</span></span>`).join(""))}</div>
      ${body}
    </div>`;
  syncStrips(box);
}

/** 讓同一張對照表裡所有的標記列一起左右滑。 */
function syncStrips(box) {
  const strips = [...box.querySelectorAll(".pv-strip")];
  let leader = null; // 正在被手指滑動的那一列；其他列跟著設定，避免互相觸發
  strips.forEach(el => {
    const lead = () => { leader = el; };
    el.addEventListener("touchstart", lead, { passive: true });
    el.addEventListener("pointerdown", lead);
    el.addEventListener("wheel", lead, { passive: true });
    el.addEventListener("scroll", () => {
      if (leader && leader !== el) return;
      leader = el;
      strips.forEach(other => {
        if (other !== el) other.scrollLeft = el.scrollLeft;
      });
    }, { passive: true });
  });
}

function pvMark(mark) {
  const label = { agree: "有此主張", differ: "說法不同" }[mark] || "沒提到";
  return `<i class="pv-mark ${mark || "none"}" role="img" aria-label="${label}"></i>`;
}

function pvShort(title, limit = 10) {
  return title.length <= limit ? title : `${title.slice(0, limit)}…`;
}

function pvGenerated() {
  const ins = pv.insights;
  return ins?.generated_at ? `${ins.generated_at.slice(0, 10)} 依 ${ins.source_guids.length} 篇摘要產生。` : "";
}

// ── 心智圖 ────────────────────────────────────────────

function renderPvMindmap() {
  const code = pv.insights?.mindmap;
  document.querySelector("#pv-mindmap-wrap").hidden = !code;
  document.querySelector("#pv-mindmap-info").textContent = code
    ? pvGenerated()
    : "尚未產生。在本機的專案頁按「產生 AI 整理」，會把各篇內容整理成一張專案心智圖。";
  if (!code || typeof window.renderMarkmap !== "function") return;
  try {
    window.renderMarkmap(code, document.querySelector("#pv-mindmap"),
      { rootColor: mindmapRootColor(), interactive: false });
  } catch {
    document.querySelector("#pv-mindmap").innerHTML = `<p class="muted">心智圖語法錯誤，請在本機重新產生</p>`;
  }
}

document.querySelector("#pv-mindmap-full").addEventListener("click", e => {
  e.stopPropagation();
  openMindmapFull(pv?.insights?.mindmap);
});
document.querySelector("#pv-mindmap").addEventListener("click", e => {
  if (!e.target.closest("circle")) openMindmapFull(pv?.insights?.mindmap);
});
window.addEventListener("markmap-ready", () => {
  if (pv && pvTab === "mindmap") renderPvMindmap();
});

// ── 筆記 ──────────────────────────────────────────────
// 開始編輯時記下原內容（base），存檔時後端比對，另一台裝置改過就回 409，不覆蓋。

function pvMarkdown(text) {
  if (!window.marked || !window.DOMPurify) return `<p class="pv-plain">${escapeHtml(text)}</p>`;
  return DOMPurify.sanitize(marked.parse(text, { gfm: true, breaks: true }));
}

function mountPvNote(box, o) {
  const editing = pvEditing.get(o.key);
  box.innerHTML = `<div class="md-bar">
      <div class="md-seg" role="group" aria-label="筆記模式">
        <button type="button" data-mode="view" aria-pressed="${!editing}">檢視</button>
        <button type="button" data-mode="edit" aria-pressed="${!!editing}">編輯</button>
      </div>
      <span class="md-state muted"></span>
    </div>
    <div class="md-body"></div>`;
  const body = box.querySelector(".md-body");
  if (editing) {
    const area = document.createElement("textarea");
    area.className = "md-src";
    area.rows = o.rows || 14;
    area.value = editing.value;
    area.placeholder = "用 Markdown 寫…";
    area.addEventListener("input", () => { editing.value = area.value; });
    body.appendChild(area);
  } else {
    const text = o.value();
    body.innerHTML = text.trim() ? `<div class="md-view">${pvMarkdown(text)}</div>` : `<p class="muted">${o.empty}</p>`;
  }
  box.querySelector('[data-mode="edit"]').addEventListener("click", () => {
    if (pvEditing.has(o.key)) return;
    const value = o.value();
    pvEditing.set(o.key, { base: value, value, save: o.save });
    mountPvNote(box, o);
    box.querySelector("textarea").focus();
  });
  box.querySelector('[data-mode="view"]').addEventListener("click", async () => {
    if (!pvEditing.has(o.key)) return mountPvNote(box, o);
    box.querySelector(".md-state").textContent = "儲存中…";
    if (await flushPvNote(o.key)) {
      mountPvNote(box, o);
      if (o.key.startsWith("q:")) renderPvQuestions(); // 更新「有筆記」標記
    } else box.querySelector(".md-state").textContent = "";
  });
}

/** 把編輯中的筆記存回資料庫；沒改就直接結束編輯。成功或沒改回傳 true。 */
async function flushPvNote(key) {
  const editing = pvEditing.get(key);
  if (!editing) return true;
  if (editing.value === editing.base) {
    pvEditing.delete(key);
    return true;
  }
  try {
    await editing.save(editing.value, editing.base);
    pvEditing.delete(key);
    return true;
  } catch (err) {
    alert(err.message);
    return false;
  }
}

// 換頁前存下編輯中的筆記
window.addEventListener("hashchange", () => {
  for (const key of [...pvEditing.keys()]) flushPvNote(key);
});
window.addEventListener("beforeunload", e => {
  if ([...pvEditing.values()].some(n => n.value !== n.base)) e.preventDefault();
});

function renderPvNote() {
  const id = pv.id;
  mountPvNote(document.querySelector("#pv-note"), {
    key: "project",
    value: () => pv.note,
    empty: "還沒有專案筆記。按「編輯」用 Markdown 寫下目前的理解、還不懂的地方與下一步。",
    save: async (note, base) => {
      const res = await api(`/projects/${id}/note`, { method: "PUT", ...jsonBody({ note, base }) });
      if (pv?.id === id) {
        pv.note = note;
        pv.note_updated_at = res.note_updated_at;
        renderPvNoteInfo();
      }
    },
  });
  renderPvNoteInfo();
}

function renderPvNoteInfo() {
  document.querySelector("#pv-note-info").textContent = pv.note_updated_at
    ? `上次修改 ${new Date(pv.note_updated_at).toLocaleString("zh-TW", { hour12: false })}；切回「檢視」時儲存`
    : "切回「檢視」時儲存";
}
