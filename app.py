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
SQUAD_MIN = 60  # 명단 갱신 주기(분). 넥슨 API 일일 한도(1,000건)를 아끼려고 시세보다 길게 둠
INTRA_DAYS = 7  # 시간대별 시세 보관 기간(일)
INFO_DAYS = 7  # 스탯, 특성 재조회 주기(일)
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


# ---------- 넥슨 오픈 API : 구단 명단 ----------

def nexon(path, **q):
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
    ids = [m for mt in (50, 52) for m in nexon("user/match", ouid=s["ouid"], matchtype=mt, offset=0, limit=1)]
    if not ids:
        raise RuntimeError("최근 공식경기, 감독모드 기록이 없어 명단을 알 수 없음")
    if ids != s.get("ids"):
        s["players"], s["match"] = pick_squad([nexon("match-detail", matchid=i) for i in ids], s["ouid"])
        s["ids"] = ids
    s["at"] = time.time()


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


def datacenter(page, spid, grade):
    data = urllib.parse.urlencode({"spid": spid, "n1strong": grade}).encode()
    return get(DATACENTER + page, data).decode(errors="replace")


# ---------- 상태 (직전 결과를 이어받음) ----------

STATE = {"squads": {}, "intra": {}, "info": {}}


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
    """시간대별 시세 추가. 같은 값이 이어지면 구간의 처음과 끝만 남겨 용량을 줄임"""
    s = STATE["intra"].setdefault(key, [])
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
        # 대표 오버롤 : 첫 번째 포지션 기준, 강화 단계 반영
        "ovr": next((v for _, v in STATE["info"].get(f"{spid}_{grade}", {}).get("positions", [])), None),
    }


def write(name, obj):
    f = SITE / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def build():
    load_state()
    now = int(time.time())

    squads = STATE["squads"] = {n: STATE["squads"].get(n) or {"players": [], "match": None, "at": 0} for n in OWNERS}
    for n, s in squads.items():
        if not api_key():
            s["error"] = "넥슨 오픈 API 키가 없어 명단을 불러오지 못함"
        elif s.get("error") or now - s["at"] >= SQUAD_MIN * 60:
            try:
                refresh_squad(n, s)
                s["error"] = None
            except Exception as e:
                s["error"] = str(e)  # 이전 명단은 유지, 다음 실행 때 다시 시도
                print("명단 조회 실패", n, e, file=sys.stderr)

    watch = json.loads((ROOT / "watchlist.json").read_text())
    keys = {(p["spid"], p["grade"]) for s in squads.values() for p in s["players"]}
    keys |= {(w["spid"], w["grade"]) for w in watch}
    prices = {}
    for k in sorted(keys):
        key = f"{k[0]}_{k[1]}"
        try:
            prices[k] = parse_price(datacenter("PlayerPriceGraph", *k))
            if prices[k]["now"]:
                add_intra(key, now, prices[k]["now"])
            if now - STATE["info"].get(key, {}).get("at", 0) >= INFO_DAYS * 86400:
                STATE["info"][key] = {**parse_info(datacenter("PlayerAbility", *k)), "at": now}
        except Exception as e:
            print("데이터센터 조회 실패", k, e, file=sys.stderr)
        time.sleep(0.1)  # 데이터센터에 부담 주지 않게 천천히
    if keys and not prices:
        raise RuntimeError("조회할 선수가 있는데 시세를 하나도 받지 못함. 이전 결과를 유지함")

    cutoff = now - INTRA_DAYS * 86400
    STATE["intra"] = {k: [p for p in v if p[0] >= cutoff] for k, v in STATE["intra"].items()}
    STATE["intra"] = {k: v for k, v in STATE["intra"].items() if v}

    write("state.json", STATE)
    write("data.json", {
        "updated": now, "repo": REPO,
        "owners": [{"nick": n, "error": s.get("error"), "match": s["match"],
                    "players": [card(**p, price=prices.get((p["spid"], p["grade"]))) for p in s["players"]]}
                   for n, s in squads.items()],
        "watch": [card(**w, price=prices.get((w["spid"], w["grade"]))) for w in watch],
    })
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
    for name in ("index.html", "favicon.svg", "icon-180.png"):
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
    print("점검 통과. 손흥민(8강) 현재가", p["now"], "/ 특성", i["traits"])


if __name__ == "__main__":
    try:
        {"build": build, "check": check}.get(" ".join(sys.argv[1:]), preview)()
    except RuntimeError as e:
        sys.exit(str(e))
