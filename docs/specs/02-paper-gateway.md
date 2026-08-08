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

| 委託 | 撮合邏輯 |
|---|---|
| `LMT` + `ROD` | 掛單。當後續 tick 價格穿越委託價時成交（買單：`tick.price <= limit`；賣單：`tick.price >= limit`） |
| `LMT` + `IOC` | 若當下最新價已滿足則立即成交，否則立即取消 |
| `MKP` / `MKT` | 以最新成交價 ± `slippage_ticks` 立即成交 |
| `FOK` | 全部可成交才成交，否則整筆取消 |

- 漲跌停鎖死：若委託方向會突破 `price_limits`，一律**不成交**（用於緊急平倉失敗情境測試）
- `reject_rate > 0` 時按機率隨機拒單，用於測試 `on_reject` 路徑
- `fill_delay_sec > 0` 時延遲送出 `FillEvent`，用於測試「回報順序」容錯

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

## 邊界情境測試

| 情境 | 期望 |
|---|---|
| 平倉單口數大於持倉 | 只平掉持有口數，多餘部分拒絕並記錄 |
| 同一 `client_id` 重複下單 | 第二次直接回 `accepted=False`，**不得重複成交**（冪等） |
| 漲跌停鎖死時送平倉單 | 委託被接受但永不成交，`list_positions()` 仍有部位 |
| 斷線後下單 | 拋 `ConnectionLostError` |
| `simtrade=True` 的 tick | 仍會傳給 callback（過濾是 market 層的責任，不是 broker 層） |

## 測試要求

- 每條撮合規則各一個測試
- 冪等測試：同 `client_id` 送 3 次，最終部位只有 1 口
- 損益測試：微台做多 23000 → 平倉 23050，已實現損益 = 500 元
- 漲跌停鎖死測試（供任務 06 的緊急平倉失敗情境重用）
- 覆蓋率 ≥ 90%（這是測試基礎設施，本身必須夠可靠）

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] 不 import shioaji
- [ ] `pytest tests/test_paper_gateway.py` 在**無網路、無 .env** 的環境下可通過
- [ ] 提供一個 `python -m microtx demo` 能用的重播資料（`tests/fixtures/sample_ticks.csv`）
