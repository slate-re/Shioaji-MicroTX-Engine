# 任務 05 — 引擎核心（部位 / 風控 / 下單路由 / 排程）

## 目標

實作引擎的四個支柱模組。**不含主協調器與緊急平倉**（那是任務 06）。

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/engine/position.py` | 新增 |
| `src/microtx/engine/risk.py` | 新增 |
| `src/microtx/engine/order_router.py` | 新增 |
| `src/microtx/engine/scheduler.py` | 新增 |
| `tests/test_position.py`、`test_risk.py`、`test_order_router.py`、`test_scheduler.py` | 新增 |

---

## position.py — PositionTracker

```python
@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    direction: Direction | None   # None = 空手
    quantity: int
    average_price: float
    unrealized_points: float
    unrealized_ntd: float

class PositionTracker:
    def on_fill(self, fill: FillEvent) -> None: ...
    def on_tick(self, tick: TickEvent) -> None: ...      # 更新未實現損益
    def snapshot(self) -> PositionSnapshot: ...
    @property
    def realized_pnl_ntd(self) -> float: ...             # 當日已實現損益
    @property
    def total_pnl_ntd(self) -> float: ...                # 已實現 + 未實現
    @property
    def trade_count(self) -> int: ...                    # 當日進場次數
    def reset_daily(self) -> None: ...
    def reconcile(self, broker_positions: list[Position]) -> list[str]:
        """與券商實際部位比對，回傳不一致描述清單（空 = 一致）。"""
```

要點：

- 反向成交先平後開（FIFO），平倉時結算已實現損益
- 所有金額換算走 `spec.points_to_ntd()`，**不得出現硬編碼乘數**
- `reconcile()` 是雙軌驗證的關鍵，任務 06 的緊急平倉會用到

---

## risk.py — RiskManager

```python
@dataclass(frozen=True, slots=True)
class RiskContext:
    now: datetime
    position: PositionSnapshot
    realized_pnl_ntd: float
    total_pnl_ntd: float
    trade_count: int
    session: SessionType

class RiskManager:
    def check(self, request: OrderRequest, ctx: RiskContext) -> RiskDecision: ...
    def should_halt(self, ctx: RiskContext) -> tuple[bool, str]: ...
    def reset_daily(self) -> None: ...
```

### 檢查規則（依序，任一不過即拒絕並回傳原因）

| # | 規則 | 拒絕訊息範例 |
|---|---|---|
| 1 | **緊急平倉直接放行** | `request.intent is OrderIntent.EMERGENCY` → 立即 `approved=True` |
| 2 | 非交易時段 | 「非交易時段（目前 SessionType.CLOSED）」 |
| 3 | 引擎已 HALTED 且非平倉單 | 「引擎已停機，僅允許平倉」 |
| 4 | 單日累計虧損 ≤ `-max_daily_loss` 且非平倉單 | 「已達單日停損 3,000 元」 |
| 5 | 單日交易次數 ≥ `max_daily_trades` 且為新倉 | 「已達單日交易上限 10 筆」 |
| 6 | 新倉後部位 > `max_position_size` | 「將超過最大持倉 2 口」 |
| 7 | 距上次下單 < `order_cooldown_sec` 且為新倉 | 「下單節流中，剩餘 1.2 秒」 |
| 8 | 委託價超出漲跌停 | 「委託價 44200 超出漲停 44136」 |

> ⚠️ 規則 1 是設計核心：緊急平倉必須是**第一條**檢查、直接放行。
> 若讓 EMERGENCY 單受規則 4/5/7 約束，會出現「因為虧太多所以不准你停損」的致命反模式。
> 這條規則必須有專門的測試。

### `should_halt()`

當日總損益 ≤ `-max_daily_loss` 時回傳 `(True, 原因)`，供 `TradingEngine` 觸發
`EmergencyCloser.execute(PANIC, source="risk_halt")`。

---

## order_router.py — OrderRouter

```python
class OrderRouter:
    def __init__(
        self,
        gateway: BrokerGateway,
        *,
        risk: RiskManager,
        lock: threading.RLock,       # 與 EmergencyCloser 共用
    ) -> None: ...

    def submit(self, request: OrderRequest, ctx: RiskContext) -> OrderAck: ...
    def submit_unchecked(self, request: OrderRequest) -> OrderAck:
        """🚨 僅供 EmergencyCloser 使用，繞過 RiskManager。"""
    def cancel(self, broker_order_id: str) -> bool: ...
    def cancel_all(self) -> int: ...
    @property
    def in_flight(self) -> dict[str, OrderRequest]: ...  # client_id -> request
```

要點：

- **冪等**：`client_id` 已在 `in_flight` 或已完成 → 直接回 `accepted=False`，不重送
- 下單前呼叫 `risk.check()`；被拒時寫 WARNING 日誌（含拒絕原因）並回 `accepted=False`
- 用 `@retry(attempts=3, exceptions=(BrokerError,))` 包裝實際的 gateway 呼叫
- 所有下單/刪單操作持有 `lock`，確保與緊急平倉互斥
- `submit_unchecked` 必須在 docstring 明確警示「僅限緊急平倉，其他呼叫者一律用 submit」

---

## scheduler.py — Scheduler

```python
class Scheduler:
    def __init__(self, settings: Settings, *, on_force_close: Callable[[str], None]) -> None: ...
    def current_session(self, now: datetime | None = None) -> SessionType: ...
    def is_tradable(self, now: datetime | None = None) -> bool: ...
    def start(self) -> None:   # 啟動背景執行緒，每秒檢查
    def stop(self) -> None: ...
```

要點：

- 時區固定 `Asia/Taipei`（用 `zoneinfo.ZoneInfo`），**不可依賴機器本地時區**
  （Mac Mini 時區設錯會導致強平時間錯亂）
- 日盤 `session_start`–`session_end`；夜盤 15:00–次日 05:00 **跨日判定**要正確
- 到達 `force_close_time` 時呼叫 `on_force_close("scheduler")`，**當日只觸發一次**
- 週末與國定假日：本版先只處理週末（週六日不交易）；
  假日行事曆列為 TODO，在程式中留明確註記
- 00:00 觸發 `reset_daily` 回呼

---

## 測試要求

| 模組 | 必測 |
|---|---|
| PositionTracker | 多空建倉、反向平倉、部分成交、均價計算、已實現/未實現損益、`reconcile` 偵測不一致 |
| RiskManager | **每條規則各一測試**；EMERGENCY 繞過測試（規則 4/5/7 全部觸發的情況下仍放行）|
| OrderRouter | 冪等（同 client_id 送 3 次只成 1 次）、風控拒絕路徑、重試、`submit_unchecked` 繞過風控 |
| Scheduler | 用 `freezegun` 測日盤/夜盤/收盤判定、跨日夜盤、強平當日只觸發一次、週末不交易 |

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] `engine/` 不 import shioaji（只依賴 `broker/base.py`）
- [ ] 覆蓋率 ≥ 90%
- [ ] `RiskManager` 的 EMERGENCY 繞過測試存在且通過
