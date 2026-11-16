"use strict";

const $ = (id) => document.getElementById(id);
let lastResult = null;

function fmt(v) {
  if (v === null || v === undefined) return "NULL";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return String(v);
    return String(Math.round(v * 1e6) / 1e6);
  }
  if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
  return JSON.stringify(v);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function postJSON(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `请求失败（HTTP ${resp.status}）`);
  return data;
}

async function getJSON(path) {
  const resp = await fetch(path);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `请求失败（HTTP ${resp.status}）`);
  return data;
}

function showError(el, err) {
  el.textContent = err && err.message ? err.message : String(err);
  el.style.display = "block";
}

function clearError(el) {
  el.textContent = "";
  el.style.display = "none";
}

// ---------- 上传 ----------

function renderDatasetSummary(summary) {
  const parts = Object.entries(summary.tables).map(([name, t]) =>
    `<span class="pill">${name}：${t.rows} 行（${t.columns.map(esc).join(", ")}）</span>`
  );
  $("upload-status").innerHTML =
    `<div class="ok">已装载，共 ${summary.total_rows} 行（上限 ${summary.max_total_rows}）。</div>` +
    `<div class="pill-row">${parts.join("")}</div>`;
}

async function uploadData() {
  const status = $("upload-status");
  let payload;
  try {
    payload = JSON.parse($("json-input").value);
  } catch (e) {
    status.innerHTML = `<div class="error">JSON 解析失败：${esc(e.message)}</div>`;
    return;
  }
  try {
    const summary = await postJSON("/api/upload", payload);
    renderDatasetSummary(summary);
  } catch (e) {
    status.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

async function loadSampleData() {
  try {
    const sample = await getJSON("/api/sample");
    $("json-input").value = JSON.stringify(sample.data, null, 2);
    const row = $("sample-sql-row");
    row.querySelectorAll("button[data-sql]").forEach((b) => b.remove());
    sample.queries.forEach((q) => {
      const btn = document.createElement("button");
      btn.textContent = `示例：${q.name}`;
      btn.dataset.sql = q.sql;
      btn.addEventListener("click", () => { $("sql-input").value = q.sql; });
      row.insertBefore(btn, $("btn-run"));
    });
  } catch (e) {
    $("upload-status").innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

// ---------- 结果渲染 ----------

function recordHTML(table, rowid) {
  if (rowid === null || rowid === undefined) {
    return `<span class="null">NULL（外连接未匹配）</span>`;
  }
  const rec = lastResult.records[table][String(rowid)];
  if (!rec) return `<span class="null">（未取到行 #${rowid}）</span>`;
  const rows = Object.entries(rec)
    .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(fmt(v))}</td></tr>`)
    .join("");
  return `<table class="record"><tbody>${rows}</tbody></table>`;
}

function evidenceFor(groupKey, orderRowid) {
  const gk = JSON.stringify(groupKey);
  return lastResult.evidence.rows.filter(
    (r) => JSON.stringify(r.group_key) === gk && r.order_rowid === orderRowid
  );
}

function renderOrderEvidence(group, order) {
  const combos = evidenceFor(group.key, order.order_rowid);
  const hasP = lastResult.joined_tables.includes("payments");
  const hasR = lastResult.joined_tables.includes("refunds");
  const head = ["#", hasP ? "支付行" : null, hasR ? "退款行" : null]
    .filter(Boolean).map((h) => `<th>${h}</th>`).join("");
  const rows = combos.map((c, i) => {
    const cells = [`<td>${i + 1}</td>`];
    if (hasP) cells.push(`<td>${recordHTML("payments", c.payment_rowid)}</td>`);
    if (hasR) cells.push(`<td>${recordHTML("refunds", c.refund_rowid)}</td>`);
    return `<tr>${cells.join("")}</tr>`;
  }).join("");
  const note = lastResult.evidence.truncated
    ? `<p class="warn">行级证据已截断（上限 ${lastResult.evidence.limit} 条），此处可能不完整。</p>`
    : "";
  return `${note}<table class="evidence"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
}

function renderGroup(group, idx) {
  const div = document.createElement("div");
  div.className = "group";
  const keyStr = group.key
    .map((v, i) => `${lastResult.group_columns[i]} = ${fmt(v)}`)
    .join("，");
  const dupCount = group.orders.filter((o) => o.duplicated).length;
  const head = document.createElement("button");
  head.className = "group-head";
  head.innerHTML =
    `<span class="caret">▸</span> <b>分组 ${idx + 1}</b>：${esc(keyStr)}　` +
    `<span class="sum">汇总 ${esc(group.sum_label)} = ${fmt(group.total)}</span>　` +
    `<span class="check">贡献合计 ${fmt(group.contrib_sum)} ✓</span>　` +
    `<span class="muted">${group.orders.length} 个订单</span>` +
    (dupCount ? ` <span class="badge dup">${dupCount} 个订单重复计入</span>` : "");
  const body = document.createElement("div");
  body.className = "group-body";
  head.addEventListener("click", () => {
    body.classList.toggle("open");
    head.querySelector(".caret").textContent = body.classList.contains("open") ? "▾" : "▸";
  });

  const orderRows = group.orders.map((o, oi) => {
    const orderRec = recordHTML("orders", o.order_rowid);
    return `
      <tr class="order-row ${o.duplicated ? "dup-row" : ""}" data-gi="${idx}" data-oi="${oi}">
        <td>#${o.order_rowid}</td>
        <td>${esc(fmt(o.order_id))}</td>
        <td class="num">${fmt(o.amount)}</td>
        <td class="num">${o.count}</td>
        <td class="num">${fmt(o.contribution)}</td>
        <td>${o.duplicated ? `<span class="badge dup">重复计入 ×${o.count}</span>` : `<span class="badge ok">正常</span>`}</td>
      </tr>
      <tr class="order-detail" data-gi="${idx}" data-oi="${oi}" style="display:none">
        <td colspan="6">
          <div class="detail-grid">
            <div><h4>订单原行</h4>${orderRec}</div>
            <div><h4>参与汇总的关联行组合（${o.count} 组）</h4>${renderOrderEvidence(group, o)}</div>
          </div>
        </td>
      </tr>`;
  }).join("");

  body.innerHTML = `
    <table class="orders">
      <thead><tr>
        <th>订单行号</th><th>订单号</th><th>金额</th>
        <th>计入次数</th><th>贡献金额</th><th>判定</th>
      </tr></thead>
      <tbody>${orderRows}</tbody>
    </table>`;
  div.appendChild(head);
  div.appendChild(body);
  return div;
}

function renderResult(result) {
  lastResult = result;
  const el = $("result");
  el.innerHTML = "";
  const meta = document.createElement("div");
  meta.className = "result-meta";
  meta.innerHTML =
    `<div>执行 SQL：<code>${esc(result.sql)}</code></div>` +
    `<div>共 <b>${result.groups.length}</b> 个分组；关联维表：` +
    `${result.joined_tables.length ? result.joined_tables.join("、") : "（无）"}` +
    (result.evidence.truncated
      ? `　<span class="warn">行级证据过多，仅返回前 ${result.evidence.limit} 条（汇总与贡献数值不受影响）</span>`
      : "") +
    `</div>`;
  el.appendChild(meta);
  if (!result.groups.length) {
    el.insertAdjacentHTML("beforeend", `<p class="muted">查询结果为空（无分组）。</p>`);
    return;
  }
  result.groups.forEach((g, i) => el.appendChild(renderGroup(g, i)));
}

// 订单行点击展开/收起证据
document.addEventListener("click", (e) => {
  const row = e.target.closest("tr.order-row");
  if (!row) return;
  const detail = row.parentElement.querySelector(
    `tr.order-detail[data-gi="${row.dataset.gi}"][data-oi="${row.dataset.oi}"]`
  );
  if (detail) detail.style.display = detail.style.display === "none" ? "" : "none";
});

// ---------- 运行 ----------

async function runQuery() {
  const errEl = $("query-error");
  clearError(errEl);
  try {
    const result = await postJSON("/api/query", { sql: $("sql-input").value });
    renderResult(result);
  } catch (e) {
    $("result").innerHTML = `<p class="muted">查询失败，未展示任何结果。</p>`;
    showError(errEl, e);
  }
}

$("btn-upload").addEventListener("click", uploadData);
$("btn-sample-data").addEventListener("click", loadSampleData);
$("btn-run").addEventListener("click", runQuery);
$("file-input").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (f) $("json-input").value = await f.text();
});

// 页面加载后回填已装载的数据集摘要
getJSON("/api/dataset").then((d) => {
  if (d.loaded) renderDatasetSummary(d.summary);
}).catch(() => {});
