'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = value => Number.isFinite(value) ? '$' + value.toLocaleString('en-US', {minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
const pct = value => Number.isFinite(value) ? (value * 100).toFixed(2) + '%' : '—';
const num = value => Number.isFinite(value) ? value.toLocaleString('en-US',{maximumFractionDigits:2}) : '—';
const PAGE_SIZE = 25, STOCK_PAGE_SIZE = 12;
let stocks = [], selected = 'MSTR', info = null, data = null, selectionVersion = 0, chainVersion = 0, detailVersion = 0;
let stockPage = 0, page = 0, view = [], busy = false;
let favorites;
try { const saved = JSON.parse(localStorage.getItem('equityFavorites') || '["MSTR"]'); favorites = new Set(Array.isArray(saved) ? saved.filter(x=>typeof x==='string') : ['MSTR']); }
catch { favorites = new Set(['MSTR']); }

async function api(action, params = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 60000);
  try {
    const response = await fetch('/api/stock-options/' + action + '?' + new URLSearchParams(params), {signal:controller.signal});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || '行情读取失败，请稍后重试。');
    return result;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('读取行情超时，请点击刷新重试。');
    throw error;
  } finally { clearTimeout(timeout); }
}

function status(message, error = false) {
  $('status').textContent = message;
  $('status').className = 'notice' + (error ? ' error' : '');
}

function renderStocks() {
  const query = $('search').value.trim().toLowerCase(), type = $('assetType').value;
  const filtered = stocks.filter(s => (type === 'ALL' || s.type === type) &&
    (!$('favoritesOnly').checked || favorites.has(s.symbol)) &&
    (s.symbol + ' ' + s.name).toLowerCase().includes(query));
  filtered.sort((a,b) => Number(favorites.has(b.symbol)) - Number(favorites.has(a.symbol)) || a.symbol.localeCompare(b.symbol));
  const pages = Math.max(1, Math.ceil(filtered.length / STOCK_PAGE_SIZE));
  stockPage = Math.min(stockPage, pages - 1);
  $('stocks').innerHTML = filtered.slice(stockPage * STOCK_PAGE_SIZE, (stockPage + 1) * STOCK_PAGE_SIZE)
    .map(s => `<button class="stock-item" data-stock="${esc(s.symbol)}" aria-pressed="${selected===s.symbol}"><strong>${esc(s.symbol)}${favorites.has(s.symbol)?' · 已关注':''}</strong><span>${esc(s.name)}</span></button>`).join('') || '<p class="muted small">没有匹配的股票，可修改搜索或筛选条件。</p>';
  $('stockPage').textContent = `${stockPage+1} / ${pages} · ${filtered.length} 项`;
  $('stockPrev').disabled = stockPage === 0; $('stockNext').disabled = stockPage >= pages - 1;
  $('favorite').textContent = favorites.has(selected) ? '已关注 · 取消' : '关注';
  $('favorite').setAttribute('aria-pressed', String(favorites.has(selected)));
}

async function loadCatalog() {
  $('retryCatalog').disabled = true;
  try {
    const result = await api('catalog');
    stocks = result.symbols;
    $('catalogCount').textContent = `${stocks.filter(x=>x.type==='STOCK').length} 只股票 · ${stocks.filter(x=>x.type==='ETF').length} 只 ETF`;
    renderStocks();
    if (!info && stocks.length) {
      const requested = new URLSearchParams(location.search).get('symbol');
      await selectStock(stocks.some(x=>x.symbol===requested)?requested:stocks.some(x=>x.symbol==='MSTR')?'MSTR':stocks[0].symbol);
    }
  } catch (error) { $('catalogCount').textContent = '目录加载失败，请点击重试。'; status(error.message, true); }
  finally { $('retryCatalog').disabled = false; }
}

function resetData() {
  data = null; page = 0; renderRows();
  $('spot').textContent = '—'; $('contractCount').textContent = '—'; $('fetchedAt').textContent = '—'; $('market').textContent = '';
}

async function selectStock(symbol) {
  const version = ++selectionVersion; ++chainVersion;
  selected = symbol; info = null; busy = true; resetData();
  $('detail').close(); ++detailVersion;
  const stock = stocks.find(s=>s.symbol===symbol);
  if (stock && $('assetType').value !== 'ALL' && $('assetType').value !== stock.type) $('assetType').value = stock.type;
  $('stockTitle').textContent = symbol; $('stockName').textContent = stock?.name || '';
  $('official').href = 'https://www.binance.com/en/stocks/options/' + encodeURIComponent(stock?.assetCode || 'EQ_'+symbol);
  $('expiration').disabled = true; $('expiration').innerHTML = '<option>加载中…</option>';
  $('refresh').disabled = true; renderStocks();
  history.replaceState(null,'','/stock-options?symbol='+encodeURIComponent(symbol));
  status('正在读取 ' + symbol + ' 的到期日…');
  try {
    const result = await api('overview',{symbol});
    if (version !== selectionVersion) return;
    info = result;
    $('expiration').innerHTML = result.expirations.map((e,i)=>`<option value="${i}">${esc(e.expirationDate)}${e.rootSymbol!==symbol?' · 调整系列 '+esc(e.rootSymbol):''}</option>`).join('');
    if (!result.expirations.length) {
      $('expiration').innerHTML = '<option>暂无可用到期日</option>';
      status('币安目录支持该股票期权，但当前未返回可用到期日。'); return;
    }
    // Prefer the next expiry over same-day expiry; user can still inspect either.
    const todayET = new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
    const next = result.expirations.findIndex(e=>e.expirationDate>todayET && e.rootSymbol===symbol);
    $('expiration').value = String(next >= 0 ? next : 0);
    $('expiration').disabled = false;
    await loadChain(version);
  } catch (error) { if (version === selectionVersion) { $('expiration').innerHTML='<option>加载失败</option>'; status(error.message,true); } }
  finally { if (version === selectionVersion) { busy=false; $('refresh').disabled=false; } }
}

async function loadChain(selection = selectionVersion) {
  const expiry = info?.expirations[Number($('expiration').value)];
  if (!expiry) return;
  const version = ++chainVersion; busy=true; resetData();
  $('detail').close(); ++detailVersion;
  $('refresh').disabled = true; status('正在读取合约与买卖报价…');
  try {
    const result = await api('chain',{symbol:selected,expiration:expiry.expirationDate,root:expiry.rootSymbol});
    if (selection !== selectionVersion || version !== chainVersion) return;
    data = result;
    $('spot').textContent = money(result.price); $('contractCount').textContent = num(result.rows.length);
    $('fetchedAt').textContent = new Date(result.fetchedAt).toLocaleString('zh-CN',{hour12:false});
    const phase = result.market.phase;
    $('market').textContent = phase === 'MARKET_OPEN'
      ? '美股常规交易时段。接口未提供报价时间，成交价格以币安订单预览为准。'
      : phase === 'UNKNOWN' ? '交易时段暂无法确认。以下为报价快照，不能确认当前可成交。'
      : '当前非美股常规交易时段：报价可能来自上一交易日，不能视为当前可成交价格。';
    status(result.missingCount ? `${result.missingCount} 个合约缺少报价，已标记且不计算收益。` : '行情已读取。按买一价估算每张权利金，报价缓存 30 秒。');
    renderRows();
  } catch (error) { if (selection===selectionVersion && version===chainVersion) { resetData(); status(error.message,true); } }
  finally { if (selection===selectionVersion && version===chainVersion) { busy=false; $('refresh').disabled=false; } }
}

function renderRows() {
  const feeText = $('fee').value.trim(), fee = feeText === '' ? NaN : Number(feeText);
  const feeValid = Number.isFinite(fee) && fee >= 0;
  $('fee').setCustomValidity(feeValid?'':'请输入不小于 0 的费用。');
  $('fee').setAttribute('aria-invalid',String(!feeValid));
  const now = Date.now();
  view = (data?.rows || []).filter(r=>r.type===$('strategy').value)
    .map(r=>({...r,calc:EquityMath.calculate(r,data.price,fee,now)}))
    .filter(r=>(!$('otmOnly').checked || r.calc.otm>0) && (!$('bidOnly').checked || r.calc.eligible));
  const sort = $('sort').value;
  view.sort((a,b)=>{
    const distance = Math.abs(a.calc.otm??Infinity)-Math.abs(b.calc.otm??Infinity);
    if (sort==='strike') return (a.strike??Infinity)-(b.strike??Infinity);
    if (sort==='distance') return distance;
    return ((b.calc[sort]??-Infinity)-(a.calc[sort]??-Infinity)) || distance;
  });
  const pages = Math.max(1,Math.ceil(view.length/PAGE_SIZE)); page=Math.min(page,pages-1);
  $('rows').innerHTML=view.slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE).map(r=>{
    const c=r.calc;
    return `<tr><td><strong>${money(r.strike)}</strong><span>${pct(c.otm)} ${c.otm>0?'虚值':c.otm===0?'平值':'实值'}${r.adjusted?' · 调整合约':''}</span></td>
      <td>${money(r.bid)} / ${money(r.ask)}${r.quoteMissing?'<span>报价缺失</span>':''}</td><td>${num(r.bidSize)} / ${num(r.askSize)}</td>
      <td>${money(c.net)}</td><td>${money(c.capital)}<span>${r.type==='CALL'?num(r.multiplier)+' 股市值':'现金担保'}</span></td>
      <td class="${c.yield>=0?'positive':'negative'}">${pct(c.yield)}</td><td>${pct(c.apr)}${c.days!==null&&c.days<1?'<span>不足一天</span>':''}</td>
      <td>${pct(r.iv)}<span>Δ ${num(r.delta)}</span></td><td><button data-detail="${esc(r.symbol)}">详情</button></td></tr>`;
  }).join('');
  $('empty').hidden=view.length>0;
  $('empty').textContent = !feeValid ? '请输入有效的每张预估费用（不小于 0）。' : !data ? '选择股票和到期日后显示合约。' : '当前条件没有合约。可切换到期日或放宽筛选。';
  $('resultCount').textContent=`${view.length} 个合约`;
  $('page').textContent=`${page+1} / ${pages}`;
  $('prev').disabled=page===0; $('next').disabled=page>=pages-1;
}

async function showDetail(optionSymbol) {
  const row=view.find(r=>r.symbol===optionSymbol); if(!row || !data) return;
  const version=++detailVersion;
  const c=row.calc;
  $('detailBody').innerHTML=`<h3>${esc(optionSymbol)}</h3><dl>
    <dt>剩余时间</dt><dd>${num(c.days)} 天</dd><dt>每张毛权利金</dt><dd>${money(c.premium)}</dd>
    <dt>每张净权利金（估算）</dt><dd>${money(c.net)}</dd><dt>${row.type==='PUT'?'接股盈亏平衡价':'持股组合盈亏平衡价'}</dt><dd>${money(c.breakeven)}</dd>
    ${row.type==='CALL'?`<dt>被行权总收益率</dt><dd>${pct(c.assignedReturn)}</dd>`:''}
    <dt>成交量 / 持仓量</dt><dd>${num(row.volume)} / ${num(row.openInterest)}</dd></dl>
    <p class="small muted">${row.type==='CALL'?'持股收益按当前参考市值计算，不是你的历史成本。被行权总收益包含股票价差与净权利金。':'现金担保金额不扣除待收权利金。被指派时需按行权价买入股票。'}</p>
    <p id="contractCheck" class="notice">正在核对币安合约规则…</p>`;
  $('detail').showModal();
  try {
    const result=await api('contract',{symbol:selected,contract:optionSymbol});
    if(version!==detailVersion) return;
    const contract=result.contract;
    const standard=!row.adjusted && contract.rs===selected && contract.deliverables?.length===1 && contract.deliverables[0].type==='EQUITY' && contract.deliverables[0].symbol===selected && Number(contract.deliverables[0].amount)===row.multiplier;
    $('contractCheck').textContent = `合约乘数 ${contract.mul}；交割：${(contract.deliverables||[]).map(x=>`${x.amount} ${x.symbol || x.type}`).join(' + ')}。` +
      (!standard?'调整合约或非标准交割：请在币安核对，勿套用标准合约收益。':contract.olc?'该合约仅限平仓，不可新开仓。':'标准股票交割合约。临近到期可能进入仅限平仓，实际可卖数量及开仓状态以币安为准。');
  } catch(error) { if(version===detailVersion) $('contractCheck').textContent='合约规则核对失败：'+error.message; }
}

$('stocks').addEventListener('click',e=>{const b=e.target.closest('[data-stock]'); if(b) selectStock(b.dataset.stock);});
$('rows').addEventListener('click',e=>{const b=e.target.closest('[data-detail]'); if(b) showDetail(b.dataset.detail);});
for(const id of ['search','assetType','favoritesOnly']) $(id).addEventListener(id==='search'?'input':'change',()=>{stockPage=0;renderStocks();});
for(const id of ['strategy','otmOnly','bidOnly','sort','fee']) $(id).addEventListener(id==='fee'?'input':'change',()=>{page=0;renderRows();});
$('favorite').addEventListener('click',()=>{favorites.has(selected)?favorites.delete(selected):favorites.add(selected);try{localStorage.setItem('equityFavorites',JSON.stringify([...favorites]));}catch{status('浏览器无法保存关注列表，本次会话仍可使用。');}renderStocks();});
$('expiration').addEventListener('change',()=>loadChain());
$('refresh').addEventListener('click',()=>{if(!busy) info?.expirations.length?loadChain():selectStock(selected);});
$('retryCatalog').addEventListener('click',loadCatalog);
$('stockPrev').addEventListener('click',()=>{stockPage--;renderStocks();});
$('stockNext').addEventListener('click',()=>{stockPage++;renderStocks();});
$('prev').addEventListener('click',()=>{page--;renderRows();});
$('next').addEventListener('click',()=>{page++;renderRows();});
$('closeDetail').addEventListener('click',()=>{$('detail').close();++detailVersion;});
$('detail').addEventListener('cancel',()=>{++detailVersion;});
setInterval(()=>{if(!document.hidden) loadCatalog();},3600000);
loadCatalog();
