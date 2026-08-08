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
| `tests/test_emergency_closer.py` | 新增 |
| `tests/test_engine.py` | 新增 |
| `src/microtx/config.py` | **擴充**（新增下方設定項） |
| `.env.example` | 已含對應項目，對照即可 |

## config.py 需新增

```python
emergency_max_retries: int = Field(default=5, ge=1, le=20)
emergency_retry_interval_sec: float = Field(default=0.5, ge=0.1, le=5.0)
emergency_use_market_order: bool = Field(default=True)
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
        lock: threading.RLock,                      # 與 OrderRouter 共用
        on_state_change: Callable[[EngineState], None],
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
    PANIC   → 維持 HALTED，不自動恢復
    FLATTEN → 所有策略轉 CANCELLED，引擎回 RUNNING 待命
    finally: self._is_closing = False
```

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
| 11 | PANIC vs FLATTEN 收尾 | PANIC → `EngineState.HALTED`；FLATTEN → `RUNNING` 且策略皆 `CANCELLED` |
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
