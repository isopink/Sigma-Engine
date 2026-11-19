"""
백테스트 실행 (LLM · 키움 호출 0). python run.py backtest 또는 python src/backtest.py

1. 검증: 그때 기준값(git 기록의 params.json)으로 재현 → 실제 계좌 · 체결과 비교
2. 기준: 지금 기준값으로 전 기간 재현
3. 탐색: config/tuning.json 의 키를 하나씩 범위(min~max, step 간격) 안에서 바꿔 재현, 좋은 것끼리 둘씩 조합
4. walk-forward: 앞 구간(학습)에서 고르고 뒤 구간(확인)에서 확인. 둘 다 기준보다 나아야 '지지'
5. 결과: work/backtest/{날짜}.json · .md → 16:00 조정(get_tuning_context 의 backtest)이 참고 자료로 읽음 (자동 반영 안 함)
6. 대시보드 Simulator: SIM_KEYS 네 개의 모든 조합을 미리 계산 → docs/data/simulator.json (브라우저는 고르기만, 토큰 0)

점수 = KODEX 200 대비 초과수익률. 단 MDD 가 기준보다 1%p 넘게 나빠지거나 거래가 대회 최소(8회) 미만이면 제외.
"""
import argparse
import copy
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import replay as R  # noqa: E402

MDD_SLACK = 0.01
UNTESTABLE = {"screen": "후보 선별 값이라 재현 불가 (그때 후보에 없던 종목은 판단 기록이 없음)"}
LIMITS = [
    "확신도는 그때 값 고정 — 프롬프트 · 모델 · 교훈 변경 효과는 검증 못 함",
    "후보에 없던 종목은 판단 기록이 없음 (후보 선별 기준값은 탐색 안 함)",
    "실전에 없던 보유(실제론 관망)는 매도 확신도가 없어 손절 · 익절 · 보유일로만 팖",
    "판단 시점 가격은 git 기록의 회차별 장중 가격, 없으면 일봉 (고+저+종)/3. 회차 사이 손절 · 익절은 일봉 저가 · 고가로 근사 (둘 다 닿으면 손절 먼저)",
    "에이전트가 규칙과 다르게 한 판단(기준 이상인데 관망 등)은 규칙대로 처리",
]


# 11-19 S.Lee: 대시보드 Simulator 에서 바꿔 볼 기준값 (수익률을 가장 크게 움직인 넷). 값 = tuning.json 범위 + 지금 값
SIM_KEYS = ["decision.buy_min_confidence", "decision.stop_loss_pct", "decision.take_profit_pct", "regime_cap.하락"]


def set_value(p, key, v):
    sec, leaf = key.split(".", 1)
    p[sec][leaf] = v


def grid(spec):
    vals, v = [], spec["min"]
    while v <= spec["max"] + 1e-9:
        vals.append(round(v, 4))
        v += spec["step"]
    return vals


def score(m, base, full=None):
    """초과수익률. 제약(전 기간 거래가 대회 최소 미만 · MDD 가 기준보다 MDD_SLACK 넘게 나쁨)을 어기면 None."""
    if m["excess"] is None or (full and full["trades"] < C.params()["competition"]["min_trades"]):
        return None
    if m["mdd"] < base["mdd"] - MDD_SLACK:
        return None
    return m["excess"]


def run_variant(data, p, changes, split):
    q = copy.deepcopy(p)
    for k, v in changes.items():
        set_value(q, k, v)
    res = R.simulate(data, q)
    days = data["days"]
    return {"changes": changes,
            "full": R.metrics(res, data),
            "train": R.metrics(res, data, end=days[split - 1]),
            "test": R.metrics(res, data, start=days[split]),
            "blocks": dict(list(res["blocks"].items())[:5])}


def discretion(data, p):
    """에이전트가 규칙과 다르게 한 판단: 그때 기준값으로 매수 구간인데 관망 (매매 회차). 건수 · 예시."""
    out = []
    for x in data["runs"]:
        if x["mode"] != "decide":
            continue
        q = data["snap"].params_at(x["time"])
        floor = round(q["decision"]["buy_min_confidence"] - (q["decision"].get("probe") or {}).get("band", 0), 3)
        for it in x["items"].values():
            if it["src"] == "decision" and it["side"] == "매수" and it["adj_known"] and it["conf"] is not None and it["conf"] >= floor - 1e-9:
                out.append(f"{it['time']} {it['name']} 보정 후 {it['conf']:.2f} ≥ 소액 매수 하한 {floor:.2f} 인데 관망")
    return {"count": len(out), "examples": out[:8]}


def simulator_grid(data, p, rl, report):
    """SIM_KEYS 의 모든 조합을 전 기간 재현 → 대시보드용 숫자. 수익률 · 낙폭 · 승률은 0.01% 단위 정수 (파일 크기)."""
    bp = lambda v: None if v is None else round(v * 10000)
    keys = []
    for k in SIM_KEYS:
        sec, leaf = k.split(".", 1)
        cur = p[sec][leaf]
        vals = sorted(set(grid(rl["keys"][k])) | {round(cur, 4)})
        keys.append({"key": k, "label": rl["keys"][k]["label"], "values": vals, "current": round(cur, 4)})
    init = C.risk_limits()["initial_capital"]
    ret, mdd, trades, win, curve = [], [], [], [], []
    for combo in itertools.product(*[k["values"] for k in keys]):
        q = copy.deepcopy(p)
        for k, v in zip(keys, combo):
            set_value(q, k["key"], v)
        res = R.simulate(data, q)
        m = R.metrics(res, data)
        ret.append(bp(m["return"]))
        mdd.append(bp(m["mdd"]))
        trades.append(m["trades"])
        win.append(bp(m["win_rate"]))
        curve.append([bp(e["total"] / init - 1) for e in res["equity"]])
    bench, b0 = R.bench_curve(data)
    actual = {e["date"]: e["total"] for e in data["equity"]}
    days = data["days"]
    return {
        "asof": report["asof"], "days": days, "init": init,
        "keys": keys, "order": "keys 순서대로, 마지막 키가 가장 빨리 바뀜 (index = 혼합 진법)",
        "bench": [bp(bench[d] / b0 - 1) if bench.get(d) and b0 else None for d in days],
        "actual": [bp(actual[d] / init - 1) if actual.get(d) else None for d in days],
        "ret": ret, "mdd": mdd, "trades": trades, "win": win, "curve": curve,
        "regime_days": report["regime_days"],
        "validation": {"final_gap": report["validation"]["final_gap"], "recall": report["validation"]["recall"]},
        "limits": report["limits"][:3],
    }


def main(journal=None, ref="HEAD", fetch=True, out_dir=None, quick=False, sim_out=C.DOCS_DATA):
    p, rl = C.params(), C.read_json(os.path.join(C.CONFIG, "tuning.json"))
    data = R.load_data(journal, ref, fetch)
    days = data["days"]
    split = max(1, round(len(days) * 0.6))

    hist = R.simulate(data, historical=True)
    validation = {**R.validate(data, hist), "metrics": R.metrics(hist, data), "coverage": R.coverage(data),
                  "agent_discretion": discretion(data, p)}
    validation.pop("days")
    validation["equity"] = [{"date": e["date"], "sim": e["total"], "actual": a.get("total")}
                            for e in hist["equity"] for a in [next((x for x in data["equity"] if x["date"] == e["date"]), {})]]

    base = run_variant(data, p, {}, split)
    sweep, skipped = [], {}
    for key, spec in rl["keys"].items():
        sec = key.split(".")[0]
        if sec in UNTESTABLE:
            skipped[key] = UNTESTABLE[sec]
            continue
        cur = p[sec][key.split(".", 1)[1]]
        for v in grid(spec):
            if abs(v - cur) < 1e-9:
                continue
            x = run_variant(data, p, {key: v}, split)
            x.update(key=key, label=spec["label"], current=cur, value=v,
                     within_step=abs(v - cur) <= spec["step"] + 1e-9,
                     score_train=score(x["train"], base["train"], x["full"]), score_test=score(x["test"], base["test"], x["full"]))
            sweep.append(x)
    bt, bs = score(base["train"], base["train"]), score(base["test"], base["test"])

    # 키마다 학습 구간 최고 값 → 확인 구간
    best_by_key = {}
    for x in sweep:
        if x["score_train"] is None:
            continue
        b = best_by_key.get(x["key"])
        if not b or x["score_train"] > b["score_train"]:
            best_by_key[x["key"]] = x
    singles = sorted(best_by_key.values(), key=lambda x: -x["score_train"])
    pairs = []
    if not quick:
        top = [x for x in singles if x["score_train"] > bt][:4]
        for a, b in itertools.combinations(top, 2):
            y = run_variant(data, p, {a["key"]: a["value"], b["key"]: b["value"]}, split)
            y.update(score_train=score(y["train"], base["train"], y["full"]), score_test=score(y["test"], base["test"], y["full"]))
            pairs.append(y)
        pairs.sort(key=lambda y: -(y["score_train"] if y["score_train"] is not None else -9))

    def verdict(x):
        if x["score_train"] is None or x["score_test"] is None:
            return "제외 (MDD · 거래 수 제약)"
        if x["score_train"] > bt + 0.001 and x["score_test"] > bs + 0.001:
            return "지지 (학습 · 확인 둘 다 기준보다 나음)"
        tr, te = x["score_train"] - bt, x["score_test"] - bs
        if tr > 0.001:
            return "과적합 의심 (학습만 나음)"
        if te > 0.001 and tr >= -0.001:
            return "근거 약함 (확인 구간만 나음)"
        if abs(tr) <= 0.001 and abs(te) <= 0.001:
            return "차이 없음"
        return "기준보다 나쁨"

    suggestions = []
    for x in singles:   # 오늘 조정이 쓸 수 있는 폭(1 step) 안의 값만 따로
        near = [y for y in sweep if y["key"] == x["key"] and y["within_step"] and y["score_train"] is not None]
        nb = max(near, key=lambda y: y["score_train"]) if near else None
        suggestions.append({
            "key": x["key"], "label": x["label"], "current": x["current"],
            "best": x["value"], "best_verdict": verdict(x),
            "best_train_excess_delta": round(x["score_train"] - bt, 4), "best_test_excess_delta": round(x["score_test"] - bs, 4) if x["score_test"] is not None else None,
            "one_step": nb and {"value": nb["value"], "verdict": verdict(nb),
                                "train_excess_delta": round(nb["score_train"] - bt, 4),
                                "test_excess_delta": round(nb["score_test"] - bs, 4) if nb["score_test"] is not None else None},
        })
    regimes = {}
    for x in data["runs"]:
        if x["regime"]:
            regimes.setdefault(x["time"][:10], x["regime"])   # 그날 첫 국면
    dist = {k: list(regimes.values()).count(k) for k in sorted(set(regimes.values()))}
    limits = list(LIMITS)
    top = max(dist, key=dist.get) if dist else None
    if top and dist[top] >= 0.7 * len(regimes):   # 11-19: 하락 10 · 횡보 4 → 하락 상한을 낮출수록(현금) 이기는 건 시장 방향 때문
        limits.insert(0, f"{len(regimes)}일 중 {dist[top]}일이 {top} 국면 — {top} 국면 비중 상한 결과는 이 기간 시장 방향을 그대로 반영 (방향이 바뀌면 반대일 수 있음)")
    report = {
        "asof": C.stamp(), "regime_days": dist, "days": days, "split": {"train": [days[0], days[split - 1]], "test": [days[split], days[-1]]},
        "score_note": f"점수 = KODEX 200 대비 초과수익률. MDD 가 기준보다 {MDD_SLACK:.0%}p 넘게 나쁘면 제외. 학습 · 확인 둘 다 나아야 '지지'",
        "validation": validation,
        "baseline": {"full": base["full"], "train": base["train"], "test": base["test"], "blocks": base["blocks"]},
        "suggestions": suggestions,
        "pairs": [{"changes": y["changes"], "verdict": verdict(y), "full": y["full"],
                   "train_excess_delta": round(y["score_train"] - bt, 4) if y["score_train"] is not None else None,
                   "test_excess_delta": round(y["score_test"] - bs, 4) if y["score_test"] is not None else None} for y in pairs],
        "sweep": [{"key": x["key"], "value": x["value"], "full": x["full"], "train_excess": x["train"]["excess"],
                   "test_excess": x["test"]["excess"]} for x in sweep],
        "skipped": skipped,
        "limits": limits,
        "usage": {"claude_calls": 0, "kiwoom_calls": 0},
    }
    out_dir = out_dir or R.BT
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{days[-1]}.json")
    C.write_json(path, report)
    with open(path[:-5] + ".md", "w", encoding="utf-8") as f:
        f.write(markdown(report))
    C.write_json(os.path.join(out_dir, "latest.json"), {"path": os.path.basename(path), **summary(report)})
    if sim_out:
        sim = simulator_grid(data, p, rl, report)
        with open(os.path.join(sim_out, "simulator.json"), "w", encoding="utf-8") as f:   # 한 줄로 (대시보드가 받는 크기)
            json.dump(sim, f, ensure_ascii=False, separators=(",", ":"))
    return path, report


def _pct(v):
    return "-" if v is None else f"{v:+.2%}"


def summary(rep):
    """조정(tune)에 넘길 요약. 지지된 것만 앞에."""
    v = rep["validation"]
    order = ("지지", "근거 약함", "과적합", "차이 없음", "기준보다", "제외")

    def rank(s):
        o = s["one_step"] or {}
        vd = o.get("verdict", "제외")
        return (next((i for i, w in enumerate(order) if vd.startswith(w)), len(order)), -(o.get("train_excess_delta") or -9))
    sug = sorted(rep["suggestions"], key=rank)
    return {
        "asof": rep["asof"], "split": rep["split"],
        "validation": {"final_gap": v["final_gap"], "mean_abs_gap": v["mean_abs_gap"], "fill_recall": v["recall"],
                       "fill_precision": v["precision"], "agent_discretion": v["agent_discretion"]["count"]},
        "baseline": rep["baseline"]["full"],
        "one_step": [{"key": s["key"], "label": s["label"], "current": s["current"], **(s["one_step"] or {})} for s in sug if s["one_step"]],
        "pairs": rep["pairs"][:3],
        "note": "참고 자료 (자동 반영 안 함). " + rep["score_note"] + ". 한계: " + " / ".join(rep["limits"][:3]),
    }


def markdown(rep):
    v, b = rep["validation"], rep["baseline"]
    L = [f"# 백테스트 {rep['days'][-1]}", "",
         f"- 기간 {rep['days'][0]} ~ {rep['days'][-1]} ({len(rep['days'])}일) · 학습 {rep['split']['train'][0]}~{rep['split']['train'][1]} · 확인 {rep['split']['test'][0]}~{rep['split']['test'][1]}",
         f"- 국면별 일수 " + " · ".join(f"{k} {v}" for k, v in rep["regime_days"].items()),
         f"- Claude 호출 0 · 키움 호출 0. {rep['score_note']}", "",
         "## 검증 (그때 기준값으로 재현 vs 실제)", "",
         f"- 최종 자산 차이 {_pct(v['final_gap'])} · 날짜별 평균 차이 {v['mean_abs_gap']:.2%}",
         f"- 체결 (날짜·종목·방향) 일치: 실제 {v['fills_real']} · 재현 {v['fills_sim']} · 일치 {v['fills_matched']} (재현율 {v['recall']:.0%}, 정밀도 {v['precision']:.0%})",
         f"- 판단 시점 장중 가격 확보 {v['coverage']['ratio']:.0%} (나머지 일봉 근사)",
         f"- 에이전트가 규칙과 다르게 관망한 판단 {v['agent_discretion']['count']}건", "",
         "| 날짜 | 재현 | 실제 |", "|---|---:|---:|"]
    L += [f"| {e['date']} | {e['sim']:,} | {e['actual']:,} |" if e["actual"] else f"| {e['date']} | {e['sim']:,} | - |" for e in v["equity"]]
    f = b["full"]
    L += ["", "## 지금 기준값으로 전 기간", "",
          f"- 수익률 {_pct(f['return'])} · KODEX 200 {_pct(f['bench'])} · 초과 {_pct(f['excess'])} · MDD {_pct(f['mdd'])} · 거래 {f['trades']} · 승률 {f['win_rate'] if f['win_rate'] is not None else '-'} · 회전율 {f['turnover']}",
          f"- 학습 구간 초과 {_pct(b['train']['excess'])} · 확인 구간 초과 {_pct(b['test']['excess'])}", "",
          "## 키별 (오늘 조정 가능한 1 step 안 최선 / 범위 전체 최선)", "",
          "| 기준값 | 지금 | 1 step | 학습 Δ | 확인 Δ | 판정 | 범위 최선 | 판정 |", "|---|---:|---:|---:|---:|---|---:|---|"]
    for s in rep["suggestions"]:
        o = s["one_step"] or {}
        L.append(f"| {s['label']} | {s['current']} | {o.get('value', '-')} | {_pct(o.get('train_excess_delta'))} | {_pct(o.get('test_excess_delta'))} | {o.get('verdict', '-')} | {s['best']} | {s['best_verdict']} |")
    if rep["pairs"]:
        L += ["", "## 두 개 조합", "", "| 조합 | 학습 Δ | 확인 Δ | 판정 |", "|---|---:|---:|---|"]
        L += [f"| {', '.join(f'{k}={v}' for k, v in y['changes'].items())} | {_pct(y['train_excess_delta'])} | {_pct(y['test_excess_delta'])} | {y['verdict']} |" for y in rep["pairs"]]
    L += ["", "## 탐색 안 함", ""] + [f"- {k}: {why}" for k, why in rep["skipped"].items()]
    L += ["", "## 한계", ""] + [f"- {x}" for x in rep["limits"]]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", help="journal.jsonl 경로 (기본 work/journal.jsonl)")
    ap.add_argument("--ref", default="HEAD", help="git 기록을 읽을 브랜치 (기본 HEAD)")
    ap.add_argument("--no-fetch", action="store_true", help="일봉을 새로 받지 않고 캐시만")
    ap.add_argument("--out", help="결과 폴더 (기본 work/backtest)")
    ap.add_argument("--quick", action="store_true", help="두 개 조합 생략")
    ap.add_argument("--quiet", action="store_true", help="요약 한 줄만 출력 (run.py tune)")
    ap.add_argument("--sim-out", default=C.DOCS_DATA, help="대시보드 Simulator 파일 폴더 (기본 docs/data)")
    ap.add_argument("--no-sim", action="store_true", help="Simulator 조합 계산 생략")
    a = ap.parse_args()
    path, rep = main(a.journal, a.ref, not a.no_fetch, a.out, a.quick, None if a.no_sim else a.sim_out)
    if a.quiet:
        v, f = rep["validation"], rep["baseline"]["full"]
        print(f"백테스트 {rep['days'][-1]}: 지금 기준값 초과 {_pct(f['excess'])} · MDD {_pct(f['mdd'])} · 검증 자산 차이 {_pct(v['final_gap'])} · 체결 일치 {v['recall']:.0%} → {path}")
    else:
        print(markdown(rep))
        print(f"→ {path}")
