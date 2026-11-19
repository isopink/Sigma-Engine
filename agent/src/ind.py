"""지표 (GS Quant) · 국면 규칙 힌트. 국내(data_prep) · 미국(us_data) 이 같이 씀."""
import gs_quant.timeseries as gts


def indicators(close, digits=0):
    """종가 시리즈 → 숫자 요약. 데이터가 60일 미만이면 None. digits = 가격 소수 자리 (국내 0 = 원 단위 int, 미국 2)."""
    rp = round if digits == 0 else (lambda v: round(v, digits))
    close = close.astype(float)
    if len(close) < 60:
        return None
    ma5 = gts.moving_average(close, gts.Window(5, 0))
    ma20 = gts.moving_average(close, gts.Window(20, 0))
    ma60 = gts.moving_average(close, gts.Window(60, 0))
    rsi = gts.relative_strength_index(close, 14)
    vol = gts.volatility(close, gts.Window(20, 0))          # 연율화 %
    mdd = gts.max_drawdown(close, gts.Window(60, 0))        # 60일 최대 낙폭 (음수)
    c = float(close.iloc[-1])
    return {
        "close": rp(c),
        "ma5": rp(float(ma5.iloc[-1])),
        "ma20": rp(float(ma20.iloc[-1])),
        "ma60": rp(float(ma60.iloc[-1])),
        "ma20_slope5": round(float(ma20.iloc[-1] / ma20.iloc[-6] - 1), 4),
        "ret5": round(c / float(close.iloc[-6]) - 1, 4),
        "ret20": round(c / float(close.iloc[-21]) - 1, 4),
        "rsi14": round(float(rsi.iloc[-1]), 1),
        "vol20": round(float(vol.iloc[-1]) / 100, 3),
        "mdd60": round(float(mdd.iloc[-1]), 3),
    }



def rule_hint(ind):
    """종가·5일선·20일선 배열과 20일선 기울기로 국면 힌트.
    대회가 4주라 60일선 기준은 너무 늦게 반응해서 11-03부터 5·20일선으로 바꿈."""
    if ind["close"] > ind["ma5"] > ind["ma20"] and ind["ma20_slope5"] > 0:
        return "상승"
    if ind["close"] < ind["ma5"] < ind["ma20"] and ind["ma20_slope5"] < 0:
        return "하락"
    return "횡보"
