"""Yahoo Finance chart API (공개, 키 없음) — 미국 일봉 · 현재가 · 환율.

데이터 준비(일봉 150일)와 가상 계좌 시세에 씀. 실제 주문 · 잔고는 KIS (src/kis.py).
국내의 data_prep.us_overnight 와 같은 주소를 씀.
"""
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={iv}"
_last = [0.0]


def ysym(code):
    """KIS · 지수 구성 표기(BRK.B) → Yahoo 표기(BRK-B)."""
    return str(code).replace(".", "-")


def chart(code, rng="1y", interval="1d", timeout=10):
    wait = 0.25 - (time.time() - _last[0])   # 연속 호출 간격 (Yahoo 가 막지 않도록)
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    url = URL.format(sym=urllib.parse.quote(ysym(code)), rng=rng, iv=interval)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        body = json.load(res)
    r = ((body or {}).get("chart") or {}).get("result") or []
    if not r:
        raise RuntimeError(f"{code}: Yahoo 응답 없음 ({((body or {}).get('chart') or {}).get('error')})")
    return r[0]


def daily(code, rng="1y"):
    """일봉 [{date, open, high, low, close, volume}] (값 없는 날 제외, 뉴욕 거래일)."""
    r = chart(code, rng, "1d")
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    off = (r.get("meta") or {}).get("gmtoffset", 0)
    out = []
    for i, t in enumerate(ts):
        c = (q.get("close") or [None])[i] if i < len(q.get("close") or []) else None
        if c is None:
            continue
        day = datetime.fromtimestamp(t + off, timezone.utc).strftime("%Y-%m-%d")
        g = lambda k: (q.get(k) or [None] * len(ts))[i]   # noqa: E731
        out.append({"date": day, "open": g("open"), "high": g("high"), "low": g("low"), "close": float(c), "volume": g("volume") or 0})
    return out


def quote(code):
    """(현재가, 오늘 등락률). 장중이면 지금 가격, 장 밖이면 마지막 종가."""
    m = chart(code, "1d", "1m").get("meta") or {}
    px = m.get("regularMarketPrice")
    prev = m.get("chartPreviousClose") or m.get("previousClose")
    if not px:
        raise RuntimeError(f"{code}: 현재가 없음")
    return round(float(px), 2), (round(float(px) / float(prev) - 1, 4) if prev else None)


def fx_usdkrw():
    """원/달러 환율 (KRW=X)."""
    return quote("KRW=X")[0]
