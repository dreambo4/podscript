// 研究專案頁（/project#<id>，獨立於單集清單）：研究問題、篇目時間軸、AI 整理（對照表、心智圖、缺口）與筆記。
// spec：.claude/specs/specs_20261009_研究專案頁面.md
// 共用工具見 common.js：$、api、escapeHtml、EP_KINDS、episodeKind、listCoverHtml、episodeRow、
// formatTime、openMindmapFull。

const QUESTION_STATES = {
  open: { label: "還沒有答案", next: "partial" },
  partial: { label: "有初步想法", next: "resolved" },
  resolved: { label: "已釐清", next: "open" },
};

let allEpisodes = []; // GET /api/episodes：篇目、標籤與所屬專案（含尚未上傳的）
let allProjects = [];
let project = null; // GET /api/projects/{id} 的回應
let projectTab = "items";
let projectSort = "published";
let projectKind = ""; // "" 全部、EP_KINDS 的鍵，或 "candidates"
let projectHits = null; // 專案內搜尋結果；null 表示沒有在搜尋
const openQuestions = new Set();
const noteEditors = new Map(); // 編輯中的筆記：key → { base, value, save }
let questionSorter = null;
let aiTimer = null;

// ── 載入與切換 ────────────────────────────────────────
// 專案本身、專案清單、全部單集三個請求同時發出，專案一到就先畫；
// 單集清單（篇目、可能相關）最慢，到了再補畫，不讓整頁等它。

const PREVIEW_KEY = "podscript_project_preview"; // 專案總覽點格子時先存名稱與說明，見 project-manage.js
let libraryLoaded = false;

async function loadLibrary() {
  allEpisodes = await api("/api/episodes");
  libraryLoaded = true;
  if (!project) return;
  renderProjectMeta();
  renderAiStatus();
  if (openQuestions.size) renderQuestions(); // 缺口裡的篇目標題取自單集清單
  if (projectTab === "items") renderItems();
}

async function loadProjects() {
  allProjects = await api("/api/projects");
  renderProjectSelect();
}

function renderProjectSelect() {
  const select = $("pp-project-select");
  select.innerHTML = allProjects
    .map((p) => `<option value="${p.id}">${escapeHtml(p.name)}（${p.item_count}）</option>`)
    .join("");
  select.value = project?.id || location.hash.slice(1);
}

$("pp-project-select").addEventListener("change", () => {
  location.hash = $("pp-project-select").value;
});

/** 專案資料還沒到之前，先顯示已知的名稱與說明（來自專案清單或總覽頁帶過來的）。 */
function showPreview(id) {
  let known = allProjects.find((p) => p.id === id);
  if (!known) {
    try {
      const saved = JSON.parse(sessionStorage.getItem(PREVIEW_KEY) || "null");
      if (saved?.id === id) known = saved;
    } catch {
      // 讀不到就只顯示「載入中」
    }
  }
  $("pp-name").textContent = known?.name || "載入中…";
  $("pp-desc").textContent = known?.description || "";
  $("pp-desc").hidden = !known?.description;
  if (known?.name) document.title = `${known.name} · 研究專案`;
}

/** 網址的 #<專案 id> 決定顯示哪個專案；沒帶時開第一個。 */
async function openFromHash() {
  let id = location.hash.slice(1);
  if (!id && allProjects.length) {
    id = allProjects[0].id;
    history.replaceState(null, "", `#${id}`);
  }
  if (project && project.id !== id) {
    flushNotes(); // 換專案前存下編輯中的筆記
    clearTimeout(aiTimer);
  }
  if (!id) {
    $("pp-name").textContent = "還沒有研究專案";
    $("pp-desc").textContent = "到頁首的「研究專案」新增專案。";
    return;
  }
  if (project?.id !== id) {
    project = null;
    showPreview(id);
    $("pp-meta").textContent = "";
    $("pp-ai").innerHTML = "";
    $("pp-questions").innerHTML = `<li class="muted pp-empty">載入中…</li>`;
    $("pp-items").innerHTML = `<li class="muted pp-empty">載入中…</li>`;
    projectTab = "items";
    projectKind = "";
    projectHits = null;
    $("pp-search").value = "";
    openQuestions.clear();
  }
  $("pp-project-select").value = id;
  try {
    await loadProject(id);
  } catch (err) {
    $("pp-name").textContent = "無法載入研究專案";
    $("pp-desc").textContent = err.message;
    return;
  }
  window.scrollTo({ top: 0 });
}

async function loadProject(id) {
  const data = await api(`/api/projects/${id}`);
  if (location.hash.slice(1) !== id) return; // 已切到別的專案
  project = data;
  document.title = `${project.name} · 研究專案`;
  renderProject();
  watchAi();
}

function renderProject() {
  $("pp-name").textContent = project.name;
  $("pp-desc").textContent = project.description || "";
  $("pp-desc").hidden = !project.description;
  renderProjectMeta();
  renderAiStatus();
  renderQuestions();
  renderSuggestions();
  renderTabs();
}

function projectEpisodes() {
  const added = new Map((project?.items || []).map((i, n) => [i.guid, { at: i.added_at, n }]));
  return allEpisodes
    .filter((e) => (e.projects || []).includes(project.id))
    .map((e) => ({ ...e, added_at: added.get(e.guid)?.at || "", added_n: added.get(e.guid)?.n ?? -1 }));
}

function renderProjectMeta() {
  const eps = projectEpisodes();
  const local = eps.filter((e) => !e.uploaded_at).length;
  $("pp-meta").innerHTML = [
    `${libraryLoaded ? eps.length : project.items.length} 篇`,
    `建立於 ${project.created_at.slice(0, 10)}`,
    local ? `<span class="pp-warn">${local} 篇尚未上傳，手機看不到</span>` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

// ── 編輯與刪除專案 ────────────────────────────────────

$("pp-edit").addEventListener("click", () => {
  if (!project) return;
  $("pp-edit-name").value = project.name;
  $("pp-edit-desc").value = project.description || "";
  $("pp-edit-dialog").showModal();
});

$("pp-edit-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "ok") return;
  e.preventDefault();
  try {
    await api(`/api/projects/${project.id}`, {
      method: "PUT",
      body: { name: $("pp-edit-name").value, description: $("pp-edit-desc").value },
    });
  } catch (err) {
    alert(err.message);
    return;
  }
  $("pp-edit-dialog").close();
  project.name = $("pp-edit-name").value.trim();
  project.description = $("pp-edit-desc").value.trim();
  document.title = `${project.name} · 研究專案`;
  renderProject();
  loadProjects();
});

$("pp-delete").addEventListener("click", async () => {
  if (!confirm(`刪除專案「${project.name}」？\n研究問題、筆記與 AI 整理會一併刪除；單集不受影響。`)) return;
  try {
    await api(`/api/projects/${project.id}`, { method: "DELETE" });
  } catch (err) {
    alert(err.message);
    return;
  }
  location.href = "/projects";
});

// ── AI 整理狀態（標題卡片）──────────────────────────────

function renderAiStatus() {
  const ins = project.insights;
  const running = project.ai?.insights?.running;
  const error = project.ai?.insights?.error;
  const used = new Set(ins?.source_guids || []);
  const fresh = ins?.generated_at
    ? projectEpisodes().filter((e) => e.has_summary && !used.has(e.guid)).length
    : 0;
  const when = ins?.generated_at
    ? `<span class="pp-ai-tag"><svg class="icon-sm"><use href="#ic-spark"/></svg>${ins.generated_at.slice(0, 10)} 依 ${used.size} 篇摘要</span>`
    : `<span class="muted">尚未產生</span>`;
  $("pp-ai").innerHTML = `
    <span class="pp-ai-what">AI 整理：對照表、心智圖、研究問題缺口（同一次產生，只用各篇摘要）</span>
    ${when}
    ${fresh ? `<span class="pp-warn">${fresh} 篇新篇目尚未納入</span>` : ""}
    <span class="pp-grow"></span>
    <button type="button" id="pp-ai-run" class="${!ins?.generated_at || fresh ? "primary" : ""}" ${running ? "disabled" : ""}>
      ${running ? '<span class="spinner"></span> 產生中…' : ins?.generated_at ? "重新產生" : "產生 AI 整理"}
    </button>
    ${error ? `<p class="error pp-ai-error">${escapeHtml(error)}</p>` : ""}`;
  $("pp-ai-run").addEventListener("click", () => startAi("insights"));
}

async function startAi(kind) {
  try {
    project.ai = await api(`/api/projects/${project.id}/${kind}`, { method: "POST" });
  } catch (err) {
    alert(err.message);
    return;
  }
  renderAiStatus();
  renderSuggestions();
  watchAi();
}

/** 有產生中的工作就每 3 秒查一次進度；結束後重新載入專案以取得結果。 */
function watchAi() {
  clearTimeout(aiTimer);
  const running = () => project?.ai?.insights?.running || project?.ai?.suggestions?.running;
  if (!running()) return;
  const id = project.id;
  aiTimer = setTimeout(async () => {
    if (project?.id !== id) return;
    try {
      const ai = await api(`/api/projects/${id}/ai`);
      const finished = !ai.insights.running && !ai.suggestions.running;
      project.ai = ai;
      if (finished) {
        // 編輯中的筆記保留在 noteEditors，重畫後仍是編輯狀態，不必先存
        await loadProject(id);
        return;
      }
    } catch (err) {
      console.error(err);
    }
    watchAi();
  }, 3000);
}

// ── 研究問題 ──────────────────────────────────────────

function questionStateSvg(status) {
  const color = { open: "var(--muted)", partial: "var(--warn)", resolved: "var(--accent)" }[status];
  if (status === "open") {
    return `<svg viewBox="0 0 22 22"><circle cx="11" cy="11" r="8" fill="none" stroke="${color}" stroke-width="2"/></svg>`;
  }
  if (status === "partial") {
    return `<svg viewBox="0 0 22 22"><circle cx="11" cy="11" r="8" fill="none" stroke="${color}" stroke-width="2"/><path d="M11 3a8 8 0 0 1 0 16z" fill="${color}"/></svg>`;
  }
  return `<svg viewBox="0 0 22 22"><circle cx="11" cy="11" r="9" fill="${color}"/><path d="M7 11.5l2.5 2.5L15 8.5" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

/** 這題的缺口；產生 AI 整理之後才新增的問題沒有缺口。 */
function questionGap(q) {
  return project.insights?.gaps?.[q.id] || null;
}

function episodeTitle(guid) {
  return allEpisodes.find((e) => e.guid === guid)?.title || "（已不在單集清單）";
}

function renderQuestions() {
  const list = $("pp-questions");
  const qs = project.questions;
  $("pp-q-hint").hidden = !qs.length;
  list.innerHTML = qs.length
    ? qs.map(questionHtml).join("")
    : `<li class="muted pp-empty">還沒有研究問題。寫下你想搞清楚的事，或按「AI 建議問題」。</li>`;

  list.querySelectorAll("li[data-id]").forEach((li) => {
    const q = qs.find((x) => x.id === li.dataset.id);
    li.querySelector(".pp-q-state").addEventListener("click", () =>
      updateQuestion(q, { status: QUESTION_STATES[q.status].next })
    );
    li.querySelectorAll("[data-toggle]").forEach((el) =>
      el.addEventListener("click", () => {
        if (openQuestions.has(q.id)) {
          flushNote(`q:${q.id}`);
          openQuestions.delete(q.id);
        } else openQuestions.add(q.id);
        renderQuestions();
      })
    );
    const box = li.querySelector(".md-box");
    if (box) mountQuestionNote(box, q);
    li.querySelector("[data-action=rename]")?.addEventListener("click", () => renameQuestion(q));
    li.querySelector("[data-action=delete]")?.addEventListener("click", () => deleteQuestion(q));
  });

  questionSorter?.destroy();
  questionSorter =
    window.Sortable && qs.length > 1
      ? Sortable.create(list, {
          handle: ".pp-q-handle",
          animation: 150,
          ghostClass: "pp-q-ghost",
          onEnd: saveQuestionOrder,
        })
      : null;
}

function questionHtml(q) {
  const open = openQuestions.has(q.id);
  const gap = questionGap(q);
  const covered = gap ? `${gap.covered.length} 篇有談到` : "";
  const sub = [
    `<span class="pp-q-status ${q.status}">${QUESTION_STATES[q.status].label}</span>`,
    covered ? `<span>${covered}</span>` : "",
    q.note.trim() ? `<span class="pp-has-note"><svg class="icon-xs"><use href="#ic-note"/></svg>有筆記</span>` : "",
  ].join("");
  return `<li class="pp-q" data-id="${q.id}">
    <div class="pp-q-row">
      <button type="button" class="pp-q-handle" aria-label="拖曳調整順序"><svg><use href="#ic-grip"/></svg></button>
      <button type="button" class="pp-q-state" aria-label="狀態：${QUESTION_STATES[q.status].label}，點一下切換">${questionStateSvg(q.status)}</button>
      <div class="pp-q-body">
        <div class="pp-q-text" data-toggle>${escapeHtml(q.text)}</div>
        <div class="pp-q-sub">${sub}</div>
      </div>
      <button type="button" class="pp-q-open" data-toggle aria-expanded="${open}" aria-label="展開筆記與缺口">
        <svg><use href="#ic-chevron"/></svg>
      </button>
    </div>
    ${open ? questionDetailHtml(q, gap) : ""}
  </li>`;
}

function questionDetailHtml(q, gap) {
  const gapHtml = gap
    ? `<h4>有談到的篇目</h4>
       <ul>${gap.covered.length ? gap.covered.map((g) => `<li>${escapeHtml(episodeTitle(g))}</li>`).join("") : "<li>還沒有</li>"}</ul>
       <h4>還缺哪類來源</h4>
       <ul>${gap.missing.map((m) => `<li>${escapeHtml(m)}</li>`).join("")}</ul>`
    : `<p class="muted">${project.insights?.generated_at ? "這題在上次產生之後才新增，下次產生 AI 整理時才會分析。" : "產生 AI 整理後會列出哪些篇有談到、還缺哪類來源。"}</p>`;
  return `<div class="pp-q-detail">
    <h4>我的筆記</h4>
    <div class="md-box small"></div>
    <div class="pp-gap"><span class="pp-ai-tag"><svg class="icon-sm"><use href="#ic-spark"/></svg>AI 整理的缺口</span>${gapHtml}</div>
    <div class="pp-q-actions">
      <button type="button" class="link-btn" data-action="rename">修改問題</button>
      <button type="button" class="link-btn danger" data-action="delete">刪除</button>
    </div>
  </div>`;
}

async function updateQuestion(q, body) {
  try {
    const updated = await api(`/api/projects/${project.id}/questions/${q.id}`, { method: "PUT", body });
    Object.assign(q, updated);
  } catch (err) {
    alert(err.message);
    if (err.message.includes("重新載入")) await loadProject(project.id);
    return null;
  }
  renderQuestions();
  return q;
}

async function renameQuestion(q) {
  const text = (prompt("修改研究問題", q.text) || "").trim();
  if (text && text !== q.text) await updateQuestion(q, { text });
}

async function deleteQuestion(q) {
  if (!confirm(`刪除研究問題「${q.text}」？\n這題的筆記也會一併刪除。`)) return;
  try {
    await api(`/api/projects/${project.id}/questions/${q.id}`, { method: "DELETE" });
  } catch (err) {
    alert(err.message);
    return;
  }
  noteEditors.delete(`q:${q.id}`);
  project.questions = project.questions.filter((x) => x.id !== q.id);
  renderQuestions();
}

async function saveQuestionOrder() {
  const ids = [...$("pp-questions").querySelectorAll("li[data-id]")].map((li) => li.dataset.id);
  const byId = new Map(project.questions.map((q) => [q.id, q]));
  project.questions = ids.map((id) => byId.get(id));
  try {
    await api(`/api/projects/${project.id}/questions/order`, { method: "PUT", body: { ids } });
  } catch (err) {
    alert(err.message);
    await loadProject(project.id);
  }
}

$("pp-add-question").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("pp-new-question").value.trim();
  if (!text) return;
  try {
    const created = await api(`/api/projects/${project.id}/questions`, {
      method: "POST",
      body: { texts: [text] },
    });
    project.questions.push(...created);
    $("pp-new-question").value = "";
  } catch (err) {
    alert(err.message);
    return;
  }
  renderQuestions();
});

// ── AI 建議問題 ────────────────────────────────────────

$("pp-suggest").addEventListener("click", () => startAi("suggestions"));

function renderSuggestions() {
  const box = $("pp-suggestions");
  const running = project.ai?.suggestions?.running;
  const error = project.ai?.suggestions?.error;
  const items = project.insights?.suggestions || [];
  $("pp-suggest").disabled = running;
  $("pp-suggest").innerHTML = running ? '<span class="spinner"></span> 產生中…' : "AI 建議問題";
  box.hidden = !items.length && !error;
  if (box.hidden) return;
  box.innerHTML = `
    ${error ? `<p class="error">${escapeHtml(error)}</p>` : ""}
    ${
      items.length
        ? `<div class="pp-sugg-head"><span class="pp-ai-tag"><svg class="icon-sm"><use href="#ic-spark"/></svg>AI 建議</span>
            <span class="muted">勾選要加入研究問題清單的</span></div>
          ${items
            .map(
              (s, i) => `<label class="pp-sugg-item"><input type="checkbox" data-i="${i}">
                <span>${escapeHtml(s.text)}${s.why ? `<span class="muted">${escapeHtml(s.why)}</span>` : ""}</span></label>`
            )
            .join("")}
          <div class="pp-sugg-foot">
            <button type="button" class="primary" id="pp-sugg-add" disabled>加入所選</button>
            <button type="button" id="pp-sugg-more" ${running ? "disabled" : ""}>換一批</button>
            <button type="button" class="link-btn" id="pp-sugg-dismiss">都不要</button>
          </div>`
        : ""
    }`;
  if (!items.length) return;

  const checked = () => [...box.querySelectorAll("input[data-i]:checked")].map((c) => items[+c.dataset.i]);
  box.querySelectorAll("input[data-i]").forEach((c) =>
    c.addEventListener("change", () => {
      const n = checked().length;
      $("pp-sugg-add").disabled = !n;
      $("pp-sugg-add").textContent = n ? `加入所選（${n}）` : "加入所選";
    })
  );
  $("pp-sugg-add").addEventListener("click", async () => {
    try {
      const created = await api(`/api/projects/${project.id}/questions`, {
        method: "POST",
        body: { texts: checked().map((s) => s.text), from_suggestions: true },
      });
      project.questions.push(...created);
      project.insights.suggestions = [];
    } catch (err) {
      alert(err.message);
      return;
    }
    renderQuestions();
    renderSuggestions();
  });
  $("pp-sugg-more").addEventListener("click", () => startAi("suggestions"));
  $("pp-sugg-dismiss").addEventListener("click", async () => {
    try {
      await api(`/api/projects/${project.id}/suggestions`, { method: "DELETE" });
      project.insights.suggestions = [];
    } catch (err) {
      alert(err.message);
      return;
    }
    renderSuggestions();
  });
}

// ── 分頁 ──────────────────────────────────────────────

document.querySelectorAll(".pp-tabs [data-tab]").forEach((tab) =>
  tab.addEventListener("click", () => {
    if (projectTab === "notes") flushNote("project");
    projectTab = tab.dataset.tab;
    renderTabs();
  })
);

function renderTabs() {
  document.querySelectorAll(".pp-tabs [data-tab]").forEach((t) =>
    t.setAttribute("aria-selected", t.dataset.tab === projectTab)
  );
  for (const name of ["items", "claims", "mindmap", "notes"]) {
    $(`pp-tab-${name}`).hidden = name !== projectTab;
  }
  if (projectTab === "items") renderItems();
  else if (projectTab === "claims") renderClaims();
  else if (projectTab === "mindmap") renderProjectMindmap();
  else renderProjectNote();
}

// ── 內容：時間軸、類型、可能相關、專案內搜尋 ─────────────

document.querySelectorAll("#pp-tab-items [data-sort]").forEach((btn) =>
  btn.addEventListener("click", () => {
    projectSort = btn.dataset.sort;
    document
      .querySelectorAll("#pp-tab-items [data-sort]")
      .forEach((b) => b.setAttribute("aria-pressed", b === btn));
    renderItems();
  })
);

let searchTimer = null;
$("pp-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 300);
});

async function runSearch() {
  const q = $("pp-search").value.trim();
  if (!q) {
    projectHits = null;
    renderItems();
    return;
  }
  try {
    const hits = await api(`/api/projects/${project.id}/search?q=${encodeURIComponent(q)}`);
    if ($("pp-search").value.trim() !== q) return; // 已改了關鍵字
    projectHits = new Map(hits.map((h) => [h.guid, h]));
  } catch (err) {
    alert(err.message);
    return;
  }
  renderItems();
}

/** 可能相關：標籤與專案內容重疊、還沒歸入的單集，依共同標籤數排序（不呼叫模型）。 */
function candidateEpisodes() {
  const mine = projectEpisodes();
  const tags = new Set(mine.flatMap((e) => e.hashtags || []));
  if (!tags.size) return [];
  return allEpisodes
    .filter((e) => !(e.projects || []).includes(project.id) && e.ready)
    .map((e) => ({ ...e, shared: (e.hashtags || []).filter((t) => tags.has(t)) }))
    .filter((e) => e.shared.length)
    .sort((a, b) => b.shared.length - a.shared.length || (b.published_at || "").localeCompare(a.published_at || ""))
    .slice(0, 10);
}

function renderKindChips(eps, candidates) {
  const count = (k) => eps.filter((e) => !k || episodeKind(e) === k).length;
  const kinds = Object.keys(EP_KINDS).filter((k) => count(k));
  if (projectKind && projectKind !== "candidates" && !kinds.includes(projectKind)) projectKind = "";
  if (projectKind === "candidates" && !candidates.length) projectKind = "";
  $("pp-kinds").innerHTML =
    [["", "全部"], ...kinds.map((k) => [k, EP_KINDS[k].label])]
      .map(([k, label]) => `<button type="button" data-kind="${k}" aria-pressed="${projectKind === k}">${label} ${count(k)}</button>`)
      .join("") +
    (candidates.length
      ? `<button type="button" class="pp-chip-cand" data-kind="candidates" aria-pressed="${projectKind === "candidates"}">
          <svg class="icon-sm"><use href="#ic-link"/></svg>可能相關 ${candidates.length}</button>`
      : "");
  $("pp-kinds").querySelectorAll("[data-kind]").forEach((btn) =>
    btn.addEventListener("click", () => {
      projectKind = btn.dataset.kind;
      renderItems();
    })
  );
}

function renderItems() {
  if (!libraryLoaded) {
    $("pp-kinds").innerHTML = "";
    $("pp-items").innerHTML = `<li class="muted pp-empty">載入篇目中…</li>`;
    return;
  }
  const eps = projectEpisodes();
  const candidates = candidateEpisodes();
  renderKindChips(eps, candidates);
  const list = $("pp-items");

  if (projectKind === "candidates") {
    list.innerHTML =
      `<li class="pp-cand-note muted">還沒歸入這個專案，但標籤與專案內容相近；依共同標籤數排序。</li>` +
      candidates.map(candidateRow).join("");
    list.querySelectorAll("[data-adopt]").forEach((btn) =>
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        adoptCandidate(btn.dataset.adopt);
      })
    );
    bindItemClicks(list);
    return;
  }

  let shown = eps.filter((e) => !projectKind || episodeKind(e) === projectKind);
  if (projectHits) shown = shown.filter((e) => projectHits.has(e.guid));
  if (projectHits) {
    shown.sort((a, b) => projectHits.get(b.guid).count - projectHits.get(a.guid).count);
  } else if (projectSort === "added") {
    shown.sort((a, b) => b.added_n - a.added_n);
  } else {
    shown.sort((a, b) => (b.published_at || "").localeCompare(a.published_at || ""));
  }

  if (!shown.length) {
    list.innerHTML = `<li class="muted pp-empty">${
      projectHits ? `這個專案裡沒有提到「${escapeHtml($("pp-search").value.trim())}」的內容`
      : eps.length ? "這個類型沒有內容"
      : "還沒有內容。在單集頁的「研究專案」區塊把單集歸入這個專案。"
    }</li>`;
    return;
  }

  let html = "";
  let group = "";
  for (const e of shown) {
    if (!projectHits) {
      const date = projectSort === "added" ? e.added_at : e.published_at;
      const label = (date || "").slice(0, 4) || "日期不明";
      if (label !== group) {
        group = label;
        html += `<li class="group">${projectSort === "added" ? `${label} 歸入` : label}</li>`;
      }
    }
    html += episodeRow(e, false).replace("</li>", `${hitsHtml(e)}</li>`);
  }
  list.innerHTML = html;
  bindItemClicks(list);
}

function hitsHtml(e) {
  const hit = projectHits?.get(e.guid);
  if (!hit) return "";
  return `<div class="pp-hits">${hit.hits
    .map(
      (h) => `<div class="pp-hit">${h.start != null ? `<span class="pp-hit-time">${formatTime(h.start)}</span>` : ""}${highlight(h.snippet)}</div>`
    )
    .join("")}${hit.count > hit.hits.length ? `<div class="muted">共 ${hit.count} 處</div>` : ""}</div>`;
}

function highlight(text) {
  const q = $("pp-search").value.trim();
  const safe = escapeHtml(text);
  if (!q) return safe;
  const pattern = new RegExp(escapeHtml(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
  return safe.replace(pattern, (m) => `<mark>${m}</mark>`);
}

function candidateRow(e) {
  const { icon, label } = EP_KINDS[episodeKind(e)];
  const meta = [escapeHtml(e.podcast_name), (e.published_at || "").slice(0, 10)].filter(Boolean).join(" · ");
  return `<li data-guid="${e.guid}" class="ep-card pp-cand">
    ${listCoverHtml(e)}
    <div class="ep-body">
      <span class="ep-name">${escapeHtml(e.title)}</span>
      <span class="ep-meta"><svg class="kind-icon" role="img" aria-label="${label}"><use href="#${icon}"/></svg>${meta}</span>
      <span class="pp-shared">${e.shared.map((t) => `<span>#${escapeHtml(t)}</span>`).join("")}</span>
    </div>
    <button type="button" class="pp-adopt" data-adopt="${e.guid}">歸入</button>
  </li>`;
}

function bindItemClicks(list) {
  list.querySelectorAll("li[data-guid]").forEach((li) =>
    li.addEventListener("click", () => {
      location.href = `/#${li.dataset.guid}`;
    })
  );
}

async function adoptCandidate(guid) {
  const ep = allEpisodes.find((e) => e.guid === guid);
  try {
    await api(`/api/episodes/${guid}/projects`, {
      method: "PUT",
      body: { project_ids: [...(ep?.projects || []), project.id] },
    });
    await Promise.all([loadLibrary(), loadProjects()]);
    project = { ...project, ...(await api(`/api/projects/${project.id}`)) };
  } catch (err) {
    alert(err.message);
    return;
  }
  renderProject();
}

// ── 對照表 ────────────────────────────────────────────

function renderClaims() {
  const box = $("pp-tab-claims");
  const claims = project.insights?.claims;
  if (!claims?.rows?.length) {
    box.innerHTML = `<p class="muted pp-empty">尚未產生。按左上角專案卡片的「產生 AI 整理」，會依各篇摘要整理出主張對照表。</p>`;
    return;
  }
  const sources = claims.sources;
  const groups = [
    ["說法不同", (r) => Object.values(r.marks).includes("differ")],
    ["多篇提到", (r) => Object.values(r.marks).filter((m) => m === "agree").length >= 2],
    ["只有一篇提到", () => true],
  ];
  const used = new Set();
  const body = groups
    .map(([label, test]) => {
      const rows = claims.rows.filter((r) => !used.has(r) && test(r));
      rows.forEach((r) => used.add(r));
      if (!rows.length) return "";
      return `<tr class="pp-claim-group"><td colspan="${sources.length + 1}">${label}</td></tr>` +
        rows
          .map(
            (r) => `<tr><td class="pp-claim">${escapeHtml(r.claim)}${r.note ? `<span class="muted">${escapeHtml(r.note)}</span>` : ""}</td>
              ${sources.map((s) => `<td>${markHtml(r.marks[s.guid])}</td>`).join("")}</tr>`
          )
          .join("");
    })
    .join("");
  box.innerHTML = `
    <p class="pp-gen muted">${generatedLine()}只整理各篇「說了什麼」，不判斷誰對。</p>
    <div class="pp-legend muted">
      <span><i class="pp-mark agree"></i>這篇有此主張</span>
      <span><i class="pp-mark differ"></i>這篇說法不同</span>
      <span><i class="pp-mark none"></i>沒提到</span>
    </div>
    <div class="pp-table-wrap"><table class="pp-claims">
      <thead><tr><th>主張</th>${sources
        .map(
          (s) => `<th><a href="/#${encodeURIComponent(s.guid)}" title="${escapeHtml(s.title)}">${escapeHtml(shortTitle(s.title))}</a>
            <span class="muted">${(s.published || "").slice(0, 4)}</span></th>`
        )
        .join("")}</tr></thead>
      <tbody>${body}</tbody>
    </table></div>`;
}

function markHtml(mark) {
  const label = { agree: "有此主張", differ: "說法不同" }[mark] || "沒提到";
  return `<i class="pp-mark ${mark || "none"}" role="img" aria-label="${label}"></i>`;
}

function shortTitle(title, limit = 14) {
  return title.length <= limit ? title : `${title.slice(0, limit)}…`;
}

function generatedLine() {
  const ins = project.insights;
  return ins?.generated_at ? `${ins.generated_at.slice(0, 10)} 依 ${ins.source_guids.length} 篇摘要產生。` : "";
}

// ── 心智圖 ────────────────────────────────────────────

function renderProjectMindmap() {
  const code = project.insights?.mindmap;
  $("pp-mindmap-wrap").hidden = !code;
  $("pp-mindmap-info").textContent = code
    ? generatedLine()
    : "尚未產生。按左上角專案卡片的「產生 AI 整理」，會把各篇內容整理成一張專案心智圖。";
  if (!code) return;
  try {
    window.renderMarkmap(code, $("pp-mindmap"), { interactive: false });
  } catch {
    $("pp-mindmap").innerHTML = `<p class="error">心智圖語法錯誤，請重新產生</p>`;
  }
}

$("pp-mindmap-full").addEventListener("click", (e) => {
  e.stopPropagation();
  openMindmapFull(project?.insights?.mindmap);
});
$("pp-mindmap").addEventListener("click", (e) => {
  if (!e.target.closest("circle")) openMindmapFull(project?.insights?.mindmap);
});

// ── 筆記（Markdown）────────────────────────────────────
// 檢視：marked 轉換、DOMPurify 過濾；編輯：OverType（標記即時上色，不用工具列）。
// 開始編輯時記下原內容（base），存檔時後端比對，另一台裝置改過就回 409，不覆蓋。

function markdownHtml(text) {
  if (!window.marked) return `<pre>${escapeHtml(text)}</pre>`;
  const html = marked.parse(text, { gfm: true, breaks: true });
  return window.DOMPurify ? DOMPurify.sanitize(html) : escapeHtml(text);
}

function overtypeTheme() {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  return {
    name: "podscript",
    colors: {
      bgPrimary: v("--card"), bgSecondary: v("--card"), text: v("--ink"),
      h1: v("--accent"), h2: v("--accent"), h3: v("--accent"),
      strong: v("--ink"), em: v("--ink"), link: v("--accent"),
      code: v("--ink"), codeBg: v("--accent-soft"), blockquote: v("--muted"), hr: v("--line"),
      syntaxMarker: v("--muted"), listMarker: v("--muted"), cursor: v("--accent"), selection: v("--accent-soft"),
    },
  };
}

/**
 * 掛一個「檢視／編輯」筆記區塊。
 * @param {object} o key、value()、save(note, base)（回傳 Promise）、empty（空白時的提示）、minHeight
 */
function mountNote(box, o) {
  const editing = noteEditors.get(o.key);
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
    if (window.OverType) {
      const host = document.createElement("div");
      host.className = "md-editor";
      body.appendChild(host);
      new OverType(host, {
        value: editing.value,
        toolbar: false,
        autoResize: true,
        minHeight: o.minHeight || "320px",
        placeholder: "用 Markdown 寫…",
        fontFamily: 'ui-monospace, Menlo, "PingFang TC", "Noto Sans TC", monospace',
        fontSize: "14px",
        lineHeight: 1.75,
        padding: "12px",
        theme: overtypeTheme(),
        onChange: (value) => {
          editing.value = value;
        },
      });
      body.querySelector("textarea")?.focus();
    } else {
      // 編輯器沒載入（離線等）時退回一般文字框
      const area = document.createElement("textarea");
      area.className = "md-plain";
      area.value = editing.value;
      area.addEventListener("input", () => (editing.value = area.value));
      body.appendChild(area);
      area.focus();
    }
  } else {
    const text = o.value();
    body.innerHTML = text.trim()
      ? `<div class="md-view">${markdownHtml(text)}</div>`
      : `<p class="muted">${o.empty}</p>`;
  }

  box.querySelector('[data-mode="edit"]').addEventListener("click", () => {
    if (noteEditors.has(o.key)) return;
    const value = o.value();
    noteEditors.set(o.key, { base: value, value, save: o.save });
    mountNote(box, o);
  });
  box.querySelector('[data-mode="view"]').addEventListener("click", async () => {
    // 已在別處存檔（例如換分頁）但畫面還停在編輯：直接切回檢視
    if (!noteEditors.has(o.key)) return mountNote(box, o);
    const state = box.querySelector(".md-state");
    state.textContent = "儲存中…";
    if (await flushNote(o.key)) mountNote(box, o);
    else state.textContent = "";
  });
}

/** 把編輯中的筆記存回資料庫；沒改就直接結束編輯。成功或沒改回傳 true。 */
async function flushNote(key) {
  const editing = noteEditors.get(key);
  if (!editing) return true;
  if (editing.value === editing.base) {
    noteEditors.delete(key);
    return true;
  }
  try {
    await editing.save(editing.value, editing.base);
    noteEditors.delete(key);
    return true;
  } catch (err) {
    alert(err.message);
    return false;
  }
}

function flushNotes() {
  for (const key of [...noteEditors.keys()]) flushNote(key);
}

window.addEventListener("beforeunload", (e) => {
  if ([...noteEditors.values()].some((n) => n.value !== n.base)) e.preventDefault();
});

function renderProjectNote() {
  const id = project.id;
  mountNote($("pp-note"), {
    key: "project",
    value: () => project.note,
    empty: "還沒有專案筆記。按「編輯」用 Markdown 寫下目前的理解、還不懂的地方與下一步。",
    save: async (note, base) => {
      const res = await api(`/api/projects/${id}/note`, { method: "PUT", body: { note, base } });
      if (project?.id === id) {
        project.note = note;
        project.note_updated_at = res.note_updated_at;
        renderNoteInfo();
      }
    },
  });
  renderNoteInfo();
}

function renderNoteInfo() {
  $("pp-note-info").textContent = project.note_updated_at
    ? `上次修改 ${new Date(project.note_updated_at).toLocaleString("zh-TW", { hour12: false })}；切回「檢視」時儲存`
    : "切回「檢視」時儲存";
}

function mountQuestionNote(box, q) {
  const id = project.id;
  mountNote(box, {
    key: `q:${q.id}`,
    value: () => q.note,
    empty: "還沒有筆記。按「編輯」寫下對這個問題的想法、待查事項。",
    minHeight: "120px",
    save: async (note, base) => {
      const updated = await api(`/api/projects/${id}/questions/${q.id}`, {
        method: "PUT",
        body: { note, base_note: base },
      });
      Object.assign(q, updated);
      // 「有筆記」標記在問題列上，存完更新那一行
      const sub = box.closest(".pp-q")?.querySelector(".pp-q-sub");
      if (sub) sub.outerHTML = questionHtml(q).match(/<div class="pp-q-sub">.*?<\/div>/s)[0];
    },
  });
}

// ── 啟動 ──────────────────────────────────────────────

window.addEventListener("hashchange", openFromHash);

// 從瀏覽器「上一頁」回來時，頁面是從記憶體還原的舊畫面，重新抓一次才會反映別頁的修改
window.addEventListener("pageshow", (e) => {
  if (!e.persisted || !project) return;
  loadProjects().catch(console.error);
  loadLibrary().catch(console.error);
  loadProject(project.id).catch(console.error);
});

const projectsReady = loadProjects().catch((err) => {
  $("pp-name").textContent = "無法載入";
  $("pp-desc").textContent = err.message;
});
loadLibrary().catch((err) => {
  $("pp-items").innerHTML = `<li class="error">無法載入篇目：${escapeHtml(err.message)}</li>`;
});
// 網址有專案 id 就直接載入，不等專案清單；沒有的話等清單出來再開第一個
if (location.hash.slice(1)) openFromHash();
else projectsReady.then(openFromHash);
