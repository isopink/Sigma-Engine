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
    out = subprocess.run([sys.executable, "-c", code], cwd=os.path.join(AGENT, "src"), env=env, capture_output=True, text=True).stdout.split()
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
    r = subprocess.run([sys.executable, "-c", "import common"], cwd=os.path.join(AGENT, "src"), env=env, capture_output=True, text=True)
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
