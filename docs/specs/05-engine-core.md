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
| `src/microtx/broker/base.py` | **擴充**：`OrderRequest` 新增 `strategy_id: str = ""` |
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

### 正式契約（取代先前所有版本）

```python
@dataclass(frozen=True, slots=True)
class RiskContext:
    """風控決策所需的**完整**輸入快照。

    RiskManager 是純函式，判斷所需的一切都必須由本結構提供，
    不得由 RiskManager 自行查詢或累計。
    """

    now: datetime                               # tz-aware, Asia/Taipei
    session: SessionType
    engine_state: EngineState                   # 規則 3 需要
    position: PositionSnapshot
    realized_pnl_ntd: float
    total_pnl_ntd: float
    trade_count: int
    last_order_at: datetime | None              # 規則 7 cooldown 需要
    price_limits: tuple[float, float] | None    # 規則 8 需要 (limit_down, limit_up)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str


class RiskManager:
    """純決策函式，**不持有任何可變狀態**。"""

    def __init__(self, settings: Settings) -> None: ...
    def check(self, request: OrderRequest, ctx: RiskContext) -> RiskDecision: ...
    def should_halt(self, ctx: RiskContext) -> tuple[bool, str]: ...
```

### 為什麼 RiskManager 沒有 `on_fill()` / `reset_daily()`

先前版本讓 `RiskManager` 自行累計損益與交易次數，等於與 `PositionTracker` **各記一本帳**。
兩本帳一旦漂移，風控就會依錯誤數字判斷 —— 而漂移通常只在出事那天才被發現。

改為單一真相來源：

| 狀態 | 擁有者 |
|---|---|
| 已實現／未實現損益、交易次數、部位 | `PositionTracker` |
| 上次成功下單時間 | `OrderRouter` |
| 引擎狀態 | `TradingEngine` |
| 交易時段 | `Scheduler` |

`RiskManager` 只是 `(request, ctx) -> decision`。好處：零鎖需求，
測試不必安排狀態序列，直接構造 `ctx` 即可窮舉每條規則。

### `should_halt()` 簽章衝突的裁決

`architecture.md` 舊版寫 `should_halt(self) -> bool`，任務單寫
`should_halt(self, ctx) -> tuple[bool, str]`。

**以任務單版本為準**，`architecture.md` 已同步更新。理由：

- `RiskManager` 既然無狀態，判斷必須吃 `ctx`
- 停機是重大事件，`reason` 字串會寫入 CRITICAL 日誌與通知，
  「為什麼停機」比「是否停機」更重要

### 價格限制的注入方式

`price_limits` 由**呼叫端**（`OrderRouter`）向 gateway 取得後放進 `ctx`，
**不讓 `RiskManager` 持有 gateway 參考**。

理由：一旦 `RiskManager` 能呼叫 gateway，它就不再是純函式 ——
測試得備妥假 gateway，且風控判斷會夾帶網路 I/O 延遲。
風控必須是純運算、瞬間完成的。

`price_limits` 為 `None` 時（例如尚未載入商品檔），規則 8 **跳過檢查**並記錄 DEBUG，
不因此拒單 —— 風控缺資料不應變成阻擋交易的理由。

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
    def on_event(self, event: OrderEvent) -> None:
        """更新 in_flight 與 exchange_order_no 快取。由 EventWorker 呼叫。"""

    @property
    def in_flight(self) -> dict[str, OrderRequest]: ...  # client_id -> request
    @property
    def last_order_at(self) -> datetime | None: ...
```

### 「同一策略」的識別方式

`OrderRequest` 新增 `strategy_id: str = ""` 欄位（見 `architecture.md` §4.1）。

- 空字串代表**非策略發起**（緊急平倉、手動下單），不參與策略層級的撤單邏輯
- `in_flight` 維持 `dict[str, OrderRequest]` 即可 ——
  `OrderRequest` 自身帶著 `strategy_id`，可直接過濾，**不需要平行的第二個字典**
  （平行結構就是漂移的溫床）
- 未來 `ShioajiGateway` 可把 `strategy_id` 寫進 Shioaji `FuturesOrder.custom_field`，
  讓券商端的委託紀錄也能回溯來源

```python
def _cancel_working_entries(self, strategy_id: str) -> CancelOutcome:
    """盡力撤掉指定策略所有未成交的進場委託。"""
    if not strategy_id:
        return CancelOutcome((), ())
    targets = [
        req for req in self._in_flight.values()
        if req.strategy_id == strategy_id and req.intent is OrderIntent.ENTRY
    ]
    ...
```

---

## 撤單失敗時的正式行為（安全關鍵）

### 不變式的正確表述

先前寫的「**先撤單再平倉**」容易被誤讀成「撤單成功是平倉的前置條件」。
正確的不變式是關於**結果**，不是機制：

> **不得因為未撤銷的進場委託，而使部位反向。**

達成方式不是「撤不掉就不平倉」，而是「撤不掉就事後補償」。

### 為什麼不能用「撤單失敗就拒送出場單」的 fail-safe

比較兩個分支的最壞情況：

| 分支 | 最壞情況 | 損失上界 |
|---|---|---|
| 拒送出場單 | 已成交部位裸露在快市中，沒有停損保護 | **無界**，直到下次重試成功 |
| 照送出場單 | 殘餘進場單事後成交，形成反向部位 | **有界**，最多為未成交的進場口數 |

用無界風險換有界風險是錯的。而且諷刺的是，撤單失敗最可能發生在**系統忙碌／連線不穩**時 ——
也正是最需要停損的時候。

因此：**保護性出場（STOP_LOSS / FORCE_CLOSE / EMERGENCY）永遠優先送出，
撤單失敗不得阻擋它。**

> 這與 `06-emergency-close.md` 一致：那裡的 `cancel_all_orders()` 失敗也不會中止平倉，
> 只會反映在 `CloseReport`。撤單是**盡力而為的前置動作**，不是出場的前置條件。

### 進場委託的 broker_order_id 解析順序

`FillEvent` 可能早於 `AckEvent` 抵達，此時 `broker_order_id` 尚未快取。解析順序：

```
① self._order_no_cache[client_id]        ← AckEvent 建立的快取，最快
② gateway.list_open_orders() 以 client_id 比對   ← 一次往返，可接受
③ 兩者皆無 → 標記為 abandoned，進入補償機制
```

步驟 ③ 的「找不到」有兩種可能（已全部成交、或尚未到交易所），
兩者都無法立即撤銷，一律走補償路徑。

### 補償機制（真正的保險）

```python
@dataclass(frozen=True, slots=True)
class CancelOutcome:
    cancelled: tuple[str, ...]   # 成功撤銷的 client_id
    abandoned: tuple[str, ...]   # 撤銷失敗或查無此單，需事後補償
```

`OrderRouter` 維護 `_abandoned_entries: dict[str, OrderRequest]`，並執行兩件事：

**1. 持續重試撤單**

每次 `on_event()` 被呼叫時，對 `_abandoned_entries` 內尚未成交者再試一次撤單
（收到 `AckEvent` 時尤其有機會成功，因為此時 `broker_order_id` 才剛拿到）。
成功即移出清單。

**2. 殘餘成交立即反向平倉（關鍵）**

```python
def on_event(self, event: OrderEvent) -> None:
    match event:
        case FillEvent() if event.client_id in self._abandoned_entries:
            # 撤不掉的進場單事後成交了 —— 這就是會造成反向部位的那一筆。
            # 立即送出等量反向 EMERGENCY 平倉單把它抵銷掉。
            logger.critical(
                "殘餘進場委託成交 client_id=%s 口數=%d，立即反向平倉",
                event.client_id, event.quantity,
            )
            self.submit_unchecked(OrderRequest(
                symbol=..., action=event.action.opposite,
                quantity=event.quantity, price=None,
                price_type=PriceType.MKP, time_in_force=TimeInForce.IOC,
                intent=OrderIntent.EMERGENCY, client_id=new_client_id(),
                strategy_id="",
            ))
```

用 `EMERGENCY` 意圖是刻意的：這張抵銷單**必須繞過風控**，
否則會被「單日交易次數上限」擋下，反向部位就留在帳上了。

### 對應測試（皆為必測）

| # | 情境 | 期望 |
|---|---|---|
| A | `FillEvent` 早於 `AckEvent`，`_order_no_cache` 無資料 | 走 `list_open_orders()` 以 `client_id` 找到並撤單成功 |
| B | 兩種來源都查無此單 | 標記 abandoned，**出場單仍照送**（`accepted=True`） |
| C | `cancel_order()` 回傳 `False` | 標記 abandoned，**出場單仍照送** |
| D | `cancel_order()` 拋 `BrokerError` | 同上，例外被捕捉不外拋 |
| E | abandoned 的進場單事後成交 | 立即送出等量反向 `EMERGENCY` 單，寫 CRITICAL 日誌 |
| F | E 的抵銷單須繞過風控 | 讓風控三條規則同時觸發，抵銷單仍須送出 |
| G | abandoned 進場單在後續 `AckEvent` 後撤單成功 | 移出 `_abandoned_entries`，不再重試 |
| H | 端到端：部分成交 → 停損 → 殘餘成交 | 最終部位為 0，**不得為反向** |

> 情境 H 是本節的驗收核心，請以 `PaperGateway` 建構完整序列並斷言最終部位。

⚠️ 本任務需要修改 `src/microtx/broker/base.py` 以新增 `strategy_id` 欄位。
該欄位有預設值，不會破壞既有呼叫端。已列入本任務的允許檔案清單。

### cooldown 時間的擁有者

`OrderRouter` 自行記錄 `_last_submit_at`（成功送出且被券商接受時更新）。

`submit()` 收到 `ctx` 後，先用自己的紀錄覆寫再交給風控：

```python
def submit(self, request: OrderRequest, ctx: RiskContext) -> OrderAck:
    with self._lock:
        ctx = replace(ctx, last_order_at=self._last_submit_at)   # 以 router 的紀錄為準
        decision = self._risk.check(request, ctx)
        ...
```

理由：只有 `OrderRouter` 知道「上一次真的送出委託」是什麼時候，
由呼叫端（引擎）填這個欄位會有時序落差。`ctx` 的其他欄位仍由引擎組裝。

要點：

- **冪等**：`client_id` 已在 `in_flight` 或已完成 → 直接回 `accepted=False`，不重送
- **送出場單前，先盡力撤掉同一策略所有未成交的進場委託。**

  ```python
  if request.intent in _CLOSE_ONLY_INTENTS:
      outcome = self._cancel_working_entries(request.strategy_id)  # 盡力撤單
      ...                                                          # 無論結果都送出場單
  ```

  理由：策略在部分成交時可能仍持有未成交的進場委託。若出場單先成交、
  殘留的進場單隨後才成交，部位會從「已平倉」變成「反向持倉」——
  與 `06-emergency-close.md` 的「先刪單再平倉」是同一條原則，
  只是這裡的粒度是單一策略，那裡是全帳戶。

  把撤單責任放在引擎層而非策略層，是為了讓 `strategies/` 維持純邏輯、零 I/O。

  ⚠️ **撤單失敗不得阻擋出場單**，改以事後補償保證不變式 ——
  詳見下方「撤單失敗時的正式行為」。
- 下單前呼叫 `risk.check()`；被拒時寫 WARNING 日誌（含拒絕原因）並回 `accepted=False`
- 用 `@retry(attempts=3, exceptions=(BrokerError,))` 包裝實際的 gateway 呼叫
- 所有下單/刪單操作持有 `lock`，確保與緊急平倉互斥
- `submit_unchecked` 必須在 docstring 明確警示「僅限緊急平倉，其他呼叫者一律用 submit」

---

## scheduler.py — Scheduler

```python
class Scheduler:
    def __init__(
        self,
        settings: Settings,
        *,
        on_force_close: Callable[[str], None],   # 13:40 到點，參數為觸發來源字串
        on_reset_daily: Callable[[], None],      # 00:00 跨日重置
    ) -> None: ...
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
