# Shioaji-MicroTX-Engine

[![CI](https://github.com/jam/Shioaji-MicroTX-Engine/actions/workflows/ci.yml/badge.svg)](https://github.com/jam/Shioaji-MicroTX-Engine/actions/workflows/ci.yml)

> 台指期（微台 TMF / 小台 MXF / 大台 TXF）**當沖自動條件單引擎**
> 基於永豐金證券 [Shioaji API](https://sinotrade.github.io/zh/)，以 Python 實作。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Mode: Simulation by default](https://img.shields.io/badge/mode-simulation%20by%20default-brightgreen)]()

---

## 為什麼有這個專案

Shioaji **原生沒有條件單 API**。官方文件提供的 `TouchOrder` 只有 20 行範例，
用 `price == touch_price` 做精確相等比對——**跳空時永遠不會觸發**，也沒有出場、
沒有風控、沒有執行緒安全保護，無法直接上線。

本專案把那 20 行範例補成一套可長期運行的當沖引擎：

| 官方範例的問題 | 本專案的解法 |
|---|---|
| `price == trigger` 精確比對，跳空失效 | **穿越判定**：多單 `price >= trigger`、空單 `price <= trigger` |
| 未過濾試撮價 `simtrade` | 行情層第一道過濾，杜絕盤前假價格誤觸發 |
| `flag` 布林旗標非執行緒安全 | `threading.Lock` + 冪等狀態機 |
| callback 內直接 `place_order` 阻塞行情執行緒 | callback 只推事件進佇列，worker thread 負責下單 |
| 無出場、無風控、無收盤處理 | 停利停損、單日停損停機、部位上限、13:40 強制平倉 |
| 假設「委託回報先於成交回報」 | 狀態機容忍成交回報先到（交易所實際行為） |

---

## 核心功能

### 策略一：Scalp 觸價單

指定**方向、觸發價、停利點數、停損點數**，引擎自動完成整個交易生命週期。

```
方向 = 做多
觸發價 = 23150      ← 價格「觸及」才進場，不預掛限價單
停利 = 50 點        ← 成交價 +50 點自動平倉
停損 = 30 點        ← 成交價 -30 點自動平倉
```

> **為什麼不直接掛限價單？**
> 若現價 23100 就掛 23150 的買單，交易所會**立刻以 23100 成交**——
> 你想在突破時追多，結果變成在低點就買進，條件完全沒被驗證。
> 本引擎改為「監控行情 → 價格真正觸及 23150 → 才送出委託」。

### 策略二：OCO 括號單

同時武裝上下兩個觸發條件，**任一成交即自動撤銷另一邊**（One-Cancels-the-Other），
適合區間突破或不預判方向的場景。

### 🚨 立即平倉（Kill Switch）

突發事件時的一鍵止損。引擎在 Mac Mini 上無頭常駐，另開終端機（或 SSH 進去）即可觸發：

```bash
microtx panic      # 刪單 → 平倉 → 引擎停機，需人工重啟
microtx flatten    # 刪單 → 平倉 → 引擎繼續待命，可重新武裝策略
```

三個關鍵設計，都是為了「**引擎自己出問題時這個開關仍然有效**」：

1. **部位向券商重新查詢**，不信任引擎內部狀態 —— 狀態機卡死或不同步時照樣平得掉
2. **先刪單、再平倉** —— 順序反了的話，平倉後殘留的進場單成交會讓你從空手變成反向持倉
3. **繞過 RiskManager** —— 風控的「單日虧損停機」「交易次數上限」在緊急時會擋下救命的平倉單，
   「因為虧太多所以不准你停損」是致命反模式

實作上，CLI 透過 PID 檔送 `SIGUSR1` / `SIGUSR2`；訊號處理器只設一個 `Event`，
真正的平倉由常駐的 `EmergencyWorker` 執行緒完成。同樣的 `EmergencyCloser.execute()`
也是 13:40 強制平倉、單日停損停機、未預期例外的共用出口 —— 入口多個，核心邏輯只有一份。

詳細流程與 15 項邊界情境見 [`docs/specs/06-emergency-close.md`](docs/specs/06-emergency-close.md)。

### 風控（全套）

- 單日最大虧損達標 → 引擎進入 `HALTED`，只准平倉不准開新倉
- 同時最大持倉口數上限
- 單日最大交易次數上限（防程式失控連續下單）
- 下單節流（cooldown），防重複委託
- **13:40 強制平倉**（日盤 13:45 收盤，預留 5 分鐘滑價餘裕）
- 委託價格自動檢查是否落在 `limit_up` / `limit_down` 之間
- 每 60 秒比對券商實際部位與引擎內部狀態，不一致即告警

---

## 架構

```
                          ┌─────────────────────────────┐
                          │        Shioaji API          │
                          │   (模擬 / 正式，由設定切換)   │
                          └──────┬───────────────┬──────┘
                     行情 Tick   │               │  委託/成交回報
                                 ▼               ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                    broker/ 券商閘道層                          │
   │  BrokerGateway (ABC) ── ShioajiGateway ── PaperGateway        │
   │  · 隔離 SDK，策略層零依賴  · PaperGateway 免帳號即可 Demo/測試   │
   └──────┬──────────────────────────────────────────┬────────────┘
          │ 正規化 Tick                                │ 委託指令
          ▼                                           │
   ┌─────────────────────────┐                        │
   │   market/ 行情層         │                        │
   │  · simtrade 試撮過濾     │                        │
   │  · Tick 正規化           │                        │
   │  · 事件佇列（不阻塞 CB）  │                        │
   └──────────┬──────────────┘                        │
              │ TickEvent                             │
              ▼                                       │
   ┌─────────────────────────┐                        │
   │  strategies/ 策略層      │                        │
   │  · ScalpStrategy         │                        │
   │  · OcoStrategy           │                        │
   │  （純函式邏輯，易測試）    │                        │
   └──────────┬──────────────┘                        │
              │ Signal                                │
              ▼                                       │
   ┌──────────────────────────────────────────────────┴────────────┐
   │                      engine/ 引擎層                             │
   │  RiskManager ─→ OrderRouter ─→ PositionTracker ─→ Scheduler    │
   │  風控閘門       下單/改/刪+重試   部位與損益        時段/強平       │
   │       ▲            ▲                                            │
   │       │            │  submit_unchecked()                        │
   │    ⛔繞過 ─────────┘                                            │
   │  🚨 EmergencyCloser ← SIGUSR1/2 ← microtx panic / flatten       │
   │       · 直查券商部位，不依賴內部狀態                              │
   │       · 先刪單再平倉，MKP + IOC                                  │
   │                                                                 │
   │            TradingEngine（主協調器，狀態機）                      │
   └────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                   utils/ 日誌（含機密遮蔽） · notify/ 通知
```

> 完整分層職責、介面契約與執行緒模型見 [`docs/architecture.md`](docs/architecture.md)。

### 資料流：一次完整的 Scalp 交易

```
1. 使用者設定    方向/觸發價/停利點/停損點  →  StrategyState.ARMED
2. Tick 進來     過濾 simtrade → 正規化 → 佇列
3. 穿越判定      close >= trigger（多單）    →  ENTRY_PENDING
4. 風控閘門      部位/次數/時段/單日損益檢查   →  通過才放行
5. 送出委託      MKP + IOC + DayTrade
6. 成交回報      記錄成交均價                 →  IN_POSITION
7. 持倉監控      每 tick 檢查停利/停損價位     →  EXIT_PENDING
8. 平倉成交      更新當日損益、寫日誌          →  CLOSED
   ── 或 13:40 到 → 強制平倉 ──
```

---

## 目錄結構

```
Shioaji-MicroTX-Engine/
├── src/microtx/
│   ├── __init__.py
│   ├── __main__.py              # CLI 進入點（python -m microtx）
│   ├── config.py                # ✅ 設定管理（pydantic-settings，機密遮蔽、雙開關防呆）
│   ├── contracts.py             # ✅ 商品規格表（TXF/MXF/TMF 每點價值換算）
│   ├── enums.py                 # ✅ Direction / StrategyState / EngineState ...
│   │
│   ├── broker/                  # 券商閘道層（隔離 Shioaji SDK）
│   │   ├── base.py              #    BrokerGateway 抽象介面
│   │   ├── shioaji_gateway.py   #    真實 Shioaji 實作
│   │   └── paper_gateway.py     #    純本地模擬，免帳號可跑
│   │
│   ├── market/                  # 行情層
│   │   ├── tick.py              #    正規化 Tick 資料結構
│   │   └── feed.py              #    訂閱管理 + simtrade 過濾 + 事件佇列
│   │
│   ├── strategies/              # 策略層
│   │   ├── base.py              #    Strategy 抽象基底 + 狀態機
│   │   ├── scalp.py             #    觸價進場 + 點數停利停損
│   │   └── oco.py               #    OCO 括號單
│   │
│   ├── engine/                  # 引擎層
│   │   ├── order_router.py      #    下單/改單/刪單 + 重試 + 冪等保護
│   │   ├── position.py          #    部位、均價、當日損益追蹤
│   │   ├── risk.py              #    RiskManager 風控閘門
│   │   ├── scheduler.py         #    交易時段判定 + 強制平倉排程
│   │   ├── emergency.py         # 🚨 EmergencyCloser 立即平倉
│   │   └── engine.py            #    TradingEngine 主協調器
│   │
│   ├── cli/                     # CLI：run / scalp / oco / panic / flatten / demo
│   ├── notify/                  # 通知層（Telegram / Console）
│   └── utils/
│       ├── logger.py            # ✅ 日誌（雙通道 + 機密遮蔽 Filter）
│       ├── pidfile.py           #    PID 檔管理（kill switch 靠它找到行程）
│       └── retry.py             #    指數退避重試裝飾器
│
├── tests/                       # pytest 單元測試
├── docs/
│   ├── architecture.md          # ✅ 架構總綱：分層、介面契約、執行緒模型
│   ├── shioaji_guide.md         # ✅ Shioaji API 在地化速查（開發時只需看這份）
│   └── specs/                   # ✅ 分模組實作任務單（01–08）
├── scripts/                     # 部署腳本（launchd plist、安裝、健康檢查）
├── AGENTS.md                    # ✅ AI 編碼代理的專案規範
├── .env.example                 # ✅ 環境變數範本
├── .gitignore                   # ✅ 排除 .env / *.pfx / *.log / runtime/
├── .pre-commit-config.yaml      # ✅ gitleaks 機密掃描 + ruff + mypy
└── pyproject.toml               # ✅ 依賴、ruff、mypy、pytest 設定
```

✅ = 已完成　其餘為規劃中模組（規格已定稿，見 [`docs/specs/00-roadmap.md`](docs/specs/00-roadmap.md)）

---

## 安裝與設定

### 1. 取得程式碼

```bash
git clone https://github.com/<your-account>/Shioaji-MicroTX-Engine.git
cd Shioaji-MicroTX-Engine
```

### 2. 建立虛擬環境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. 設定環境變數

```bash
cp .env.example .env
```

編輯 `.env`，填入 [永豐 API 金鑰](https://sinotrade.github.io/zh/tutor/prepare/token/)：

```dotenv
SHIOAJI_API_KEY=your_api_key
SHIOAJI_SECRET_KEY=your_secret_key
SIMULATION=true          # 預設模擬模式
SYMBOL=TMFR1             # TMFR1 微台 / MXFR1 小台 / TXFR1 大台
```

### 4. 啟用提交前檢查（建議）

```bash
pre-commit install
```

安裝後每次 `git commit` 會自動執行 **gitleaks 機密掃描**，
金鑰誤入版控會直接被擋下。

### 5. 執行測試

```bash
pytest
```

---

## 使用方式

```bash
# 離線 Demo：無需永豐帳號、無需 .env，重播 tick 完整跑一輪策略
microtx demo

# 模擬模式：做多，23150 觸發，停利 50 點，停損 30 點
microtx run --strategy scalp --direction long --trigger 23150 --tp 50 --sl 30

# OCO：向上 23200 做多 / 向下 23050 做空，先到先做，另一邊自動撤銷
microtx run --strategy oco --upper 23200 --lower 23050 --tp 50 --sl 30

# 查看引擎狀態
microtx status

# 🚨 緊急情況（另開終端機 / SSH）
microtx flatten    # 平掉所有部位，引擎待命
microtx panic      # 平掉所有部位並停機
```

---

## 跨機部署流程

```
MacBook（開發）                GitHub                Mac Mini（7×24 運行）
     │                           │                          │
     │  git push ──────────────► │                          │
     │                           │ ◄────────── git pull ────│
     │                           │                          │
     │                                          .env 獨立設定（不同步）
     │                                          launchd 常駐 + 開機自啟
```

**關鍵原則：`.env` 與憑證檔永遠不進 Git，兩台機器各自維護一份。**

Mac Mini 部署重點：

- 用 `launchd`（非 `cron`）常駐，支援崩潰自動重啟
- 系統設定開啟「自動設定日期與時間」（時間偏差會導致 `Sign data is timeout`）
- 電源設定關閉自動睡眠
- 日誌按日輪替，預設保留 30 天
- `.env` 權限設 `chmod 600`
- **SSH 進去就能 `microtx panic`** —— 人不在電腦前也能止損

---

## 開發規範

| 項目 | 工具 | 指令 |
|---|---|---|
| 格式化 / Lint | ruff | `ruff format . && ruff check --fix .` |
| 型別檢查 | mypy (strict) | `mypy src` |
| 測試 | pytest | `pytest --cov=microtx` |
| 機密掃描 | gitleaks | `pre-commit run --all-files` |

- 全面採用 **Type Hints**，`mypy --strict` 零錯誤
- 註解與 docstring 使用**繁體中文**
- 商業邏輯（穿越判定、損益計算、風控閘門）必須有對應單元測試

---

## 安全設計

| 面向 | 措施 |
|---|---|
| 金鑰管理 | 全部由環境變數注入，程式碼零硬編碼 |
| 版控隔離 | `.gitignore` 排除 `.env`、`*.pfx`、`*.pem`、`*.log`、`logs/`、`data/` |
| 提交攔截 | pre-commit + gitleaks + `detect-private-key` |
| 記憶體保護 | 金鑰以 `SecretStr` 儲存，`repr()` / `str()` 自動遮蔽 |
| 日誌保護 | `SecretMaskingFilter` 攔截疑似金鑰字串，寫檔前遮蔽 |
| 實盤防呆 | 需 `SIMULATION=false` **且** `ALLOW_LIVE_TRADING=true` 兩道開關同時打開 |
| 憑證檢查 | 實盤模式啟動時驗證憑證檔存在，缺少即拒絕啟動 |

---

## ⚠️ 免責聲明

本專案為**個人技術作品集（Portfolio）**，用於展示 Python 工程能力、
API 整合、狀態機設計與軟體工程實務，**不構成任何投資建議**。

- 期貨為**高槓桿商品**，可能在極短時間內造成超過本金的損失。
- 程式交易存在**軟體缺陷、網路延遲、行情中斷、API 變更、憑證失效**等風險，
  自動化並不降低市場風險，反而可能因失控而放大損失。
- 模擬環境的成交為模擬撮合，**與實盤成交結果必然存在差異**，
  模擬獲利不代表實盤可複製。
- 使用本程式進行任何真實交易所產生的**一切盈虧與法律責任，概由使用者自行承擔**，
  作者不負任何擔保或賠償責任。
- 強烈建議：先在模擬環境長期驗證，實盤請從**最小口數（微台 1 口）**開始。

**在你完整讀懂每一行程式碼之前，請勿用於實盤。**

---

## 授權

[MIT License](LICENSE)

## 參考資料

- [Shioaji 官方文件](https://sinotrade.github.io/zh/)
- [本專案在地化速查表](docs/shioaji_guide.md)
- [臺灣期貨交易所 — 臺股期貨契約規格](https://www.taifex.com.tw/)
