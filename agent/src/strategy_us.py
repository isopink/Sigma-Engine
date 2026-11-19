"""
미국 전략 끼우는 곳 (SIGMA_MARKET=US, us-stocks 11-19).

S.Lee 가 정할 메인 설계 로직(매수 · 매도 · 확신도 · 리스크)은 **이 파일 함수만 채우면** 코드 전체에 반영됨.
plan_order(사이징) · 리스크 Hook · 체결 기록 · 감시(손절 · 익절 · 보유일)가 모두 같은 함수를 부르므로
에이전트가 보는 계산과 Hook 이 막는 기준이 어긋나지 않음.

규칙
- 함수가 None 을 돌려주면 기존 동작(국내 규칙 복사 = 임시값) 그대로
- 숫자는 config/us/params.json · risk_limits.json 에 두고 ctx["p"] · ctx["r"] 로 읽는 걸 권장 (조정 · 기록이 쉬움)
- READY = False 인 동안은 미국 주문 전부 차단 (비상 정지와 같음). 다 채우면 True 로 커밋
- 에이전트는 이 파일을 고칠 수 없음 (도구 권한에서 Edit · Write 금지). 사람이 커밋해야 바뀜
- 시작 전 점검: run_us.bat check

ctx 키 (모든 함수 공통)
  p        params (config/us/params.json)          r        risk_limits (config/us/risk_limits.json)
  row      브리핑의 그 종목 행 (close · ma5 · ma20 · rsi14 · rsi14_live · ret20 · rel_strength · vol20 · news …), 없으면 {}
  regime   오늘 국면 (상승 · 횡보 · 하락)           default  기존 동작이 냈을 값 (그대로 쓰거나 고쳐서 돌려줌)
  account  계좌 {total, cash, holdings}  (buy_weight · sell_qty · risk_rules 에만)
"""

READY = False                      # 메인 설계를 끼운 뒤 True
NAME = "임시 (국내 규칙 복사, S.Lee 미정)"
RULES_TEXT = [                     # 브리핑 '오늘 기준값' 끝에 붙는 설명 (에이전트가 읽음). 규칙을 바꾸면 여기도 같이
]


def confidence(code, side, conf, ctx):
    """확신도 보정. conf = 에이전트가 넘긴 보정 전 확신도, ctx['default'] = 기존 보정 결과 (보정 후, 설명).
    돌려줄 값: (보정 후 확신도 0~1, 설명 문자열 또는 None) 또는 None(기존 그대로)."""
    return None


def buy_weight(code, conf, ctx):
    """보정 후 확신도 → 종목 목표 비중 (총자산 대비 0~1). 0 이면 안 삼. ctx['default'] = 기존 비중.
    리스크 한도(종목당 · 현금 · 국면 상한 · 1회 주문)는 이 값과 별개로 계속 적용됨.
    None 이 아니면 '매수 확신도 기준' 검사도 이 값으로 대신함 (0 → 차단)."""
    return None


def sell_qty(code, have, conf, ctx):
    """판단 매도 수량 (보유 have 주 중). 손절 · 익절 · 보유일 규칙 매도는 항상 전량이라 여기 안 옴.
    ctx['default'] = 기존 수량. None 이 아니면 '매도 확신도 기준' 검사도 이 값으로 대신함 (0 → 차단)."""
    return None


def exit_levels(code, ctx):
    """(손절 %, 익절 %, 최대 보유 거래일) — 예: (-0.07, 0.10, 10). 손절 없으면 손절 자리에 None 은 쓰지 말고
    params 의 no_stop 을 씀. ctx['default'] = 기존 값. 매수 체결 때 계산돼 보유 종목에 붙고 감시가 그 값으로 매도."""
    return None


def risk_rules(order, account, price, ctx):
    """추가 리스크 규칙 (Hook 과 plan_order 가 같이 씀). order = {code, side '매수'|'매도', qty, confidence(보정 후)}.
    위반이면 [(규칙 이름, 설명)] 을, 없으면 [] 를 돌려줌. 비상 정지 · 종목 범위 · 누적 손실 정지 · risk_limits 한도는 이미 따로 검사함.
    매도를 막는 규칙은 손절 매도도 막을 수 있으니 order['rule_exit'] 를 보고 판단."""
    return []
