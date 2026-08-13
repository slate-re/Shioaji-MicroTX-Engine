# 任務 09 — 當日狀態持久化與交易日邊界修正

## 背景：同一個 bug 的兩個實例

風控的單日虧損上限（`max_daily_loss`）依賴 `PositionTracker.realized_pnl_ntd`
與 `trade_count`。這兩個值目前**只存在記憶體中**，且**以日曆日重置**。

因此上限會在兩種情況下**靜默失效** —— 沒有錯誤訊息、沒有日誌、測試也是綠的：

### 實例 A：重啟後歸零

```
崩潰前：realized_pnl = -2900，距 3000 上限只剩 100
重啟後：realized_pnl = 0      ← 當天可以再虧 3000
```

launchd 設定為崩潰自動重啟，所以這條路徑是**必然會走到的**。

### 實例 B：夜盤跨午夜歸零（更嚴重）

`scheduler.py` 目前的判斷：

```python
if self._last_seen_date is not None and current.date() != self._last_seen_date:
    self._on_reset_daily()        # ← 日曆日
```

夜盤時段為 15:00 至次日 05:00。**每晚 00:00 一到就在盤中重置** ——
不需要崩潰、不需要重啟，只要 `ENABLE_NIGHT_SESSION=true` 就每天發生。

> 目前 `enable_night_session` 預設 `false`，所以這個 bug 是**潛伏**狀態。
> 但它是預設值在保護我們，不是程式碼在保護我們 —— 這種保護不算數。

### 共同根因

「當日」被定義為**日曆日**（`datetime.date()`），而正確定義是**交易日**。

---

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/engine/trading_day.py` | 新增（交易日定義，單一真相來源） |
| `src/microtx/engine/daily_state.py` | 新增（`DailyState` + `DailyStateStore`） |
| `src/microtx/engine/scheduler.py` | **修改**：重置改以交易日為界 |
| `src/microtx/engine/position.py` | **擴充**：支援載入既有累計值 |
| `src/microtx/engine/engine.py` | **擴充**：啟動載入、成交後寫入、`notifier` 參數 |
| `src/microtx/cli/commands.py` | **擴充**：`run --reset-daily-state` 旗標 |
| `src/microtx/config.py` | **擴充**：`daily_state_file`、`trading_day_boundary` |
| `src/microtx/enums.py` | **擴充**：新增 `LoadOutcome` |
| `.env.example` | **擴充** |
| `tests/test_trading_day.py`、`tests/test_daily_state.py` | 新增 |
| `tests/test_scheduler.py`、`tests/test_position.py`、`tests/test_engine.py` | **擴充** |

---

## 1. `trading_day.py` —— 交易日的單一定義

```python
def trading_date(now: datetime, *, boundary: time) -> date:
    """回傳 ``now`` 所屬的交易日。

    台指期夜盤為 15:00 至次日 05:00，因此凌晨時段仍屬**前一個**交易日。
    邊界設在夜盤結束（05:00）與日盤開盤（08:45）之間，預設 06:00。

    Examples:
        週五 22:30  → 週五
        週六 01:30  → **週五**（仍在週五夜盤）
        週六 07:00  → 週六
    """
    taipei_now = now.astimezone(ZoneInfo("Asia/Taipei"))
    if taipei_now.time() < boundary:
        return taipei_now.date() - timedelta(days=1)
    return taipei_now.date()
```

- 這是**唯一**的交易日定義，`Scheduler` 與 `DailyStateStore` 都必須用它
- 邊界由 `settings.trading_day_boundary` 提供，預設 `06:00`
- 選 06:00 是因為它落在「夜盤已結束、日盤未開始」的空窗，不會切在盤中

### `config.py` 新增

```python
daily_state_file: Path = Field(default=Path("runtime/daily_state.json"))
trading_day_boundary: time = Field(default=time(6, 0))
```

`trading_day_boundary` 需加入既有的 `_validate_time` 驗證器，
並新增檢查：必須落在夜盤結束與 `session_start` 之間，否則會切在盤中。

---

## 2. `daily_state.py` —— 持久化

```python
@dataclass(frozen=True, slots=True)
class DailyState:
    """跨重啟需要保留的當日累計值。**不含部位** —— 部位真相來源是券商。"""

    schema_version: int          # 固定為 1
    trading_date: date
    realized_pnl_ntd: float
    trade_count: int
    updated_at: datetime


class DailyStateStore:
    def __init__(self, path: Path, *, boundary: time) -> None: ...

    def load(self, now: datetime) -> LoadResult: ...
    def save(self, state: DailyState) -> None: ...
    def clear(self) -> None: ...
```

### 為什麼不持久化部位

Gemini 之類的建議常說「重啟要記得自己有部位」。**本專案不需要**：
`EmergencyCloser` 與 `ReconcileWorker` 一律以 `gateway.list_positions()` 為準，
部位的真相來源從來不是本地記憶。若再存一份本地部位，只會多一個會漂移的來源。

**只持久化券商查不到的東西**：當日已實現損益與交易次數。

### 原子寫入

與 `status.json` 相同：寫 `.tmp` 再 `os.replace()`。
`save()` 的呼叫時機是每次成交後，頻率低（當沖一天數十筆），效能不是問題。

### 載入的四種結果（**這是本任務的核心**）

`LoadOutcome` 放在 `src/microtx/enums.py`（本任務授權擴充該檔），
與 `NotifyLevel` 一致 —— 專案的列舉集中在一處，讀者不必猜。

```python
# enums.py
class LoadOutcome(str, Enum):
    """當日狀態檔的載入結果。"""

    FRESH = "FRESH"
    """檔案不存在 —— 本交易日第一次啟動。"""

    RESTORED = "RESTORED"
    """交易日相同 —— 還原累計值。"""

    ROLLED_OVER = "ROLLED_OVER"
    """交易日已變更 —— 捨棄舊值，重新起算。"""

    UNREADABLE = "UNREADABLE"
    """檔案損毀或解析失敗 —— 風控狀態未知，見下方處置。"""
```

### `LoadResult` 正式定義

```python
@dataclass(frozen=True, slots=True)
class LoadResult:
    outcome: LoadOutcome
    state: DailyState | None      # ⚠️ UNREADABLE 時為 None
    previous: DailyState | None = None   # 僅 ROLLED_OVER 有值，供日誌記錄前一交易日結算
    error: str = ""                      # 僅 UNREADABLE 有值，診斷用
```

#### 為何 `UNREADABLE` 回傳 `None` 而不是零值 `DailyState`

回傳零值 state 是可行的，但**危險**：呼叫端只要忘記檢查 `outcome`，
就會拿到一個看起來完全正常的「今天虧 0 元」——
**靜默重建本任務要修的那個 bug**，而且這次連日誌都不會提醒。

改成 `None` 之後，`mypy --strict` 會強制呼叫端處理 `None` 分支。
**把不安全的路徑變成型別上不可表達**，比靠註解提醒可靠。

| outcome | `state` | `previous` | `error` |
|---|---|---|---|
| `FRESH` | 當前交易日的零值 | `None` | `""` |
| `RESTORED` | 還原的值 | `None` | `""` |
| `ROLLED_OVER` | 當前交易日的零值 | 前一交易日的最終值 | `""` |
| `UNREADABLE` | **`None`** | `None` | 錯誤描述（不含檔案完整路徑以外的敏感資訊） |

| 結果 | 行為 | 日誌 |
|---|---|---|
| `FRESH` | 從 0 起算 | INFO |
| `RESTORED` | 還原 `realized_pnl` 與 `trade_count` | INFO，含還原數值 |
| `ROLLED_OVER` | 從 0 起算，覆寫舊檔 | INFO，含前一交易日結算 |
| `UNREADABLE` | **引擎啟動後直接進入 `HALTED`** | CRITICAL + 通知 |

### `UNREADABLE` 為何要停機

檔案損毀代表「今天已經虧了多少」是**未知數**。

| 選項 | 後果 |
|---|---|
| 當作 0 繼續跑 | 靜默重建原本要修的 bug —— 而且這次連日誌都不會提醒 |
| 拒絕啟動（exit） | 無法交易，但也無法平掉既有部位 ❌ |
| **啟動但 HALTED** ✅ | 不開新倉，但 `microtx panic` / `flatten` 仍可用 |

選第三個：風控狀態未知時，唯一安全的行為是**不開新倉，但保留平倉能力**。

人工確認後可用旗標覆寫：

```bash
microtx run --reset-daily-state    # 明確宣告「我知道，從 0 起算」
```

⛔ 這個旗標**不可**有預設值或環境變數等價物。
它必須是人類在終端機上敲下去的 —— 否則就退化成靜默重置。

### 通知注入

`UNREADABLE` 需發送 CRITICAL 通知，但 `TradingEngine` 目前沒有 `Notifier`。

**裁決：本任務為 `TradingEngine` 加上 notifier 參數。**

```python
class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        gateway: BrokerGateway,
        *,
        notifier: Notifier | None = None,     # ← 新增，有預設值
    ) -> None: ...
```

- 有預設值，**不破壞既有呼叫端**
- Engine 把它轉交給 `EmergencyCloser`（該處早已有此參數，先前一直是 `None`）
- CLI wiring **維持現狀傳 `None`** —— 具體的 Telegram 實作不在本任務範圍

理由：`EmergencyCloser` 已經需要 notifier，而它是由 Engine 建構的，
所以擁有權本來就該在 Engine。現在補上是**把既有的斷線接起來**，不是新增功能。
等日後做 Telegram 時，接線已經在了，只需替換傳入的實例。

測試以一個記錄用的假 `Notifier` 驗證 `UNREADABLE` 時確實嘗試發送
（`notifier=None` 時則只寫日誌，不得拋例外）。

---

## 3. `scheduler.py` 修正

```python
# ⛔ 修正前：日曆日，夜盤跨午夜會在盤中重置
if self._last_seen_date is not None and current.date() != self._last_seen_date:
    self._on_reset_daily()

# ✅ 修正後：交易日
current_trading_date = trading_date(current, boundary=self._settings.trading_day_boundary)
if self._last_trading_date is not None and current_trading_date != self._last_trading_date:
    self._on_reset_daily()
self._last_trading_date = current_trading_date
```

`_last_force_close_date` 同樣改用交易日，避免夜盤跨午夜時強平被重複觸發。

---

## 4. `PositionTracker` 擴充

```python
def restore_daily(self, *, realized_pnl_ntd: float, trade_count: int) -> None:
    """由持久化狀態還原當日累計值。僅供啟動時呼叫一次。"""
```

- 只還原累計值，**不還原部位**
- 需防止重複呼叫（第二次呼叫拋 `MicroTXError`），避免中途被誤用而灌爆計數

`reset_daily()` 維持現狀，但引擎需在其後呼叫 `store.clear()`。

---

## 5. 寫入時機

| 時機 | 動作 |
|---|---|
| 啟動 | `load()` → 依結果還原或停機 |
| 每次 `FillEvent` 處理完 | `save()` |
| `reset_daily()` 觸發後 | `clear()` 後重新 `save()` 新交易日的初始狀態 |
| `engine.stop()` | 最後 `save()` 一次 |

寫入失敗一律 `try/except Exception` + WARNING，**不得影響交易**。
（讀取失敗才需要停機；寫入失敗只是下次重啟會少還原一點。）

---

## 測試要求

| # | 情境 | 期望 |
|---|---|---|
| 1 | `trading_date` 週五 22:30 | 回傳週五 |
| 2 | `trading_date` 週六 01:30 | 回傳**週五**（夜盤跨午夜） |
| 3 | `trading_date` 週六 07:00 | 回傳週六 |
| 4 | `trading_date` 邊界前後 1 分鐘 | 分屬不同交易日 |
| 5 | **Scheduler 夜盤跨午夜** | 用 `freezegun` 從 23:59 走到 00:01，**`on_reset_daily` 不得被呼叫** |
| 6 | Scheduler 跨交易日 | 從週五 22:00 走到週六 07:00，`on_reset_daily` 呼叫**恰好一次** |
| 7 | `load()` 檔案不存在 | `FRESH`，從 0 起算 |
| 8 | `load()` 同交易日 | `RESTORED`，數值正確 |
| 9 | `load()` 不同交易日 | `ROLLED_OVER`，從 0 起算 |
| 10 | `load()` 檔案為損毀 JSON | `UNREADABLE` |
| 11 | `load()` 檔案缺欄位 / `schema_version` 不符 | `UNREADABLE` |
| 12 | **`UNREADABLE` 時引擎行為** | 進入 `HALTED`，且 `panic` / `flatten` 仍可執行 |
| 13 | `--reset-daily-state` | 覆寫 `UNREADABLE`，正常啟動並從 0 起算 |
| 14a | **端到端：重啟後風控仍生效** | 累計虧損達 **-3000**（= `max_daily_loss`）→ 停止引擎 → 重啟 → 送新倉單，**須被風控拒絕** |
| 14b | **端到端：還原值精確** | 累計虧損 **-2900** → 停止引擎 → 重啟 → 直接斷言 `tracker.realized_pnl_ntd == -2900`，且新倉**應通過**（尚未達上限） |
| 15 | 寫檔失敗（目錄不可寫） | 只記 WARNING，交易照常 |
| 16 | 原子性 | 併發讀寫時讀到的一律是完整 JSON |
| 17 | `restore_daily` 重複呼叫 | 第二次拋 `MicroTXError` |
| 18 | 狀態檔無機密 | 序列化結果不含任何金鑰、帳號 |
| 19 | `UNREADABLE` 的 `LoadResult.state` | 必為 `None`（型別強制呼叫端處理） |
| 20 | `ROLLED_OVER` 的 `previous` | 含前一交易日的最終累計值，供日誌記錄 |
| 21 | `UNREADABLE` 時發送通知 | 以假 `Notifier` 驗證確實嘗試發送 CRITICAL |
| 22 | `notifier=None` 時 `UNREADABLE` | 只寫日誌，不拋例外 |

> **情境 5、14a、14b 是本任務的驗收核心。**
> 5 對應 bug B（夜盤跨午夜），14a/14b 對應 bug A（重啟歸零）。

### 為何把 14 拆成兩條

先前版本寫「虧 2900 → 重啟 → 新倉須被拒絕」，**這在數學上不成立**：
`-2900 > -3000`，現有風控規則會放行，而且放行是正確的 ——
系統沒有「剩餘預算不足就禁止開倉」這條規則。

更根本的問題是：那條測試想驗證持久化，卻**透過風控閘門間接觀察**。
一旦風控規則調整，測試就會以看似無關的理由失敗。

拆開後各司其職：

- **14a**：虧損**達到**上限 → 重啟 → 被拒。驗證「風控確實讀到還原後的值」
- **14b**：虧損**未達**上限 → 重啟 → **直接斷言** `realized_pnl_ntd == -2900`，
  並確認新倉照常通過。驗證「還原的數值精確無誤」

**能直接斷言的東西，不要透過另一個模組的行為間接推論。**

> 附註：「剩餘風險預算」（開倉前檢查該筆最大可能虧損是否超出剩餘額度）
> 是真實交易系統會有的機制，對本專案也可行 ——
> Scalp 策略的最大風險是已知的（`stop_loss_points × point_value × quantity`）。
> 但那是**風控模型的擴充**，不屬於本任務。如要做，另開任務單。

---

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] `daily_state.py`、`trading_day.py` 覆蓋率 ≥ 95%
- [ ] `runtime/` 已在 `.gitignore`，`daily_state.json` 不會進版控
- [ ] 情境 5、14 的測試必須有繁中註解，說明它們對應的 bug 是什麼
- [ ] 交付時說明：為什麼選擇「持久化累計值」而不是「持久化部位」
