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
- 終態（`CLOSED` / `CANCELLED` / `ABORTED` / `ERROR`）之後 `on_tick()` 一律回傳空 list
- `describe()` 回傳人類可讀的一行摘要，供 CLI 與日誌使用

> **`ABORTED` 與 `abort()` 由任務 06 補上**（見 `06-emergency-close.md` §③）。
> 重點差異：`_transition()` 對非法轉換必須拋 `StrategyError`，
> 但 `abort()` 是**唯一不得拋例外**的入口 ——
> 它是緊急平倉路徑的一部分，安全裝置不能被狀態機檢查卡住。
>
> 語意區分（稽核用，不可混用）：
>
> | 終態 | 意義 |
> |---|---|
> | `CLOSED` | 正常完成一輪交易並平倉 |
> | `CANCELLED` | 主動取消，**從未進場** |
> | `ABORTED` | 被緊急平倉／強制停機中止，**可能曾持有部位** |
> | `ERROR` | 不可恢復錯誤，需人工介入 |

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

> 註：在正數 TP/SL 的前提下，多單恆有 `sl_price < entry_price < tp_price`
> （空單相反），因此**單一 tick 不可能同時滿足停利與停損**，兩者互斥。
> 先前規格的「同時滿足時優先停損」是錯誤描述，已移除。
> 真正需要處理的競合見下方「出場訊號唯一性」。

---

## 出場訊號唯一性（核心安全不變式）

真正會出事的競合不是「TP 對上 SL」，而是**任何原因造成同一部位被送出兩張出場單**。

```
持倉 1 口 → 送出停利單 1 口 → 收盤強平又送 1 口
          → 兩張都成交 → 部位變成 -1 口（反向持倉）
```

這與 `06-emergency-close.md` 要求「先刪單再平倉」防範的是**同一類災難**：
平倉動作重複執行，會把「出場」變成「反向建倉」。

因此策略層必須保證：

1. **`IN_POSITION` 狀態下，`on_tick()` / `on_fill()` / `force_close()` 合計只能產生一個出場訊號。**
2. 發出出場訊號的**同一次呼叫內**即轉入 `EXIT_PENDING`（不要等 `on_fill` 才轉）。
3. `EXIT_PENDING` 與所有終態下，`on_tick()` 一律回傳空 list。
4. 優先序（同時成立時）：`FORCE_CLOSE` > `STOP_LOSS` > `TAKE_PROFIT`。
   保守原則：先確保離場，再談離場方式。

> PaperGateway 的 close-only 夾擠是**最後一道防線**，不是可以依賴的東西。
> 策略層本身就不該產生重複出場訊號 —— 在真實券商上沒有那道防線。

---

## 部分成交下的狀態與風險監控

**狀態轉換**（Codex 提案，確認採用）：

| 情況 | 狀態 |
|---|---|
| 進場累計成交 < `quantity` | 維持 `ENTRY_PENDING` |
| 進場全數成交 | 轉 `IN_POSITION` |
| 出場累計成交 < 持倉口數 | 維持 `EXIT_PENDING` |
| 出場全數成交 | 轉 `CLOSED` |

**但風險監控不看狀態，看 `filled_quantity`：**

```python
# ⛔ 錯誤：部分成交時已經有部位、已經有風險，卻因狀態還是 ENTRY_PENDING 而不監控
if self._state is StrategyState.IN_POSITION:
    check_stop_loss(tick)

# ✅ 正確：只要成交過就有曝險，就要監控
if self._filled_quantity > 0:
    check_stop_loss(tick)
```

理由：進場 2 口只成交 1 口時，那 1 口的市場風險**已經完全存在**。
若因為狀態還停在 `ENTRY_PENDING` 就不跑停損，等於裸露一個沒有保護的部位 ——
而這恰好發生在流動性不足的時候，也就是最需要停損的時候。

配套規則：

- 從 `ENTRY_PENDING` 出場時，出場口數 = **`filled_quantity`**，不是原始 `quantity`
- 未成交的剩餘進場委託由**引擎層**負責撤銷，不是策略的責任
  （見 `05-engine-core.md`：`OrderRouter` 在送出同一策略的出場單前，
  必須先撤掉該策略所有未成交的進場委託 —— 與緊急平倉「先刪單再平倉」同一原則）
- `ENTRY_PENDING` 直接出場時，狀態轉為 `EXIT_PENDING`（跳過 `IN_POSITION`），
  此轉換需列入 `_VALID_TRANSITIONS`

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
| 7a | 出場訊號唯一性（**核心不變式**） | `IN_POSITION` 期間**只能發出一個出場訊號**；發出後立即轉 `EXIT_PENDING`，後續 tick 不得再發出任何出場訊號 |
| 7b | `force_close()` 與 TP/SL 於同一 tick 皆成立 | 只發出**一個** `FORCE_CLOSE` 訊號，TP/SL 不得另外再發 |
| 7c | `on_fill()` 當下最新價已越過停損價 | **立刻在 `on_fill()` 回傳停損訊號**，不等下一個 tick |
| 8 | 移動停利推進後價格回落 | 停損價不回退，於推進後的價位出場 |
| 9 | 終態後再餵 tick | 回傳空 list，不拋例外 |
| 10 | 非法狀態轉換 | 拋 `StrategyError` |
| 11 | `force_close()` 於任一狀態 | `ARMED` → `CANCELLED`；`IN_POSITION` → 發出 `FORCE_CLOSE` 訊號 |
| 12 | OCO 上方先觸發 | 下方條件失效，後續跌破 `lower_trigger` 不得觸發 |
| 13 | `on_reject()` 於 `ENTRY_PENDING` | 轉 `CANCELLED` 並記錄原因 |
| 14 | 部分成交（`on_fill` 分兩次送達） | 均價正確、口數累加、狀態依上表轉換 |
| 15 | 部分成交後觸及停損（仍在 `ENTRY_PENDING`） | **必須發出停損訊號**，口數 = `filled_quantity`，狀態轉 `EXIT_PENDING` |
| 16 | 已發出停利訊號後，同一 tick 再呼叫 `force_close()` | 不得再發出第二個出場訊號 |
| 17 | `EXIT_PENDING` 期間持續餵入越過停損價的 tick | 一律回傳空 list |

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] `strategies/` 底下**不 import** `broker` 的具體實作、不 import `shioaji`、無 `time.sleep`
- [ ] 覆蓋率 ≥ 95%（純邏輯層，應接近全覆蓋）
- [ ] 情境 1（跳空）、7a（出場訊號唯一性）、15（部分成交下的停損）
      必須有繁中註解說明設計理由 —— 這三條是本模組的核心
