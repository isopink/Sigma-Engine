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

처음 켜는 순서
1. `.env.us.example` 을 `.env.us` 로 복사하고 KIS 키 · 계좌번호 입력 (`MODE=mock` 이면 가상 계좌로 먼저 돌려 볼 수 있음)
2. `run_us.bat prep` → `run_us.bat decide --dry` 로 브리핑 · 명령 확인
3. `.\setup_tasks_us.ps1` 로 작업 등록 (SigmaUS-tick · monitor · snapshot)
4. 첫 실제 주문 전에 `config/us/risk_limits.json` 의 `enabled` 를 사람이 직접 `true` 로 커밋 (기본 false = 비상 정지)

- 비상 정지: `enabled=false` 커밋 또는 `work_us/STOP` 파일 만들기 → 매도 포함 모든 미국 주문 차단
- 양도세 참고: `run_us.bat tax` → `work_us/tax_{연도}.csv`
- 화면: `python -m http.server 8000 --directory docs` → http://localhost:8000/us.html
