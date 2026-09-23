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

// rootColor 可覆寫（手機端深色模式需要較亮的 root 色）。
export function renderMindmap(code, el, { rootColor = ROOT_COLOR } = {}) {
  if (!code) {
    el.innerHTML = "";
    return;
  }
  // markmap 需要一個 <svg>；每次重建避免殘留舊圖。
  el.innerHTML = '<svg style="width:100%;height:100%"></svg>';
  const svg = el.querySelector("svg");
  const transformer = new Transformer();
  const { root } = transformer.transform(mermaidToMarkdown(code));
  Markmap.create(svg, {
    spacingVertical: 6,
    paddingX: 12,
    color: makeColorFn(rootColor),
    lineWidth: () => 4,
    nodeMinHeight: 24,
  }, root);
}
