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
