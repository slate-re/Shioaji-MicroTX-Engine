# 任務 03 — 行情層（MarketFeed）

## 目標

把券商原生 tick 轉成乾淨的 `TickEvent` 並送進佇列，
**確保 Shioaji 行情執行緒永不被阻塞**。

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/market/tick.py` | 新增 |
| `src/microtx/market/feed.py` | 新增 |
| `tests/test_market_feed.py` | 新增 |

## tick.py

`TickEvent` 定義見 `docs/architecture.md` §4.2。額外提供：

```python
@dataclass(frozen=True, slots=True)
class TickEvent:
    ...
    @property
    def latency_ms(self) -> float:
        """交易所時間到本機收到的延遲（毫秒），用於監控行情健康度。"""

    @classmethod
    def from_raw(cls, raw: RawTick, *, symbol: str) -> TickEvent:
        """由 RawTick 建構。呼叫端須自行確保 raw.simtrade is False。"""
```

## feed.py

```python
class MarketFeed:
    def __init__(
        self,
        gateway: BrokerGateway,
        *,
        symbol: str,
        queue_maxsize: int = 1000,
        drop_simtrade: bool = True,
    ) -> None: ...

    def start(self) -> None:
        """訂閱行情並註冊 callback。"""

    def stop(self) -> None:
        """取消訂閱。"""

    def get(self, timeout: float | None = None) -> TickEvent | None:
        """由 worker thread 呼叫，取出下一筆 tick。逾時回傳 None。"""

    @property
    def stats(self) -> FeedStats:
        """行情健康度統計。"""


@dataclass(frozen=True, slots=True)
class FeedStats:
    received: int          # 收到的原始 tick 數
    filtered_simtrade: int # 被過濾掉的試撮數
    dropped_overflow: int  # 佇列滿而丟棄的數量
    delivered: int         # 成功送進佇列的數量
    last_tick_at: datetime | None
    max_latency_ms: float
```

## 實作要點（這是最容易寫錯的一層）

### 1. callback 內只做三件事

```python
def _on_raw_tick(self, raw: RawTick) -> None:
    # ① 過濾試撮 —— 必須是第一件事
    if self._drop_simtrade and raw.simtrade:
        self._stats_filtered += 1
        return
    # ② 轉為 TickEvent（純建構，無 I/O）
    event = TickEvent.from_raw(raw, symbol=self._symbol)
    # ③ 非阻塞入佇列
    self._enqueue(event)
```

⛔ callback 內**禁止**：日誌 I/O（除非是 overflow 這種罕見情況）、下單、DB 寫入、
   網路請求、`time.sleep()`、取得可能被長時間持有的鎖。

### 2. 佇列滿的處理：丟舊不丟新

```python
def _enqueue(self, event: TickEvent) -> None:
    try:
        self._queue.put_nowait(event)
    except queue.Full:
        try:
            self._queue.get_nowait()      # 丟棄最舊
            self._queue.put_nowait(event) # 放入最新
        except queue.Empty:
            pass
        self._stats_dropped += 1
```

理由：當沖只在乎最新價，舊 tick 沒有價值；但**阻塞行情執行緒**會導致整條行情斷流。
`dropped_overflow` 持續增加代表下游處理不及，應觸發告警。

### 3. 統計計數器

用 `itertools.count` 或簡單 int 搭配 `threading.Lock`。
`stats` 屬性回傳快照（frozen dataclass），不要回傳可變的內部物件。

### 4. 斷線重連

`MarketFeed` 不自己處理重連，但要提供 `resubscribe()` 供 `TradingEngine` 在
偵測到重連後呼叫。重連後 `stats` 不重置（累計值有診斷價值）。

## 測試要求

| 測試 | 說明 |
|---|---|
| 試撮過濾 | `simtrade=True` 的 tick 不得進入佇列，`filtered_simtrade` 正確累加 |
| 佇列溢位 | 灌入 `maxsize + 100` 筆，佇列仍為 `maxsize`，且**保留的是最新的**那批 |
| 非阻塞 | 佇列滿時 `_on_raw_tick` 的耗時必須 < 1ms（用計時斷言） |
| 延遲計算 | `latency_ms` 正確（用 `freezegun` 固定時間） |
| 統計正確性 | `received == filtered + delivered + dropped` |
| 執行緒安全 | 多執行緒同時 `feed_tick`，統計數字不遺失（用 `PaperGateway` + 多 thread） |
| start/stop 冪等 | 重複呼叫不拋例外、不重複訂閱 |

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] 不 import shioaji（只依賴 `broker/base.py` 的抽象型別）
- [ ] callback 路徑上沒有任何 `logger.info` 等級以上的同步 I/O
- [ ] 覆蓋率 ≥ 85%
