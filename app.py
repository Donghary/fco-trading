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
from collections import Counter
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
SITE = ROOT / "site"
# 구단주 추가 : 이 목록에 닉네임만 넣으면 됨. 다음 실행에서 그 사람의 30일 치 경기를 받아 옴. 순서를 바꾸지 말고 뒤에 붙일 것(화면 주소 #/o/번호 가 이 순서)
OWNERS = ["456화이팅", "우끼끼하하", "게임좀그만해라", "이게임초보인데"]
REPO = os.environ.get("GITHUB_REPOSITORY", "Donghary/fco-trading")  # 관심선수 저장 위치
REFRESH_MIN = 10  # 미리보기 모드의 갱신 주기(분). 배포 주기는 .github/workflows/update.yml
SQUAD_MIN = 25  # 명단과 경기 목록 갱신 주기(분). 서비스 단계 키라 매 실행(30분)마다 확인
INTRA_DAYS = 7  # 시간대별 시세 보관 기간(일)
PAY_PER_RUN = 400  # 공식경기 상대 선수 급여를 실행당 몇 명까지 새로 받을지
INFO_DAYS = 7  # 스탯, 특성 재조회 주기(일)
MATCH_PER_RUN = 600  # 한 번에 받을 경기 상세 수. 한도에 걸리면 그 실행은 멈추고 다음 실행에서 이어 받음
VALUE_DAYS = 120  # 실제 구단 가치 기록 보관 기간(일)
OPP_PER_RUN = 15  # 한 번에 상대 구단 가치를 매길 공식경기 수 (경기당 데이터센터 시세 조회 최대 18번)
GROW = 4  # 능력치 기준 적응도 : 0~4 가 게임의 1~5 단계. 팀컬러는 화면의 오버롤 계산에서 더함
INFO_V = 2  # parse_info 결과 형식. 올리면 저장된 능력치 정보를 다음 실행에서 전부 다시 받음 (2 : 팀컬러 목록 추가)
TC_PER_RUN = 150  # 팀컬러 효과표를 실행당 몇 개까지 새로 받을지
TC_DAYS = 30  # 팀컬러 효과 재조회 주기(일)
ENHANCE_TC = (10004, 10001, 10002, 10003)  # 강화 팀컬러 : 백금빛(11강↑), 금빛(8강↑), 은빛(5강↑), 동빛(3강↑) 물결
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
    time.sleep(0.05)  # 서비스 단계 키 : 초당 500건, 하루 2,000만 건
    url = f"https://open.api.nexon.com/fconline/v1/{path}?{urllib.parse.urlencode(q)}"
    try:
        return json.loads(get(url, headers={"x-nxopen-api-key": api_key()}))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"넥슨 API 오류 {e.code} : {e.read().decode(errors='replace')[:200]}")


def pick_squad(details, owner_ouid):
    """매치 상세 여러 개 중 명단이 온전한(11명 이상) 가장 최근 경기에서 해당 구단주의 출전 명단을 고름.
    몰수, 접속 끊김 경기는 명단이 비어 있음 (실제 3건 확인). 그런 경기만 있으면 None : 이전 명단을 유지함"""
    full = []
    for d in details:
        me = next((i for i in d["matchInfo"] if i["ouid"] == owner_ouid), None)
        if me and len(me.get("player") or []) >= 11:
            full.append((d["matchDate"], me))
    if not full:
        return None
    date, me = max(full, key=lambda x: x[0])
    return [{"spid": p["spId"], "grade": p["spGrade"], "pos": p["spPosition"]} for p in me["player"]], date


def refresh_squad(nick, s):
    """s 를 최신 명단으로 고침. ouid 와 최근 경기 번호를 기억해 넥슨 API 호출을 줄임"""
    s["ouid"] = s.get("ouid") or nexon("id", nickname=nick)["ouid"]
    # 명단은 공식경기 기준 (감독모드 실험 명단으로 구단 가치가 출렁이지 않게, 2026-11-20 사용자 선택). 공식경기가 전혀 없을 때만 감독모드
    official = nexon("user/match", ouid=s["ouid"], matchtype=50, offset=0, limit=20)
    s["official"] = official[:10]
    STATE["off"][nick] = list(dict.fromkeys(official + STATE["off"].get(nick, [])))
    ids = official[:3] or nexon("user/match", ouid=s["ouid"], matchtype=52, offset=0, limit=1)  # 공식 3경기 : 최근 경기 명단이 비었을 때 대비
    if not ids:
        raise RuntimeError("최근 공식경기, 감독모드 기록이 없어 명단을 알 수 없음")
    if ids != s.get("ids"):
        picked = pick_squad([nexon("match-detail", matchid=i) for i in ids], s["ouid"])
        if picked:
            s["players"], s["match"] = picked
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
                "season": md.get("seasonId"), "goals": goals,  # 시즌 번호는 사람마다 다를 수 있어 각자 것을 씀
                "lu": [[q.get("spId"), q.get("spGrade"), q.get("spPosition")] for q in x.get("player") or []]}  # 그 경기 명단
    return {"v": 2, "date": d["matchDate"], "type": d.get("matchType"), "p": [side(x) for x in d.get("matchInfo", [])]}  # v2 : 골 위치 포함


def refresh_matches():
    """구단주끼리 붙은 클래식 1on1(40) 과 각자의 공식경기(50) 상세를 받아 쌓음. 넥슨은 최근 약 30일만 주므로 받은 것은 계속 보관"""
    owners = {n: s["ouid"] for n, s in STATE["squads"].items() if s.get("ouid")}
    if len(owners) < 2:
        return
    by_ouid, cache = {o: n for n, o in owners.items()}, STATE["matches"]
    # 30일 치 전체 받기는 구단주마다 한 번. 예전 상태의 True 는 지금 구단주 전원을 받은 것으로 봄 (구단주를 늘리면 새 사람만 받음)
    for k in ("seeded", "off_seeded"):
        if not isinstance(STATE[k], list):
            STATE[k] = list(owners) if STATE[k] else []
    # 새 구단주가 있으면 1대1 목록은 전원을 끝까지 넘김 : 새 사람과 기존 사람의 옛 경기도 양쪽 목록에 다 보여야 상대전적으로 잡힘
    # 갱신이 3시간 넘게 끊겼다 이어져도(점검, 장애) 전원을 끝까지 넘김 : 짧은 목록(30건)만 보면 그 사이 경기가 빠짐.
    # m_at 은 배포된 실행에서만 저장되므로 끊긴 시간이 그대로 반영됨
    gap = time.time() - STATE["m_at"] > 3 * 3600
    first = bool(set(owners) - set(STATE["seeded"])) or gap
    listing = first or time.time() - STATE["m_at"] >= SQUAD_MIN * 60
    seen = {}
    for n, o in owners.items() if listing else []:
        for off in range(0, 1000 if first else 1, 100):
            got = nexon("user/match", ouid=o, matchtype=40, offset=off, limit=100 if first else 30)
            for m in got:
                seen.setdefault(m, set()).add(n)
            if len(got) < 100:
                break
    h2h = [m for m, who in seen.items() if len(who) >= 2]  # 두 명 이상의 목록에 같이 있으면 구단주끼리 경기
    if listing:
        STATE["m_at"] = time.time()
    for n, o in owners.items():  # 공식경기 목록 : 구단주마다 처음 한 번만 30일 치 전부. 이후는 명단 갱신 때 같이 받음
        if n in STATE["off_seeded"] and not gap:
            continue
        got = []
        for off in range(0, 1000, 100):
            page = nexon("user/match", ouid=o, matchtype=50, offset=off, limit=100)
            got += page
            if len(page) < 100:
                break
        STATE["off"][n] = list(dict.fromkeys(got + STATE["off"].get(n, [])))
    recent = [m for s in STATE["squads"].values() for m in s.get("official", [])]
    older = [m for ids in STATE["off"].values() for m in ids]
    old = lambda r: r.get("v") != 2 or (r.get("type") == 50 and any("season" not in p or "lu" not in p for p in r["p"]))  # 골 위치, 시즌 번호, 명단이 없는 예전 기록
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
    left = [m for m in todo if m not in cache or old(cache[m])]  # 실패했거나 아직 못 받은 것 전부 (성공 수로 자르면 실패한 id 가 빠짐)
    keep = set(recent) | {m for ids in STATE["off"].values() for m in ids}
    STATE["matches"] = {m: r for m, r in cache.items() if r.get("type") == 40 or m in keep}
    STATE["off"] = {n: [m for m in ids if m in STATE["matches"] or m in left] for n, ids in STATE["off"].items()}
    STATE["queue"] = [m for m in left if m in h2h or m in STATE["queue"]]
    if listing:
        STATE["seeded"] = sorted(set(STATE["seeded"]) | set(owners))
    STATE["off_seeded"] = sorted(set(STATE["off_seeded"]) | set(owners))
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


def fetch_price(k):
    """시세 조회. 거래되는 카드(최근 일별 시세가 있음)인데 현재가가 0 이면 데이터센터 일시 오류로 봄 (2026-11-20 11:05 실제 발생).
    한 번 더 묻고, 그래도 0 이면 마지막으로 받은 현재가(없으면 어제 값)를 쓰고 stale 로 표시함"""
    p = parse_price(datacenter("PlayerPriceGraph", *k))
    if p["now"] or not any(p["daily"][-3:]):
        return p
    time.sleep(0.5)
    p = parse_price(datacenter("PlayerPriceGraph", *k))
    if not p["now"]:
        last = (STATE["intra"].get(f"{k[0]}_{k[1]}") or [[0, 0]])[-1][1]
        p["now"], p["stale"] = last or next(v for v in reversed(p["daily"]) if v), True
        print("현재가 0 응답, 마지막 값으로 대체", k, file=sys.stderr)
    return p


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
        # 이 카드가 쓸 수 있는 팀컬러 [[번호, 이름]] : 소속(클럽, 국가, 시즌)과 관계(특정 시즌 동료). 0 은 "선택 안 함" 항목
        # 같은 tdefault 목록 앞쪽에 강화 팀컬러(10001~, "Lv.1 금빛 물결")가 섞여 있어 뺌. 강화는 ENHANCE_TC 로 따로 다룸
        "tc": [[int(i), clean(n)] for i, n in re.findall(r'class="selector_item tdefault(\d+)"[^>]*>([^<]+)<', html) if i != "0" and not 10000 < int(i) < 10100],
        "tcr": [[int(i), clean(n)] for i, n in re.findall(r'class="selector_item tspecial(\d+)"[^>]*>([^<]+)<', html) if i != "0"],
    }


def parse_tcolor(html):
    """팀컬러 상세 : 단계마다 [필요 인원, {"all": 전체 능력치, 능력치 이름: 추가}]. 강화 팀컬러는 min(최소 강화)도 담음"""
    name = re.search(r'<strong>(.*?) : </strong>', html)
    lv = []
    for block in re.findall(r'<div class="level lv[a-z]?\d+">(.*?)</ul>', html, re.S):  # 소속 lv1, 강화 lvu1, 황금세대와 듀오 같은 특수 lvs1
        need = re.search(r'class="num">(\d+)<', block)
        eff = {("all" if s == "전체 능력치" else s.replace("발리 슛", "발리슛")): int(v)
               for s, v in re.findall(r'<li>\s*([^<+]+?) \+(\d+)\s*</li>', block)}
        lv.append([int(need.group(1)) if need else 0, eff])
    low = re.search(r'(\d+)강 이상의 선수들로', html)
    return {"name": clean(name.group(1)) if name else "", "lv": lv, **({"min": int(low.group(1))} if low else {})}


def tcolor_detail(tid):  # 데이터센터 팀컬러 팝업. 이 두 헤더가 없으면 오류 페이지로 넘어감
    return parse_tcolor(get(f"{DATACENTER}TeamColorDetail?teamcolorid={tid}",
                            headers={"X-Requested-With": "XMLHttpRequest", "Referer": DATACENTER + "teamcolor"}).decode(errors="replace"))


def datacenter(page, spid, grade, **extra):
    data = urllib.parse.urlencode({"spid": spid, "n1strong": grade, **extra}).encode()
    return get(DATACENTER + page, data).decode(errors="replace")


# ---------- 상태 (직전 결과를 이어받음) ----------

STATE = {"squads": {}, "intra": {}, "info": {}, "matches": {}, "value": {}, "queue": [], "seeded": False, "off": {}, "off_seeded": False, "m_at": 0, "pay": {}, "seed_used": False, "ok_at": None, "tcolor": {}}
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
        # 404 도 중단함 : Pages 장애나 설정 변경 때 빈 상태로 시작해 쌓인 기록을 덮어쓰지 않기 위함.
        # 정말 처음부터 시작할 때만 FRESH=1 로 실행 (워크플로 수동 실행)
        if e.code != 404 or not os.environ.get("FRESH"):
            raise
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
        "pay": STATE["pay"].get(str(spid)) or None,  # 급여. 강화와 무관하게 카드마다 고정. 선발 11명 합계 상한 315 (사용자 기준)
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
        row = {"d": r["date"], "s": me.get("season"), "dv": me["div"], "r": me["res"], "e": me["end"], "g": me["goal"], "og": op["goal"], "op": op["nick"],
               "pk": [me["pk"], op["pk"]], "div": DIVISIONS.get(me["div"], "")}
        if goals:
            row.update(poss=me["poss"], sh=[me["sh"], op["sh"]], sot=[me["sot"], op["sot"]],
                       mg=[named(g) for g in me["goals"]], og2=[named(g) for g in op["goals"]], ov=op.get("ov"),
                       olu=[[NAMES.get(q[0], str(q[0])), q[1], POSITIONS.get(q[2], ""), q[3] if len(q) > 3 else None,
                             SEASONS.get(q[0] // 1000000, {}).get("seasonImg"), STATE["pay"].get(str(q[0]))] for q in op.get("lu", [])] if "ov" in op else None)
        rows.append(row)
    return sorted(rows, key=lambda r: r["d"], reverse=True)


def baselines():
    """게임 안에서 확인한 누적 기록 (사용자가 알려준 값). asof 시각까지의 기록이고, 그 뒤 경기는 받은 기록으로 더함"""
    f = ROOT / "baselines.json"
    return json.loads(f.read_text()) if f.exists() else {}


def season_split():
    """시즌 : 구단주 본인 쪽 seasonId 기준. 다른 번호의 기간 안에 끼어 잠깐 나오는 번호(예 : 202606 기간 중 11.06 밤 202611)는
    월드클래스 리그 같은 리그 경기임. 공식경기로 기록되지만 공식경기 전적에는 들어가지 않아(사용자 확인) 따로 묶고 시즌 전적에서 뺌"""
    rows = {n: official_rows(n, STATE["off"].get(n, []), goals=True) for n in OWNERS}
    span = {}
    for rs in rows.values():
        for r in rs:
            if r["s"]:
                lo, hi = span.get(r["s"], (r["d"], r["d"]))
                span[r["s"]] = (min(lo, r["d"]), max(hi, r["d"]))
    side = {k for k, (lo, hi) in span.items() if any(a < lo and hi < b for o, (a, b) in span.items() if o != k)}
    main = sorted((k for k in span if k not in side), key=lambda k: span[k][1], reverse=True)
    return rows, span, side, main


def mdate(d):  # 넥슨 시각(UTC) → 한국 날짜 "11.06"
    t = time.gmtime(time.mktime(time.strptime(d[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone + 9 * 3600)
    return f"{t.tm_mon}.{t.tm_mday:02d}"


def write_official():
    """공식경기 : 지금 시즌은 official.json, 지난 시즌은 official_<시즌>.json 으로 나눠 씀. 시즌 요약은 모든 시즌을 담음"""
    rows, span, side, main = season_split()
    for rs in rows.values():
        for r in rs:
            if not r["s"]:  # 시즌 번호를 아직 못 받은 경기는 날짜로 추정
                r["s"] = next((k for k in main if span[k][0] <= r["d"] <= span[k][1]), main[0] if main else None)
            r["s"] = str(r["s"]) if r["s"] else "전체"

    def league_name(n, k):
        """리그는 전 시즌 마감 등급으로 정해짐 (사용자 확인) : 챌린저 이상 → 레전더리 리그, 월드클래스와 마스터 → 월드클래스 리그, 프로 이하 → 프로 리그"""
        mine = [r for r in rows[n] if r["s"] == str(k)]
        start = min(r["d"] for r in mine)
        now = next((m for m in main if span[m][0] <= start <= span[m][1]), None)
        prev = max((m for m in main if now and span[m][1] < span[now][0]), key=lambda m: span[m][1], default=None)
        last = next((r["dv"] for r in rows[n] if r["s"] == str(prev) and r["dv"]), None)  # 행은 최신순이라 첫 값이 마감 등급
        name = "리그" if not last else "레전더리 리그" if last <= 1300 else "월드클래스 리그" if last <= 2200 else "프로 리그"
        return name  # 날짜는 화면에서 시즌 줄 아래에 붙임

    labels = {str(k): f"{str(k)[:4]}-{int(str(k)[4:])}차" for k in main} | {str(k): "리그" for k in side} | {"전체": "시즌 구분 전"}  # 202606 → 2026-6차 (게임 표기)
    seasons = [str(k) for k in main] + [str(k) for k in sorted(side, key=lambda k: span[k][0], reverse=True)] + ["전체"]
    seasons = [x for x in seasons if any(r["s"] == x for rs in rows.values() for r in rs)]
    cur = seasons[0] if seasons else None
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
            by.setdefault(r["s"], []).append(r)
        out[n]["sum"] = {str(k): summary(n, k, v) for k, v in by.items() if k}
        out[n]["rows"] = by.get(cur, [])
        out[n]["labels"] = {str(k): league_name(n, k) for k in side if str(k) in by}
    write("official.json", {"pending": PENDING[0], "current": cur, "order": seasons, "labels": labels, "files": {x: i for i, x in enumerate(seasons[1:], 1)}, "owners": out})
    for i, season in enumerate(seasons[1:], 1):
        write(f"official_{i}.json", {"owners": {n: [r for r in rs if r["s"] == season] for n, rs in rows.items()}})


def official_of(nick, s, league=()):
    """구단주의 최근 공식경기 요약 : 경기별 결과와 가장 최근 경기의 등급. 리그 경기는 공식경기 전적이 아니라 뺌"""
    rows = [r for r in official_rows(nick, s.get("official", [])) if r["s"] not in league]
    return {"official": rows, "div": rows[0]["div"] if rows else ""}


def fill_pay(limit=PAY_PER_RUN):
    """급여 캐시 STATE["pay"] = {spid: 급여}. 우리 선수는 이미 받은 능력치에서, 공식경기 상대 선수는 데이터센터에서 실행당 조금씩"""
    for k, info in STATE["info"].items():
        if info.get("pay"):
            STATE["pay"][k.split("_")[0]] = int(info["pay"])
    todo = sorted({q[0] for r in STATE["matches"].values() if r.get("type") == 50 for side in r["p"] for q in side.get("lu", [])
                   if str(q[0]) not in STATE["pay"]}, reverse=True)
    for spid in todo[:limit]:
        try:
            info = parse_info(datacenter("PlayerAbility", spid, 1))
        except Exception as e:
            print("급여 조회 실패", spid, e, file=sys.stderr)
            break  # 막혔으면 다음 실행 때 이어서
        if info["pay"] or info["positions"]:  # 정상 페이지인데 급여가 없으면 0 으로 적어 다시 묻지 않음
            STATE["pay"][str(spid)] = int(info["pay"] or 0)
        time.sleep(0.1)
    return max(0, len(todo) - limit)


def fill_tcolor(limit=TC_PER_RUN):
    """팀컬러 효과표 캐시 STATE["tcolor"] = {번호: 상세}. 추적 카드에 나오는 팀컬러와 강화 팀컬러만, 실행당 조금씩"""
    now = int(time.time())
    ids = {str(i) for i in ENHANCE_TC} | {str(i) for info in STATE["info"].values() for f in ("tc", "tcr") for i, _ in info.get(f, [])}
    todo = sorted(i for i in ids if now - STATE["tcolor"].get(i, {}).get("at", 0) >= TC_DAYS * 86400 or not STATE["tcolor"][i].get("lv"))
    for tid in todo[:limit]:
        try:
            t = tcolor_detail(tid)
        except Exception as e:
            print("팀컬러 조회 실패", tid, e, file=sys.stderr)
            break
        if not t["name"]:  # 오류 페이지로 넘어감 (요청 방식이 바뀐 경우). 비워 두고 다음 실행에서 다시
            print("팀컬러 상세를 읽지 못함", tid, file=sys.stderr)
            break
        STATE["tcolor"][tid] = {**t, "at": now}
        time.sleep(0.1)
    return max(0, len(todo) - limit)


def tc_level(tid, n):
    """n명일 때 켜지는 단계 (0 = 안 켜짐)"""
    return sum(1 for need, _ in STATE["tcolor"].get(str(tid), {}).get("lv", []) if n >= need)


def owner_colors(players):
    """선발 11명으로 켜질 팀컬러 추정 : 소속, 강화, 관계에서 각각 효과가 가장 큰 것 하나 → {"main": [번호, 단계], ...}
    게임에서 직접 고른 팀컬러와 다를 수 있어 화면에서 바꿀 수 있게 함"""
    xi = [p for p in players if p["pos"] != "SUB"]
    infos = [STATE["info"].get(f"{p['spid']}_{p['grade']}", {}) for p in xi]
    score = lambda tid, lv: sum(v * (34 if k == "all" else 1) for k, v in STATE["tcolor"][str(tid)]["lv"][lv - 1][1].items())

    def best(counts):
        cand = [(score(t, lv), t, lv) for t, n in counts.items() if (lv := tc_level(t, n))]
        return [max(cand)[1], max(cand)[2]] if cand else None
    # 팀컬러별 해당 선수 수. 단일 팀(90000000)은 모든 카드 목록에 있지만 "한 클럽 선수만"이 조건이라 세는 방식으로는 판단 못 해 뺌
    count = lambda src: Counter(i for info in infos for i in {i for i, _ in info.get(src, [])} if i != 90000000)
    return {"main": best(count("tc")), "rel": best(count("tcr")),
            "enh": best({t: sum(p["grade"] >= STATE["tcolor"].get(str(t), {}).get("min", 99) for p in xi) for t in ENHANCE_TC})}


def value_opponents(prices):
    """공식경기 상대의 그 경기 명단에 지금 시세를 매겨 저장. 경기마다 한 번만 계산하고 최신 경기부터 조금씩"""
    cache = dict(prices)  # 이번 실행에서 이미 받은 시세는 다시 받지 않음
    todo = sorted((m for ids in STATE["off"].values() for m in ids if m in STATE["matches"]
                   and STATE["matches"][m].get("type") == 50 and "ov" not in STATE["matches"][m]
                   and all("lu" in p for p in STATE["matches"][m]["p"])),
                  key=lambda m: STATE["matches"][m]["date"], reverse=True)
    for m in todo[:OPP_PER_RUN]:
        r = STATE["matches"][m]
        for side in r["p"]:
            if side["own"]:
                continue
            total, ok = 0, True
            for q in side["lu"]:
                k = (q[0], q[1])
                if k not in cache:
                    try:
                        cache[k] = fetch_price(k)
                    except Exception as e:
                        print("상대 시세 실패", k, e, file=sys.stderr)
                        ok = False
                        break
                    time.sleep(0.1)
                q[3:] = [cache[k]["now"]]
                total += cache[k]["now"]
            if ok:
                side["ov"] = total
        if all(side["own"] or "ov" in side for side in r["p"]):
            r["ov"] = int(time.time())  # 매긴 시각
    return len(todo) - min(len(todo), OPP_PER_RUN)


def write(name, obj):
    f = SITE / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def build():
    load_state()
    seed = ROOT / "seed_matches.json"  # 로컬에서 미리 받아 둔 경기 상세. 넥슨 일일 한도를 아끼려고 처음 한 번만 씀
    if seed.exists() and not STATE["seed_used"] and not STATE["seeded"]:  # 완전히 빈 상태에서만 한 번 (seeded 는 구단주 목록이라 따로 표시)
        STATE["matches"].update(json.loads(seed.read_text()))
        STATE["seed_used"] = True
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
            prices[k] = fetch_price(k)
            if prices[k]["now"] and not prices[k].get("stale"):  # 대체한 값은 시간대별 기록에 넣지 않음
                add_intra(key, now, prices[k]["now"])
            info = STATE["info"].get(key, {})
            if now - info.get("at", 0) >= INFO_DAYS * 86400 or info.get("grow") != GROW or info.get("v") != INFO_V:
                page = datacenter("PlayerAbility", *k, n1Grow=GROW)
                STATE["info"][key] = {**parse_info(page), "at": now, "grow": GROW, "v": INFO_V}
        except Exception as e:
            print("데이터센터 조회 실패", k, e, file=sys.stderr)
        time.sleep(0.1)  # 데이터센터에 부담 주지 않게 천천히
    if keys and not any(p["now"] for p in prices.values()):  # 하나도 못 받았거나 전부 0 (페이지 구조가 바뀐 경우)
        # 목요일 패치 점검 등으로 데이터센터가 막힘(403). 점검 중엔 거래도 없으니 배포를 건너뛰고 이전 페이지를 그대로 둠.
        # 실패로 끝내지 않음 : 점검 내내 실패 메일이 쌓이지 않게 함
        print("시세를 하나도 받지 못함 (넥슨 점검 추정). 이번 배포는 건너뜀", file=sys.stderr)
        if STATE["ok_at"] and now - STATE["ok_at"] > 14 * 3600:  # 목요일 점검(길면 11시간 안팎)보다 길면 고장일 수 있음 : 실패로 끝내 메일이 오게 함
            raise RuntimeError(f"데이터센터에서 시세를 {(now - STATE['ok_at']) // 3600}시간째 받지 못함. 페이지 구조 변경이나 차단 확인 필요")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("skip=true\n")
        return

    try:
        left = value_opponents(prices)
        if left:
            print("상대 구단 가치", left, "경기는 다음 갱신 때 매김")
    except Exception as e:
        print("상대 구단 가치 실패", e, file=sys.stderr)

    try:
        left = fill_pay()
        if left:
            print("상대 선수 급여", left, "명은 다음 갱신 때 받음")
    except Exception as e:
        print("급여 채우기 실패", e, file=sys.stderr)

    try:
        left = fill_tcolor()
        if left:
            print("팀컬러 효과", left, "개는 다음 갱신 때 받음")
    except Exception as e:
        print("팀컬러 채우기 실패", e, file=sys.stderr)

    for n, s in squads.items():  # 실제 구단 가치 : 명단의 모든 선수 시세를 받았을 때만 기록
        ks = [(p["spid"], p["grade"]) for p in s["players"]]
        if ks and all(k in prices for k in ks):
            add_point(STATE["value"].setdefault(n, []), now, sum(prices[k]["now"] for k in ks))
    vcut = now - VALUE_DAYS * 86400
    STATE["value"] = {n: [p for p in v if p[0] >= vcut] for n, v in STATE["value"].items() if n in OWNERS}
    # 한 번만 툭 떨어졌다 돌아온 점은 조회 오류로 보고 지움 (앞뒤 값보다 20% 넘게 낮은 단일 점)
    # ponytail: 단일 점만 잡음. 여러 번 이어진 오류는 fetch_price 의 대체값이 막음
    STATE["value"] = {n: [p for i, p in enumerate(v) if not (0 < i < len(v) - 1 and p[1] < .8 * min(v[i - 1][1], v[i + 1][1]))]
                      for n, v in STATE["value"].items()}

    cutoff = now - INTRA_DAYS * 86400
    STATE["intra"] = {k: [p for p in v if p[0] >= cutoff] for k, v in STATE["intra"].items()}
    STATE["intra"] = {k: v for k, v in STATE["intra"].items() if v}

    STATE["ok_at"] = now
    write("state.json", STATE)
    league = season_split()[2]
    write("data.json", {
        "updated": now, "repo": REPO, "v": hashlib.md5((ROOT / "index.html").read_bytes()).hexdigest()[:8],
        "owners": [{"nick": n, "error": s.get("error"), "match": s["match"], **official_of(n, s, league), "tc": owner_colors(s["players"]),
                    "players": [card(**p, price=prices.get((p["spid"], p["grade"]))) for p in s["players"]]}
                   for n, s in squads.items()],
        "watch": {n: [card(**w, price=prices.get((w["spid"], w["grade"]))) for w in ws] for n, ws in watch.items()},
    })
    write("values.json", STATE["value"])
    # 선수 상세의 오버롤 계산용 : 포지션 가중치와 팀컬러 효과표. 선수 상세를 열 때만 받아감
    w = json.loads((ROOT / "ovr_weights.json").read_text())
    write("calc.json", {"w": w["pos"], "sum": w["summary"],
                        "tc": {i: {k: t[k] for k in ("name", "lv", "min") if k in t} for i, t in STATE["tcolor"].items() if t.get("lv")}})
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
        i: [s["className"].split(" (")[0], s["seasonImg"]] for i, s in SEASONS.items()}, "pay": STATE["pay"]})
    for name in ("index.html", "favicon.png", "icon-180.png", "icon-192.png", "icon-512.png", "manifest.json"):
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
    xi = [{"spId": i, "spGrade": 5, "spPosition": 25} for i in range(11)]
    new = {"matchDate": "2026-11-01T00:00:00", "matchInfo": [{"ouid": "a", "player": []}, {"ouid": "b", "player": xi}]}  # 최근 경기는 몰수(빈 명단)
    old = {"matchDate": "2026-10-01T00:00:00", "matchInfo": [{"ouid": "a", "player": xi}]}
    assert pick_squad([old, new], "a")[1] == old["matchDate"]  # 빈 명단 경기는 건너뜀
    assert pick_squad([new], "a") is None  # 온전한 명단이 없으면 이전 명단 유지

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
    # 오버롤 계산 : 로카텔리 26 TOTS 10강. 가중치 합 / 100 의 버림이 데이터센터 CDM 값과 같아야 함
    loca = parse_info(datacenter("PlayerAbility", 866222077, 10, n1Grow=GROW))
    w, st = json.loads((ROOT / "ovr_weights.json").read_text()), dict(loca["stats"])
    assert sum(v * st[n] for n, v in w["pos"]["CDM/RDM/LDM"].items()) // 100 == dict(loca["positions"])["CDM"], loca["positions"]
    assert all(sum(v * st[n] for n, v in w["summary"][k].items()) // 100 == x for k, x in loca["summary"]), loca["summary"]
    assert [1021, "유벤투스"] in loca["tc"] and loca["tcr"], loca["tc"]
    juve, gold = tcolor_detail(1021), tcolor_detail(10001)
    assert juve["name"] == "유벤투스" and juve["lv"][3] == [11, {"all": 4, "민첩성": 3, "짧은 패스": 3}], juve
    assert gold["min"] == 8 and gold["lv"] == [[5, {"all": 3}], [8, {"all": 4}]], gold
    keep = (ROOT / "watchlist.json").read_text()
    (ROOT / "watchlist.json").write_text(json.dumps({OWNERS[0]: [{"spid": 201200104, "grade": 8}, {"spid": "x", "grade": 8}, {"spid": 201200104, "grade": 99}, 7], "남": [{"spid": 201200104, "grade": 1}], OWNERS[1]: "x"}))
    try:
        assert read_watch() == {OWNERS[0]: [{"spid": 201200104, "grade": 8}], **{n: [] for n in OWNERS[1:]}}, read_watch()
    finally:
        (ROOT / "watchlist.json").write_text(keep)
    print("점검 통과. 손흥민(8강) 현재가", p["now"], "/ 특성", i["traits"])


if __name__ == "__main__":
    try:
        {"build": build, "check": check}.get(" ".join(sys.argv[1:]), preview)()
    except RuntimeError as e:
        sys.exit(str(e))
