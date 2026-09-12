const $ = (s) => document.querySelector(s),
  esc = (s) =>
    String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
let me = null,
  owner = "",
  page = "overview",
  jobs = [],
  stores = [],
  wf = {},
  mods = [],
  overview = {},
  filter = "",
  search = "",
  production = null,
  sourceMode = "account",
  selectionDrafts = new Map(),
  rendering = false;
const phase = {
  archived: "已归档",
  offline: "已下架",
  queued: "待识别",
  matched: "利润核算",
  qualified: "等待店铺",
  ready: "准备上架",
  prepared: "待提交",
  publishing: "提交回查",
  reconciling: "平台处理中",
  stock_ready: "准备库存",
  stock_pending: "库存回查",
  checking: "确认可售",
  selling: "已验证可售",
  rejected: "未通过规则",
  attention: "需要处理",
};
const kinds = {
  candidates: "商品候选池",
  matcher: "1688 同款识别",
  profit: "利润计算",
  publisher: "上架与回查",
};

const navGroups = [
  ["上架工作", ["overview", "运行概览"], ["jobs", "上架商品"]],
  ["自动化", ["sources", "商品来源与筛选"], ["calculator", "利润计算器"], ["workflow", "工作流配置"], ["modules", "模块中心"]],
  ["店铺资产", ["stores", "店铺连接"], ["blocks", "禁止上架清单"]],
];
const paths = {
  overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  jobs: "M4 5h16v15H4z M4 10h16 M9 10v10",
  workflow: "M5 5h14 M5 12h14 M5 19h14 M9 3v4 M15 10v4 M10 17v4",
  modules: "M4 4h6v6H4z M14 4h6v6h-6z M4 14h6v6H4z M17 14v6 M14 17h6",
  stores: "M3 9l2-5h14l2 5 M3 9v3h18V9 M5 12v8h14v-8 M10 20v-5h4v5",
  blocks: "M12 3l8 3v6c0 5-8 9-8 9S4 17 4 12V6z M9 9l6 6 M15 9l-6 6",
  users:
    "M16 20v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 3a4 4 0 1 0 0 8a4 4 0 1 0 0-8 M17 4a4 4 0 0 1 0 7 M18 14a4 4 0 0 1 4 4v2",
  events: "M5 3h14v18H5z M9 7h6 M9 12h6 M9 17h4",
  search: "M10 3a7 7 0 1 0 0 14a7 7 0 1 0 0-14 M15 15l6 6",
  arrow: "M5 12h14 M14 7l5 5-5 5",
};
function icon(name) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${(
    paths[name] || paths.overview
  )
    .split(" M")
    .map((d, i) => `<path d="${i ? "M" : ""}${d}"/>`)
    .join("")}</svg>`;
}
function stateBadge(j) {
  return `<span class="badge ${j.phase === "selling" ? "success" : ["rejected", "attention"].includes(j.phase) ? "gray" : "pending"}">${phase[j.phase] || esc(j.phase)}</span>`;
}
function visibleJobs() {
  const q = search.trim().toLowerCase();
  return jobs.filter(
    (j) =>
      (!filter || j.phase === filter) &&
      (!q ||
        [j.title, j.source_key, j.id].some((v) =>
          String(v).toLowerCase().includes(q),
        )),
  );
}
function drawJobs() {
  const list = visibleJobs();
  $("#jobtable").innerHTML = table(list);
  $("#resultcount").textContent = `${list.length} / ${jobs.length} 件`;
  bindJobs();
}

function toast(t) {
  $("#toast").textContent = t;
  $("#toast").style.display = "block";
  setTimeout(() => ($("#toast").style.display = "none"), 4500);
}
async function api(path, method = "GET", body, scoped = true) {
  const suffix =
    scoped && owner
      ? (path.includes("?") ? "&" : "?") + "owner=" + encodeURIComponent(owner)
      : "";
  const r = await fetch("/api" + path + suffix, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": me?.csrf || "",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const d = await r.json();
  if (!r.ok) {
    if (r.status === 401 && path != "/login") {
      me = null;
      login();
    }
    throw Error(
      typeof d.detail === "string" ? d.detail : "填写信息不完整或格式不正确",
    );
  }
  return d;
}
function login(change = false) {
  $("#root").innerHTML =
    `<div class="login"><section class="login-art"><div class="brand"><span class="mark">f</span>FlowHub</div><div class="orbit"></div><h1>从候选商品<br>到确认可售。</h1><p>模块可替换 · 工作区独立 · 任务可恢复</p></section><section class="login-form"><form class="login-inner" id="login"><h2>${change ? "设置你的新密码" : "登录工作台"}</h2><p class="muted">${change ? "首次登录需要更改初始密码。" : "使用管理员分配的账号登录。"}</p><label>${change ? "当前临时密码" : "账号"}</label><input id="username" ${change ? 'type="password"' : 'autocomplete="username"'} required><label>${change ? "新密码 · 至少 12 位" : "密码"}</label><input id="password" type="password" autocomplete="${change ? "new-password" : "current-password"}" required ${change ? 'minlength="12"' : ""}><button class="primary">${change ? "保存并重新登录" : "登录"}</button><p class="footnote">FlowHub / 0.3<br>店铺凭据仅保存在服务端，不向其他用户公开。</p></form></section></div>`;
  $("#login").onsubmit = async (e) => {
    e.preventDefault();
    try {
      if (change) {
        await api(
          "/password",
          "POST",
          { current: $("#username").value, new: $("#password").value },
          false,
        );
        me = null;
        login();
        toast("密码已更新，请重新登录");
      } else {
        await api(
          "/login",
          "POST",
          { username: $("#username").value, password: $("#password").value },
          false,
        );
        await boot();
      }
    } catch (e) {
      toast(e.message);
    }
  };
}
async function boot() {
  try {
    me = await api("/me", "GET", undefined, false);
    if (me.must_change) {
      login(true);
      return;
    }
    owner = owner || me.id;
    await shell();
  } catch {
    login();
  }
}
async function shell() {
  const users =
    me.role === "admin" ? await api("/users", "GET", undefined, false) : [];
  const groups = [
    ...navGroups,
    ...(me.role === "admin"
      ? [["系统管理", ["users", "用户管理"], ["events", "故障记录"]]]
      : []),
  ];
  $("#root").innerHTML =
    `<aside><div class="brand"><span class="mark">f</span><span>FlowHub<small>跨境上架工作台</small></span></div><nav>${groups.map(([label, ...items]) => `<div class="nav-group"><div class="workspace">${label}</div>${items.map(([id, name]) => `<button data-page="${id}">${icon(id)}<span>${name}</span></button>`).join("")}</div>`).join("")}</nav><footer><span class="avatar">${esc(me.username[0].toUpperCase())}</span><div><strong>${esc(me.username)}</strong><small>${me.role === "admin" ? "管理员" : "独立工作区"}</small></div><button id="logout" class="linkbutton">退出</button></footer></aside><main class="app"><header class="topbar"><span class="breadcrumb">工作空间 <span>/</span> <strong id="currentpage">运行概览</strong></span><div class="toolbar">${users.length ? `<select id="owner" aria-label="切换工作区">${users.map((u) => `<option value="${u.id}" ${owner === u.id ? "selected" : ""}>${esc(u.username)} 的工作区</option>`).join("")}</select>` : ""}<span class="version">本地验收版</span></div></header><div class="content" id="content"></div></main>`;
  document.querySelectorAll("[data-page]").forEach(
    (b) =>
      (b.onclick = async () => {
        page = b.dataset.page;
        filter = "";
        search = "";
        production = null;
        await render();
        window.scrollTo(0, 0);
      }),
  );
  $("#logout").onclick = async () => {
    await api("/logout", "POST");
    me = null;
    owner = "";
    sourceMode = "account";
    selectionDrafts.clear();
    login();
  };
  if ($("#owner"))
    $("#owner").onchange = (e) => {
      owner = e.target.value;
      search = "";
      sourceMode = "account";
      production = null;
      render();
    };
  await render();
}
function head(title, desc, actions = "") {
  return `<div class="pagehead"><div><h1>${title}</h1><p>${desc}</p></div><div class="actions">${actions}</div></div>`;
}
function table(list) {
  return `<div class="table-wrap"><table class="jobs-table"><thead><tr><th>商品 / 来源</th><th>1688 同款</th><th>相似度 / 结构分数</th><th>成本利润率</th><th>店铺 / 进度</th><th></th></tr></thead><tbody>${list.map((j) => `<tr class="row" data-job="${esc(j.id)}" tabindex="0" aria-label="查看商品 ${esc(j.source_key)}"><td><div class="product"><img src="${esc(j.image)}" alt="候选商品" loading="lazy"><div><strong>${esc(j.title)}</strong><span class="muted tiny">${j.live ? "OZON" : "模拟"} · ${esc(j.source_key)}</span></div></div></td><td>${j.supplier_image ? `<img class="supplier-thumb" src="${esc(j.supplier_image)}" alt="1688同款" loading="lazy">` : '<span class="muted tiny">待识别</span>'}</td><td>${j.score == null ? '<span class="muted">—</span>' : `<span class="score">${(j.score * 100).toFixed(1)}<small> / 100</small></span><div class="score-track"><span style="width:${Math.max(0, Math.min(100, j.score * 100))}%"></span></div><small class="muted">结构 ${j.dhash == null ? "—" : (j.dhash * 100).toFixed(1)}</small>`}</td><td><strong class="profit ${j.profit != null && j.profit < 0 ? "negative" : ""}">${j.profit == null ? "—" : j.profit.toFixed(1) + "%"}</strong></td><td><div class="store-label">${esc(j.store_name || stores.find((s) => s.id === j.store_id)?.name || "待分配店铺")}</div>${stateBadge(j)}</td><td><span class="row-arrow">${j.can_manage ? "管理" : "↗"}</span></td></tr>`).join("") || '<tr><td colspan="6"><div class="empty">没有匹配的商品<p>调整筛选条件，或连接店铺后启动工作流。</p></div></td></tr>'}</tbody></table></div>`;
}
function bindJobs() {
  document.querySelectorAll("[data-job]").forEach((e) => {
    e.onclick = () => detail(jobs.find((j) => j.id === e.dataset.job));
    e.onkeydown = (k) => {
      if (k.key === "Enter") e.click();
    };
  });
}
function detail(j) {
  const el = document.createElement("div");
  el.className = "drawer-bg";
  el.innerHTML = `<div class="drawer"><button class="close" aria-label="关闭商品详情">✕</button><p class="muted tiny">商品详情 / ${j.live ? "真实上架" : "模拟验收"}</p><h2>${esc(j.title)}</h2>${stateBadge(j)}<div class="compare"><div><img src="${esc(j.image)}" alt="候选商品原图"><small>候选商品图片</small></div><div>${j.supplier_image ? `<img src="${esc(j.supplier_image)}" alt="1688同款原图">` : "尚未识别"}<small>1688 同款图片</small></div></div>${[
    ["源商品 SKU", j.source_key],
    ["所属店铺", j.store_name || stores.find((s) => s.id === j.store_id)?.name || "待分配"],
    [
      "图片 / 结构分数",
      j.score === null
        ? "—"
        : `${((j.score || 0) * 100).toFixed(1)} / ${j.dhash == null ? "—" : (j.dhash * 100).toFixed(1)}`,
    ],
    ["成本利润率", j.profit == null ? "—" : j.profit.toFixed(2) + "%"],
    ["目标库存", j.stock],
    ...(j.price != null ? [["价格参考（CNY）", j.price.toFixed(2)]] : []),
    ...(j.observed_stock != null ? [["最近操作确认库存", j.observed_stock]] : []),
    ["当前说明", j.note],
    ["资料缺项", (j.dossier_missing || []).join("；") || "—"],
  ]
    .map(
      ([k, v]) =>
        `<div class="kv"><span class="muted">${k}</span><span>${esc(v)}</span></div>`,
    )
    .join(
      "",
    )}<p class="tiny muted" style="margin-top:20px">任务编号 ${esc(j.id)}</p>${j.can_manage ? `<section class="product-actions"><h3>商品操作</h3><p class="tiny muted">库存调整作用于嘉兴邮政仓；下架会清零全部已查询到的仓库。改价单位为人民币（CNY）。</p><div class="row2"><button data-operation="activate">继续上架 · 库存99</button><button data-operation="delist">下架 · 库存清零</button><button data-operation="archive">下架并归档</button><button data-operation="verify">重新回查操作</button></div><label>嘉兴邮政仓库存</label><div class="row2"><input aria-label="新的库存" id="manage-stock" type="number" min="0" max="10000" step="1" value="${j.observed_stock ?? j.stock}"><button data-operation="stock">调整库存</button></div><label>新价格（CNY）</label><div class="row2"><input aria-label="新的价格" id="manage-price" type="number" min="0.01" step="0.01" value="${j.price || ""}"><button data-operation="price">调整价格</button></div><div id="action-review"></div><p id="action-result" role="status" class="tiny muted"></p></section>` : ""}${j.supplier_url?.startsWith("https://") ? `<a target="_blank" rel="noreferrer" href="${esc(j.supplier_url)}">查看货源页面 ↗</a>` : ""}</div>`;
  document.body.append(el);
  const previousFocus = document.activeElement;
  const drawer = el.querySelector(".drawer");
  drawer.setAttribute("role", "dialog");
  drawer.setAttribute("aria-modal", "true");
  drawer.setAttribute("aria-label", "商品图片对比");
  const close = () => {
    el.remove();
    previousFocus?.focus();
  };
  el.querySelectorAll("[data-operation]").forEach(button => {
    button.onclick = () => {
      const action = button.dataset.operation;
      const value = action === "stock" ? Number(el.querySelector("#manage-stock").value) : action === "price" ? Number(el.querySelector("#manage-price").value) : null;
      const labels = {activate:"继续上架，嘉兴邮政仓库存设为99", delist:"下架，所有已查询仓库库存清零", archive:"清零库存后归档", stock:`嘉兴邮政仓库存设为 ${value}`, price:`售价调整为 ${value} CNY`, verify:"重新回查上次操作"};
      const review = el.querySelector("#action-review");
      review.innerHTML = `<p>${esc(j.store_name)} · ${esc(j.source_key)}<br>${esc(labels[action])}</p><button id="confirm-action" class="primary">${action === "verify" ? "开始回查" : "确认执行"}</button>`;
      review.querySelector("button").onclick = async () => {
        el.querySelectorAll("[data-operation],#confirm-action").forEach(b => b.disabled = true);
        const output = el.querySelector("#action-result");
        output.textContent = "正在核验并执行，请勿重复提交…";
        try {
          const result = await api(`/production/${encodeURIComponent(j.id)}/action`, "POST", {action, value, request_id:crypto.randomUUID()});
          output.textContent = result.message;
          j.note = result.message;
          if(result.observed_stock != null) j.observed_stock = result.observed_stock;
          if(result.observed_price != null) j.price = result.observed_price;
        } catch(e) { output.textContent = e.message; }
        finally { review.innerHTML = ""; el.querySelectorAll("[data-operation]").forEach(b => b.disabled = false); }
      };
    };
  });
  el.querySelector(".close").focus();
  el.onclick = (e) => {
    if (e.target === el || e.target.closest(".close")) close();
  };
  el.onkeydown = (e) => {
    if (e.key === "Escape") close();
    if (e.key === "Tab") {
      const targets = [...el.querySelectorAll("button:not(:disabled),a[href],input,select")];
      const first = targets[0],
        last = targets[targets.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
}
async function renderProfitCalculator(container) {
  const catalog = await api('/profit/catalog');
  const field = (name, label, value = '', step = '0.01') => `<div><label>${label}</label><input aria-label="${label}" name="${name}" type="number" min="0" step="${step}" value="${value}" required></div>`;
  container.innerHTML = head('利润计算器', '邮政与 GUOO · 本地核算，不消耗毛子 ERP 请求额度') +
    `<div class="notice"><strong>官方佣金已接入 · FBS 自发货</strong><p>${esc(catalog.commission.message)}</p><a href="${esc(catalog.commission.official_url)}" target="_blank" rel="noopener">查看 Ozon 官方佣金说明 ↗</a></div>
    <form id="profitform" class="formarea" style="max-width:none">
    <div class="grid2"><div><label>物流商</label><select name="provider"><option value="ChinaPost">邮政陆运</option><option value="GUOO">GUOO</option></select></div>
    <div><label>配送模式</label><select name="mode"><option value="realFBS">FBS 自发货（官方 realFBS）</option><option value="FBP">FBP</option></select></div>
    <div><label>测算线路</label><select name="route_id"></select></div><div><label>佣金方式</label><select name="commission_method" aria-label="佣金方式"><option value="official">官方类目自动匹配</option><option value="manual">手动费率</option></select></div>
    <div><label>搜索具体商品类型</label><input name="category" placeholder="例如：按摩枕、收纳盒、手机"><button type="button" id="searchcommission">搜索官方类目</button></div>
    <div><label>选择官方商品类型</label><select name="commission_row_id" aria-label="选择官方商品类型"><option value="">先搜索并选择商品类型</option></select></div>
    <div><label>实际品牌</label><input name="brand" placeholder="例如 Apple；无品牌填无品牌"></div>
    ${field('sell_cny','预计成交价 · 人民币')}${field('rub_per_cny','结算汇率 · 1 人民币 = 多少卢布','','0.0001')}
    ${field('purchase_cny','采购成本 · 元')}${field('weight_g','包装后重量 · 克','','1')}
    ${field('length','长 · 厘米')}${field('width','宽 · 厘米')}${field('height','高 · 厘米')}
    ${field('commission_pct','该类目佣金 · %')}
    <div><label>佣金来源 / 核实日期</label><input name="commission_source" required placeholder="例如：店铺后台费率，2026-09-10"></div>
    ${field('domestic_cny','国内运费 · 元','0')}${field('packing_cny','包装 / 贴单 · 元','0')}${field('other_cny','其他固定费用 · 元','0')}
    ${field('ads_pct','广告费占售价 · %','0')}${field('reserve_pct','退货 / 损耗预留占售价 · %','0')}${field('profit_min','成本利润率门槛 · %','30')}
    </div><div id="guoo-fees" class="grid2" hidden>
    ${field('acquiring_pct','GUOO 收单业务费 · %')}${field('last_mile_cny','GUOO 另计尾程费用 · 元')}${field('withdrawal_pct','GUOO 提现手续费 · %')}
    </div><p id="feehelp" class="muted tiny"></p>
    <button class="primary" type="submit">计算各线路利润</button><p class="muted tiny">附加费用默认 0 表示未预留，请按实际填写。计算不会上架、改价或切换生产物流。</p></form>
    <div id="profitresult" style="margin-top:24px"></div>`;
  const form = $('#profitform');
  function routes() {
    const provider = form.elements.provider.value;
    const postal = provider === 'ChinaPost';
    if (postal) form.elements.mode.value = 'realFBS';
    form.elements.mode.disabled = postal;
    const options = catalog.routes.filter(r => r.provider === provider && r.mode === form.elements.mode.value);
    form.elements.route_id.innerHTML = '<option value="">比较全部适用线路</option>' + options.map(r => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('');
    $('#guoo-fees').hidden = postal;
    for (const key of ['acquiring_pct','last_mile_cny','withdrawal_pct']) form.elements[key].disabled = postal;
    $('#feehelp').textContent = postal ? '邮政按原表：26 元/公斤 + 1.9 元/票；收单 1.9%；尾程为售价的 2%（最低 1.3 元，最高 18 元）；结算余额提现 1.2%。已修正原表漏扣收单费的错误。' : 'GUOO 表只提供运费。请填写实际收单费、另计尾程费和提现费；已经包含的费用明确填 0，避免重复扣费。';
  }
  function commissionMode() {
    const automatic = form.elements.commission_method.value === 'official';
    form.elements.commission_pct.disabled = automatic;
    form.elements.commission_source.disabled = automatic;
    form.elements.commission_row_id.disabled = !automatic;
    form.elements.commission_row_id.required = automatic;
    form.elements.commission_pct.placeholder = automatic ? '按类目、品牌和价格自动计算' : '';
    form.elements.commission_source.placeholder = automatic ? 'Ozon 官方中国卖家费率表' : '费率来源和日期';
    $('#profitresult').innerHTML = '';
  }
  form.elements.commission_method.onchange = commissionMode;
  commissionMode();
  $('#searchcommission').onclick = async () => {
    try {
      const r = await api('/profit/categories?q=' + encodeURIComponent(form.elements.category.value));
      form.elements.commission_row_id.innerHTML = '<option value="">请选择商品类型（最多显示 60 条，可缩小关键词）</option>' + r.items.map(x => `<option value="${esc(x.id)}">${esc(x.name)} / ${esc(x.category)} / ${esc(x.brand === 'All' ? '通用品牌' : x.brand)}</option>`).join('');
      if (!r.items.length) toast('没有匹配类型，请尝试更具体的名称');
    } catch (error) { toast(error.message); }
  };
  form.oninput = form.onchange = () => { $("#profitresult").innerHTML = ""; };
  form.elements.provider.onchange = routes;
  form.elements.mode.onchange = routes;
  routes();
  form.onsubmit = async e => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(form));
    payload.mode = form.elements.mode.value;
    delete payload.commission_method;
    payload.dimensions_cm = [payload.length, payload.width, payload.height];
    for (const key of ['length','width','height']) delete payload[key];
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const r = await api('/profit/calculate', 'POST', payload);
      const feeNames = {purchase:'采购',domestic:'国内运费',packing:'包装贴单',freight:'国际运费',commission:'佣金',acquiring:'收单费',last_mile:'另计尾程',withdrawal:'提现费',ads:'广告费',reserve:'损耗预留',other:'其他'};
      $('#profitresult').innerHTML = `<div class="panel"><h2>预计成交 ${r.sell_rub.toFixed(2)} ₽ · 佣金 ${r.commission_pct}%</h2><p class="muted">成本利润率 = 利润 ÷ 总成本。利润达标仍需核实店铺线路与商品承运条件。</p>
      ${r.quotes.length ? `<div class="table-wrap"><table><thead><tr><th>适用线路</th><th>计费重</th><th>国际运费</th><th>总成本</th><th>利润</th><th>成本利润率</th><th>净利率</th></tr></thead><tbody>${r.quotes.map(q => `<tr><td>${esc(q.name)}</td><td>${q.billed_kg} kg</td><td>¥${q.fees_cny.freight.toFixed(2)}</td><td>¥${q.total_cost.toFixed(2)}</td><td>¥${q.profit_cny.toFixed(2)}</td><td><span class="badge ${q.profit_pass ? 'success' : 'gray'}">${q.cost_return.toFixed(2)}% · ${q.profit_pass ? '达标' : '未达标'}</span></td><td>${q.net_margin.toFixed(2)}%</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">没有适用线路，请查看重量、货值和尺寸限制。</div>'}
      ${r.quotes.map(q => `<details style="margin-top:16px"><summary>${esc(q.name)} · 费用明细与承运备注</summary><p>${Object.entries(q.fees_cny).map(([k,v]) => `${feeNames[k]} ¥${v.toFixed(2)}`).join(' · ')}</p><p>${esc(q.battery_note)}</p><p class="muted tiny">来源：${esc(q.source_cell)}</p></details>`).join('')}
      <details style="margin-top:16px"><summary>不适用线路（${r.unavailable.length}）</summary>${r.unavailable.map(q => `<p>${esc(q.name)}：${q.reasons.map(esc).join('、')}</p>`).join('')}</details>
      <p class="muted tiny">${r.warnings.map(esc).join('<br>')}</p><p class="muted tiny">费率版本 ${esc(r.version)} · ${r.commission_status === "official" ? "官方自动匹配：" + esc(r.commission_match.name) + " · " + esc(r.commission_match.tier_label) : "手动佣金"} · ${esc(r.commission_source)}</p></div>`;
    } catch (error) { toast(error.message); } finally { button.disabled = false; }
  };
}

async function render() {
  if (rendering || (page === "sources" && document.activeElement?.closest("#source-library-form"))) return;
  rendering = true;
  const focusedSearch = document.activeElement?.id === "jobsearch";
  const caret = focusedSearch ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  document
    .querySelectorAll("[data-page]")
    .forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  try {
    [wf, stores, mods, overview] = await Promise.all([
      api("/workflow"),
      api("/stores"),
      api("/modules"),
      api("/overview"),
    ]);
    const c = $("#content");
    c.classList.toggle("source-library-view", page === "sources");
    const activeNav = document.querySelector("[data-page].active");
    if ($("#currentpage") && activeNav)
      $("#currentpage").textContent = activeNav.textContent;
    if (page === "sources") {
      await renderSourceLibrary(c);
    } else if (page === "overview" || page === "jobs") {
      jobs = (
        await api(
          "/jobs" + (filter ? "?phase=" + encodeURIComponent(filter) : ""),
        )
      ).sort((a, b) => b.updated - a.updated);
      production = me.role === "admin" ? await api("/production" + (filter ? "?phase=" + encodeURIComponent(filter) : "")) : null;
      const hasProduction = production?.available;
      if (sourceMode !== "production") production = null;
      if (production?.available) {
        jobs = production.jobs;
        overview = production.overview;
        wf = {...wf, enabled: production.active, rules: {...wf.rules, live: true, stock: 99, logistics: "ChinaPost"},
          notice: `${production.notice ? production.notice + " " : ""}数据来源：本地正式上架流程 · 当前店铺 ${production.shop_name} · 每 10 秒刷新 · 最近读取 ${new Date(production.fetched_at * 1000).toLocaleTimeString()}。此处展示已进入正式上架的任务。`};
      }
      const selectionKey = `${owner || me.id}:${wf.rules.live}`;
      const eligibleStores = stores.filter(s => s.enabled && s.verified && (wf.rules.live ? s.kind !== "demo" : s.kind === "demo"));
      const selectedIds = wf.enabled ? (wf.selected_store_ids || eligibleStores.map(s => s.id)) : (selectionDrafts.get(selectionKey) || wf.selected_store_ids || eligibleStores.map(s => s.id));
      const processing = Object.entries(overview.phases)
        .filter(([k]) => !["selling", "rejected", "attention", "archived", "offline"].includes(k))
        .reduce((s, [, v]) => s + v, 0);
      c.innerHTML =
        head(
          page === "overview" ? "运行概览" : "上架商品",
          `${wf.rules.live ? "真实上架" : "模拟验收"} · ${wf.rules.logistics === "ChinaPost" ? "邮政物流" : esc(wf.rules.logistics)} · 目标库存 ${wf.rules.stock}`,
          `${hasProduction ? `<select id="sourceview" aria-label="进度来源"><option value="account" ${sourceMode === "account" ? "selected" : ""}>本账号上架任务</option><option value="production" ${sourceMode === "production" ? "selected" : ""}>已有正式流程记录</option></select>` : ""}<button id="refresh">刷新数据</button>${production?.available ? "" : `<button class="${wf.enabled ? "" : "primary"}" id="toggle">${wf.enabled ? "暂停新增" : "启动上架"}</button>`}`,
        ) +
        (!production?.available ? `<section class="panel store-selection"><div class="panel-title"><h2>上架店铺</h2><span class="muted tiny">${wf.enabled ? "运行中 · 暂停新增后可更改店铺" : "勾选店铺后启动 · 同账号共享任务与店铺"}</span></div><div class="store-options">${eligibleStores.map(s => `<label class="store-option"><input type="checkbox" data-target-store="${esc(s.id)}" ${selectedIds.includes(s.id) ? "checked" : ""} ${wf.enabled ? "disabled" : ""}><span><strong>${esc(s.name)}</strong><small>${s.kind === "demo" ? "模拟店铺" : esc(s.config.shop_id)}</small></span></label>`).join("") || '<p class="muted">尚无可用店铺，请先在「店铺连接」中添加并核验与当前模式对应的店铺。</p>'}</div><p class="muted tiny">只向勾选店铺分配新任务；已提交商品继续在原店回查。换电脑登录同一账号后可查看相同店铺和运行进度。</p></section>` : "") +
        (page === "overview"
          ? `<div class="metrics">${[
              [
                "已确认可售",
                overview.phases.selling || 0,
                "可售状态与库存已核验",
                "jobs",
              ],
              ["待完成任务", processing, "包含待识别及平台处理中", "workflow"],
              [
                "近一小时完成",
                overview.last_hour,
                "最近 60 分钟确认可售",
                "overview",
              ],
              [
                "需处理异常",
                overview.phases.attention || 0,
                "需要人工检查的任务",
                "events",
              ],
            ]
              .map(
                ([t, n, d, i]) =>
                  `<div class="metric"><div class="metric-label">${t}${icon(i)}</div><strong>${n}<span>件</span></strong><small>${d}</small></div>`,
              )
              .join(
                "",
              )}</div><section class="flow-panel"><div class="flow-heading"><div><span class="dot ${overview.worker_alive ? "" : "off"}"></span><strong>自动上架流程</strong><span class="badge ${wf.enabled && !production?.waiting ? "success" : "gray"}">${production?.available ? esc(production.status_label) : wf.enabled ? "正在运行" : "新增已暂停"}</span></div><small class="muted">${overview.heartbeat ? "心跳 " + new Date(overview.heartbeat * 1000).toLocaleTimeString() : ""}</small></div><div class="flow-stages">${[
              ["candidates", "候选商品", overview.phases.queued || 0],
              ["matcher", "同款识别", overview.phases.matched || 0],
              ["profit", "利润审核", overview.phases.qualified || 0],
              [
                "publisher",
                "上架与回查",
                [
                  "ready",
                  "prepared",
                  "publishing",
                  "reconciling",
                  "stock_ready",
                  "stock_pending",
                  "checking",
                ].reduce((n, k) => n + (overview.phases[k] || 0), 0),
              ],
            ]
              .map(
                ([k, t, n], i) =>
                  `<div class="flow-stage"><span class="stage-number">0${i + 1}</span><div><strong>${t}</strong><small>${n} 件待完成</small></div>${i < 3 ? '<span class="stage-arrow">→</span>' : ""}</div>`,
              )
              .join(
                "",
              )}</div><div class="flow-note">${esc(wf.notice) || "连接店铺并配置模块后，即可启动工作流。"}</div></section>`
          : "") +
        `<section class="panel"><div class="panel-title"><h2>${page === "overview" ? "商品进度" : "商品列表"}</h2><span class="muted tiny" id="resultcount"></span></div><div class="list-toolbar"><div class="tabs">${[
          ["", "全部"],
          ["selling", "已可售"],
          ["rejected", "未通过"],
          ["attention", "需处理"],
          ["offline", "已下架"],
          ["archived", "已归档"],
        ]
          .map(
            ([k, t]) =>
              `<button data-filter="${k}" class="${filter === k ? "active" : ""}">${t}<span>${k ? overview.phases[k] || 0 : Object.values(overview.phases).reduce((n, v) => n + v, 0)}</span></button>`,
          )
          .join(
            "",
          )}</div><div class="search-field">${icon("search")}<input id="jobsearch" aria-label="搜索商品" placeholder="搜索标题、源 SKU 或任务编号" value="${esc(search)}"></div></div><div id="jobtable"></div><div class="table-foot">展示最近 100 件 · 点击商品查看图片对比<span>相似度为评分，不代表匹配正确率</span></div></section>`;
      drawJobs();
      $("#jobsearch").oninput = (e) => {
        search = e.target.value;
        drawJobs();
      };
      if (focusedSearch) { $("#jobsearch").focus(); $("#jobsearch").setSelectionRange(...caret); }
      $("#refresh").onclick = render;
      if ($("#sourceview")) $("#sourceview").onchange = (e) => { sourceMode = e.target.value; filter = ""; render(); };
      document.querySelectorAll("[data-target-store]").forEach(input => input.onchange = () => {
        selectionDrafts.set(selectionKey, [...document.querySelectorAll("[data-target-store]:checked")].map(el => el.dataset.targetStore));
      });
      if ($("#toggle")) $("#toggle").onclick = async () => {
        try {
          const ids = [...document.querySelectorAll("[data-target-store]:checked")].map(el => el.dataset.targetStore);
          if (!wf.enabled && !ids.length) { toast("请至少勾选一家上架店铺"); return; }
          $("#toggle").disabled = true;
          await api("/workflow/" + (wf.enabled ? "pause" : "start"), "POST", wf.enabled ? undefined : {store_ids: ids});
          selectionDrafts.delete(selectionKey);
          await render();
        } catch (e) {
          toast(e.message);
          if ($("#toggle")) $("#toggle").disabled = false;
        }
      };
      document.querySelectorAll("[data-filter]").forEach(
        (e) =>
          (e.onclick = () => {
            filter = e.dataset.filter;
            render();
          }),
      );
    } else if (page === "stores") {
      c.innerHTML =
        head("店铺连接", "店铺绑定在当前 FlowHub 账号下；其他电脑登录同一服务和账号后自动显示。") +
        `<div class="grid2"><div class="panel"><div class="panel-title"><h2>已连接店铺</h2></div><table><thead><tr><th>顺序 / 店铺</th><th>连接</th><th>操作</th></tr></thead><tbody>${stores.map((s) => `<tr><td>${s.position + 1} · ${esc(s.name)}<br><small class="muted">${s.kind === "demo" ? "模拟店铺" : esc(s.config.shop_id)}</small></td><td>${s.verified ? "已核验" : "待核验"}<br><small class="muted">${overview.quotas.find((q) => q.store_id === s.id) ? "余 " + overview.quotas.find((q) => q.store_id === s.id).remaining + " 个额度" : ""}</small></td><td><button class="linkbutton" data-verify="${s.id}">核验</button><button class="linkbutton" data-enable="${s.id}">${s.enabled ? "停用" : "启用"}</button></td></tr>`).join("") || '<tr><td colspan="3" class="empty">还没有连接店铺</td></tr>'}</tbody></table></div><form id="storeform" class="formarea"><h2>连接新店铺</h2><label>店铺名称</label><input name="name" placeholder="例如：我的一号店" required><div class="row2"><div><label>连接方式</label><select name="kind"><option value="demo">模拟店铺</option><option value="maozi">毛子 ERP + Ozon</option><option value="ozon">Ozon 官方直连（无需 ERP）</option><option value="http">自定义上架 API</option></select></div><div><label>排序 · 0 为第一家</label><input name="position" type="number" min="0" value="${stores.length}"></div></div>${[
          ["shop_id", "毛子店铺 ID"],
          ["warehouse_id", "目标仓库 ID"],
          ["watermark_id", "水印 ID"],
          ["client_id", "Ozon Client ID"],
          ["api_key", "Ozon API Key"],
          ["erp_token", "毛子 ERP Token"],
        ]
          .map(
            ([k, t]) =>
              `<label>${t}</label><input name="${k}" ${["api_key", "erp_token"].includes(k) ? 'type="password" autocomplete="new-password"' : ""}>`,
          )
          .join(
            "",
          )}<button class="primary" style="margin-top:22px">保存连接</button><p class="muted tiny" style="margin-top:15px">密钥加密保存，不会回显。真实店铺需核验后才能使用。</p></form></div>`;
      $("#storeform [name=kind]").onchange = (e) => {
        for (const name of ["shop_id", "watermark_id", "erp_token"]) {
          const input = $("#storeform [name=" + name + "]");
          input.hidden = e.target.value === "ozon";
          input.previousElementSibling.hidden = input.hidden;
        }
      };
      $("#storeform").onsubmit = async (e) => {
        e.preventDefault();
        let p = Object.fromEntries(new FormData(e.target));
        p.position = Number(p.position);
        if (p.kind === "demo") {
          p.shop_id = "demo";
          p.warehouse_id = "demo-warehouse";
          p.watermark_id = "0";
        }
        try {
          await api("/stores", "POST", p);
          toast("店铺已保存");
          render();
        } catch (e) {
          toast(e.message);
        }
      };
      document.querySelectorAll("[data-verify]").forEach(
        (b) =>
          (b.onclick = async () => {
            b.disabled = true;
            try {
              await api("/stores/" + b.dataset.verify + "/verify", "POST");
              toast("连接已核验");
              render();
            } catch (e) {
              toast(e.message);
              b.disabled = false;
            }
          }),
      );
      document.querySelectorAll("[data-enable]").forEach(
        (b) =>
          (b.onclick = async () => {
            let s = stores.find((s) => s.id === b.dataset.enable);
            await api("/stores/" + s.id, "PATCH", { enabled: !s.enabled });
            render();
          }),
      );
    } else if (page === "workflow") {
      c.innerHTML =
        head(
          "工作流配置",
          "模块按固定接口连接；修改只影响新候选，已提交任务保留原计划。",
          `<button class="primary" id="savewf">保存配置</button>`,
        ) +
        `<form id="wf" class="grid2"><div class="formarea"><h2>模块编排</h2>${Object.entries(
          kinds,
        )
          .map(
            ([k, t], i) =>
              `<div class="subsection"><label>0${i + 1} / ${t}</label><select name="module-${k}">${mods
                .filter((m) => m.kind === k)
                .map(
                  (m) =>
                    `<option value="${m.id}" ${wf.modules[k] === m.id ? "selected" : ""}>${esc(m.name)}</option>`,
                )
                .join(
                  "",
                )}</select><label>本工作区的模块密钥 ${wf.configured_credentials.includes(wf.modules[k]) ? "· 已配置" : ""}</label><input type="password" name="secret-${k}" placeholder="留空保持现有密钥"></div>`,
          )
          .join(
            "",
          )}</div><div class="formarea"><h2>商业规则</h2><p class="muted tiny">小商品须包裹重量＜500g且人民币售价＜135元，任一达到上限为大商品，信息不足为未知。成本利润率＜25%直接淘汰，达到25%才继续同款审核。DINO＜63% 淘汰；≥86% 且小商品直接通过；其余交千问：同款且≥82% 通过，不同款且≤64% 淘汰，其他人工审核。明确品牌或型号冲突淘汰。颜色、包装、角度和销售件数不单独否决。下方图片、结构阈值仅适用于旧模块。</p><div class="row2">${[
          ["profit_min", "旧模块利润率最低值 %（compareBot 固定25%）"],
          ["stock", "目标库存"],
          ["image_min", "图片分数最低值 / 100"],
          ["dhash_min", "结构分数最低值 / 100"],
        ]
          .map(
            ([k, t]) =>
              `<div><label>${t}</label><input name="${k}" type="number" value="${wf.rules[k]}" required></div>`,
          )
          .join(
            "",
          )}</div><label>物流渠道</label><input name="logistics" value="${esc(wf.rules.logistics)}"><label>候选补充间隔 / 秒</label><input type="number" name="interval" value="${wf.rules.interval}" min="10"><label>本次候选总上限 · 0 为持续运行</label><input type="number" name="max_items" value="${wf.rules.max_items || 0}" min="0"><label>累计实际提交上限 · 0 为不限</label><input type="number" name="max_publications" value="${wf.rules.max_publications || 0}" min="0"><div class="checkline"><input type="checkbox" name="live" id="live" ${wf.rules.live ? "checked" : ""}><label for="live">启用真实上架</label></div><p class="alert" style="margin-top:20px">真实模式会向已核验店铺提交商品并设置库存。须选择全部真实模块；请先确认店铺及规则。</p><p class="muted tiny">当前：${wf.enabled ? "运行中，请先暂停新增再保存配置" : "已暂停，可以修改配置"}</p></div></form>`;
      $("#savewf").onclick = async () => {
        let f = new FormData($("#wf")),
          p = { rules: {}, modules: {}, credentials: {} };
        for (let k of [
          "profit_min",
          "stock",
          "image_min",
          "dhash_min",
          "interval",
          "max_items",
          "max_publications",
        ])
          p.rules[k] = Number(f.get(k));
        p.rules.logistics = f.get("logistics");
        p.rules.live = f.get("live") === "on";
        for (let k of Object.keys(kinds)) {
          p.modules[k] = f.get("module-" + k);
          if (f.get("secret-" + k))
            p.credentials[p.modules[k]] = f.get("secret-" + k);
        }
        try {
          await api("/workflow", "PUT", p);
          toast("配置已保存");
          render();
        } catch (e) {
          toast(e.message);
        }
      };
    } else if (page === "calculator") {
      await renderProfitCalculator(c);
    } else if (page === "modules") {
      c.innerHTML =
        head(
          "模块中心",
          "管理员安装模块；各用户选择模块并填写自己的连接密钥。",
        ) +
        `<div class="grid2"><div>${mods.map((m, i) => `<div class="module-line"><span class="index">${String(i + 1).padStart(2, "0")}</span><div><h3>${esc(m.name)}</h3><p>${kinds[m.kind]} · 协议 v1 · ${esc(m.id)}</p></div><span class="badge gray">${m.driver === "demo" ? "模拟" : m.driver === "http" ? "API" : m.driver === "plugin" ? "插件" : "内置适配器"}</span></div>`).join("")}</div>${
          me.role === "admin"
            ? `<form id="moduleform" class="formarea"><h2>注册新模块</h2><label>模块 ID · 建议包含版本号</label><input name="id" required placeholder="my-matcher-v1"><label>显示名称</label><input name="name" required><label>模块类型</label><select name="kind">${Object.entries(
                kinds,
              )
                .map(([k, t]) => `<option value="${k}">${t}</option>`)
                .join(
                  "",
                )}</select><label>接入方式</label><select name="driver"><option value="http">HTTPS API</option><option value="plugin">已安装的服务器插件</option></select><label>API 地址 / 已安装插件 ID</label><input name="endpoint" required placeholder="https://api.example.com/flowhub"><button class="primary" style="margin-top:22px">注册模块</button><p class="muted tiny" style="margin-top:18px">不允许用户上传可执行代码。开发者插件由服务器管理员安装。</p></form>`
            : ""
        }</div>`;
      if ($("#moduleform"))
        $("#moduleform").onsubmit = async (e) => {
          e.preventDefault();
          try {
            await api(
              "/modules",
              "POST",
              Object.fromEntries(new FormData(e.target)),
            );
            toast("模块已注册");
            render();
          } catch (e) {
            toast(e.message);
          }
        };
    } else if (page === "users") {
      let users = await api("/users", "GET", undefined, false);
      c.innerHTML =
        head("用户管理", "管理员创建账号，每个账号拥有独立店铺、密钥和任务。") +
        `<div class="grid2"><div class="panel"><table><thead><tr><th>账号</th><th>角色</th><th>状态</th><th>操作</th></tr></thead><tbody>${users.map((u) => `<tr><td>${esc(u.username)}</td><td>${u.role === "admin" ? "管理员" : "用户"}</td><td>${u.active ? "正常" : "已停用"}</td><td>${u.id !== me.id && u.active ? `<button class="linkbutton" data-disable="${u.id}">停用</button>` : ""}</td></tr>`).join("")}</tbody></table></div><form class="formarea" id="userform"><h2>创建用户</h2><label>账号 · 字母、数字、下划线</label><input name="username" required><label>临时密码 · 至少12位</label><input type="password" name="password" minlength="12" required><button class="primary" style="margin-top:20px">创建账号</button><p class="muted tiny" style="margin-top:15px">新用户首次登录必须修改临时密码。</p></form></div>`;
      $("#userform").onsubmit = async (e) => {
        e.preventDefault();
        try {
          await api(
            "/users",
            "POST",
            Object.fromEntries(new FormData(e.target)),
            false,
          );
          toast("用户已创建");
          render();
        } catch (e) {
          toast(e.message);
        }
      };
      document.querySelectorAll("[data-disable]").forEach(
        (b) =>
          (b.onclick = async () => {
            await api(
              "/users/" + b.dataset.disable + "/disable",
              "POST",
              {},
              false,
            );
            render();
          }),
      );
    } else if (page === "events") {
      const ev = await api("/events");
      c.innerHTML =
        head(
          "故障记录",
          "仅管理员可查看；不包含密钥、外部接口原文或原始日志。",
        ) +
        `<section class="panel"><table><thead><tr><th>时间</th><th>事件</th><th>说明</th></tr></thead><tbody>${ev.map((e) => `<tr><td>${new Date(e.created * 1000).toLocaleString()}</td><td>${esc(e.code)}</td><td>${esc(e.message)}</td></tr>`).join("")}</tbody></table></section>`;
    } else if (page === "blocks") {
      const bs = await api("/blocks");
      c.innerHTML =
        head(
          "禁止上架清单",
          "命中清单的商品不会提交或补库存；已发布商品需要另行下架处理。",
        ) +
        `<div class="grid2"><div class="panel"><table><thead><tr><th>源 SKU</th><th>原因</th></tr></thead><tbody>${bs.map((b) => `<tr><td>${esc(b.source_key)}</td><td>${esc(b.reason)}</td></tr>`).join("") || '<tr><td colspan="2" class="empty">暂无记录</td></tr>'}</tbody></table></div><form id="blockform" class="formarea"><h2>添加禁止上架商品</h2><label>源商品 SKU</label><input name="source_key" required><label>原因</label><input name="reason"><button class="primary" style="margin-top:20px">加入清单</button></form></div>`;
      $("#blockform").onsubmit = async (e) => {
        e.preventDefault();
        await api(
          "/blocks",
          "POST",
          Object.fromEntries(new FormData(e.target)),
        );
        render();
      };
    }
  } catch (e) {
    toast(e.message);
  } finally {
    rendering = false;
  }
}
boot();
setInterval(() => {
  if (
    me &&
    !me.must_change &&
    ["overview", "jobs", "sources"].includes(page) &&
    !document.querySelector(".drawer-bg") &&
    !document.querySelector("[data-browser-seller]")?.value
  )
    render();
}, 10000);


let sourceLibraryDraft = null;
let sourceLibraryOwner = null;
let sourceLibraryCursor = 0;
let sourceLibraryState = "";
async function renderSourceLibrary(container) {
  if(sourceLibraryOwner !== owner){sourceLibraryOwner=owner;sourceLibraryDraft=null;sourceLibraryCursor=0;}
  const result = await api(`/sources?cursor=${sourceLibraryCursor}${sourceLibraryState ? "&state=" + sourceLibraryState : ""}`);
  const status = result.status;
  const storefrontTasks = await api("/sources/storefront");
  sourceLibraryDraft ||= {...status.filters};
  const labels = {qualified:"初筛通过", needs_review:"资料待补", rejected:"条件不符"};
  const fieldLabels = {sku:"SKU",seller_id:"来源店铺",title:"标题",image:"图片",category_id:"类目",price_min:"最低统计均价", price_max:"最高统计均价", weight_max_g:"最大重量", sales_min:"最低榜单销量", category:"类目", pure_fbs:"配送模式", follow_allowed:"可跟卖状态",same_seller:"种子同店来源证据", freshness:"数据新鲜度"};
  const stamp = value => value ? new Date(value*1000).toLocaleString() : "尚未核查";
  container.innerHTML = `<div class="page-head"><div><h1>商品来源与筛选</h1><p class="muted">历史未归档商品作为种子，有本店成交证据的优先；入库与选品独立运行。</p></div><span class="badge">${status.enabled ? "采集已启用" : "采集已暂停"}</span></div>
  <div class="panel" style="padding:20px;margin-bottom:20px"><strong>已积累 ${status.products} 个商品 · ${status.seeds} 个历史绑定 · ${status.current_seeds ?? 0} 个当前可用种子 · ${status.tasks} 个采集任务</strong><p class="muted tiny">支持毛子 ERP 榜单与 Safari / Chrome 店铺分页导入。店铺读取使用独立的暂停和续跑控制，页面商品需补齐 ERP 资料后才能通过筛选。统计均价为 RUB，不能直接用于最终利润计算。</p>
  <form id="source-library-form"><div class="form-grid">${[["price_min","最低统计均价 / RUB"],["price_max","最高统计均价 / RUB"],["weight_max_g","最大重量 / g"],["sales_min","最低 28 天榜单销量"],["max_age_hours","数据最长保存有效期 / 小时"]].map(([key,label])=>`<label>${label}<input type="number" min="0" step="any" name="${key}" value="${esc(sourceLibraryDraft[key] ?? (key==='max_age_hours'?168:''))}"></label>`).join("")}<label>类目 ID（逗号分隔）<input name="categories" value="${esc((sourceLibraryDraft.categories||[]).join(','))}"></label><label>商品来源 ERP 密钥<input type="password" name="erp_token" autocomplete="new-password" placeholder="留空保留原连接"></label></div><label class="checkline"><input type="checkbox" name="pure_fbs" ${sourceLibraryDraft.pure_fbs!==false?'checked':''}>仅纯 FBS</label><label class="checkline"><input type="checkbox" name="require_follow_allowed" ${sourceLibraryDraft.require_follow_allowed?'checked':''}>仅保留明确可跟卖的商品</label><label class="checkline"><input type="checkbox" name="same_seller_only" ${sourceLibraryDraft.same_seller_only?'checked':''}>只看种子同店扩品</label><div class="actions"><button class="primary" type="submit">保存筛选条件</button><button type="button" id="source-library-toggle">${status.enabled?'暂停采集':'启用采集'}</button>${me.role==='admin'?'<button type="button" id="source-library-import">导入本地历史上架记录</button>':''}<button type="button" id="source-library-export">导出本页初筛候选</button></div></form><p id="source-library-notice" class="muted tiny">缺失数据保留为空，资料不足不会默认合格。</p></div>
  <div class="actions"><select id="source-library-state"><option value="">全部判断</option>${Object.entries(labels).map(([key,label])=>`<option value="${key}" ${sourceLibraryState===key?'selected':''}>${label}</option>`).join('')}</select><button id="source-library-first">回到第一页</button><button id="source-library-next" ${!result.has_more?'disabled':''}>下一页</button></div>
  <div class="table-wrap"><table><thead><tr><th>商品 / 来源</th><th>统计均价 / RUB</th><th>重量 / g</th><th>28 天榜单销量</th><th>判断与缺失</th><th>采集 / 实时核查</th></tr></thead><tbody>${result.items.map((p,i)=>`<tr><td><strong>${esc(p.title)}</strong><div class="muted tiny">SKU ${esc(p.sku)} · 来源店铺 ${esc(p.seller_id||'未知')} · 类目 ${esc(p.category_id||'未知')}</div></td><td>${esc(p.average_price_rub??'未知')}</td><td>${esc(p.weight_g??'未知')}</td><td>${esc(p.sold_count_28d??'未知')}</td><td>${labels[p.assessment.state]}<div class="muted tiny">${p.assessment.missing.concat(p.assessment.failed).map(x=>fieldLabels[x]||esc(x)).join('、')}</div>${p.listing_review?`<div class="muted tiny">上架核查：${esc(p.listing_review.label)} · ${stamp(p.listing_review.observed_at)}</div>`:''}</td><td><small>${stamp(p.collected_at)}<br>${stamp(p.verified_at)}</small><br><button data-source-check="${i}">实时核查</button><button data-source-proof="${i}">查看证据</button>${p.assessment.state==='qualified'?`<button data-source-handoff="${i}">送入主软件待核查</button>`:''}</td></tr>`).join('')||'<tr><td colspan="6"><div class="empty">暂无商品。导入历史种子并启用采集后，这里会持续积累候选。</div></td></tr>'}</tbody></table></div>`;
  const browserPanel = document.createElement('div');
  browserPanel.className='panel';
  browserPanel.style='padding:20px;margin-bottom:20px';
  browserPanel.innerHTML=`<strong>店铺分页采集 · Safari / Chrome</strong><p class="muted tiny">打开当前分页地址，在 Safari 中保存“页面源码”，或在 Chrome 中保存“网页，仅 HTML”后导入。每次只提交一页，暂停和重启保留下一页地址。尚未读到明确末页的任务不会显示完成。</p>
    <div class="actions"><button data-browser-prepare>从来源种子建立任务</button>
    <select data-browser-seller><option value="">选择来源店铺（${storefrontTasks.length} 个）</option>${storefrontTasks.map(t=>`<option value="${esc(t.seller)}">${esc(t.seller)} · 第 ${t.page} 页 · ${esc(t.state)}</option>`).join('')}</select>
    <button data-browser-action="resume">继续</button><button data-browser-action="pause">暂停</button><button data-browser-action="retry">重试失败页</button>
    <input data-browser-file type="file" accept=".html,text/html"><button data-browser-import>导入当前页</button></div>
    <p data-browser-status class="muted tiny"></p><a data-browser-url target="_blank" rel="noopener noreferrer" hidden>打开当前分页地址</a>`;
  container.querySelector('.table-wrap').before(browserPanel);
  const browserSelect=browserPanel.querySelector('[data-browser-seller]');
  const browserNotice=browserPanel.querySelector('[data-browser-status]');
  const browserTask=()=>storefrontTasks.find(t=>t.seller===browserSelect.value);
  browserSelect.onchange=()=>{
    const t=browserTask();browserNotice.textContent=t?`第 ${t.page} 页 · ${t.state}${t.error?' · 上次失败：'+t.error:''}`:'请选择来源店铺';
    const a=browserPanel.querySelector('[data-browser-url]');a.hidden=!t?.next_url;if(t?.next_url)a.href=t.next_url;
  };
  const browserRun=async fn=>{try{await fn();}catch(e){browserNotice.textContent=e.message;}};
  browserPanel.querySelector('[data-browser-prepare]').onclick=()=>browserRun(async()=>{await api('/sources/storefront/prepare','POST',{});await render();});
  browserPanel.querySelectorAll('[data-browser-action]').forEach(b=>b.onclick=()=>browserRun(async()=>{
    const t=browserTask();if(!t)throw Error('请选择来源店铺');
    Object.assign(t,await api('/sources/storefront/'+t.seller+'/'+b.dataset.browserAction,'POST',{}));browserSelect.onchange();
  }));
  browserPanel.querySelector('[data-browser-import]').onclick=()=>browserRun(async()=>{
    const t=browserTask(),file=browserPanel.querySelector('[data-browser-file]').files[0];
    if(!t||!file)throw Error('请选择店铺和该页 HTML 文件');
    if(file.size>6000000)throw Error('页面文件超过 6 MB');
    const r=await api('/sources/storefront/'+t.seller+'/import','POST',{html:await file.text(),requested_url:t.next_url});
    browserNotice.textContent=r.state==='failed'?'导入失败，保留原分页：'+r.reason:`已处理，新增 ${r.added} 个商品；重复页面不会重复入库。`;
    Object.assign(t,(await api('/sources/storefront')).find(x=>x.seller===t.seller));
    const a=browserPanel.querySelector('[data-browser-url]');a.hidden=!t.next_url;if(t.next_url)a.href=t.next_url;
  });
  const form = container.querySelector('#source-library-form');
  const read = () => {
    const draft = {};
    for (const key of ['price_min','price_max','weight_max_g','sales_min','max_age_hours'])
      if (form.elements[key].value !== '') draft[key] = Number(form.elements[key].value);
    draft.categories = form.elements.categories.value.split(',').map(x=>x.trim()).filter(Boolean);
    draft.pure_fbs = form.elements.pure_fbs.checked;
    draft.require_follow_allowed = form.elements.require_follow_allowed.checked;
    draft.same_seller_only = form.elements.same_seller_only.checked;
    sourceLibraryDraft = draft;
    return draft;
  };
  form.oninput = read;
  const save = async enabled => {
    const payload = {enabled, filters:read()};
    if (form.elements.erp_token.value) payload.erp_token = form.elements.erp_token.value;
    await api('/sources/settings','PUT',payload);
    sourceLibraryCursor=0;
    document.activeElement?.blur();
    await render();
  };
  const run = async fn => {try {await fn();} catch(e) {container.querySelector('#source-library-notice').textContent=e.message;}};
  form.onsubmit=e=>{e.preventDefault();run(()=>save(status.enabled));};
  container.querySelector('#source-library-toggle').onclick=()=>run(()=>save(!status.enabled));
  const importer=container.querySelector('#source-library-import');
  if(importer) importer.onclick=()=>run(async()=>{importer.disabled=true;const r=await api('/sources/import-history','POST',{});container.querySelector('#source-library-notice').textContent=`导入 ${r.bindings} 个历史绑定，冲突 ${r.conflicts} 个${r.blocked?'；需要处理：'+r.blocked:''}`;importer.disabled=false;});
  container.querySelector('#source-library-state').onchange=e=>{sourceLibraryState=e.target.value;sourceLibraryCursor=0;render();};
  container.querySelector('#source-library-first').onclick=()=>{sourceLibraryCursor=0;render();};
  container.querySelector('#source-library-next').onclick=()=>{sourceLibraryCursor=result.cursor;render();};
  container.querySelector('#source-library-export').onclick=()=>run(async()=>{
    const data=await api('/sources/export?cursor='+sourceLibraryCursor);const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
    const a=document.createElement('a');a.href=url;a.download='flowhub-source-candidates.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  container.querySelectorAll('[data-source-check]').forEach(button=>button.onclick=()=>run(async()=>{
    button.disabled=true;const p=result.items[Number(button.dataset.sourceCheck)];
    const r=await api('/sources/'+encodeURIComponent(p.sku)+'/recheck?seller='+encodeURIComponent(p.seller_id||''),'POST',{});
    container.querySelector('#source-library-notice').textContent=`已回查 SKU ${p.sku}：配送模式 ${r.sales_schema||'未知'}；当前售价和具体规格仍需补充。`;
    button.disabled=false;
  }));
  container.querySelectorAll('[data-source-handoff]').forEach(button=>button.onclick=()=>run(async()=>{
    const p=result.items[Number(button.dataset.sourceHandoff)];
    const r=await api('/sources/'+encodeURIComponent(p.sku)+'/handoff?seller='+encodeURIComponent(p.seller_id||''),'POST',{});
    container.querySelector('#source-library-notice').textContent=r.created?'已进入主软件的需要处理列表，等待售价、规格和同款核查。':'主软件中已存在此商品，未重复创建。';
  }));
  container.querySelectorAll('[data-source-proof]').forEach(button=>button.onclick=()=>{
    const p=result.items[Number(button.dataset.sourceProof)];const dialog=document.createElement('dialog');const pre=document.createElement('pre');pre.style.cssText='max-width:70vw;max-height:65vh;overflow:auto;white-space:pre-wrap';pre.textContent=JSON.stringify(p,null,2);const close=document.createElement('button');close.textContent='关闭';close.onclick=()=>dialog.close();dialog.append(close,pre);document.body.append(dialog);dialog.onclose=()=>dialog.remove();dialog.showModal();
  });
}
