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
    """行情健康度統計。所有計數器皆為單調遞增的累計值。"""

    received: int           # 收到的原始 tick 總數
    filtered_simtrade: int  # 因 simtrade=True 而被丟棄，未進入佇列
    delivered: int          # 成功放入佇列的 tick 總數
    consumed: int           # 已被 get() 取走的 tick 總數
    evicted_overflow: int   # 佇列已滿時，被擠出佇列的「舊」tick 總數
    queue_depth: int        # 當下佇列長度（唯一的瞬時值，非累計）
    last_tick_at: datetime | None
    max_latency_ms: float
```

### 計數器語意與守恆律（重要，先前規格此處有誤）

先前規格寫的 `received == filtered + delivered + dropped` **是錯的**，
因為它把「被拒收的新 tick」和「被擠出的舊 tick」混為一談。

本專案的溢位策略是**丟舊留新**：佇列滿時，新 tick 一定會被放進去，
被犧牲的是佇列裡最舊的那一筆。所以：

- 新 tick **從不被拒收** → 它必然計入 `delivered`
- 被擠出的是**先前已計入 `delivered`** 的舊 tick → 這是另一個維度的事件

因此 `delivered` 維持最直覺的語意（「成功入列的累計數」），**不要**改成淨值。
正確的守恆律有兩條：

```
① 入口守恆   received == filtered_simtrade + delivered
② 出口守恆   delivered == consumed + queue_depth + evicted_overflow
```

欄位名由 `dropped_overflow` 改為 **`evicted_overflow`**：
「dropped」會讓人以為是新 tick 被丟掉，「evicted（淘汰）」才準確表達
「已在佇列中的舊資料被擠出」。命名歧異正是這次矛盾的根源。

> `evicted_overflow > 0` 代表下游處理速度跟不上行情，應觸發告警 ——
> 這個數字的診斷價值，正是它必須維持單調累計、不可與 `delivered` 互相抵銷的理由。

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
            self._queue.get_nowait()       # 淘汰最舊
            self._evicted_overflow += 1    # ← 只在真的擠掉東西時才 +1
        except queue.Empty:
            pass                           # 競態下佇列剛好被清空，不算淘汰
        self._queue.put_nowait(event)
    self._delivered += 1                   # ← 新 tick 一定入列，必然 +1
```

⚠️ 兩個計數器的更新位置是刻意的：
`_delivered` 在**兩條路徑上都要 +1**（新 tick 從不被拒收），
`_evicted_overflow` 只在**確實擠掉舊資料時** +1。
搞混會讓上面的兩條守恆律不成立。

理由：當沖只在乎最新價，舊 tick 沒有價值；但**阻塞行情執行緒**會導致整條行情斷流。
`evicted_overflow` 持續增加代表下游處理不及，應觸發告警。

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
| **守恆律 ①** | `received == filtered_simtrade + delivered` |
| **守恆律 ②** | `delivered == consumed + queue_depth + evicted_overflow` |
| 守恆律於溢位下仍成立 | 灌入遠超 `maxsize` 的量後取走部分，兩條等式仍須同時成立 |
| 執行緒安全 | 多執行緒同時 `feed_tick`，兩條守恆律仍成立（用 `PaperGateway` + 多 thread） |
| start/stop 冪等 | 重複呼叫不拋例外、不重複訂閱 |

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] 不 import shioaji（只依賴 `broker/base.py` 的抽象型別）
- [ ] callback 路徑上沒有任何 `logger.info` 等級以上的同步 I/O
- [ ] 覆蓋率 ≥ 85%
