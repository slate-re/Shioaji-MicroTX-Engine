# 任務 07 — ShioajiGateway（接真實 API）

## 目標

實作 `BrokerGateway` 的 Shioaji 版本。這是**唯一**允許 `import shioaji` 的模組。

> 所有 API 用法請查 `docs/shioaji_guide.md`，**不要上網查官方文件**。

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/broker/shioaji_gateway.py` | 新增 |
| `src/microtx/broker/_mapping.py` | 新增（列舉雙向轉換） |
| `tests/test_shioaji_mapping.py` | 新增（純轉換測試，免連線） |
| `tests/integration/test_shioaji_gateway.py` | 新增（標記 `@pytest.mark.integration`） |

---

## _mapping.py — 列舉轉換

把本專案列舉與 Shioaji 常數雙向對映，集中在一處：

| 本專案 | Shioaji |
|---|---|
| `Direction.LONG` / `SHORT` | `Action.Buy` / `Action.Sell` |
| `PriceType.LMT/MKP/MKT` | `FuturesPriceType.LMT/MKP/MKT` |
| `TimeInForce.ROD/IOC/FOK` | `OrderType.ROD/IOC/FOK` |
| 所有 `OrderIntent` → `octype` | 由 `settings.futures_octype` 決定（見下） |

要點：

- 提供 `to_shioaji_*()` 與 `from_shioaji_*()` 成對函式
- 未知值一律拋 `ValueError`，**不要靜默 fallback**（靜默 fallback 會變成下錯單）

#### `octype` 的正式契約

`config.py` 新增（本任務允許修改 `config.py`）：

```python
futures_octype: str = Field(
    default="Auto",
    description="期貨倉別：Auto（自動判斷新倉/平倉，預設）或 DayTrade（當沖，需當沖資格）",
)

@field_validator("futures_octype")
@classmethod
def _validate_octype(cls, value: str) -> str:
    allowed = {"Auto", "DayTrade"}
    if value not in allowed:
        raise ValueError(f"futures_octype 必須是 {sorted(allowed)} 之一")
    return value
```

裁決：**所有意圖一律使用同一個 `octype`，不依 intent 分歧。**

- 預設 `Auto`：Shioaji 自動判斷新倉／平倉，官方範例即用此值，最不易出錯
- `DayTrade` 為選用：可享當沖保證金，但**需帳戶具備當沖資格**，
  且進出場倉別必須一致才能正確沖銷

⛔ **不要**實作「進場 `DayTrade`、出場 `Cover`」的分歧邏輯。
兩者混用時的沖銷行為我們無法在模擬環境完整驗證，
猜錯的後果是部位沒沖掉、保證金爆掉。單一值最安全。

> `.env.example` 需加上 `FUTURES_OCTYPE=Auto` 並註明
> 「改為 DayTrade 前請先在模擬環境確認帳戶當沖資格」。

---

## shioaji_gateway.py

### 登入

```python
api = sj.Shioaji(simulation=settings.simulation)
api.login(
    api_key=settings.shioaji_api_key.get_secret_value(),
    secret_key=settings.shioaji_secret_key.get_secret_value(),
    subscribe_trade=True,
    receive_window=30_000,
)
if settings.is_live:
    api.activate_ca(ca_path=..., ca_passwd=..., person_id=...)
```

要求：

- 金鑰**只在呼叫當下**用 `get_secret_value()` 取出，不存成一般屬性
- 登入後檢查 `api.futopt_account.signed is True`，否則拋 `BrokerError`
  （未簽署的帳號下單一定失敗，早點爆掉比較好）
- 憑證啟用只在 `settings.is_live` 為真時執行

### 行情

```python
@api.on_tick_fop_v1()
def _on_tick(exchange: Exchange, tick: TickFOPv1) -> None:
    self._tick_callback(RawTick(
        code=tick.code,
        timestamp=<tz-aware datetime, Asia/Taipei>,
        price=float(tick.close),
        volume=tick.volume,
        total_volume=tick.total_volume,
        tick_type=tick.tick_type,
        simtrade=tick.simtrade,       # ← 原樣傳遞，過濾是 market 層的責任
    ))
```

⛔ callback 內只做欄位搬運。**不要**在這裡過濾、記錄或運算。

`Decimal` → `float` 的轉換在此完成（Shioaji 回傳 `Decimal`，
下游全部用 `float`，轉換點只有這一處）。

### 下單

```python
def place_order(self, request: OrderRequest) -> OrderAck:
    contract = self._resolve_contract(request.symbol)
    self._validate_price_limits(request, contract)
    order = sj.FuturesOrder(
        action=to_shioaji_action(request.action),
        price=request.price or 0,
        quantity=request.quantity,
        price_type=to_shioaji_price_type(request.price_type),
        order_type=to_shioaji_time_in_force(request.time_in_force),
        octype=to_shioaji_octype(request.intent),
        account=self._api.futopt_account,
    )
    trade = self._api.place_order(contract, order, timeout=30_000)
    self._client_id_map[request.client_id] = trade.status.id
    return OrderAck(...)
```

### 商品解析與快取（規格已依 1.7.0 修正）

```python
contract = api.contracts.get(symbol)        # 精簡 Contract，足供下單與訂閱
info = api.contracts.info(contract)         # FuturesInfo，含 limit_up / limit_down
```

- 用**小寫** `api.contracts`（1.7.0 新 API）。大寫 `api.Contracts` 是相容層，新程式碼不用
- `get()` 查無代碼時回傳 `None` → 拋 `BrokerError`，不可讓 `None` 往下流
- `limit_up` / `limit_down` 在 **`info()`** 的回傳物件上，不在 `Contract` 上；
  `get_price_limits()` 必須走 `info()`
- `Contract` 可長期快取；`info()` 的漲跌停建議加**短期 TTL**（如 60 秒），
  因為交易所有分階段漲跌停

#### ⛔ 不要實作「跨日重新載入商品檔」

先前規格要求跨日重載，**這是錯的**。`docs/shioaji_guide.md` §11 記載官方說明：

> 「1.7.0 起，商品合約會在有更新時自動保持最新，您不必再關心更新時機。」

SDK 已自動處理。自己再寫一套只會製造 bug 與不一致。

#### ⛔ 不要對指數期貨呼叫 `tick_bands()`

台指期為 `tick_basis='fixed'`、`tick=1.0`，呼叫 `tick_bands()` 會拋 `ShioajiValueError`。
直接用 `info.tick` 即可。

### 改單 / 刪單

```python
# ⚠️ 前置必須先 update_status 取得 ordno，否則失敗
self._api.update_status(self._api.futopt_account)
self._api.cancel_order(trade)
```

`cancel_all_orders()`：先 `update_status()` → `list_trades()` →
對狀態為 `Submitted` / `PreSubmitted` / `PartFilled` 的逐一 `cancel_order()` →
回傳成功筆數。**單筆失敗不可中斷整批**（緊急平倉依賴它盡量刪光）。

### 回報處理

```python
def _order_callback(self, state: OrderState, msg: dict) -> None:
    # 轉成 FillEvent / RejectEvent / AckEvent / CancelEvent，非阻塞入佇列
```

必須處理：

- `OrderState.FuturesDeal` → `FillEvent`
- `OrderState.FuturesOrder` 且 `operation.op_code != "00"` → `RejectEvent`
- 用 `order.id` / `trade_id` 反查 `client_id`
- ⚠️ **成交回報可能早於委託回報**，`client_id` 反查失敗時要能容忍
  （記錄 WARNING 並以 `broker_order_id` 為主鍵，不可拋例外）

#### `AckEvent` / `CancelEvent` 的產生規則

| 條件 | 產生 |
|---|---|
| `op_type == "New"` 且 `op_code == "00"` | `AckEvent`，`exchange_order_no = order.ordno` |
| `op_type == "Cancel"` 且 `op_code == "00"` | `CancelEvent`（`reason` 依下表推導） |
| `op_code != "00"` | `RejectEvent` |

`CancelEvent` 的數量欄位取自委託回報 `status`：

```python
cancelled_quantity = status["cancel_quantity"]
remaining_quantity = status["order_quantity"] - status["cancel_quantity"] - deal_quantity
```

#### ⚠️ `reason` 沒有對應的 SDK 欄位 —— 由本專案推導

Shioaji 的委託回報**不區分**「使用者刪單」與「IOC 未成交自動失效」，
兩者都是 `op_type == "Cancel"`。因此 `reason` 是**本專案的推導值**，規則如下（依序）：

```python
if client_id in self._pending_cancels:      # 本程式主動送出過 cancel_order()
    reason = "user"
elif original_request.time_in_force is TimeInForce.IOC:
    reason = "ioc_expired"
elif original_request.time_in_force is TimeInForce.FOK:
    reason = "fok_expired"
else:                                        # ROD 卻被取消 → 只可能是收盤作廢
    reason = "session_end"
```

- `ShioajiGateway` 需維護 `_pending_cancels: set[str]`，
  在 `cancel_order()` 送出時加入、收到對應 `CancelEvent` 後移除
- 若查不到原始 request（例如重啟後收到舊委託的回報）→ `reason = ""`，
  **不可猜測**。空字串代表「無法判定」，下游需容忍
- 此推導規則必須寫在 `_mapping.py` 的 docstring 中，
  並註明「Shioaji 未提供此資訊，屬本專案推導」，避免日後被誤認為 SDK 語意

### 斷線重連（規格已修正：不要自己寫重連迴圈）

`docs/shioaji_guide.md` §14 記載官方說明：

> 「不用擔心在不用任何的設定下，我們將重連預設為 50 次。」

底層 Solace SDK **已內建自動重連**。本專案的責任只有「監聽事件 + 重連後重新訂閱」。

```python
@api.quote.on_event
def _on_event(resp_code: int, event_code: int, info: str, event: str) -> None:
    ...
```

| Event Code | 處理 |
|---|---|
| `0` UP_NOTICE | `is_connected = True` |
| `1` DOWN_ERROR / `2` CONNECT_FAILED / `12` RECONNECTING | `is_connected = False` |
| `13` RECONNECTED | `is_connected = True` **並重新訂閱行情** |
| `16` SUBSCRIPTION_OK | 記 DEBUG |
| `17` VIRTUAL_ROUTER_NAME_CHANGED | ⚠️ **強制重新訂閱** |

> Code 17 特別重要：Virtual Router 改名後既有訂閱會**靜默失效** ——
> 行情停止推送卻沒有任何錯誤訊息。漏掉這條，引擎會在完全「正常」的狀態下瞎掉。

- `is_connected` 為 `False` 期間，`place_order` 拋 `ConnectionLostError`，
  讓 `OrderRouter` 拒絕新單
- event callback 也適用「不得阻塞」紀律：只更新旗標與推事件，不做 I/O
- ⛔ **不新增任何 `reconnect_max_attempts` 之類的設定** —— 重連不是我們的職責

---

## 部位與未成交委託的欄位對映（正式契約）

### `list_positions()` → `Position`

`FuturePosition` 只有七個欄位（見 `docs/shioaji_guide.md` §12）：

| Shioaji `FuturePosition` | 本專案 `Position` |
|---|---|
| `code` | `code` |
| `direction`（`Action.Buy/Sell`） | `direction`（`Direction.LONG/SHORT`） |
| `quantity`（恆為正） | `quantity`（恆為正） |
| `price`（平均成本） | `average_price` |
| `pnl` | `unrealized_pnl` |
| `id` / `last_price` | 不使用 |

> 兩邊的「quantity 恆為正、方向由 direction 表示」是**同一個不變式**，
> 直接對映即可，不需任何正負號轉換技巧。

呼叫方式：`api.list_positions(account=api.futopt_account, timeout=5000)`

### `list_trades()` → `OpenOrder`

先 `api.update_status(api.futopt_account)`，再 `api.list_trades()`，
篩選 `status.status` ∈ {`Submitted`, `PreSubmitted`, `PartFilled`}：

| Shioaji | 本專案 `OpenOrder` |
|---|---|
| `trade.status.id`（= `order.id`） | `broker_order_id` |
| 由 `_client_id_map` 反查 | `client_id`（查無則 `None`） |
| `trade.contract.code` | `code` |
| `trade.order.action` | `action` |
| `trade.order.price` | `price` |
| `trade.status.order_quantity` | `quantity` |
| `trade.status.deal_quantity` | `filled_quantity` |

`cancel_all_orders()` 即以上述清單逐一 `cancel_order(trade)`，
**單筆失敗不可中斷整批**（緊急平倉依賴它盡量刪光），回傳成功筆數。

---

### 登出

`disconnect()` 必須呼叫 `api.logout()`。Shioaji 連線數有上限，不登出會累積佔用。

---

## 測試要求

### 免連線測試（`tests/test_shioaji_mapping.py`）

- 所有列舉雙向轉換正確
- 未知值拋 `ValueError`
- `Decimal` → `float` 轉換不失精度（台指期為整數點位，應無誤差）
- `RawTick` 建構：用假的 `TickFOPv1` 具名元組驗證欄位搬運正確

### 整合測試（`@pytest.mark.integration`，預設跳過）

- 模擬環境登入 → 訂閱 → 收到至少一筆 tick → 下單 → 查詢 → 刪單 → 登出
- 測試檔開頭必須有 skip 條件：無 `.env` 或 `SHIOAJI_API_KEY` 為空時自動跳過
- **整合測試一律只在 `simulation=True` 下執行**，程式碼中硬性斷言此條件

---

---

## 07b 補強：`shioaji` 改為選用依賴（lazy import）

### 問題

目前 `shioaji` 是 `pyproject.toml` 的**必要依賴**，且 `_mapping.py` 在模組層級
`import shioaji`。因此在未安裝 shioaji 的環境中：

```
ModuleNotFoundError: No module named 'shioaji'
ERROR collecting tests/test_shioaji_mapping.py
ERROR collecting tests/integration/test_shioaji_gateway.py
```

這削弱了兩項本專案刻意建立的價值：

1. **「clone 下來就能跑 demo」的承諾被打折**
   `08-cli-deploy.md` 要求 `git clone && pip install -e . && microtx demo` 三步可跑。
   若 shioaji 是硬性依賴，只想看一眼 Demo 的人（例如面試官）
   得先安裝一整套券商 SDK。這正是 `PaperGateway` 存在的理由要避免的摩擦。

2. **分層隔離的效益被浪費**
   架構刻意把 shioaji 侷限在兩個檔案，但只要依賴是硬性的，
   「策略層零券商依賴」在**安裝層面**就不成立。隔離只做到一半。

### 修正

**(a) `pyproject.toml`：移出必要依賴，新增 `live` extra**

```toml
dependencies = [
    "pydantic>=2.5",
    "pydantic-settings>=2.1",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
live = ["shioaji>=1.2.0"]     # 實際連線永豐才需要
dev  = [...]                  # 維持現狀
```

安裝方式因此分成兩種：

| 指令 | 能做什麼 |
|---|---|
| `pip install -e ".[dev]"` | 離線 Demo、全部單元測試、開發 |
| `pip install -e ".[dev,live]"` | 以上再加真實連線（模擬或實盤） |

**(b) `_mapping.py` 與 `shioaji_gateway.py`：改為 lazy import**

```python
def _require_shioaji() -> ModuleType:
    """延遲載入 shioaji，未安裝時給出可操作的錯誤訊息。"""
    try:
        import shioaji
    except ImportError as exc:
        raise BrokerError(
            "未安裝 shioaji。若要連線永豐請執行：pip install -e \".[live]\"；"
            "若只是要跑離線 Demo 或測試，請改用 PaperGateway。"
        ) from exc
    return shioaji
```

- 模組層級**不得**有 `import shioaji`
- 型別註解用 `if TYPE_CHECKING:` 區塊 import，執行期不觸發
- 錯誤訊息必須告訴使用者**下一步怎麼做**，不要只丟 `ModuleNotFoundError`

**(c) `tests/test_shioaji_mapping.py`：不得因缺 shioaji 而 collection error**

該檔的定位是「免連線測試」，應進一步做到「**免安裝 SDK**」：
純轉換邏輯（列舉對映、`reason` 推導、`Decimal→float`）本來就不需要真的 shioaji。

- 若轉換函式必須碰 shioaji 常數 → 以 `pytest.importorskip("shioaji")` 標記該檔或個別測試
- 更好的作法：把不依賴 SDK 的推導邏輯（如 `CancelEvent.reason`）抽成純函式，
  這部分**無條件測試**；只有真正需要 SDK 常數的對映才 skip

### 對應測試

| # | 情境 | 期望 |
|---|---|---|
| 28 | 未安裝 shioaji 時 `pytest` | **零 collection error**，相關測試 skip 而非 error |
| 29 | 未安裝 shioaji 時建立 `ShioajiGateway` | 拋 `BrokerError`，訊息含 `pip install -e ".[live]"` |
| 30 | 未安裝 shioaji 時 `import microtx` 及各層模組 | 全部成功（證明隔離在安裝層面也成立） |
| 31 | 未安裝 shioaji 時 `microtx demo` | 完整跑完，退出碼 0 |

> 情境 28/30 請用 `monkeypatch` 讓 `import shioaji` 失敗來模擬，
> 不要依賴實際環境有沒有裝。

---

## 驗收條件

- [ ] 四項驗收指令全綠（整合測試預設跳過）
- [ ] `import shioaji` 只出現在 `shioaji_gateway.py` 與 `_mapping.py`，
      且**皆為函式內的 lazy import**，模組層級不得出現
- [ ] 金鑰不被存成一般屬性、不出現在任何日誌或例外訊息
- [ ] `pytest -m "not integration"` 在無 `.env` 環境下全部通過
- [ ] **在未安裝 `shioaji` 的環境中 `pytest` 零 collection error**
- [ ] 交付時說明：斷線重連期間的委託如何處理、跨日商品檔如何更新
