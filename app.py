#!/usr/bin/env python3
"""FC온라인 구단 시세 트래커 : 정적 사이트 생성기 (표준 라이브러리만 사용)

python3 app.py build   site/ 폴더 생성. GitHub Actions 가 10분마다 실행해 Pages 로 배포
python3 app.py         10분마다 build 하면서 http://localhost:8000 에서 미리보기 (이 맥에서만 접속됨)
python3 app.py check   자체 점검

API 키    : api_key.txt 한 줄 또는 환경변수 NEXON_API_KEY
구단 명단 : 넥슨 오픈 API (구단주명 → ouid → 최근 경기 → 출전 선수 18명)
시세, 스탯 : FC온라인 데이터센터 페이지 (오픈 API 에 없음. 비공식이라 페이지가 바뀌면 parse_* 수정)
기록 유지 : 실행마다 상태가 사라지므로 직전 결과(state.json)를 이어받아 시간대별 시세를 쌓음
"""
import functools
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
SITE = ROOT / "site"
OWNERS = ["456화이팅", "우끼끼하하", "게임좀그만해라", "이게임초보인데"]
REPO = os.environ.get("GITHUB_REPOSITORY", "Donghary/fco-trading")  # 관심선수 저장 위치
REFRESH_MIN = 10  # 미리보기 모드의 갱신 주기(분). 배포 주기는 .github/workflows/update.yml
SQUAD_MIN = 120  # 명단과 경기 목록 갱신 주기(분). 넥슨 API 일일 한도(1,000건)를 아끼려고 시세보다 길게 둠
INTRA_DAYS = 7  # 시간대별 시세 보관 기간(일)
INFO_DAYS = 7  # 스탯, 특성 재조회 주기(일)
MATCH_PER_RUN = 10  # 한 번에 받을 경기 상세 수 (일일 한도를 나눠 씀)
VALUE_DAYS = 120  # 실제 구단 가치 기록 보관 기간(일)
GROW = 4  # 능력치 기준 적응도 : 0~4 가 게임의 1~5 단계. 팀컬러는 구단마다 달라 반영하지 않음
PORT = 8000
DATACENTER = "https://fconline.nexon.com/datacenter/"


def api_key():
    f = ROOT / "api_key.txt"
    return os.environ.get("NEXON_API_KEY") or (f.read_text().strip() if f.exists() else "")


def get(url, data=None, headers={}):
    req = urllib.request.Request(url, data, {"User-Agent": "Mozilla/5.0", **headers})
    return urllib.request.urlopen(req, timeout=20).read()


def meta(name):
    f = ROOT / f"meta_{name}.json"
    if not f.exists() or time.time() - f.stat().st_mtime > 86400:
        try:
            f.write_bytes(get(f"https://open.api.nexon.com/static/fconline/meta/{name}.json"))
        except OSError:
            if not f.exists():
                raise
    return json.loads(f.read_bytes())


NAMES = {p["id"]: p["name"] for p in meta("spid")}
SEASONS = {s["seasonId"]: s for s in meta("seasonid")}
POSITIONS = {p["spposition"]: p["desc"] for p in meta("spposition")}
DIVISIONS = {d["divisionId"]: d["divisionName"] for d in meta("division")}


# ---------- 넥슨 오픈 API : 구단 명단 ----------

def nexon(path, **q):
    time.sleep(0.3)  # 연달아 부르면 초당 한도에 걸림(429). 일일 한도는 1,000건
    url = f"https://open.api.nexon.com/fconline/v1/{path}?{urllib.parse.urlencode(q)}"
    try:
        return json.loads(get(url, headers={"x-nxopen-api-key": api_key()}))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"넥슨 API 오류 {e.code} : {e.read().decode(errors='replace')[:200]}")


def pick_squad(details, owner_ouid):
    """매치 상세 여러 개 중 가장 최근 경기에서 해당 구단주의 출전 명단을 고름"""
    latest = max(details, key=lambda d: d["matchDate"])
    me = next(i for i in latest["matchInfo"] if i["ouid"] == owner_ouid)
    players = [{"spid": p["spId"], "grade": p["spGrade"], "pos": p["spPosition"]} for p in me["player"]]
    return players, latest["matchDate"]


def refresh_squad(nick, s):
    """s 를 최신 명단으로 고침. ouid 와 최근 경기 번호를 기억해 넥슨 API 호출을 줄임"""
    s["ouid"] = s.get("ouid") or nexon("id", nickname=nick)["ouid"]
    # 50 공식경기, 52 감독모드 각각의 최근 1경기 중 더 최근 것
    official = nexon("user/match", ouid=s["ouid"], matchtype=50, offset=0, limit=20)
    s["official"] = official[:10]
    STATE["off"][nick] = list(dict.fromkeys(official + STATE["off"].get(nick, [])))
    ids = official[:1] + nexon("user/match", ouid=s["ouid"], matchtype=52, offset=0, limit=1)
    if not ids:
        raise RuntimeError("최근 공식경기, 감독모드 기록이 없어 명단을 알 수 없음")
    if ids != s.get("ids"):
        s["players"], s["match"] = pick_squad([nexon("match-detail", matchid=i) for i in ids], s["ouid"])
        s["ids"] = ids
    s["at"] = time.time()


def minute(t):
    """goalTime : 상위 비트가 구간(0 전반, 1 후반, 2 연장 전반, 3 연장 후반, 4 승부차기), 하위 24비트가 그 구간의 초"""
    half, sec = t >> 24, t & 0xFFFFFF
    return [0, 45, 90, 105, 120][min(half, 4)] + sec // 60 + 1


def match_rec(d, by_ouid):
    def side(x):
        md, sh = x.get("matchDetail") or {}, x.get("shoot") or {}
        goals = [{"t": g.get("type"), "sp": g.get("spId"), "g": g.get("spGrade"), "a": g.get("assistSpId") if g.get("assist") else 0,
                  "m": minute(g.get("goalTime") or 0), "x": round(g.get("x") or 0, 3), "y": round(g.get("y") or 0, 3), "in": bool(g.get("inPenalty"))}
                 for g in x.get("shootDetail") or [] if g.get("result") == 3]
        return {"nick": by_ouid.get(x["ouid"], x.get("nickname", "")), "own": x["ouid"] in by_ouid, "res": md.get("matchResult"),
                "end": md.get("matchEndType"), "goal": sh.get("goalTotal"), "pk": sh.get("shootOutScore"), "poss": md.get("possession"),
                "sh": sh.get("shootTotal"), "sot": sh.get("effectiveShootTotal"), "div": x.get("division"),
                "season": md.get("seasonId"), "goals": goals}  # 시즌 번호는 사람마다 다를 수 있어 각자 것을 씀
    return {"v": 2, "date": d["matchDate"], "type": d.get("matchType"), "p": [side(x) for x in d.get("matchInfo", [])]}  # v2 : 골 위치 포함


def refresh_matches():
    """네 명끼리 붙은 클래식 1on1(40) 과 각자의 공식경기(50) 상세를 받아 쌓음. 넥슨은 최근 약 30일만 주므로 받은 것은 계속 보관"""
    owners = {n: s["ouid"] for n, s in STATE["squads"].items() if s.get("ouid")}
    if len(owners) < 2:
        return
    by_ouid, cache = {o: n for n, o in owners.items()}, STATE["matches"]
    first = not STATE["seeded"]  # 처음 한 번은 넥슨이 주는 30일 치를 끝까지 넘겨 받음
    listing = first or time.time() - STATE["m_at"] >= SQUAD_MIN * 60
    seen = {}
    for n, o in owners.items() if listing else []:
        for off in range(0, 1000 if first else 1, 100):
            got = nexon("user/match", ouid=o, matchtype=40, offset=off, limit=100 if first else 30)
            for m in got:
                seen.setdefault(m, set()).add(n)
            if len(got) < 100:
                break
    h2h = [m for m, who in seen.items() if len(who) >= 2]  # 두 명 이상의 목록에 같이 있으면 네 명끼리 경기
    if listing:
        STATE["m_at"] = time.time()
    off_first = not STATE["off_seeded"]
    for n, o in owners.items() if off_first else []:  # 공식경기 목록 : 처음 한 번만 30일 치 전부. 이후는 명단 갱신 때 같이 받음
        got = []
        for off in range(0, 1000 if off_first else 1, 100):
            page = nexon("user/match", ouid=o, matchtype=50, offset=off, limit=100)
            got += page
            if len(page) < 100:
                break
        STATE["off"][n] = list(dict.fromkeys(got + STATE["off"].get(n, [])))
    recent = [m for s in STATE["squads"].values() for m in s.get("official", [])]
    older = [m for ids in STATE["off"].values() for m in ids]
    old = lambda r: r.get("v") != 2 or (r.get("type") == 50 and any("season" not in p for p in r["p"]))  # 골 위치나 각자의 시즌 번호가 없는 예전 기록
    stale = [m for m, r in cache.items() if old(r)]
    missing = [m for m in dict.fromkeys(recent + h2h + STATE["queue"] + older) if m not in cache]
    todo = missing + [m for m in dict.fromkeys(recent + older + stale) if m in cache and old(cache[m])]
    done = 0
    for m in todo[:MATCH_PER_RUN]:
        try:
            cache[m] = match_rec(nexon("match-detail", matchid=m), by_ouid)
            done += 1
        except Exception as e:
            print("경기 상세 실패", m, e, file=sys.stderr)
            if "429" in str(e):
                break  # 한도에 걸리면 이번 실행은 여기까지
    left = todo[done:]
    keep = set(recent) | {m for ids in STATE["off"].values() for m in ids}
    STATE["matches"] = {m: r for m, r in cache.items() if r.get("type") == 40 or m in keep}
    STATE["off"] = {n: [m for m in ids if m in STATE["matches"] or m in left] for n, ids in STATE["off"].items()}
    STATE["queue"], STATE["seeded"], STATE["off_seeded"] = [m for m in left if m in h2h or m in STATE["queue"]], True, True
    PENDING[0] = len(left)
    if PENDING[0]:
        print("경기 상세", PENDING[0], "건은 다음 갱신 때 받음")


# ---------- 데이터센터 : 시세, 스탯 ----------

def clean(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def parse_price(html):
    def series(key):
        return re.findall(r'"([^"]+)"', re.search(rf'"{key}":\s*\[(.*?)\]', html, re.S).group(1))
    now = re.search(r'alt="([\d,]+)"', html)
    return {
        "now": int(now.group(1).replace(",", "")) if now else 0,  # 0 = 시세 없음
        "daily": [int(float(v)) for v in series("value")],  # 일별 최대 365일, 마지막 값이 어제
        "days": series("time"),  # "11.16" 형식
    }


def parse_info(html):
    def one(pattern):
        m = re.search(pattern, html, re.S)
        return clean(m.group(1)) if m else ""
    stats = [[clean(n), int(v)] for n, v in re.findall(
        r'<li class="ab"[^>]*>\s*<div class="txt">([^<]+)</div>\s*<div class="value[^"]*">\s*(\d+)', html)]
    traits = re.search(r'class="skill_wrap">(.*?)class="en_selector_wrap"', html, re.S)
    return {
        "pay": one(r'class="pay_side">\s*(\d+)'),
        "positions": [[p, int(v)] for p, v in re.findall(
            r'<span class="txt">(\w+)</span>\s*<span class="skillData_\d+ value">\s*(\d+)', html)],
        "birth": one(r'class="etc birth">(.*?)</span>'), "height": one(r'class="etc height">(.*?)</span>'),
        "weight": one(r'class="etc weight">(.*?)</span>'), "body": one(r'class="etc physical">(.*?)</span>'),
        "skill": one(r'class="etc skill">(.*?)</span>').count("★"),
        "foot": one(r'class="etc foot">(.*?)</span>').replace("–", "/"),
        "fame": one(r'class="etc season">(.*?)</span>'),
        "team": one(r'class="etc team">.*?class="txt">(.*?)</span>'),
        "nation": one(r'class="etc nation">.*?class="txt">(.*?)</span>'),
        "traits": re.findall(r'class="desc">([^<]+)<', traits.group(1)) if traits else [],
        "summary": stats[:6], "stats": stats[6:],  # 앞 6개 : 스피드, 슛, 패스, 드리블, 수비, 피지컬
    }


def datacenter(page, spid, grade, **extra):
    data = urllib.parse.urlencode({"spid": spid, "n1strong": grade, **extra}).encode()
    return get(DATACENTER + page, data).decode(errors="replace")


# ---------- 상태 (직전 결과를 이어받음) ----------

STATE = {"squads": {}, "intra": {}, "info": {}, "matches": {}, "value": {}, "queue": [], "seeded": False, "off": {}, "off_seeded": False, "m_at": 0}
PENDING = [0]  # 아직 못 받은 경기 상세 수


def load_state():
    actions = "GITHUB_REPOSITORY" in os.environ
    try:
        if actions:  # 직전 배포본
            owner, name = REPO.split("/")
            raw = get(f"https://{owner.lower()}.github.io/{name}/state.json?t={int(time.time())}")
        else:
            raw = (SITE / "state.json").read_bytes()
    except FileNotFoundError:
        return
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise  # 일시 오류면 중단함. 빈 상태로 덮어써 쌓인 기록을 날리지 않기 위함
        return
    for k, v in json.loads(raw).items():
        if k in STATE:
            STATE[k] = v


def add_intra(key, t, price):
    add_point(STATE["intra"].setdefault(key, []), t, price)


def add_point(s, t, price):
    """시간대별 값 추가. 같은 값이 이어지면 구간의 처음과 끝만 남겨 용량을 줄임"""
    if len(s) >= 2 and s[-1][1] == s[-2][1] == price:
        s[-1][0] = t
    else:
        s.append([t, price])


def card(spid, grade, pos=None, price=None):
    season = SEASONS.get(spid // 1000000, {})
    return {
        "spid": spid, "grade": grade, "pos": POSITIONS.get(pos, ""),
        "name": NAMES.get(spid, str(spid)),
        "season": season.get("className", "").split(" (")[0], "seasonImg": season.get("seasonImg"),
        "now": price and price["now"], "hist": price["daily"][-30:] if price else [],
        # 포지션별 오버롤 : 강화 단계와 적응도 5 반영, 팀컬러 제외
        "positions": STATE["info"].get(f"{spid}_{grade}", {}).get("positions", []),
    }


def read_watch():
    """watchlist.json : {구단주명: [{spid, grade}]}. 페이지에서 누구나 고칠 수 있으므로 모양이 틀린 항목은 버림"""
    try:
        raw = json.loads((ROOT / "watchlist.json").read_text())
    except ValueError:
        raw = {}
    def ok(w):
        return (isinstance(w, dict) and type(w.get("spid")) is int and w["spid"] in NAMES
                and type(w.get("grade")) is int and 1 <= w["grade"] <= 13)
    lists = raw if isinstance(raw, dict) else {}
    return {n: [{"spid": w["spid"], "grade": w["grade"]} for w in lists.get(n, []) if ok(w)][:30]
            if isinstance(lists.get(n, []), list) else [] for n in OWNERS}


def official_rows(nick, ids, goals=False):
    named = lambda g: {**g, "n": NAMES.get(g["sp"], ""), "an": NAMES.get(g["a"], "") if g["a"] else ""}
    rows = []
    for m in ids:
        r = STATE["matches"].get(m)
        if not r or len(r["p"]) != 2:
            continue
        me = next((p for p in r["p"] if p["nick"] == nick), None)
        if not me:
            continue
        op = r["p"][1] if r["p"][0] is me else r["p"][0]
        row = {"d": r["date"], "s": me.get("season"), "r": me["res"], "e": me["end"], "g": me["goal"], "og": op["goal"], "op": op["nick"],
               "pk": [me["pk"], op["pk"]], "div": DIVISIONS.get(me["div"], "")}
        if goals:
            row.update(poss=me["poss"], sh=[me["sh"], op["sh"]], sot=[me["sot"], op["sot"]],
                       mg=[named(g) for g in me["goals"]], og2=[named(g) for g in op["goals"]])
        rows.append(row)
    return sorted(rows, key=lambda r: r["d"], reverse=True)


def baselines():
    """게임 안에서 확인한 누적 기록 (사용자가 알려준 값). asof 시각까지의 기록이고, 그 뒤 경기는 받은 기록으로 더함"""
    f = ROOT / "baselines.json"
    return json.loads(f.read_text()) if f.exists() else {}


def write_official():
    """공식경기 : 지금 시즌은 official.json, 지난 시즌은 official_<시즌>.json 으로 나눠 씀. 시즌 요약은 모든 시즌을 담음"""
    rows = {n: official_rows(n, STATE["off"].get(n, []), goals=True) for n in OWNERS}
    seasons = sorted({r["s"] for rs in rows.values() for r in rs if r["s"]}, reverse=True)
    latest = [next((r["s"] for r in rs if r["s"]), None) for rs in rows.values()]  # 각자 가장 최근 경기의 시즌
    cur = max((x for x in latest if x), default=None)
    base = baselines().get("official", {})

    def summary(n, season, rs):
        b = base.get(n, {}).get(str(season))
        after = [r for r in rs if not b or r["d"] > b["asof"]]
        w, d, l = (sum(r["r"] == x for r in after) for x in ("승", "무", "패"))
        if b:
            w, d, l = w + b["w"], d + b["d"], l + b["l"]
        return {"got": len(rs), "w": w, "d": d, "l": l, "gf": sum(r["g"] or 0 for r in rs), "ga": sum(r["og"] or 0 for r in rs),
                "first": rs[-1]["d"], "last": rs[0]["d"], "div0": rs[-1]["div"], "div": rs[0]["div"], "base": b}

    out = {n: {"sum": {}, "rows": []} for n in OWNERS}
    for n, rs in rows.items():
        by = {}
        for r in rs:
            by.setdefault(r["s"] or cur, []).append(r)  # 시즌 번호가 아직 없는 경기는 지금 시즌으로 봄
        out[n]["sum"] = {str(k): summary(n, k, v) for k, v in by.items() if k}
        out[n]["rows"] = by.get(cur, [])
    write("official.json", {"pending": PENDING[0], "current": str(cur) if cur else None, "owners": out})
    for season in seasons[1:]:
        write(f"official_{season}.json", {"owners": {n: [r for r in rs if r["s"] == season] for n, rs in rows.items()}})


def official_of(nick, s):
    """구단주의 최근 공식경기 요약 : 경기별 결과와 가장 최근 경기의 등급"""
    rows = []
    for m in s.get("official", []):
        r = STATE["matches"].get(m)
        if not r or len(r["p"]) != 2:
            continue
        me = next((p for p in r["p"] if p["nick"] == nick), None)
        if not me:
            continue
        op = r["p"][1] if r["p"][0] is me else r["p"][0]
        rows.append({"d": r["date"], "r": me["res"], "e": me["end"], "g": me["goal"], "og": op["goal"], "op": op["nick"],
                     "pk": [me["pk"], op["pk"]], "div": DIVISIONS.get(me["div"], "")})
    return {"official": rows, "div": rows[0]["div"] if rows else ""}


def write(name, obj):
    f = SITE / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def build():
    load_state()
    seed = ROOT / "seed_matches.json"  # 로컬에서 미리 받아 둔 경기 상세. 넥슨 일일 한도를 아끼려고 처음 한 번만 씀
    if seed.exists() and not STATE["seeded"]:
        STATE["matches"].update(json.loads(seed.read_text()))
        STATE["seeded"] = True
    now = int(time.time())

    squads = STATE["squads"] = {n: STATE["squads"].get(n) or {"players": [], "match": None, "at": 0} for n in OWNERS}
    for n, s in squads.items():
        if not api_key():
            s["error"] = "넥슨 오픈 API 키가 없어 명단을 불러오지 못함"
        elif s.get("error") or now - s["at"] >= SQUAD_MIN * 60 or "official" not in s:
            try:
                refresh_squad(n, s)
                s["error"] = None
            except Exception as e:
                if "429" not in str(e):  # 일일 한도 초과는 잠깐이라 화면에 띄우지 않음
                    s["error"] = str(e)  # 이전 명단은 유지, 다음 실행 때 다시 시도
                print("명단 조회 실패", n, e, file=sys.stderr)

    if api_key():
        try:
            refresh_matches()
        except Exception as e:
            print("경기 기록 갱신 실패", e, file=sys.stderr)

    watch = read_watch()
    keys = {(p["spid"], p["grade"]) for s in squads.values() for p in s["players"]}
    keys |= {(w["spid"], w["grade"]) for ws in watch.values() for w in ws}
    prices = {}
    for k in sorted(keys):
        key = f"{k[0]}_{k[1]}"
        try:
            prices[k] = parse_price(datacenter("PlayerPriceGraph", *k))
            if prices[k]["now"]:
                add_intra(key, now, prices[k]["now"])
            info = STATE["info"].get(key, {})
            if now - info.get("at", 0) >= INFO_DAYS * 86400 or info.get("grow") != GROW:
                page = datacenter("PlayerAbility", *k, n1Grow=GROW)
                STATE["info"][key] = {**parse_info(page), "at": now, "grow": GROW}
        except Exception as e:
            print("데이터센터 조회 실패", k, e, file=sys.stderr)
        time.sleep(0.1)  # 데이터센터에 부담 주지 않게 천천히
    if keys and not prices:
        raise RuntimeError("조회할 선수가 있는데 시세를 하나도 받지 못함. 이전 결과를 유지함")

    for n, s in squads.items():  # 실제 구단 가치 : 명단의 모든 선수 시세를 받았을 때만 기록
        ks = [(p["spid"], p["grade"]) for p in s["players"]]
        if ks and all(k in prices for k in ks):
            add_point(STATE["value"].setdefault(n, []), now, sum(prices[k]["now"] for k in ks))
    vcut = now - VALUE_DAYS * 86400
    STATE["value"] = {n: [p for p in v if p[0] >= vcut] for n, v in STATE["value"].items() if n in OWNERS}

    cutoff = now - INTRA_DAYS * 86400
    STATE["intra"] = {k: [p for p in v if p[0] >= cutoff] for k, v in STATE["intra"].items()}
    STATE["intra"] = {k: v for k, v in STATE["intra"].items() if v}

    write("state.json", STATE)
    write("data.json", {
        "updated": now, "repo": REPO, "v": hashlib.md5((ROOT / "index.html").read_bytes()).hexdigest()[:8],
        "owners": [{"nick": n, "error": s.get("error"), "match": s["match"], **official_of(n, s),
                    "players": [card(**p, price=prices.get((p["spid"], p["grade"]))) for p in s["players"]]}
                   for n, s in squads.items()],
        "watch": {n: [card(**w, price=prices.get((w["spid"], w["grade"]))) for w in ws] for n, ws in watch.items()},
        "lock": (ROOT / "lock.json").exists(),  # 친구용 편집 비밀번호가 정해졌는지
    })
    write("values.json", STATE["value"])
    write_official()
    own = set(OWNERS)
    named = lambda g: {**g, "n": NAMES.get(g["sp"], ""), "an": NAMES.get(g["a"], "") if g["a"] else ""}
    write("h2h.json", {"pending": PENDING[0], "base": baselines().get("h2h", {}), "matches": sorted(({"id": m, "date": r["date"], "p": [{**p, "goals": [named(g) for g in p["goals"]]} for p in r["p"]]}
                                          for m, r in STATE["matches"].items()
                                          if r.get("type") == 40 and len(r["p"]) == 2 and all(p["nick"] in own for p in r["p"])),
                                         key=lambda m: m["date"], reverse=True)})
    for k, price in prices.items():  # 선수 상세 : 눌렀을 때만 받아감
        key = f"{k[0]}_{k[1]}"
        write(f"p/{key}.json", {**card(*k, price=price), "daily": price["daily"], "days": price["days"],
                                "intra": STATE["intra"].get(key, []), "info": STATE["info"].get(key)})
    # 선수 검색용 색인 : 선수번호(pid) → [이름, [시즌번호...]]. spid = 시즌번호 * 1000000 + pid
    players = {}
    for spid, name in NAMES.items():
        players.setdefault(spid % 1000000, [name, []])[1].append(spid // 1000000)
    write("names.json", {"players": players, "seasons": {
        i: [s["className"].split(" (")[0], s["seasonImg"]] for i, s in SEASONS.items()}})
    for name in ("index.html", "favicon.png", "icon-180.png", "lock.json"):
        if (ROOT / name).exists():
            (SITE / name).write_bytes((ROOT / name).read_bytes())
    print(time.strftime("%H:%M"), "생성 완료 : 시세", len(prices), "건")


def preview():
    def loop():
        while True:
            try:
                build()
            except Exception as e:
                print("갱신 실패", e, file=sys.stderr)
            time.sleep(REFRESH_MIN * 60)

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    SITE.mkdir(exist_ok=True)
    threading.Thread(target=loop, daemon=True).start()
    print(f"미리보기 : http://localhost:{PORT}  (종료 Ctrl+C)")
    ThreadingHTTPServer(("127.0.0.1", PORT), functools.partial(Quiet, directory=str(SITE))).serve_forever()


def check():
    new = {"matchDate": "2026-11-01T00:00:00", "matchInfo": [
        {"ouid": "a", "player": [{"spId": 1, "spGrade": 5, "spPosition": 25}]}, {"ouid": "b", "player": []}]}
    old = {"matchDate": "2026-10-01T00:00:00", "matchInfo": [{"ouid": "a", "player": []}]}
    assert pick_squad([old, new], "a") == ([{"spid": 1, "grade": 5, "pos": 25}], new["matchDate"])

    STATE["intra"].clear()
    for t, v in [(1, 10), (2, 10), (3, 10), (4, 20), (5, 20)]:
        add_intra("k", t, v)
    assert STATE["intra"].pop("k") == [[1, 10], [3, 10], [4, 20], [5, 20]]

    p = parse_price(datacenter("PlayerPriceGraph", 201200104, 8))  # 실제 데이터센터 조회
    assert p["now"] > 0 and len(p["daily"]) == len(p["days"]) >= 30, p
    assert parse_price(datacenter("PlayerPriceGraph", 999999999, 1))["now"] == 0
    i = parse_info(datacenter("PlayerAbility", 866200104, 8))
    assert [n for n, _ in i["summary"]] == ["스피드", "슛", "패스", "드리블", "수비", "피지컬"], i["summary"]
    assert len(i["stats"]) == 34 and i["stats"][0][0] == "속력" and i["traits"] and i["positions"] and i["pay"], i
    assert POSITIONS[28] == "SUB" and card(201200104, 8)["name"] == "손흥민"
    keep = (ROOT / "watchlist.json").read_text()
    (ROOT / "watchlist.json").write_text(json.dumps({OWNERS[0]: [{"spid": 201200104, "grade": 8}, {"spid": "x", "grade": 8}, {"spid": 201200104, "grade": 99}, 7], "남": [{"spid": 201200104, "grade": 1}], OWNERS[1]: "x"}))
    try:
        assert read_watch() == {OWNERS[0]: [{"spid": 201200104, "grade": 8}], OWNERS[1]: [], OWNERS[2]: [], OWNERS[3]: []}, read_watch()
    finally:
        (ROOT / "watchlist.json").write_text(keep)
    print("점검 통과. 손흥민(8강) 현재가", p["now"], "/ 특성", i["traits"])


if __name__ == "__main__":
    try:
        {"build": build, "check": check}.get(" ".join(sys.argv[1:]), preview)()
    except RuntimeError as e:
        sys.exit(str(e))
