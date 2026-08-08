# 任務 02 — PaperGateway（離線模擬撮合）

## 目標

實作**不需要永豐帳號**就能運行的券商閘道，作為：

1. 全專案單元測試的替身（取代 mock，行為更真實）
2. 面試官 / 他人 clone 專案後的離線 Demo
3. 策略邏輯的快速驗證環境

> 這個模組決定了後續所有任務能否離線開發，優先度僅次於 01。

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/broker/paper_gateway.py` | 新增 |
| `src/microtx/enums.py` | **擴充**：新增 `EventOrder`（見下方「回報事件順序」） |
| `tests/test_paper_gateway.py` | 新增 |
| `tests/fixtures/sample_ticks.csv` | 新增（一段合成的日內 tick 序列，含跳空與試撮） |

## 介面

實作 `BrokerGateway` 全部抽象方法。額外提供測試用控制面：

```python
class PaperGateway(BrokerGateway):
    def __init__(
        self,
        *,
        spec: FuturesSpec,
        initial_price: float = 23_000.0,
        slippage_ticks: int = 0,
        fill_delay_sec: float = 0.0,
        reject_rate: float = 0.0,
        max_fill_quantity_per_tick: int | None = None,   # None = 流動性無限
        event_order: EventOrder = EventOrder.FILL_FIRST,
    ) -> None: ...

    # --- 測試控制面（僅供測試與 Demo，非 BrokerGateway 介面） ---
    def feed_tick(self, price: float, *, volume: int = 1, simtrade: bool = False) -> None:
        """手動注入一筆 tick，同步觸發已註冊的行情 callback。"""

    def replay(self, ticks: Iterable[RawTick], *, speed: float = 0.0) -> None:
        """重播 tick 序列。speed=0 表示不延遲（測試用），1.0 表示原速。"""

    def force_disconnect(self) -> None:
        """模擬斷線，供重連邏輯測試。"""

    def set_price_limits(self, down: float, up: float) -> None:
        """模擬漲跌停，供緊急平倉『鎖死無法成交』情境測試。"""
```

## 撮合規則

### 前提：流動性模型（重要）

**tick 的 `volume` 欄位不參與撮合判定。**

PaperGateway 是**確定性測試夾具**，不是市場模擬器。若讓成交量取決於 tick volume，
每個測試都得精心編造 volume 數值，測試會變脆弱且失焦 ——
任務 06 的緊急平倉測試會變成在測合成流動性，而不是在測平倉邏輯。

因此預設 `max_fill_quantity_per_tick = None`，代表**流動性無限**：
只要價格條件成立，委託即全額成交。

需要測試部分成交時，用 `max_fill_quantity_per_tick` 明確指定，
這個旋鈕是確定性的、可重現的，不依賴 tick 內容。

```python
available = min(request.quantity, max_fill_quantity_per_tick or request.quantity)
```

### 撮合矩陣

| 委託 | 價格條件 | 成交量不足時（`available < quantity`） |
|---|---|---|
| `LMT` + `ROD` | 買單 `tick.price <= limit`；賣單 `tick.price >= limit` | 成交 `available`，**餘量續留市場** |
| `LMT` + `IOC` | 同上，僅檢查當下最新價 | 成交 `available`，**餘量立即取消**（`CancelEvent` reason=`"ioc_expired"`） |
| `LMT` + `FOK` | 同上 | **整筆取消**，一口都不成交（`reason="fok_expired"`） |
| `MKP` / `MKT` | 無條件（受漲跌停限制） | 依 `time_in_force` 套用上列規則 |

- 成交價：`LMT` 以委託價成交；`MKP` / `MKT` 以最新成交價 ± `slippage_ticks` 成交
- 漲跌停鎖死：若成交價會突破 `price_limits`，一律**不成交**且**不取消**，
  委託留在市場上（這正是緊急平倉失敗情境要模擬的狀態）
- `reject_rate > 0` 時按機率整筆拒單（`RejectEvent`），用於測試 `on_reject` 路徑

---

## 回報事件順序（必須可控，不可隱含）

`docs/shioaji_guide.md` 記載交易所的實際行為：
**成交回報的優先順序高於委託回報**，立即成交時可能先收到成交回報。

引擎必須容忍**兩種**順序，所以順序是**顯式參數**，不可由 `fill_delay_sec` 推導 ——
把「時間延遲」和「事件順序」耦合在一起，會導致無法測試「零延遲但 Ack 先到」的組合。
兩者是正交的。

```python
class EventOrder(str, Enum):
    """立即成交時的回報送出順序。"""

    FILL_FIRST = "FILL_FIRST"
    """成交回報先於委託回報。交易所的實際行為，本專案預設值。"""

    ACK_FIRST = "ACK_FIRST"
    """委託回報先於成交回報。"""
```

規則：

| 情況 | 送出順序 |
|---|---|
| 立即成交，`event_order=FILL_FIRST`（預設） | `FillEvent` → `AckEvent` |
| 立即成交，`event_order=ACK_FIRST` | `AckEvent` → `FillEvent` |
| 未立即成交（掛單等待） | 只送 `AckEvent`；日後成交時才送 `FillEvent` |
| `fill_delay_sec > 0` | 只影響 `FillEvent` 的**送出時間**，不影響上述順序 |

- **預設值選 `FILL_FIRST` 是刻意的**：這是比較難處理的情況，也是交易所的真實行為。
  預設走難路，測試才誠實。
- `FILL_FIRST` 時 `AckEvent` 仍必須送出（引擎需要其中的 `exchange_order_no`），只是排在後面
- `AckEvent.exchange_order_no` 由 PaperGateway 產生一個穩定的假單號（如 `"P" + 序號`）

---

## 平倉單不得反轉部位（安全不變式）

`intent` 為 `TAKE_PROFIT` / `STOP_LOSS` / `FORCE_CLOSE` / `EMERGENCY` 的委託
一律視為 **close-only**，成交量必須夾在目前持倉口數以內：

```python
fillable = min(request.quantity, current_position_quantity)
```

| 情況 | 行為 |
|---|---|
| `fillable == request.quantity` | 正常全額成交 |
| `0 < fillable < request.quantity` | 成交 `fillable` 口；超額部分送 `CancelEvent`，`reason="over_close"`，`cancelled_quantity` = 超額口數、`remaining_quantity=0` |
| `fillable == 0`（空手） | 不成交；送 `CancelEvent`，`reason="no_position"` |

- `OrderAck.accepted` 一律為 `True` —— 委託本身被接受了，只是可成交量受限
- **不要用 `RejectEvent` 表示超額部分**。`RejectEvent` 的契約語意是「券商拒絕**整筆**委託」，
  拿來表示部分超額會汙染語意，也會讓 `OrderRouter` 的 `in_flight` 記帳錯亂。
  `CancelEvent` 本來就有 `cancelled_quantity` / `remaining_quantity` / `reason`，正是為此而設。

> ⚠️ 這條是**安全不變式**，不只是撮合細節：
> close-only 委託在任何情況下都不得使部位變號。
> 若平倉單能超額成交，「平倉」就會變成「反向建倉」——
> 這正是 `06-emergency-close.md` 要求「先刪單再平倉」所要防範的同一類災難。
> 請在程式中以繁中註解標明此不變式，並附對應測試。

## 部位與損益

- 內部維護 `dict[str, Position]`，成交後更新數量與均價
- 反向成交時先平倉（FIFO），平完才反向建倉
- `list_positions()` 只回傳 `quantity > 0` 的部位
- 已實現損益用 `spec.points_to_ntd()` 換算，不要自己寫乘數

## 必須支援的關鍵行為

1. **`cancel_all_orders()` 要真的刪光**，並回傳實際刪除筆數（緊急平倉流程依賴它）
2. **`list_positions()` 是唯一真相來源**，即使內部有其他快取也要以此為準
3. **執行緒安全**：`feed_tick()` 可能從測試的其他執行緒呼叫，內部用 `threading.RLock`
4. 回報事件透過 `set_order_event_callback()` 註冊的 callback 送出，
   且可設定 `fill_delay_sec` 讓成交回報早於 / 晚於委託回報，模擬交易所實際行為

---

## 執行緒紀律（兩條硬性規則）

PaperGateway 的執行緒行為必須與 `ShioajiGateway` 一致，否則它作為測試替身就失去意義 ——
在假的執行緒模型上通過的測試，換到真 gateway 會爆。

### 規則 1：callback 一律在釋放鎖之後才送出

⛔ **禁止在持有 `self._lock` 的狀態下同步呼叫使用者 callback。**

正確作法是「鎖內收集、鎖外送出」：

```python
def place_order(self, request: OrderRequest) -> OrderAck:
    with self._lock:
        ...            # 撮合、更新部位、產生 events
        ack, events = ..., ...
    # ← 鎖已釋放
    for event in events:
        self._emit(event)
    return ack
```

理由（這不是潔癖，是會出人命的死鎖）：

任務 05 的 `OrderRouter` 與 `EmergencyCloser` 共用一把 RLock。若 callback 在
gateway 鎖內執行，就會出現典型的 AB-BA 死鎖：

```
執行緒 A（緊急平倉）：持有 router-lock → 呼叫 place_order → 等待 gateway-lock
執行緒 B（EventWorker）：持有 gateway-lock（callback 執行中）→ 等待 router-lock
                          ↓
                    互相等待，永久卡死
```

而這會發生在**緊急平倉的當下** —— 最不能卡死的那一刻。

真實的 Shioaji 也是由 SDK 的獨立執行緒送 callback，不會在呼叫端的鎖內執行。

### 規則 2：延遲事件的 timer 必須被追蹤與取消

`fill_delay_sec > 0` 時用 `threading.Timer` 送延遲事件，必須：

```python
self._pending_timers: list[Timer]          # 追蹤所有未觸發的 timer

def _cancel_pending_timers(self) -> int:   # 回傳取消筆數
    ...

def disconnect(self) -> None:
    self._cancel_pending_timers()          # 斷線即取消
    ...

def cancel_all_orders(self) -> int:
    self._cancel_pending_timers()          # 刪單即取消對應的延遲成交
    ...
```

並額外提供測試控制面：

```python
def flush_pending_events(self) -> int:
    """立即觸發所有待送事件，回傳送出筆數。

    測試一律用本方法推進時間，**禁止用 time.sleep() 等 timer 自然觸發**。
    """
```

理由：

- **測試污染**：`fill_delay_sec > 0` 的測試結束後留下 daemon timer，稍後觸發時
  會呼叫已失效的 callback，造成幽靈事件與間歇性失敗 —— 最難查的那種 bug
- **與任務 06 直接衝突**：緊急平倉的流程是「先 `cancel_all_orders()` 再平倉」。
  若 timer 未被取消，已刪除委託的 `FillEvent` 仍會在事後送達，
  這正是規格要防範的「殘留委託成交導致部位反轉」情境。
  測試夾具自己製造這種假象，會遮蔽或偽造真實行為。

### 對應測試

| 測試 | 期望 |
|---|---|
| callback 內回呼 gateway | 在 callback 裡呼叫 `list_positions()` / `place_order()` 不得死鎖（設 timeout 斷言） |
| callback 執行時鎖狀態 | 在 callback 內以另一執行緒嘗試取得 gateway 操作，須能立即取得（證明鎖已釋放） |
| `disconnect()` 取消 timer | `fill_delay_sec=10` 下單後立即 disconnect，`flush_pending_events()` 回傳 0 |
| `cancel_all_orders()` 取消 timer | 同上，刪單後不得再收到該委託的 `FillEvent` |
| `flush_pending_events()` | 延遲事件可被確定性觸發，測試全程零 `time.sleep()` |

## 邊界情境測試

| 情境 | 期望 |
|---|---|
| 平倉單口數大於持倉 | 成交至持倉口數，超額部分 `CancelEvent(reason="over_close")`，**部位不得變號** |
| 空手時送平倉單 | 不成交，`CancelEvent(reason="no_position")`，`accepted=True` |
| 同一 `client_id` 重複下單 | 第二次直接回 `accepted=False`，**不得重複成交**（冪等） |
| 漲跌停鎖死時送平倉單 | 委託被接受但永不成交且不取消，`list_positions()` 仍有部位 |
| 斷線後下單 | 拋 `ConnectionLostError` |
| `simtrade=True` 的 tick | 仍會傳給 callback（過濾是 market 層的責任，不是 broker 層） |
| tick `volume=1` 但下單 5 口 | **全額成交**（預設流動性無限，volume 不參與撮合） |
| `max_fill_quantity_per_tick=2`，下單 5 口 IOC | 成交 2 口，`CancelEvent(reason="ioc_expired", cancelled_quantity=3)` |
| 同上但為 FOK | **一口都不成交**，整筆 `CancelEvent(reason="fok_expired")` |
| 同上但為 LMT+ROD | 成交 2 口，餘 3 口續留市場（`list_open_orders()` 可見） |

## 測試要求

- 撮合矩陣每一格各一個測試
- **事件順序**：`FILL_FIRST` 與 `ACK_FIRST` 各一個測試，斷言 callback 收到的實際先後
- **安全不變式**：任何 close-only 委託送出後，部位方向不得改變（用 property-based 思維，
  對多空、各種口數組合各測一輪）
- 冪等測試：同 `client_id` 送 3 次，最終部位只有 1 口
- 損益測試：微台做多 23000 → 平倉 23050，已實現損益 = 500 元
- 漲跌停鎖死測試（供任務 06 的緊急平倉失敗情境重用）
- 覆蓋率 ≥ 90%（這是測試基礎設施，本身必須夠可靠）

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] 不 import shioaji
- [ ] `pytest tests/test_paper_gateway.py` 在**無網路、無 .env** 的環境下可通過
- [ ] 提供一個 `python -m microtx demo` 能用的重播資料（`tests/fixtures/sample_ticks.csv`）
