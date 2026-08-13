# 任務 10 — `microtx watch` 唯讀監看介面

## 目標與定位

前九個任務每一個都在防某種災難。**這一個不是** ——
它不會讓系統更安全，只會讓使用者**敢用它**。

交易程式在跑真錢時，看不到內部狀態的人會焦慮到想關掉它。
本任務提供一個終端機介面，把「引擎在幹嘛」攤開來。

### 三條不可妥協的原則

| 原則 | 理由 |
|---|---|
| **獨立行程，不內嵌引擎** | 引擎卡死時 TUI 仍須存活並**告訴你它卡死了** |
| **唯讀，零操作入口** | 看得到即時損益又能一鍵下單的介面，會誘導人在情緒最激動時手動干預 |
| **可完全關閉，關閉後零開銷** | 顯示功能不得成為交易路徑的負擔 |

⛔ **TUI 上不得有任何按鈕或快捷鍵能下單、改參數、或停止引擎。**
要停就另開終端機 `microtx panic` —— 那點摩擦是刻意的。

---

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/engine/quote_writer.py` | 新增（`QuoteSnapshot` + `QuoteWriter`） |
| `src/microtx/engine/engine.py` | **擴充**：啟動 `QuoteWriter` 執行緒 |
| `src/microtx/market/feed.py` | **擴充**：callback 記錄最新 tick（單一屬性賦值） |
| `src/microtx/tui/__init__.py`、`tui/watch.py` | 新增（`rich` 版面與更新迴圈） |
| `src/microtx/cli/commands.py` | **擴充**：`watch` 子指令 |
| `src/microtx/config.py` | **擴充**：`quote_file`、`quote_write_interval_sec`、`enable_quote_snapshot` |
| `pyproject.toml` | **擴充**：新增 `tui` optional extra（`rich`） |
| `.env.example`、`README.md` | **擴充** |
| `tests/test_quote_writer.py`、`tests/test_tui_watch.py` | 新增 |

---

## 1. 效能約束（**已實測，須以測試驗證**）

規格制定前的實測數據（Python 3.11，一般 SSD）：

| 項目 | 實測值 |
|---|---|
| 單次原子寫入 `quote.json`（154 bytes） | 中位數 **0.037 ms**，p99 0.072 ms |
| 250ms 週期的 CPU 佔用 | **0.015%** |
| callback 端單一屬性賦值 | **0.04 微秒/tick** |
| 若誤在 callback 內取鎖 | 0.073 微秒/tick（1.7×，且有阻塞風險） |

### 三條實作規則

**① 寫檔絕不在行情 callback 內**

```python
def _on_raw_tick(self, raw: RawTick) -> None:
    if self._drop_simtrade and raw.simtrade:
        ...
        return
    event = TickEvent.from_raw(raw, symbol=self._symbol)
    self._latest_tick = event      # ← 只多這一行
    self._enqueue(event)
```

由獨立的 `QuoteWriter` 執行緒每 250ms 讀取並寫檔，
與 `StatusWriter` 同一模式。

**② 顯示功能不得「新增」鎖**

> 注意用詞：不是「callback 路徑上完全無鎖」。
> `MarketFeed._enqueue` 本來就有 `_stats_lock`（任務 03 維護統計計數用），
> 那是既有且必要的。**本任務不得再多加一把。**

`self._latest_tick = event` 是單一屬性賦值，CPython 下為原子操作，**不需要鎖**。

⛔ 不得為此新增任何鎖，也**不得把捕捉包進既有的 `_stats_lock`**。
加了鎖不只變慢，更會讓顯示功能變成
**又一條與緊急平倉搶鎖的路徑** —— 見 `06-emergency-close.md` §⑤ 的死鎖情境。

### 情境 2 必須用 AST 檢查，不可用字串比對

```python
# ⛔ 字串比對有漏洞（已實測驗證）
capture_line = next(l for l in source.splitlines() if "_latest_tick =" in l)
assert "with" not in capture_line      # 跨行的 with 抓不到

# ✅ AST：找到對 _latest_tick 的 Assign，斷言它不在任何 ast.With 內
```

若日後有人改成下面這樣，字串比對的三個斷言**全部會通過**：

```python
with self._stats_lock:
    self._latest_tick = event      # ← capture_line 不含 "with" 也不含 "lock"
```

（`"acquire" not in source` 也會通過，因為用的是 `with` 語法而非 `.acquire()`。）

**能用結構檢查的，不要用字串比對** —— 字串比對會在重構時給出假的安全感。

**③ 寫檔失敗只記 WARNING，且不重試**

`quote.json` 純顯示用。磁碟滿、權限錯，交易照跑。

---

## 2. `quote.json` 契約

```jsonc
{
  "schema_version": 1,
  "symbol": "TMFR1",
  "last_price": 23150.0,
  "tick_at": "2026-08-13T10:23:45.123+08:00",   // 交易所時間
  "written_at": "2026-08-13T10:23:45.130+08:00",
  "latency_ms": 38.2                             // 交易所時間 → 本機收到
}
```

- **刻意只放價格**，不放部位或損益（理由見 §3）
- 目標檔案大小 < 300 bytes
- 原子寫入：`.tmp` → `os.replace()`，與 `status.json` 一致
- 尚未收到任何 tick 時（如盤前），`last_price` 為 `null`

### `config.py` 新增

```python
enable_quote_snapshot: bool = Field(default=True)
quote_file: Path = Field(default=Path("runtime/quote.json"))
quote_write_interval_sec: float = Field(default=0.25, ge=0.05, le=5.0)
```

`enable_quote_snapshot=False` 時，`QuoteWriter` **執行緒完全不啟動**，
`MarketFeed` 也不記錄 `_latest_tick` —— 零開銷，不是「寫了但不用」。

---

## 3. 損益由 TUI 自行計算（**關鍵設計**）

部位資料在 `status.json`，5 秒才更新。若損益也走那條路，
畫面會出現「價格每 250ms 跳、損益 5 秒才動一次」的割裂感。

**解法：TUI 用慢速部位 + 快速價格自行計算。**

```python
# TUI 端，每次刷新時計算
unrealized_ntd = (
    (last_price - position.average_price)
    * position.direction.sign
    * position.quantity
    * spec.point_value
)
```

| 資料 | 來源 | 頻率 | 變動頻率 |
|---|---|---|---|
| 部位（方向／口數／均價） | `status.json` | 5s | 低（進出場才變） |
| 最新價 | `quote.json` | 250ms | 高 |
| **未實現損益** | **TUI 計算** | 250ms | 跟著價格平滑跳動 |

好處：引擎端**完全不為顯示做任何額外運算**，`quote.json` 保持極小。

> 已實現損益、交易次數等仍直接取自 `status.json`，不需計算。

---

## 4. TUI 版面

```
┌─ MicroTX ─────────────────── 🟢 CONNECTED ── 10:23:45 ─┐
│ TMFR1 微型臺指      23150  ▲ 42 (+0.18%)   延遲 38ms   │
├────────────────────────────────────────────────────────┤
│ 部位   多 1 口 @ 23108        未實現  +420 元          │
│ 當日   已實現 -150 元   總計 +270 元   交易 3/10 筆    │
├────────────────────────────────────────────────────────┤
│ 策略   scalp  IN_POSITION  觸發23100 TP50 SL30         │
│ 引擎   RUNNING   風控 正常   強平 13:40                │
└────────────────────────────────────────────────────────┘
```

### 右上角的燈號與時間是核心

| 顯示 | 判定依據 |
|---|---|
| 🟢 `CONNECTED` | `status.json` 新鮮 且 `degraded=false` 且 `broker_connected=true` |
| 🟡 `DEGRADED` | `degraded=true` —— **引擎卡在共用鎖上，建議立即 `microtx panic`** |
| 🟡 `DISCONNECTED` | `broker_connected=false`（SDK 重連中） |
| 🔴 `NO RESPONSE` | `status.json` 逾時未更新（> 3× 寫入間隔） |
| ⚫ `STOPPED` | PID 不存在 |

**時間欄位必須顯示 `status.json` 的 `written_at`，不是本機時鐘。**
時間停止跳動 = 引擎不再更新狀態，使用者一眼就知道出事了，不必等任何告警。

> 這是本任務最重要的一格：它把任務 06b／08 建立的「分辨掛掉 vs 卡死」
> 機制，第一次變成人類看得到的東西。

### 版面規則

- 損益數字**正綠負紅**，但**不得**使用閃爍或音效（交易介面上的噪音會誘發衝動）
- 終端機寬度 < 60 時降級為單欄純文字，不得崩潰
- 非 TTY 環境（如 `microtx watch | tee`）自動改為每秒印一行純文字快照

---

## 5. 依賴與指令

```toml
[project.optional-dependencies]
tui = ["rich>=13.0"]
```

```bash
pip install -e ".[dev,tui]"
microtx watch                    # 讀 runtime/ 下的檔案
microtx watch --interval 0.5     # 自訂刷新率
```

未安裝 `rich` 時執行 `microtx watch` → 拋出可操作的錯誤訊息
（`pip install -e ".[tui]"`），**不得**是裸的 `ModuleNotFoundError`。
比照 07b 的 lazy import 模式。

⛔ `rich` **不得**成為必要依賴 —— `microtx demo` 與全部單元測試
在未安裝 `rich` 的環境仍須通過（CI 已在驗證此條件）。

---

## 測試要求

| # | 情境 | 期望 |
|---|---|---|
| 1 | **callback 端成本** | 以 20 萬次迴圈量測 `_on_raw_tick` 新增成本，**須 < 1 微秒/tick** |
| 2 | **顯示功能不得新增鎖** | **AST 檢查**：`_on_raw_tick` 中對 `_latest_tick` 的 `Assign` 節點，不得位於任何 `ast.With` 內 |
| 3 | 單次寫入耗時 | `QuoteWriter` 單次寫入 **< 5 ms**（寬鬆上限，實測約 0.04ms） |
| 4 | 原子性 | 併發讀寫下讀到的一律是完整 JSON |
| 5 | `enable_quote_snapshot=False` | `QuoteWriter` 執行緒**不啟動**，`quote.json` 不存在，`_latest_tick` 不記錄 |
| 6 | 寫檔失敗 | 目錄不可寫時只記 WARNING，引擎繼續運行 |
| 7 | 盤前無 tick | `last_price` 為 `null`，TUI 顯示「--」不崩潰 |
| 8 | **TUI 損益計算** | 給定 `status.json` 部位 + `quote.json` 價格，計算結果正確（含多空、多口數） |
| 9 | **五種燈號** | 各構造對應的檔案狀態，斷言燈號與文字正確 |
| 10 | **`DEGRADED` 顯示** | `degraded=true` 時顯示黃燈與「建議 panic」提示 |
| 11 | **時間來自 `status.json`** | 本機時鐘前進但 `written_at` 不變時，顯示的時間**不得**跟著跳 |
| 12 | `status.json` 過期 | 逾 3× 間隔未更新 → 🔴 `NO RESPONSE` |
| 13 | 未安裝 `rich` | `microtx watch` 給出含 `pip install -e ".[tui]"` 的錯誤，退出碼非 0 |
| 14 | 未安裝 `rich` 時其他功能 | `microtx demo` 與全部單元測試照常通過 |
| 15 | 窄終端機 | 寬度 40 時降級為單欄，不崩潰 |
| 16 | 非 TTY | 管線輸出時改為純文字行模式 |
| 17 | **無操作入口** | 靜態檢查：`tui/` 底下不得 import `OrderRouter` / `TradingEngine` / `EmergencyCloser` |

> **情境 1、2、17 是本任務的驗收核心。**
> 1 與 2 確保顯示功能不拖累交易；17 確保它永遠只是個觀景窗。

---

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] 在**未安裝 `rich`** 的環境中 `pytest` 零 collection error、`microtx demo` 退出碼 0
- [ ] `tui/` 不 import 任何會下單的模組（情境 17）
- [ ] `enable_quote_snapshot=False` 時引擎行為與本任務實作前**完全一致**
- [ ] 交付時附上情境 1 的實測數字，並說明：
      為什麼 TUI 必須是獨立行程而不是 `microtx run --tui`
