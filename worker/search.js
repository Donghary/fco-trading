// 구단 검색과 관심선수 저장 중계소 (Cloudflare Worker)
// GET  /club?nick=닉네임 → 그 구단의 최근 공식경기(없으면 감독모드) 출전 18명, 선수별 지금 시세와 어제 시세, 급여, 그 경기 때 등급
// POST /watch {pass, nick, op: 'add'|'del', spid, grade} → 비밀번호 확인 후 watchlist.json 을 고쳐 커밋. op 가 없으면 비밀번호 확인만
// 비밀값 : NEXON_API_KEY(넥슨), GH_TOKEN(이 저장소 Contents 쓰기), WATCH_PASS(관심선수 비밀번호). 페이지에는 어느 것도 내보내지 않음
const API = 'https://open.api.nexon.com/fconline/v1/';
const PRICE = 'https://fconline.nexon.com/datacenter/PlayerPriceGraph';
const ABILITY = 'https://fconline.nexon.com/datacenter/PlayerAbility';
const DIVISIONS = { 800: '슈퍼 챔피언스', 900: '챔피언스', 1000: '슈퍼 챌린지', 1100: '챌린저1', 1200: '챌린저2', 1300: '챌린저3', 1700: '마스터1', 1800: '마스터2',
  1900: '마스터3', 2000: '월드클래스1', 2100: '월드클래스2', 2200: '월드클래스3', 2300: '프로1', 2400: '프로2', 2500: '프로3', 2600: '세미프로1', 2700: '세미프로2',
  2800: '세미프로3', 2900: '유망주1', 3000: '유망주2', 3100: '유망주3' };
const POS = ['GK', 'SW', 'RWB', 'RB', 'RCB', 'CB', 'LCB', 'LB', 'LWB', 'RDM', 'CDM', 'LDM', 'RM', 'RCM', 'CM', 'LCM', 'LM', 'RAM', 'CAM', 'LAM', 'RF', 'CF', 'LF', 'RW', 'RS', 'ST', 'LS', 'LW', 'SUB'];

class Miss extends Error {}

async function nexon(env, path, missing = '') {
  const r = await fetch(API + path, { headers: { 'x-nxopen-api-key': env.NEXON_API_KEY } });
  if (r.ok) return r.json();
  const code = (await r.json().catch(() => ({})))?.error?.name || '';
  // 00009 데이터 준비 중, 00010 게임 점검, 00011 API 점검. 00004 는 없는 닉네임일 때 옴 (그 밖의 400 은 키 오류 등이라 숨기지 않음)
  if (['OPENAPI00009', 'OPENAPI00010', 'OPENAPI00011'].includes(code)) throw new Miss('넥슨 점검 중이라 지금은 찾을 수 없음');
  if (missing && code === 'OPENAPI00004') throw new Miss(missing);
  throw new Error(`넥슨 API 오류 ${r.status} ${code}`);
}

async function price(spid, grade) {  // 데이터센터 시세 페이지에서 지금 시세와 일별 시세의 마지막(어제) 값을 읽음
  const r = await fetch(PRICE, { method: 'POST', body: `spid=${spid}&n1strong=${grade}`,
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'Mozilla/5.0' } });
  const html = await r.text();
  const now = html.match(/alt="([\d,]+)"/), vals = html.match(/"value":\s*\[([\s\S]*?)\]/);
  const daily = vals ? [...vals[1].matchAll(/"([^"]+)"/g)].map(m => Math.round(+m[1])) : [];
  return { now: now ? +now[1].replace(/,/g, '') : 0, d1: daily.at(-1) ?? null, d7: daily.at(-7) ?? null };
}

async function pay(spid) {  // 급여 : 강화와 무관하게 카드마다 고정. 능력치 페이지의 pay_side 값
  try {
    const r = await fetch(ABILITY, { method: 'POST', body: `spid=${spid}&n1strong=1`,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'Mozilla/5.0' } });
    const m = (await r.text()).match(/class="pay_side">\s*(\d+)/);
    return m ? +m[1] : null;
  } catch { return null; }
}

async function club(env, nick) {
  const { ouid } = await nexon(env, `id?nickname=${encodeURIComponent(nick)}`, '그 닉네임의 구단을 찾지 못함');
  // 공식 3경기 + 감독모드 1경기 중 명단이 온전한(11명 이상) 가장 최근 경기. 몰수, 접속 끊김 경기는 명단이 비어 있음
  const ids = [...await nexon(env, `user/match?ouid=${ouid}&matchtype=50&offset=0&limit=3`), ...await nexon(env, `user/match?ouid=${ouid}&matchtype=52&offset=0&limit=1`)];
  if (!ids.length) throw new Miss('최근 공식경기, 감독모드 기록이 없어 명단을 알 수 없음');
  const details = await Promise.all(ids.map(id => nexon(env, `match-detail?matchid=${id}`)));
  const full = details.filter(d => (d.matchInfo.find(i => i.ouid === ouid)?.player || []).length >= 11);
  if (!full.length) throw new Miss('최근 경기가 몰수나 접속 끊김이라 명단을 알 수 없음');
  const latest = full.sort((a, b) => b.matchDate.localeCompare(a.matchDate))[0];
  const me = latest.matchInfo.find(i => i.ouid === ouid), op = latest.matchInfo.find(i => i.ouid !== ouid);
  const players = await Promise.all(me.player.map(async p => ({ spid: p.spId, grade: p.spGrade, pos: POS[p.spPosition] || '', pay: await pay(p.spId), ...await price(p.spId, p.spGrade) })));
  return {
    nick: me.nickname, match: latest.matchDate, mode: latest.matchType === 50 ? '공식경기' : '감독모드',
    div: latest.matchType === 50 ? DIVISIONS[me.division] || '' : '', result: me.matchDetail?.matchResult, score: [me.shoot?.goalTotal, op?.shoot?.goalTotal], opp: op?.nickname,
    players, at: Date.now(),
  };
}

// 매시 5분, 35분 : GitHub 저장소에 갱신을 요청 (repository_dispatch). GitHub 실행기를 기다리게 하지 않으려고 예약은 여기서 함
async function refresh(env) {
  const r = await fetch(`https://api.github.com/repos/${env.REPO}/dispatches`, {
    method: 'POST', body: JSON.stringify({ event_type: 'refresh' }),
    headers: { Authorization: `Bearer ${env.GH_TOKEN}`, Accept: 'application/vnd.github+json', 'User-Agent': 'fco-search-worker' },
  });
  if (r.status !== 204) throw new Error(`갱신 요청 실패 ${r.status} ${await r.text()}`);
}

// ---------- 관심선수 저장 ----------
const GH = env => ({ Authorization: `Bearer ${env.GH_TOKEN}`, Accept: 'application/vnd.github+json', 'User-Agent': 'fco-search-worker' });
const b64 = s => btoa(String.fromCharCode(...new TextEncoder().encode(s)));
const unb64 = s => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/\s/g, '')), c => c.charCodeAt(0)));
const LIMIT = 8;  // 같은 IP 에서 10분 동안 틀릴 수 있는 횟수. 4자리 비밀번호를 대입으로 못 풀게 함

async function tries(ip, add) {  // ponytail: 캐시 기반 계수라 정확하지 않음(동시 요청은 덜 셀 수 있음). 남용이 보이면 Durable Object 로
  const key = new Request(`https://cache.fco-search/try/${encodeURIComponent(ip)}`), cache = caches.default;
  const n = +(await (await cache.match(key))?.text() || 0) + (add ? 1 : 0);
  if (add) await cache.put(key, new Response(String(n), { headers: { 'Cache-Control': 'max-age=600' } }));
  return n;
}

async function watch(env, req) {
  const ip = req.headers.get('CF-Connecting-IP') || '?';
  if (await tries(ip, false) >= LIMIT) return [429, { error: '비밀번호를 너무 많이 틀림. 10분 뒤 다시 하면 됨' }];
  const body = await req.json().catch(() => ({}));
  if (!env.WATCH_PASS || body.pass !== env.WATCH_PASS) { await tries(ip, true); return [401, { error: '비밀번호가 맞지 않음' }]; }
  if (!body.op) return [200, { ok: true }];
  const spid = +body.spid, grade = +body.grade;
  if (!['add', 'del'].includes(body.op) || !Number.isInteger(spid) || spid <= 0 || !Number.isInteger(grade) || grade < 1 || grade > 13)
    return [400, { error: '잘못된 요청' }];
  const owners = (await (await fetch(`${env.SITE}data.json?t=${Date.now()}`)).json()).owners.map(o => o.nick);  // 구단주 목록은 사이트 기준
  if (!owners.includes(body.nick)) return [400, { error: '없는 구단주' }];
  const url = `https://api.github.com/repos/${env.REPO}/contents/watchlist.json`;
  for (let attempt = 0; attempt < 2; attempt++) {  // 다른 저장과 겹치면(409) 한 번 다시 받아 고침
    const cur = await fetch(url, { headers: GH(env) });
    if (!cur.ok) return [502, { error: '저장소를 읽지 못함' }];
    const file = await cur.json(), all = JSON.parse(unb64(file.content));
    const list = (Array.isArray(all[body.nick]) ? all[body.nick] : []).filter(w => !(w.spid === spid && w.grade === grade));
    if (body.op === 'add') { if (list.length >= 30) return [400, { error: '관심선수는 30명까지 저장할 수 있음' }]; list.push({ spid, grade }); }
    all[body.nick] = list;
    const put = await fetch(url, { method: 'PUT', headers: GH(env),
      body: JSON.stringify({ message: '관심선수 변경', content: b64(JSON.stringify(all)), sha: file.sha }) });
    if (put.ok) return [200, { lists: all }];
    if (put.status !== 409) return [502, { error: '저장하지 못함. 잠시 뒤 다시 누르면 됨' }];
  }
  return [409, { error: '다른 변경과 겹침. 다시 눌러 주면 됨' }];
}

export default {
  async scheduled(event, env, ctx) { ctx.waitUntil(refresh(env)); },
  async fetch(req, env, ctx) {
    const url = new URL(req.url), origin = req.headers.get('Origin') || '';
    const local = /^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(origin);  // 로컬 미리보기
    const cors = { 'Access-Control-Allow-Origin': origin === env.ALLOW_ORIGIN || local ? origin : env.ALLOW_ORIGIN,
      'Access-Control-Allow-Methods': 'GET, POST', 'Access-Control-Allow-Headers': 'Content-Type', 'Content-Type': 'application/json; charset=utf-8' };
    if (req.method === 'OPTIONS') return new Response(null, { headers: cors });
    if (url.pathname === '/watch' && req.method === 'POST') {
      if (origin !== env.ALLOW_ORIGIN && !local) return new Response(JSON.stringify({ error: '허용되지 않은 출처' }), { status: 403, headers: cors });
      try { const [status, out] = await watch(env, req); return new Response(JSON.stringify(out), { status, headers: cors }); }
      catch { return new Response(JSON.stringify({ error: '저장하지 못함. 잠시 뒤 다시 누르면 됨' }), { status: 502, headers: cors }); }
    }
    const nick = (url.searchParams.get('nick') || '').trim();
    if (url.pathname !== '/club' || !nick || nick.length > 30) return new Response(JSON.stringify({ error: '닉네임을 넣어야 함' }), { status: 400, headers: cors });
    const cache = caches.default, key = new Request(`https://cache.fco-search/club/${encodeURIComponent(nick)}`);  // 같은 닉네임은 10분 동안 저장된 결과를 줌
    const hit = await cache.match(key);
    if (hit) return new Response(hit.body, { headers: cors });
    try {
      const body = JSON.stringify(await club(env, nick));
      ctx.waitUntil(cache.put(key, new Response(body, { headers: { 'Cache-Control': 'max-age=600' } })));
      return new Response(body, { headers: cors });
    } catch (e) {
      return new Response(JSON.stringify({ error: e instanceof Miss ? e.message : '잠시 뒤 다시 찾아보면 됨' }), { status: e instanceof Miss ? 404 : 502, headers: cors });
    }
  },
};
