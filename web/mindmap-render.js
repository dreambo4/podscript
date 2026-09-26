// 心智圖渲染：把後端存的 mermaid mindmap 語法轉成 markdown 大綱，用 markmap 畫。
// 資料流不變（DB 仍存 mermaid_mindmap），只換前端渲染引擎，隨時可回退。
// 配色/線粗/字級沿用 mindmap-demo.html 定案的參數。
import { Transformer } from "https://cdn.jsdelivr.net/npm/markmap-lib@0.18/+esm";
import { Markmap } from "https://cdn.jsdelivr.net/npm/markmap-view@0.18/+esm";

// 各第一層分支的色相（藍 綠 桃 橘 紫 青 紅 黃綠），子孫沿用同色相、越深越淺。
const HUES = [217, 142, 330, 32, 265, 190, 0, 90];
const ROOT_COLOR = "#475569";

// mermaid mindmap 縮排語法 → markdown 大綱（markmap 的輸入格式）。
function mermaidToMarkdown(code) {
  const lines = code.split("\n").slice(1); // 去掉第一行 "mindmap"
  const rows = [];
  let baseIndent = null;
  for (const raw of lines) {
    if (!raw.trim()) continue;
    const indent = raw.match(/^\s*/)[0].length;
    if (baseIndent === null) baseIndent = indent;
    const level = Math.max(0, Math.round((indent - baseIndent) / 2));
    const text = raw.trim().replace(/^root\(\((.*)\)\)$/, "$1"); // 去掉 root(( )) 外框
    rows.push({ level, text });
  }
  return rows.map((r) => (r.level === 0 ? `# ${r.text}` : `${"  ".repeat(r.level - 1)}- ${r.text}`)).join("\n");
}

// markmap render 時用 node.state.path（形如 "0.2.1"，段數=深度、第二段=第一層分支序號）
// 呼叫 color；順著它的原生資料流上色，第 1 階決定色相、越深越淺。
function makeColorFn(rootColor) {
  return (node) => {
    const path = node?.state?.path || "0";
    const parts = path.split(".");
    if (parts.length <= 1) return rootColor; // root
    const hue = HUES[Number(parts[1]) % HUES.length];
    const depth = parts.length;
    const l = Math.min(45 + (depth - 2) * 13, 82); // 第1階 45%，每深一階 +13%
    const s = Math.max(72 - (depth - 2) * 8, 45);
    return `hsl(${hue}, ${s}%, ${l}%)`;
  };
}

// 平移縮放限制：心智圖外框至少佔畫面一半寬、一半高（內容比畫面小時則整張留在畫面內），
// 避免拖到一片空白找不回來。只留一小角不夠：外框角落常沒有節點。
function boundZoom(mm) {
  mm.zoom.scaleExtent([0.25, 4]).constrain((t, extent) => {
    const [[vx0, vy0], [vx1, vy1]] = extent;
    const { x1, y1, x2, y2 } = mm.state.rect;
    const keepX = Math.min((vx1 - vx0) / 2, (x2 - x1) * t.k);
    const keepY = Math.min((vy1 - vy0) / 2, (y2 - y1) * t.k);
    const clamp = (v, lo, hi) => (lo > hi ? (lo + hi) / 2 : Math.min(hi, Math.max(lo, v)));
    const tx = clamp(t.x, vx0 + keepX - x2 * t.k, vx1 - keepX - x1 * t.k);
    const ty = clamp(t.y, vy0 + keepY - y2 * t.k, vy1 - keepY - y1 * t.k);
    return tx === t.x && ty === t.y ? t : new t.constructor(t.k, tx, ty);
  });
  // markmap 內建的觸控板捲動平移走 zoom.transform，不經過 constrain；
  // 關掉它（pan: false）改用 translateBy，限制才會生效。
  // 非 Mac（scrollForPan=false）滾輪本來就是縮放，ctrl+滾輪（雙指縮放）也交給 d3 處理。
  mm.svg.on("wheel.bounded", (e) => {
    if (!mm.options.scrollForPan || e.ctrlKey) return;
    e.preventDefault();
    const k = mm.svg.node().__zoom?.k || 1;
    mm.svg.call(mm.zoom.translateBy, -e.deltaX / k, -e.deltaY / k);
  }, { passive: false });
}

// rootColor 可覆寫（手機端深色模式需要較亮的 root 色）。
// interactive=false 為唯讀縮圖：不攔拖曳/縮放手勢（手機上滑過去照常捲頁面），
// 展開收合節點後自動重新置中。回傳 Markmap 實例供呼叫端 fit()/destroy()。
export function renderMindmap(code, el, { rootColor = ROOT_COLOR, interactive = true } = {}) {
  if (!code) {
    el.innerHTML = "";
    return null;
  }
  // markmap 需要一個 <svg>；每次重建避免殘留舊圖。
  el.innerHTML = '<svg style="width:100%;height:100%"></svg>';
  const svg = el.querySelector("svg");
  const transformer = new Transformer();
  const { root } = transformer.transform(mermaidToMarkdown(code));
  const mm = Markmap.create(svg, {
    spacingVertical: 6,
    paddingX: 12,
    color: makeColorFn(rootColor),
    lineWidth: () => 4,
    nodeMinHeight: 24,
    pan: false,
    ...(interactive ? {} : { zoom: false, autoFit: true }),
  }, root);
  if (interactive) boundZoom(mm);
  // 容器隱藏時（已切到別集或別的分頁）寬高為 0，fit 會算出 NaN 縮放寫進 transform。
  // 延遲置中、autoFit、重置按鈕都會呼叫 fit，故在實例上統一略過；
  // 重新顯示時本機會重繪、手機縮圖由 autoFit 重新置中。
  const fit = mm.fit.bind(mm);
  mm.fit = (...args) => {
    const { width, height } = svg.getBoundingClientRect();
    return width && height ? fit(...args) : Promise.resolve();
  };
  // 首次 fit 時節點展開動畫與文字量測未必完成，大張圖會有邊緣被切掉；稍後再對齊一次。
  setTimeout(() => mm.fit(), 600);
  return mm;
}
