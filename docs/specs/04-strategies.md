# 任務 04 — 策略層（Scalp / OCO）

## 目標

實作兩種條件單策略。**本層是純邏輯：無 I/O、無執行緒、無 sleep、不 import broker 實作**，
因此可以 100% 單元測試覆蓋，是整個專案最能展示工程品質的部分。

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/strategies/base.py` | 新增 |
| `src/microtx/strategies/scalp.py` | 新增 |
| `src/microtx/strategies/oco.py` | 新增 |
| `tests/test_scalp_strategy.py`、`tests/test_oco_strategy.py` | 新增 |

---

## base.py

`Signal` 與 `Strategy(ABC)` 定義見 `docs/architecture.md` §4.3。

補充：

```python
class Strategy(ABC):
    def __init__(self, *, spec: FuturesSpec, quantity: int) -> None:
        self._state = StrategyState.IDLE
        ...

    @property
    def state(self) -> StrategyState: ...

    def _transition(self, new_state: StrategyState, reason: str) -> None:
        """狀態轉換的唯一入口。非法轉換必須拋 StrategyError。"""
```

要求：

- 定義 `_VALID_TRANSITIONS: dict[StrategyState, frozenset[StrategyState]]`，
  在 `_transition` 中檢查。非法轉換是 bug，必須立刻爆而不是靜默忽略
- 終態（`CLOSED` / `CANCELLED` / `ERROR`）之後 `on_tick()` 一律回傳空 list
- `describe()` 回傳人類可讀的一行摘要，供 CLI 與日誌使用

---

## scalp.py — 觸價進場 + 點數停利停損

### 建構參數

```python
class ScalpStrategy(Strategy):
    def __init__(
        self,
        *,
        spec: FuturesSpec,
        direction: Direction,      # LONG / SHORT
        trigger_price: float,      # 觸發價
        take_profit_points: int,   # 停利點數（正整數）
        stop_loss_points: int,     # 停損點數（正整數）
        quantity: int = 1,
        trailing_points: int | None = None,  # 選配：移動停利
    ) -> None: ...
```

### 觸價判定（核心，絕不可寫錯）

```python
# ⛔ 錯誤（官方範例的寫法，跳空時永不觸發）：
if tick.price == self._trigger_price: ...

# ✅ 正確：穿越判定
if self._direction is Direction.LONG:
    triggered = tick.price >= self._trigger_price   # 向上突破做多
else:
    triggered = tick.price <= self._trigger_price   # 向下跌破做空
```

### 停利停損價位

```python
sign = self._direction.sign          # LONG=+1, SHORT=-1
tp_price = self._entry_price + sign * self._take_profit_points
sl_price = self._entry_price - sign * self._stop_loss_points
```

出場判定同樣用穿越比較：

- 多單：`price >= tp_price` 停利、`price <= sl_price` 停損
- 空單：`price <= tp_price` 停利、`price >= sl_price` 停損

### 移動停利（`trailing_points` 不為 None 時）

- 記錄進場後的最有利價 `_best_price`
- 停損價隨之推進：`sl_price = _best_price - sign * trailing_points`
- **停損價只能往有利方向移動，絕不回退**（這是移動停利的定義，必須有測試）

### 狀態流轉

```
IDLE ──arm()──> ARMED ──觸價──> ENTRY_PENDING ──on_fill──> IN_POSITION
                  │                    │                        │
             cancel()             on_reject()            停利/停損/強平
                  ▼                    ▼                        ▼
             CANCELLED             CANCELLED             EXIT_PENDING
                                                                │ on_fill
                                                                ▼
                                                             CLOSED
```

### 建構參數驗證（在 `__init__` 就檢查，不要拖到執行期）

| 檢查 | 失敗行為 |
|---|---|
| `take_profit_points > 0` | 拋 `ValueError` |
| `stop_loss_points > 0` | 拋 `ValueError` |
| `quantity > 0` | 拋 `ValueError` |
| `trigger_price > 0` | 拋 `ValueError` |
| `trailing_points > 0`（若有給） | 拋 `ValueError` |

---

## oco.py — 雙向括號單

```python
class OcoStrategy(Strategy):
    def __init__(
        self,
        *,
        spec: FuturesSpec,
        upper_trigger: float,      # 向上突破 → 做多
        lower_trigger: float,      # 向下跌破 → 做空
        take_profit_points: int,
        stop_loss_points: int,
        quantity: int = 1,
    ) -> None: ...
```

- 內部組合兩個方向的觸價條件；**任一方觸發，另一方立即失效**
- 觸發後行為與 `ScalpStrategy` 完全一致（建議內部委派給一個 `ScalpStrategy` 實例，
  而非複製貼上邏輯 —— 程式碼重複是面試扣分點）
- 建構驗證：`upper_trigger > lower_trigger`，否則拋 `ValueError`

---

## 必測情境（每項都要有對應測試）

| # | 情境 | 期望 |
|---|---|---|
| 1 | **跳空穿越** 觸發價 23150，價格從 23140 直接跳到 23180 | **必須觸發**（這是官方範例的致命缺陷） |
| 2 | 價格恰好等於觸發價 | 觸發 |
| 3 | 觸發後再收到更多 tick | **不重複發出進場訊號**（冪等） |
| 4 | 空單向下跌破觸發 | 正確判定方向 |
| 5 | 多單停利：進場 23000、TP 50 點 → 價格 23050 | 發出停利訊號 |
| 6 | 多單停損：進場 23000、SL 30 點 → 價格 22970 | 發出停損訊號 |
| 7 | 同一 tick 同時滿足停利與停損（極端跳空） | **優先停損**（保守原則），且必須有明確註解說明理由 |
| 8 | 移動停利推進後價格回落 | 停損價不回退，於推進後的價位出場 |
| 9 | 終態後再餵 tick | 回傳空 list，不拋例外 |
| 10 | 非法狀態轉換 | 拋 `StrategyError` |
| 11 | `force_close()` 於任一狀態 | `ARMED` → `CANCELLED`；`IN_POSITION` → 發出 `FORCE_CLOSE` 訊號 |
| 12 | OCO 上方先觸發 | 下方條件失效，後續跌破 `lower_trigger` 不得觸發 |
| 13 | `on_reject()` 於 `ENTRY_PENDING` | 轉 `CANCELLED` 並記錄原因 |
| 14 | 部分成交（`on_fill` 分兩次送達） | 均價正確、口數累加、狀態正確 |

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] `strategies/` 底下**不 import** `broker` 的具體實作、不 import `shioaji`、無 `time.sleep`
- [ ] 覆蓋率 ≥ 95%（純邏輯層，應接近全覆蓋）
- [ ] 情境 1（跳空）與情境 7（同時觸發）的測試必須有繁中註解說明為何這樣設計
