import http from 'http';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { execFile, spawn } from 'child_process';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = process.env.PORT || 3000;
const PUBLIC_DIR = path.join(__dirname, 'public');
const SCREENER_TOOL = path.join(__dirname, 'westock-tool.js');
const T_ENGINE = path.join(__dirname, 't_engine', 't_engine.py');

// 做T引擎 Python 解释器: 优先 env PYTHON_BIN, 否则 Windows=python / 其他=python3
function findPython() {
  if (process.env.PYTHON_BIN) return process.env.PYTHON_BIN;
  return process.platform === 'win32' ? 'python' : 'python3';
}
// 做T引擎代理: 设了 T_PROXY 才走代理, 否则直连(服务器直连最快)
const T_PROXY = (process.env.T_PROXY || '').trim();

function num(v, def) {
  const n = Number(v);
  return Number.isFinite(n) ? n : def;
}

// ---------- 选股器: 调用 westock-tool.js ----------
function runScreen({ yld, rsi, pct, drawdown }) {
  return new Promise((resolve, reject) => {
    const drawFactor = (1 - drawdown / 100).toFixed(4);
    const expr = `intersect([PE_TTM > 0, DividendRatioTTM >= ${yld}, RSI_24 < ${rsi}, PE_TTMPct10Y < ${pct}, ClosePrice <= Week52High * ${drawFactor}])`;
    const args = [SCREENER_TOOL, 'filter', expr, '--orderby', 'DividendRatioTTM', '--desc', '--limit', '500', '--raw'];
    execFile('node', args, { maxBuffer: 32 * 1024 * 1024, windowsHide: true }, (err, stdout, stderr) => {
      if (err) return reject(new Error(stderr || err.message));
      let rows;
      try { rows = JSON.parse(stdout); } catch (e) { return reject(new Error('结果解析失败')); }
      if (!Array.isArray(rows)) rows = [];
      resolve(rows);
    });
  });
}

function sinaId(code) { return code.replace(/^(sh|sz|hk|us)/i, ''); }

async function fetchText(url, timeoutMs = 8000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: ctrl.signal, headers: { 'User-Agent': 'Mozilla/5.0' } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return Buffer.from(await r.arrayBuffer());
  } finally { clearTimeout(t); }
}

async function getDividendHistory(code) {
  const id = sinaId(code);
  const url = `https://money.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/${id}.phtml`;
  const buf = await fetchText(url, 9000);
  const html = new TextDecoder('gbk').decode(buf);
  const rows = [...html.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/g)].map(m =>
    [...m[1].matchAll(/<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/g)].map(c => c[1].replace(/<[^>]+>/g, '').replace(/&nbsp;/g, '').trim())
  );
  const paidYears = new Set();
  let latestDps = null, latestDate = '';
  for (const cells of rows) {
    if (cells.length < 5) continue;
    const date = cells[0];
    const div = num(cells[3], NaN);
    const status = cells[4] || '';
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) continue;
    if (!status.includes('实施') || !Number.isFinite(div) || div <= 0) continue;
    const year = parseInt(date.slice(0, 4), 10);
    paidYears.add(year);
    if (date > latestDate) { latestDate = date; latestDps = div / 10; }
  }
  const years = [...paidYears].sort((a, b) => b - a);
  let run = 0;
  if (years.length) {
    let expect = years[0];
    for (const y of years) {
      if (y === expect) { run++; expect--; } else if (y < expect) break;
    }
  }
  return { consecYears: run, totalPaidYears: years.length, latestDps, ok: years.length > 0 };
}

async function getValuation(code) {
  const id = sinaId(code);
  const url = `https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEANALYSIS_DET&columns=ALL&filter=(SECURITY_CODE%3D%22${id}%22)&pageSize=1&source=WEB&client=WEB`;
  const buf = await fetchText(url, 9000);
  const j = JSON.parse(buf.toString('utf8'));
  const rec = j && j.result && j.result.data && j.result.data[0];
  if (!rec) throw new Error('无数据');
  return { pcfOcfTtm: num(rec.PCF_OCF_TTM, NaN) };
}

async function enrich(stock) {
  const yld = num(stock.DividendRatioTTM, NaN);
  const pe = num(stock.PE_TTM, NaN);
  const out = { ...stock, consecYears: null, consec5: false, latestDps: null, payoutRatio: null, fcfCover: null, divNote: [] };
  if (Number.isFinite(yld) && Number.isFinite(pe) && pe > 0) out.payoutRatio = +(yld * pe).toFixed(1);
  const [div, val] = await Promise.allSettled([getDividendHistory(stock.code), getValuation(stock.code)]);
  if (div.status === 'fulfilled' && div.value.ok) {
    out.consecYears = div.value.consecYears;
    out.consec5 = div.value.consecYears >= 5;
    out.latestDps = div.value.latestDps;
  } else out.divNote.push('新浪分红历史获取失败');
  if (val.status === 'fulfilled' && Number.isFinite(val.value.pcfOcfTtm) && val.value.pcfOcfTtm > 0) {
    if (Number.isFinite(yld) && yld > 0) out.fcfCover = +(100 / (val.value.pcfOcfTtm * yld)).toFixed(2);
  } else out.divNote.push('东财市现率获取失败');
  return out;
}

// ---------- 稳健增长选股器 ----------
// 条件: PE_TTM>0 + 最近5个报告期 归母净利润>0 且 净利润同比>0 且 经营现金流>0 + 连续5年分红
// 数据链路: 粗筛池=westock(腾讯自选股) / 财务三条件=东财业绩报表批量 / 连续派息=新浪(复用分红历史)
const EM_API = 'https://datacenter-web.eastmoney.com/api/data/v1/get';
const FIN_TTL = 2 * 3600 * 1000;   // 东财财务批量缓存 2 小时(年报数据, 变化极少)
const DIV_TTL = 12 * 3600 * 1000;  // 新浪派息核验缓存 12 小时
const POOL_TTL = 10 * 60 * 1000;   // westock 池缓存 10 分钟
const STEADY_TTL = 5 * 60 * 1000;  // 整条流水线结果缓存 5 分钟
let finCache = null;               // { t, chk: Map(code6 -> 财务校验结果) }
let poolCache = null;              // { t, rows }
let steadyCache = null;            // { t, data: { hits, candCount, poolCount } }
const divCache = new Map();        // code6 -> { t, consecYears }

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// 并发受限的 map: 防新浪限速
async function mapLimit(items, limit, fn) {
  const ret = new Array(items.length);
  let i = 0;
  async function w() {
    while (i < items.length) {
      const j = i++;
      try { ret[j] = await fn(items[j]); } catch (e) { ret[j] = null; }
    }
  }
  const n = Math.max(1, Math.min(limit, items.length));
  await Promise.all(Array.from({ length: n }, w));
  return ret.filter(x => x !== null);
}

// 东财通用分页拉取(4路并发, 断点重试)
async function emFetchPages(reportName, filterStr, columns) {
  const mkUrl = p => `${EM_API}?reportName=${reportName}&columns=${encodeURIComponent(columns)}&filter=${encodeURIComponent(filterStr)}&pageSize=500&pageNumber=${p}&source=WEB&client=WEB`;
  const j0 = JSON.parse((await fetchText(mkUrl(1), 25000)).toString('utf8'));
  const res = j0 && j0.result;
  if (!res || !Array.isArray(res.data)) throw new Error('东财数据获取失败: ' + ((j0 && j0.message) || '无结果'));
  const pages = res.pages || 1;
  const rows = res.data.slice();
  let idx = 2;
  async function worker() {
    while (idx <= pages) {
      const p = idx++;
      for (let a = 0; a < 3; a++) {
        try {
          const jj = JSON.parse((await fetchText(mkUrl(p), 25000)).toString('utf8'));
          const dd = jj.result && jj.result.data;
          if (Array.isArray(dd)) { rows.push(...dd); break; }
        } catch (e) { if (a === 2) break; }
        await sleep(400);
      }
    }
  }
  await Promise.all([worker(), worker(), worker(), worker()]);
  return rows;
}

// 财务校验(2026-10-09口径): 近4个年报(12-31) + 最近一期已披露财报(中报/三季报/一季报, 若无则即最新年报)
// 每一期都须满足 归母>0 & 同比>0 & 每股经营现金流>0
function computeFinChk(rows) {
  const best = new Map(); // code|date -> rec (修订去重, 取 UPDATE_DATE 最新)
  for (const r of rows) {
    const c = r.SECURITY_CODE;
    const d = String(r.REPORTDATE || '').slice(0, 10);
    if (!c || !/^\d{4}-\d{2}-\d{2}$/.test(d)) continue;
    const k = c + '|' + d;
    const old = best.get(k);
    if (!old || String(r.UPDATE_DATE || '') >= String(old.UPDATE_DATE || '')) best.set(k, r);
  }
  const byCode = new Map();
  for (const r of best.values()) {
    const c = r.SECURITY_CODE;
    let arr = byCode.get(c);
    if (!arr) byCode.set(c, arr = []);
    arr.push(r);
  }
  const chk = new Map();
  const pass = r => {
    const pn = num(r.PARENT_NETPROFIT, NaN), yoy = num(r.SJLTZ, NaN), ocf = num(r.MGJYXJJE, NaN);
    return pn > 0 && yoy > 0 && ocf > 0;
  };
  for (const [c, arr] of byCode) {
    arr.sort((a, b) => String(b.REPORTDATE).localeCompare(String(a.REPORTDATE)));
    const annuals = arr.filter(r => String(r.REPORTDATE).slice(5, 10) === '12-31');
    if (annuals.length < 4) continue;                 // 4个年报是底线
    const latest = arr[0];                             // 最近一期(含非年报; 未披露则=最新年报)
    const req = [latest];
    for (const a of annuals) if (a !== latest && req.length < 5) req.push(a);
    const yoys = [];
    let ok = true;
    for (const r of req) {
      if (!pass(r)) { ok = false; break; }
      yoys.push(+num(r.SJLTZ, 0).toFixed(2));
    }
    if (!ok) continue;
    chk.set(c, {
      yoyList: yoys,
      yoyMin: Math.min(...yoys),
      yoyAvg: +(yoys.reduce((a, b) => a + b, 0) / yoys.length).toFixed(2),
      reports: req.map(r => String(r.REPORTDATE).slice(0, 10)),
      latestOcf: num(latest.MGJYXJJE, NaN),
    });
  }
  return chk;
}

async function getFinChk() {
  if (finCache && Date.now() - finCache.t < FIN_TTL) return finCache.chk;
  // 精确逐期拉取: 近7个年报期(取4用,余量防缺失/上市晚) + 当年3个非年报期(最近一期候选, 未披露=空)
  const y = bjNow().getUTCFullYear();
  const dates = [];
  for (let i = 1; i <= 7; i++) dates.push(`${y - i}-12-31`);
  dates.push(`${y}-09-30`, `${y}-06-30`, `${y}-03-31`);
  const chunks = [];
  for (const d of dates) {
    try {
      chunks.push(await emFetchPages('RPT_LICO_FN_CPD', `(REPORTDATE='${d}')`,
        'SECURITY_CODE,REPORTDATE,PARENT_NETPROFIT,SJLTZ,MGJYXJJE,UPDATE_DATE'));
    } catch (e) { /* 单期失败不阻断 */ }
  }
  const rows = chunks.flat();
  if (!rows.length) throw new Error('东财业绩报表无数据');
  const chk = computeFinChk(rows);
  finCache = { t: Date.now(), chk };
  return chk;
}

function westockFilter(expr, limit = 5000) {
  return new Promise((resolve, reject) => {
    execFile('node', [SCREENER_TOOL, 'filter', expr, '--limit', String(limit), '--raw'],
      { maxBuffer: 32 * 1024 * 1024, windowsHide: true, timeout: 300000 },
      (err, stdout, stderr) => {
        if (err) return reject(new Error(String(stderr || err.message || err)));
        let rows;
        try { rows = JSON.parse(stdout); } catch (e) { return reject(new Error('westock 结果解析失败')); }
        resolve(Array.isArray(rows) ? rows : []);
      });
  });
}

async function getSteadyPool() {
  if (poolCache && Date.now() - poolCache.t < POOL_TTL) return poolCache.rows;
  const rows = await westockFilter('intersect([PE_TTM > 0, DividendRatioTTM > 0])');
  poolCache = { t: Date.now(), rows };
  return rows;
}

// 连续派息年数(复用新浪分红历史, 12h缓存; 拉取失败返回 null 不缓存)
async function getDivConsec(code) {
  const c6 = sinaId(code);
  const hit = divCache.get(c6);
  if (hit && Date.now() - hit.t < DIV_TTL) return hit.consecYears;
  try {
    const v = await getDividendHistory(code);
    const cy = v.ok ? v.consecYears : 0;
    divCache.set(c6, { t: Date.now(), consecYears: cy });
    return cy;
  } catch (e) { return null; }
}

async function runSteadyAll() {
  const [pool, chk] = await Promise.all([getSteadyPool(), getFinChk()]);
  const cand = pool.filter(s => chk.has(sinaId(s.code)));
  const enriched = await mapLimit(cand, 8, async s => {
    const k = chk.get(sinaId(s.code));
    const cy = await getDivConsec(s.code);
    return {
      code: s.code, name: s.name,
      PE_TTM: num(s.PE_TTM, NaN),
      DividendRatioTTM: num(s.DividendRatioTTM, NaN),
      ClosePrice: num(s.ClosePrice, NaN),
      ChangePCT: num(s.ChangePCT, NaN),
      consecYears: cy, consec5: cy !== null && cy >= 5, divFailed: cy === null,
      yoyList: k.yoyList, yoyMin: k.yoyMin, yoyAvg: k.yoyAvg, reports: k.reports, latestOcf: k.latestOcf,
    };
  });
  const hits = enriched.filter(x => x.consec5);
  hits.sort((a, b) => (a.ClosePrice || 1e9) - (b.ClosePrice || 1e9));   // 股价从低到高
  const divFailed = enriched.filter(x => x.divFailed).length;
  return { hits, candCount: cand.length, poolCount: pool.length, divFailed };
}

// ---------- 做T助手: 调用 Python 引擎 ----------
function runT(code, shares, cost, comm) {
  return new Promise((resolve, reject) => {
    const py = findPython();
    const args = T_PROXY ? [T_ENGINE, code, '--proxy', T_PROXY] : [T_ENGINE, code, '--no-proxy'];
    if (shares) args.push('--shares=' + parseInt(shares, 10));
    if (cost) args.push('--cost=' + parseFloat(cost));
    if (comm) args.push('--comm=' + parseFloat(comm));
    const env = { ...process.env };
    if (T_PROXY) { env.http_proxy = T_PROXY; env.https_proxy = T_PROXY; }
    execFile(py, args, { maxBuffer: 16 * 1024 * 1024, windowsHide: true, env, timeout: 25000 },
      (err, stdout, stderr) => {
        if (err) {
          const msg = (stderr || err.message || '').toString();
          if (/ENOENT/.test(msg) || /spawn .* ENOENT/.test(msg)) {
            return reject(new Error(`Python 引擎未找到(${py})。请在环境变量 PYTHON_BIN 指定 python 路径，或在该服务器安装 Python3。`));
          }
          return reject(new Error(msg || '做T引擎执行失败'));
        }
        try { resolve(JSON.parse(stdout)); }
        catch (e) { reject(new Error('做T引擎输出解析失败: ' + stdout.slice(0, 200))); }
      });
  });
}

// ---------- 做T引擎串行队列: 防并发请求触发上游限速(连接被掐) ----------
let tQueue = Promise.resolve();
let tLastStart = 0;
const T_GAP_MS = 350;

function runTQueued(code, shares, cost, comm) {
  const run = tQueue.then(async () => {
    const wait = Math.max(0, tLastStart + T_GAP_MS - Date.now());
    if (wait) await new Promise(r => setTimeout(r, wait));
    tLastStart = Date.now();
    return runT(code, shares, cost, comm);
  });
  tQueue = run.catch(() => {});
  return run;
}

// ---------- 缓存 ----------
const cache = new Map();
const CACHE_TTL = 5 * 60 * 1000;
const tCache = new Map();          // 做T短缓存: 分时数据为分钟级, 20s内同参数直接复用
const T_CACHE_TTL = 20 * 1000;

// ---------- 信号推送: 微信(Server酱/PushPlus) + 邮件, 服务器端巡检(网页关了也能推) ----------
const NOTIFY_FILE = path.join(__dirname, 'notify_config.json');
let notifyCfg = { wx: { provider: 'none', key: '' }, email: { host: 'smtp.qq.com', port: 465, user: '', pass: '', to: '' }, bark: { enabled: false, server: '', key: '', group: '股票工具箱' }, codes: [] };
try { Object.assign(notifyCfg, JSON.parse(fs.readFileSync(NOTIFY_FILE, 'utf8'))); } catch (e) {}
function saveNotifyCfg(cfg) {
  notifyCfg = cfg;
  try { fs.writeFileSync(NOTIFY_FILE, JSON.stringify(cfg, null, 2)); } catch (e) {}
}
function readBody(req) {
  return new Promise(resolve => {
    let b = '';
    req.on('data', c => { b += c; if (b.length > 1e6) req.destroy(); });
    req.on('end', () => resolve(b));
  });
}

async function sendWx(title, body) {
  const wx = notifyCfg.wx || {};
  if (!wx.provider || wx.provider === 'none' || !wx.key) return { ok: false, skipped: true };
  try {
    let j;
    if (wx.provider === 'serverchan') {
      const r = await fetch(`https://sctapi.ftqq.com/${encodeURIComponent(wx.key)}.send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ title, desp: body }).toString(),
      });
      j = await r.json();
      return { ok: j.code === 0, raw: j };
    }
    // pushplus
    const r = await fetch('https://www.pushplus.plus/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: wx.key, title, content: body, template: 'txt' }),
    });
    j = await r.json();
    return { ok: j.code === 200, raw: j };
  } catch (e) {
    return { ok: false, err: String(e.message || e) };
  }
}

// Bark 推送(iOS App 自建/公共服务器): POST <服务器>/<key> 表单 title/body/group/ttl
async function sendBark(title, body) {
  const bk = notifyCfg.bark || {};
  if (!bk.enabled || !bk.server || !bk.key) return { ok: false, skipped: true };
  const base = String(bk.server).replace(/\/+$/, '');
  try {
    const r = await fetch(`${base}/${encodeURIComponent(bk.key)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ title, body, group: bk.group || '股票工具箱', ttl: '0' }).toString(),   // ttl=0: 通知持久保留(用户要求, 不10分钟自动清除)
    });
    const j = await r.json().catch(() => ({}));
    return { ok: r.ok && j.code === 200, raw: j, http: r.status };
  } catch (e) {
    return { ok: false, err: String(e.message || e) };
  }
}

async function sendEmail(title, body) {
  const em = notifyCfg.email || {};
  if (!em.user || !em.pass || !em.to) return { ok: false, skipped: true };
  return new Promise(resolve => {
    const proc = spawn(findPython(), [path.join(__dirname, 'notify_email.py')], { windowsHide: true });
    let out = '', err = '';
    proc.stdout.on('data', c => { out += c; });
    proc.stderr.on('data', c => { err += c; });
    proc.on('close', code => {
      try { resolve(JSON.parse(out)); }
      catch (e) { resolve({ ok: false, err: (err || `exit ${code}`).slice(0, 200) }); }
    });
    proc.stdin.end(JSON.stringify({
      host: em.host || 'smtp.qq.com', port: +(em.port || 465),
      user: em.user, pass: em.pass, to: em.to, subject: title, body,
    }));
    setTimeout(() => { try { proc.kill(); } catch (e) {} }, 40000);
  });
}

// 信号巡检: 交易时段每60秒过一遍监测股
// v2(2026-10-09): 修漏报——①去重键曾缺时间导致"每天每方向只推首发"(第2次起的信号被吃);
// ②由"只看瞬时live"改为"扫描当日信号列表+回看窗", 轮询间隙闪现即灭的信号也能补推;
// ③邮件失败自动重试一次; ④推送间节流防QQ邮箱频率限制连锁失败。
const pushed = new Map();        // code|date|type|time -> epochMs (落盘持久化, 防重启重推)
const openT = new Map();         // code|date|SELL_T|time -> {price,targetY,stopY,idx,state} 反T回补闭环(同样持久化)
const pushedFile = path.join(__dirname, 'pushed_state.json');
let pushStateDate = '';
try {
  const st = JSON.parse(fs.readFileSync(pushedFile, 'utf8'));
  const todayStr = bjNow().toDateString();
  if (st.date === todayStr) {   // 只恢复当日状态, 隔日作废
    if (st.pushed) for (const [k, v] of Object.entries(st.pushed)) pushed.set(k, v);
    if (st.openT) for (const [k, v] of Object.entries(st.openT)) openT.set(k, v);
    pushStateDate = st.date;
  }
} catch (e) {}
let savePushedTimer = null;
function savePushed() {
  if (savePushedTimer) return;
  savePushedTimer = setTimeout(() => {
    savePushedTimer = null;
    try { fs.writeFileSync(pushedFile, JSON.stringify({ date: bjNow().toDateString(), pushed: Object.fromEntries(pushed), openT: Object.fromEntries(openT) })); } catch (e) {}
  }, 500);
}
const fetchErr = {};             // code -> {n, last} 数据抓取失败退避(避免持续断连时轰炸)
const LOOKBACK_MIN = 20;         // 回看窗(分钟): 只推还来得及跟的信号
const PUSH_THROTTLE_MS = 4000;   // 连续推送节流
let pushedDate = '';
function bjNow() { return new Date(Date.now() + 8 * 3600 * 1000); }
function tradingNowSrv() {
  const d = bjNow();
  const day = d.getUTCDay();
  if (day === 0 || day === 6) return false;
  const m = d.getUTCHours() * 60 + d.getUTCMinutes();
  return (m >= 570 && m <= 690) || (m >= 780 && m <= 900);   // 9:30-11:30 / 13:00-15:00
}
async function sendEmailRetry(title, body) {
  let r = await sendEmail(title, body);
  if (!r.ok && !r.skipped) { await new Promise(res => setTimeout(res, 3000)); r = await sendEmail(title, body); }
  return r;
}
// 三路并发推送: 微信 + 邮件(带重试) + Bark(iOS)
async function pushAll(title, body) {
  const [wx, em, bk] = await Promise.all([sendWx(title, body), sendEmailRetry(title, body), sendBark(title, body)]);
  return { wx, em, bk };
}
function tag(r) { return r.skipped ? '未配置' : (r.ok ? '已发' : '失败:' + String(r.raw && r.raw.code || r.http || r.err || '').slice(0, 40)); }
async function monitorTick() {
  if (!tradingNowSrv()) return;
  const bj = bjNow();
  const today = bj.toDateString();
  if (pushStateDate && pushStateDate !== today) { pushed.clear(); openT.clear(); pushStateDate = today; savePushed(); }
  if (pushedDate !== today) { pushedDate = today; }
  const nowMin = bj.getUTCHours() * 60 + bj.getUTCMinutes();
  const codes = (notifyCfg.codes || []).slice(0, 10);
  if (!codes.length) return;
  for (const code of codes) {
    try {
      const d = await runTQueued(code, null, null, null);
      if (!d || d.error) {
        const fe = fetchErr[code] = fetchErr[code] || { n: 0, last: 0 };
        if (!fe.last) {   // 断连只记日志(用户要求: 行情异常不推邮箱/Bark), 每小时最多记一次
          console.log(`[行情异常] ${code} ${String((d && d.error) || '未知').slice(0, 80)} 巡检退避重试`);
          fe.last = Date.now();
        } else if (Date.now() - fe.last > 3600e3) { fe.last = 0; }
        continue;
      }
      fetchErr[code] = { n: 0, last: 0 };
      // ---- 反T回补闭环: 注册40个交易分钟内卖点 -> 止盈/止损/到期 自动提醒(每卖点仅一次) ----
      const series = d.series || {};
      const nBars = (series.times || []).length;
      const epsFen = (d.cost && d.cost.breakevenFen) || 2;
      const targetY = ((d.cost && d.cost.suggestFen) || 2) / 100;
      const stopY = Math.max(0.05, 2 * epsFen / 100);
      const curPx = d.curPrice;
      for (const s of (d.signals || [])) {
        if (s.type !== 'SELL_T' || !/^\d{2}:\d{2}$/.test(s.time || '')) continue;
        const idx = (series.times || []).indexOf(s.time);
        if (idx < 0) continue;
        const ageBars = nBars - 1 - idx;
        if (ageBars > 15) continue;   // 只登记15分钟内仍新鲜的卖点; 过老的错过提醒时机, 不再注册
        const k = `${code}|${today}|${s.type}|${s.time}`;
        if (!openT.has(k)) openT.set(k, { price: s.price, targetY, stopY, idx, ageBars, state: 'PENDING' });
      }
      savePushed();
      for (const [k, o] of openT) {
        if (!k.startsWith(code + '|' + today + '|SELL_T|') || o.state !== 'PENDING') continue;
        const sigTm = k.split('|')[3];
        let msg = null, st = null;
        const elapsed = nBars - 1 - o.idx;   // 交易分钟(跨午休准确)
        if (curPx >= o.price + o.stopY) { st = 'SL'; msg = `🛑 止损买回: 现价${curPx} 已破止损线${(o.price + o.stopY).toFixed(2)}(卖点${o.price}+${Math.round(o.stopY * 100)}分)。反T${sigTm}卖错, 按纪律买回认小亏, 硬扛只会更亏`; }
        else if (curPx <= o.price - o.targetY) { st = 'TP'; msg = `✅ 可获利买回: 现价${curPx} 回落到目标区${(o.price - o.targetY).toFixed(2)}以下(卖点${o.price})。买回完成一次T, 净赚约${Math.round((o.targetY * 100 - epsFen) * ((d.cost && d.cost.shares) || 0))}元`; }
        else if (elapsed >= 32) { st = 'EXP'; const diffFen = (curPx - o.price) * 100; msg = `⏰ 回补到期: 卖点${sigTm}已${elapsed}个交易分钟未回落到位, 现价${curPx}(较卖价${diffFen >= 0 ? '+' : ''}${diffFen.toFixed(0)}分)。按纪律市价买回, 不留隔夜敞口赌回落`; }
        if (!msg) { o.ageBars = elapsed; continue; }
        o.state = st;
        savePushed();
        const title = `🔄 ${d.name || code}(${code}) 反T回补提醒 ${sigTm} ${st === 'SL' ? '🛑止损' : st === 'TP' ? '✅止盈' : '⏰到期'}`;
        const pr2 = await pushAll(title, msg);
        console.log(`[回补提醒] ${title} wx=${tag(pr2.wx)} email=${tag(pr2.em)} bark=${tag(pr2.bk)}`);
        await new Promise(res => setTimeout(res, PUSH_THROTTLE_MS));
      }
      if (openT.size > 400) { for (const [k, v] of openT) if (v.state !== 'PENDING') openT.delete(k); }
      // ---- 信号推送: 当日信号列表 + 实时(去重键含具体时刻, 同方向第N次也推) ----
      const daySigs = [...(d.signals || [])];
      const lv = d.live || {};
      const curHHMM = String(bj.getUTCHours()).padStart(2, '0') + ':' + String(bj.getUTCMinutes()).padStart(2, '0');
      if (lv.triggered && lv.action) daySigs.push({ time: curHHMM, price: d.curPrice, type: lv.action, level: lv.level, strength: lv.strength, fillText: lv.fillText, stopText: lv.stopText, advice: lv.advice });
      daySigs.sort((a, b) => (b.time || '').localeCompare(a.time || ''));
      let sentThisRound = false;
      for (const s of daySigs) {
        if (!s.time || !/^\d{2}:\d{2}$/.test(s.time)) continue;
        const sigMin = parseInt(s.time.slice(0, 2), 10) * 60 + parseInt(s.time.slice(3, 5), 10);
        if (nowMin - sigMin > LOOKBACK_MIN) continue;          // 超回看窗=已来不及跟, 不轰炸
        const key = `${code}|${today}|${s.type}|${s.time}`;
        if (pushed.has(key)) continue;                          // 同一次信号只推一次
        pushed.set(key, Date.now());
        const act = s.type === 'BUY_T' ? '▲ 正T买点' : '▼ 反T卖点';
        const title = `🚨 ${d.name || code}(${code}) ${act} ${s.time} 现价${s.price}`;
        const body = [
          `${act}（${s.level || '-'} · 共振${s.strength}/5）@${s.time}`,
          s.fillText || '',
          s.stopText || '',
          s.advice || '',
        ].filter(Boolean).join('\n');
        const pr3 = await pushAll(title, body);
        console.log(`[信号推送] ${title}  wx=${tag(pr3.wx)} email=${tag(pr3.em)} bark=${tag(pr3.bk)}`);
        sentThisRound = true;
      }
      if (sentThisRound) { savePushed(); await new Promise(res => setTimeout(res, PUSH_THROTTLE_MS)); }
    } catch (e) { /* 单只异常不影响其他 */ }
  }
}
let monitorBusy = false;   // 防上一轮(10只串行)未跑完时下一轮叠跑
setInterval(() => {
  if (monitorBusy) return;
  monitorBusy = true;
  monitorTick().catch(e => console.log('[巡检异常]', String(e).slice(0, 120))).finally(() => { monitorBusy = false; });
}, 60 * 1000);

// ---------- 监测股分时数据落盘(复盘/回测样本库) ----------
// 节奏(用户定: 收盘存档为主, 不必5分钟一次): 每天最多3个节点
//   ① 11:31-11:40 午盘备份  ② 14:56-15:00 尾盘兜底  ③ >=15:00 收盘正式存档(complete封存)
// 窗口内每5分钟调度检查一次, 该节点已存过(文件标记)即跳过; 服务重启后20s自动补当窗。
const TDATA_DIR = path.join(__dirname, 't_data');
async function snapshotOnce() {
  const bj = bjNow();
  const dow = bj.getUTCDay();
  if (dow === 0 || dow === 6) return { skipped: '周末' };
  const hm = bj.getUTCHours() * 60 + bj.getUTCMinutes();
  const morningWin = hm >= 691 && hm <= 700;    // 11:31-11:40
  const tailWin = hm >= 896 && hm < 900;        // 14:56-15:00
  const closeWin = hm >= 900 && hm < 1439;      // 15:00-23:59 收盘档
  if (!morningWin && !tailWin && !closeWin) return { skipped: '不在窗口' };
  const node = closeWin ? 'complete' : (tailWin ? 'tail' : 'morning');   // 高档覆盖低档
  const date = bj.toISOString().slice(0, 10);
  const dir = path.join(TDATA_DIR, date);
  fs.mkdirSync(dir, { recursive: true });
  const codes = (notifyCfg.codes || []).slice(0, 10);
  let wrote = 0;
  for (const code of codes) {
    if (!/^\d{6}$/.test(code)) continue;
    const fp = path.join(dir, code + '.json');
    let oldNode = '';
    try {
      const old = JSON.parse(fs.readFileSync(fp, 'utf8'));
      if (old.complete) continue;                       // 已封存
      oldNode = old.node || '';
    } catch (e) {}
    const rank = { '': 0, morning: 1, tail: 2, complete: 3 };
    if (rank[node] <= rank[oldNode]) continue;          // 同档或低档已存
    try {
      const d = await runTQueued(code, null, null, null);
      if (!d || d.error || !d.series || !d.series.times || !d.series.times.length) continue;
      const lastTm = d.series.times[d.series.times.length - 1];
      if (node === 'complete' && lastTm < '14:30') continue;   // 分钟数据异常短, 等下轮
      const rec = {
        code, name: d.name, date, node,
        savedAt: new Date().toISOString(),
        complete: node === 'complete',
        preClose: d.preClose, curPrice: d.curPrice, dayAmp: d.dayAmp,
        cost: { shares: d.cost.shares, basePrice: d.cost.basePrice, breakevenFen: d.cost.breakevenFen, suggestFen: d.cost.suggestFen },
        signals: d.signals,
        series: d.series,
      };
      fs.writeFileSync(fp, JSON.stringify(rec));
      wrote++;
    } catch (e) { /* 单只失败不影响其他 */ }
  }
  return { date, node, wrote, codes: codes.length };
}
let snapBusy = false;
function snapTick() {
  if (snapBusy) return;
  snapBusy = true;
  snapshotOnce().then(r => { if (!r.skipped) console.log(`[分时存档] ${r.node} ${r.date} 写${r.wrote}/${r.codes}`); })
    .catch(e => console.log('[存档异常]', String(e).slice(0, 120)))
    .finally(() => { snapBusy = false; });
}
setTimeout(snapTick, 20 * 1000);
setInterval(snapTick, 5 * 60 * 1000);

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, `http://${req.headers.host}`);
  const sendJson = (code, obj) => {
    res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Access-Control-Allow-Origin': '*' });
    res.end(JSON.stringify(obj));
  };

  if (u.pathname === '/api/screen') {
    const yld = num(u.searchParams.get('yield'), 3.5);
    const rsi = num(u.searchParams.get('rsi'), 35);
    const pct = num(u.searchParams.get('pct'), 25);
    const drawdown = num(u.searchParams.get('drawdown'), 25);
    const key = `${yld}_${rsi}_${pct}_${drawdown}`;
    try {
      let rows;
      if (cache.has(key)) {
        const c = cache.get(key);
        if (Date.now() - c.t < CACHE_TTL) rows = c.rows;
      }
      if (!rows) {
        const base = await runScreen({ yld, rsi, pct, drawdown });
        rows = await Promise.all(base.map(enrich));
        cache.set(key, { t: Date.now(), rows });
      }
      sendJson(200, { ok: true, count: rows.length, params: { yld, rsi, pct, drawdown }, rows });
    } catch (e) {
      sendJson(500, { ok: false, error: String(e.message || e) });
    }
    return;
  }

  if (u.pathname === '/api/steady') {
    try {
      let data;
      if (steadyCache && Date.now() - steadyCache.t < STEADY_TTL) {
        data = steadyCache.data;
      } else {
        data = await runSteadyAll();
        steadyCache = { t: Date.now(), data };
      }
      sendJson(200, { ok: true, count: data.hits.length, ...data, updatedAt: steadyCache.t });
    } catch (e) {
      sendJson(500, { ok: false, error: String(e.message || e) });
    }
    return;
  }

  if (u.pathname === '/api/t') {
    const code = (u.searchParams.get('code') || '600000').trim();
    const shares = u.searchParams.get('shares');
    const cost = u.searchParams.get('cost');
    const comm = u.searchParams.get('comm');
    const key = `${code}|${shares || ''}|${cost || ''}|${comm || ''}`;
    try {
      let data;
      const hit = tCache.get(key);
      if (hit && Date.now() - hit.t < T_CACHE_TTL) {
        data = hit.data;
      } else {
        data = await runTQueued(code, shares, cost, comm);
        tCache.set(key, { t: Date.now(), data });
      }
      if (data && data.error) return sendJson(200, { ok: false, error: data.error });
      sendJson(200, { ok: true, ...data });
    } catch (e) {
      sendJson(200, { ok: false, error: String(e.message || e) });
    }
    return;
  }

  if (u.pathname === '/api/notify-config') {
    if (req.method === 'POST') {
      try {
        const cfg = JSON.parse(await readBody(req));
        // 敏感密钥"留空=保持": GET 已脱敏不回显明文, 前端提交空值时沿用服务器已存值
        const wxKey = String(cfg.wx?.key || '') || (notifyCfg.wx?.key || '');
        const emPass = String(cfg.email?.pass || '') || (notifyCfg.email?.pass || '');
        // bark 字段整体缺失(旧版前端/其他客户端提交) => 保持服务器已存配置, 防止误关
        const curBk = notifyCfg.bark || {};
        const bk = cfg.bark ? {
          enabled: !!cfg.bark.enabled,
          server: String(cfg.bark.server || curBk.server || ''),
          key: String(cfg.bark.key || '') || (curBk.key || ''),
          group: String(cfg.bark.group || curBk.group || '股票工具箱'),
        } : { enabled: !!curBk.enabled, server: curBk.server || '', key: curBk.key || '', group: curBk.group || '股票工具箱' };
        saveNotifyCfg({
          wx: { provider: String(cfg.wx?.provider || 'none'), key: wxKey },
          email: {
            host: String(cfg.email?.host || 'smtp.qq.com'), port: +(cfg.email?.port || 465),
            user: String(cfg.email?.user || ''), pass: emPass, to: String(cfg.email?.to || ''),
          },
          bark: bk,
          codes: (Array.isArray(cfg.codes) ? cfg.codes : []).filter(c => /^\d{6}$/.test(c)).slice(0, 10),
        });
        sendJson(200, { ok: true });
      } catch (e) {
        sendJson(200, { ok: false, error: String(e.message || e) });
      }
    } else {
      // 凭证脱敏: 只返回"是否已设置"标记, 明文密钥不出服务器
      const c = notifyCfg;
      sendJson(200, { ok: true, config: {
        wx: { provider: c.wx?.provider || 'none', hasKey: !!c.wx?.key },
        email: { host: c.email?.host || 'smtp.qq.com', port: c.email?.port || 465,
                 user: c.email?.user || '', to: c.email?.to || '', hasPass: !!c.email?.pass },
        bark: { enabled: !!c.bark?.enabled, server: c.bark?.server || '', group: c.bark?.group || '股票工具箱', hasKey: !!c.bark?.key },
        codes: c.codes || [],
      }});
    }
    return;
  }

  if (u.pathname === '/api/notify-test') {
    try {
      const cfg = JSON.parse(await readBody(req));
      // 与 notify-config 相同的"留空=保持"合并, 防止测试时抹掉已存密钥
      const wxKey = String(cfg.wx?.key || '') || (notifyCfg.wx?.key || '');
      const emPass = String(cfg.email?.pass || '') || (notifyCfg.email?.pass || '');
      const curBk2 = notifyCfg.bark || {};
      const bk2 = cfg.bark ? {
        enabled: !!cfg.bark.enabled,
        server: String(cfg.bark.server || curBk2.server || ''),
        key: String(cfg.bark.key || '') || (curBk2.key || ''),
        group: String(cfg.bark.group || curBk2.group || '股票工具箱'),
      } : { enabled: !!curBk2.enabled, server: curBk2.server || '', key: curBk2.key || '', group: curBk2.group || '股票工具箱' };
      const merged = {
        wx: { provider: String(cfg.wx?.provider || 'none'), key: wxKey },
        email: {
          host: String(cfg.email?.host || 'smtp.qq.com'), port: +(cfg.email?.port || 465),
          user: String(cfg.email?.user || ''), pass: emPass, to: String(cfg.email?.to || ''),
        },
        bark: bk2,
        codes: (Array.isArray(cfg.codes) ? cfg.codes : []).filter(c => /^\d{6}$/.test(c)).slice(0, 10),
      };
      saveNotifyCfg(merged);
      const [wxR, emR, bkR] = await Promise.all([
        sendWx('📢 股票工具箱推送测试', '配置成功! 交易时段出现实时买卖信号时会推送到这里。'),
        sendEmail('📢 股票工具箱推送测试', '配置成功! 交易时段出现实时买卖信号时会推送到这里。'),
        sendBark('📢 股票工具箱推送测试', '配置成功! 交易时段出现实时买卖信号时会推送到这里。'),
      ]);
      sendJson(200, {
        ok: true,
        wx: wxR.skipped ? '未配置' : (wxR.ok ? '已发送,请查看微信' : '失败: ' + JSON.stringify(wxR.raw || wxR.err).slice(0, 150)),
        email: emR.skipped ? '未配置' : (emR.ok ? '已发送,请查收邮箱' : '失败: ' + (emR.err || '').slice(0, 150)),
        bark: bkR.skipped ? '未配置' : (bkR.ok ? '已发送,请查看Bark' : '失败: ' + JSON.stringify(bkR.raw || bkR.err || ('HTTP' + bkR.http)).slice(0, 150)),
      });
    } catch (e) {
      sendJson(200, { ok: false, error: String(e.message || e) });
    }
    return;
  }

  if (u.pathname === '/api/tdata') {
    // 存档索引/下载: ?date=YYYY-MM-DD 列清单; ?date=..&code=600630 返回该日该股JSON
    try {
      const date = (u.searchParams.get('date') || '').trim();
      const code = (u.searchParams.get('code') || '').trim();
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
        const dates = fs.existsSync(TDATA_DIR) ? fs.readdirSync(TDATA_DIR).filter(d => /^\d{4}-\d{2}-\d{2}$/.test(d)) : [];
        return sendJson(200, { ok: true, dates });
      }
      const dir = path.join(TDATA_DIR, date);
      if (!fs.existsSync(dir)) return sendJson(200, { ok: true, files: [] });
      if (code) {
        if (!/^\d{6}$/.test(code)) return sendJson(400, { ok: false, error: 'bad code' });
        const fp = path.join(dir, code + '.json');
        if (!fs.existsSync(fp)) return sendJson(200, { ok: true, data: null });
        return sendJson(200, { ok: true, data: JSON.parse(fs.readFileSync(fp, 'utf8')) });
      }
      const files = fs.readdirSync(dir).filter(f => /\.json$/.test(f));
      const meta = files.map(f => {
        try {
          const j = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
          return { code: j.code, name: j.name, bars: (j.series && j.series.times.length) || 0, signals: (j.signals || []).length, complete: !!j.complete };
        } catch (e) { return { file: f, err: true }; }
      });
      sendJson(200, { ok: true, date, files: meta });
    } catch (e) { sendJson(500, { ok: false, error: String(e.message || e) }); }
    return;
  }

  // 静态文件
  let p = u.pathname === '/' ? '/index.html' : u.pathname;
  const fp = path.join(PUBLIC_DIR, path.normalize(p).replace(/^(\.\.[/\\])+/, ''));
  fs.readFile(fp, (err, data) => {
    if (err) { res.writeHead(404); res.end('Not found'); return; }
    const ext = path.extname(fp);
    const ct = ext === '.html' ? 'text/html' : ext === '.js' ? 'text/javascript' : ext === '.css' ? 'text/css' : 'application/octet-stream';
    res.writeHead(200, { 'Content-Type': ct + '; charset=utf-8' });
    res.end(data);
  });
});

// JSON.stringify 在浏览器端中文无碍; 这里直接序列化(已确保 ascii 不强制)
const ensure_ascii_safe = undefined;

// JSON 直接序列化(浏览器端中文无碍)
server.listen(PORT, '0.0.0.0', () => {
  console.log(`[Stock-Toolkit] 统一服务已启动: http://0.0.0.0:${PORT}`);
  console.log(`  选股器  -> /api/screen   稳健增长 -> /api/steady   做T助手 -> /api/t`);
  console.log(`  Python引擎: ${findPython()}  |  T_PROXY: ${T_PROXY || '(直连)'}`);
  console.log(`  手机访问: http://<服务器IP或域名>:${PORT}`);
});
