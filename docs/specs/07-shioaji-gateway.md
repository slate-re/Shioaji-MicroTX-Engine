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
| `OrderIntent` → `octype` | `ENTRY`→`DayTrade`；其餘→`Cover` |

要點：

- 提供 `to_shioaji_*()` 與 `from_shioaji_*()` 成對函式
- 未知值一律拋 `ValueError`，**不要靜默 fallback**（靜默 fallback 會變成下錯單）
- `octype` 對映需可由 config 切換：當沖用 `DayTrade`，保守模式用 `Auto`

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

### 商品解析與快取

- `api.Contracts.Futures.TMF.TMFR1` 這類存取要快取，不要每次下單重新走屬性鏈
- 快取 `limit_up` / `limit_down`，供 `get_price_limits()` 與下單前檢查
- 商品檔每日更新，**跨日時必須重新載入**

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

### 斷線重連

- 用 `api.set_event_callback()` 監聽 Session down（Event Code 對應見 guide）
- 重連採指數退避，最多 N 次；重連成功後**必須重新訂閱行情**
- 重連期間 `is_connected` 回傳 `False`，讓 `OrderRouter` 拒絕新單

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

## 驗收條件

- [ ] 四項驗收指令全綠（整合測試預設跳過）
- [ ] `import shioaji` 只出現在 `shioaji_gateway.py` 與 `_mapping.py`
- [ ] 金鑰不被存成一般屬性、不出現在任何日誌或例外訊息
- [ ] `pytest -m "not integration"` 在無 `.env` 環境下全部通過
- [ ] 交付時說明：斷線重連期間的委託如何處理、跨日商品檔如何更新
