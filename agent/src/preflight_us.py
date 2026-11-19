"""
미국 실행 시작 전 점검 (SIGMA_MARKET=US). 주문은 내지 않고 읽기만 함.

  run_us.bat check            (= python run.py check)

모든 줄이 ✅ 면 그날 밤부터 바로 돌릴 수 있음. ❌ 는 꼭 고칠 것, ⚠ 는 알고 넘어가도 되는 것.
"""
import json
import os
import subprocess
import sys
import time

import common as C

OK, BAD, WARN = "✅", "❌", "⚠ "


def filled(fn):
    """strategy_us 함수가 기본(docstring + return None / return []) 그대로가 아니면 True."""
    import ast
    import inspect
    import textwrap
    body = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0].body
    body = [b for b in body if not (isinstance(b, ast.Expr) and isinstance(getattr(b, "value", None), ast.Constant))]
    empty = len(body) == 1 and isinstance(body[0], ast.Return) and (
        body[0].value is None or (isinstance(body[0].value, ast.Constant) and body[0].value.value is None)
        or (isinstance(body[0].value, ast.List) and not body[0].value.elts))
    return not empty


def checks():
    out = []
    add = lambda mark, name, detail="": out.append((mark, name, detail))  # noqa: E731
    p, r = C.params(), C.risk_limits()
    S = C.strategy()

    # 1. 메인 설계
    add(OK if S.READY else BAD, "전략 확정 (strategy_us.READY)", S.NAME)
    custom = [f for f in ("confidence", "buy_weight", "sell_qty", "exit_levels", "risk_rules") if filled(getattr(S, f))]
    add(OK if custom or S.READY else WARN, "전략 함수", ", ".join(custom) + " 를 채움" if custom else "전부 비어 있음 → 국내 규칙 복사로 돎")
    pend = [n for n, d in (("params.decision", p.get("decision", {})), ("risk_limits", r)) if "_pending" in d]
    add(BAD if pend else OK, "임시값 표시 지움 (_pending)", ", ".join(pend) or "없음")

    # 2. 리스크 · 스위치
    add(OK if r.get("enabled") else BAD, "비상 정지 해제 (risk_limits.enabled)", "true" if r.get("enabled") else "false → 주문 전부 막힘")
    add(BAD if os.path.exists(C.STOP_FILE) else OK, "work_us/STOP 파일 없음")
    add(OK, "리스크 한도", f"종목당 {r['max_weight_per_stock']:.0%} · {r['max_positions']}종목 · 현금 {r['min_cash_ratio']:.0%} · "
                          f"1회 {C.money(r['max_order_amount'])} · 일일 {r['daily_loss_limit']:.0%} · 누적 {r['total_loss_limit']:.0%} · 시작 {C.money(r['initial_capital'])}")
    tun = C.read_json(os.path.join(C.CONFIG, "tuning.json"), {}) or {}
    keys = [k for k in (tun.get("tunable") or tun.get("keys") or {})]
    add(WARN if keys else OK, "자동 조정", f"매일 {len(keys)}개 기준값을 바꿀 수 있음 (config/us/tuning.json)" if keys else "꺼짐")

    # 3. 키 · 계좌
    env = os.path.exists(C.ENV_FILE)
    add(OK if env else BAD, ".env.us", "있음" if env else "agent/.env.us.example 을 복사해 채울 것")
    miss = [k for k in ("KIS_APPKEY", "KIS_APPSECRET", "KIS_ACCOUNT", "KIS_BASE") if not os.environ.get(k)]
    add(BAD if miss else OK, "KIS 키 · 계좌번호", ("비어 있음: " + ", ".join(miss)) if miss else "채워짐")
    add(OK if C.mode() == "live" else WARN, "MODE", C.mode() + ("" if C.mode() == "live" else " → 가상 계좌"))
    if C.mode() == "live" and not miss:
        try:
            import broker
            acc = broker.get().account()
            add(OK, "KIS 계좌 조회", f"총자산 {C.money(acc['total'])} · 현금 {C.money(acc['cash'])} · {len(acc['holdings'])}종목 · 환율 {acc.get('fx')}")
            gap = acc["total"] / r["initial_capital"] - 1 if r.get("initial_capital") else 0
            add(OK if abs(gap) < 0.05 else WARN, "시작 금액 ↔ 실제 잔고", f"{gap:+.1%} (5% 넘게 다르면 risk_limits.initial_capital 을 고칠 것)")
        except Exception as e:  # noqa: BLE001
            add(BAD, "KIS 계좌 조회", f"{type(e).__name__}: {e}")

    # 4. 데이터 · 일정
    u = os.path.join(C.CONFIG, "universe.json")
    age = (time.time() - os.path.getmtime(u)) / 86400 if os.path.exists(u) else None
    add(OK if age is not None and age < 10 else WARN, "종목 목록 universe.json",
        f"{len(C.universe(p))}종목 · {age:.0f}일 전" if age is not None else "없음 (prep 이 받음)")
    if sys.platform == "win32":
        for t in ("SigmaUS-tick", "SigmaUS-monitor", "SigmaUS-snapshot"):
            ok = subprocess.run(["schtasks", "/query", "/tn", t], capture_output=True).returncode == 0
            add(OK if ok else BAD, f"작업 스케줄러 {t}", "등록됨" if ok else "setup_tasks_us.ps1 로 등록")
    nxt = C.now()
    add(OK, "뉴욕 시간", f"{nxt.strftime('%Y-%m-%d %H:%M')} · 오늘 {'개장' if C.is_trading_day(nxt.date()) else '휴장'}")

    # 5. 검사
    t = subprocess.run([sys.executable, os.path.join(C.AGENT, "tests", "test_us.py")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    add(OK if t.returncode == 0 else BAD, "test_us.py", "통과" if t.returncode == 0 else (t.stdout + t.stderr).strip().splitlines()[-1:])
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    C.load_env()
    rows = checks()
    w = max(len(n) for _, n, _ in rows)
    for mark, name, detail in rows:
        print(f"{mark} {name.ljust(w)}  {detail}")
    bad = [n for m, n, _ in rows if m == BAD]
    print()
    print("시작 가능 ✅" if not bad else f"시작 전 고칠 것 {len(bad)}개: " + " · ".join(bad))
    C.write_json(os.path.join(C.WORK, "preflight.json"), {"time": C.stamp(), "ok": not bad, "rows": rows})
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
