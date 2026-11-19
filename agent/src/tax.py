"""
미국 주식 양도차익 (해외주식 양도소득세 참고용). SIGMA_MARKET=US run.py tax [--year 2027]

체결 기록(work_us/journal.jsonl 의 fill, 체결마다 원/달러 환율 fx 를 남김)으로 연간 실현 손익을 원화로 계산:
- 취득가는 **선입선출(FIFO)**, 매수 · 매도 금액은 각 체결 때 환율로 원화 환산
- 수수료(params.costs.fee_rate)는 필요경비로 뺌
- 기본공제 250만 원, 세율 22% (양도소득세 20% + 지방소득세 2%)

주의: 신고용 정식 계산은 결제일 기준환율 · 증권사 실제 수수료를 씀. 이 표는 그 전에 대략을 보는 용도이고,
실제 신고는 증권사 양도세 계산 자료와 대조할 것. 결과는 work_us/tax_{연도}.csv (공개 저장소에 올리지 않음).
"""
import csv
import os
import sys
from collections import defaultdict, deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

DEDUCTION = 2_500_000
RATE = 0.22


def realized(rows, fee_rate, year=None):
    """체결 → [매도 건별 실현 손익 (원화)]. 매수 로트는 FIFO 로 소진."""
    lots, out = defaultdict(deque), []
    for r in sorted((x for x in rows if x.get("kind") == "fill" and x.get("qty")), key=lambda x: x["time"]):
        fx = r.get("fx")
        if not fx:
            continue   # 환율이 없는 기록은 원화 환산 불가 → 빠짐 (리포트에 개수 표시)
        qty, px = int(r["qty"]), float(r["price"])
        if r["side"] == "매수":
            lots[r["code"]].append([qty, px * (1 + fee_rate) * fx])   # 1주당 원화 취득가 (수수료 포함)
            continue
        left, cost = qty, 0.0
        while left and lots[r["code"]]:
            lot = lots[r["code"]][0]
            take = min(left, lot[0])
            cost += take * lot[1]
            lot[0] -= take
            left -= take
            if lot[0] == 0:
                lots[r["code"]].popleft()
        proceeds = (qty - left) * px * (1 - fee_rate) * fx
        if year and not r["time"].startswith(str(year)):
            continue
        out.append({"date": r["time"][:10], "code": r["code"], "name": r.get("name", ""), "qty": qty - left,
                    "price_usd": px, "fx": fx, "proceeds_krw": round(proceeds), "cost_krw": round(cost),
                    "gain_krw": round(proceeds - cost), "unmatched_qty": left})
    return out


def summary(items):
    gain = sum(x["gain_krw"] for x in items)
    taxable = max(0, gain - DEDUCTION)
    return {"gain_krw": gain, "deduction": DEDUCTION, "taxable_krw": taxable, "tax_krw": round(taxable * RATE), "sells": len(items)}


def main():
    if not C.US:
        sys.exit("tax 는 SIGMA_MARKET=US 에서만 (국내 주식 매매는 양도세 대상이 아님)")
    year = int(sys.argv[sys.argv.index("--year") + 1]) if "--year" in sys.argv else C.today().year
    rows = C.read_jsonl(C.JOURNAL)
    items = realized(rows, C.params()["costs"]["fee_rate"], year)
    path = os.path.join(C.WORK, f"tax_{year}.csv")
    os.makedirs(C.WORK, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["date", "code", "name", "qty", "price_usd", "fx", "proceeds_krw", "cost_krw", "gain_krw", "unmatched_qty"])
        w.writeheader()
        w.writerows(items)
    s = summary(items)
    no_fx = sum(1 for r in rows if r.get("kind") == "fill" and not r.get("fx"))
    print(f"{year}년 미국 주식 실현 손익 {s['gain_krw']:+,}원 · 매도 {s['sells']}건 · 과세표준 {s['taxable_krw']:,}원 · 예상 세액 {s['tax_krw']:,}원 (22%)")
    if no_fx:
        print(f"  ! 환율 없는 체결 {no_fx}건은 빠짐")
    print(f"  → {path}")


if __name__ == "__main__":
    main()
