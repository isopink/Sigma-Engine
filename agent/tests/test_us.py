"""미국 시장(SIGMA_MARKET=US) 검사. 대회(KR) 규칙 검사는 test_rules.py.

  python agent/tests/test_us.py

US 모듈은 import 할 때 시장이 정해지므로, 이 파일은 맨 처음에 SIGMA_MARKET=US 를 넣고 시작함.
KR 기본값이 그대로인지는 별도 프로세스로 확인 (test_kr_default_paths).
"""
import os
import subprocess
import sys

os.environ["SIGMA_MARKET"] = "US"
os.environ["MODE"] = "mock"
HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(AGENT, "src"))
import common as C  # noqa: E402


def test_kr_default_paths():
    """SIGMA_MARKET 이 없으면 예전 경로 · 시간대 그대로 (대회 동작 보존)."""
    env = {k: v for k, v in os.environ.items() if k != "SIGMA_MARKET"}
    code = ("import common as C, os;"
            "print(C.MARKET, C.CONFIG.endswith(os.path.join('agent','config')), C.WORK.endswith(os.path.join('agent','work')),"
            " C.DOCS_DATA.endswith(os.path.join('docs','data')), C.ENV_FILE.endswith(os.path.join('agent','.env')), C.TZ is C.KST, C.px_round(1234.6))")
    out = subprocess.run([sys.executable, "-c", code], cwd=os.path.join(AGENT, "src"), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.split()
    assert out == ["KR", "True", "True", "True", "True", "True", "1235"], out


def test_us_paths_and_clock():
    assert C.MARKET == "US" and C.US
    assert C.CONFIG.endswith(os.path.join("config", "us"))
    assert C.WORK.endswith("work_us") and C.ENV_FILE.endswith(".env.us")
    assert C.DOCS_DATA.endswith(os.path.join("docs", "data", "us"))
    assert str(C.TZ) == "America/New_York"
    assert C.px_round(123.456) == 123.46 and C.px_ceil(100.001) == 100.01
    assert C.money(1234.5) == "$1,234.50" and C.money(-3, signed=True) == "-$3.00"


def test_bad_market_rejected():
    env = {**os.environ, "SIGMA_MARKET": "JP"}
    r = subprocess.run([sys.executable, "-c", "import common"], cwd=os.path.join(AGENT, "src"), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode != 0 and "SIGMA_MARKET" in r.stderr


def _acc(total=7000.0, cash=7000.0, hs=None):
    return {"total": total, "cash": cash, "stock_value": total - cash, "holdings": hs or []}


def _with_universe(fn):
    """universe.json 대신 작은 목록으로."""
    def run():
        old = C.universe
        C.universe = lambda p=None: {"AAPL": {"name": "Apple", "exchange": "NASDAQ"}, "BRK.B": {"name": "Berkshire", "exchange": "NYSE"}}
        try:
            fn()
        finally:
            C.universe = old
    run.__name__ = fn.__name__
    return run


def _ready(fn):
    """전략 확정(READY=True) 상태로 실행 (기본은 False = 주문 전부 차단)."""
    def run():
        import strategy_us as S
        old = S.READY
        S.READY = True
        try:
            fn()
        finally:
            S.READY = old
    run.__name__ = fn.__name__
    return run


@_ready
@_with_universe
def _guard_cases():
    import risk
    p, r = C.params(), {**C.risk_limits(), "enabled": True}
    g = lambda code, side="매수", acc=None, rr=r: [x for x, _ in risk.us_guard({"code": code, "side": side}, acc or _acc(), p, rr)]  # noqa: E731
    assert g("AAPL") == [] and g("BRK.B") == [] and g("SPY") == [] and g("SH") == []
    assert "종목 범위" in g("TQQQ")                 # 레버리지 ETF · 범위 밖
    assert "시장" in g("005930")                     # 국내 코드는 미국 실행에서 거절
    assert "비상 정지" in g("AAPL", rr={**r, "enabled": False})
    assert "비상 정지" in g("AAPL", "매도", rr={**r, "enabled": False})   # 비상 정지는 매도도 막음
    assert "누적 손실 정지" in g("AAPL", acc=_acc(total=6200))            # 7000 → 6200 = −11.4%
    assert "누적 손실 정지" not in g("AAPL", "매도", acc=_acc(total=6200))  # 손실 정지여도 매도는 됨
    os.makedirs(C.WORK, exist_ok=True)
    open(C.STOP_FILE, "w").close()
    try:
        assert "비상 정지" in g("AAPL")
    finally:
        os.remove(C.STOP_FILE)


def test_us_guard():
    _guard_cases()


def test_default_risk_limits_halted():
    """처음 받은 상태는 비상 정지 (키를 넣고 사람이 직접 켜야 주문이 나감)."""
    import risk
    assert C.risk_limits()["enabled"] is False and risk.us_halted()


def test_brokers_refuse_wrong_market():
    import broker
    import kis
    try:
        broker.KiwoomBroker()
        raise AssertionError("US 실행에서 키움 브로커가 만들어짐")
    except RuntimeError as e:
        assert "키움" in str(e)
    m = kis.MockUsBroker.__new__(kis.MockUsBroker)
    try:
        broker.check_market(m, "005930")
        raise AssertionError("미국 브로커가 국내 코드를 받음")
    except RuntimeError:
        pass
    broker.check_market(m, "AAPL")
    k = broker.MockBroker.__new__(broker.MockBroker)   # KR 가상 계좌 (MARKETS KR) 는 US 실행에서 거절
    try:
        broker.check_market(k, "AAPL")
        raise AssertionError("KR 브로커가 US 실행에서 주문 받음")
    except RuntimeError:
        pass


@_ready
def test_sizing_us_limit_and_cents():
    import sizing
    p, r = C.params(), {**C.risk_limits(), "enabled": True}
    old = C.universe
    C.universe = lambda p=None: {"AAPL": {"name": "Apple", "exchange": "NASDAQ"}}
    try:
        plan = sizing.plan_buy("AAPL", 0.85, 201.37, _acc(), p, r, regime="상승")
    finally:
        C.universe = old
    assert plan["qty"] > 0 and plan["amount"] <= r["max_order_amount"] * 1.0001, plan
    assert plan["stop"] == round(201.37 * (1 + p["decision"]["stop_loss_pct"]), 2)
    halted = sizing.plan_buy("AAPL", 0.85, 201.37, _acc(), p, {**r, "enabled": False}, regime="상승")
    assert halted["qty"] == 0 and "비상 정지" in halted["limited_by"]


def test_calendar_session():
    from datetime import date, datetime
    p = C.params()
    at = lambda d, hm: datetime(*d, *hm, tzinfo=C.TZ)  # noqa: E731
    assert C.in_session(at((2026, 11, 30), (10, 0)), p)                 # 월요일 장중
    assert not C.in_session(at((2026, 11, 30), (9, 0)), p)              # 개장 전
    assert not C.in_session(at((2026, 11, 26), (11, 0)), p)             # 추수감사절 휴장
    assert C.in_session(at((2026, 11, 27), (12, 30)), p)                # 조기 마감일 13:00 전
    assert not C.in_session(at((2026, 11, 27), (13, 30)), p)            # 조기 마감 뒤
    assert C.business_days("2026-11-25", date(2026, 11, 30), p) == 2   # 26 휴장 · 주말 빼고 27 · 30


def test_ticker_format():
    assert C.code_market("AAPL") == "US" and C.code_market("BRK.B") == "US"
    assert C.code_market("005930") == "KR" and C.code_market("0009K0") == "KR"
    assert C.code_market("ENERGY") is None and C.code_market("HEALTH CARE") is None   # S&P 500 표의 업종 합계 행


def test_briefing_text():
    import us_data
    row = {"code": "AAPL", "name": "Apple", "close": 230.12, "ma5": 228.5, "ma20": 220.1, "ma60": 210.0, "ret5": 0.012,
           "ret20": 0.05, "rsi14": 61.2, "vol20": 0.25, "mdd60": -0.08, "rel_strength": 0.03, "trading_value_20d": 9e9, "score": 0.9,
           "news": [{"date": "11-18", "title": "Apple faces lawsuit", "risk": True}]}
    b = {"asof": "2026-11-18", "market": {"source": "S&P 500", "close": 7323.63, "ma5": 7400, "ma20": 7500, "ma60": 7600,
                                           "rule_hint": "하락", "us": []}, "candidates": [row], "holdings": []}
    t = us_data.to_text(b)
    assert "| AAPL | Apple | 230.12 |" in t and "$9,000M" in t and "⚠ Apple faces lawsuit" in t and "SPY 대비" in t


def test_tax_fifo_krw():
    import tax
    rows = [
        {"kind": "fill", "time": "2027-01-05 10:00", "code": "AAPL", "side": "매수", "qty": 10, "price": 100.0, "fx": 1300},
        {"kind": "fill", "time": "2027-02-05 10:00", "code": "AAPL", "side": "매수", "qty": 10, "price": 120.0, "fx": 1400},
        {"kind": "fill", "time": "2027-03-05 10:00", "code": "AAPL", "side": "매도", "qty": 15, "price": 130.0, "fx": 1350},
        {"kind": "fill", "time": "2027-03-06 10:00", "code": "MSFT", "side": "매수", "qty": 1, "price": 400.0},   # 환율 없음 → 빠짐
    ]
    it = tax.realized(rows, 0.0, 2027)
    assert len(it) == 1
    x = it[0]
    assert x["cost_krw"] == 10 * 100 * 1300 + 5 * 120 * 1400            # FIFO: 먼저 산 10주 + 다음 5주
    assert x["proceeds_krw"] == 15 * 130 * 1350
    s = tax.summary(it)
    assert s["taxable_krw"] == max(0, x["gain_krw"] - 2_500_000) and s["tax_krw"] == round(s["taxable_krw"] * 0.22)


def test_monitor_backup_after_grace():
    import time as _t
    import monitor
    calls = []
    old = monitor.backup_sell
    monitor.backup_sell = lambda h, why: calls.append(h["code"])
    try:
        hs = [{"code": "AAPL", "name": "Apple", "qty": 3, "price": 90.0}]
        hit = {"AAPL": ("AAPL@k", "손절")}
        st = {"date": C.today().isoformat(), "fired": {"AAPL@k": _t.time() - 60}}
        monitor.backup(hit, hs, st)
        assert calls == []                                   # 실행 뒤 1분: 아직 에이전트를 기다림
        st["fired"]["AAPL@k"] = _t.time() - 6 * 60
        monitor.backup(hit, hs, st)
        monitor.backup(hit, hs, st)
        assert calls == ["AAPL"]                             # 5분 넘게 그대로면 코드가 한 번만 매도
    finally:
        monitor.backup_sell = old
        try:
            os.remove(monitor.STATE)
        except OSError:
            pass
    assert monitor.trade_from() == 1000                      # 미국은 10:00 전 규칙 매도 보류


def test_tick_plan():
    from datetime import datetime
    sys.path.insert(0, AGENT)
    import run
    p = C.params()
    at = lambda d, h, m: datetime(2026, 11, d, h, m, 4, tzinfo=C.TZ)  # noqa: E731
    plan = lambda d, h, m, st=None: run.tick_plan(at(d, h, m), p, st or {})  # noqa: E731
    assert plan(30, 7, 0) == []                                  # 월 07:00 (한국 21:00): 아직 없음
    assert plan(30, 8, 30) == ["prep"]
    assert plan(30, 10, 0) == ["decide"]                         # 첫 회차 (준비는 판단이 대신 함)
    assert plan(30, 10, 7, {"decide": "10:00"}) == []            # 같은 회차는 한 번만
    assert plan(30, 15, 45) == ["decide"] and plan(30, 16, 0) == []
    assert plan(30, 16, 15, {"prep": 1}) == ["close"]
    assert plan(30, 16, 30, {"prep": 1, "close": 1}) == []       # 월요일엔 회고 없음
    assert plan(30, 16, 45, {"prep": 1, "close": 1}) == ["tune"]
    wed = {"prep": 1, "close": 1}
    assert run.tick_plan(datetime(2026, 12, 2, 16, 30, tzinfo=C.TZ), p, wed) == ["review"]
    assert run.tick_plan(datetime(2026, 12, 2, 17, 0, tzinfo=C.TZ), p, {**wed, "review": 1, "tune": 1}) == ["report"]
    assert plan(26, 10, 0) == []                                 # 추수감사절 휴장
    assert plan(27, 12, 45) == ["decide"] and plan(27, 13, 0) == []   # 조기 마감 13:00 → 12:45 회차까지
    assert plan(28, 10, 0) == []                                 # 토요일


def test_strategy_default_halts():
    """메인 설계 전(READY=False)엔 enabled=true 여도 주문 전부 차단. 국내 실행엔 전략 모듈이 끼지 않음."""
    import risk
    import strategy_us as S
    assert S.READY is False
    assert "READY" in risk.us_halted({**C.risk_limits(), "enabled": True})
    env = {k: v for k, v in os.environ.items() if k != "SIGMA_MARKET"}
    out = subprocess.run([sys.executable, "-c", "import common as C; print(C.strategy())"], cwd=os.path.join(AGENT, "src"),
                         env=env, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
    assert out == "None", out


@_ready
@_with_universe
def test_strategy_plugs():
    """strategy_us 함수를 채우면 plan_order(사이징) · Hook(risk.check) · 손절 익절 계산이 모두 그 값을 씀."""
    import risk
    import sizing
    import strategy_us as S
    p, r = C.params(), {**C.risk_limits(), "enabled": True}
    saved = {k: getattr(S, k) for k in ("confidence", "buy_weight", "sell_qty", "exit_levels", "risk_rules")}
    dp, risk.daily_pnl = risk.daily_pnl, lambda acc: 0.0   # work_us/day_start.json (가상 계좌 기록) 영향 없이
    rl, C.risk_limits = C.risk_limits, lambda: r              # plan_sell 은 파일의 한도를 읽음 → 켠 상태로
    try:
        S.confidence = lambda code, side, conf, ctx: (conf + 0.1, "테스트 +0.1")
        S.buy_weight = lambda code, conf, ctx: 0.05 if conf >= 0.5 else 0.0
        S.exit_levels = lambda code, ctx: (-0.04, 0.06, 5)
        S.sell_qty = lambda code, have, conf, ctx: have if conf >= 0.9 else 0
        S.risk_rules = lambda order, acc, price, ctx: [("테스트 금지", "BRK.B 금지")] if order["code"] == "BRK.B" else []
        assert C.conf_boost("AAPL", "매수", 0.45, p)[0] == 0.55
        acc = _acc(total=7000.0, cash=7000.0)
        plan = sizing.plan_buy("AAPL", 0.55, 100.0, acc, p, r, regime="상승")
        assert plan["target_weight"] == 0.05 and plan["qty"] == 3, plan          # 7000 × 5% = 350 → 3주
        assert plan["stop"] == 96.0 and plan["take"] == 106.0 and plan["max_days"] == 5
        assert risk.check({"code": "AAPL", "side": "매수", "qty": 3, "confidence": 0.45}, acc, 100.0, p, r) == []   # 0.45 + 0.1 → 비중 5%
        assert any("확신도" in x for x, _ in risk.check({"code": "AAPL", "side": "매수", "qty": 3, "confidence": 0.3}, acc, 100.0, p, r))
        assert sizing.plan_buy("BRK.B", 0.9, 100.0, acc, p, r, regime="상승")["qty"] == 0
        assert any("테스트 금지" in x for x, _ in risk.check({"code": "BRK.B", "side": "매수", "qty": 1, "confidence": 0.9}, acc, 100.0, p, r))
        held = _acc(total=7000.0, cash=6000.0, hs=[{"code": "AAPL", "name": "Apple", "qty": 10, "avg_price": 100.0, "price": 100.0, "value": 1000.0}])
        assert sizing.plan_sell("AAPL", 0.6, 100.0, held, p)["qty"] == 0               # plan_sell 은 보정 후 확신도를 받음
        assert sizing.plan_sell("AAPL", 0.95, 100.0, held, p)["qty"] == 10
        assert risk.check({"code": "AAPL", "side": "매도", "qty": 10, "confidence": 0.85}, held, 100.0, p, r) == []
        assert risk.check({"code": "AAPL", "side": "매도", "qty": 10, "confidence": 0.1, "rule_exit": True}, held, 100.0, p, r) == []   # 규칙 매도는 전략 수량과 무관
        S.risk_rules = lambda *a: 1 / 0
        assert any("전략 규칙 오류" in x for x, _ in risk.check({"code": "AAPL", "side": "매수", "qty": 3, "confidence": 0.45}, acc, 100.0, p, r))   # 오류면 막음
    finally:
        for k, v in saved.items():
            setattr(S, k, v)
        risk.daily_pnl, C.risk_limits = dp, rl


if __name__ == "__main__":
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            try:
                f()
                print("ok  ", n)
            except AssertionError as e:
                fails += 1
                print("FAIL", n, e)
    sys.exit(1 if fails else 0)
