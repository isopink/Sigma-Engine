"""
미국 계좌 잔고 루프 (SIGMA_MARKET=US · KIS). 뉴욕 시간 정규장 동안 30초마다 잔고 + S&P 500 지수를
live/last_snapshot_us.json 에 남김 → 장중 감시(monitor) · 실시간 국면 힌트가 KIS 를 다시 부르지 않고 읽음.

실제 돈 계좌라 Supabase(공개 대시보드)에는 기본으로 올리지 않음 (us-stocks 11-19).

  python live/snapshot_us.py          한 번
  python live/snapshot_us.py --loop   장 끝날 때까지 반복 (작업 스케줄러 SigmaUS-snapshot)
"""
import json
import os
import sys
import time

os.environ["SIGMA_MARKET"] = "US"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "agent", "src"))
import common as C  # noqa: E402

C.load_env()
OUT = C.LAST_SNAPSHOT
PID = os.path.join(C.WORK, "snapshot.pid")


def index_now():
    try:
        import yahoo
        return yahoo.quote("^GSPC")[0]
    except Exception as e:  # noqa: BLE001
        print(f"  (S&P 500 조회 건너뜀: {e})")
        return None


def run_once(brk, idx_cache):
    acc = brk.account()
    if time.time() - idx_cache.get("t", 0) > 60:   # 지수는 1분에 한 번
        idx_cache.update(v=index_now(), t=time.time())
    snap = {"total": acc["total"], "cash": acc["cash"], "stock_eval": acc["stock_value"], "holdings": acc["holdings"],
            "fx": acc.get("fx"), "index": idx_cache.get("v"), "currency": "USD", "epoch": time.time(), "time": C.stamp()}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    print(f"[{C.now().strftime('%m-%d %H:%M:%S')} 뉴욕] 총자산 {C.money(acc['total'])} · 현금 {C.money(acc['cash'])} · "
          f"종목 {len(acc['holdings'])}개 · S&P 500 {snap['index']}")


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass
    import broker
    brk = broker.get()
    cache = {}
    if "--loop" not in sys.argv:
        return run_once(brk, cache)
    os.makedirs(C.WORK, exist_ok=True)
    with open(PID, "w") as f:
        f.write(str(os.getpid()))
    p = C.params()
    interval = max(30, int((p.get("live") or {}).get("snapshot_sec") or 30))
    s = p["schedule"]
    start = s.get("monitor_start", "09:25")
    print(f"미국 잔고 루프 · {interval}초 간격 · 뉴욕 {start} ~ 마감 5분 뒤")
    while True:
        n = C.now()
        hm = n.strftime("%H:%M")
        close = C.session_close(n.date()) or "16:00"
        end = f"{int(close[:2]):02d}:{int(close[3:]) + 5:02d}" if int(close[3:]) < 55 else close
        if not C.is_trading_day(n.date()) or hm > end:
            print("장 마감 · 종료")
            return
        if hm >= start:
            try:
                run_once(brk, cache)
            except Exception as e:  # noqa: BLE001
                print(f"  ! 실패: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
