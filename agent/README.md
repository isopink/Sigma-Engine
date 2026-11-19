# agent/

The trading agent. **The agent proposes, the rules decide**: Claude Code reads the market and debates each stock; Python code computes indicators and order sizes; a Claude Code hook blocks any order that breaks a risk limit.

## Daily schedule (weekdays, KST)

| Time | Run | LLM |
|---|---|---|
| 08:30 | `prep` — account + data prep → `work/briefing.json` | no |
| 08:30–15:20 | `monitor` — every 5 min, checks stop / take-profit / max holding days; launches an `exit` run when hit | only when triggered |
| 09:30–15:15, every 15 min | `decide` — intraday re-screen → routing (only changed candidates) → regime ∥ position ∥ research → bull ⇄ bear on one fact sheet → claim check → lead agent decides → executor places orders, every slot since 11-05 (`params.schedule.trade_times` lists all 24); a stock sold today is not bought back the same day | yes |
| 15:45 | `close` — record today's equity, publish to dashboard | no |
| 16:00 | `tune` — agent proposes 0–2 threshold changes; `src/tuning.py` applies only those inside `config/tuning.json` and commits | yes |
| Wed · Fri 15:50 | `review` — weekly review + suggestion | yes |
| weekdays 16:00 | `tune` — scorer → tuner | yes |
| Wed · Fri 17:00 | `report` — weekly report draft (writer) | yes |
| always (from logon) | `sync` — every minute, pulls pushed code, checks it, restarts `monitor` / the balance loop if their code changed | no |

Register them with `setup_tasks.ps1` (Windows Task Scheduler). The agent runs on an always-on PC and is edited from a laptop.

## Layout

```
agent/
├── CLAUDE.md                 # Lead agent instructions (decide / exit / review / tune / report runs)
├── .claude/
│   ├── agents/               # Sub-agents: regime, position, research, bull, bear, executor, scorer, tuner, review, writer
│   ├── hooks/risk_check.py   # PreToolUse hook on mcp__kiwoom__place_order
│   └── settings.json         # Hook registration, tool permissions (no Bash / Edit / Write)
├── .mcp.json                 # kiwoom MCP server
├── config/
│   ├── risk_limits.json      # Fixed limits; enforced by the hook
│   ├── params.json           # Decision thresholds, conviction adjustments, screen settings (auto-tuned daily)
│   └── tuning.json           # What the tuner may change: keys, ranges, step size, cooldown
├── src/
│   ├── data_prep.py          # pykrx + Naver news → GS Quant indicators → candidate screen
│   ├── intraday.py           # Before each decision: Kiwoom rank APIs → re-screen candidates, intraday RSI
│   ├── sizing.py             # Conviction → quantity, stop, take-profit, max holding days
│   ├── risk.py               # Risk rules (shared by the hook and the MCP server)
│   ├── broker.py             # mock account / Kiwoom REST
│   ├── mcp_server.py         # Tools the agent can call
│   ├── logger.py             # work/journal.jsonl → docs/data/*.json
│   ├── monitor.py            # Intraday stop / take-profit watcher
│   ├── tuning.py             # Validates and applies the daily threshold changes
│   └── sync.py               # Pulls pushed code every minute and restarts the loops that changed
├── tests/test_rules.py
├── run.py · run.bat          # Entry point for each run
└── setup_tasks.ps1           # Task Scheduler registration
```

## Setup

```bat
cd agent
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      :: fill KRX_ID / KRX_PW / NAVER_CLIENT_ID / NAVER_CLIENT_SECRET, MODE
.venv\Scripts\python tests\test_rules.py
.venv\Scripts\python run.py prep
.venv\Scripts\python run.py decide
```

- `MODE=mock` trades a virtual account in `work/mock_account.json`. `MODE=live` uses the Kiwoom mock-trading server with keys from `live/.env`.
- Without `KRX_ID` / `KRX_PW`, data prep falls back to a fixed large-cap list without investor flow data.

> Kiwoom App Key / Secret and any `.env` file are never committed (excluded via `.gitignore`).

## 미국 주식 (SIGMA_MARKET=US, us-stocks 11-19)

대회 계좌와 별개인 **실제 돈** 미국 주식 계좌 (한국투자증권 KIS). 코드는 같고 실행할 때 시장만 고름.

| | 국내 (기본) | 미국 |
|---|---|---|
| 진입점 | `run.bat` | `run_us.bat` (`SIGMA_MARKET=US`) |
| 설정 | `config/*.json` | `config/us/*.json` (+ `universe.json`) |
| 키 | `.env` · `live/.env` | `.env.us` (예시 `.env.us.example`) |
| 작업 파일 | `work/` | `work_us/` |
| 시각 | 한국 시간 | 뉴욕 시간 (서머타임 자동) |
| 대시보드 | `docs/index.html` (공개) | `docs/us.html` + `docs/data/us/` (비공개, 로컬) |

메인 설계 끼우기 (매수 · 매도 · 확신도 · 리스크)
- **`src/strategy_us.py` 함수 5개만 채우면 됨**: `confidence` (확신도 보정) · `buy_weight` (확신도 → 비중) · `sell_qty` (판단 매도 수량) · `exit_levels` (손절 · 익절 · 보유일) · `risk_rules` (추가 차단 규칙)
- None 을 돌려주면 기존(국내 복사) 동작. plan_order · 리스크 Hook · 감시가 모두 같은 함수를 불러 계산과 차단이 어긋나지 않음
- 숫자는 `config/us/params.json` · `risk_limits.json` 에 두고 함수에서 `ctx["p"]` · `ctx["r"]` 로 읽음. 에이전트에게 설명할 규칙은 `RULES_TEXT` (브리핑에 붙음)
- 다 채우면 `READY = True`, 두 config 의 `_pending` 메모 삭제

처음 켜는 순서
1. `.env.us.example` 을 `.env.us` 로 복사하고 KIS 키 · 계좌번호 입력 (`MODE=mock` 이면 가상 계좌로 먼저 돌려 볼 수 있음)
2. `.\setup_tasks_us.ps1` 로 작업 등록 (SigmaUS-tick · monitor · snapshot)
3. 메인 설계를 끼우고 `config/us/risk_limits.json` 의 `enabled` 를 사람이 직접 `true` 로 커밋
4. **`run_us.bat check`** → 전부 ✅ 면 그날 밤부터 자동 시작 (❌ 가 있으면 무엇을 고칠지 알려 줌, 주문은 안 냄)

- 비상 정지: `enabled=false` 커밋 또는 `work_us/STOP` 파일 만들기 → 매도 포함 모든 미국 주문 차단
- 양도세 참고: `run_us.bat tax` → `work_us/tax_{연도}.csv`
- 화면: `python -m http.server 8000 --directory docs` → http://localhost:8000/us.html
