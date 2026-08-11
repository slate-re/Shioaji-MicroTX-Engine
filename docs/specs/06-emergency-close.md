# 任務 06 — 🚨 緊急平倉 + 主協調器

## 目標

實作本專案最重要的安全裝置：**立即平倉**。
設計前提是「**引擎自己可能已經出問題**」，因此這條路徑必須盡可能少依賴引擎內部狀態。

完整設計背景見 `docs/architecture.md` §5，本任務單是實作規格。

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/engine/emergency.py` | 新增 |
| `src/microtx/engine/engine.py` | 新增 |
| `src/microtx/utils/pidfile.py` | 新增 |
| `src/microtx/notify/base.py` | 新增（`Notifier` Protocol） |
| `src/microtx/enums.py` | **擴充**：新增 `NotifyLevel`、`StrategyState.ABORTED` |
| `src/microtx/strategies/base.py` | **擴充**：新增 `abort()`、`_VALID_TRANSITIONS` 加入 `ABORTED` |
| `src/microtx/strategies/{scalp,oco}.py` | **擴充**：`abort()` 的必要覆寫（OCO 需連帶中止兩腿） |
| `tests/test_scalp_strategy.py`、`tests/test_oco_strategy.py` | **擴充**：`abort()` 相關測試 |
| `tests/test_emergency_closer.py` | 新增 |
| `tests/test_engine.py` | 新增 |
| `src/microtx/config.py` | **擴充**（新增下方設定項） |
| `.env.example` | 已含對應項目，對照即可 |

## config.py 需新增

```python
emergency_max_retries: int = Field(default=5, ge=1, le=20)
emergency_retry_interval_sec: float = Field(default=0.5, ge=0.1, le=5.0)
emergency_use_market_order: bool = Field(default=True)
emergency_lock_timeout_sec: float = Field(default=2.0, ge=0.1, le=30.0)
flatten_on_shutdown: bool = Field(default=True)
pid_file: Path = Field(default=Path("runtime/microtx.pid"))
```

---

## 1. emergency.py — EmergencyCloser

```python
class EmergencyCloser:
    def __init__(
        self,
        gateway: BrokerGateway,
        router: OrderRouter,
        tracker: PositionTracker,
        settings: Settings,
        *,
        lock: threading.RLock,                            # 與 OrderRouter 共用
        on_state_change: Callable[[EngineState], None],
        on_cancel_strategies: Callable[[str], None],      # 參數為原因字串，供稽核
        is_tradable: Callable[[], bool],                  # 由 Scheduler.is_tradable 注入
        notifier: Notifier | None = None,
    ) -> None: ...

    def execute(self, mode: CloseMode, source: str) -> CloseReport:
        """執行緊急平倉。可重入呼叫（第二次會被冪等擋下）。"""

    @property
    def is_closing(self) -> bool: ...

    @property
    def pending(self) -> CloseMode | None:
        """非交易時段觸發時暫存的待執行模式，開盤後由引擎消化。"""
```

### 執行步驟（嚴格照順序）

```
[0] 冪等閘門
    with self._reentry_lock:
        if self._is_closing:
            log.warning("緊急平倉已在執行中，忽略重複觸發 source=%s", source)
            return self._last_report or <空報告 succeeded=False>
        self._is_closing = True

[1] 先鎖門
    on_state_change(EngineState.HALTED)
    理由：平倉期間絕不能再有新單送出。先改狀態再動作。

[2] 刪光未成交委託
    cancelled = gateway.cancel_all_orders()
    ⚠️ 必須在平倉之前。否則平倉後殘留的進場單成交 → 從空手變成反向持倉，
       比原本的處境更危險。這條順序是本模組的核心，必須有註解說明。

[3] 向券商查真實部位
    positions = gateway.list_positions()
    notes = tracker.reconcile(positions)
    if notes: log.warning(...)   # 暴露內部狀態同步 bug，但不中斷流程
    if not positions: → 跳至 [5]

[4] 送反向平倉單 + 收斂輪詢
    for attempt in range(1, settings.emergency_max_retries + 1):
        for pos in positions:
            req = OrderRequest(
                symbol=..., action=pos.direction.opposite,
                quantity=pos.quantity, price=None,
                price_type=PriceType.MKP,          # 非 MKT：滑價有上限
                time_in_force=TimeInForce.IOC,     # 不留單
                intent=OrderIntent.EMERGENCY,      # ← 讓風控放行的關鍵
                client_id=new_client_id(),
            )
            ack = router.submit_unchecked(req)     # ⛔ 繞過 RiskManager
        sleep(settings.emergency_retry_interval_sec)
        positions = gateway.list_positions()       # 重新查，不猜
        if not positions: break
    ⚠️ 迴圈上限必須是有限的。漲跌停鎖死時無限重試會變成無窮迴圈 + 委託洗版。

[5] 產出 CloseReport，寫稽核日誌（CRITICAL 等級若未平完），發送通知

[6] 收尾
    on_cancel_strategies(source)    ← 兩種模式都要取消，避免日後解除 HALTED 時舊策略復活
    PANIC   → 維持 HALTED，不自動恢復
    FLATTEN → on_state_change(RUNNING)，引擎待命
    finally: self._is_closing = False
```

---

## 四項相依契約（正式裁決）

### ① `Notifier` —— 用 Protocol，不用 ABC

```python
# src/microtx/notify/base.py
from typing import Protocol

class Notifier(Protocol):
    """通知管道的結構型別。實作者不需繼承，只要方法簽章相符即可。"""

    def notify(self, level: NotifyLevel, title: str, body: str) -> None: ...

# src/microtx/enums.py
class NotifyLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
```

裁決理由：

- **接文字，不接 `CloseReport`。** 若 `notify()` 吃 `CloseReport`，`notify/` 就得
  import `engine/`，方向倒置。由 `EmergencyCloser` 負責把報告格式化成文字。
- **用 `Protocol` 不用 ABC。** 通知是可插拔的邊緣設施，結構型別讓
  測試用一個記錄用的簡單物件即可替代，不必繼承。
- 本任務只定義 Protocol，Telegram 等具體實作留待後續。

> ⚠️ **通知失敗絕不可影響平倉。**
> 每一次 `notifier.notify()` 都必須包在 `try/except Exception` 中，
> 失敗只寫日誌。通知管道掛掉而導致 kill switch 失效，是不可接受的。

### ② 時段判定 —— 注入 `is_tradable`，不得複製 Scheduler 邏輯

```python
is_tradable: Callable[[], bool]     # 由 TradingEngine 傳入 scheduler.is_tradable
```

裁決理由：時段規則若在兩處各寫一份，必然漂移；漂移的後果是
「引擎認為開盤、平倉器認為休市」，於是 panic 靜默失效。單一真相來源。

**休市觸發的正式行為：**

```
1. 立刻 on_state_change(HALTED)          ← 即使不能送單，也要先鎖門
2. 立刻 on_cancel_strategies(reason)     ← 避免開盤瞬間被舊策略觸發
3. self._pending = mode                  ← 記下待執行模式
4. 回傳 CloseReport(succeeded=False, notes=("非交易時段，已排入下次開盤執行",))
5. 發送 WARNING 通知
```

> 使用者按下 panic 卻什麼都沒發生，是最危險的沉默。
> 通知**必須**發出，且明確說明「已排程、尚未執行」。

若 `is_tradable()` 自身拋例外：**視為可交易並繼續平倉**。
寧可送出單被交易所拒絕，也不要因為判斷程式壞掉而不平倉。

### ③ 策略中止 —— 新增終態 `ABORTED`，兩種模式都要中止

先前規格寫「策略皆轉 `CANCELLED`」，**這是錯的**，與任務 04 的狀態機契約衝突：

- `CANCELLED` 在 `enums.py` 的定義是「使用者或風控主動取消，**未進場**」
- `_VALID_TRANSITIONS` 因此不允許 `IN_POSITION → CANCELLED`、`EXIT_PENDING → CANCELLED`

把持倉中的策略標成 `CANCELLED` 會**銷毀稽核資訊** ——
隔天翻日誌時，無法分辨「這個策略從未觸發」與「這個策略帶著部位被強制中止」。

#### 裁決：新增終態 `StrategyState.ABORTED`

```python
# enums.py
ABORTED = "ABORTED"
"""因緊急平倉／強制停機而中止。可能曾經持有部位，部位已由引擎層平掉。"""
```

- 納入 `is_terminal`
- `_VALID_TRANSITIONS` 中，**所有非終態**皆可轉入 `ABORTED`

#### `Strategy.abort()` —— 絕不失敗的中止入口

```python
def abort(self, reason: str) -> None:
    """緊急中止。不回傳任何訊號，且**在任何狀態下都不得拋例外**。

    已是終態時為無操作（no-op）。
    """
```

⚠️ **這個方法不可以拋 `StrategyError`。** 理由是 kill switch 的核心前提：
若 `abort()` 在某個狀態下拋例外，緊急平倉流程就會被策略層的狀態機檢查卡住 ——
安全裝置被它要保護的東西弄壞，是最糟的失敗模式。

`abort()` **不產生任何 Signal**。實際的部位平倉由 `EmergencyCloser` 在券商層完成，
策略只需要停止動作。

#### 為何不採用「持倉策略等平倉回報後轉 CLOSED」

該方案語意最精確，但**違反本模組的核心設計前提**：
它要求引擎的事件迴圈正常運作才能到達終態。
若 `StrategyWorker` 已經卡死，成交回報永遠不會被處理，策略永遠停在 `EXIT_PENDING`。

緊急平倉不能依賴任何「引擎還活著」的假設。`abort()` 是同步、無 I/O、不可失敗的。

#### 兩種模式的差異

| 模式 | 策略終態 | 結束後引擎狀態 |
|---|---|---|
| `FLATTEN` | `ABORTED` | `RUNNING`（待命，可重新武裝新策略） |
| `PANIC` | `ABORTED` | `HALTED`（需人工重啟） |

兩者唯一差別就是最終引擎狀態。**PANIC 也必須中止策略** ——
否則引擎停在 `HALTED` 而策略仍停在 `ARMED`，日後解除 HALTED 時
一批舊條件單會突然全部活過來。

```python
on_cancel_strategies: Callable[[str], None]     # 參數為原因字串；實作內逐一呼叫 strategy.abort()
```

### ④ `emergency_use_market_order=False` 的行為

先前規格新增了此設定卻未定義行為，且他處又固定要求 `MKP` —— 這是規格缺陷，現裁決：

| 設定 | 委託方式 |
|---|---|
| `True`（預設） | `PriceType.MKP` + `TimeInForce.IOC` |
| `False` | `PriceType.LMT` + `TimeInForce.IOC`，**價格取漲跌停價** |

`False` 時的限價取法（用 `gateway.get_price_limits(symbol)`）：

```python
limit_down, limit_up = gateway.get_price_limits(symbol)
price = limit_up if action is Direction.LONG else limit_down
```

即：買回（平空單）掛**漲停價**、賣出（平多單）掛**跌停價**。

裁決理由：對緊急平倉而言，**沒成交遠比滑價嚴重**。
掛在漲跌停價的限價單享有與市價單相當的成交順位，
卻有一個硬性的價格邊界 —— 這是「有上限的市價單」，兩者兼得。

⛔ **絕不可**把 `False` 解讀成「掛在現價或近價的限價單」。
那會讓平倉單在快市中掛著不成交，正是最糟的結果。

**降級規則**：若 `get_price_limits()` 取不到值或拋例外，
**退回 `MKP`** 並寫 WARNING 日誌。缺資料不得成為不平倉的理由。

---

---

## ⑤ 共用鎖不得成為 kill switch 的單點故障（06b 補強）

交付時已正確指出一個殘留前提：

> 「前提是……共享鎖未被另一條故障路徑永久占用。」

這個前提**必須消除**，否則本模組的核心承諾不成立。具體失效路徑：

```
StrategyWorker 送進場單
   → OrderRouter.submit() 取得 RLock
   → 在持鎖狀態下呼叫 gateway.place_order()
   → 網路停滯 / socket 卡住（無逾時或逾時很長）
   → 鎖被長期占用
使用者按下 microtx panic
   → EmergencyWorker 執行 `with self._lock:`
   → 無限期阻塞
   → 🔴 kill switch 失效
```

這正是本模組存在的理由所在的情境：**引擎壞掉時仍要能平倉**。
「另一條路徑卡住」不是假設性問題 —— 網路停滯在券商 API 上是常態。

### 修正 (a)：`OrderRouter` 不得在持鎖狀態下做網路 I/O

沿用任務 02 對 `PaperGateway` 立下的同一條紀律：**鎖內只動狀態，鎖外做 I/O**。

```python
def submit(self, request, ctx):
    with self._lock:
        ...                               # 冪等檢查、風控、登記 in_flight
        if not approved: return ...
    # ← 鎖已釋放
    ack = self._gateway.place_order(request)   # 網路呼叫在鎖外
    with self._lock:
        ...                               # 依結果更新狀態
    return ack
```

`in_flight` 已在第一段鎖內登記，冪等性不受影響。
`cancel` / `cancel_all` / `submit_unchecked` 全部比照辦理。

### 修正 (b)：緊急路徑改用有界取鎖，逾時後**無鎖繼續**

```python
# config.py 新增
emergency_lock_timeout_sec: float = Field(default=2.0, ge=0.1, le=30.0)
```

```python
@contextmanager
def _emergency_lock(self) -> Iterator[bool]:
    """緊急路徑專用取鎖。取不到就放行，並回報是否持有鎖。"""
    acquired = self._lock.acquire(timeout=self._settings.emergency_lock_timeout_sec)
    if not acquired:
        logger.critical(
            "緊急平倉無法在 %.1f 秒內取得共用鎖，判定有其他路徑卡住，"
            "改以無鎖模式強制繼續平倉",
            self._settings.emergency_lock_timeout_sec,
        )
    try:
        yield acquired
    finally:
        if acquired:
            self._lock.release()
```

`execute()` 中所有 `with self._lock:` 改為 `with self._emergency_lock() as locked:`，
且**無論 `locked` 為 True 或 False 都繼續執行平倉**。

#### 取捨說明（必須寫進程式碼註解）

| 選項 | 後果 |
|---|---|
| 等鎖到底 | 有部位平不掉，損失**無界** |
| 逾時後無鎖繼續 | 可能與另一條路徑併發送單，損失**有界** |

與 `05-engine-core.md` 的撤單失敗裁決同一原則：**不得用無界風險換有界風險**。

那把鎖的用途是避免併發送單（正確性上的講究）；
kill switch 失效則是災難性失敗。兩者不同量級。

且併發送單的最壞情況已有兩道既有防線兜底：
PaperGateway / 券商的 close-only 夾擠，以及 `OrderRouter` 的殘餘成交反向抵銷。

`CloseReport.notes` 需加入 `"未取得共用鎖，以無鎖模式執行"` 供事後稽核。

### 對應測試（必測）

| # | 情境 | 期望 |
|---|---|---|
| 24 | 另一執行緒持鎖不放，呼叫 `execute()` | **在 `emergency_lock_timeout_sec + 1` 秒內完成平倉**，最終部位為 0 |
| 25 | 同上 | `CloseReport.notes` 含無鎖模式註記，且有 CRITICAL 日誌 |
| 26 | 正常情況（鎖可取得） | 走正常路徑，`notes` 不含該註記 |
| 27 | `OrderRouter.submit()` 期間鎖的持有狀況 | 於 `gateway.place_order` 中以另一執行緒嘗試取鎖，**須能立即取得**（證明 I/O 在鎖外） |

> 情境 24 是本補強的驗收核心，請用 `threading.Event` 讓假 gateway 在
> `place_order` 內卡住，模擬網路停滯。測試必須有逾時保護，避免自己掛死。

---

### 關鍵設計約束

| 約束 | 理由 |
|---|---|
| 部位來源必須是 `gateway.list_positions()` | 引擎內部狀態可能已錯亂，kill switch 不能建立在可能故障的東西上 |
| 必須呼叫 `router.submit_unchecked()` | 走 `submit()` 會被風控的「單日虧損上限」擋下 —— 「因為虧太多所以不准停損」是致命反模式 |
| 先刪單再平倉 | 見 [2] |
| 重試次數有限 | 漲跌停鎖死時避免無窮迴圈 |
| 用 MKP 不用 MKT | MKT 在跌停附近可能以極端價成交 |
| 用 IOC 不用 ROD | 平倉單留在市場上比沒平掉更糟 |
| 整個 `execute()` 不得拋例外到外層 | kill switch 自己壞掉是最糟的情況；一律捕捉並反映在 `CloseReport.succeeded` |

---

## 2. utils/pidfile.py

```python
class PidFile:
    def __init__(self, path: Path) -> None: ...
    def acquire(self) -> None:
        """寫入當前 PID。若已存在且該 PID 存活 → 拋 MicroTXError（防止重複啟動）。"""
    def release(self) -> None: ...
    def __enter__(self) -> PidFile: ...
    def __exit__(self, *exc: object) -> None: ...

    @staticmethod
    def read_pid(path: Path) -> int | None:
        """讀取並驗證 PID 存活（os.kill(pid, 0)）。陳舊或不存在回傳 None。"""
```

要點：

- **陳舊 PID 檔必須能自動清除**（程式崩潰後殘留），否則下次啟動會被自己擋住
- `runtime/` 目錄需加入 `.gitignore`
- CLI 送訊號前用 `read_pid()` 檢查，拿不到就明確報「引擎未運行」，**不可靜默失敗**

---

## 3. engine.py — TradingEngine

```python
class TradingEngine:
    def __init__(self, settings: Settings, gateway: BrokerGateway) -> None: ...
    def add_strategy(self, strategy: Strategy) -> str: ...   # 回傳 strategy_id
    def start(self) -> None: ...
    def run_forever(self) -> None: ...
    def stop(self) -> None: ...
    def panic(self, source: str = "api") -> CloseReport: ...
    def flatten(self, source: str = "api") -> CloseReport: ...
    @property
    def state(self) -> EngineState: ...
```

### 訊號處理

```python
signal.signal(signal.SIGUSR1, self._on_signal)   # PANIC
signal.signal(signal.SIGUSR2, self._on_signal)   # FLATTEN
signal.signal(signal.SIGTERM, self._on_shutdown)
signal.signal(signal.SIGINT,  self._on_shutdown)
```

⚠️ **訊號處理器內只做兩件事**：

```python
def _on_signal(self, signum: int, frame: object) -> None:
    self._pending_mode = CloseMode.PANIC if signum == signal.SIGUSR1 else CloseMode.FLATTEN
    self._emergency_event.set()
```

真正的平倉在常駐的 `EmergencyWorker` 執行緒完成。
Python 訊號處理器在主執行緒的位元組碼之間執行，**不可做網路 I/O 或取鎖**。

### 執行緒清單

| 執行緒 | 職責 |
|---|---|
| `StrategyWorker` | 消費 `tick_queue` → 跑策略 → 過風控 → 下單 |
| `EventWorker` | 消費委託/成交回報 → 更新 tracker → 通知策略 |
| `Scheduler` | 時段檢查、13:40 強平、00:00 重置 |
| `EmergencyWorker` | 🚨 等待 `_emergency_event`，被喚醒即執行 `EmergencyCloser.execute()` |
| `ReconcileWorker` | 每 60 秒比對券商部位與內部狀態，不一致告警 |
| 主執行緒 | 訊號處理、生命週期、`run_forever()` |

### 關機流程（SIGTERM / SIGINT）

```
1. state = SHUTTING_DOWN
2. 停止接受新訊號來源（feed.stop()、scheduler.stop()）
3. if settings.flatten_on_shutdown:
       EmergencyCloser.execute(PANIC, source="shutdown")
   else:
       gateway.cancel_all_orders()    # 至少不要留掛單
4. 各 worker join(timeout=10)
5. gateway.disconnect()（Shioaji 連線數有上限，務必登出）
6. pidfile.release()
```

### 未預期例外

任何 worker 捕捉到未預期例外 → CRITICAL 日誌 →
`EmergencyCloser.execute(PANIC, source="unhandled_exception")`。
**引擎壞掉時的預設行為是平倉，不是繼續帶著部位跑。**

---

## 測試要求（每項都必須有）

| # | 情境 | 期望 |
|---|---|---|
| 1 | 有多單 1 口，`execute(PANIC)` | 送出反向 IOC/MKP 平倉單，最終 `list_positions()` 為空，`succeeded=True` |
| 2 | **先刪單再平倉的順序** | 用 mock 記錄呼叫序，`cancel_all_orders` 必須早於第一筆 `place_order` |
| 3 | **繞過風控** | 先讓 RiskManager 處於「單日虧損達標 + 交易次數超限 + cooldown 中」，`execute()` 仍必須成功送單 |
| 4 | **部位來源** | 令 `PositionTracker` 顯示空手、但 `gateway.list_positions()` 有 2 口 → 必須平掉這 2 口，且 `notes` 含不一致警告 |
| 5 | 漲跌停鎖死（`PaperGateway.set_price_limits`） | 重試至上限後停止，`succeeded=False`，`residual_positions` 非空，寫 CRITICAL 日誌 |
| 6 | 部分成交 | 第一次成交 1 口、殘餘 1 口 → 第二輪只重送 1 口 |
| 7 | 重入 | 連續呼叫兩次 `execute()`，第二次被冪等擋下且不重複送單 |
| 8 | 空手時觸發 | 不送任何委託，`succeeded=True`，`positions_before` 為空 |
| 9 | 非交易時段觸發 | 不送單，`pending` 被設定，開盤後由引擎自動執行 |
| 10 | 斷線時觸發（`force_disconnect()`） | 嘗試重連 3 次，失敗則 CRITICAL 告警 + `succeeded=False`，**不拋例外** |
| 11 | PANIC vs FLATTEN 收尾 | **兩者策略皆轉 `ABORTED`**；PANIC → `HALTED`、FLATTEN → `RUNNING` |
| 21 | `abort()` 於**每一種**狀態呼叫 | 全部不得拋例外；非終態轉 `ABORTED`，終態為 no-op（逐一參數化測試所有 `StrategyState`） |
| 22 | `abort()` 後再餵 tick / fill | 回傳空 list，不產生任何訊號 |
| 23 | OCO 的 `abort()` | 兩腿子策略皆轉 `ABORTED` |
| 16 | `notifier.notify()` 拋例外 | 被吞下並記錄，平倉流程**照常完成**，`succeeded` 不受影響 |
| 17 | `is_tradable()` 拋例外 | 視為可交易，繼續平倉（不得因判斷程式壞掉而不平倉） |
| 18 | `emergency_use_market_order=False` | 送出 `LMT` + `IOC`，價格為漲停（買）／跌停（賣） |
| 19 | 同上但 `get_price_limits()` 失敗 | 降級為 `MKP`，寫 WARNING，仍完成平倉 |
| 20 | 休市觸發 | 立即 `HALTED` + 取消策略 + 設 `pending` + 發 WARNING 通知；`succeeded=False` |
| 12 | 平倉期間收到觸價訊號 | 訊號被丟棄，不產生新委託 |
| 13 | `execute()` 內部任一步拋例外 | 被捕捉，回傳 `succeeded=False` 的報告，**不向外拋** |
| 14 | 陳舊 PID 檔 | `PidFile.acquire()` 能清除並取得；`read_pid()` 對死掉的 PID 回傳 `None` |
| 15 | 訊號處理器 | 用 `os.kill(os.getpid(), SIGUSR1)` 驗證 `_emergency_event` 被設定且處理器耗時 < 1ms |

> 測試一律使用 `PaperGateway`，**不得依賴真實連線**。
> 涉及時間的用 `freezegun`，涉及輪詢的把 `emergency_retry_interval_sec` 設為極小值。

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] `emergency.py` 覆蓋率 ≥ 95%（這是安全裝置，標準比其他模組高）
- [ ] 情境 2、3、4 的測試必須存在（這三條是本模組的設計核心）
- [ ] `execute()` 在任何情況下都不會向外拋例外（用 `pytest.raises` 反向驗證）
- [ ] 交付時在回覆中說明：你如何確保「引擎卡死時 kill switch 仍可用」
