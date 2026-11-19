"""
백테스트 (replay). LLM · 키움을 다시 부르지 않고, 기록된 판단(확신도)에 규칙 · 기준값만 바꿔 다시 계산합니다.

실전과 같은 규칙을 쓰려고 sizing · risk · monitor 를 그대로 import 하고, 그 함수들이 읽는
'지금 시각 · 체결 기록 · 잔고 · 손절가 · 브리핑'만 시뮬레이션 값으로 바꿔 끼웁니다 (patched).

입력
- journal.jsonl: decision(주문 안 한 판단) + fill(주문한 판단) = 회차별 확신도, regime = 회차별 국면
  관망 · 매수 예정 = 매수 확신도, 보유 · 매도 예정 = 매도 확신도. 손절 · 익절 · 보유일 체결은 시뮬레이션이 따로 만듦
- git 기록: docs/data/briefing.json · holdings.json (회차별 장중 가격 · 지표), agent/config/params.json (그때 기준값)
- 일봉: pykrx (work/backtest/prices 캐시, KRX 만 부름). 판단 시점 가격이 git 기록에 없으면 (고+저+종)/3

근사 (보고서 한계)
- 확신도는 그때 값 고정. 실전에 없던 보유(실제론 관망)는 매도 확신도가 없어 손절 · 익절 · 보유일로만 팖
- 장중 손절 · 익절: 회차 가격으로 먼저 보고, 장 마감에 일봉 저가 · 고가로 한 번 더 (둘 다 닿으면 손절 먼저, 시가가 넘었으면 시가)
"""
import bisect
import copy
import json
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta

import common as C
import monitor
import risk
import sizing

BT = os.path.join(C.WORK, "backtest")
PRICES = os.path.join(BT, "prices")
BENCH = "069500"


# ---------------- 입력: git 기록 ----------------
def _git(*args, inp=None):
    r = subprocess.run(["git", *args], cwd=C.ROOT, input=inp, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace"))
    return r.stdout


def git_history(path, ref="HEAD"):
    """[(커밋 시각 'YYYY-MM-DD HH:MM', 내용 dict)] 오래된 것부터. 캐시(work/backtest/git_*.json)에 없는 커밋만 읽음."""
    cache_path = os.path.join(BT, "git_" + path.replace("/", "_"))
    cache = C.read_json(cache_path, {}) or {}
    log = _git("log", ref, "--format=%H %ad", "--date=format:%Y-%m-%d %H:%M", "--", path).decode("utf-8")
    commits = [ln.split(" ", 1) for ln in log.splitlines() if ln.strip()]
    new = [h for h, _ in commits if h not in cache]
    if new:
        out = _git("cat-file", "--batch", inp="".join(f"{h}:{path}\n" for h in new).encode())
        i = 0
        for h in new:
            nl = out.index(b"\n", i)
            head = out[i:nl].split()
            if head[-1] == b"missing":
                cache[h] = None
                i = nl + 1
                continue
            size = int(head[2])
            body = out[nl + 1: nl + 1 + size]
            i = nl + 1 + size + 1
            try:
                cache[h] = json.loads(body.decode("utf-8"))
            except ValueError:
                cache[h] = None
        C.write_json(cache_path, cache)
    return sorted(((t, cache[h]) for h, t in commits if cache.get(h) is not None), key=lambda x: x[0])


class Snapshots:
    """회차별 장중 가격 · 브리핑 (git 기록). 같은 날 그 시각 이전 마지막 값만 씀."""

    def __init__(self, ref="HEAD"):
        self.px = {}          # code -> [(time, price)]
        self.brief = []       # [(time, briefing)]
        for t, b in git_history("docs/data/briefing.json", ref):
            it = (b.get("intraday") or {}).get("time")
            st = f"{t[:10]} {it}" if it else (b.get("created") or t)
            if st[:10] != t[:10] or st > t:   # 커밋 시각보다 늦을 수 없음
                st = t
            self.brief.append((st, b))
            for c in b.get("candidates", []):
                if c.get("price"):
                    self.px.setdefault(c["code"], []).append((st, float(c["price"])))
        for t, hs in git_history("docs/data/holdings.json", ref):
            for h in hs if isinstance(hs, list) else []:
                if h.get("price"):
                    self.px.setdefault(h["code"], []).append((t, float(h["price"])))
        for v in self.px.values():
            v.sort()
        self.brief.sort(key=lambda x: x[0])
        self.params = git_history("agent/config/params.json", ref)

    def price(self, code, t):
        s = self.px.get(code)
        if not s:
            return None
        i = bisect.bisect_right(s, (t, float("inf")))
        return s[i - 1][1] if i and s[i - 1][0][:10] == t[:10] else None

    def briefing(self, t):
        i = bisect.bisect_right([x[0] for x in self.brief], t)
        return self.brief[i - 1][1] if i and self.brief[i - 1][0][:10] == t[:10] else {}

    def params_at(self, t):
        i = bisect.bisect_right([x[0] for x in self.params], t)
        return self.params[max(0, i - 1)][1]


# ---------------- 입력: 일봉 ----------------
def daily(codes, start, end, fetch=True):
    """{code: {date: (시, 고, 저, 종)}}. 캐시에 end 까지 없으면 pykrx 로 받음 (KRX, 키움 아님)."""
    os.makedirs(PRICES, exist_ok=True)
    out, stock = {}, None
    for code in sorted(codes):
        path = os.path.join(PRICES, f"{code}.json")
        d = C.read_json(path, {}) or {}
        if fetch and d.get("_until", "") < end:
            if stock is None:
                from pykrx import stock  # noqa: PLC0415  무거워서 필요할 때만
            try:
                df = stock.get_market_ohlcv(start.replace("-", ""), end.replace("-", ""), code)
                for ix, row in df.iterrows():
                    o, h, lo, c = (float(x) for x in row.iloc[:4])
                    if c > 0:
                        d[ix.strftime("%Y-%m-%d")] = [o or c, h or c, lo or c, c]
                d["_until"] = end   # 이 날짜까지 받음 (거래 없는 종목을 매번 다시 받지 않게)
                C.write_json(path, d)
            except Exception as e:  # noqa: BLE001
                print(f"  ! 일봉 {code}: {e}", flush=True)
        out[code] = {k: v for k, v in d.items() if k != "_until"}
    return out


# ---------------- 입력: 판단 기록 → 회차 ----------------
def load_runs(rows, start):
    """회차 목록 (시각순). 각 회차 = {id, time, mode, regime, cap, items: {code: 판단}, actual: [실제 체결]}."""
    runs = {}

    def run_of(r):
        rid = r.get("run") or ("t" + r["time"])
        if rid not in runs:
            mode = rid.rsplit("-", 1)[-1] if "-" in rid else (r.get("mode") or "decide")
            runs[rid] = {"id": rid, "time": r["time"], "mode": mode, "regime": None, "cap": None, "items": {}, "actual": []}
        x = runs[rid]
        x["time"] = min(x["time"], r["time"])
        return x

    for r in rows:
        if r.get("time", "") < start or r["kind"] not in ("regime", "decision", "fill"):
            continue
        x = run_of(r)
        if r["kind"] == "regime":
            x["regime"], x["cap"] = r.get("regime"), r.get("cap")
        elif r["kind"] == "decision":
            side = "매도" if r.get("action") in ("보유", "매도 예정") else "매수"
            adj = r.get("confidence_adj")
            x["items"][r["code"]] = {"code": r["code"], "name": r.get("name", r["code"]), "side": side,
                                     "conf": adj if adj is not None else r.get("confidence"),
                                     "adj_known": adj is not None, "src": "decision", "time": r["time"]}
        else:
            x["actual"].append(r)
            if r.get("rule_exit") or x["mode"] == "exit":
                continue   # 규칙 매도는 시뮬레이션이 스스로 만듦
            x["items"][r["code"]] = {"code": r["code"], "name": r.get("name", r["code"]), "side": r["side"],
                                     "conf": r.get("confidence"), "adj_known": True, "src": "fill",
                                     "price": r.get("price"), "time": r["time"]}
    out = sorted(runs.values(), key=lambda x: x["time"])
    last = None
    for x in out:   # 국면 기록이 없는 회차(손절 · 익절 실행 등)는 그날 직전 국면
        if x["regime"]:
            last = (x["time"][:10], x["regime"], x["cap"])
        elif last and last[0] == x["time"][:10]:
            x["regime"], x["cap"] = last[1], last[2]
    return out


def load_data(journal=None, ref="HEAD", fetch=True):
    p = C.params()
    start = p["competition"]["start"]
    rows = C.read_jsonl(journal or C.JOURNAL)
    runs = load_runs(rows, start)
    days = sorted({x["time"][:10] for x in runs if x["regime"]})
    codes = {c for x in runs for c in x["items"]} | {f["code"] for x in runs for f in x["actual"]} | {BENCH}
    first = (datetime.fromisoformat(start) - timedelta(days=10)).date().isoformat()
    return {"runs": runs, "days": days, "snap": Snapshots(ref),
            "ohlc": daily(codes, first, days[-1], fetch),
            "markets": C.read_json(os.path.join(C.DOCS_DATA, "exchange.json"), {}) or {},
            "equity": C.read_json(os.path.join(C.DOCS_DATA, "equity.json"), []) or [],
            "kodex200": C.read_json(os.path.join(C.DOCS_DATA, "kodex200.json"), {}) or {},
            "journal": rows}


# ---------------- 시뮬레이션 ----------------
class Sim:
    def __init__(self, data, p, r):
        self.d, self.p, self.r = data, p, r
        self.cash = float(r["initial_capital"])
        self.pos = {}        # code -> {qty, avg, name, cf}   cf = 실전엔 없던 보유 (매도 확신도 없음)
        self.meta = {}       # positions.json 대신
        self.journal = []    # 체결 · 국면 기록 (sold_today · buys_today · fill_count · latest_regime 이 읽음)
        self.now = None
        self.briefing = {}
        self.adj = {}
        self.day_start = {}
        self.last = {}       # code -> 마지막으로 본 가격
        self.equity, self.closed, self.blocks = [], [], {}

    # 가격
    def price(self, code, t, item=None):
        if item and item.get("price"):
            v = float(item["price"])
        else:
            v = self.d["snap"].price(code, t)
            if v is None:
                bar = self.d["ohlc"].get(code, {}).get(t[:10])
                v = (bar[1] + bar[2] + bar[3]) / 3 if bar else None
        if v:
            self.last[code] = v
        return v or self.last.get(code)

    def change(self, code, day, price):
        """오늘 등락률 = 지금 가격 / 전일 종가 − 1 (일봉)."""
        bars = self.d["ohlc"].get(code, {})
        prev = [k for k in bars if k < day]
        return price / bars[max(prev)][3] - 1 if prev and price else None

    def account(self):
        hs = []
        for c, x in self.pos.items():
            px = self.last.get(c, x["avg"])
            hs.append({"code": c, "name": x["name"], "qty": x["qty"], "avg_price": x["avg"], "price": px, "value": x["qty"] * px})
        return {"total": self.cash + sum(h["value"] for h in hs), "cash": self.cash, "holdings": hs}

    # 체결
    def _fill(self, run, code, side, qty, price, conf, rule=False, why=None):
        self.journal.append({"kind": "fill", "time": C.stamp(self.now), "run": run, "code": code, "side": side,
                             "qty": qty, "price": price, "amount": qty * price, "confidence": conf,
                             "rule_exit": rule, "why": why, "name": self.pos.get(code, {}).get("name") or code})

    def buy(self, run, code, name, qty, price, conf, cf):
        fee = self.p["costs"]["fee_rate"]
        self.cash -= qty * price * (1 + fee)
        x = self.pos.get(code)
        if x:
            x["avg"] = (x["avg"] * x["qty"] + price * qty) / (x["qty"] + qty)
            x["qty"] += qty
        else:
            self.pos[code] = {"qty": qty, "avg": price, "name": name, "cf": cf, "entry": C.today().isoformat()}
        m = self.meta.get(code) or {"name": name, "entry_date": C.today().isoformat()}
        sl, tp, md = C.exit_levels(code, self.p)
        m.update(stop=round(price * (1 + sl)), take=round(price * (1 + tp)), max_days=md)
        self.meta[code] = m
        self.last[code] = price
        self._fill(run, code, "매수", qty, price, conf)

    def sell(self, run, code, qty, price, conf, rule=False, why=None):
        x = self.pos[code]
        etf = C.market_etf(code, self.p)[0]
        tax = (etf or {}).get("sell_tax_rate", self.p["costs"]["sell_tax_rate"]) if etf else self.p["costs"]["sell_tax_rate"]
        fee = self.p["costs"]["fee_rate"]
        self.cash += qty * price * (1 - fee - tax)
        self.closed.append({"code": code, "name": x["name"], "entry": x["entry"], "exit": C.stamp(self.now), "qty": qty,
                            "ret": round(price * (1 - fee - tax) / (x["avg"] * (1 + fee)) - 1, 4),
                            "rule": rule, "why": why, "counterfactual": x["cf"]})
        self.last[code] = price
        self._fill(run, code, "매도", qty, price, conf, rule, why)
        x["qty"] -= qty
        if x["qty"] <= 0:
            del self.pos[code]
            self.meta.pop(code, None)

    def block(self, rule):
        self.blocks[rule] = self.blocks.get(rule, 0) + 1


@contextmanager
def patched(sim):
    """common 의 '지금 · 기록 · 파일' 읽기를 시뮬레이션 값으로. sizing · risk · monitor 는 손대지 않음."""
    names = ("now", "today", "read_json", "read_jsonl", "params", "risk_limits", "conf_boost")
    orig = {n: getattr(C, n) for n in names}
    env = {k: os.environ.get(k) for k in ("RUN_ID", "RUN_MODE")}

    def read_json(path, default=None):
        if path == C.BRIEFING:
            return sim.briefing
        if path == C.POSITIONS:
            return sim.meta
        if path == C.ACCOUNT:
            return sim.account()
        if path == C.MARKETS:
            return sim.d["markets"]
        if os.path.basename(path) == "day_start.json":
            return sim.day_start
        return orig["read_json"](path, default)

    def conf_boost(code, side, conf, p=None, briefing=None, regime=None):
        v = sim.adj.get((code, side))   # 기록된 보정 후 확신도가 있으면 그 값 (그때 실제로 쓰인 값)
        if v is not None:
            return v, None
        return orig["conf_boost"](code, side, conf, p, briefing, regime)

    C.now = lambda: sim.now
    C.today = lambda: sim.now.date()
    C.read_json = read_json
    C.read_jsonl = lambda path: sim.journal if path == C.JOURNAL else orig["read_jsonl"](path)
    C.params = lambda: sim.p
    C.risk_limits = lambda: sim.r
    C.conf_boost = conf_boost
    try:
        yield
    finally:
        for n, f in orig.items():
            setattr(C, n, f)
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _dt(t):
    return datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=C.KST)


def _rule_exits(sim, run_id):
    hs = [h for h in sim.account()["holdings"]]
    for code, (_, why) in monitor.triggers(hs).items():
        if code in sim.pos:
            sim.sell(run_id, code, sim.pos[code]["qty"], sim.last[code], 1.0, rule=True, why=why)


def _step(sim, run):
    t = run["time"]
    sim.now = _dt(t)
    if sim.hist:
        sim.p = sim.d["snap"].params_at(t)
    sim.briefing = sim.d["snap"].briefing(t)
    os.environ["RUN_ID"], os.environ["RUN_MODE"] = run["id"], run["mode"]
    if run["regime"]:
        sim.journal.append({"kind": "regime", "time": t, "run": run["id"], "regime": run["regime"], "cap": run["cap"]})
    items = run["items"]
    sim.adj = {(c, it["side"]): it["conf"] for c, it in items.items() if it["adj_known"] and it["conf"] is not None}
    px = {}
    for c in set(items) | set(sim.pos):
        it = items.get(c)
        px[c] = sim.price(c, it["time"] if it else t, it)

    if sim.now.hour * 100 + sim.now.minute >= monitor.TRADE_FROM:
        _rule_exits(sim, run["id"])

    if run["mode"] != "decide":   # 매매 회차인지는 실제 실행 종류를 따름 (그때 일정표 기준. 판단 갱신 view · 손절 exit 은 매매 없음)
        return
    p, r = sim.p, sim.r
    for c, it in items.items():   # 1) 판단 매도
        if it["side"] != "매도" or c not in sim.pos or not px.get(c) or it["conf"] is None:
            continue
        conf, _ = C.conf_boost(c, "매도", it["conf"], p)
        if conf < p["decision"]["sell_min_confidence"] - 1e-9:
            continue
        acc = sim.account()
        plan = sizing.plan_sell(c, conf, px[c], acc, p)
        if not plan["qty"]:
            sim.block(plan["limited_by"].split(" (")[0])
            continue
        bad = risk.check({"code": c, "side": "매도", "qty": plan["qty"], "confidence": it["conf"]}, acc, px[c], p, r)
        if bad:
            sim.block(bad[0][0])
            continue
        sim.sell(run["id"], c, plan["qty"], px[c], conf)
    buys = sorted((it for it in items.values() if it["side"] == "매수" and it["conf"] is not None and px.get(it["code"])),
                  key=lambda it: -it["conf"])
    for it in buys:   # 2) 매수 (확신도 높은 순)
        c = it["code"]
        conf, _ = C.conf_boost(c, "매수", it["conf"], p)
        if conf < C.probe_floor(p) - 1e-9:
            continue
        acc = sim.account()
        chg = sim.change(c, t[:10], px[c])
        plan = sizing.plan_buy(c, conf, px[c], acc, p, r, regime=run["regime"], today_change=chg)
        if not plan["qty"]:
            sim.block(plan["limited_by"])
            continue
        bad = risk.check({"code": c, "side": "매수", "qty": plan["qty"], "confidence": it["conf"], "today_change": chg}, acc, px[c], p, r)
        if bad:
            sim.block(bad[0][0])
            continue
        real_held = it["src"] == "fill" or c in sim.real_held.get(t[:10], set())
        sim.buy(run["id"], c, it["name"], plan["qty"], px[c], conf, cf=not real_held)


def _close_day(sim, day):
    """장 마감: 회차 사이에 놓친 손절 · 익절을 일봉 저가 · 고가로 (오늘 산 종목 제외), 종가로 평가."""
    sim.now = _dt(f"{day} 15:20")
    meta = C.position_meta(sim.account()["holdings"], sim.p)
    for code in list(sim.pos):
        bar = sim.d["ohlc"].get(code, {}).get(day)
        m = meta.get(code) or {}
        if not bar or m.get("entry_date") == day:
            continue
        o, hi, lo, _ = bar
        if m.get("stop") and lo <= m["stop"]:
            sim.sell("close", code, sim.pos[code]["qty"], min(o, m["stop"]), 1.0, True, f"일봉 저가 {lo:,.0f} ≤ 손절가 {m['stop']:,}")
        elif m.get("take") and hi >= m["take"]:
            sim.sell("close", code, sim.pos[code]["qty"], max(o, m["take"]), 1.0, True, f"일봉 고가 {hi:,.0f} ≥ 익절가 {m['take']:,}")
    for code in sim.pos:
        bar = sim.d["ohlc"].get(code, {}).get(day)
        if bar:
            sim.last[code] = bar[3]
    acc = sim.account()
    sim.equity.append({"date": day, "total": round(acc["total"]), "cash": round(acc["cash"]), "positions": len(acc["holdings"])})


def simulate(data, p=None, r=None, historical=False):
    """p 기준값으로 전 기간 재현. historical=True 면 회차마다 그때의 params.json (git 기록) — 검증용."""
    p = copy.deepcopy(p or C.params())
    r = r or C.risk_limits()
    sim = Sim(data, p, r)
    sim.hist = historical
    sim.real_held = _real_holdings(data)
    with patched(sim):
        by_day = {}
        for x in data["runs"]:
            by_day.setdefault(x["time"][:10], []).append(x)
        prev_total = r["initial_capital"]
        for day in data["days"]:
            sim.day_start = {"date": day, "total": prev_total}
            for run in by_day.get(day, []):
                _step(sim, run)
            _close_day(sim, day)
            prev_total = sim.equity[-1]["total"]
    return {"equity": sim.equity, "fills": [x for x in sim.journal if x["kind"] == "fill"], "closed": sim.closed,
            "blocks": dict(sorted(sim.blocks.items(), key=lambda kv: -kv[1])), "holdings": sim.account()["holdings"]}


def _real_holdings(data):
    """날짜별 실제 보유 종목 (실제 매수 체결 ~ 전량 매도). 실전에 없던 보유인지 표시용."""
    qty, out = {}, {}
    for x in data["runs"]:
        for f in x["actual"]:
            qty[f["code"]] = qty.get(f["code"], 0) + (f["qty"] if f["side"] == "매수" else -f["qty"])
        out.setdefault(x["time"][:10], set()).update(c for c, q in qty.items() if q > 0)
    return out


# ---------------- 지표 ----------------
def bench_curve(data):
    k = data["kodex200"]
    days = k.get("days", {})
    out = {d: v["c"] for d, v in days.items()}
    for d, bar in data["ohlc"].get(BENCH, {}).items():
        out.setdefault(d, bar[3])
    return out, k.get("start") or data["ohlc"].get(BENCH, {}).get(data["days"][0], [None])[0]


def metrics(res, data, start=None, end=None, r=None):
    """구간 [start, end] 지표. 수익률은 구간 앞날 종가(첫 구간이면 초기 자본) 대비."""
    r = r or C.risk_limits()
    eq = res["equity"]
    days = [e["date"] for e in eq]
    start, end = start or days[0], end or days[-1]
    seg = [e for e in eq if start <= e["date"] <= end]
    before = [e for e in eq if e["date"] < start]
    base = before[-1]["total"] if before else r["initial_capital"]
    curve = [base] + [e["total"] for e in seg]
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    bench, b0 = bench_curve(data)
    bdays = [d for d in sorted(bench) if days[0] <= d < start]   # 첫 구간은 시작일 시가 (대시보드와 같음. 11-19 전날 종가로 잡던 것 고침)
    bbase = bench[bdays[-1]] if bdays else b0
    bret = bench.get(end) / bbase - 1 if bench.get(end) and bbase else None
    fills = [f for f in res["fills"] if start <= f["time"][:10] <= end]
    closed = [c for c in res["closed"] if start <= c["exit"][:10] <= end]
    avg_eq = sum(curve) / len(curve)
    ret = curve[-1] / base - 1
    return {"start": start, "end": end, "final": round(curve[-1]), "return": round(ret, 4),
            "bench": round(bret, 4) if bret is not None else None,
            "excess": round(ret - bret, 4) if bret is not None else None,
            "mdd": round(mdd, 4), "trades": len(fills), "buys": sum(f["side"] == "매수" for f in fills),
            "turnover": round(sum(f["amount"] for f in fills) / avg_eq, 2),
            "win_rate": round(sum(c["ret"] > 0 for c in closed) / len(closed), 3) if closed else None,
            "closed": len(closed), "counterfactual_closed": sum(c["counterfactual"] for c in closed)}


def validate(data, res):
    """재현이 실제와 얼마나 맞는지: 날짜별 자산 차이 · 체결 (날짜, 종목, 방향) 일치율."""
    actual = {e["date"]: e["total"] for e in data["equity"]}
    rows = [{"date": e["date"], "sim": e["total"], "actual": actual.get(e["date"]),
             "gap": round(e["total"] / actual[e["date"]] - 1, 4) if actual.get(e["date"]) else None} for e in res["equity"]]
    real = {(f["time"][:10], f["code"], f["side"]) for x in data["runs"] for f in x["actual"]}
    sim = {(f["time"][:10], f["code"], f["side"]) for f in res["fills"]}
    both = real & sim
    gaps = [abs(x["gap"]) for x in rows if x["gap"] is not None]
    return {"days": rows, "final_gap": rows[-1]["gap"] if rows else None,
            "mean_abs_gap": round(sum(gaps) / len(gaps), 4) if gaps else None,
            "fills_real": len(real), "fills_sim": len(sim), "fills_matched": len(both),
            "recall": round(len(both) / len(real), 3) if real else None,
            "precision": round(len(both) / len(sim), 3) if sim else None,
            "missed": sorted(real - sim)[:30], "extra": sorted(sim - real)[:30]}


def coverage(data):
    """판단 중 git 장중 가격으로 맞춘 비율 (나머지는 일봉 근사)."""
    n = hit = 0
    for x in data["runs"]:
        for c, it in x["items"].items():
            n += 1
            hit += bool(it.get("price") or data["snap"].price(c, it["time"]))
    return {"items": n, "intraday_price": hit, "ratio": round(hit / n, 3) if n else None}
