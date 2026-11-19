"""
미국 데이터 준비 (SIGMA_MARKET=US). data_prep.py 가 US 면 이 모듈의 build() 를 부름.
결과 형식(work_us/briefing.json)은 국내와 같아서 판단 · 라우팅 · 사이징 · 대시보드가 그대로 씀.

1. 종목 범위 (universe.json, 주 1회): S&P 500 (Wikipedia 구성 종목 표) + params.screen.ndx_extra (나스닥 100 중 S&P 500 밖, 직접 관리)
   거래소(NASDAQ · NYSE · AMEX)는 Yahoo 시세 응답에서 받아 KIS 주문 코드에 씀
2. 일봉 150일: Yahoo (공개, 키 없음). 지표는 국내와 같은 함수 (data_prep_ind.indicators, 소수 2자리)
3. 후보: 평균 거래대금 · 추세(5 · 20일선) · 점수 (20일 수익률 + 20일선 이격 + SPY 대비 상대강도). 수급(외국인 · 기관)은 미국에 없음
4. 뉴스: Yahoo Finance 종목 RSS 제목 (키 없음). 악재 단어가 있으면 ⚠
"""
import html
import html.parser
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import yahoo  # noqa: E402

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
UA = {"User-Agent": "Mozilla/5.0 (sigma-engine research bot)"}
NEWS_DAYS = 3
RISK_WORDS = ["offering", "dilut", "lawsuit", "sued", "probe", "investigation", "subpoena", "fraud", "downgrade",
              "bankrupt", "delist", "recall", "guidance cut", "cuts guidance", "misses", "sec charges", "restatement"]
EXCH = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NAS": "NASDAQ", "NASDAQ": "NASDAQ",
        "NYQ": "NYSE", "NYS": "NYSE", "NYSE": "NYSE", "ASE": "AMEX", "PCX": "AMEX", "AMX": "AMEX", "BTS": "AMEX"}


# ---------------- 종목 범위 ----------------
class _Tables(html.parser.HTMLParser):
    """Wikipedia wikitable → [[행 셀 텍스트]] (바깥 표만)."""

    def __init__(self):
        super().__init__()
        self.tables, self.depth, self.row, self.cell = [], 0, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
            if self.depth == 1 and "wikitable" in (dict(attrs).get("class") or ""):
                self.tables.append([])
        elif tag == "tr" and self.depth == 1:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.tables:
                self.tables[-1].append(self.row)
            self.row = None
        elif tag == "table":
            self.depth -= 1

    def handle_data(self, d):
        if self.cell is not None:
            self.cell.append(d)


def sp500():
    """[(티커, 회사명, 업종)]. 'Symbol' 열이 있는 첫 표."""
    with urllib.request.urlopen(urllib.request.Request(SP500_URL, headers=UA), timeout=20) as r:
        t = r.read().decode("utf-8")
    p = _Tables()
    p.feed(t)
    for tb in p.tables:
        if tb and tb[0] and tb[0][0].lower() == "symbol":
            hdr = [h.lower() for h in tb[0]]
            si = hdr.index("gics sector") if "gics sector" in hdr else None
            out = [(r[0].strip().upper(), r[1].strip(), r[si] if si is not None and len(r) > si else "") for r in tb[1:]
                   if len(r) > 1 and C.code_market(r[0].strip().upper()) == "US"]   # 표 아래 업종 합계 행 등은 뺌
            if len(out) > 400:
                return out
    raise RuntimeError("S&P 500 구성 종목 표를 찾지 못함")


def refresh_universe(p, force=False):
    """universe.json 갱신 (7일 지났거나 force). 실패하면 기존 목록 유지. {티커: {name, exchange, sector, index}}"""
    path = os.path.join(C.CONFIG, p["screen"].get("universe_file", "universe.json"))
    old = C.read_json(path, {}) or {}
    if not force and old.get("updated", "") >= (C.today() - timedelta(days=7)).isoformat() and old.get("tickers"):
        return old["tickers"]
    try:
        rows = [(t, n, s, "SP500") for t, n, s in sp500()]
    except Exception as e:  # noqa: BLE001
        print(f"  ! S&P 500 목록 갱신 실패, 기존 목록 유지: {e}")
        return old.get("tickers") or {}
    extra = {k: v for k, v in (p["screen"].get("ndx_extra") or {}).items() if not k.startswith("_")}
    rows += [(t, n, "", "NDX") for t, n in extra.items() if t not in {r[0] for r in rows}]
    prev = old.get("tickers") or {}
    tickers = {t: {"name": n, "sector": s, "index": ix, "exchange": (prev.get(t) or {}).get("exchange")} for t, n, s, ix in rows}
    C.write_json(path, {"_note": "미국 주문 가능 종목 (us_data.refresh_universe 가 주 1회 갱신). 거래소는 Yahoo 응답에서 채움",
                        "updated": C.today().isoformat(), "count": len(tickers), "tickers": tickers})
    print(f"종목 범위 갱신: {len(tickers)}종목 (S&P 500 + 나스닥 100 추가 {len(extra)})")
    return tickers


def save_exchanges(found):
    """일봉을 받으며 알게 된 거래소를 universe.json 에 채움 (KIS 주문 코드)."""
    if not found:
        return
    path = os.path.join(C.CONFIG, C.params()["screen"].get("universe_file", "universe.json"))
    u = C.read_json(path, {}) or {}
    ch = False
    for t, ex in found.items():
        if t in (u.get("tickers") or {}) and ex and u["tickers"][t].get("exchange") != ex:
            u["tickers"][t]["exchange"], ch = ex, True
    if ch:
        C.write_json(path, u)


# ---------------- 시세 · 지표 ----------------
def completed(rows):
    """장 마감 전이면 오늘의 미완성 일봉을 뺌 (뉴욕 시간)."""
    n = C.now()
    close = C.session_close(n.date()) or "16:00"
    if rows and rows[-1]["date"] == n.date().isoformat() and n.strftime("%H:%M") < close:
        return rows[:-1]
    return rows


def ohlcv(code, days=150):
    """(일봉 목록, 거래소). Yahoo 1년치에서 마지막 days 개."""
    r = yahoo.chart(code, "1y", "1d")
    ex = EXCH.get(((r.get("meta") or {}).get("exchangeName") or "").upper())
    rows = completed(yahoo.daily_from(r))
    return rows[-days:], ex


def analyze(code, name, p):
    import ind as I
    import pandas as pd
    rows, ex = ohlcv(code)
    if len(rows) < 60:
        return None, ex
    close = pd.Series([x["close"] for x in rows], index=pd.to_datetime([x["date"] for x in rows]))
    ind = I.indicators(close, digits=2)
    if not ind:
        return None, ex
    tv = [x["close"] * (x["volume"] or 0) for x in rows]
    s = p["screen"]
    lb = s.get("limit_up_lookback", 5)
    jumps = [rows[i]["close"] / rows[i - 1]["close"] - 1 for i in range(len(rows) - lb, len(rows)) if rows[i - 1]["close"]]
    return {"code": code, "name": name, **ind, "exchange": ex,
            "trading_value_20d": int(sum(tv[-20:]) / 20),
            "limit_up_recent": any(j >= s.get("spike_day", 0.25) for j in jumps),   # 미국은 상한가가 없어 하루 +25% 이상 급등을 같은 뜻으로
            "closes": [round(x["close"], 2) for x in rows[-120:]]}, ex


def market_summary():
    """시장 = S&P 500 지수(^GSPC). 지수 일봉으로 국내와 같은 규칙 힌트."""
    import ind as I
    import pandas as pd
    rows = completed(yahoo.daily("^GSPC"))
    close = pd.Series([x["close"] for x in rows], index=pd.to_datetime([x["date"] for x in rows]))
    ind = I.indicators(close, digits=2) or {}
    others = []
    for sym, nm in (("^IXIC", "나스닥"), ("^SOX", "필라델피아 반도체"), ("^VIX", "VIX")):
        try:
            d = completed(yahoo.daily(sym, "1mo"))
            others.append({"symbol": sym, "name": nm, "date": d[-1]["date"], "close": round(d[-1]["close"], 2),
                           "chg": round(d[-1]["close"] / d[-2]["close"] - 1, 4)})
        except Exception as e:  # noqa: BLE001
            print(f"  ! {nm} 조회 실패: {e}")
    return {"source": "S&P 500", "asof": rows[-1]["date"], **ind,
            "rule_hint": I.rule_hint(ind) if ind else "횡보", "us": others,
            "closes": [round(x["close"], 2) for x in rows[-65:]]}


def live_market(b):
    """장중이면 마감된 일봉 + 지금 S&P 500 (잔고 루프가 받는 값)으로 힌트를 다시 계산. 없으면 None."""
    m, snap = b.get("market", {}), C.read_json(os.path.join(C.LIVE, "last_snapshot_us.json")) or {}
    closes, px = m.get("closes") or [], snap.get("index")
    if len(closes) < 65 or not px or time.time() - snap.get("epoch", 0) > 15 * 60:
        return None
    import ind as I
    s = closes + [float(px)]
    ma = lambda n, off=0: sum(s[len(s) - off - n:len(s) - off]) / n   # noqa: E731
    v = {"close": round(float(px), 2), "ma5": round(ma(5), 2), "ma20": round(ma(20), 2), "ma60": round(ma(60), 2),
         "ma20_slope5": round(ma(20) / ma(20, 5) - 1, 4)}
    return {**v, "rule_hint": I.rule_hint(v), "time": C.now().strftime("%H:%M")}


# ---------------- 후보 ----------------
def screen(rows, p, spy_ret20, regime=None):
    import pandas as pd
    s = p["screen"]
    ok = []
    for r in rows:
        if r["limit_up_recent"] or r["trading_value_20d"] < s["min_trading_value"]:
            continue
        hv = p.get("high_vol") or {}
        r["high_vol"] = r["vol20"] > s.get("max_vol20", 9)
        if r["high_vol"] and not (hv.get("enabled") and r["vol20"] <= hv.get("max_vol20", 0)):
            continue
        if not C.trend_ok(r["close"], r, regime, p):
            continue
        r["rel_strength"] = round(r["ret20"] - (spy_ret20 or 0), 4)   # SPY 대비 20일 초과 수익 (국내 수급 점수 자리)
        ok.append(r)
    if not ok:
        return []
    df = pd.DataFrame(ok)
    df["trend_gap"] = df["close"] / df["ma20"] - 1
    score = df["ret20"].rank(pct=True) + df["trend_gap"].rank(pct=True) + df["rel_strength"].rank(pct=True)
    score -= (df["ret5"] > s.get("spike_ret5", 9)).astype(float) * s.get("spike_penalty", 0)
    score = score.clip(lower=0)
    df["score"] = (score / score.max()).round(3) if score.max() > 0 else 0.0
    df = df.drop(columns=["trend_gap"]).sort_values("score", ascending=False)
    hv_n = (p.get("high_vol") or {}).get("max_candidates", 0)
    return df[~df["high_vol"]].head(s["max_candidates"]).to_dict("records") + df[df["high_vol"]].head(hv_n).to_dict("records")


def etf_rows(p, held):
    out = []
    for kind in ("inverse", "index"):
        cfg = p.get(kind) or {}
        if not cfg.get("enabled") or cfg.get("buy_banned"):
            continue
        for code, nm in cfg.get("codes", {}).items():
            if code in held:
                continue
            try:
                r, _ = analyze(code, nm, p)
            except Exception as e:  # noqa: BLE001
                print(f"  ! {nm}({code}) 건너뜀: {e}")
                continue
            if r:
                r[kind], r["score"] = True, None
                out.append(r)
    return out


def news(rows):
    """Yahoo Finance 종목 RSS 최근 제목 (최대 3개, NEWS_DAYS 일). 키 없음. 실패한 종목은 빈 결과."""
    since = C.today() - timedelta(days=NEWS_DAYS)
    out = {}
    for r in rows:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={yahoo.ysym(r['code'])}&region=US&lang=en-US"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as res:
                root = ET.fromstring(res.read())
        except Exception:  # noqa: BLE001
            continue
        finally:
            time.sleep(0.2)
        hits, seen = [], set()
        for it in root.iter("item"):
            title = html.unescape((it.findtext("title") or "").strip())
            try:
                d = parsedate_to_datetime(it.findtext("pubDate") or "")
            except (TypeError, ValueError):
                continue
            if d.date() < since or not title or title in seen:
                continue
            seen.add(title)
            low = title.lower()
            hits.append({"date": d.strftime("%m-%d"), "title": title, "risk": any(w in low for w in RISK_WORDS)})
        hits.sort(key=lambda h: not h["risk"])
        if hits:
            out[r["code"]] = hits[:3]
    return out


def build():
    p = C.params()
    market = market_summary()
    held = {h["code"]: h["name"] for h in (C.read_json(C.ACCOUNT, {}) or {}).get("holdings", [])}
    uni = refresh_universe(p)
    source = "universe" if uni else "fallback"
    if not uni:
        uni = {c: {"name": c} for c in p["screen"]["fallback_universe"]}
    rows, found, done = [], {}, set()
    for code, info in list(uni.items()) + [(c, {"name": n}) for c, n in held.items()]:
        if code in done:
            continue
        done.add(code)
        try:
            r, ex = analyze(code, info.get("name") or code, p)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {code} 건너뜀: {e}")
            continue
        found[code] = ex
        if r:
            rows.append(r)
    save_exchanges(found)
    C.write_json(C.MARKETS, {c: ex for c, ex in found.items() if ex})
    spy = next((r for r in rows if r["code"] == "SPY"), None)
    spy_ret20 = spy["ret20"] if spy else market.get("ret20")
    cands = screen([r for r in rows if r["code"] not in held and not C.market_etf(r["code"], p)[0]], p, spy_ret20, market.get("rule_hint"))
    cands += etf_rows(p, held)
    holdings = [r for r in rows if r["code"] in held]
    found_news = news(cands + holdings)
    for r in cands + holdings:
        r["news"] = found_news.get(r["code"], [])
    out = {"created": C.stamp(), "asof": market["asof"], "source": source, "market": market,
           "candidates": cands, "holdings": holdings, "universe_size": len(rows), "currency": "USD"}
    C.write_json(C.BRIEFING, out)
    C.write_json(os.path.join(C.WORK, "prep_rows.json"), [{k: v for k, v in r.items() if k != "news"} for r in rows])
    return out


# ---------------- LLM 에 넘길 요약 ----------------
def pct(x):
    return "-" if x is None else f"{x * 100:+.1f}%"


def usd(x):
    return "-" if x is None else f"${x / 1e6:,.0f}M"


def to_text(b):
    m = b["market"]
    lines = [
        f"# 브리핑 — 미국 주식 ({b['asof']} 종가 기준, 뉴욕 시간 · 금액은 달러 · 실제 돈 계좌)",
        "",
        f"## 시장 ({m['source']})",
        f"종가 {m.get('close', 0):,} · 5일선 {m.get('ma5', 0):,} · 20일선 {m.get('ma20', 0):,} · 60일선 {m.get('ma60', 0):,} · "
        f"20일선 5일 기울기 {pct(m.get('ma20_slope5'))} · 20일 수익률 {pct(m.get('ret20'))} · "
        f"변동성 {m.get('vol20', 0) * 100:.0f}% · 60일 최대낙폭 {pct(m.get('mdd60'))} · 규칙 기반 힌트: {m['rule_hint']}",
    ]
    if m.get("us"):
        lines.append("다른 지수 (" + m["us"][0]["date"] + "): " + " · ".join(f"{u['name']} {u['close']:,} ({pct(u['chg'])})" for u in m["us"]))
    lv = m.get("live")
    if lv:
        lines.append(f"장중 실시간 ({lv['time']}, 지금 S&P 500 을 오늘 종가로 가정): {lv['close']:,} · 5일선 {lv['ma5']:,} · 20일선 {lv['ma20']:,} · 실시간 힌트: {lv['rule_hint']}")
    lines += ["", "미국은 투자자별 수급(외국인 · 기관) 자료가 없음 → 수급 대신 'SPY 대비' = 20일 수익률 − SPY 20일 수익률 (상대강도)", ""]
    cols = "| 티커 | 종목 | 종가 | 5일선 | 20일선 | 60일선 | 5일 | 20일 | RSI | 변동성 | 60일 낙폭 | SPY 대비 | 평균 거래대금 | 점수 |"
    sep = "|" + "---|" * 14

    def row(r):
        kind = C.market_etf(r["code"])[1]
        tag = " 🔻인버스" if kind == "인버스" else " 📈지수" if kind == "지수" else " ⚡고변동" if r.get("high_vol") else ""
        return (f"| {r['code']} | {r['name']}{tag} | {r['close']:,} | {r.get('ma5') or '-'} | {r['ma20']:,} | {r['ma60']:,} | {pct(r['ret5'])} | "
                f"{pct(r['ret20'])} | {r['rsi14']} | {r['vol20'] * 100:.0f}% | {pct(r['mdd60'])} | {pct(r.get('rel_strength'))} | "
                f"{usd(r.get('trading_value_20d'))} | {r.get('score', '-')} |")

    it = b.get("intraday") or {}
    if it:
        lines += [f"## 장중 ({it['time']} 뉴욕) — 후보 · 보유 현재가와 오늘 등락은 아래 '오늘 장중' 표", ""]
    for title, rs in (("후보 종목", b["candidates"]), ("보유 종목", b["holdings"])):
        lines.append(f"## {title} ({len(rs)})")
        lines += [cols, sep] + [row(r) for r in rs] if rs else ["없음"]
        notes = [f"- {r['name']} {n['date']}{' ⚠' if n['risk'] else ''} {n['title']}" for r in rs for n in r.get("news", [])]
        if notes:
            lines += ["", f"최근 뉴스 ({NEWS_DAYS}일, 영어 원문, ⚠ = 악재 단어 포함):"] + notes
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    if "--universe" in sys.argv:
        refresh_universe(C.params(), force=True)
        sys.exit(0)
    if "--show" not in sys.argv:
        t = time.time()
        b = build()
        print(f"완료 · {b['source']} · {b['universe_size']}종목 분석 · 후보 {len(b['candidates'])}개 · {time.time() - t:.0f}초\n")
    print(to_text(C.read_json(C.BRIEFING)))
