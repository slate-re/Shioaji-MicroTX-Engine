# 任務 11 — Scalp 支援絕對價格的停利停損

## 背景：使用者要的不是點數，是價位

目前 `ScalpStrategy` 的 TP/SL 是**點數偏移**，且以**實際成交價**為基準：

```
--trigger 46500 --tp 50 --sl 30
跳空成交在 46550  →  停利 46600、停損 46520   ← 停損跟著成交價滑走
```

使用者實際的思考方式是**直接指定價位**（那些價位通常有技術面理由）：

```
--trigger 46500 --tp 46600 --sl 46400
跳空成交在 46550  →  停利 46600、停損 46400   ← 風險底線不動
```

### 這不只是輸入格式的差異

| | 點數模式 | 絕對價格模式 |
|---|---|---|
| 成交價比觸發價差 50 點 | 停損**跟著往下滑 50 點**，實際風險變大 | 停損不動，**風險上限固定** |
| 快市跳空 | 風險隨滑價擴張 | 風險有硬性底線 |

**絕對價格模式在跳空時的風險控制較佳** —— 差的成交價只會壓縮獲利空間，不會擴大虧損。

---

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/strategies/scalp.py` | **擴充**：支援兩種 TP/SL 表達方式 |
| `src/microtx/cli/commands.py` | **擴充**：`--tp-price` / `--sl-price` |
| `src/microtx/strategies/oco.py` | **擴充**：兩腿沿用同一機制 |
| `tests/test_scalp_strategy.py`、`tests/test_cli.py` | **擴充** |
| `README.md` | **擴充**：使用方式 |

---

## 1. 介面

**兩種模式並存，但不可混用。**

```bash
# 點數模式（既有，保留）
microtx run --strategy scalp --direction long --trigger 46500 --tp 50 --sl 30

# 絕對價格模式（新增）
microtx run --strategy scalp --direction long --trigger 46500 --tp-price 46600 --sl-price 46400
```

```python
class ScalpStrategy(Strategy):
    def __init__(
        self,
        *,
        spec: FuturesSpec,
        direction: Direction,
        trigger_price: float,
        take_profit_points: int | None = None,
        stop_loss_points: int | None = None,
        take_profit_price: float | None = None,     # 新增
        stop_loss_price: float | None = None,       # 新增
        quantity: int = 1,
        trailing_points: int | None = None,
    ) -> None: ...
```

### 建構驗證（啟動時就爆，不留到執行期）

| 檢查 | 失敗行為 |
|---|---|
| TP 只能擇一（`points` 或 `price`），不可同時給或都不給 | `ValueError` |
| SL 同上 | `ValueError` |
| **TP 與 SL 必須使用同一種模式** | `ValueError`（不可 `--tp 50 --sl-price 46400`） |
| 多單：`sl_price < trigger_price < tp_price` | `ValueError` |
| 空單：`tp_price < trigger_price < sl_price` | `ValueError` |
| 絕對價格須為正且對齊 tick（整數點） | `ValueError` |

CLI 端也要擋：同時給 `--tp` 與 `--tp-price` → 退出碼 2 並說明。

---

## 1b. OCO 的絕對價格介面

OCO 有兩條方向相反的進場腿，**無法共用同一組 TP/SL** ——
因為 `upper_trigger > lower_trigger`，同一個 TP 不可能同時高於 upper 又低於 lower。

因此絕對價格模式需要**四個獨立欄位**：

```python
class OcoStrategy(Strategy):
    def __init__(
        self,
        *,
        spec: FuturesSpec,
        upper_trigger: float,          # 向上突破 → 做多
        lower_trigger: float,          # 向下跌破 → 做空
        # 點數模式（既有，兩腿共用）
        take_profit_points: int | None = None,
        stop_loss_points: int | None = None,
        # 絕對價格模式（新增，每腿獨立）
        long_take_profit_price: float | None = None,
        long_stop_loss_price: float | None = None,
        short_take_profit_price: float | None = None,
        short_stop_loss_price: float | None = None,
        quantity: int = 1,
    ) -> None: ...
```

```bash
microtx run --strategy oco --upper 46500 --lower 46300 \
    --long-tp-price 46600 --long-sl-price 46450 \
    --short-tp-price 46200 --short-sl-price 46350
```

### 驗證規則

| 規則 | 說明 |
|---|---|
| 四個絕對價位**必須一起提供** | 只給部分 → `ValueError` |
| 不可與點數模式混用 | 同時給 `take_profit_points` 與任一絕對價位 → `ValueError` |
| **多腿**：`long_sl < upper_trigger < long_tp` | 沿用 Scalp 的多單規則 |
| **空腿**：`short_tp < lower_trigger < short_sl` | 沿用 Scalp 的空單規則 |
| `upper_trigger > lower_trigger` | 既有規則，不變 |

⛔ **不要新增跨腿的約束**（例如要求 `long_sl > lower_trigger`）。
一腿觸發後另一腿即失效，兩腿的價位彼此獨立，多加限制只會擋掉合法的寬停損設定。

### 實作方式

`OcoStrategy` 既有設計是**委派給兩個 `ScalpStrategy` 實例**。
絕對價格模式沿用此結構：建構時把對應的四個價位分別傳給兩腿，
**驗證邏輯由 `ScalpStrategy` 自己完成** —— OCO 只負責檢查
「四個都給了」與「沒和點數模式混用」。

⛔ 不要在 `oco.py` 裡複製一份價位順序驗證。單一來源。

---

## 2. 語意

### 出場價位的決定時機

| 模式 | 出場價位 | 決定於 |
|---|---|---|
| 點數 | `entry_price ± points` | **成交後**（依實際成交價） |
| 絕對價格 | 使用者給的值 | **建構時**（固定不變） |

絕對價格模式下，`on_fill()` **不重算** TP/SL。

### 觸發判定不變

仍是穿越比較（多單 `price >= tp_price` 停利、`price <= sl_price` 停損），
與既有邏輯共用同一段程式碼 —— 只有價位來源不同。

### 移動停利（`trailing_points`）

**絕對價格模式下不支援 `trailing_points`**，同時給定則 `ValueError`。

理由：移動停利的語意是「停損隨最有利價推進」，本質上是點數偏移。
與「固定價位」混用會產生「到底以哪個為準」的歧義。要用移動停利就用點數模式。

### ⚠️ 成交價已越過出場價位的情況

跳空可能導致成交價直接超過 `tp_price` 或跌破 `sl_price`：

```
多單 trigger 46500 / tp 46600 / sl 46400
市場從 46480 直接跳到 46650 → 觸發進場，成交在 46650（已越過 tp_price）
```

**處置：`on_fill()` 立即發出停利訊號**（`OrderIntent.TAKE_PROFIT`），
不等下一個 tick。若成交價低於 `sl_price` 則立即發停損。

這與 `04-strategies.md` 情境 7c（成交當下已越過停損價）是同一條規則，
只是這裡多了「已越過停利價」的對稱情況。

---

## 3. `describe()` 輸出

需能一眼分辨模式，供 CLI 與 TUI 顯示：

```
點數模式    ：做多 觸發46500 TP+50 SL-30
絕對價格模式：做多 觸發46500 TP@46600 SL@46400
```

---

## 測試要求

| # | 情境 | 期望 |
|---|---|---|
| 1 | 絕對價格多單正常流程 | 觸發 46500 → 成交 46500 → 價格到 46600 發停利 |
| 2 | 絕對價格空單 | `tp_price < trigger < sl_price`，方向判定正確 |
| 3 | **跳空成交，停損不滑動** | 成交在 46550，停損仍為 46400（**不是** 46520） |
| 4 | **成交價已越過 `tp_price`** | `on_fill()` **立即**回傳停利訊號，不等下一 tick |
| 5 | 成交價已跌破 `sl_price` | `on_fill()` 立即回傳停損訊號 |
| 6 | 同時給 `points` 與 `price` | `ValueError` |
| 7 | 兩者都不給 | `ValueError` |
| 8 | TP 用 price、SL 用 points | `ValueError`（不可混用） |
| 9 | 多單 `sl_price > trigger` | `ValueError` |
| 10 | 空單價位順序錯誤 | `ValueError` |
| 11 | 絕對價格 + `trailing_points` | `ValueError` |
| 12 | 點數模式維持原行為 | **既有測試全數不得修改且仍通過** |
| 13 | CLI 同時給 `--tp` 與 `--tp-price` | 退出碼 2，訊息說明只能擇一 |
| 14 | `describe()` 兩種模式輸出可辨識 | 分別含 `TP+` 與 `TP@` |
| 15 | OCO 兩腿使用絕對價格 | 各腿價位獨立驗證 |

> **情境 3 與 12 是驗收核心。**
> 3 是本任務的價值所在（風險底線不隨滑價移動）；
> 12 確保既有點數模式零回歸。

---

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] **`tests/test_scalp_strategy.py` 的既有測試一行未改且全部通過**
- [ ] `strategies/` 仍為純邏輯（無 I/O、無執行緒、不 import broker 具體實作）
- [ ] 覆蓋率維持 ≥ 95%
- [ ] 交付時說明：為什麼絕對價格模式不支援 `trailing_points`
