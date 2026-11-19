"""
미국 주식 계좌 (SIGMA_MARKET=US). broker.get() 이 MODE 에 따라 둘 중 하나를 줌.

- mock : work_us/mock_account.json 가상 계좌. 가격은 Yahoo, 주문은 즉시 체결로 가정
- live : 한국투자증권(KIS) Open API 해외주식. 실제 돈. 키는 agent/.env.us

agent/.env.us (커밋 금지, 예시는 agent/.env.us.example)
  KIS_APPKEY / KIS_APPSECRET    KIS Developers 에서 발급
  KIS_ACCOUNT                   계좌번호 앞 8자리 (CANO)
  KIS_ACCOUNT_PROD              계좌상품코드 뒤 2자리 (보통 01)
  KIS_BASE                      실전 https://openapi.koreainvestment.com:9443
                                (모의 https://openapivts.koreainvestment.com:29443, 거래 ID 가 V 로 시작)
  KIS_MIN_INTERVAL              호출 간격 초 (기본 실전 0.1, 모의 0.6)

주의: 주문 · 잔고 · 예수금 응답 필드는 KIS 문서 기준으로 짰음. 실제 계좌 첫 응답으로 확인 필요
→ 주문 응답은 work_us/orders.jsonl, 잔고 · 예수금 원문은 work_us/kis_raw/ 에 남김.

미국 주문은 전부 **지정가** (현재가 ± params.us.limit_slippage). 시장가는 쓰지 않음.
체결 안 된 주문은 params.us.unfilled_cancel_min 분 뒤 cancel_stale() 가 취소 (감시 루프 · 판단 전).
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import common as C
import broker as B
import yahoo

TOKEN = os.path.join(C.WORK, "kis_token.json")
LAST = os.path.join(C.WORK, "kis_last_call.json")       # 프로세스 사이 호출 간격 (잔고 루프 · 판단 · 감시가 같이 부름)
OPEN_ORDERS = os.path.join(C.WORK, "open_orders.json")  # 낸 주문 중 아직 체결 확인 안 된 것
RAW = os.path.join(C.WORK, "kis_raw")

EXCH_ORDER = {"NASDAQ": "NASD", "NYSE": "NYSE", "AMEX": "AMEX"}   # 주문 · 잔고용 거래소 코드
EXCH_QUOTE = {"NASDAQ": "NAS", "NYSE": "NYS", "AMEX": "AMS"}      # 시세용 거래소 코드


def exchange(code, p=None):
    """티커 → 'NASDAQ' | 'NYSE' | 'AMEX'. params.us.exchanges(ETF) → universe → 모르면 NASDAQ."""
    p = p or C.params()
    ex = {k: v for k, v in ((p.get("us") or {}).get("exchanges") or {}).items() if not k.startswith("_")}.get(code)
    return ex or (C.universe(p).get(code) or {}).get("exchange") or "NASDAQ"


class KisError(RuntimeError):
    pass


class KisUsBroker:
    name = "live"
    MARKETS = {"US"}

    def __init__(self):
        if not C.US:
            raise RuntimeError("KIS 미국 계좌는 SIGMA_MARKET=US 에서만")
        e = os.environ
        miss = [k for k in ("KIS_APPKEY", "KIS_APPSECRET", "KIS_ACCOUNT", "KIS_BASE") if not e.get(k)]
        if miss:
            raise RuntimeError(f"agent/.env.us 에 {', '.join(miss)} 없음")
        self.base = e["KIS_BASE"].rstrip("/")
        self.paper = "openapivts" in self.base
        self.cano, self.prod = e["KIS_ACCOUNT"], e.get("KIS_ACCOUNT_PROD", "01")
        self.gap = float(e.get("KIS_MIN_INTERVAL") or (0.6 if self.paper else 0.1))

    # ---------------- HTTP ----------------
    def _tr(self, real):
        """모의투자는 거래 ID 첫 글자가 V (실전 T → 모의 V)."""
        return "V" + real[1:] if self.paper and real[0] == "T" else real

    def _wait(self):
        last = (C.read_json(LAST, {}) or {}).get("t", 0)
        w = self.gap - (time.time() - last)
        if w > 0:
            time.sleep(w)
        C.write_json(LAST, {"t": time.time()})

    def _token(self, force=False):
        t = C.read_json(TOKEN, {}) or {}
        if not force and t.get("token") and t.get("exp", 0) > time.time() + 600 and t.get("key_tail") == os.environ["KIS_APPKEY"][-4:]:
            return t["token"]
        body = {"grant_type": "client_credentials", "appkey": os.environ["KIS_APPKEY"], "appsecret": os.environ["KIS_APPSECRET"]}
        res = self._http("POST", "/oauth2/tokenP", body=body, auth=False)
        tok = res.get("access_token")
        if not tok:
            raise KisError(f"토큰 발급 실패: {res.get('error_description') or res}")
        C.write_json(TOKEN, {"token": tok, "exp": time.time() + int(res.get("expires_in") or 86400), "key_tail": os.environ["KIS_APPKEY"][-4:]})
        return tok

    def _http(self, method, path, params=None, body=None, tr_id=None, auth=True):
        self._wait()
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        hdr = {"content-type": "application/json; charset=utf-8"}
        if auth:
            hdr.update({"authorization": "Bearer " + self._token(), "appkey": os.environ["KIS_APPKEY"],
                        "appsecret": os.environ["KIS_APPSECRET"], "tr_id": tr_id, "custtype": "P"})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            try:
                return json.loads(txt)
            except ValueError:
                raise KisError(f"HTTP {e.code}: {txt[:200]}") from e

    def _call(self, method, path, tr_real, params=None, body=None):
        tr = self._tr(tr_real)
        res = self._http(method, path, params, body, tr)
        if res.get("msg_cd") == "EGW00201":   # 초당 호출 수 초과 → 1초 쉬고 한 번만 다시
            time.sleep(1)
            res = self._http(method, path, params, body, tr)
        if res.get("msg_cd") in ("EGW00123", "EGW00121"):   # 토큰 만료 · 무효 → 새로 받아 한 번만 다시
            self._token(force=True)
            res = self._http(method, path, params, body, tr)
        if str(res.get("rt_cd", "0")) != "0":
            raise KisError(f"{tr} 실패: {res.get('msg_cd')} {res.get('msg1')}")
        return res

    def _raw(self, name, res):
        C.write_json(os.path.join(RAW, name + ".json"), {"time": C.stamp(), "res": res})

    # ---------------- 시세 ----------------
    def quote(self, code):
        p = B.cached_quote(code, 30)
        if p:
            return p
        res = self._call("GET", "/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                         {"AUTH": "", "EXCD": EXCH_QUOTE[exchange(code)], "SYMB": code})
        o = res.get("output") or {}
        px = float(o.get("last") or 0)
        if px <= 0:   # 장 밖 · 시세 없음 → Yahoo 마지막 값
            px, chg = yahoo.quote(code)
        else:
            chg = round(float(o.get("rate") or 0) / 100, 4)
        B.cache_quote(code, round(px, 2), chg)
        return round(px, 2)

    def quote_info(self, code):
        q = (C.read_json(B.QUOTES, {}) or {}).get(code) or {}
        if q.get("change") is None or time.time() - q.get("ts", 0) > 30:
            C.write_json(B.QUOTES, {**(C.read_json(B.QUOTES, {}) or {}), code: {**q, "ts": 0}})
        p = self.quote(code)
        return {"price": p, "change": B.cached_change(code)}

    # ---------------- 잔고 ----------------
    def _holdings(self):
        """해외주식 잔고 (TTTS3012R). 실전은 NASD 한 번이 미국 전체, 모의는 거래소별로 따로 조회."""
        out, seen = [], set()
        for ex in (["NASD"] if not self.paper else ["NASD", "NYSE", "AMEX"]):
            fk = nk = ""
            for _ in range(10):   # 연속 조회
                res = self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-balance", "TTTS3012R",
                                 {"CANO": self.cano, "ACNT_PRDT_CD": self.prod, "OVRS_EXCG_CD": ex, "TR_CRCY_CD": "USD",
                                  "CTX_AREA_FK200": fk, "CTX_AREA_NK200": nk})
                self._raw(f"balance_{ex}", res)
                for h in res.get("output1") or []:
                    code, qty = h.get("ovrs_pdno"), int(float(h.get("ovrs_cblc_qty") or 0))
                    if not code or qty <= 0 or code in seen:
                        continue
                    seen.add(code)
                    px = float(h.get("now_pric2") or 0)
                    avg = float(h.get("pchs_avg_pric") or 0)
                    out.append({"code": code, "name": h.get("ovrs_item_name") or code, "qty": qty,
                                "avg_price": C.px_round(avg), "price": C.px_round(px), "value": C.px_round(px * qty),
                                "pnl_pct": round(px / avg - 1, 4) if avg else 0.0, "exchange": h.get("ovrs_excg_cd")})
                fk, nk = (res.get("ctx_area_fk200") or "").strip(), (res.get("ctx_area_nk200") or "").strip()
                if res.get("tr_cont") not in ("F", "M") or not nk:
                    break
        return out

    def _cash(self):
        """(주문에 쓸 수 있는 달러, 환율). 체결기준현재잔고 (CTRP6504R) 의 USD 행."""
        res = self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-present-balance", "CTRP6504R",
                         {"CANO": self.cano, "ACNT_PRDT_CD": self.prod, "WCRC_FRCR_DVSN_CD": "02",
                          "NATN_CD": "840", "TR_MKET_CD": "00", "INQR_DVSN_CD": "00"})
        self._raw("present_balance", res)
        usd = next((x for x in res.get("output2") or [] if x.get("crcy_cd") == "USD"), {})
        cash = float(usd.get("frcr_drwg_psbl_amt_1") or usd.get("frcr_dncl_amt_2") or 0)
        fx = float(usd.get("frst_bltn_exrt") or 0) or None
        return cash, fx

    def account(self):
        hs = self._holdings()
        cash, fx = self._cash()
        for h in hs:
            B.cache_quote(h["code"], h["price"])
        acc = B.build_account(cash, hs)
        acc["fx"] = fx or fx_cached()
        return B.save_account(acc)

    # ---------------- 주문 ----------------
    def order(self, code, name, side, qty):
        B.check_market(self, code)
        p = C.params()
        slip = (p.get("us") or {}).get("limit_slippage", 0.003)
        px = self.quote(code)
        limit = round(px * (1 + slip) if side == "매수" else px * (1 - slip), 2)   # 지정가 (미국 호가 단위 1센트)
        ex = exchange(code, p)
        tr = "TTTT1002U" if side == "매수" else ("TTTT1006U" if not self.paper else "TTTT1001U")   # 모의 매도는 VTTT1001U
        body = {"CANO": self.cano, "ACNT_PRDT_CD": self.prod, "OVRS_EXCG_CD": EXCH_ORDER[ex], "PDNO": code,
                "ORD_QTY": str(int(qty)), "OVRS_ORD_UNPR": f"{limit:.2f}", "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00"}
        before = next((h["qty"] for h in (C.read_json(C.ACCOUNT, {}) or {}).get("holdings", []) if h["code"] == code), 0)
        try:
            res = self._call("POST", "/uapi/overseas-stock/v1/trading/order", tr, body=body)
        except KisError as e:
            C.append_jsonl(os.path.join(C.WORK, "orders.jsonl"), {"time": C.stamp(), "run": os.environ.get("RUN_ID", ""), "tr": tr,
                                                                  "code": code, "side": side, "qty": qty, "limit": limit, "error": str(e)})
            return {"ok": False, "msg": str(e)}
        odno = str((res.get("output") or {}).get("ODNO") or "")
        C.append_jsonl(os.path.join(C.WORK, "orders.jsonl"), {"time": C.stamp(), "run": os.environ.get("RUN_ID", ""), "tr": tr,
                                                              "code": code, "side": side, "qty": qty, "limit": limit, "response": res})
        oo = C.read_json(OPEN_ORDERS, {}) or {}
        oo[odno or f"{code}-{time.time():.0f}"] = {"code": code, "side": side, "qty": qty, "limit": limit, "exchange": ex, "epoch": time.time(), "before": before}
        C.write_json(OPEN_ORDERS, oo)
        acc, after, moved = None, None, 0
        for _ in range(5):   # 지정가라 바로 안 맞을 수 있음 → 2초 간격으로 최대 5번 잔고 확인
            time.sleep(2)
            try:
                acc = self.account()
            except Exception:  # noqa: BLE001
                continue
            after = next((h for h in acc["holdings"] if h["code"] == code), None)
            moved = abs((after["qty"] if after else 0) - before)
            if moved >= qty:
                break
        if moved >= qty and odno in oo:
            oo.pop(odno, None)
            C.write_json(OPEN_ORDERS, oo)
        fill_px = (after["avg_price"] if side == "매수" and after and not before else limit) if moved else limit
        return {"ok": True, "order_no": odno, "price": fill_px, "qty": moved or qty, "filled": moved > 0 if acc else None,
                "limit": limit, "pending": max(0, qty - moved)}

    def cancel(self, odno, o):
        body = {"CANO": self.cano, "ACNT_PRDT_CD": self.prod, "OVRS_EXCG_CD": EXCH_ORDER[o["exchange"]], "PDNO": o["code"],
                "ORGN_ODNO": odno, "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "OVRS_ORD_UNPR": "0", "ORD_SVR_DVSN_CD": "0"}
        return self._call("POST", "/uapi/overseas-stock/v1/trading/order-rvsecncl", "TTTT1004U", body=body)


def cancel_stale(brk, minutes=None):
    """unfilled_cancel_min 분 넘게 체결 안 된 지정가 주문 취소. 이미 체결된 주문은 취소가 실패해도 그냥 지움.
    반환: 취소한 주문 목록. 가상 계좌 · KR 은 아무것도 안 함."""
    if not isinstance(brk, KisUsBroker):
        return []
    m = minutes if minutes is not None else (C.params().get("us") or {}).get("unfilled_cancel_min", 10)
    oo, done = C.read_json(OPEN_ORDERS, {}) or {}, []
    for odno, o in list(oo.items()):
        if time.time() - o["epoch"] < m * 60:
            continue
        try:
            brk.cancel(odno, o)
            done.append({"order_no": odno, **o})
            C.journal("cancel", code=o["code"], side=o["side"], qty=o["qty"], limit=o["limit"], order_no=odno,
                      detail=f"지정가 {o['limit']} {m}분 미체결 → 취소")
        except Exception as e:  # noqa: BLE001  이미 체결 · 취소된 주문
            C.append_jsonl(os.path.join(C.WORK, "orders.jsonl"), {"time": C.stamp(), "cancel": odno, "error": str(e)[:200]})
        oo.pop(odno, None)
    C.write_json(OPEN_ORDERS, oo)
    return done


def fill_extra(res):
    """체결 기록에 붙이는 값: 통화 · 원/달러 환율 · 원화 금액 · 지정가 · 미체결 수량 (양도세 계산 · 원화 수익률용)."""
    fx = (C.read_json(C.ACCOUNT, {}) or {}).get("fx") or fx_cached()
    amt = res["qty"] * res["price"]
    return {"currency": "USD", "fx": fx, "amount_krw": round(amt * fx) if fx else None,
            "limit": res.get("limit"), "pending": res.get("pending", 0)}


def fx_cached(max_age=3600):
    """원/달러 환율 (1시간 캐시, Yahoo). 실패하면 마지막 값."""
    path = os.path.join(C.WORK, "fx.json")
    f = C.read_json(path, {}) or {}
    if f.get("rate") and time.time() - f.get("epoch", 0) < max_age:
        return f["rate"]
    try:
        rate = yahoo.fx_usdkrw()
        C.write_json(path, {"rate": rate, "epoch": time.time()})
        return rate
    except Exception:  # noqa: BLE001
        return f.get("rate")


# ---------------- 가상 계좌 ----------------
class MockUsBroker(B.MockBroker):
    """가상 미국 계좌: 시작 금액 = config/us/risk_limits.json initial_capital (달러), 가격 = Yahoo, 즉시 체결."""
    MARKETS = {"US"}

    def __init__(self):   # pykrx 를 부르지 않음
        self.state = C.read_json(B.MOCK_ACCOUNT) or {"cash": C.risk_limits()["initial_capital"], "positions": {}}

    def quote(self, code):
        p = B.cached_quote(code, 60)
        if p:
            return p
        px, chg = yahoo.quote(code)
        B.cache_quote(code, px, chg)
        return px

    def account(self):
        acc = super().account()
        acc["fx"] = fx_cached()
        return B.save_account(acc)
