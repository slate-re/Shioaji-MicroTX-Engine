# 任務 12 — 進場／停利／停損可分別選擇市價或限價

## 現況

**目前所有委託都是 `MKP` + `IOC`**（範圍市價、不留單），沒有任何限價單：

```python
# engine.py
price_type = PriceType.LMT if signal.limit_price is not None else PriceType.MKP
time_in_force = TimeInForce.ROD if price_type is LMT else TimeInForce.IOC
```

策略發出的 `Signal` 從不帶 `limit_price`，因此永遠落在 `MKP` 分支。

本任務讓使用者**分別**為三條腿選擇委託方式。

---

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/enums.py` | **擴充**：新增 `ExecutionStyle` |
| `src/microtx/strategies/scalp.py` | **擴充**：三個 style 參數，出場／進場 Signal 帶 `limit_price` |
| `src/microtx/strategies/oco.py` | **擴充**：轉交兩腿 |
| `src/microtx/cli/commands.py` | **擴充**：`--entry-order` / `--tp-order` / `--sl-order` |
| `tests/test_scalp_strategy.py`、`tests/test_cli.py` | **擴充** |
| `README.md`、`docs/operations.md` | **擴充** |

---

## 1. 介面

```python
class ExecutionStyle(str, Enum):
    """出場／進場委託的送出方式。"""

    MARKET = "MARKET"
    """範圍市價（MKP）+ IOC。會成交，滑價有上限。本專案預設。"""

    LIMIT = "LIMIT"
    """限價（LMT）+ ROD。價格明確，但**可能不成交**。"""
```

```bash
# 全部維持現況（預設）
microtx run --strategy scalp --direction long --trigger 46500 \
    --tp-price 46600 --sl-price 46400

# 進場不追高、停利要拿到指定價、停損照樣市價
microtx run --strategy scalp --direction long --trigger 46500 \
    --tp-price 46600 --sl-price 46400 \
    --entry-order limit --tp-order limit --sl-order market
```

三個參數預設皆為 `market`，**不指定時行為與現在完全相同**。

### 各腿的限價價位來源

| 腿 | `LIMIT` 時掛在哪 | 語意 |
|---|---|---|
| 進場 | `trigger_price` | 突破就進，但**不追高**；追不到就放棄 |
| 停利 | `take_profit_price` | 要這個價或更好，不到就繼續抱 |
| 停損 | `stop_loss_price` | ⚠️ 見下方警告 |

---

## 2. 停損用限價的風險（**必須讓使用者看見**）

```
停損掛 LMT 46400
市場從 46420 直接跳到 46350
→ 委託掛在 46400 沒成交，部位還在，而且已經比停損價更差
```

**停損不成交，會把「有限的虧損」變成「無限的虧損」。**
這與 `06-emergency-close.md` 堅持 `MKP` + `IOC`、以及
`emergency_use_market_order=False` 時要掛**漲跌停價**而非近價，是同一個道理。

`MKP`（範圍市價）**本來就不是無上限的市價** —— 它有內建價格區間保護。
使用者已經同時擁有「會成交」與「滑價可控」。

### 處置：開放但不沉默

⛔ **不阻擋** —— 使用者的錢，使用者決定。

✅ 但 `--sl-order limit` 時，**啟動當下必須輸出 WARNING**：

```
⚠️  停損採限價委託（LMT @ 46400）。快市穿價時可能不成交，
    部位將持續裸露。建議改用 --sl-order market（範圍市價，滑價有上限）。
```

同時 `describe()` 與 `status.json` 的策略摘要要標示出來，
讓 `microtx watch` 上看得到目前是限價停損。

---

## 3. 限制

### `LIMIT` 僅適用於絕對價格模式

點數模式的 TP/SL 是**成交後**才算出來的浮動價位，
與「我要這個價」的意圖對不上，且需等成交回報才能送單，時序上多一跳。

| 組合 | 結果 |
|---|---|
| 絕對價格 + `LIMIT` | ✅ |
| 絕對價格 + `MARKET` | ✅ |
| 點數模式 + `MARKET` | ✅ |
| **點數模式 + `LIMIT`** | ⛔ `ValueError`，訊息說明限價需搭配絕對價格 |

進場的 `LIMIT` 不受此限（掛在 `trigger_price`，該值一定存在）。

### 強平與緊急平倉不受影響

`FORCE_CLOSE` 與 `EMERGENCY` **一律 `MKP` + `IOC`**，
不受任何 `ExecutionStyle` 設定影響。

理由：那兩條路徑的存在意義就是「一定要出場」。
讓使用者設定去影響它們，等於允許把安全裝置關掉。

⛔ 不得為強平／緊急平倉新增任何委託方式選項。

### 成交價已越過出場價位時

任務 11 定義了「成交價已越過 TP/SL → `on_fill()` 立即發出場訊號」。
此時**強制使用 `MARKET`**，忽略 `LIMIT` 設定 ——
價位已經穿過去了，掛限價在那裡等於掛一張不會成交的單。
需記 INFO 日誌說明降級原因。

---

## 4. 未成交的限價單怎麼辦

`LIMIT` + `ROD` 會留在市場上。三種既有機制會處理它：

| 情況 | 處置 | 來源 |
|---|---|---|
| 13:40 強制平倉 | `OrderRouter` 送出場單前先撤掉同策略未成交委託 | `05-engine-core.md` |
| `microtx panic` / `flatten` | `cancel_all_orders()` 後才平倉 | `06-emergency-close.md` |
| 收盤 | 交易所自動作廢，回報 `CancelEvent(reason="session_end")` | `07-shioaji-gateway.md` |

⛔ **不要為此新增逾時撤單機制。** 既有三條路徑已足夠，
再加一層只會多一個會與緊急平倉互動的狀態。

---

## 測試要求

| # | 情境 | 期望 |
|---|---|---|
| 1 | 三個參數皆未指定 | **行為與本任務實作前完全相同**（`MKP` + `IOC`） |
| 2 | `--tp-order limit` | 停利 Signal 帶 `limit_price = take_profit_price`，引擎送 `LMT` + `ROD` |
| 3 | `--sl-order limit` | 同上，且**啟動時輸出 WARNING** |
| 4 | `--entry-order limit` | 進場 Signal 帶 `limit_price = trigger_price` |
| 5 | 混合設定 | 三條腿可各自不同，互不影響 |
| 6 | **點數模式 + `--tp-order limit`** | `ValueError`，訊息含「限價需搭配絕對價格」 |
| 7 | 點數模式 + `--entry-order limit` | ✅ 允許（掛在 trigger_price） |
| 8 | **成交價已越過 TP，且設 `LIMIT`** | **降級為 `MARKET`**，記 INFO 日誌 |
| 9 | 同上但已跌破 SL | 同樣降級 |
| 10 | **`FORCE_CLOSE` 不受影響** | 即使全設 `LIMIT`，強平仍為 `MKP` + `IOC` |
| 11 | **`EMERGENCY` 不受影響** | 同上 |
| 12 | `describe()` 標示 | 輸出可辨識目前的委託方式 |
| 13 | OCO 兩腿各自設定 | 轉交正確，互不干擾 |
| 14 | CLI 參數值非法 | 退出碼 2，列出可用值 |

> **情境 1、10、11 是驗收核心。**
> 1 確保零回歸；10/11 確保安全裝置不可被設定關掉。

---

## 驗收條件

- [ ] 四項驗收指令全綠（3.10 / 3.11 matrix）
- [ ] **既有測試一行未改且全部通過**
- [ ] `strategies/` 仍為純邏輯
- [ ] 覆蓋率維持 ≥ 95%
- [ ] 交付時說明：為什麼強平與緊急平倉不開放此選項
