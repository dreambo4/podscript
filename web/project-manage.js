// 專案管理頁（/projects）：左側單集清單，右側專案資料夾一格一格排列，把單集拖進資料夾歸類；
// 點一篇單集，它所在的專案資料夾會亮起來，可在亮起的資料夾上移出。
// 歸類沿用 PUT /api/episodes/{guid}/projects（整組取代），直接寫資料庫，未上傳的單集也能歸類。
// 共用工具見 common.js：$、api、escapeHtml、episodeRow。

let allEpisodes = [];
let allProjects = [];
let selectedGuid = "";
let listFilter = "all"; // all、unfiled
const savingProjects = new Set();
let toastTimer = null;

async function loadAll() {
  [allEpisodes, allProjects] = await Promise.all([api("/api/episodes"), api("/api/projects")]);
  // 處理中或失敗的單集還沒有內容，不列入
  allEpisodes = allEpisodes.filter((e) => e.ready && !e.processing);
}

function render() {
  renderFolders();
  renderEpisodes();
  renderSelectedHint();
}

function projectName(id) {
  return allProjects.find((p) => p.id === id)?.name || "";
}

// ── 專案資料夾 ────────────────────────────────────────

function renderFolders() {
  const selected = allEpisodes.find((e) => e.guid === selectedGuid);
  const mine = new Set(selected?.projects || []);
  $("pm-folders").innerHTML =
    allProjects
      .map((p) => {
        const on = mine.has(p.id);
        const cls = ["pm-folder", on && "on", selected && !on && "dim", savingProjects.has(p.id) && "saving"]
          .filter(Boolean)
          .join(" ");
        // 整格可點，進入專案頁；同時是拖放目標
        return `<section class="${cls}" data-id="${p.id}" role="link" tabindex="0" aria-label="開啟「${escapeHtml(p.name)}」專案頁">
          <div class="pm-folder-top">
            <svg class="pm-folder-icon"><use href="#ic-folder"/></svg>
            ${on ? `<span class="pm-in"><svg><use href="#ic-check"/></svg>在這裡</span>` : ""}
          </div>
          <h3 class="pm-folder-name">${escapeHtml(p.name)}</h3>
          <p class="pm-folder-meta">${p.item_count} 篇${p.last_added_at ? ` · 最近加入 ${relativeDay(p.last_added_at)}` : ""}</p>
          ${on ? `<button type="button" class="link-btn danger pm-remove" data-action="remove">移出這篇</button>` : ""}
        </section>`;
      })
      .join("") +
    `<section class="pm-folder pm-new">
      <form id="pm-new-form">
        <input id="pm-new-name" type="text" maxlength="100" placeholder="新專案名稱">
        <button type="submit" class="primary">＋ 新增專案</button>
      </form>
    </section>`;

  $("pm-folders").querySelectorAll(".pm-folder[data-id]").forEach((tile) => {
    const id = tile.dataset.id;
    const open = () => {
      // 先把名稱與說明帶到專案頁，資料還沒載入前就能顯示（類似 Android 的 Intent extras）
      const p = allProjects.find((x) => x.id === id);
      try {
        sessionStorage.setItem("podscript_project_preview", JSON.stringify({ id, name: p.name, description: p.description }));
      } catch {
        // 存不了只是少了預覽，專案頁仍會照常載入
      }
      location.href = `/project#${id}`;
    };
    tile.addEventListener("click", (e) => {
      if (!e.target.closest("[data-action]")) open();
    });
    tile.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target === tile) open();
    });
    tile.querySelector("[data-action=remove]")?.addEventListener("click", () => {
      const ep = allEpisodes.find((x) => x.guid === selectedGuid);
      if (ep && confirm(`把「${shortTitle(ep.title, 30)}」移出「${projectName(id)}」？`)) removeFrom(selectedGuid, id);
    });
    bindDropTarget(tile, id);
  });
  $("pm-new-form").addEventListener("submit", createProject);
}

async function createProject(e) {
  e.preventDefault();
  const name = $("pm-new-name").value.trim();
  if (!name) {
    $("pm-new-name").focus();
    return;
  }
  try {
    await api("/api/projects", { method: "POST", body: { name } });
    allProjects = await api("/api/projects");
  } catch (err) {
    alert(err.message);
    return;
  }
  renderFolders();
  showToast(`已新增專案「${name}」`);
}

// ── 拖曳歸類 ──────────────────────────────────────────

function bindDropTarget(tile, projectId) {
  tile.addEventListener("dragover", (e) => {
    if (!e.dataTransfer.types.includes("text/plain")) return;
    e.preventDefault(); // 允許放下
    e.dataTransfer.dropEffect = "copy";
    tile.classList.add("drop-hover");
  });
  tile.addEventListener("dragleave", (e) => {
    if (!tile.contains(e.relatedTarget)) tile.classList.remove("drop-hover");
  });
  tile.addEventListener("drop", (e) => {
    e.preventDefault();
    tile.classList.remove("drop-hover");
    const guid = e.dataTransfer.getData("text/plain");
    if (guid) addTo(guid, projectId);
  });
}

async function addTo(guid, projectId) {
  const ep = allEpisodes.find((e) => e.guid === guid);
  if (!ep) return;
  const before = [...(ep.projects || [])];
  if (before.includes(projectId)) {
    showToast(`已經在「${projectName(projectId)}」裡`);
    return;
  }
  if (await saveProjects(ep, [...before, projectId], projectId)) {
    showToast(`已把「${shortTitle(ep.title)}」歸入「${projectName(projectId)}」`, () => saveProjects(ep, before, projectId));
  }
}

async function removeFrom(guid, projectId) {
  const ep = allEpisodes.find((e) => e.guid === guid);
  if (!ep) return;
  const before = [...(ep.projects || [])];
  if (await saveProjects(ep, before.filter((id) => id !== projectId), projectId)) {
    showToast(`已把「${shortTitle(ep.title)}」移出「${projectName(projectId)}」`, () => saveProjects(ep, before, projectId));
  }
}

/** 寫入這集的所屬專案；成功後更新篇數並重畫。成功回傳 true。 */
async function saveProjects(ep, ids, changedProjectId) {
  savingProjects.add(changedProjectId);
  renderFolders();
  try {
    const res = await api(`/api/episodes/${ep.guid}/projects`, { method: "PUT", body: { project_ids: ids } });
    ep.projects = res.projects;
    allProjects = await api("/api/projects"); // 篇數以資料庫為準
    return true;
  } catch (err) {
    alert(err.message);
    return false;
  } finally {
    savingProjects.delete(changedProjectId);
    render();
  }
}

// ── 左側單集清單 ──────────────────────────────────────

document.querySelectorAll("[data-filter]").forEach((btn) =>
  btn.addEventListener("click", () => {
    listFilter = btn.dataset.filter;
    renderEpisodes();
  })
);
$("pm-search").addEventListener("input", renderEpisodes);

function renderEpisodes() {
  const keyword = $("pm-search").value.trim().toLowerCase();
  document.querySelectorAll("[data-filter]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.filter === listFilter));

  const shown = allEpisodes.filter(
    (e) =>
      (listFilter === "all" || !(e.projects || []).length) &&
      (!keyword || [e.title, e.podcast_name, ...(e.hashtags || [])].join(" ").toLowerCase().includes(keyword))
  );

  const list = $("pm-episodes");
  list.innerHTML = shown.length
    ? shown
        .map((e) => {
          const n = (e.projects || []).length;
          const badge = n
            ? `<span class="pm-badge" title="${escapeHtml(e.projects.map(projectName).join("、"))}">${n} 個專案</span>`
            : `<span class="pm-badge none">未歸類</span>`;
          return episodeRow(e, e.guid === selectedGuid).replace("<li ", '<li draggable="true" ').replace("</li>", `${badge}</li>`);
        })
        .join("")
    : `<li class="muted">${keyword ? "沒有符合的單集" : listFilter === "unfiled" ? "全部都歸類好了" : "沒有單集"}</li>`;

  list.querySelectorAll("li[data-guid]").forEach((li) => {
    li.addEventListener("click", () => {
      selectedGuid = selectedGuid === li.dataset.guid ? "" : li.dataset.guid;
      render();
    });
    li.addEventListener("dblclick", () => {
      location.href = `/#${li.dataset.guid}`;
    });
    li.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", li.dataset.guid);
      e.dataTransfer.effectAllowed = "copy";
      document.body.classList.add("pm-dragging");
      li.classList.add("dragging");
    });
    li.addEventListener("dragend", () => {
      document.body.classList.remove("pm-dragging");
      li.classList.remove("dragging");
      document.querySelectorAll(".drop-hover").forEach((t) => t.classList.remove("drop-hover"));
    });
  });
}

function renderSelectedHint() {
  const ep = allEpisodes.find((e) => e.guid === selectedGuid);
  const hint = $("pm-selected-hint");
  if (!ep) {
    hint.textContent = allProjects.length
      ? "點左側一篇單集，它所在的專案會亮起來；按住拖到專案格子即可歸類。點格子進入專案頁，點兩下單集開啟單集。"
      : "還沒有研究專案，先在下方新增一個。";
    return;
  }
  const n = (ep.projects || []).length;
  hint.innerHTML = `<strong>${escapeHtml(shortTitle(ep.title, 40))}</strong>　${
    n ? `在 ${n} 個專案裡` : "還沒歸入任何專案，拖到資料夾即可歸類"
  }`;
}

/** 「今天」「3 天前」這類相對日期。 */
function relativeDay(iso) {
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  if (days <= 0) return "今天";
  if (days === 1) return "昨天";
  if (days < 30) return `${days} 天前`;
  return iso.slice(0, 10);
}

function shortTitle(title, limit = 20) {
  return title.length <= limit ? title : `${title.slice(0, limit)}…`;
}

// ── 提示（可復原）────────────────────────────────────

function showToast(message, undo) {
  const toast = $("pm-toast");
  toast.innerHTML = `<span>${escapeHtml(message)}</span>${undo ? '<button type="button" class="link-btn">復原</button>' : ""}`;
  toast.hidden = false;
  toast.querySelector("button")?.addEventListener("click", async () => {
    toast.hidden = true;
    if (await undo()) showToast("已復原");
  });
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (toast.hidden = true), 6000);
}

// ── 啟動 ──────────────────────────────────────────────

// 從瀏覽器「上一頁」回來時，頁面是從記憶體還原的舊畫面（例如剛在專案頁改了名稱），重新抓一次
window.addEventListener("pageshow", (e) => {
  if (e.persisted) loadAll().then(render).catch(console.error);
});

loadAll()
  .then(render)
  .catch((err) => {
    $("pm-folders").innerHTML = `<p class="error">無法載入：${escapeHtml(err.message)}</p>`;
  });
