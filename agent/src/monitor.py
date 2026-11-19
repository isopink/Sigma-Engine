"""
장중 감시 (LLM 없음). 1분마다 보유 종목의 손절가·익절가·보유일을 확인하고,
조건에 닿은 종목이 있으면 손절·익절 실행(run.py exit)을 띄웁니다. 주문은 그 실행의 에이전트가 Hook 을 거쳐 냅니다.

키움 호출을 늘리지 않도록 live 모드에서는 live/snapshot.py 가 남긴 last_snapshot.json (US 는 last_snapshot_us.json) 만 읽습니다.
US 는 실행이 grace_min 분 안에 못 팔면 코드가 직접 매도 (backup, 11-19 S.Lee: 대회용 아님 · 실제 돈).
"""
import os
import subprocess
import sys
import time

import common as C

STATE = os.path.join(C.WORK, "monitor_state.json")
RETRY_SEC = 10 * 60   # 매도 실행 뒤에도 잔고에 남아 있으면 이만큼 지나서 다시


def holdings():
    if C.mode() == "live":
        snap = C.read_json(C.LAST_SNAPSHOT)
        if not snap or time.time() - snap.get("epoch", 0) > 15 * 60:
            return None
        return snap["holdings"]
    import broker
    return broker.get().account()["holdings"]


TRADE_FROM = 930   # 11-06 S.Lee: 09:30 전엔 손절·익절·보유일에 걸려도 매도하지 않음 (09:10 손절 보류에서 넓힘)


def trade_from(p=None):
    """이 시각 전엔 규칙 매도 보류 (HHMM). KR 09:30 그대로, US 는 params.schedule.sell_from (개장 30분 뒤 10:00)."""
    f = ((p or C.params()).get("schedule") or {}).get("sell_from")
    return int(f.replace(":", "")) if f else TRADE_FROM


def triggers(hs):
    """{code: (매수 건 키, 사유)}. 키는 진입일·손절가·익절가로 만들어 다시 사거나 더 사면 새 건으로 봄."""
    meta = C.position_meta(hs)
    hit = {}
    for h in hs:
        m = meta.get(h["code"])
        if not m:
            continue
        key = f"{h['code']}@{m.get('entry_date')}/{m.get('stop')}/{m.get('take')}"
        if m.get("stop") and h["price"] <= m["stop"]:
            hit[h["code"]] = (key, f"{h['name']} 현재가 {h['price']:,} ≤ 손절가 {m['stop']:,}")
        elif m.get("take") and h["price"] >= m["take"]:
            hit[h["code"]] = (key, f"{h['name']} 현재가 {h['price']:,} ≥ 익절가 {m['take']:,}")
        elif m.get("max_days") and m.get("entry_date") and C.business_days(m["entry_date"], C.today()) >= m["max_days"]:   # 11-18 S.Lee: 일반 종목 10거래일 (값이 없으면 보유일로 안 팖)
            hit[h["code"]] = (key, f"{h['name']} 보유 {C.business_days(m['entry_date'], C.today())}일 ≥ 최대 {m['max_days']}일")
    n = C.now()
    tf = trade_from()
    if hit and n.hour * 100 + n.minute < tf:   # 장 전·장 초반 투매 구간 (11-06 KODEX 200 이 시가 −8.6% 에 손절된 일)
        for _, why in hit.values():
            print(f"  매도 보류 ({tf // 100}:{tf % 100:02d} 전): {why}", flush=True)
        return {}
    return hit


def check_once():
    hs = holdings()
    if hs is None:
        print("  (최근 잔고 스냅샷이 없어 건너뜀)", flush=True)
        return
    hit = triggers(hs)
    st = C.read_json(STATE, {}) or {}
    if st.get("date") != C.today().isoformat() or not isinstance(st.get("fired"), dict):
        st = {"date": C.today().isoformat(), "fired": {}}
    if C.US:
        backup(hit, hs, st)
    n = C.now()
    hm, t = n.hour * 100 + n.minute, time.time()

    def due(key):
        # 같은 매수 건은 실행 뒤 RETRY_SEC 동안 다시 띄우지 않음 (매도 체결 전 중복 주문 방지).
        # 장 전(08:30~09:00)에 낸 매도는 09:00 동시호가로 체결되거나 거절될 수 있음 → 장 전엔 한 번만, 09:05 뒤 남아 있으면 다시
        at = st["fired"].get(key)
        return at is None or (hm >= (905 if not C.US else trade_from() + 5) and t - at >= RETRY_SEC)

    new = {c: why for c, (key, why) in hit.items() if due(key)}
    if not new:
        return
    print(f"[{C.now().strftime('%H:%M')}] 조건 도달: " + " / ".join(new.values()), flush=True)
    r = subprocess.run([sys.executable, os.path.join(C.AGENT, "run.py"), "exit",
                        "--codes", ",".join(new), "--why", " / ".join(new.values())], cwd=C.AGENT)
    if r.returncode == 0:   # 실행이 잠금으로 건너뛰었으면 다음 회차에 다시
        st["fired"].update({hit[c][0]: t for c in new})
        C.write_json(STATE, st)


def backup(hit, hs, st):
    """US 손절 백업 (params.us.backup_stop, 11-19 S.Lee: 실제 돈). 조건이 닿아 손절 · 익절 실행을 띄웠는데
    grace_min 분 뒤에도 그대로 들고 있으면 (Claude 사용 한도 · 실행 실패 등) 코드가 직접 전량 매도.
    리스크 검사(비상 정지 포함)는 Hook 과 같은 risk.check 를 거침. 같은 건은 한 번만."""
    cfg = ((C.params().get("us") or {}).get("backup_stop")) or {}
    if not cfg.get("enabled"):
        return
    grace = cfg.get("grace_min", 5) * 60
    done = st.setdefault("backup", {})
    for code, (key, why) in hit.items():
        at = st["fired"].get(key)
        if at is None or time.time() - at < grace or key in done:
            continue
        h = next((x for x in hs if x["code"] == code), None)
        if not h or h["qty"] <= 0:
            continue
        done[key] = time.time()
        C.write_json(STATE, st)
        print(f"  ! 백업 매도: {why} · 에이전트 실행 뒤 {cfg.get('grace_min', 5)}분 지나도 보유 중", flush=True)
        try:
            backup_sell(h, why)
        except Exception as e:  # noqa: BLE001
            print(f"  ! 백업 매도 실패: {e}", flush=True)
            C.journal("block", code=code, name=h["name"], side="매도", rule="백업 매도 실패", detail=str(e)[:200], by="monitor")


def backup_sell(h, why):
    import broker
    import risk
    os.environ["RUN_MODE"] = "exit"
    brk = broker.get()
    acc = brk.account()
    held = next((x for x in acc["holdings"] if x["code"] == h["code"]), None)
    if not held:
        return print("  이미 팔림", flush=True)
    price = brk.quote_info(h["code"])["price"]
    order = {"code": h["code"], "side": "매도", "qty": held["qty"], "confidence": 1.0, "rule_exit": True}
    bad = risk.check(order, acc, price)
    if bad:
        for rule, detail in bad:
            C.journal("block", code=h["code"], name=h["name"], side="매도", rule=rule, detail=detail, by="monitor")
        return print("  백업 매도 막힘: " + " / ".join(f"{a}({b})" for a, b in bad), flush=True)
    res = brk.order(h["code"], h["name"], "매도", held["qty"])
    if not res.get("ok"):
        raise RuntimeError(res.get("msg"))
    import kis   # 체결 기록에 환율 · 원화 금액
    meta = C.read_json(C.POSITIONS, {}) or {}
    meta.pop(h["code"], None)
    C.write_json(C.POSITIONS, meta)
    C.journal("fill", code=h["code"], name=h["name"], side="매도", qty=res["qty"], ordered=held["qty"], price=res["price"],
              amount=res["qty"] * res["price"], confidence=1.0, rule_exit=True, by="monitor_backup",
              bull="", bear="", decision=f"코드 백업 매도: {why} (에이전트 실행이 시간 안에 팔지 못함)",
              order_no=res.get("order_no"), filled=res.get("filled", True), **kis.fill_extra(res))


def loop():
    s = C.params()["schedule"]
    start, end = (int(t.replace(":", "")) for t in (s["monitor_start"], s["monitor_end"]))
    if C.US and (sc := C.session_close()) and sc < s["monitor_end"]:   # 조기 마감일 (추수감사절 다음 날 등)
        end = int(sc.replace(":", ""))
    print(f"장중 감시 시작 · {s['monitor_interval_sec']}초 간격 · {s['monitor_end']} 종료", flush=True)
    if C.US:   # run.py tick 이 살아 있는지 봄
        os.makedirs(C.WORK, exist_ok=True)
        with open(os.path.join(C.WORK, "monitor.pid"), "w") as f:
            f.write(str(os.getpid()))
    while True:
        n = C.now()
        hm = n.hour * 100 + n.minute
        if not C.is_trading_day(n.date()) or hm > end:   # KR 은 휴장일 목록이 없어 예전처럼 주말만
            print("감시 종료", flush=True)
            return
        if C.US and C.mode() == "live":   # 오래된 미체결 지정가 주문 취소
            try:
                import broker
                import kis
                kis.cancel_stale(broker.get())
            except Exception as e:  # noqa: BLE001
                print(f"  ! 미체결 취소 실패: {e}", flush=True)
        if hm >= start:
            try:
                check_once()
            except Exception as e:  # noqa: BLE001
                print(f"  ! 감시 실패: {e}", flush=True)
        time.sleep(s["monitor_interval_sec"])
