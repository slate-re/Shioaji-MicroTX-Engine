# Shioaji API 開發重點速查（在地化文件）

> 來源：<https://sinotrade.github.io/zh/>（登入 / 模擬模式 / 期貨下單 / 即時行情 / 主動回報 / 觸價委託）
> 整理日期：2026-08-08　適用版本：Shioaji 1.7+
> **本專案後續開發一律參考本檔，不再重複抓取官網（節省 Token）。**

---

## 目錄

1. [安裝與環境](#1-安裝與環境)
2. [登入與帳號](#2-登入與帳號)
3. [模擬模式](#3-模擬模式)
4. [商品檔（Contracts）](#4-商品檔contracts)
5. [即時行情訂閱與 Callback](#5-即時行情訂閱與-callback)
6. [期貨下單 / 改單 / 刪單](#6-期貨下單--改單--刪單)
7. [委託與成交回報](#7-委託與成交回報)
8. [觸價委託（條件單）實作模式](#8-觸價委託條件單實作模式)
9. [台指期商品規格對照](#9-台指期商品規格對照)
10. [常見陷阱與最佳實務](#10-常見陷阱與最佳實務)

---

## 1. 安裝與環境

```bash
pip install shioaji
# 憑證相關功能（正式下單）需額外安裝
pip install shioaji[speed]
```

- 支援 macOS / Linux / Windows，Python 3.8+。
- **開發（MacBook）與部署（Mac Mini）皆可用同一份程式碼**，差異只在 `.env`。

---

## 2. 登入與帳號

```python
import shioaji as sj

api = sj.Shioaji(simulation=True)      # True = 模擬環境（本專案預設）
accounts = api.login(
    api_key="YOUR_API_KEY",            # 一律由環境變數注入，禁止硬編碼
    secret_key="YOUR_SECRET_KEY",
    subscribe_trade=True,              # 自動訂閱委託/成交回報（預設 True）
    receive_window=30_000,             # 登入有效執行時間（毫秒）
)
```

### `login()` 參數

| 參數 | 型別 | 說明 |
|---|---|---|
| `api_key` | `str` | API 金鑰 |
| `secret_key` | `str` | 密鑰 |
| `subscribe_trade` | `bool` | 是否訂閱委託/成交回報（預設 `True`） |
| `receive_window` | `int` | 登入動作有效執行時間，預設 30,000 ms |

### 帳號

```python
api.list_accounts()          # 列出所有帳號
api.stock_account            # 預設證券帳號（StockAccount）
api.futopt_account           # 預設期貨帳號（FutureAccount）← 本專案使用
api.set_default_account(acc) # 切換預設帳號
api.logout()                 # 結束時務必登出（連線數有上限）
```

- 帳號型別：`S`=證券、`F`=期貨、`H`=複委託（API 不支援下單）。
- `signed=False` 代表尚未完成 API 簽署或測試報告，**無法下單**。
- 正式環境下單前需啟用憑證（`.pfx`），模擬環境不需要。

> ⚠️ **Sign data is timeout**：系統時間與伺服器差異過大，或登入超過 `receive_window`。
> Mac Mini 部署時請確認已開啟「自動設定日期與時間」。

---

## 3. 模擬模式

```python
api = sj.Shioaji(simulation=True)
```

模擬環境**可用**的 API：

| 類別 | 可用函式 |
|---|---|
| 行情 | `quote.subscribe` / `quote.unsubscribe` / `ticks` / `kbars` / `snapshots` / `scanners` |
| 下單 | `place_order` / `update_order` / `cancel_order` / `update_status` / `list_trades` |
| 帳務 | `list_positions` / `list_profit_loss` |

- 模擬環境**不支援**興櫃與零股。
- 模擬環境**不需憑證**，因此 clone 本專案的人可零成本測試。

---

## 4. 商品檔（Contracts）

```python
api.Contracts.Futures.TXF.TXFR1   # 臺股期貨（大台）近月連續
api.Contracts.Futures.MXF.MXFR1   # 小型臺指期貨（小台）近月連續
api.Contracts.Futures.TMF.TMFR1   # 微型臺指期貨（微台）近月連續
```

- `R1` = 近月連續、`R2` = 次月連續。Python SDK 會自動解析成實際代碼（如 `TXFF6`）。
- Contract 重要欄位：`code`、`target_code`、`delivery_month`、`limit_up`、`limit_down`、`reference`（參考價）、`multiplier`、`unit`。
- **`limit_up` / `limit_down` 可用於下單價格合法性檢查**（本專案 RiskManager 會使用）。

---

## 5. 即時行情訂閱與 Callback

### 訂閱

```python
api.subscribe(
    api.Contracts.Futures.TMF.TMFR1,
    quote_type=sj.QuoteType.Tick,     # Tick / BidAsk / Quote
)
api.unsubscribe(contract, quote_type=sj.QuoteType.Tick)
```

- 期貨/選擇權 `intraday_odd` 固定為 `False`。
- 訂閱不佔用流量額度；僅在開盤時段推送。

### Tick Callback（本專案條件單觸發的主要事件源）

```python
from shioaji import TickFOPv1, Exchange

@api.on_tick_fop_v1()
def on_tick(exchange: Exchange, tick: TickFOPv1) -> None:
    ...   # ⚠️ 官方明示：避免在 callback 內做重運算
```

傳統寫法：`api.set_on_tick_fop_v1_callback(fn)`

### `TickFOPv1` 欄位

| 欄位 | 型別 | 說明 |
|---|---|---|
| `code` | `str` | 商品代碼（實際月份碼，如 `TMFF6`） |
| `datetime` | `datetime` | 日期時間 |
| `open` / `high` / `low` / `close` | `Decimal` | 開/高/低/成交價 |
| `avg_price` | `Decimal` | 均價 |
| `underlying_price` | `Decimal` | 標的指數現價 |
| `volume` / `total_volume` | `int` | 單筆 / 總成交量（口） |
| `amount` / `total_amount` | `Decimal` | 單筆 / 總成交額 |
| `tick_type` | `int` | 內外盤別 `{1: 外盤, 2: 內盤, 0: 無法判定}` |
| `chg_type` | `int` | `{1: 漲停, 2: 漲, 3: 平盤, 4: 跌, 5: 跌停}` |
| `price_chg` / `pct_chg` | `Decimal` | 漲跌 / 漲跌幅 |
| `bid_side_total_vol` / `ask_side_total_vol` | `int` | 買/賣盤成交總量 |
| **`simtrade`** | `bool` | **試撮！必須過濾，否則會被假價格觸發條件單** |

### `BidAskFOPv1` 重點欄位

`bid_price[5]` / `bid_volume[5]` / `ask_price[5]` / `ask_volume[5]`、
`diff_bid_vol` / `diff_ask_vol`、`bid_total_vol` / `ask_total_vol`、`simtrade`。

> 條件單觸發後若要「打對手價」搶成交，用 `ask_price[0]`（買進）或 `bid_price[0]`（賣出）。

### `QuoteFOPv1`

Tick + BidAsk 的合併版，另有 `bid_side_total_cnt` / `ask_side_total_cnt`（成交筆數）。

---

## 6. 期貨下單 / 改單 / 刪單

### 下單

```python
contract = api.Contracts.Futures.TMF.TMFR1
order = sj.FuturesOrder(
    action=sj.constant.Action.Buy,                    # Buy / Sell
    price=23150,
    quantity=1,
    price_type=sj.constant.FuturesPriceType.LMT,      # LMT / MKT / MKP
    order_type=sj.constant.OrderType.ROD,             # ROD / IOC / FOK
    octype=sj.constant.FuturesOCType.Auto,            # Auto / New / Cover / DayTrade
    account=api.futopt_account,
)
trade = api.place_order(contract, order, timeout=30_000)
```

#### 參數對照

| 參數 | 選項 | 本專案用法 |
|---|---|---|
| `action` | `Buy` / `Sell` | 由 `Direction` 推導 |
| `price_type` | `LMT` 限價 / `MKT` 市價 / `MKP` 範圍市價 | 觸發進場用 `MKP`（避免市價滑價無上限）；出場停損用 `MKP` |
| `order_type` | `ROD` / `IOC` / `FOK` | 條件觸發進場用 `IOC`（不留單）；一般掛單 `ROD` |
| `octype` | `Auto` / `New` / `Cover` / `DayTrade` | 當沖用 `DayTrade`（享當沖保證金）；保守可用 `Auto` |

### 委託狀態（`trade.status.status`）

| 狀態 | 意義 |
|---|---|
| `PendingSubmit` | 傳送中 |
| `PreSubmitted` | 預約單 |
| `Submitted` | 傳送成功 |
| `Failed` | 失敗 |
| `Cancelled` | 已刪除 |
| `PartFilled` | 部分成交 |
| `Filled` | 完全成交 |

### 更新狀態 / 改單 / 刪單

```python
api.update_status(api.futopt_account)          # 主動更新；改/刪單前必須先呼叫（取得 ordno）
api.update_order(trade=trade, price=23160)     # 改價
api.update_order(trade=trade, qty=1)           # 改量（只能減量）
api.cancel_order(trade)                        # 刪單
api.list_trades()                              # 當日所有委託
```

> ⚠️ **改單/刪單前一定要先 `update_status`**，否則沒有 `ordno` 會失敗。

---

## 7. 委託與成交回報

透過 `api.set_order_callback(fn)` 接收，`OrderState` 分為：

- `OrderState.FuturesOrder`（`'FORDER'`）：委託回報
- `OrderState.FuturesDeal`（`'FDEAL'`）：成交回報

```python
from shioaji.constant import OrderState

def order_callback(state: OrderState, msg: dict) -> None:
    if state == OrderState.FuturesDeal:
        trade_id = msg["trade_id"]; price = msg["price"]; qty = msg["quantity"]
    elif state == OrderState.FuturesOrder:
        op_code = msg["operation"]["op_code"]     # "00" 成功，其他為失敗
        op_msg  = msg["operation"]["op_msg"]
```

### 委託回報結構

```
operation: {op_type: New/Cancel/UpdatePrice/UpdateQty, op_code: "00"=成功, op_msg: 錯誤訊息}
order:     {id, seqno, ordno, action, price, quantity, order_type, price_type,
            market_type: Day/Night, oc_type: New/Cover/Auto/DayTrade, account, combo}
status:    {id, exchange_ts, modified_price, cancel_quantity, order_quantity, web_id}
contract:  {security_type, code, full_code, exchange, delivery_month, option_right}
```

### 成交回報結構

```
{trade_id, seqno, ordno, exchange_seq, action, code, full_code,
 price, quantity, security_type, delivery_month, market_type, ts, ...}
```

- `委託回報.order.id` == `成交回報.trade_id`，用此關聯同一筆委託。
- ⚠️ **交易所回報優先順序：成交回報 > 委託回報**。立即成交時可能**先收到成交回報**。
  → 狀態機必須容忍「成交先到」，不可假設順序。

---

## 8. 觸價委託（條件單）實作模式

Shioaji **原生沒有條件單 API**，官方範例是「訂閱行情 → 在 callback 中比價 → 條件成立才 `place_order`」。

官方最小範例（`sinotrade.github.io/zh/tutor/advanced/touchorder/`）：

```python
class TouchOrder:
    def __init__(self, api, condition):
        self.flag = False
        self.api = api
        self.order = condition.order
        self.contract = condition.contract
        self.touch_price = condition.touch_price
        self.api.quote.subscribe(self.contract)
        self.api.quote.set_quote_callback(self.touch)

    def touch(self, topic, quote):
        price = quote["Close"][0]
        if price == self.touch_price and not self.flag:
            self.flag = True
            self.api.place_order(self.contract, self.order)
            self.api.quote.unsubscribe(self.contract)
```

### 官方範例的問題（本專案必須改進）

| 問題 | 本專案對策 |
|---|---|
| `price == touch_price` 精確相等比對，跳空時**永遠不會觸發** | 改用**穿越比較**：多單 `price >= trigger`、空單 `price <= trigger` |
| 未過濾 `simtrade` 試撮價 | callback 第一行 `if tick.simtrade: return` |
| `flag` 非執行緒安全，callback 併發可能重複下單 | `threading.Lock` + 冪等狀態機 |
| 觸發後才 `place_order`，callback 內做 I/O 會阻塞行情執行緒 | callback 只推事件進 `queue`，由 worker thread 執行下單 |
| 無出場、無風控 | 獨立 `PositionTracker` + `RiskManager` |

---

## 9. 台指期商品規格對照

| 商品 | 代碼 | 連續近月 | 每點價值 | 說明 |
|---|---|---|---|---|
| 臺股期貨（大台） | `TXF` | `TXFR1` | NT$200 | 流動性最佳，保證金最高 |
| 小型臺指期貨（小台） | `MXF` | `MXFR1` | NT$50 | 散戶主流 |
| 微型臺指期貨（微台） | `TMF` | `TMFR1` | NT$10 | 2022 上市，練習/小額最適 |

- **最小跳動單位（Tick Size）：1 點**（三者相同）。
- 交易時段：**日盤 08:45–13:45**、**夜盤 15:00–次日 05:00**。
- 當沖強制平倉時間建議設在 **13:40**（日盤收盤前 5 分鐘），留出滑價與重試餘裕。

> 注意：`MXF` 是**小台**、`TMF` 才是**微台**，勿混用。本專案以 `config` 的 `SYMBOL` 參數切換三者。

---

## 10. 常見陷阱與最佳實務

1. **`simtrade=True` 的試撮資料必須過濾**——盤前 08:30–08:45 與夜盤前試撮價格會亂跳，是條件單誤觸發的頭號兇手。
2. **Callback 內禁止重運算 / 阻塞 I/O**——官方明文警告。行情執行緒被卡住會漏 tick。
3. **改單/刪單前先 `update_status()`** 取得 `ordno`。
4. **成交回報可能早於委託回報**，狀態機需容錯。
5. **連線數有上限**，程式結束務必 `api.logout()`（用 `try/finally` 或 signal handler）。
6. **`update_order` 只能減量**，不能加量。
7. 下單價格須落在 `contract.limit_up` / `limit_down` 之間，否則直接被拒。
8. **模擬環境的成交是模擬撮合**，成交價與實盤會有差異，回測結論不可直接外推。
9. Mac Mini 長跑需處理**斷線重連**（`set_event_callback` 監控 Session down）與**跨日重登入**。
10. API 金鑰一律走環境變數；`.pfx` 憑證檔與 `.env` 永不進版控。

---

## 附錄：本專案常用 Import

```python
import shioaji as sj
from shioaji import TickFOPv1, BidAskFOPv1, Exchange
from shioaji.constant import (
    Action, FuturesPriceType, OrderType, FuturesOCType, OrderState, QuoteType,
)
```
