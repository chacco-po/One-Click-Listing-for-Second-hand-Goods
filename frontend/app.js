/* 闲置物品一键上架 · 前端逻辑（原生 JS，无构建步骤） */
'use strict';

const TOKEN_KEY = 'idlelister.token';

const state = {
  meta: null,
  token: localStorage.getItem(TOKEN_KEY) || null,
  user: null,          // {id, username}
  file: null,          // 原始 File 句柄（裁剪后重新处理用）
  imageId: null,
  originalUrl: null,
  resultUrl: null,
  report: null,
  recognition: null,
  categoryId: null,
  gradeId: null,
  answers: {},
  manualGrade: false,
  suggestion: null,
  price: null,
  copy: null,          // 文案生成结果：{titles, selling_points, description, engine, warnings}
  busy: false,
  cropBox: null,       // {x,y,w,h} 归一化
  cropping: false,
  cropStart: null,
  authMode: 'login',   // login | register
  shop: {
    items: [],
    total: 0,
    keyword: '',
    category: '',
    sort: 'latest',
    status: '',        // '' | on_sale | sold | mine
    loading: false,
    seq: 0,            // 请求序号：并发时只渲染最后一次结果
  },
  orderRole: 'buyer',  // buyer | seller
};

const $ = (sel) => document.querySelector(sel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------------- API ---------------- */
async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  let body = null;
  try { body = await res.json(); } catch (_) { /* noop */ }
  if (!res.ok) {
    const msg = (body && body.error && body.error.message) || `请求失败 (${res.status})`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return body;
}

async function apiJson(path, payload, method = 'POST') {
  return api(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/* ---------------- Toast ---------------- */
let toastTimer = null;
function toast(msg, type = '') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 2600);
}

/* ---------------- 视图切换 ---------------- */
function switchView(name) {
  $('#view-workbench').classList.toggle('hidden', name !== 'workbench');
  $('#view-shop').classList.toggle('hidden', name !== 'shop');
  $('#view-products').classList.toggle('hidden', name !== 'products');
  $('#view-drafts').classList.toggle('hidden', name !== 'drafts');
  $('#nav-workbench').classList.toggle('active', name === 'workbench');
  $('#nav-shop').classList.toggle('active', name === 'shop');
  $('#nav-products').classList.toggle('active', name === 'products');
  $('#nav-drafts').classList.toggle('active', name === 'drafts');
  if (name === 'drafts') loadDrafts();
  if (name === 'products') loadProducts();
  if (name === 'shop') { loadShop(); loadOrders(); }
}

/* ---------------- 初始化 ---------------- */
async function init() {
  try {
    state.meta = await api('/api/meta');
  } catch (e) {
    toast('无法连接后端：' + e.message, 'err');
    return;
  }
  $('#max-mb').textContent = state.meta.limits.max_upload_mb;
  renderCopyFields();

  // 规格下拉
  const specSel = $('#spec-select');
  state.meta.specs.forEach((s) => {
    const o = document.createElement('option');
    o.value = s.id; o.textContent = s.label;
    if (s.id === state.meta.default_spec) o.selected = true;
    specSel.appendChild(o);
  });

  // 商城类目筛选（复用 meta 的类目分组）
  const shopCat = $('#shop-category');
  state.meta.category_groups.forEach((g) => {
    const og = document.createElement('optgroup');
    og.label = g.group;
    g.items.forEach((it) => {
      const o = document.createElement('option');
      o.value = it.id; o.textContent = it.label;
      og.appendChild(o);
    });
    shopCat.appendChild(og);
  });

  bindEvents();
  if (state.token) await refreshUser();
  await loadDrafts();
  await loadProducts();
  await loadShop();
}

/* ---------------- 事件绑定 ---------------- */
function bindEvents() {
  $('#nav-workbench').addEventListener('click', () => switchView('workbench'));
  $('#nav-drafts').addEventListener('click', () => switchView('drafts'));
  $('#nav-products').addEventListener('click', () => switchView('products'));
  $('#nav-shop').addEventListener('click', () => switchView('shop'));
  $('#btn-auth').addEventListener('click', () => {
    if (state.user) signOut();
    else openAuth('login');
  });

  const dz = $('#dropzone');
  const fileInput = $('#file-input');
  dz.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', (e) => { if (e.target.files[0]) handleFile(e.target.files[0]); });
  dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('drag'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
  dz.addEventListener('drop', (e) => {
    e.preventDefault(); dz.classList.remove('drag');
    if (e.dataTransfer.files[0]) handleFile(e.target.files[0]);
  });

  // 排版参数实时预览（debounce）
  let layoutTimer = null;
  const onLayoutChange = () => {
    if (!state.imageId) return;
    clearTimeout(layoutTimer);
    layoutTimer = setTimeout(applyLayout, 250);
  };
  $('#spec-select').addEventListener('change', onLayoutChange);
  $('#fill-range').addEventListener('input', (e) => { $('#fill-value').textContent = e.target.value + '%'; onLayoutChange(); });
  $('#anchor-select').addEventListener('change', onLayoutChange);
  $('#shadow-chk').addEventListener('change', onLayoutChange);
  $('#btn-fine-edge').addEventListener('click', runFineEdge);

  $('#btn-save-draft').addEventListener('click', saveDraft);
  $('#btn-publish').addEventListener('click', publishProduct);
  $('#btn-reset').addEventListener('click', resetAll);
  $('#btn-generate-copy').addEventListener('click', generateCopy);

  // 裁剪
  $('#btn-crop').addEventListener('click', startCrop);
  $('#crop-cancel').addEventListener('click', cancelCrop);
  $('#crop-apply').addEventListener('click', applyCrop);

  // 成色手动调整
  $('#btn-manual-grade').addEventListener('click', () => {
    state.manualGrade = true;
    $('#manual-grade').classList.remove('hidden');
    const g = $('#manual-grade'); g.value = state.gradeId || '';
    updateGradeBadge(g.value);
    invalidateCopy();
    recomputePricing();
  });
  $('#manual-grade').addEventListener('change', (e) => {
    updateGradeBadge(e.target.value);
    invalidateCopy();
    recomputePricing();
  });

  // 弹窗关闭
  $('#draft-modal').addEventListener('click', (e) => { if (e.target.id === 'draft-modal') closeModal(); });
  $('#auth-modal').addEventListener('click', (e) => { if (e.target.id === 'auth-modal') closeAuth(); });

  // 商城筛选
  let shopTimer = null;
  $('#shop-keyword').addEventListener('input', (e) => {
    state.shop.keyword = e.target.value.trim();
    clearTimeout(shopTimer);
    shopTimer = setTimeout(loadShop, 300);
  });
  $('#shop-category').addEventListener('change', (e) => {
    state.shop.category = e.target.value;
    loadShop();
  });
  $('#shop-sort').addEventListener('change', (e) => {
    state.shop.sort = e.target.value;
    loadShop();
  });
  $('#shop-filters').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    $('#shop-filters').querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === chip));
    state.shop.status = chip.dataset.status;
    loadShop();
  });
  $('#order-tabs').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    $('#order-tabs').querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === chip));
    state.orderRole = chip.dataset.role;
    loadOrders();
  });

  // 登录 / 注册
  $('#auth-close').addEventListener('click', closeAuth);
  $('#auth-toggle').addEventListener('click', () => {
    state.authMode = state.authMode === 'login' ? 'register' : 'login';
    applyAuthMode();
  });
  $('#auth-submit').addEventListener('click', submitAuth);
  $('#auth-password').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitAuth(); });
}

/* ---------------- 认证 ---------------- */
function applyAuthMode() {
  const isLogin = state.authMode === 'login';
  $('#auth-title').textContent = isLogin ? '登录' : '注册';
  $('#auth-submit').textContent = isLogin ? '登录' : '注册并登录';
  $('#auth-toggle').textContent = isLogin ? '没有账号？去注册' : '已有账号？去登录';
  $('#auth-password').setAttribute('autocomplete', isLogin ? 'current-password' : 'new-password');
  $('#auth-error').classList.add('hidden');
}

function openAuth(mode) {
  state.authMode = mode;
  applyAuthMode();
  $('#auth-username').value = '';
  $('#auth-password').value = '';
  $('#auth-modal').classList.remove('hidden');
  $('#auth-username').focus();
}

function closeAuth() { $('#auth-modal').classList.add('hidden'); }

async function submitAuth() {
  const username = $('#auth-username').value.trim();
  const password = $('#auth-password').value;
  const errBox = $('#auth-error');
  errBox.classList.add('hidden');
  if (!username || !password) {
    errBox.textContent = '请输入用户名和密码';
    errBox.classList.remove('hidden');
    return;
  }
  const btn = $('#auth-submit');
  btn.disabled = true;
  try {
    const path = state.authMode === 'login' ? '/api/auth/login' : '/api/auth/register';
    const data = await apiJson(path, { username, password });
    state.token = data.token;
    state.user = data.user;
    localStorage.setItem(TOKEN_KEY, data.token);
    renderUser();
    closeAuth();
    toast(state.authMode === 'login' ? '登录成功' : '注册成功，已自动登录', 'ok');
    await loadProducts();
    await loadDrafts();
    await loadShop();
    await loadOrders();
  } catch (e) {
    errBox.textContent = e.message;
    errBox.classList.remove('hidden');
  } finally {
    btn.disabled = false;
  }
}

async function refreshUser() {
  try {
    const data = await api('/api/auth/me');
    if (data.user) { state.user = data.user; renderUser(); }
    else { clearSession(); }
  } catch (e) { clearSession(); }
}

function clearSession() {
  state.token = null; state.user = null;
  localStorage.removeItem(TOKEN_KEY);
  renderUser();
}

async function signOut() {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) { /* 忽略 */ }
  clearSession();
  toast('已退出登录');
  await loadProducts();
  await loadShop();
  await loadOrders();
}

function renderUser() {
  const btn = $('#btn-auth');
  if (state.user) {
    btn.textContent = `${state.user.username} · 退出`;
    btn.classList.add('logged');
  } else {
    btn.textContent = '登录';
    btn.classList.remove('logged');
  }
}

/* ---------------- 文件处理（步骤1+2+3） ---------------- */
function handleFile(file) {
  const okType = (file.type || '').startsWith('image/');
  const maxBytes = state.meta.limits.max_upload_mb * 1024 * 1024;
  if (!okType) { toast('请上传图片文件', 'err'); return; }
  if (file.size > maxBytes) { toast(`图片超过 ${state.meta.limits.max_upload_mb}MB 限制`, 'err'); return; }

  resetAll(false);
  state.file = file; // 保留原始文件句柄，供「框选主体」后重新处理

  const localUrl = URL.createObjectURL(file);
  setImage($('#img-original'), localUrl, $('#wrap-original'));

  state.busy = true;
  setSteps('segment', 'active');
  processUpload(file);
}

async function processUpload(file, crop = null) {
  try {
    const fd = new FormData();
    fd.append('file', file);
    if (crop) fd.append('crop', JSON.stringify(crop));

    const data = await api('/api/process', { method: 'POST', body: fd });

    setSteps('segment', 'done');
    setSteps('layout', 'active'); await sleep(280);
    setSteps('layout', 'done');
    setSteps('recognize', 'active'); await sleep(280);
    setSteps('recognize', 'done');

    state.imageId = data.image_id;
    state.originalUrl = null;
    state.resultUrl = data.result_url;
    state.report = data.report;
    state.recognition = data.recognition;

    if (data.original_url) setImage($('#img-original'), data.original_url, $('#wrap-original'));
    setImage($('#img-result'), data.result_url, $('#wrap-result'));

    renderRecognition();
    renderConditionQuestions();
    renderLayoutWarnings(data.report, data.matting_warning);

    ['#card-pipeline', '#card-recognition', '#card-condition', '#card-pricing',
     '#card-copywriting', '#workbench-actions'].forEach(
      (s) => $(s).classList.remove('hidden')
    );
    $('#crop-bar').classList.remove('hidden');
    toast('处理完成，可在下方调整排版与类目', 'ok');
  } catch (e) {
    setSteps('segment', 'pending');
    toast('处理失败：' + e.message, 'err');
  } finally {
    state.busy = false;
  }
}

function setSteps(name, status) {
  const el = document.querySelector(`.p-step[data-step="${name}"]`);
  if (el) el.dataset.status = status;
}

/* ---------------- 图片设置（维持宽高比，便于裁剪映射） ---------------- */
function setImage(img, url, wrap) {
  const isBlob = url.startsWith('blob:');
  img.onload = () => {
    if (wrap && img.naturalWidth) {
      wrap.style.aspectRatio = img.naturalWidth + ' / ' + img.naturalHeight;
    }
    if (isBlob) URL.revokeObjectURL(url);
  };
  img.src = url;
}

/* ---------------- 识别结果（步骤3） ---------------- */
function renderRecognition() {
  const body = $('#recognition-body');
  const rec = state.recognition;
  const groups = state.meta.category_groups;

  let opts = '<option value="">— 请选择类目 —</option>';
  groups.forEach((g) => {
    opts += `<optgroup label="${g.group}">`;
    g.items.forEach((it) => { opts += `<option value="${it.id}">${it.label}</option>`; });
    opts += '</optgroup>';
  });

  let html = '';
  if (rec && rec.available && rec.primary) {
    const p = rec.primary;
    html += `<div class="rec-main">
      <span class="rec-title">识别为：${p.label_zh}</span>
      <span class="conf-bar"><i style="width:${Math.round(p.confidence * 100)}%"></i></span>
      <span class="muted">置信度 ${Math.round(p.confidence * 100)}%</span>
    </div>`;
    if (rec.reason) html += `<div class="rec-notice">⚠️ ${rec.reason}</div>`;
    if (rec.detections && rec.detections.length > 1) {
      const others = rec.detections.filter((d) => !(d.label === p.label && d.confidence === p.confidence));
      if (others.length) {
        html += '<div class="muted" style="font-size:12px">其他检测：</div><div class="chips">' +
          others.map((d) => `<span class="chip">${d.label_zh} ${Math.round(d.confidence * 100)}%</span>`).join('') +
          '</div>';
      }
    }
    if (rec.category_id) { state.categoryId = rec.category_id; }
  } else {
    html += `<div class="rec-notice">⚠️ 未启用或未能自动识别物品（${rec ? (rec.reason || '无结果') : '识别服务不可用'}）。请从下方手动选择类目。</div>`;
  }
  html += `<label class="field-label">选择上架类目（用于定价参考）</label>
    <select id="category-select">${opts}</select>`;
  body.innerHTML = html;

  const sel = $('#category-select');
  if (state.categoryId) sel.value = state.categoryId;
  sel.addEventListener('change', () => {
    state.categoryId = sel.value || null;
    invalidateCopy();
    recomputePricing();
  });
}

/* ---------------- 成色自评（步骤4） ---------------- */
function renderConditionQuestions() {
  const body = $('#condition-body');
  body.innerHTML = state.meta.condition_questions.map((q) => {
    const opts = q.options.map((o) =>
      `<label class="opt"><input type="radio" name="${q.id}" value="${o.value}"><span>${o.label}</span></label>`
    ).join('');
    return `<div class="q"><div class="q-title">${q.title}<span class="q-hint">${q.hint || ''}</span></div><div class="opts">${opts}</div></div>`;
  }).join('');

  body.querySelectorAll('input[type="radio"]').forEach((r) => {
    r.addEventListener('change', async (e) => {
      state.answers[e.target.name] = e.target.value;
      invalidateCopy();
      if (state.manualGrade) return;
      try {
        const g = await apiJson('/api/condition', { answers: state.answers });
        state.gradeId = g.grade_id;
        updateGradeBadge(g.grade_id);
        if (g.missing && g.missing.length) toast('部分问题未作答，已按中间档估算');
        recomputePricing();
      } catch (err) { toast(err.message, 'err'); }
    });
  });
}

function updateGradeBadge(gradeId) {
  const badge = $('#grade-badge');
  const g = state.meta.grades.find((x) => x.id === gradeId);
  if (g) {
    badge.textContent = g.label;
    badge.className = 'grade-badge grade-' + gradeId;
  } else {
    badge.textContent = '待评估';
    badge.className = 'grade-badge grade-empty';
  }
}

/* ---------------- 参考定价（步骤5） ---------------- */
async function recomputePricing() {
  if (!state.categoryId) {
    $('#pricing-body').innerHTML = '<p class="muted">请先在上方选择类目后再定价。</p>';
    return;
  }
  const gradeId = state.gradeId || (state.manualGrade ? $('#manual-grade').value : null);
  if (!gradeId) {
    $('#pricing-body').innerHTML = '<p class="muted">请完成「成色自评」或手动调整成色。</p>';
    return;
  }
  try {
    const s = await apiJson('/api/pricing', { category_id: state.categoryId, grade_id: gradeId });
    state.suggestion = s;
    state.price = s.suggested_mid;
    $('#pricing-body').innerHTML = `
      <div class="price-suggest">
        <div class="price-range">参考区间 <b>¥${s.suggested_low} – ¥${s.suggested_high}</b></div>
        <div class="price-basis">${s.basis}</div>
      </div>
      <div class="price-input-row">
        <span class="cny">¥</span>
        <input type="number" id="price-input" min="1" step="1" value="${s.suggested_mid}" />
        <span class="muted">可手动修改为最终上架价</span>
      </div>`;
    $('#price-input').addEventListener('input', (e) => { state.price = Number(e.target.value) || null; });
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------- 标题与描述文案生成（步骤6） ---------------- */
function renderCopyFields() {
  const box = $('#copy-fields');
  const fields = (state.meta && state.meta.copywriting_fields) || [];
  box.innerHTML = fields.map((f) => `
    <label class="copy-field">
      <span class="copy-field-label">${f.label}</span>
      <input type="text" id="copy-${f.id}" placeholder="${f.placeholder}" maxlength="${f.max_length}" />
    </label>`).join('');
}

function collectExtras() {
  const extras = {};
  ((state.meta && state.meta.copywriting_fields) || []).forEach((f) => {
    const el = $('#copy-' + f.id);
    if (el && el.value.trim()) extras[f.id] = el.value.trim();
  });
  return extras;
}

async function generateCopy() {
  if (!state.categoryId) { toast('请先选择上架类目', 'err'); return; }
  const btn = $('#btn-generate-copy');
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    const gradeId = state.gradeId || (state.manualGrade ? $('#manual-grade').value : null);
    const priceInput = $('#price-input');
    const data = await apiJson('/api/copywriting', {
      category_id: state.categoryId,
      grade_id: gradeId,
      condition_answers: state.answers,
      extras: collectExtras(),
      image_id: state.imageId,
      price: priceInput ? Number(priceInput.value) : state.price,
    });
    state.copy = data;
    renderCopy(data);
    toast(data.engine === 'llm' ? '文案已生成（大模型润色）' : '文案已生成', 'ok');
  } catch (e) {
    toast('生成失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '生成文案';
  }
}

function renderCopy(data) {
  const body = $('#copy-body');

  // 标题候选
  let html = '<div class="copy-section"><div class="copy-section-head">'
    + '<b>标题候选</b><span class="muted">点击即可填入下方商品名称</span></div>'
    + '<div class="title-candidates">';
  (data.titles || []).forEach((t, i) => {
    html += `<button type="button" class="title-cand" data-title="${escapeHtml(t.text)}" title="${escapeHtml(t.template || '')}">
      <span class="tc-style">${escapeHtml(t.style || '方案' + (i + 1))}</span>
      <span class="tc-text">${escapeHtml(t.text)}</span>
    </button>`;
  });
  html += '</div></div>';

  // 卖点分组
  html += '<div class="copy-section"><div class="copy-section-head"><b>卖点描述</b>'
    + '<span class="muted">每条均可追溯来源，不臆造未提供的信息</span></div>';
  (data.selling_points || []).forEach((g) => {
    html += `<div class="sp-group"><div class="sp-group-name">${escapeHtml(g.group)}</div><ul class="sp-list">`;
    (g.items || []).forEach((it) => {
      const src = it.source ? `<span class="sp-src">${escapeHtml(it.source)}</span>` : '';
      const tip = it.detail ? ` title="${escapeHtml(it.detail)}"` : '';
      html += `<li${tip}>${escapeHtml(it.text)}${src}</li>`;
    });
    html += '</ul></div>';
  });
  html += '</div>';

  // 描述全文
  html += '<div class="copy-section"><div class="copy-section-head"><b>描述全文</b>'
    + '<span class="muted">可直接编辑后随商品一起发布</span></div>'
    + `<textarea id="copy-description" rows="8" maxlength="1200">${escapeHtml(data.description || '')}</textarea></div>`;

  // 依据与提示
  if ((data.basis || []).length) {
    html += '<div class="copy-basis"><b>生成依据：</b>' + escapeHtml(data.basis.join('；')) + '</div>';
  }
  if ((data.warnings || []).length) {
    html += '<div class="copy-warnings"><b>可补充：</b><ul>'
      + data.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join('') + '</ul></div>';
  }

  body.innerHTML = html;

  // 点击标题候选 → 填入商品名称
  body.querySelectorAll('.title-cand').forEach((b) => {
    b.addEventListener('click', () => {
      const input = $('#product-title');
      input.value = b.dataset.title;
      body.querySelectorAll('.title-cand').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      toast('已填入商品名称', 'ok');
    });
  });

  // 默认采纳第一个标题（仅在商品名称为空时，避免覆盖用户手输）
  const titleInput = $('#product-title');
  const first = (data.titles || [])[0];
  if (first && !titleInput.value.trim()) {
    titleInput.value = first.text;
    const firstBtn = body.querySelector('.title-cand');
    if (firstBtn) firstBtn.classList.add('active');
  }
}

/* 重新排版/改类目/改成色后，文案结果已过期 */
function invalidateCopy() {
  if (!state.copy) return;
  state.copy = null;
  const body = $('#copy-body');
  if (body) {
    body.innerHTML = '<p class="muted">条件已变化，请重新生成文案。</p>';
  }
}

/* ---------------- 重新排版 / 精细边缘 ---------------- */
async function applyLayout() {
  if (state.busy) return;
  try {
    const data = await apiJson('/api/layout', {
      image_id: state.imageId,
      spec: $('#spec-select').value,
      fill_ratio: Number($('#fill-range').value) / 100,
      anchor: $('#anchor-select').value,
      shadow: $('#shadow-chk').checked,
    });
    state.resultUrl = data.result_url;
    state.report = data.report;
    setImage($('#img-result'), data.result_url, $('#wrap-result'));
    renderLayoutWarnings(data.report, null);
  } catch (e) { toast(e.message, 'err'); }
}

async function runFineEdge() {
  if (state.busy || !state.imageId) return;
  state.busy = true;
  $('#btn-fine-edge').disabled = true;
  toast('正在用精细边缘重新抠图…');
  try {
    const data = await api(`/api/reprocess?image_id=${encodeURIComponent(state.imageId)}&fine_edge=true`, { method: 'POST' });
    state.resultUrl = data.result_url;
    state.report = data.report;
    setImage($('#img-result'), data.result_url, $('#wrap-result'));
    renderLayoutWarnings(data.report, null);
    toast('精细边缘处理完成', 'ok');
  } catch (e) { toast(e.message, 'err'); }
  finally { state.busy = false; $('#btn-fine-edge').disabled = false; }
}

function renderLayoutWarnings(report, mattingWarning) {
  const el = $('#layout-warnings');
  const warns = (report && report.warnings) || [];
  if (mattingWarning) warns.unshift('【许可提示】' + mattingWarning);
  if (!warns.length) { el.classList.add('hidden'); el.innerHTML = ''; return; }
  el.classList.remove('hidden');
  el.innerHTML = '<b>质检提示：</b><ul>' + warns.map((w) => `<li>${w}</li>`).join('') + '</ul>';
}

/* ---------------- 裁剪 ---------------- */
function startCrop() {
  state.cropping = true;
  $('#crop-overlay').classList.remove('hidden');
  $('#btn-crop').classList.add('hidden');
  $('#crop-apply').classList.remove('hidden');
  $('#crop-cancel').classList.remove('hidden');
  toast('在左图上拖动框选主体区域');
}
function cancelCrop() {
  state.cropping = false;
  $('#crop-overlay').classList.add('hidden');
  $('#btn-crop').classList.remove('hidden');
  $('#crop-apply').classList.add('hidden');
  $('#crop-cancel').classList.add('hidden');
  resetCropRect();
}
function resetCropRect() {
  const r = $('#crop-rect'); r.style.left = '0px'; r.style.top = '0px'; r.style.width = '0px'; r.style.height = '0px';
}
function applyCrop() {
  if (!state.cropBox) { toast('请先框选区域', 'err'); return; }
  const f = state.file;
  if (!f) { toast('原文件已失效，请重新上传', 'err'); cancelCrop(); return; }
  cancelCrop();
  state.busy = true;
  setSteps('segment', 'active');
  processUpload(f, state.cropBox);
}

(function cropDrag() {
  const overlay = $('#crop-overlay');
  overlay.addEventListener('pointerdown', (e) => {
    if (!state.cropping) return;
    const rect = overlay.getBoundingClientRect();
    state.cropStart = { x: e.clientX - rect.left, y: e.clientY - rect.top, rect };
    overlay.setPointerCapture(e.pointerId);
  });
  overlay.addEventListener('pointermove', (e) => {
    if (!state.cropStart) return;
    const r = state.cropStart;
    const cx = Math.min(Math.max(e.clientX - r.rect.left, 0), r.rect.width);
    const cy = Math.min(Math.max(e.clientY - r.rect.top, 0), r.rect.height);
    const x = Math.min(r.x, cx), y = Math.min(r.y, cy);
    const w = Math.abs(cx - r.x), h = Math.abs(cy - r.y);
    const el = $('#crop-rect');
    el.style.left = x + 'px'; el.style.top = y + 'px';
    el.style.width = w + 'px'; el.style.height = h + 'px';
  });
  overlay.addEventListener('pointerup', () => {
    if (!state.cropStart) return;
    const r = $('#crop-rect');
    const rect = state.cropStart.rect;
    const w = parseFloat(r.style.width), h = parseFloat(r.style.height);
    if (w < 8 || h < 8) { resetCropRect(); state.cropStart = null; return; }
    state.cropBox = {
      x: parseFloat(r.style.left) / rect.width,
      y: parseFloat(r.style.top) / rect.height,
      w: w / rect.width,
      h: h / rect.height,
    };
    state.cropStart = null;
  });
})();

/* ---------------- 草稿（步骤9） ---------------- */
async function saveDraft() {
  if (!state.imageId) { toast('请先上传并处理照片', 'err'); return; }
  if (!state.categoryId) { toast('请选择上架类目', 'err'); return; }
  if (!state.gradeId && !state.manualGrade) { toast('请完成成色自评', 'err'); return; }

  const priceInput = $('#price-input');
  const price = priceInput ? Number(priceInput.value) : (state.price || null);
  const title = $('#product-title').value.trim();

  state.busy = true;
  $('#btn-save-draft').disabled = true;
  try {
    const payload = {
      image_id: state.imageId,
      category_id: state.categoryId,
      condition_answers: state.answers,
      grade_id: state.manualGrade ? ($('#manual-grade').value || state.gradeId) : state.gradeId,
      price: price,
      title: title,
      notes: $('#draft-notes').value,
    };
    const copy = collectCopyPayload();
    if (copy) Object.assign(payload, copy);
    await apiJson('/api/drafts', payload);
    toast('草稿已保存', 'ok');
    await loadDrafts();
  } catch (e) { toast(e.message, 'err'); }
  finally { state.busy = false; $('#btn-save-draft').disabled = false; }
}

/* 收集当前文案（描述全文 + 结构化卖点），未生成时不带该字段 */
function collectCopyPayload() {
  const desc = $('#copy-description');
  if (!desc) return null;
  const points = [];
  const groups = (state.copy && state.copy.selling_points) || [];
  groups.forEach((g) => {
    (g.items || []).forEach((it) => {
      points.push({ group: g.group, text: it.text, source: it.source, detail: it.detail });
    });
  });
  return { description: desc.value.trim(), selling_points: points };
}

async function loadDrafts() {
  try {
    const { items } = await api('/api/drafts');
    $('#draft-count').textContent = items.length;
    renderDrafts(items);
  } catch (e) { toast(e.message, 'err'); }
}

function renderDrafts(items) {
  const grid = $('#drafts-grid');
  const empty = $('#drafts-empty');
  if (!items.length) { grid.innerHTML = ''; empty.classList.remove('hidden'); return; }
  empty.classList.add('hidden');
  grid.innerHTML = items.map((d) => `
    <div class="draft-card" data-id="${d.id}">
      <div class="card-img-wrap">
        <img src="${d.image_path}" alt="草稿" loading="lazy" />
        <button class="del-btn" data-act="del-draft" data-id="${d.id}" title="删除该草稿">删除</button>
      </div>
      <div class="draft-info">
        <b class="draft-title">${d.title ? escapeHtml(d.title) : '<span class="muted">未命名商品</span>'}</b>
        <span class="muted small">${d.category_label} · ${d.condition_grade_label}${d.status === 'published' ? ' · 已发布' : ''}</span>
        <span class="price">¥${d.price}</span>
        <time>${formatTime(d.created_at)}</time>
      </div>
    </div>`).join('');
  grid.querySelectorAll('.draft-card').forEach((c) => {
    c.addEventListener('click', (e) => {
      if (e.target.closest('[data-act="del-draft"]')) return;
      openDraftModal(c.dataset.id);
    });
  });
  grid.querySelectorAll('[data-act="del-draft"]').forEach((b) => {
    b.addEventListener('click', (e) => { e.stopPropagation(); askDelete(b, () => deleteDraft(b.dataset.id, b)); });
  });
}

/* 两步确认删除（不依赖原生 confirm，避免部分浏览器/内嵌预览拦截弹窗） */
function askDelete(btn, onConfirm) {
  if (btn.dataset.confirm === '1') {
    btn.dataset.confirm = '0';
    btn.textContent = '删除';
    btn.classList.remove('armed');
    onConfirm();
    return;
  }
  btn.dataset.confirm = '1';
  btn.textContent = '确认删除?';
  btn.classList.add('armed');
  setTimeout(() => {
    if (btn.isConnected && !btn.disabled) {
      btn.dataset.confirm = '0';
      btn.textContent = '删除';
      btn.classList.remove('armed');
    }
  }, 4000);
}

async function deleteDraft(id, btn) {
  markDeleting(btn);
  try {
    await api('/api/drafts/' + id, { method: 'DELETE' });
    toast('草稿已删除', 'ok');
    await loadDrafts();
  } catch (e) {
    toast('删除失败：' + e.message, 'err');
    resetDeleting(btn);
  }
}

/* 删除期间给出即时反馈（文件清理可能耗时 1-3 秒） */
function markDeleting(btn) {
  if (!btn) return;
  btn.dataset.busy = '1';
  btn.disabled = true;
  btn.textContent = '删除中…';
  btn.classList.add('busy');
}
function resetDeleting(btn) {
  if (!btn || !btn.isConnected) return;
  btn.dataset.busy = '0';
  btn.disabled = false;
  btn.textContent = '删除';
  btn.classList.remove('busy', 'armed');
  btn.dataset.confirm = '0';
}

function formatTime(iso) {
  try { return new Date(iso).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }); }
  catch (_) { return iso; }
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* 结构化卖点的只读展示（草稿/商品详情弹窗复用） */
function renderSellingPoints(points) {
  if (!points || !points.length) return '';
  const groups = [];
  points.forEach((p) => {
    let g = groups.find((x) => x.name === p.group);
    if (!g) { g = { name: p.group, items: [] }; groups.push(g); }
    g.items.push(p);
  });
  const body = groups.map((g) =>
    `<div class="sp-group"><div class="sp-group-name">${escapeHtml(g.name)}</div><ul class="sp-list">`
    + g.items.map((it) =>
        `<li>${escapeHtml(it.text)}${it.source ? `<span class="sp-src">${escapeHtml(it.source)}</span>` : ''}</li>`
      ).join('')
    + '</ul></div>'
  ).join('');
  return `<div class="kv kv-block"><span class="k">卖点</span><div class="sp-static">${body}</div></div>`;
}

async function openDraftModal(id) {
  try {
    const d = await api('/api/drafts/' + id);
    const card = $('#draft-modal-card');
    card.innerHTML = `
      <div class="modal-head"><h3>草稿详情</h3><button class="modal-close" id="modal-close">×</button></div>
      <div class="modal-body">
        <img src="${d.image_path}" alt="白底图" />
        <div class="kv"><span class="k">商品名称</span><span>${d.title ? escapeHtml(d.title) : '未填写'}</span></div>
        <div class="kv"><span class="k">类目</span><span>${d.category_label}</span></div>
        <div class="kv"><span class="k">识别</span><span>${d.recognized_label ? d.recognized_label + '（' + Math.round((d.recognized_confidence || 0) * 100) + '%）' : '—'}</span></div>
        <div class="kv"><span class="k">成色</span><span>${d.condition_grade_label}</span></div>
        <div class="kv"><span class="k">参考区间</span><span>¥${d.price_low} – ¥${d.price_high}</span></div>
        <div class="kv"><span class="k">备注</span><span>${escapeHtml(d.notes) || '—'}</span></div>
        ${d.description ? `<div class="kv kv-block"><span class="k">描述</span><pre class="kv-desc">${escapeHtml(d.description)}</pre></div>` : ''}
        ${renderSellingPoints(d.selling_points)}
        <label class="field-label">商品名称</label>
        <input type="text" id="modal-title" maxlength="60" value="${escapeHtml(d.title)}" placeholder="填写商品名称" />
        <label class="field-label">修改上架价格</label>
        <div class="price-input-row"><span class="cny">¥</span>
          <input type="number" id="modal-price" min="1" value="${d.price}" /></div>
      </div>
      <div class="modal-foot">
        <button class="btn btn-primary" id="modal-save">保存修改</button>
        <button class="btn btn-publish" id="modal-publish">发布上架</button>
        <button class="btn btn-danger" id="modal-delete">删除草稿</button>
      </div>`;
    $('#draft-modal').classList.remove('hidden');
    $('#modal-close').addEventListener('click', closeModal);
    $('#modal-save').addEventListener('click', async () => {
      try {
        await apiJson('/api/drafts/' + id, {
          price: Number($('#modal-price').value),
          title: $('#modal-title').value.trim(),
        }, 'PUT');
        toast('已保存修改', 'ok'); closeModal(); await loadDrafts();
      } catch (e) { toast(e.message, 'err'); }
    });
    $('#modal-publish').addEventListener('click', async () => {
      const title = $('#modal-title').value.trim();
      const copy = {
        description: d.description || '',
        selling_points: d.selling_points || [],
      };
      await publishFromDraft(id, title, Number($('#modal-price').value), copy);
    });
    const delBtn = $('#modal-delete');
    delBtn.addEventListener('click', () => {
      askDelete(delBtn, async () => {
        markDeleting(delBtn);
        try {
          await api('/api/drafts/' + id, { method: 'DELETE' });
          toast('草稿已删除', 'ok'); closeModal(); await loadDrafts();
        } catch (e) { toast('删除失败：' + e.message, 'err'); resetDeleting(delBtn); }
      });
    });
  } catch (e) { toast(e.message, 'err'); }
}

function closeModal() { $('#draft-modal').classList.add('hidden'); }

/* ---------------- 发布上架 ---------------- */
async function publishProduct() {
  if (!state.user) { toast('请先登录后再发布', 'err'); openAuth('login'); return; }
  if (!state.imageId) { toast('请先上传并处理照片', 'err'); return; }
  if (!state.categoryId) { toast('请选择上架类目', 'err'); return; }
  const title = $('#product-title').value.trim();
  if (!title) { toast('请填写商品名称', 'err'); $('#product-title').focus(); return; }

  // 先存草稿再发布，保证草稿与商品数据一致
  const priceInput = $('#price-input');
  const price = priceInput ? Number(priceInput.value) : (state.price || null);
  try {
    const payload = {
      image_id: state.imageId,
      category_id: state.categoryId,
      condition_answers: state.answers,
      grade_id: state.manualGrade ? ($('#manual-grade').value || state.gradeId) : state.gradeId,
      price: price,
      title: title,
      notes: $('#draft-notes').value,
    };
    const copy = collectCopyPayload();
    if (copy) Object.assign(payload, copy);
    const draft = await apiJson('/api/drafts', payload);
    await publishFromDraft(draft.id, title, price, copy);
  } catch (e) { toast('发布失败：' + e.message, 'err'); }
}

async function publishFromDraft(draftId, title, price, copy) {
  if (!state.user) { toast('请先登录后再发布', 'err'); openAuth('login'); return; }
  if (!title) { toast('请填写商品名称', 'err'); return; }
  try {
    const body = { draft_id: draftId, title, price };
    if (copy) Object.assign(body, copy);
    await apiJson('/api/products', body);
    toast('已发布上架', 'ok');
    closeModal();
    await loadDrafts();
    await loadProducts();
    await loadShop();
    switchView('products');
  } catch (e) { toast('发布失败：' + e.message, 'err'); }
}

/* ---------------- 商城 ---------------- */
async function loadShop() {
  const grid = $('#shop-grid');
  const empty = $('#shop-empty');
  // 请求序号：并发请求时只渲染最后一次的结果，避免旧响应覆盖新筛选条件。
  // 注意：不能用「加载中就 return」的写法 —— 那会把用户在飞行期间的筛选变更直接丢掉。
  const seq = ++state.shop.seq;
  state.shop.loading = true;
  try {
    const params = new URLSearchParams();
    if (state.shop.keyword) params.set('keyword', state.shop.keyword);
    if (state.shop.category) params.set('category_id', state.shop.category);
    if (state.shop.sort) params.set('sort', state.shop.sort);
    // 「我买到的」是前端语义：后端用 only_bought 参数
    if (state.shop.status === 'mine') {
      params.set('only_bought', 'true');
    } else if (state.shop.status) {
      params.set('status', state.shop.status);
    }
    const data = await api('/api/shop?' + params.toString());
    if (seq !== state.shop.seq) return;   // 已有更新的请求，丢弃本次结果
    state.shop.items = data.items || [];
    state.shop.total = data.total || 0;
    renderShop();
  } catch (e) {
    if (seq !== state.shop.seq) return;
    grid.innerHTML = '';
    empty.classList.remove('hidden');
    empty.innerHTML = '商城加载失败：' + escapeHtml(e.message);
  } finally {
    if (seq === state.shop.seq) state.shop.loading = false;
  }
}

function soldStamp(size) {
  return `<div class="sold-stamp${size === 'small' ? ' small' : ''}"><span>已售出</span></div>`;
}

function renderShop() {
  const grid = $('#shop-grid');
  const empty = $('#shop-empty');
  const items = state.shop.items;
  $('#shop-count').textContent = state.shop.total;
  if (!items.length) {
    grid.innerHTML = '';
    empty.classList.remove('hidden');
    empty.innerHTML = state.shop.status === 'mine'
      ? '你还没有买过任何商品。'
      : '商城里还没有符合条件的商品。<br />在「工作台」发布一件闲置物品，它就会出现在这里。';
    return;
  }
  empty.classList.add('hidden');
  grid.innerHTML = items.map((p) => {
    const sold = p.status === 'sold';
    const mine = p.is_own;
    let action;
    if (sold) {
      action = `<button class="btn btn-ghost btn-sm" disabled>已售出</button>`;
    } else if (mine) {
      action = `<button class="btn btn-ghost btn-sm" disabled>我发布的</button>`;
    } else {
      action = `<button class="btn btn-primary btn-sm" data-act="buy" data-id="${p.id}">立即购买</button>`;
    }
    return `
      <div class="shop-card" data-id="${p.id}">
        <div class="card-img-wrap">
          <img src="${p.image_path}" alt="${escapeHtml(p.title)}" loading="lazy" />
          ${sold ? soldStamp() : ''}
          ${p.bought_by_me ? '<span class="order-badge" style="position:absolute;left:8px;top:8px;z-index:4">我买到的</span>' : ''}
        </div>
        <div class="shop-info">
          <b class="shop-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title)}</b>
          <span class="shop-meta">${escapeHtml(p.category_label)} · ${escapeHtml(p.condition_grade_label)}</span>
          <span class="shop-price">¥${p.price}</span>
          <span class="shop-seller">卖家：${escapeHtml(p.seller_name || '匿名用户')}</span>
        </div>
        <div class="shop-actions">
          <button class="btn btn-ghost btn-sm" data-act="detail" data-id="${p.id}">查看详情</button>
          ${action}
        </div>
      </div>`;
  }).join('');

  grid.querySelectorAll('[data-act="detail"]').forEach((b) => {
    b.addEventListener('click', (e) => { e.stopPropagation(); openShopModal(b.dataset.id); });
  });
  grid.querySelectorAll('[data-act="buy"]').forEach((b) => {
    b.addEventListener('click', (e) => { e.stopPropagation(); buyItem(b.dataset.id, b); });
  });
  grid.querySelectorAll('.shop-card').forEach((c) => {
    c.addEventListener('click', () => openShopModal(c.dataset.id));
  });
}

async function buyItem(productId, btn) {
  if (!state.user) { toast('请先登录后再购买', 'err'); openAuth('login'); return; }
  if (btn) { btn.disabled = true; btn.textContent = '购买中…'; }
  try {
    const data = await apiJson('/api/shop/' + productId + '/buy', {});
    toast(data.message || '购买成功', 'ok');
    await loadShop();
    await loadOrders();
    await loadProducts();
    return true;
  } catch (e) {
    toast('购买失败：' + e.message, 'err');
    await loadShop();
    return false;
  }
}

async function openShopModal(id) {
  let p;
  try {
    p = await api('/api/shop/' + id);
  } catch (e) { toast(e.message, 'err'); return; }
  const sold = p.status === 'sold';
  const mine = p.is_own;
  const card = $('#draft-modal-card');
  card.innerHTML = `
    <div class="modal-head"><h3>商品详情</h3><button class="modal-close" id="modal-close">×</button></div>
    <div class="modal-body">
      ${sold ? '<div class="sold-banner">已 售 出</div>' : ''}
      <img src="${p.image_path}" alt="${escapeHtml(p.title)}" style="width:100%;border-radius:10px;border:1px solid var(--border)" />
      <div class="kv"><span class="k">商品名称</span><span>${escapeHtml(p.title)}</span></div>
      <div class="kv"><span class="k">售价</span><span class="price">¥${p.price}</span></div>
      <div class="kv"><span class="k">类目 / 成色</span><span>${escapeHtml(p.category_label)} · ${escapeHtml(p.condition_grade_label)}</span></div>
      <div class="kv"><span class="k">卖家</span><span>${escapeHtml(p.seller_name || '匿名用户')}</span></div>
      ${p.bought_by_me ? '<div class="kv"><span class="k">我的购买</span><span>✔ 这件商品由你买下</span></div>' : ''}
      ${p.description ? `<div class="kv"><span class="k">描述</span><span class="kv-desc">${escapeHtml(p.description)}</span></div>` : ''}
      ${p.notes ? `<div class="kv"><span class="k">备注</span><span>${escapeHtml(p.notes)}</span></div>` : ''}
      <div class="modal-foot">
        ${sold
          ? '<button class="btn btn-ghost" disabled>该商品已售出</button>'
          : mine
            ? '<button class="btn btn-ghost" disabled>这是你发布的商品</button>'
            : '<button class="btn btn-primary" id="modal-buy">立即购买</button>'}
      </div>
    </div>`;
  $('#draft-modal').classList.remove('hidden');
  const close = card.querySelector('#modal-close');
  if (close) close.addEventListener('click', closeModal);
  const buy = card.querySelector('#modal-buy');
  if (buy) {
    buy.addEventListener('click', async () => {
      const ok = await buyItem(productId, buy);
      if (ok) { closeModal(); openShopModal(productId); }
    });
  }
}

async function loadOrders() {
  const list = $('#orders-list');
  const empty = $('#orders-empty');
  if (!state.user) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
    empty.innerHTML = '登录后即可查看你的购买与卖出记录。<br />' +
      '<button class="btn btn-primary" id="orders-login" style="margin-top:12px">去登录</button>';
    const btn = $('#orders-login');
    if (btn) btn.addEventListener('click', () => openAuth('login'));
    return;
  }
  try {
    const data = await api('/api/orders?role=' + state.orderRole);
    const items = data.items || [];
    if (!items.length) {
      list.innerHTML = '';
      empty.classList.remove('hidden');
      empty.innerHTML = state.orderRole === 'buyer' ? '你还没有买过任何商品。' : '你的商品还没有卖出记录。';
      return;
    }
    empty.classList.add('hidden');
    list.innerHTML = items.map((o) => `
      <div class="order-item" data-product="${o.product_id}">
        ${o.image_path ? `<img src="${o.image_path}" alt="${escapeHtml(o.title || '')}" />` : '<div style="width:72px;height:72px;border-radius:8px;background:#f3f4f6"></div>'}
        <div class="order-main">
          <b>${escapeHtml(o.title || '（商品已删除）')}</b>
          <span class="muted">${state.orderRole === 'buyer'
            ? '卖家：' + escapeHtml(o.seller_name || '匿名用户')
            : '买家：' + escapeHtml(o.buyer_name || '匿名用户')}</span>
          <span class="muted">${escapeHtml(o.category_label || '')} · ${formatTime(o.created_at)}</span>
        </div>
        <div class="order-price">¥${o.price}</div>
      </div>`).join('');
    list.querySelectorAll('.order-item').forEach((el) => {
      el.addEventListener('click', () => openShopModal(el.dataset.product));
    });
  } catch (e) {
    if (e.status === 401) { clearSession(); loadOrders(); }
    else toast(e.message, 'err');
  }
}

/* ---------------- 我的商品 ---------------- */
async function loadProducts() {
  const grid = $('#products-grid');
  const empty = $('#products-empty');
  if (!state.user) {
    grid.innerHTML = '';
    empty.classList.remove('hidden');
    empty.innerHTML = '登录后即可查看你已上架的商品。<br /><button class="btn btn-primary" id="products-login" style="margin-top:12px">去登录</button>';
    $('#product-count').textContent = '0';
    const btn = $('#products-login');
    if (btn) btn.addEventListener('click', () => openAuth('login'));
    return;
  }
  try {
    const { items } = await api('/api/products');
    $('#product-count').textContent = items.length;
    if (!items.length) { grid.innerHTML = ''; empty.classList.remove('hidden'); return; }
    empty.classList.add('hidden');
    grid.innerHTML = items.map((p) => `
      <div class="product-card" data-id="${p.id}">
        <div class="card-img-wrap">
          <img src="${p.image_path}" alt="${escapeHtml(p.title)}" loading="lazy" />
          ${p.status === 'sold' ? soldStamp('small') : ''}
          <span class="status-tag status-${p.status}">${p.status_label}</span>
          <button class="del-btn" data-act="del-product" data-id="${p.id}">删除</button>
        </div>
        <div class="draft-info">
          <b class="draft-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title)}</b>
          <span class="muted small">${p.category_label} · ${p.condition_grade_label}</span>
          <span class="price">¥${p.price}</span>
          <time>${formatTime(p.created_at)}</time>
        </div>
      </div>`).join('');
    grid.querySelectorAll('.product-card').forEach((c) => {
      c.addEventListener('click', (e) => {
        if (e.target.closest('[data-act="del-product"]')) return;
        openProductModal(c.dataset.id);
      });
    });
    grid.querySelectorAll('[data-act="del-product"]').forEach((b) => {
      b.addEventListener('click', (e) => { e.stopPropagation(); askDelete(b, () => deleteProduct(b.dataset.id, b)); });
    });
  } catch (e) {
    if (e.status === 401) { clearSession(); loadProducts(); }
    else toast(e.message, 'err');
  }
}

async function deleteProduct(id, btn) {
  markDeleting(btn);
  try {
    await api('/api/products/' + id, { method: 'DELETE' });
    toast('商品已删除', 'ok');
    await loadProducts();
    await loadShop();
    await loadOrders();
  } catch (e) {
    toast('删除失败：' + e.message, 'err');
    resetDeleting(btn);
  }
}

async function openProductModal(id) {
  try {
    const p = await api('/api/products/' + id);
    const card = $('#draft-modal-card');
    card.innerHTML = `
      <div class="modal-head"><h3>商品详情</h3><button class="modal-close" id="modal-close">×</button></div>
      <div class="modal-body">
        <img src="${p.image_path}" alt="${escapeHtml(p.title)}" />
        <label class="field-label">商品名称</label>
        <input type="text" id="p-title" maxlength="60" value="${escapeHtml(p.title)}" />
        <label class="field-label">上架价格</label>
        <div class="price-input-row"><span class="cny">¥</span>
          <input type="number" id="p-price" min="1" value="${p.price}" /></div>
        <label class="field-label">商品状态</label>
        <select id="p-status">
          <option value="on_sale"${p.status === 'on_sale' ? ' selected' : ''}>在售</option>
          <option value="sold"${p.status === 'sold' ? ' selected' : ''}>已售出</option>
          <option value="off_shelf"${p.status === 'off_shelf' ? ' selected' : ''}>已下架</option>
        </select>
        <div class="kv"><span class="k">类目 / 成色</span><span>${p.category_label} · ${p.condition_grade_label}</span></div>
        <div class="kv"><span class="k">备注</span><span>${escapeHtml(p.notes) || '—'}</span></div>
        ${p.description ? `<div class="kv kv-block"><span class="k">描述</span><pre class="kv-desc">${escapeHtml(p.description)}</pre></div>` : ''}
        ${renderSellingPoints(p.selling_points)}
      </div>
      <div class="modal-foot">
        <button class="btn btn-primary" id="p-save">保存修改</button>
        <button class="btn btn-danger" id="p-delete">删除商品</button>
      </div>`;
    $('#draft-modal').classList.remove('hidden');
    $('#modal-close').addEventListener('click', closeModal);
    $('#p-save').addEventListener('click', async () => {
      try {
        await apiJson('/api/products/' + id, {
          title: $('#p-title').value.trim(),
          price: Number($('#p-price').value),
          status: $('#p-status').value,
        }, 'PUT');
        toast('已保存修改', 'ok'); closeModal(); await loadProducts();
      } catch (e) { toast(e.message, 'err'); }
    });
    const delBtn = $('#p-delete');
    delBtn.addEventListener('click', () => {
      askDelete(delBtn, async () => {
        markDeleting(delBtn);
        try {
          await api('/api/products/' + id, { method: 'DELETE' });
          toast('商品已删除', 'ok'); closeModal(); await loadProducts();
        } catch (e) { toast('删除失败：' + e.message, 'err'); resetDeleting(delBtn); }
      });
    });
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------- 重置 ---------------- */
function resetAll(showToast = true) {
  state.imageId = null; state.resultUrl = null; state.report = null;
  state.recognition = null; state.categoryId = null; state.gradeId = null;
  state.answers = {}; state.manualGrade = false; state.suggestion = null; state.price = null;
  state.copy = null;
  state.cropBox = null; state.file = null; cancelCropSoft();
  $('#file-input').value = '';
  ['#card-pipeline', '#card-recognition', '#card-condition', '#card-pricing',
   '#card-copywriting', '#workbench-actions'].forEach(
    (s) => $(s).classList.add('hidden')
  );
  $('#crop-bar').classList.add('hidden');
  $('#img-original').src = ''; $('#img-result').src = '';
  $('#recognition-body').innerHTML = ''; $('#condition-body').innerHTML = ''; $('#pricing-body').innerHTML = '';
  $('#copy-body').innerHTML = '<p class="muted">点击「生成文案」，系统将结合识别结果与成色自评产出标题候选与卖点描述。</p>';
  $('#grade-badge').textContent = '待评估'; $('#grade-badge').className = 'grade-badge grade-empty';
  $('#draft-notes').value = ''; $('#product-title').value = '';
  $('#layout-warnings').classList.add('hidden');
  // 清空文案补充信息输入
  ((state.meta && state.meta.copywriting_fields) || []).forEach((f) => {
    const el = $('#copy-' + f.id);
    if (el) el.value = '';
  });
  if (showToast) toast('已重置，可重新上传');
}
function cancelCropSoft() {
  state.cropping = false;
  ['#crop-overlay', '#crop-apply', '#crop-cancel'].forEach((s) => $(s).classList.add('hidden'));
  $('#btn-crop').classList.remove('hidden');
}

/* ---------------- 启动 ---------------- */
document.addEventListener('DOMContentLoaded', init);
