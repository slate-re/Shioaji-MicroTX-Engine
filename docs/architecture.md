# 架構總綱（Architecture）

> 本檔定義**分層職責、介面契約、執行緒模型與關鍵設計決策**。
> 所有型別簽章以本檔為準；實作細節見 `docs/specs/`。

---

## 1. 設計原則

| 原則 | 落實方式 |
|---|---|
| **策略層零券商依賴** | 策略只吃 `TickEvent`、吐 `Signal`，完全不 import shioaji，因此可純單元測試 |
| **單向依賴** | `broker ← market ← strategies ← engine`，下層絕不反向 import 上層 |
| **callback 不做事** | Shioaji callback 只做過濾與入佇列，運算與 I/O 交給 worker thread |
| **狀態機明確** | 每個策略實例是一台狀態機，所有轉換可列舉、可測試、可記錄 |
| **失效安全（fail-safe）** | 任何不確定狀態一律偏向「不下新單」；緊急平倉繞過一切非必要檢查 |
| **雙軌驗證** | 引擎內部部位 vs. 券商實際部位定期比對，不一致即告警 |

---

## 2. 分層與職責

```
┌─────────────────────────────────────────────────────────────────────┐
│  cli/                 使用者入口：run / scalp / oco / panic / flatten │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
┌────────────────────────────────▼────────────────────────────────────┐
│  engine/              TradingEngine（主協調器，持有狀態機）             │
│  ├─ RiskManager       風控閘門：部位/次數/損益/時段/節流               │
│  ├─ OrderRouter       下單、改單、刪單、重試、冪等（client_id）         │
│  ├─ PositionTracker   部位、均價、當日已實現/未實現損益                │
│  ├─ Scheduler         時段判定、13:40 強制平倉觸發                     │
│  └─ EmergencyCloser   🚨 緊急平倉（繞過 RiskManager，直查券商部位）     │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ Signal / OrderRequest
┌────────────────────────────────▼────────────────────────────────────┐
│  strategies/          純邏輯層，無 I/O、無執行緒、可 100% 單元測試      │
│  ├─ Strategy(ABC)     on_tick(TickEvent) -> list[Signal]             │
│  ├─ ScalpStrategy     觸價進場 + 點數停利停損                          │
│  └─ OcoStrategy       雙向括號單，一邊觸發即撤銷另一邊                  │
└────────────────────────────────▲────────────────────────────────────┘
                                 │ TickEvent
┌────────────────────────────────┴────────────────────────────────────┐
│  market/              MarketFeed：訂閱管理、simtrade 過濾、正規化、佇列 │
└────────────────────────────────▲────────────────────────────────────┘
                                 │ 原始 tick / 回報
┌────────────────────────────────┴────────────────────────────────────┐
│  broker/              BrokerGateway(ABC)                             │
│  ├─ ShioajiGateway    真實 Shioaji（模擬 / 實盤由 config 決定）        │
│  └─ PaperGateway      純本地撮合，免帳號，供測試與離線 Demo            │
└─────────────────────────────────────────────────────────────────────┘
        ↕ 橫切關注：utils/logger（機密遮蔽）、notify/、exceptions.py
```

---

## 3. 執行緒模型

這是本專案最容易出錯的部分，實作必須嚴格遵守。

```
┌──────────────────┐   put()    ┌────────────┐   get()   ┌──────────────────┐
│ Shioaji 行情執行緒│ ─────────► │ tick_queue │ ────────► │  StrategyWorker  │
│ （SDK 持有）      │            │ (maxsize=N)│           │  （單一執行緒）    │
│  只做：           │            └────────────┘           │  · 跑策略邏輯      │
│  1. simtrade 過濾 │                                     │  · 過風控          │
│  2. 轉 TickEvent  │            ┌────────────┐           │  · 呼叫 OrderRouter│
│  3. put_nowait    │ ─────────► │order_queue │ ────────► └──────────────────┘
└──────────────────┘            └────────────┘
┌──────────────────┐
│ Shioaji 回報執行緒│ ── 同樣只做正規化 + 入佇列 ──►（同上 worker 消費）
└──────────────────┘
┌──────────────────┐
│  Scheduler 執行緒 │ ── 每秒檢查時段 / 13:40 強平 ──► 推 Signal 進佇列
└──────────────────┘
┌──────────────────┐
│  主執行緒          │ ── 訊號處理（SIGUSR1/2/TERM/INT）、生命週期管理
└──────────────────┘
┌──────────────────┐
│ EmergencyWorker  │ ── 🚨 獨立執行緒，被 Event 喚醒後直接對 broker 操作
│  （常駐等待）      │     不經 tick_queue、不經 RiskManager
└──────────────────┘
```

### 規則

1. **Shioaji callback 內禁止任何阻塞操作**，只允許：欄位檢查、建 dataclass、`put_nowait()`。
2. `tick_queue` 有界（建議 `maxsize=1000`）。滿了就**丟棄最舊的 tick 並計數告警**，
   絕不阻塞行情執行緒 —— 當沖情境下舊 tick 沒有價值，斷流才是災難。
3. 策略邏輯集中在**單一** worker thread，因此策略內部**不需要鎖**，大幅降低複雜度。
4. `EmergencyWorker` 是唯一允許繞過 worker 直接呼叫 broker 的路徑，
   它與 `OrderRouter` 共用一把 `threading.RLock` 以避免同時送單。

---

## 4. 介面契約

以下簽章是各層之間的正式合約，實作不得擅自更動。

### 4.1 broker/base.py

```python
@dataclass(frozen=True, slots=True)
class Position:
    """券商回報的實際部位。"""
    code: str                  # 實際月份碼，如 TMFF6
    direction: Direction       # LONG / SHORT
    quantity: int              # 恆為正數，方向由 direction 表示
    average_price: float
    unrealized_pnl: float


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """送往券商的委託請求。"""
    symbol: str                # TMFR1 等設定用代碼
    action: Direction          # 買賣方向
    quantity: int
    price: float | None        # None 表示市價（搭配 MKP）
    price_type: PriceType      # LMT / MKP
    time_in_force: TimeInForce # ROD / IOC / FOK
    intent: OrderIntent        # ENTRY / TAKE_PROFIT / STOP_LOSS / FORCE_CLOSE / EMERGENCY
    client_id: str             # 冪等鍵，UUID4；重送同 client_id 不得重複成交


@dataclass(frozen=True, slots=True)
class OrderAck:
    """下單當下的同步回應（非成交）。"""
    client_id: str
    broker_order_id: str | None
    accepted: bool
    message: str = ""


@dataclass(frozen=True, slots=True)
class OpenOrder:
    """尚未完全成交的委託。"""
    broker_order_id: str
    client_id: str | None
    code: str
    action: Direction
    price: float
    quantity: int
    filled_quantity: int


class BrokerGateway(ABC):
    """券商閘道抽象介面。實作：ShioajiGateway / PaperGateway。"""

    # --- 連線 ---
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    @property
    def is_connected(self) -> bool: ...

    # --- 行情 ---
    def subscribe_ticks(self, symbol: str, callback: Callable[[RawTick], None]) -> None: ...
    def unsubscribe_ticks(self, symbol: str) -> None: ...

    # --- 交易 ---
    def place_order(self, request: OrderRequest) -> OrderAck: ...
    def cancel_order(self, broker_order_id: str) -> bool: ...
    def cancel_all_orders(self) -> int: ...          # 回傳成功刪除筆數
    def list_open_orders(self) -> list[OpenOrder]: ...
    def list_positions(self) -> list[Position]: ...  # 🚨 緊急平倉的唯一真相來源
    def set_order_event_callback(self, callback: Callable[[OrderEvent], None]) -> None: ...

    # --- 商品 ---
    def get_price_limits(self, symbol: str) -> tuple[float, float]: ...  # (limit_down, limit_up)
```

#### OrderEvent 家族

```python
OrderEvent = FillEvent | RejectEvent | AckEvent | CancelEvent
```

四者皆為扁平的 `frozen=True, slots=True` dataclass，**不使用繼承**，
共同欄位 `client_id` / `broker_order_id` / `code` / `timestamp` 名稱與型別一致。
完整欄位簽章見 [`specs/01-foundation.md`](specs/01-foundation.md)。

- `client_id` 皆為 `str | None`：成交回報可能早於委託回報抵達，
  此時尚未建立對映，消費端須以 `broker_order_id` 為主鍵
- `AckEvent.exchange_order_no`（Shioaji `ordno`）由 `OrderRouter` 快取，
  使改單／刪單不必臨時呼叫 `update_status()` —— 緊急平倉時省下的是關鍵秒數
- 消費端一律用 `match` 分派，讓 mypy 做窮盡性檢查

### 4.2 market/tick.py

```python
@dataclass(frozen=True, slots=True)
class TickEvent:
    """正規化後的成交 tick。已保證 simtrade == False。"""
    symbol: str          # 設定用代碼，如 TMFR1
    code: str            # 實際月份碼，如 TMFF6
    timestamp: datetime  # 交易所時間（tz-aware, Asia/Taipei）
    price: float         # 成交價
    volume: int          # 單筆成交量
    total_volume: int
    tick_type: int       # 1 外盤 / 2 內盤 / 0 未定
    received_at: datetime  # 本機收到時間，用於量測延遲
```

### 4.3 strategies/base.py

```python
@dataclass(frozen=True, slots=True)
class Signal:
    """策略輸出的交易意圖。策略不下單，只表達意圖。"""
    intent: OrderIntent
    action: Direction
    quantity: int
    reason: str                 # 人類可讀原因，會寫入日誌與稽核
    limit_price: float | None = None


class Strategy(ABC):
    """策略抽象基底。純函式邏輯：無 I/O、無執行緒、無 sleep。"""

    state: StrategyState

    def on_tick(self, tick: TickEvent) -> list[Signal]: ...
    def on_fill(self, fill: FillEvent) -> list[Signal]: ...
    def on_reject(self, reject: RejectEvent) -> list[Signal]: ...
    def force_close(self, reason: str) -> list[Signal]: ...
    def describe(self) -> str: ...   # 供日誌與 CLI 顯示
```

### 4.4 engine/risk.py

```python
@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str          # 被拒時必須說明原因，會寫入日誌

class RiskManager:
    def check(self, request: OrderRequest, context: RiskContext) -> RiskDecision: ...
    def on_fill(self, fill: FillEvent) -> None: ...
    def should_halt(self) -> bool: ...
    def reset_daily(self) -> None: ...
```

---

## 5. 🚨 緊急平倉（Emergency Close）

當沖引擎最重要的安全裝置。設計目標：**在引擎本身出問題時仍然可用。**

### 5.1 兩種語意

| 指令 | 列舉 | 行為 | 使用時機 |
|---|---|---|---|
| `microtx flatten` | `CloseMode.FLATTEN` | 刪單 → 平倉 → 策略解除武裝，**引擎繼續運行** | 想離場觀望，稍後可能重新進場 |
| `microtx panic` | `CloseMode.PANIC` | 刪單 → 平倉 → 引擎進入 `HALTED`，**需人工重啟** | 突發事件、程式行為異常、要立刻全面停止 |

### 5.2 觸發管道

```
        ┌────────────────────────────────────────────┐
        │  Mac Mini：microtxd 常駐行程                 │
        │  啟動時寫入 runtime/microtx.pid              │
        └──────────────────▲─────────────────────────┘
                           │ os.kill(pid, SIGUSR1 / SIGUSR2)
        ┌──────────────────┴─────────────────────────┐
        │  另一個 shell（可經 SSH）：                   │
        │    microtx panic     → SIGUSR1              │
        │    microtx flatten   → SIGUSR2              │
        └────────────────────────────────────────────┘
```

- PID 檔：`runtime/microtx.pid`（已加入 `.gitignore`）
- CLI 送訊號前必須**檢查 PID 存活**（`os.kill(pid, 0)`），
  陳舊 PID 檔要明確報錯「引擎未運行」，而不是靜默失敗
- 訊號處理器**只做一件事**：`mode_holder.set(mode); panic_event.set()`。
  真正的平倉工作在 `EmergencyWorker` 執行緒完成（Python 訊號處理器不可做重工作）
- 未來若加 Telegram / HTTP 入口，一律呼叫同一個 `EmergencyCloser.execute()`，
  入口是薄殼，核心邏輯只有一份

### 5.3 執行流程

```
 觸發
  │
  ├─[0] 重入檢查：若已在平倉中 → 記錄「重複觸發，忽略」並返回（冪等）
  │
  ├─[1] 引擎狀態立即設為 HALTED（先鎖門，避免平倉期間又有新單送出）
  │
  ├─[2] cancel_all_orders()
  │      ⚠️ 必須先刪單再平倉。否則平倉後殘留的進場單成交，
  │         會讓你從「空手」變成「反向持倉」——比原本更危險。
  │
  ├─[3] list_positions()  ← 🚨 向券商重新查詢，不信任引擎內部狀態
  │      · 同時與 PositionTracker 比對，不一致 → WARNING 記錄（暴露同步 bug）
  │      · 若無部位 → 跳至 [6]
  │
  ├─[4] 對每個部位送反向平倉單
  │      price_type = MKP（範圍市價，非 MKT：避免極端滑價）
  │      time_in_force = IOC（不留單）
  │      octype = Cover（平倉）
  │      intent = OrderIntent.EMERGENCY
  │      ⛔ 不經過 RiskManager.check()
  │         理由：風控的「單日交易次數上限」「單日虧損停機」等規則，
  │               在緊急情境下會擋下救命的平倉單。這是致命反模式。
  │
  ├─[5] 收斂輪詢：每 500ms 重新 list_positions()，最多重試 N 次
  │      · 仍有殘餘 → 重送剩餘口數（處理部分成交）
  │      · 達重試上限仍未平完 → CRITICAL 日誌 + 通知 + 標記 succeeded=False
  │        ⛔ 絕不無限重試（漲跌停鎖死時會變成無窮迴圈）
  │
  ├─[6] 產出 CloseReport 寫入稽核日誌與通知
  │
  └─[7] PANIC → 維持 HALTED，等待人工重啟
         FLATTEN → 策略全部 CANCELLED，引擎回到 RUNNING 待命
```

### 5.4 資料結構

```python
class CloseMode(str, Enum):
    FLATTEN = "FLATTEN"   # 平倉後待命
    PANIC   = "PANIC"     # 平倉後停機

@dataclass(frozen=True, slots=True)
class CloseReport:
    mode: CloseMode
    trigger_source: str            # "SIGUSR1" / "scheduler" / "risk_halt" / "telegram"
    triggered_at: datetime
    cancelled_orders: int
    positions_before: tuple[Position, ...]
    orders_sent: tuple[OrderAck, ...]
    residual_positions: tuple[Position, ...]   # 空 tuple = 完全平倉成功
    succeeded: bool
    elapsed_sec: float
    notes: tuple[str, ...] = ()    # 例如「引擎部位與券商不一致」

class EmergencyCloser:
    def execute(self, mode: CloseMode, source: str) -> CloseReport: ...
```

> **`CloseReport` 是回傳值，不是例外的酬載。**
> `execute()` 在任何情況下都不得向外拋例外（kill switch 自己壞掉是最糟的情況），
> 因此失敗一律反映在 `succeeded=False` 與 `residual_positions`。
> `EmergencyCloseError` 只用於 `execute()` 的內部流程控制與 CLI 端回報，
> 且**不持有** `CloseReport` —— 這讓 `exceptions.py` 得以維持零專案內部依賴。

### 5.5 邊界情境（實作必須明確處理，且每項都要有測試）

| 情境 | 期望行為 |
|---|---|
| 非交易時段觸發 | 無法送單。記錄 WARNING，設 `pending_close`，**下次開盤第一時間自動執行** |
| 券商連線已斷 | 先嘗試重連（最多 3 次）；失敗則 CRITICAL 告警並明確告知「請手動至下單軟體平倉」 |
| 漲跌停鎖死無法成交 | 重試至上限後停止，`succeeded=False`，持續高頻告警直到人工介入 |
| 平倉單部分成交 | 依殘餘口數重送，不重送已成交部分 |
| 平倉期間又收到觸價訊號 | 引擎已 `HALTED`，訊號直接丟棄並記錄 |
| 連續按兩次 panic | 重入鎖擋下，第二次記錄「已在執行中」 |
| 多商品同時持倉 | 逐一平倉；任一失敗不影響其他商品繼續平 |
| 平倉單被券商拒絕 | 記錄拒絕原因，計入重試次數，不視為成功 |

### 5.6 其他自動觸發來源

`EmergencyCloser` 除了人工訊號，也是這些機制的共用出口：

- `Scheduler` 13:40 到點 → `execute(FLATTEN, source="scheduler")`
- `RiskManager` 單日虧損達標 → `execute(PANIC, source="risk_halt")`
- 收到 `SIGTERM` / `SIGINT`（例如 launchd 停止服務）
  → 若 `FLATTEN_ON_SHUTDOWN=true` 則 `execute(PANIC, source="shutdown")`，
    否則只刪單 + 登出，保留部位

---

## 6. 錯誤處理策略

| 層級 | 策略 |
|---|---|
| 網路暫時失敗 | `utils/retry.py` 指數退避，最多 3 次 |
| 券商連線中斷 | 監聽 Shioaji event callback，自動重連 + 重新訂閱；重連期間拒絕新單 |
| 委託被拒 | 記錄 `op_msg`，通知策略 `on_reject()`，由策略決定重試或放棄 |
| 部位不同步 | 定期（每 60 秒）比對 `list_positions()` 與 `PositionTracker`，不一致告警 |
| 未預期例外 | worker 捕捉 → CRITICAL 日誌 → 觸發 `execute(PANIC, source="unhandled_exception")` |
| 跨日 | 00:00 重置當日計數；Token 過期自動重新登入 |

---

## 7. 實作順序（依賴關係）

```
01 exceptions + broker/base      ← 型別基礎，其他全部依賴
02 broker/paper_gateway          ← 有它才能離線測試後續所有模組
03 market/feed                   ← 依賴 01
04 strategies/{base,scalp,oco}   ← 依賴 03，純邏輯最好測
05 engine/{position,risk,order_router,scheduler}
06 engine/emergency + engine/engine
07 broker/shioaji_gateway        ← 最後接真實 API，前面都可離線驗證
08 cli + 部署腳本
```

> 把 `ShioajiGateway` 排在倒數第二，是刻意的：前 6 步全部可以在沒有永豐帳號的
> 情況下開發與測試完成。這也讓 clone 專案的人（例如面試官）能直接跑 Demo。
