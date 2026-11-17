#!/usr/bin/env python3
"""FC온라인 구단 시세 트래커 (표준 라이브러리만 사용)

실행   : python3 app.py        → 맥 http://localhost:8000 , 휴대폰(같은 와이파이) http://<맥 IP>:8000
배포용 : python3 app.py build  → site/ 폴더에 정적 페이지 생성 (GitHub Actions 가 주기적으로 실행)
API 키 : api_key.txt 한 줄 또는 환경변수 NEXON_API_KEY (없으면 관심선수 시세만 동작)
점검   : python3 app.py check

구단 명단 : 넥슨 오픈 API (구단주명 → ouid → 최근 경기 → 출전 선수 18명)
시세      : FC온라인 데이터센터 페이지 (오픈 API 에 시세 조회가 없음. 비공식이라 페이지가 바뀌면 parse_price 수정)
"""
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
OWNERS = ["456화이팅", "우끼끼하하", "게임좀그만해라", "이게임초보인데"]
REFRESH_MIN = 10  # 시세 갱신 주기(분)
SQUAD_MIN = 60  # 명단 갱신 주기(분). 넥슨 API 일일 호출 한도를 아끼려고 시세보다 길게 둠
PORT = 8000
KEY_FILE = ROOT / "api_key.txt"
WATCH_FILE = ROOT / "watchlist.json"


def api_key():
    return os.environ.get("NEXON_API_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else "")


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


# ponytail: 메타데이터는 시작할 때만 읽음. 새 시즌 선수 이름이 숫자로 나오면 재시작
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


@lru_cache(maxsize=None)
def ouid(nick):
    return nexon("id", nickname=nick)["ouid"]


@lru_cache(maxsize=64)
def match_detail(match_id):
    return nexon("match-detail", matchid=match_id)


def pick_squad(details, owner_ouid):
    """매치 상세 여러 개 중 가장 최근 경기에서 해당 구단주의 출전 명단을 고름"""
    latest = max(details, key=lambda d: d["matchDate"])
    me = next(i for i in latest["matchInfo"] if i["ouid"] == owner_ouid)
    players = [{"spid": p["spId"], "grade": p["spGrade"], "pos": p["spPosition"]} for p in me["player"]]
    return players, latest["matchDate"]


def squad(nick):
    oid = ouid(nick)
    # 50 공식경기, 52 감독모드 각각의 최근 1경기 중 더 최근 것
    ids = [m for mt in (50, 52) for m in nexon("user/match", ouid=oid, matchtype=mt, offset=0, limit=1)]
    if not ids:
        raise RuntimeError("최근 공식경기, 감독모드 기록이 없어 명단을 알 수 없음")
    return pick_squad([match_detail(i) for i in ids], oid)


# ---------- 데이터센터 : 시세 ----------

def parse_price(html):
    def series(key):
        return re.findall(r'"([^"]+)"', re.search(rf'"{key}":\s*\[(.*?)\]', html, re.S).group(1))
    now = re.search(r'alt="([\d,]+)"', html)
    return {
        "now": int(now.group(1).replace(",", "")) if now else 0,  # 0 = 시세 없음
        "hist": [int(float(v)) for v in series("value")][-30:],  # 일별, 마지막 값이 어제
    }


def fetch_price(spid, grade):
    data = urllib.parse.urlencode({"spid": spid, "n1strong": grade}).encode()
    return parse_price(get("https://fconline.nexon.com/datacenter/PlayerPriceGraph", data).decode())


# ---------- 상태 ----------

PRICES = {}  # (spid, grade) -> {"now", "hist"}
SQUADS = {n: {"players": [], "match": None, "error": "불러오는 중", "at": 0} for n in OWNERS}
UPDATED = [0]


def load_watch():
    return json.loads(WATCH_FILE.read_text()) if WATCH_FILE.exists() else []


def refresh():
    for n in OWNERS:
        s = SQUADS[n]
        if not api_key():
            s["error"] = "넥슨 오픈 API 키가 없어 명단을 불러오지 못함"
            continue
        if not s["error"] and time.time() - s["at"] < SQUAD_MIN * 60:
            continue
        try:
            players, date = squad(n)
            SQUADS[n] = {"players": players, "match": date, "error": None, "at": time.time()}
        except Exception as e:
            s["error"] = str(e)  # 이전 명단은 유지, 다음 갱신 때 다시 시도
            print("명단 조회 실패", n, e, file=sys.stderr)
    wanted = {(p["spid"], p["grade"]) for s in SQUADS.values() for p in s["players"]}
    wanted |= {(w["spid"], w["grade"]) for w in load_watch()}
    for k in wanted:
        try:
            PRICES[k] = fetch_price(*k)
        except Exception as e:
            print("시세 조회 실패", k, e, file=sys.stderr)
        time.sleep(0.2)  # 데이터센터에 부담 주지 않게 천천히
    UPDATED[0] = time.time()


def loop():
    while True:
        try:
            refresh()
        except Exception as e:
            print("갱신 실패", e, file=sys.stderr)
        time.sleep(REFRESH_MIN * 60)


def card(spid, grade, pos=None):
    season = SEASONS.get(spid // 1000000, {})
    return {
        "spid": spid, "grade": grade, "pos": POSITIONS.get(pos, ""),
        "name": NAMES.get(spid, str(spid)),
        "season": season.get("className", "").split(" (")[0], "seasonImg": season.get("seasonImg"),
        **PRICES.get((spid, grade), {"now": None, "hist": []}),
    }


def state():
    def order(p):  # 공격수부터, 교체는 맨 뒤
        return (POSITIONS.get(p["pos"]) == "SUB", -p["pos"])
    return {
        "updated": UPDATED[0],
        "owners": [{"nick": n, "error": s["error"], "match": s["match"],
                    "players": [card(**p) for p in sorted(s["players"], key=order)]}
                   for n, s in SQUADS.items()],
        "watch": [card(**w) for w in load_watch()],
    }


def search(q):
    q = q.strip()
    return [card(i, 1) for i, n in NAMES.items() if q in n][:100] if q else []


# ---------- 웹 ----------

class Handler(BaseHTTPRequestHandler):
    def reply(self, body, ctype="application/json", code=200):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            self.reply((ROOT / "index.html").read_bytes(), "text/html")
        elif u.path == "/api/state":
            self.reply(state())
        elif u.path == "/api/search":
            self.reply(search(urllib.parse.parse_qs(u.query).get("q", [""])[0]))
        else:
            self.reply({"error": "없는 주소"}, code=404)

    def do_POST(self):
        try:
            # JSON 만 받음 (다른 사이트가 브라우저를 통해 몰래 보내는 요청 차단)
            assert self.headers.get("Content-Type") == "application/json"
            assert self.path in ("/api/watch", "/api/unwatch")
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 1000)))
            k = (int(body["spid"]), int(body["grade"]))
            assert k[0] in NAMES and 1 <= k[1] <= 13
        except Exception:
            return self.reply({"error": "잘못된 요청"}, code=400)
        watch = [w for w in load_watch() if (w["spid"], w["grade"]) != k]
        if self.path == "/api/watch":
            watch.append({"spid": k[0], "grade": k[1]})
            try:
                PRICES[k] = fetch_price(*k)
            except Exception:
                pass  # 다음 갱신 때 채워짐
        WATCH_FILE.write_text(json.dumps(watch))
        self.reply(state())

    def log_message(self, *a):
        pass


def build():
    """정적 페이지 생성. 실행마다 상태가 사라지므로 직전 배포 결과에서 명단을 이어받아 넥슨 API 호출을 줄임"""
    repo = os.environ.get("GITHUB_REPOSITORY")  # GitHub Actions 에서만 있음. 예 : 계정/저장소
    if repo:
        try:
            owner, name = repo.split("/")
            prev = json.loads(get(f"https://{owner}.github.io/{name}/data.json"))["squads"]
            SQUADS.update({n: s for n, s in prev.items() if n in SQUADS})
        except Exception as e:
            print("직전 배포 결과 없음 (첫 실행이면 정상)", e, file=sys.stderr)
    refresh()
    if not PRICES and (load_watch() or any(s["players"] for s in SQUADS.values())):
        sys.exit("조회할 선수가 있는데 시세를 하나도 받지 못해 배포하지 않음 (이전 페이지 유지)")
    out = ROOT / "site"
    out.mkdir(exist_ok=True)
    (out / "data.json").write_text(json.dumps({**state(), "squads": SQUADS}, ensure_ascii=False), encoding="utf-8")
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "const STATIC = false" in html
    (out / "index.html").write_text(html.replace("const STATIC = false", "const STATIC = true"), encoding="utf-8")
    print("생성 완료 :", out, "/ 시세", len(PRICES), "건")


def check():
    new = {"matchDate": "2026-11-01T00:00:00", "matchInfo": [
        {"ouid": "a", "player": [{"spId": 1, "spGrade": 5, "spPosition": 25}]}, {"ouid": "b", "player": []}]}
    old = {"matchDate": "2026-10-01T00:00:00", "matchInfo": [{"ouid": "a", "player": []}]}
    assert pick_squad([old, new], "a") == ([{"spid": 1, "grade": 5, "pos": 25}], new["matchDate"])
    p = fetch_price(201200104, 8)  # 실제 데이터센터 조회
    assert p["now"] > 0 and len(p["hist"]) == 30, p
    assert fetch_price(999999999, 1)["now"] == 0
    assert POSITIONS[28] == "SUB" and card(201200104, 8)["name"] == "손흥민"
    print("점검 통과. 손흥민(8강) 현재가", p["now"])


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        sys.exit(check())
    if sys.argv[1:] == ["build"]:
        sys.exit(build())
    threading.Thread(target=loop, daemon=True).start()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))  # 실제 전송 없음. 내 IP 확인용
            ip = s.getsockname()[0]
        except OSError:
            ip = "<맥 IP>"
    print(f"맥      : http://localhost:{PORT}\n휴대폰  : http://{ip}:{PORT}  (같은 와이파이)\n종료    : Ctrl+C")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
