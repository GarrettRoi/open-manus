/* Commonplace: a same-origin, dependency-free household ledger client. */
(() => {
  'use strict';
  const $ = (s, root = document) => root.querySelector(s);
  const app = $('#app'), overlay = $('#overlay');
  const state = { session: null, people: [], categories: [], view: 'overview', filters: {}, month: 'all', page: 0, draftsPage: 0, summary: null, purchases: null, drafts: null, tokens: null, models: null, selectedModel: '', modelQuery: '', modelError: '', loading: false, error: '', busy: false };
  const PAGE = 20;
  Object.assign(state,{batch:null,localFiles:[],batchError:'',batchBusy:false,audit:null,auditOffset:0,auditPage:0,auditPath:'/api/audit'});
  const writeKeys = new Map();
  const actorLabel = actor => actor ? `${actor.display || actor.id || 'Unknown'} (${actor.kind || 'unknown'})` : 'Not recorded';
  const timestamp = value => value ? new Date(value).toLocaleString() : 'Not recorded';
  function versionHeaders(record) { if (!Number.isInteger(record?.version)) throw Error('This record has no version. Reload it before making changes.'); return {'If-Match':String(record.version)}; }
  function provenance(record, kind) {
    return `<dl class="provenance">${[['Uploaded by',record.uploaded_by],['Created by',record.created_by],['Last edited by',record.last_edited_by]].map(([label,actor])=>`<div><dt>${label}</dt><dd>${esc(actorLabel(actor))}</dd></div>`).join('')}<div class="actions"><span class="fine muted">Purchaser is separate from these server-recorded actors. Version ${esc(record.version ?? 'not recorded')}.</span><button type="button" class="btn sm" data-action="record-audit" data-kind="${kind}" data-id="${esc(record.id)}">View change history</button></div></dl>`;
  }
  const icons = {
    overview:'<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    purchases:'<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    drafts:'<path d="M4 4h12l4 4v12H4zM15 4v5h5M8 14h8M8 17h5"/>',
    scan:'<path d="M4 9V4h5m6 0h5v5M4 15v5h5m6 0h5v-5M7 12h10"/>',
    people:'<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6m1 3a5 5 0 0 1 4 5v1"/>',
    settings:'<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.5-2.3 1A8 8 0 0 0 15 6l-.3-2.5H9.3L9 6a8 8 0 0 0-1.6 1l-2.3-1-2 3.5 2 1.5a7 7 0 0 0 0 2l-2 1.5 2 3.5 2.3-1A8 8 0 0 0 9 18l.3 2.5h5.4L15 18a8 8 0 0 0 1.6-1l2.3 1 2-3.5-2-1.5a7 7 0 0 0 .1-1z"/>',
    plus:'<path d="M12 5v14M5 12h14"/>', arrow:'<path d="m5 12 14 0m-7-7 7 7-7 7"/>', close:'<path d="M5 5l14 14M19 5 5 19"/>', trash:'<path d="M4 7h16M9 7V4h6v3m-9 0 1 14h10l1-14M10 11v6m4-6v6"/>', download:'<path d="M12 3v12m-4-4 4 4 4-4M4 17v4h16v-4"/>'
  };
  const icon = (key) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[key] || icons.arrow}</svg>`;
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = (c, currency = state.session?.currency || 'USD') => c == null ? '—' : new Intl.NumberFormat(undefined, {style:'currency',currency:currency || 'USD'}).format(c / 100);
  const decimal = c => c == null ? '' : `${c < 0 ? '-' : ''}${(Math.abs(c) / 100).toFixed(2)}`;
  function cents(value, label, required = false) {
    const text = String(value ?? '').trim();
    if (!text) { if (required) throw Error(`${label} is required.`); return null; }
    if (!/^-?\d+(\.\d{1,2})?$/.test(text)) throw Error(`${label} must be an amount with at most two decimal places.`);
    const negative = text.startsWith('-');
    const [whole, part = ''] = text.replace('-', '').split('.');
    const n = Number(whole) * 100 + Number(part.padEnd(2, '0'));
    if (!Number.isSafeInteger(n)) throw Error(`${label} is too large.`);
    return negative ? -n : n;
  }
  const today = () => { const d = new Date(), zone = state.session?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone; try { const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(d).map(p=>[p.type,p.value])); return `${parts.year}-${parts.month}-${parts.day}`; } catch { return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; } };
  const dateLabel = d => d ? new Date(`${d}T12:00:00`).toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'}) : 'Date needed';
  const personName = id => state.people.find(p => p.id === id)?.name || 'Unassigned';
  function notify(text, isError = false) { const node = $('#toast'); node.innerHTML = `<div class="toast ${isError?'error':''}">${esc(text)}</div>`; clearTimeout(notify.timer); notify.timer = setTimeout(() => node.innerHTML = '', 4400); }
  async function api(path, options = {}) {
    const headers = {...(options.headers || {})};
    const mutation=options.method && options.method!=='GET' && !['/api/login','/api/logout'].includes(path);
    const fingerprint=mutation && !(options.body instanceof FormData) ? `${options.method}:${path}:${headers['If-Match']||''}:${options.body||''}` : '';
    if (mutation && !headers['Idempotency-Key']) {
      if (fingerprint && !writeKeys.has(fingerprint)) writeKeys.set(fingerprint,crypto.randomUUID());
      headers['Idempotency-Key']=fingerprint?writeKeys.get(fingerprint):crypto.randomUUID();
    }
    if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
    if (options.method && options.method !== 'GET' && path !== '/api/login' && state.session?.csrf) headers['X-CSRF-Token'] = state.session.csrf;
    let response;
    try { response = await fetch(path, {...options, headers, credentials:'same-origin'}); }
    catch { throw Error('Could not connect to the household service. Check your connection and try again.'); }
    if (response.status === 401 && path !== '/api/session' && path !== '/api/login') { state.session = null; renderLogin(); throw Error('Your session ended. Please sign in again.'); }
    if (!response.ok) {
      let body = {}; try { body = await response.json(); } catch {}
      const error = new Error(body.detail || `Request failed (${response.status}).`);
      error.status = response.status; error.duplicate = body.duplicate;
      throw error;
    }
    const result=response.status === 204 ? {} : await response.json();
    if (fingerprint) writeKeys.delete(fingerprint);
    return result;
  }
  const query = obj => { const p = new URLSearchParams(); Object.entries(obj).forEach(([k,v]) => { if (v !== '' && v != null) p.set(k,v); }); return p.toString(); };
  const filtersQuery = () => query(state.filters);
  const withFilters = (path, extra = {}) => `${path}?${query({...state.filters,...extra})}`;
  const loading = () => `<div class="panel panel-pad" role="status" aria-label="Loading"><div class="skeleton" style="width:30%"></div><div class="skeleton"></div><div class="skeleton" style="width:80%"></div><div class="skeleton"></div></div>`;
  const failure = () => `<div class="notice warning" role="alert">${esc(state.error)} <button class="link-btn" data-action="retry">Try again</button></div>`;
  const empty = (heading, text, action = '') => `<div class="empty"><div class="empty-mark" aria-hidden="true">∅</div><h3>${esc(heading)}</h3><p>${esc(text)}</p>${action}</div>`;
  const nav = [
    ['overview','Overview'],['purchases','Purchases'],['drafts','Review drafts'],['scan','Scan a source'],['people','People'],['audit','Audit history'],['settings','Settings']
  ];
  function navButton([id,label], mobile = false) { return `<button type="button" data-action="view" data-view="${id}" class="${state.view===id?'active':''}" aria-current="${state.view===id?'page':'false'}">${icon(id)}<span>${label}</span></button>`; }
  function shell() {
    const s = state.session;
    app.innerHTML = `<div class="shell">
      <aside class="sidebar"><div class="brand"><div class="brand-mark">c</div><div><div class="brand-word">commonplace.</div><span class="brand-sub">The household ledger</span></div></div>
        <div class="nav-label">The ledger</div><nav class="nav" aria-label="Main navigation">${nav.map(n=>navButton(n)).join('')}</nav>
        <div class="sidebar-bottom"><div class="user-row"><div class="user-circle">${esc((s.user?.username || 'H')[0].toUpperCase())}</div><div><strong>${esc(s.user?.username || 'Household')}</strong><small>${esc(s.user?.role || 'Member')} account</small></div></div><button class="logout" data-action="logout">Sign out →</button></div>
      </aside>
      <main class="main"><div class="topbar"><span class="topbar-left">Commonplace / ${esc(nav.find(n=>n[0]===state.view)?.[1] || '')}</span><div class="topbar-right">${s.preview || s.demo?'<span class="demo-pill">DEMO · ISOLATED PREVIEW</span>':''}<span class="fine muted"><span class="dot"></span>Private household space</span></div></div><div class="content" id="view-content"></div></main>
      <nav class="mobile-nav" aria-label="Mobile navigation">${nav.map(n=>navButton(n,true)).join('')}</nav></div>`;
  }
  function renderLogin(error = '') {
    app.innerHTML = `<div class="login-page"><section class="login-art"><div class="brand-word">commonplace.</div><div><span class="eyebrow">A shared household ledger</span><h1>Every little thing<br><i>adds up.</i></h1><p>Purchases, receipts, and all the small details of a life together. Kept in one private place.</p></div><span class="mono">For the everyday, together.</span></section><section class="login-form"><div><span class="eyebrow">Welcome back</span><h2>Open your ledger.</h2><p class="muted">Sign in to your household space.</p><form id="login-form"><label class="field"><span>Username</span><input name="username" required autocomplete="username" autofocus></label><label class="field"><span>Password</span><input name="password" type="password" required autocomplete="current-password"></label><p class="error-inline" role="alert">${esc(error)}</p><button class="btn primary block" type="submit">Sign in ${icon('arrow')}</button></form><p class="fine muted" style="margin-top:27px">Access is set up privately by the household owner. There is no public registration.</p></div></section></div>`;
  }
  function header(title, desc, actions = '') { return `<div class="page-head"><div><span class="eyebrow">Your household / ${esc(state.view)}</span><h1>${title}</h1><p>${desc}</p></div><div class="actions">${actions}</div></div>`; }
  const addButton = `<button class="btn primary" data-action="manual">${icon('plus')} Add purchase</button>`;
  function personOptions(selected = '', blank = 'Everyone') { return `<option value="">${esc(blank)}</option>${state.people.map(p=>`<option value="${esc(p.id)}" ${selected===p.id?'selected':''}>${esc(p.name)}${p.active?'':' (inactive)'}</option>`).join('')}`; }
  function categoryOptions(selected = '') { return `<option value="">All categories</option>${state.categories.map(c=>`<option value="${esc(c)}" ${selected===c?'selected':''}>${esc(c)}</option>`).join('')}`; }
  function monthBounds(month) { const [y,m] = month.split('-').map(Number); return {start:`${y}-${String(m).padStart(2,'0')}-01`,end:`${y}-${String(m).padStart(2,'0')}-${String(new Date(y,m,0).getDate()).padStart(2,'0')}`}; }
  function filtersHTML() {
    const f=state.filters;
    return `<div class="panel filter-panel"><div class="filter-header"><div class="quick"><button type="button" data-action="month" data-month="all" class="${state.month==='all'?'selected':''}">All time</button><button type="button" data-action="month" data-month="this" class="${state.month==='this'?'selected':''}">This month</button><button type="button" data-action="month" data-month="last" class="${state.month==='last'?'selected':''}">Last month</button></div><button class="link-btn fine" data-action="clear-filters">Clear filters</button></div>
      <form id="filters-form"><div class="filter-grid">
        <label class="field"><span>Search stores & items</span><input name="q" placeholder="Search the ledger" value="${esc(f.q||'')}"></label>
        <label class="field"><span>From</span><input name="start" type="date" value="${esc(f.start||'')}"></label>
        <label class="field"><span>Through</span><input name="end" type="date" value="${esc(f.end||'')}"></label>
        <label class="field"><span>Person</span><select name="person">${personOptions(f.person)}</select></label>
        <label class="field"><span>Category</span><select name="category">${categoryOptions(f.category)}</select></label>
        <div class="field"><span>&nbsp;</span><button class="btn primary block" type="submit">Apply filters</button></div>
      </div></form></div>`;
  }
  function formatBasis() { return state.summary?.basis || 'Headline totals use receipt totals; category and item totals use net line items.'; }
  function chart(series, currency, title) {
    if (!series?.length) return empty('Nothing to plot yet', 'Try a different date range or add a purchase.');
    const values = series.slice(-12), max = Math.max(1,...values.map(v=>Math.max(0,v.total_cents || 0)));
    const width = 720, height = 220, baseline = 180, barW = Math.min(45, 500 / values.length), step = 620 / values.length;
    return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(title)}: ${esc(values.map(v=>`${v.date||v.month}: ${money(v.total_cents,currency)}`).join('; '))}">
      <line class="gridline" x1="45" x2="705" y1="45" y2="45"/><line class="gridline" x1="45" x2="705" y1="112" y2="112"/><line class="gridline" x1="45" x2="705" y1="${baseline}" y2="${baseline}"/>
      <text x="4" y="48">${esc(money(max,currency))}</text><text x="4" y="183">0</text>
      ${values.map((v,i)=>{ const x=62+i*step, h=Math.max(v.total_cents?3:0,(Math.max(0,v.total_cents||0)/max)*131); return `<rect class="bar" x="${x}" y="${baseline-h}" width="${barW}" height="${h}" rx="2"><title>${esc(v.date||v.month)} · ${esc(money(v.total_cents,currency))}</title></rect><text x="${x+barW/2}" y="202" text-anchor="middle">${esc((v.date||v.month).slice(5))}</text>`; }).join('')}</svg>`;
  }
  function ranking(rows, label, currency) {
    if (!rows?.length) return empty('No breakdown yet', 'Matching line items will appear here.');
    const ordered=[...rows].sort((a,b)=>b.total_cents-a.total_cents).slice(0,6), max=Math.max(1,ordered[0].total_cents);
    return `<div class="ranking">${ordered.map(r=>`<div class="rank-row"><span class="rank-name" title="${esc(r[label]||'Uncategorized')}">${esc(r[label]||'Uncategorized')}</span><strong>${esc(money(r.total_cents,currency))}</strong><div class="rank-track"><div class="rank-fill" style="width:${Math.max(0,r.total_cents/max*100)}%"></div></div></div>`).join('')}</div>`;
  }
  function purchaseTable(data, compact = false) {
    const rows = data?.purchases || [];
    if (!rows.length) return empty('No purchases found', 'Nothing matches these filters yet. Add a purchase or widen your search.', `<button class="btn primary" data-action="manual">${icon('plus')} Add purchase</button>`);
    return `<div class="table-wrap"><table class="data-table"><thead><tr><th>Date</th><th>Store / items</th><th>Person</th><th>Type</th><th>Amount</th><th><span class="hidden">Open</span></th></tr></thead><tbody>${rows.map(p=>`<tr><td>${esc(dateLabel(p.date))}</td><td><div class="table-title">${esc(p.store||'Untitled purchase')}</div><div class="table-sub">${esc((p.items||[]).slice(0,2).map(i=>i.label).join(', '))}${p.items?.length>2?' + more':''}</div></td><td>${esc(personName(p.person_id))}</td><td><span class="tag ${p.source_type==='price_screenshot'?'warm':''}">${p.source_type==='price_screenshot'?'Price image':'Receipt'}</span></td><td class="amount">${esc(money(p.total_cents,p.currency))}</td><td><button class="btn sm" data-action="open-purchase" data-id="${esc(p.id)}">View ${icon('arrow')}</button></td></tr>`).join('')}</tbody></table></div>${compact?'':`<div class="table-footer"><span>${data.total||0} matching purchases · ${Math.min(state.page*PAGE+1,data.total||0)}–${Math.min((state.page+1)*PAGE,data.total||0)}</span><div class="actions"><button class="btn sm" data-action="page" data-step="-1" ${state.page===0?'disabled':''}>Previous</button><button class="btn sm" data-action="page" data-step="1" ${(state.page+1)*PAGE >= (data.total||0)?'disabled':''}>Next</button></div></div>`}`;
  }
  function overview() {
    const summary=state.summary, primary=summary?.currencies?.[0], filtered=!!(state.filters.category||state.filters.q);
    const currencies=summary?.currencies||[];
    const totals=currencies.length?currencies.map(c=>`<span>${esc(money(c.total_cents,c.currency))}</span>`).join(' · '):money(0);
    const count=currencies.reduce((n,c)=>n+c.purchase_count,0);
    return `${header('The big picture.', 'A clear view of what comes into your home, and what it costs.', `<button class="btn" data-action="view" data-view="scan">${icon('scan')} Scan a source</button>${addButton}`)}
      ${filtersHTML()}${state.error?failure():''}
      ${state.loading?loading():`<div class="stats"><div class="panel stat featured"><span class="stat-label">${filtered?'Matched item lines':'Total household spend'}</span><div class="stat-value currency-stack">${totals}</div><span class="stat-note">${filtered?'Item-only basis; receipt-level tax and discounts excluded.':'Receipt totals, including tax and receipt-level discounts.'}</span></div><div class="panel stat"><span class="stat-label">Purchases</span><div class="stat-value">${count}</div><span class="stat-note">Confirmed records in this view</span></div><div class="panel stat"><span class="stat-label">Item lines</span><div class="stat-value">${currencies.length===1?esc(money(primary.item_total_cents,primary.currency)):'—'}</div><span class="stat-note">Net line items, excluding receipt tax</span></div></div>
      ${currencies.length>1?'<div class="notice">Multiple currencies are reported separately. Amounts are never converted or combined.</div>':''}
      <div class="notice">${esc(formatBasis())}</div>
      ${currencies.map(c=>`<div class="section-grid"><div class="panel panel-pad"><div class="section-head"><div><span class="eyebrow">${esc(c.currency)} / in time</span><h2 class="section-title">Spending rhythm</h2></div><span class="tag">By ${c.by_day?.length>1?'day':'month'}</span></div>${chart(c.by_day?.length>1?c.by_day:c.by_month,c.currency,'Spending over time')}<div class="chart-caption"><span>Confirmed purchases only</span><span>${esc(c.currency)}</span></div></div><div class="panel panel-pad"><div class="section-head"><div><span class="eyebrow">Where it goes</span><h2 class="section-title">By category</h2></div></div>${ranking(c.by_category,'category',c.currency)}</div></div><div class="section-grid"><div class="panel panel-pad"><div class="section-head"><div><span class="eyebrow">The regulars</span><h2 class="section-title">Frequent items</h2></div></div>${ranking(c.by_item,'normalized_label',c.currency)}</div><div class="panel panel-pad"><div class="section-head"><div><span class="eyebrow">A shared account</span><h2 class="section-title">By person</h2></div></div>${ranking(c.by_person,'name',c.currency)}</div></div>`).join('')}
      ${!currencies.length?`<div class="panel">${empty('A fresh page', 'No confirmed purchases match this view. Drafts stay out of the ledger until you review and confirm them.', `<button class="btn primary" data-action="manual">${icon('plus')} Record a purchase</button>`)}</div>`:''}
      <div class="toolbar"><div><span class="eyebrow">Recent activity</span><h2 class="section-title">Latest purchases</h2></div><button class="btn ghost" data-action="view" data-view="purchases">Full history ${icon('arrow')}</button></div><div class="panel">${purchaseTable({purchases:(state.purchases?.purchases||[]).slice(0,5)},true)}</div>`}`;
  }
  function purchasesView() { return `${header('Purchase history.', 'Every confirmed purchase, in one searchable place.', `${addButton}`)}${filtersHTML()}${state.error?failure():''}${state.loading?loading():`<div class="toolbar"><span class="fine muted">${state.purchases?.total||0} confirmed purchases matching this view</span><div class="actions"><button class="btn" data-action="export" data-format="csv">${icon('download')} Export CSV</button><button class="btn" data-action="export" data-format="json">${icon('download')} Export JSON</button></div></div><div class="panel">${purchaseTable(state.purchases)}</div><p class="fine muted" style="margin-top:12px">Exports include every matching result, not just the current page. Large exports may require narrower dates.</p>`}`; }
  function draftsView() {
    const d=state.drafts;
    return `${header('Review drafts.', 'Extracted details are a starting point, never a saved expense. Check every line before confirming.', `<button class="btn" data-action="view" data-view="scan">${icon('scan')} Scan another</button>${addButton}`)}
      ${state.error?failure():''}${state.loading?loading():`<div class="notice">Drafts do not appear in totals or exports. Price screenshots are not treated as purchases until you explicitly confirm them.</div><div class="panel">${d?.drafts?.length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>Created</th><th>Source / store</th><th>Person</th><th>Extracted total</th><th>Status</th><th></th></tr></thead><tbody>${d.drafts.map(x=>`<tr><td>${esc(dateLabel(x.created_at?.slice(0,10)))}</td><td><div class="table-title">${esc(x.store||'Needs a store')}</div><div class="table-sub">${x.source_type==='price_screenshot'?'Price screenshot':'Receipt'} · ${x.items?.length||0} lines${x.demo?' · DEMO':''}</div></td><td>${esc(personName(x.person_id))}</td><td class="amount">${esc(money(x.total_cents,x.currency))}</td><td><span class="tag ${x.status==='draft'?'warm':''}">${esc(x.status)}</span></td><td><button class="btn sm" data-action="open-draft" data-id="${esc(x.id)}">${x.status==='draft'?'Review':'View'} ${icon('arrow')}</button></td></tr>`).join('')}</tbody></table></div><div class="table-footer"><span>${d.total} drafts · ${Math.min(state.draftsPage*PAGE+1,d.total)}–${Math.min((state.draftsPage+1)*PAGE,d.total)}</span><div class="actions"><button class="btn sm" data-action="draft-page" data-step="-1" ${state.draftsPage===0?'disabled':''}>Previous</button><button class="btn sm" data-action="draft-page" data-step="1" ${(state.draftsPage+1)*PAGE>=d.total?'disabled':''}>Next</button></div></div>`:empty('No drafts waiting', 'Upload a receipt or price screenshot to begin a review. If scanning is unavailable, you can always add a purchase manually.', `<button class="btn primary" data-action="view" data-view="scan">Scan a source</button>`)}</div>`}`;
  }
  function scanView() {
    const available=state.session.readiness?.ocr_available;
    return `${header('Bring it into focus.', 'Scan a receipt or price image, then review the extracted lines before they enter your ledger.', addButton)}
      ${state.session.preview?'<div class="notice warning"><strong>DEMO PREVIEW</strong> · This is an isolated example household. Inference is disabled; scans cannot be run here.</div>':''}
      ${!available?`<div class="notice warning"><strong>Scanning unavailable.</strong> ${esc(state.session.readiness?.message||'OCR has not been configured.')} No image will be processed. Use manual entry instead.</div>`:''}
      <div class="section-grid"><div class="panel panel-pad"><span class="eyebrow">01 / The source</span><h2 style="margin:9px 0 17px">A new scan</h2><form id="scan-form"><div class="form-grid two">
        <label class="field"><span>What are you scanning?</span><select name="source_type"><option value="receipt">Receipt · an actual purchase</option><option value="price_screenshot">Price screenshot · review before recording</option></select></label>
        <label class="field"><span>Who is this for?</span><select name="person" required>${personOptions('', 'Choose a person')}</select></label>
        <div class="span-all upload-zone"><span class="eyebrow">JPEG · PNG · WebP · PDF</span><p class="muted fine">Choose up to 20 files for one batch. Maximum 5 MiB each; PDFs up to ${state.session.limits?.pdf_pages||20} pages. The selected model, purchaser and source type apply to every file.</p><label class="field"><span>Choose source files</span><input type="file" name="files" multiple accept="image/jpeg,image/png,image/webp,application/pdf" ${available?'':'disabled'}></label><div class="camera-source"><label class="field"><span>Or capture a receipt photo</span><input type="file" name="camera" capture="environment" accept="image/jpeg,image/png,image/webp" ${available?'':'disabled'}></label></div><div id="file-selection" aria-live="polite"></div></div>
        <div class="span-all"><label class="field"><span>Choose a live OpenRouter model before uploading</span><input id="model-search" placeholder="Search live models by name or ID" value="${esc(state.modelQuery)}" ${available?'':'disabled'} autocomplete="off"></label><div id="model-results">${modelResults()}</div></div>
      </div><div id="scan-error" role="alert"></div><div class="form-footer"><span class="fine muted">Submitting authorizes one model scan per file. Nothing is confirmed automatically.</span><button class="btn primary" type="submit" ${available && !state.batchBusy?'':'disabled'}>Upload review batch ${icon('arrow')}</button></div></form></div>
      <div class="panel panel-pad"><span class="eyebrow">02 / What happens next</span><h2 style="margin:9px 0 16px">You make the call.</h2><div class="ranking"><div><strong>1. Choose a model</strong><p class="fine muted">The catalog is fetched live from OpenRouter. Select an image-capable model for photos.</p></div><div><strong>2. Inspect the extraction</strong><p class="fine muted">Names, categories, quantities, line totals, tax, and discounts remain editable.</p></div><div><strong>3. Confirm explicitly</strong><p class="fine muted">Drafts—including price screenshots—do not affect spending until you save them as purchases.</p></div></div><div class="notice" style="margin-top:25px">PDFs use native file input or the explicit Cloudflare AI parser. Parser and model costs can differ. Failed scans may still be billed; retries require your consent. Running scans cannot be safely cancelled.</div></div></div><div id="batch-content">${batchView()}</div>`;
  }
  function batchView() {
    if (!state.batch && !state.localFiles.length) return '';
    const jobs=state.batch?.jobs||[], pending=state.localFiles.filter(f=>!f.jobId);
    return `<section class="panel batch-panel" aria-label="Batch progress"><div class="panel-pad"><span class="eyebrow">Your review queue</span><h2>One source at a time.</h2><p class="fine muted">${esc(state.batch?.model||'')} · ${esc(personName(state.batch?.person))}. Each scan stays out of spending until you review and confirm its draft.</p><button class="btn sm" data-action="refresh-batch" ${state.batch?'':'disabled'}>Refresh status</button></div>${state.batchError?`<div class="notice warning" role="alert">${esc(state.batchError)}</div>`:''}${pending.map(f=>`<div class="job-row"><div><h3>${esc(f.file.name)}</h3><span class="job-status ${esc(f.status)}">${esc(f.status)}</span>${f.error?`<p role="alert">${esc(f.error)}</p>`:''}</div><div class="actions">${f.status==='failed'?`<button class="btn sm" data-action="retry-upload" data-id="${esc(f.key)}">Retry upload</button>`:''}${!['uploading','cancelled'].includes(f.status)?`<button class="btn sm" data-action="cancel-upload" data-id="${esc(f.key)}">Remove file</button>`:''}</div></div>`).join('')}${jobs.map(j=>`<div class="job-row"><div><h3>${esc(j.filename)}</h3><span class="job-status ${esc(j.status)}">${esc(j.status.replaceAll('_',' '))}</span><p>Uploaded by ${esc(actorLabel(j.uploaded_by))} · ${esc(timestamp(j.updated_at))}</p>${j.error?`<p role="alert">${esc(j.error)}</p>`:''}</div><div class="actions">${j.draft_id?`<button class="btn sm" data-action="open-draft" data-id="${esc(j.draft_id)}">Open draft</button>`:''}${j.status==='duplicate' && j.duplicate_of?.id?`<button class="btn sm" data-action="${j.duplicate_of.kind==='purchase'?'open-purchase':'open-draft'}" data-id="${esc(j.duplicate_of.id)}">Open existing ${esc(j.duplicate_of.kind)}</button>`:''}${['failed','interrupted'].includes(j.status)?`<button class="btn sm" data-action="retry-job" data-id="${esc(j.id)}">Retry scan</button>`:''}${j.status==='queued'?`<button class="btn sm" data-action="cancel-job" data-id="${esc(j.id)}">Cancel queued scan</button>`:''}${j.status==='running'?'<span class="fine muted">Already scanning; cannot cancel safely.</span>':''}${j.upload_id?`<a class="btn sm" href="/api/jobs/${encodeURIComponent(j.id)}/source" target="_blank" rel="noopener">Private source</a>`:''}</div></div>`).join('')}</section>`;
  }
  function drawBatch() { const node=$('#batch-content'); if(node) node.innerHTML=batchView(); }
  let batchTimer;
  async function pollBatch() {
    clearTimeout(batchTimer);
    if (!state.batch || !state.session?.authenticated) return;
    const id=state.batch.id;
    try { const batch=await api(`/api/batches/${encodeURIComponent(id)}`); if (!state.session?.authenticated || state.batch?.id!==id) return; state.batch=batch; state.batchError=''; }
    catch(e) { state.batchError=`Could not update scan statuses: ${e.message}. Use Refresh status to try again.`; drawBatch(); return; }
    drawBatch();
    if (state.batch.jobs?.some(j=>['queued','running'].includes(j.status)) || state.batchBusy) batchTimer=setTimeout(pollBatch,2500);
  }
  async function uploadFile(entry) {
    entry.status='uploading'; entry.error=''; drawBatch();
    const body=new FormData(); body.set('file',entry.file);
    try {
      const job=await api(`/api/batches/${encodeURIComponent(state.batch.id)}/files`,{method:'POST',body,headers:{'Idempotency-Key':entry.key}});
      entry.jobId=job.id; entry.status=job.status;
      state.batch.jobs=state.batch.jobs||[];
      if (!state.batch.jobs.some(j=>j.id===job.id)) state.batch.jobs.push(job);
    } catch(e) { entry.status='failed'; entry.error=e.message; }
    drawBatch();
  }
  async function startBatch(form) {
    if (state.batchBusy) throw Error('This batch is still uploading.');
    if (state.batch?.jobs?.some(j=>['queued','running'].includes(j.status))) throw Error('Wait for the current batch to finish before starting another.');
    if (state.localFiles.some(f=>!f.jobId && f.status==='failed')) throw Error('Retry or remove failed uploads before starting another batch.');
    const files=[...form.elements.files.files,...form.elements.camera.files], model=state.models?.models?.find(m=>m.id===state.selectedModel);
    if (!state.session.readiness?.ocr_available) throw Error(state.session.readiness?.message||'Scanning is unavailable. Use manual entry.');
    if (!model) throw Error('Choose a model from the live catalog first.');
    if (!files.length || files.length>20) throw Error('Choose between 1 and 20 source files.');
    if (!form.elements.person.value) throw Error('Choose a purchaser for this batch.');
    for (const file of files) {
      if (!file.size || file.size>5*1048576) throw Error(`${file.name}: file must be nonempty and at most 5 MiB.`);
      if (!['image/jpeg','image/png','image/webp','application/pdf'].includes(file.type)) throw Error(`${file.name}: choose JPEG, PNG, WebP or PDF.`);
      if (file.type!=='application/pdf'&&!model.supports_images) throw Error('The selected model does not accept images.');
    }
    // Batch acceptance authorizes inference; no local draft or extraction is fabricated.
    state.batch=await api('/api/batches',{method:'POST',body:JSON.stringify({model:model.id,person:form.elements.person.value,source_type:form.elements.source_type.value})});
    try { sessionStorage.setItem(`household-batch-${state.session.user.id}`,state.batch.id); } catch { /* Storage may be unavailable; the batch remains server-side. */ }
    state.localFiles=files.map(file=>({file,key:crypto.randomUUID(),status:'selected',jobId:null,error:''}));
    state.batchError=''; state.batchBusy=true; drawBatch();
    try { for (const entry of state.localFiles) if(entry.status!=='cancelled') await uploadFile(entry); }
    finally { state.batchBusy=false; }
    form.elements.files.value=''; form.elements.camera.value=''; $('#file-selection').innerHTML='';
    await pollBatch();
    notify('Uploads checked. Review each file status below; no purchases were confirmed.');
  }
  function auditEvents(data) {
    if (!data?.events?.length) return empty('No recorded changes','New changes will show their actor, time, and edited fields here. Deleted records remain in the global audit.');
    const events=[...data.events].sort((a,b)=>new Date(b.at)-new Date(a.at));
    const value=v=>typeof v==='string'?v:JSON.stringify(v,null,2) ?? 'Not recorded';
    return `<ol class="audit-list">${events.map(event=>`<li class="audit-event"><h3>${esc(event.action)} · ${esc(event.record_kind)} ${event.tombstone?'<span class="tag warm">Deleted record</span>':''}</h3><div class="fine">${esc(actorLabel(event.actor))}</div><time datetime="${esc(event.at)}">${esc(timestamp(event.at))}</time><p class="fine muted">Record ${esc(event.record_id)}</p><div class="audit-diff">${Object.entries(event.diff||{}).map(([field,change])=>`<div class="audit-change"><strong>${esc(field.replaceAll('_',' '))}</strong>${change && typeof change==='object' && !Array.isArray(change) && ('before' in change || 'after' in change)?`<div class="audit-value">Before: ${esc(value(change.before))}</div><div class="audit-value">After: ${esc(value(change.after))}</div>`:`<div class="audit-value">${esc(value(change))}</div>`}</div>`).join('')||'<p class="fine muted">No field-level changes recorded.</p>'}</div></li>`).join('')}</ol>`;
  }
  function auditPagination(data, drawer=false) {
    const offset=drawer?state.auditOffset:state.auditPage*PAGE;
    return `<div class="table-footer"><span>${data?.total||0} recorded events · Newest first</span><div class="actions"><button class="btn sm" data-action="${drawer?'drawer-audit-page':'audit-page'}" data-step="-1" ${offset===0?'disabled':''}>Previous</button><button class="btn sm" data-action="${drawer?'drawer-audit-page':'audit-page'}" data-step="1" ${offset+PAGE>=(data?.total||0)?'disabled':''}>Next</button></div></div>`;
  }
  function auditView() { return `${header('A record of the record.', 'Who changed what, and when. Purchasers and acting users are kept separate; deletion never erases this history.')}${state.error?failure():''}${state.loading?loading():`<div class="panel panel-pad">${auditEvents(state.globalAudit)}${auditPagination(state.globalAudit)}</div>`}`; }
  let auditFocus;
  let auditRequest=0;
  function closeAudit() { auditRequest++; $('#audit-layer')?.remove(); auditFocus?.focus(); }
  async function openAudit(path=state.auditPath, reset=true) {
    const request=++auditRequest;
    if (reset) { state.auditOffset=0; auditFocus=document.activeElement; }
    state.auditPath=path;
    $('#audit-layer')?.remove();
    overlay.insertAdjacentHTML('beforeend',`<div class="audit-backdrop" id="audit-layer"><section class="audit-drawer" role="dialog" aria-modal="true" aria-label="Record change history"><div class="modal-header"><div><span class="eyebrow">Server-recorded provenance</span><h2>Change history</h2></div><button class="icon-btn" data-action="close-audit" aria-label="Close history">${icon('close')}</button></div><div class="audit-body" id="audit-body">${loading()}</div></section></div>`);
    $('#audit-layer button').focus();
    try { const data=await api(`${path}?${query({limit:PAGE,offset:state.auditOffset})}`); if(request!==auditRequest) return; state.audit=data; if($('#audit-body')) $('#audit-body').innerHTML=auditEvents(state.audit)+auditPagination(state.audit,true); }
    catch(e) { if(request===auditRequest && $('#audit-body')) $('#audit-body').innerHTML=`<div class="notice warning" role="alert">${esc(e.message)} <button class="link-btn" data-action="retry-audit">Retry history</button></div>`; }
  }
  const scopeChoices=[['purchases:read','Read purchases','Confirmed purchase history'],['summary:read','Read summaries','Spending reports'],['drafts:read','Read drafts','Unconfirmed extractions'],['purchases:write','Create & edit purchases','Can change confirmed spending'],['purchases:delete','Delete purchases','Can remove confirmed records'],['uploads:create','Upload & scan sources','May incur model charges'],['drafts:write','Edit drafts','Can alter extracted details'],['drafts:confirm','Confirm drafts','Adds reconciled drafts to spending']];
  function scopeForm() { return `<label class="field span-all"><span>Exact fleet agent_id · required for write access</span><input name="agent_id" maxlength="64" pattern="[a-z0-9][a-z0-9_-]{0,63}" placeholder="Exact existing fleet agent slug" autocomplete="off"><small class="muted">This immutable token identity is not a purchaser. Do not invent or substitute another agent’s ID.</small></label><fieldset class="span-all" style="border:0;padding:0;margin:0"><legend class="eyebrow" style="margin-bottom:12px">Explicit permissions · writes off by default</legend><div class="scope-grid">${scopeChoices.map(([scope,label,desc])=>`<label class="scope-option ${scope.endsWith(':read')?'':'write'}"><input type="checkbox" name="scope" value="${scope}" ${['purchases:read','summary:read'].includes(scope)?'checked':''}><span>${label}<small>${scope} · ${desc}</small></span></label>`).join('')}</div></fieldset><label class="fine span-all"><input type="checkbox" name="write_consent"> For write access, I will create a separate Vault connection for this exact agent and grant it only to that matching agent. Existing shared read-only access stays unchanged.</label>`; }
  function modelResults() {
    if (!state.session.readiness?.ocr_available) return `<p class="fine muted">A provider key must be configured to scan. No models are implied or simulated.</p>`;
    if (state.modelError) return `<div class="notice warning">${esc(state.modelError)} <button class="link-btn" data-action="reload-models">Retry catalog</button></div>`;
    if (!state.models) return loading();
    const list=state.models.models||[];
    return `<div class="model-meta">Live OpenRouter catalog · ${esc(state.models.fetched_at||'')} · ${list.length} matching models</div>
      <div class="model-list" role="radiogroup" aria-label="Models">${list.length?list.map(m=>`<button type="button" role="radio" aria-checked="${m.id===state.selectedModel}" class="model-option ${m.id===state.selectedModel?'selected':''}" data-action="select-model" data-id="${esc(m.id)}"><strong>${esc(m.name||m.id)}</strong><small>${esc(m.id)} · Inputs: ${esc((m.input_modalities||[]).join(', ')||'unspecified')} · Outputs: ${esc((m.output_modalities||[]).join(', ')||'unspecified')}</small><small style="display:block">${m.supports_images?'Image input enabled':'No image input'} · ${m.supports_native_pdf?'Native file/PDF enabled':m.supports_documents?'Documents via '+esc(m.document_mode||'file parser'):'PDF text via explicit parser when supported'}</small><div class="model-prices">Input ${esc(m.input_usd_per_million==null?'not listed':`$${m.input_usd_per_million}/M`)} · Output ${esc(m.output_usd_per_million==null?'not listed':`$${m.output_usd_per_million}/M`)} · Image ${esc(m.pricing?.image==null?'not listed':`$${m.pricing.image}/image unit`)} · Request ${esc(m.pricing?.request==null?'not listed':`$${m.pricing.request}/request`)}</div></button>`).join(''):'<div class="empty"><p>No models match this search.</p></div>'}</div>
      <p class="model-meta">${esc(state.models.cost_notice||'Actual cost varies with source size, parser choice, tokens, and model pricing.')} Raw image/request prices are provider per-unit fields, not a scan estimate. ${esc(state.models.parser?.fee||'')}</p>`;
  }
  function peopleView() { return `${header('The people behind it.', 'Keep purchaser attribution accurate as your household changes.')}<div class="section-grid"><div class="panel"><div class="panel-pad"><span class="eyebrow">Household members</span><h2>People</h2><p class="fine muted">Inactive people remain attached to past purchases.</p></div>${state.people.length?state.people.map(p=>`<div class="row-card"><div><h3>${esc(p.name)} <span class="tag ${p.active?'':'warm'}">${p.active?'Active':'Inactive'}</span></h3><p>Purchaser ID: ${esc(p.id)}</p></div><button class="btn sm" data-action="edit-person" data-id="${esc(p.id)}">Manage</button></div>`).join(''):empty('Nobody here yet','Add your first person to attribute purchases.')} </div><div class="panel panel-pad"><span class="eyebrow">A place at the table</span><h2>Add a person</h2><p class="fine muted">A person is an attribution label, not a separate login.</p><form id="person-form"><label class="field"><span>Name</span><input name="name" maxlength="100" required placeholder="How they appear in the ledger"></label><button class="btn primary" style="margin-top:14px" type="submit">${icon('plus')} Add person</button></form></div></div>`; }
  function settingsView() {
    const owner=state.session.user?.role==='owner';
    return `${header('The private details.', 'Manage agent access and understand how this space connects to your tools.')}
      <div class="settings-grid"><div class="panel panel-pad"><span class="eyebrow">About this space</span><h2>Household settings</h2><div class="total-line"><span>Timezone</span><strong>${esc(state.session.timezone)}</strong></div><div class="total-line"><span>Default currency</span><strong>${esc(state.session.currency)}</strong></div><div class="total-line"><span>Your role</span><strong>${esc(state.session.user?.role)}</strong></div><div class="total-line"><span>Scanning</span><strong>${state.session.readiness?.ocr_available?'Available':'Not configured'}</strong></div>${state.session.preview?'<div class="notice warning" style="margin-top:17px">DEMO PREVIEW · Isolated data. No live inference or production storage.</div>':''}</div>
      <div class="panel panel-pad"><span class="eyebrow">Scoped tools / via Vault</span><h2>Agent connection</h2><p class="fine muted"><strong>Agents → Vault → Household MCP.</strong> Read access remains the default. Write permissions are separate, explicit opt-ins that can change your ledger or spend on scans.</p>${state.session.preview?'<div class="notice warning">MCP and token issuance are disabled in this fictional preview. A dedicated HTTPS Household deployment is required; this preview is not a live Vault connection.</div>':`<div class="field"><span>Full HTTPS MCP endpoint for Vault</span><div class="code">${esc(location.origin + (state.session.mcp_endpoint||'/mcp'))}</div></div>`}<ol class="fine muted" style="padding-left:20px;line-height:1.8"><li>Keep any existing shared read-only token and connection unchanged.</li><li>For writes, issue a new token bound to the <strong>exact fleet agent_id</strong> with only the necessary scopes. This identity cannot be changed later.</li><li>Create a <strong>separate Vault Household Spending connection for each write agent</strong>. Enter the deployed HTTPS /mcp URL and paste the show-once token into the Bearer token password field.</li><li><strong>Save → Sync tools → verify</strong> the scoped household_* tools. Grant this connection <strong>only to the matching bound agent</strong> in Vault → Grants, never to a group or another agent.</li></ol><p class="fine muted">A bound token does not itself configure Vault grants. Audit attribution identifies its bound agent, so incorrect sharing would misattribute actions. Never copy tokens into agent prompts, chat, URLs or screenshots.</p><p class="fine muted">Household → Vault → OpenRouter scanning setup is independent of agent access.</p></div></div>
      <div class="panel panel-pad" style="margin-top:14px"><div class="section-head"><div><span class="eyebrow">Owner access only</span><h2 class="section-title">Household MCP tokens</h2></div></div>${owner?`<p class="fine muted">Secrets appear once for transfer into Vault. Tokens cannot be edited or rebound: revoke and reissue instead. Revocation blocks every use on the next request.</p>${state.error?failure():''}${state.loading?loading():`<div id="tokens-content">${tokenList()}</div><form id="token-form" class="form-grid" style="margin-top:22px;padding-top:22px;border-top:1px solid var(--line)"><label class="field"><span>Token name</span><input name="name" placeholder="Vault household reporting" required maxlength="100" ${state.session.preview?'disabled':''}></label><label class="field"><span>Expiry (days)</span><input name="expires_days" type="number" min="1" max="365" value="90" required></label>${scopeForm()}<button class="btn primary" type="submit" ${state.session.preview?'disabled':''}>Issue scoped token</button></form>`}`:`<div class="notice">Only the household owner can issue and revoke agent tokens. Your purchase and reporting access is unchanged.</div>`}</div>`;
  }
  function tokenList() { const tokens=state.tokens?.tokens||[]; return tokens.length?tokens.map(t=>`<div class="row-card"><div><h3>${esc(t.name)} <span class="tag ${t.revoked?'warm':''}">${t.revoked?'Revoked':'Active'}</span></h3><p>Bound agent: ${esc(t.agent_id||'Shared read-only · no bound agent')} · Identity is immutable</p><p>${esc(t.scopes?.join(' · '))} · Expires ${esc(t.expires_at?.slice(0,10)||'—')} · Last used ${esc(t.last_used_at?.slice(0,10)||'Never')}</p></div>${t.revoked?'':`<button class="btn sm" data-action="revoke-token" data-id="${esc(t.id)}">Revoke</button>`}</div>`).join(''):empty('No Household MCP tokens','Issue a scoped, expiring token when you are ready. Read-only access is selected by default.'); }
  function draw() {
    shell(); const v=$('#view-content');
    v.innerHTML=({overview,purchases:purchasesView,drafts:draftsView,scan:scanView,people:peopleView,audit:auditView,settings:settingsView}[state.view]||overview)();
  }
  async function refresh() {
    state.loading=true; state.error=''; draw();
    try {
      if (state.view==='overview') [state.summary,state.purchases]=await Promise.all([api(withFilters('/api/summary')),api(withFilters('/api/purchases',{limit:5,offset:0}))]);
      if (state.view==='purchases') state.purchases=await api(withFilters('/api/purchases',{limit:PAGE,offset:state.page*PAGE}));
      if (state.view==='drafts') state.drafts=await api(`/api/drafts?${query({limit:PAGE,offset:state.draftsPage*PAGE})}`);
      if (state.view==='audit') state.globalAudit=await api(`/api/audit?${query({limit:PAGE,offset:state.auditPage*PAGE})}`);
      if (state.view==='settings' && state.session.user?.role==='owner') state.tokens=await api('/api/agent-tokens');
      if (state.view==='scan' && state.session.readiness?.ocr_available && !state.models) await loadModels();
    } catch(e) { state.error=e.message; }
    state.loading=false; draw();
  }
  async function loadPeople() { const data=await api('/api/people'); state.people=data.people||[]; }
  async function loadCategories() { const data=await api('/api/categories'); state.categories=[...new Set([...(data.categories||[]),...(Array.isArray(data.suggested)?data.suggested:[])])].sort(); }
  async function loadModels() {
    state.modelError='';
    try { state.models=await api(`/api/models?${query({q:state.modelQuery})}`); }
    catch(e) { state.models=null; state.modelError=e.message; }
  }
  async function go(view) { state.view=view; if (view==='purchases') state.page=0; draw(); await refresh(); window.scrollTo({top:0,behavior:'smooth'}); }
  function close() { overlay.innerHTML=''; }
  function modal(title, body, wide=false, sub='') {
    overlay.innerHTML=`<div class="modal-backdrop" data-action="backdrop"><section class="modal ${wide?'wide':''}" role="dialog" aria-modal="true" aria-label="${esc(title)}"><div class="modal-header"><div><span class="eyebrow">${esc(sub||'Household ledger')}</span><h2>${esc(title)}</h2></div><button class="icon-btn" data-action="close" aria-label="Close">${icon('close')}</button></div><div class="modal-body">${body}</div></section></div>`;
    $('.modal .icon-btn')?.focus();
  }
  function confirmAction(title, text, callback, danger=true) {
    modal(title,`<p>${esc(text)}</p><div class="form-footer"><button class="btn" data-action="close">Cancel</button><button id="confirm-action" class="btn ${danger?'danger':'primary'}">Yes, ${danger?'delete':'continue'}</button></div>`);
    $('#confirm-action').onclick=async () => { const b=$('#confirm-action'); b.disabled=true; try { await callback(); close(); } catch(e) { notify(e.message,true); b.disabled=false; } };
  }
  const itemBlank = () => ({id:'',label:'',normalized_label:'',category:'',quantity:'1',unit_price_cents:null,line_total_cents:null});
  function itemRow(item = itemBlank()) {
    return `<div class="item-row" data-line-manual="${item.line_total_cents != null ? 'true' : 'false'}">
      <label class="field"><span>Item on source</span><input name="label" value="${esc(item.label)}" required placeholder="e.g. Oat milk"></label>
      <label class="field"><span>Normalized name</span><input name="normalized_label" value="${esc(item.normalized_label)}" required placeholder="Oat milk"></label>
      <label class="field"><span>Category</span><input name="category" value="${esc(item.category)}" list="category-suggestions" required placeholder="Groceries"></label>
      <label class="field"><span>Qty</span><input name="quantity" value="${esc(item.quantity||'1')}" inputmode="decimal" required></label>
      <label class="field"><span>Unit price</span><input name="unit_price" value="${esc(decimal(item.unit_price_cents))}" inputmode="decimal" placeholder="Optional"></label>
      <label class="field"><span>Line total</span><input name="line_total" value="${esc(decimal(item.line_total_cents))}" inputmode="decimal" required placeholder="0.00"></label>
      <button type="button" class="icon-btn" data-action="remove-item" aria-label="Remove item">${icon('trash')}</button></div>`;
  }
  let editorContext=null;
  function editor(record=null,kind='manual') {
    editorContext={kind,id:record?.id||null,record};
    const editable=kind!=='confirmed-draft';
    const r=record||{store:'',date:today(),currency:state.session.currency,person_id:'',source_type:'receipt',items:[itemBlank()],subtotal_cents:null,tax_cents:0,discount_cents:0,total_cents:null,notes:''};
    const source=record?.source_available && kind!=='manual' ? `/api/${kind==='purchase'?'purchases':'drafts'}/${encodeURIComponent(record.id)}/source` : '';
    modal(kind==='manual'?'Record a purchase':kind==='purchase'?'Purchase details':kind==='confirmed-draft'?'Confirmed draft':'Review extraction',
      `${record?.demo?'<div class="notice warning">DEMO RECORD · This record belongs to an isolated preview ledger.</div>':''}
      ${record?provenance(record,kind==='purchase'?'purchases':'drafts'):''}
      ${r.source_type==='price_screenshot'?'<div class="notice warning"><strong>Price screenshot.</strong> This is not proof of a transaction. Confirm only if you actually made this purchase.</div>':''}
      ${record?.warnings?.length?`<div class="notice warning"><strong>Extraction needs review</strong><ul>${record.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></div>`:''}
      ${source?`<details class="source-preview"><summary class="link-btn">View private source attachment</summary><p class="fine muted">Only signed-in household members can open this file. If your browser cannot preview it, <a href="${esc(source)}" target="_blank" rel="noopener">open the private source</a>.</p><img src="${esc(source)}" alt="Original uploaded source (PDFs may require opening in a new tab)" onerror="this.style.display='none'"></details>`:''}
      <form id="editor-form" novalidate><div class="form-grid">
        <label class="field"><span>Store / merchant</span><input name="store" value="${esc(r.store)}" required placeholder="Where was it bought?" ${editable?'':'disabled'}></label>
        <label class="field"><span>Purchase date</span><input name="date" type="date" value="${esc(r.date||'')}" required ${editable?'':'disabled'}></label>
        <label class="field"><span>Purchaser</span><select name="person_id" required ${editable?'':'disabled'}>${personOptions(r.person_id,'Choose a person')}</select></label>
        <label class="field"><span>Currency</span><input name="currency" value="${esc(r.currency||state.session.currency)}" readonly></label>
        <label class="field"><span>Source type</span><select name="source_type" ${kind==='manual'?'':'disabled'}><option value="receipt" ${r.source_type==='receipt'?'selected':''}>Receipt</option><option value="price_screenshot" ${r.source_type==='price_screenshot'?'selected':''}>Price screenshot</option></select></label>
        <label class="field"><span>Subtotal · informational</span><input name="subtotal" inputmode="decimal" value="${esc(decimal(r.subtotal_cents))}" placeholder="Optional" ${editable?'':'disabled'}></label>
      </div><div class="item-editor"><div class="toolbar"><div><span class="eyebrow">Line-by-line</span><h3 style="margin:4px 0 0">Items</h3></div>${editable?`<button class="btn sm" type="button" data-action="add-item">${icon('plus')} Add item</button>`:''}</div>
      <div id="item-rows">${(r.items?.length?r.items:[itemBlank()]).map(itemRow).join('')}</div><datalist id="category-suggestions">${state.categories.map(c=>`<option value="${esc(c)}"></option>`).join('')}</datalist></div>
      <div class="form-grid" style="margin-top:23px"><label class="field"><span>Tax</span><input name="tax" inputmode="decimal" value="${esc(decimal(r.tax_cents))}" placeholder="0.00" ${editable?'':'disabled'}></label><label class="field"><span>Additional receipt discount</span><input name="discount" inputmode="decimal" value="${esc(decimal(r.discount_cents))}" placeholder="0.00" ${editable?'':'disabled'}></label><label class="field"><span>Receipt total</span><input name="total" inputmode="decimal" value="${esc(decimal(r.total_cents))}" required placeholder="0.00" ${editable?'':'disabled'}></label><label class="field span-all"><span>Notes</span><textarea name="notes" placeholder="Anything worth remembering" ${editable?'':'disabled'}>${esc(r.notes||'')}</textarea></label></div>
      <div id="reconciliation" class="reconcile ${record?.reconciliation?.balanced?'':'off'}">${record?.reconciliation?.balanced?'The saved totals are balanced.':'Check line totals, tax, discount, and receipt total before confirming.'}</div>
      <p class="fine muted">Line totals include line-level discounts. Receipt-level discount is additional; subtotal is informational and is not added to the total.</p>
      <div class="form-footer"><div class="actions">${record && kind!=='confirmed-draft'?`<button type="button" class="btn danger" data-action="delete-record">Delete</button>`:''}</div><div class="actions"><button type="button" class="btn" data-action="close">Close</button>${editable?kind==='draft'?`<button type="submit" class="btn" data-intent="save">Save draft</button><button type="submit" class="btn primary" data-intent="confirm">Save & confirm purchase</button>`:`<button type="submit" class="btn primary" data-intent="save">${kind==='manual'?'Confirm purchase':'Save changes'}</button>`:''}${kind==='confirmed-draft' && r.confirmed_purchase_id?`<button type="button" class="btn primary" data-action="open-purchase" data-id="${esc(r.confirmed_purchase_id)}">Open purchase</button>`:''}</div></div></form>`,true, kind==='draft'?'Draft / not counted in spending':kind==='manual'?'Manual entry':kind==='purchase'?'Confirmed record':'Read only');
    if (!editable) $$('#editor-form input, #editor-form textarea, #editor-form select').forEach(e=>e.disabled=true);
    updateReconciliation();
  }
  function $$(s, root=document) { return [...root.querySelectorAll(s)]; }
  function readEditor(form, partial = false) {
    const val=name=>form.elements[name]?.value?.trim()||'';
    const items=$$('#item-rows .item-row').map((row,i)=>{
      const read=n=>row.querySelector(`[name="${n}"]`)?.value.trim()||'';
      const qty=read('quantity') || (partial?'1':'');
      if (!/^(?:\d+)(?:\.\d+)?$/.test(qty) || Number(qty)<=0) throw Error(`Item ${i+1}: quantity must be a positive number.`);
      const line=cents(read('line_total'),`Item ${i+1} line total`,!partial), unit=cents(read('unit_price'),`Item ${i+1} unit price`);
      if ((line!=null&&line<0) || (unit!=null&&unit<0)) throw Error(`Item ${i+1}: prices cannot be negative.`);
      const label=read('label'), normalized=read('normalized_label'), category=read('category');
      if (!partial && (!label||!normalized||!category)) throw Error(`Item ${i+1}: name, normalized name, and category are required.`);
      return {label,normalized_label:normalized,category,quantity:qty,unit_price_cents:unit,line_total_cents:line};
    });
    if (!partial && !items.length) throw Error('Add at least one item.');
    const tax=cents(val('tax'),'Tax'), discount=cents(val('discount'),'Discount'), total=cents(val('total'),'Receipt total',!partial), subtotal=cents(val('subtotal'),'Subtotal');
    if ((tax!=null&&tax<0)||(discount!=null&&discount<0)||(total!=null&&total<0)) throw Error('Tax, discount, and total cannot be negative.');
    if (!partial) {
      const calculated=items.reduce((sum,i)=>sum+i.line_total_cents,0)+(tax??0)-(discount??0);
      if (calculated!==total) throw Error(`Totals do not balance. Lines + tax − additional discount = ${money(calculated,val('currency'))}, but receipt total is ${money(total,val('currency'))}. Correct them before saving.`);
      if (!val('store')||!val('date')||!val('person_id')) throw Error('Store, date, and purchaser are required.');
    }
    const payload={store:val('store'),date:val('date')||null,currency:val('currency')||state.session.currency,person_id:val('person_id')||null,items,subtotal_cents:subtotal,tax_cents:partial?tax:(tax??0),discount_cents:partial?discount:(discount??0),total_cents:total,notes:val('notes')};
    if (editorContext.kind==='manual') payload.source_type=val('source_type');
    return payload;
  }
  function updateReconciliation() {
    const form=$('#editor-form'), out=$('#reconciliation'); if (!form||!out) return;
    try {
      const line=$$('#item-rows [name="line_total"]').reduce((sum,el)=>sum+(cents(el.value,'Line')||0),0);
      const tax=cents(form.elements.tax.value,'Tax')||0, discount=cents(form.elements.discount.value,'Discount')||0, total=cents(form.elements.total.value,'Total');
      const calculated=line+tax-discount, balanced=total!=null && calculated===total;
      out.className=`reconcile ${balanced?'':'off'}`;
      out.textContent=`Lines ${money(line,form.elements.currency.value)} + tax ${money(tax,form.elements.currency.value)} − discount ${money(discount,form.elements.currency.value)} = ${money(calculated,form.elements.currency.value)}. ${total==null?'Enter a receipt total.':balanced?'Balanced.':`Difference: ${money(total-calculated,form.elements.currency.value)}. Please correct before saving.`}`;
    } catch { out.className='reconcile off'; out.textContent='Enter valid amounts with no more than two decimal places.'; }
  }
  function fillLineFromUnit(row) {
    if (!row || row.dataset.lineManual === 'true') return;
    const unitInput=$('[name="unit_price"]',row), quantityInput=$('[name="quantity"]',row), lineInput=$('[name="line_total"]',row);
    if (!unitInput || !quantityInput || !lineInput) return;
    try {
      const unit=cents(unitInput.value,'Unit price');
      const match=quantityInput.value.trim().match(/^(\d+)(?:\.(\d+))?$/);
      if (unit==null || !match || !Number(match[1]+(match[2]||''))) { lineInput.value=''; return; }
      const fractional=match[2]||'', scale=10n**BigInt(fractional.length);
      const quantity=BigInt(match[1]+fractional);
      const product=BigInt(unit)*quantity;
      const total=Number((product+scale/2n)/scale);
      if (!Number.isSafeInteger(total)) return;
      lineInput.value=decimal(total);
    } catch { /* Keep the user's line total unchanged until the inputs are valid. */ }
  }
  async function saveEditor(form, intent) {
    const {kind,id}=editorContext, payload=readEditor(form,kind==='draft' && intent!=='confirm');
    if (kind==='manual') { await api('/api/purchases',{method:'POST',body:JSON.stringify(payload)}); notify('Purchase recorded.'); close(); await loadCategories(); await go('purchases'); }
    else if (kind==='purchase') { await api(`/api/purchases/${encodeURIComponent(id)}`,{method:'PATCH',headers:versionHeaders(editorContext.record),body:JSON.stringify(payload)}); notify('Purchase updated.'); close(); await loadCategories(); await refresh(); }
    else if (kind==='draft') {
      const updated=await api(`/api/drafts/${encodeURIComponent(id)}`,{method:'PATCH',headers:versionHeaders(editorContext.record),body:JSON.stringify(payload)});
      editorContext.record=updated;
      if (intent==='confirm') { await api(`/api/drafts/${encodeURIComponent(id)}/confirm`,{method:'POST',headers:versionHeaders(updated),body:'{}'}); notify('Draft confirmed as a purchase.'); close(); await loadCategories(); await go('purchases'); }
      else { notify('Draft saved. It is not counted in spending.'); close(); await refresh(); }
    }
  }
  function personModal(id) {
    const p=state.people.find(x=>x.id===id); if (!p) return;
    modal('Manage person',`<form id="edit-person-form" data-id="${esc(p.id)}"><label class="field"><span>Name</span><input name="name" value="${esc(p.name)}" required></label><label class="fine" style="display:block;margin-top:15px"><input type="checkbox" name="active" ${p.active?'checked':''}> Active purchaser</label><p class="fine muted">Deactivation keeps this person in historical reports.</p><div class="form-footer"><button class="btn" data-action="close" type="button">Cancel</button><button class="btn primary" type="submit">Save person</button></div></form>`);
  }
  function secretModal(result) {
    modal('Copy your token now',`<div class="notice warning"><strong>Shown once only.</strong> This secret cannot be retrieved after this dialog closes. Paste it only into Vault's Bearer token password field, never into agents, prompts, chat, URLs or source files. ${result.agent_id?`Create a separate connection and grant it only to <strong>${esc(result.agent_id)}</strong>. Do not modify the shared read-only connection.`:''}</div><p class="fine">Bound agent: ${esc(result.agent_id||'Shared read-only')} · ${esc(result.scopes?.join(', '))}</p><label class="field"><span>One-time secret · hidden until revealed</span><input class="token-secret" id="token-secret" type="password" readonly autocomplete="off" spellcheck="false" value="${esc(result.token)}"></label><div class="form-footer"><span class="fine muted">Expires ${esc(result.expires_at?.slice(0,10)||'as configured')}</span><div class="actions"><button class="btn" data-action="reveal-token" aria-pressed="false">Reveal</button><button class="btn" data-action="copy-token">Copy token</button><button class="btn primary" data-action="close">I have saved it</button></div></div>`);
  }
  function conflictWarning() {
    const form=$('#editor-form'); if (!form) return;
    $('#conflict-warning')?.remove();
    form.insertAdjacentHTML('afterbegin',`<div class="notice warning conflict-warning" id="conflict-warning" role="alert"><strong>This record changed or the write conflicted.</strong> Your edits have not overwritten another version. Keep a copy of any unsaved edits, then reload the current server record to review before saving again. <button class="link-btn" type="button" data-action="reload-record">Reload latest record</button></div>`);
    form.closest('.modal-body').scrollTop=0;
  }
  async function download(format) {
    const path=withFilters('/api/export',{format});
    const response=await fetch(path,{credentials:'same-origin'});
    if (!response.ok) { let detail; try { detail=(await response.json()).detail; } catch {} throw Error(detail||`Export failed (${response.status}).`); }
    const blob=await response.blob(), url=URL.createObjectURL(blob), link=document.createElement('a');
    link.href=url; link.download=`household-${today()}.${format}`; document.body.appendChild(link); link.click(); link.remove(); setTimeout(()=>URL.revokeObjectURL(url),30000);
    notify(`Full filtered ${format.toUpperCase()} export downloaded.`);
  }
  async function init() {
    try {
      state.session=await api('/api/session');
      if (!state.session.authenticated) { renderLogin(); return; }
      shell(); $('#view-content').innerHTML=loading();
      await Promise.all([loadPeople(),loadCategories()]);
      await refresh();
      try { const id=sessionStorage.getItem(`household-batch-${state.session.user.id}`); if(id) { state.batch=await api(`/api/batches/${encodeURIComponent(id)}`); await pollBatch(); } } catch { /* A removed or inaccessible previous batch must not block the ledger. */ }
    } catch(e) { if (!state.session?.authenticated) renderLogin(e.message); else { state.error=e.message; state.loading=false; draw(); } }
  }
  document.addEventListener('submit',async e=>{
    const form=e.target;
    if (!['login-form','filters-form','scan-form','editor-form','person-form','edit-person-form','token-form'].includes(form.id)) return;
    e.preventDefault(); const submitter=e.submitter, button=submitter || $('button[type=submit]',form); if(button) button.disabled=true;
    try {
      if(form.id==='login-form') {
        state.session=await api('/api/login',{method:'POST',body:JSON.stringify({username:form.elements.username.value,password:form.elements.password.value})});
        if (!state.session.authenticated) throw Error('Could not sign in.');
        await Promise.all([loadPeople(),loadCategories()]); await refresh();
      } else if(form.id==='filters-form') {
        const d=new FormData(form), next={}; ['q','start','end','person','category'].forEach(k=>{ const v=String(d.get(k)||'').trim(); if(v) next[k]=v; });
        if(next.start&&next.end&&next.start>next.end) throw Error('Start date must be before end date.');
        state.filters=next; state.month='custom'; state.page=0; await refresh();
      } else if(form.id==='scan-form') {
        await startBatch(form);
      } else if(form.id==='editor-form') await saveEditor(form,submitter?.dataset.intent||'save');
      else if(form.id==='person-form') { await api('/api/people',{method:'POST',body:JSON.stringify({name:form.elements.name.value.trim()})}); await loadPeople(); draw(); notify('Person added.'); }
      else if(form.id==='edit-person-form') { await api(`/api/people/${encodeURIComponent(form.dataset.id)}`,{method:'PATCH',body:JSON.stringify({name:form.elements.name.value.trim(),active:form.elements.active.checked})}); await loadPeople(); close(); draw(); notify('Person updated.'); }
      else if(form.id==='token-form') {
        const scopes=$$('input[name="scope"]:checked',form).map(i=>i.value); if(!scopes.length) throw Error('Choose at least one scope.');
        const writes=scopes.some(scope=>!scope.endsWith(':read')), agent_id=form.elements.agent_id.value.trim();
        if (state.session.preview) throw Error('Credential issuance is disabled in preview.');
        if (writes && !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(agent_id)) throw Error('Write credentials require the exact existing fleet agent_id (lowercase slug).');
        if (writes && !form.elements.write_consent.checked) throw Error('Acknowledge the separate, matching-agent-only Vault connection before issuing write credentials.');
        const payload={name:form.elements.name.value.trim(),scopes,expires_days:Number(form.elements.expires_days.value)};
        if (agent_id) payload.agent_id=agent_id;
        const result=await api('/api/agent-tokens',{method:'POST',body:JSON.stringify(payload)});
        // Present the secret before any follow-up request can fail. Never retain it in state or storage.
        secretModal(result);
        try { state.tokens=await api('/api/agent-tokens'); draw(); } catch(err) { notify(`Token issued. Keep this dialog open to save it. Token list refresh failed: ${err.message}`,true); }
      }
    } catch(err) {
      if(form.id==='login-form') { $('.error-inline',form).textContent=err.message; }
      else if(form.id==='editor-form' && err.status===409) { conflictWarning(); notify('Write conflict. Reload and review the latest record; your edits were not forced over it.',true); }
      else if(form.id==='scan-form') { const out=$('#scan-error'); if(out) out.innerHTML=`<div class="notice warning">${esc(err.message)} No extraction or confirmation is being reported. You can still add a purchase manually.</div>`; notify(err.message,true); }
      else { notify(err.message,true); if(err.duplicate) confirmAction('Source already exists',`This file is already saved as a ${err.duplicate.kind}. Open it instead?`,async()=>{const d=err.duplicate; const r=await api(`/api/${d.kind==='draft'?'drafts':'purchases'}/${encodeURIComponent(d.id)}`); editor(r,d.kind==='draft'?(r.status==='draft'?'draft':'confirmed-draft'):'purchase');},false); }
    } finally { if(button?.isConnected) button.disabled=false; }
  });
  document.addEventListener('click',async e=>{
    const b=e.target.closest('[data-action]'); if(!b) return;
    const a=b.dataset.action;
    if(a==='backdrop'&&e.target!==b) return;
    try {
      if(a==='view') await go(b.dataset.view);
      else if(a==='record-audit') await openAudit(`/api/${b.dataset.kind}/${encodeURIComponent(b.dataset.id)}/audit`);
      else if(a==='close-audit') closeAudit();
      else if(a==='retry-audit') await openAudit(state.auditPath,false);
      else if(a==='drawer-audit-page') { state.auditOffset=Math.max(0,state.auditOffset+Number(b.dataset.step)*PAGE); await openAudit(state.auditPath,false); }
      else if(a==='audit-page') { state.auditPage=Math.max(0,state.auditPage+Number(b.dataset.step)); await refresh(); }
      else if(a==='refresh-batch') await pollBatch();
      else if(a==='retry-upload') { const entry=state.localFiles.find(f=>f.key===b.dataset.id); if(entry && state.batch && !state.batchBusy) { b.disabled=true; state.batchBusy=true; try { await uploadFile(entry); } finally { state.batchBusy=false; } await pollBatch(); } }
      else if(a==='cancel-upload') { const entry=state.localFiles.find(f=>f.key===b.dataset.id); if(entry && !entry.jobId && entry.status!=='uploading') { entry.status='cancelled'; drawBatch(); } }
      else if(a==='retry-job') confirmAction('Retry this scan?','The previous attempt may already have been billed. Retrying authorizes one more scan using the same batch model; no automatic fallback or confirmation.',async()=>{await api(`/api/jobs/${encodeURIComponent(b.dataset.id)}/retry`,{method:'POST',body:JSON.stringify({acknowledge_cost:true})});await pollBatch();},false);
      else if(a==='cancel-job') { b.disabled=true; try { await api(`/api/jobs/${encodeURIComponent(b.dataset.id)}/cancel`,{method:'POST',body:'{}'}); } finally { await pollBatch(); } }
      else if(a==='reload-record') {
        if (!window.confirm('Reload the latest server record? Your unsaved local edits will be discarded.')) return;
        const {id,kind}=editorContext, record=await api(`/api/${kind==='purchase'?'purchases':'drafts'}/${encodeURIComponent(id)}`);
        editor(record,kind==='purchase'?'purchase':record.status==='draft'?'draft':'confirmed-draft');
      }
      else if(a==='close'||a==='backdrop') close();
      else if(a==='logout') { await api('/api/logout',{method:'POST',body:'{}'}); clearTimeout(batchTimer); state.batch=null; state.localFiles=[]; writeKeys.clear(); state.tokens=null; state.session=null; close(); renderLogin(); }
      else if(a==='retry') await refresh();
      else if(a==='manual') editor();
      else if(a==='open-purchase') { const record=await api(`/api/purchases/${encodeURIComponent(b.dataset.id)}`); editor(record,'purchase'); }
      else if(a==='open-draft') { const record=await api(`/api/drafts/${encodeURIComponent(b.dataset.id)}`); editor(record,record.status==='draft'?'draft':'confirmed-draft'); }
      else if(a==='add-item') { $('#item-rows').insertAdjacentHTML('beforeend',itemRow()); updateReconciliation(); }
      else if(a==='remove-item') { b.closest('.item-row')?.remove(); updateReconciliation(); }
      else if(a==='delete-record') {
        const {kind,id,record}=editorContext;
        if (!window.confirm(`Delete this ${kind==='draft'?'draft':'purchase'}? It will leave normal lists and totals. Its audit history and private source are retained. This cannot be undone.`)) return;
        try { await api(`/api/${kind==='draft'?'drafts':'purchases'}/${encodeURIComponent(id)}`,{method:'DELETE',headers:versionHeaders(record)}); close(); notify('Record deleted; audit history retained.'); await refresh(); }
        catch(err) { if(err.status===409) conflictWarning(); throw err; }
      }
      else if(a==='page'||a==='draft-page') { const key=a==='page'?'page':'draftsPage'; state[key]+=Number(b.dataset.step); await refresh(); }
      else if(a==='month') {
        const m=b.dataset.month; state.month=m;
        const now=today().slice(0,7); const [y,n]=now.split('-').map(Number);
        const chosen=m==='last'?`${n===1?y-1:y}-${String(n===1?12:n-1).padStart(2,'0')}`:now;
        const base={...state.filters}; delete base.start; delete base.end;
        state.filters=m==='all'?base:{...base,...monthBounds(chosen)}; state.page=0; await refresh();
      }
      else if(a==='clear-filters') { state.filters={}; state.month='all'; state.page=0; await refresh(); }
      else if(a==='export') await download(b.dataset.format);
      else if(a==='select-model') { state.selectedModel=b.dataset.id; $('#model-results').innerHTML=modelResults(); }
      else if(a==='reload-models') { state.modelError=''; $('#model-results').innerHTML=loading(); await loadModels(); $('#model-results').innerHTML=modelResults(); }
      else if(a==='edit-person') personModal(b.dataset.id);
      else if(a==='revoke-token') confirmAction('Revoke agent token?','This agent will lose access immediately. Existing token secrets cannot be restored.',async()=>{await api(`/api/agent-tokens/${encodeURIComponent(b.dataset.id)}`,{method:'DELETE'});state.tokens=await api('/api/agent-tokens');draw();notify('Token revoked.');});
      else if(a==='reveal-token') { const input=$('#token-secret'); input.type=input.type==='password'?'text':'password'; b.textContent=input.type==='password'?'Reveal':'Hide'; b.setAttribute('aria-pressed',String(input.type==='text')); }
      else if(a==='copy-token') { await navigator.clipboard.writeText($('#token-secret')?.value||''); notify('Token copied. Save it securely in Vault.'); }
    } catch(err) { notify(err.message,true); }
  });
  document.addEventListener('input',e=>{
    if(e.target.closest('#editor-form')) {
      const row=e.target.closest('.item-row');
      if (row && e.target.name==='line_total') row.dataset.lineManual='true';
      else if (row && (e.target.name==='quantity'||e.target.name==='unit_price')) fillLineFromUnit(row);
      updateReconciliation();
    }
    if(e.target.id==='model-search') {
      state.modelQuery=e.target.value; clearTimeout(state.modelTimer);
      state.modelTimer=setTimeout(async()=>{const term=state.modelQuery; if (!$('#model-results')) return; $('#model-results').innerHTML=loading(); await loadModels(); if(term===state.modelQuery && $('#model-results')) $('#model-results').innerHTML=modelResults();},350);
    }
  });
  document.addEventListener('change',e=>{
    const form=e.target.closest('#scan-form');
    if(form && ['files','camera'].includes(e.target.name)) {
      const files=[...form.elements.files.files,...form.elements.camera.files];
      $('#file-selection').innerHTML=`<p class="fine ${files.length>20?'error-inline':'muted'}">${files.length} / 20 files selected</p><ul class="selection-list">${files.map(f=>`<li>${esc(f.name)} · ${(f.size/1048576).toFixed(2)} MiB</li>`).join('')}</ul>`;
    }
    if(e.target.closest('#token-form')) { const form=$('#token-form'); form.elements.agent_id.required=$$('input[name="scope"]:checked',form).some(i=>!i.value.endsWith(':read')); }
  });
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape' && overlay.innerHTML) { if($('#audit-layer')) closeAudit(); else close(); }
    if(e.key==='Tab' && overlay.innerHTML) {
      const dialog=$('#audit-layer') || $('.modal'), focusable=$$('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href]',dialog).filter(el=>el.getClientRects().length);
      if (!focusable.length) return;
      if(e.shiftKey && document.activeElement===focusable[0]) { e.preventDefault(); focusable.at(-1).focus(); }
      else if(!e.shiftKey && document.activeElement===focusable.at(-1)) { e.preventDefault(); focusable[0].focus(); }
    }
  });
  init();
})();