// 구단 검색 중계소 (Cloudflare Worker)
// GET /club?nick=닉네임 → 그 구단의 최근 공식경기(없으면 감독모드) 출전 18명, 선수별 지금 시세와 어제 시세, 그 경기 때 등급
// 넥슨 키는 Worker 비밀값(NEXON_API_KEY)에만 두고 페이지에는 노출하지 않음
const API = 'https://open.api.nexon.com/fconline/v1/';
const PRICE = 'https://fconline.nexon.com/datacenter/PlayerPriceGraph';
const DIVISIONS = { 800: '슈퍼 챔피언스', 900: '챔피언스', 1000: '슈퍼 챌린지', 1100: '챌린저1', 1200: '챌린저2', 1300: '챌린저3', 1700: '마스터1', 1800: '마스터2',
  1900: '마스터3', 2000: '월드클래스1', 2100: '월드클래스2', 2200: '월드클래스3', 2300: '프로1', 2400: '프로2', 2500: '프로3', 2600: '세미프로1', 2700: '세미프로2',
  2800: '세미프로3', 2900: '유망주1', 3000: '유망주2', 3100: '유망주3' };
const POS = ['GK', 'SW', 'RWB', 'RB', 'RCB', 'CB', 'LCB', 'LB', 'LWB', 'RDM', 'CDM', 'LDM', 'RM', 'RCM', 'CM', 'LCM', 'LM', 'RAM', 'CAM', 'LAM', 'RF', 'CF', 'LF', 'RW', 'RS', 'ST', 'LS', 'LW', 'SUB'];

class Miss extends Error {}

async function nexon(env, path) {
  const r = await fetch(API + path, { headers: { 'x-nxopen-api-key': env.NEXON_API_KEY } });
  if (r.status === 400) throw new Miss('그 닉네임의 구단을 찾지 못함');
  if (!r.ok) throw new Error(`넥슨 API 오류 ${r.status}`);
  return r.json();
}

async function price(spid, grade) {  // 데이터센터 시세 페이지에서 지금 시세와 일별 시세의 마지막(어제) 값을 읽음
  const r = await fetch(PRICE, { method: 'POST', body: `spid=${spid}&n1strong=${grade}`,
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'Mozilla/5.0' } });
  const html = await r.text();
  const now = html.match(/alt="([\d,]+)"/), vals = html.match(/"value":\s*\[([\s\S]*?)\]/);
  const daily = vals ? [...vals[1].matchAll(/"([^"]+)"/g)].map(m => Math.round(+m[1])) : [];
  return { now: now ? +now[1].replace(/,/g, '') : 0, d1: daily.at(-1) ?? null, d7: daily.at(-7) ?? null };
}

async function club(env, nick) {
  const { ouid } = await nexon(env, `id?nickname=${encodeURIComponent(nick)}`);
  const ids = [...await nexon(env, `user/match?ouid=${ouid}&matchtype=50&offset=0&limit=1`), ...await nexon(env, `user/match?ouid=${ouid}&matchtype=52&offset=0&limit=1`)];
  if (!ids.length) throw new Miss('최근 공식경기, 감독모드 기록이 없어 명단을 알 수 없음');
  const details = await Promise.all(ids.map(id => nexon(env, `match-detail?matchid=${id}`)));
  const latest = details.sort((a, b) => b.matchDate.localeCompare(a.matchDate))[0];
  const me = latest.matchInfo.find(i => i.ouid === ouid), op = latest.matchInfo.find(i => i.ouid !== ouid);
  const players = await Promise.all(me.player.map(async p => ({ spid: p.spId, grade: p.spGrade, pos: POS[p.spPosition] || '', ...await price(p.spId, p.spGrade) })));
  return {
    nick: me.nickname, match: latest.matchDate, mode: latest.matchType === 50 ? '공식경기' : '감독모드',
    div: latest.matchType === 50 ? DIVISIONS[me.division] || '' : '', result: me.matchDetail?.matchResult, score: [me.shoot?.goalTotal, op?.shoot?.goalTotal], opp: op?.nickname,
    players, at: Date.now(),
  };
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url), origin = req.headers.get('Origin') || '';
    const cors = { 'Access-Control-Allow-Origin': origin === env.ALLOW_ORIGIN || origin.startsWith('http://127.0.0.1') ? origin : env.ALLOW_ORIGIN,
      'Access-Control-Allow-Methods': 'GET', 'Content-Type': 'application/json; charset=utf-8' };
    if (req.method === 'OPTIONS') return new Response(null, { headers: cors });
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
