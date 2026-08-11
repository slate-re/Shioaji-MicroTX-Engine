# 任務 08 — CLI 與 Mac Mini 部署

## 目標

提供人類介面與部署工具。**CLI 是薄殼，不含任何商業邏輯。**

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/cli/__init__.py`、`cli/commands.py` | 新增 |
| `src/microtx/__main__.py` | 新增 |
| `src/microtx/engine/status.py` | 新增（`StatusSnapshot` + `StatusWriter`） |
| `src/microtx/engine/engine.py` | **擴充**：啟動 `StatusWriter` 執行緒 |
| `src/microtx/config.py` | **擴充**：`status_file`、`status_write_interval_sec` |
| `tests/test_status.py`、`tests/test_engine.py` | 新增／擴充 |
| `.github/workflows/ci.yml` | 新增（見下方「shellcheck」） |
| `scripts/com.jam.microtx.plist` | 新增（launchd 設定範本） |
| `scripts/install-macmini.sh` | 新增 |
| `scripts/healthcheck.sh` | 新增 |
| `tests/test_cli.py` | 新增 |

> CLI 用標準庫 `argparse`，**不引入 click / typer**（保持依賴精簡可審計）。

---

## 指令

### `microtx run`

啟動引擎常駐。

```bash
microtx run                      # 用 .env 設定啟動，不預先武裝任何策略
microtx run --strategy scalp --direction long --trigger 23150 --tp 50 --sl 30
```

- 啟動時先印 `settings.summary()`（已保證不含機密）
- 若 `settings.is_live` 為真，**必須在終端機要求輸入 `YES` 二次確認才繼續**
  （非互動環境如 launchd 則檢查環境變數 `MICROTX_CONFIRM_LIVE=YES`）
- 取得 `PidFile` 後進入 `run_forever()`

### `microtx scalp` / `microtx oco`

對**已運行**的引擎新增策略（透過本機 socket 或 runtime 指令檔；
本版可先實作為「只能在 `run` 時以參數帶入」，並在 CLI 說明中註明）。

### 🚨 `microtx panic`

```bash
microtx panic              # 刪單 + 平倉 + 引擎停機（需人工重啟）
microtx panic --yes        # 跳過確認提示，供腳本呼叫
```

流程：

```
1. pid = PidFile.read_pid(settings.pid_file)
2. if pid is None:
       stderr 印出「引擎未運行（PID 檔不存在或行程已結束）」
       ⚠️ 同時提醒：「若確認仍有部位，請立即至永豐下單軟體手動平倉」
       exit(1)
3. 除非 --yes，否則要求輸入 y 確認（顯示「這會平掉所有部位並停機」）
4. os.kill(pid, signal.SIGUSR1)
5. 印出「已送出 PANIC 訊號至 PID xxxx，請查看引擎日誌確認結果」
6. exit(0)
```

### `microtx flatten`

同上，但送 `SIGUSR2`，語意為「平倉後待命，引擎繼續運行」。

### `microtx status`

讀取 PID 檔與 `runtime/status.json`，顯示引擎狀態、部位、當日損益、
交易次數、行情延遲、已武裝策略。

---

## `status.json` 正式契約（本任務新增）

### 為什麼需要它 —— PID 存活 ≠ 引擎健康

這是本節的設計核心：

```
行程不存在        → PID 檔查不到     → 「引擎未運行」
行程活著且正常    → status.json 新鮮 → 「運行中」
行程活著但卡死    → PID 查得到，但 status.json 不再更新 → 「⚠️ 引擎無回應」
                    ↑ 只有靠快照的「新鮮度」才偵測得到
```

第三種情況正是 `06-emergency-close.md` §⑤ 處理的失效模式（共用鎖被占住）。
若 `status` 與 healthcheck 只看 PID，一個徹底卡死的引擎會被回報為「正常運行」——
那是最危險的誤報。因此**採用方案 1：授權本任務擴充 `engine.py`**。

### `StatusSnapshot` schema

```jsonc
{
  "schema_version": 1,
  "written_at": "2026-08-11T10:23:45+08:00",   // tz-aware，healthcheck 用它算新鮮度
  "pid": 12345,
  "engine_state": "RUNNING",
  "mode": "SIMULATION",                         // "SIMULATION" | "LIVE"
  "symbol": "TMFR1",
  "session": "DAY",
  "broker_connected": true,
  "degraded": false,                            // 見下方「降級寫入」
  "degraded_reason": "",
  "position": {
    "direction": "LONG",                        // 或 null（空手）
    "quantity": 1,
    "average_price": 23150.0,
    "unrealized_ntd": 320.0
  },
  "pnl": { "realized_ntd": -150.0, "total_ntd": 170.0 },
  "trade_count": 3,
  "strategies": [
    { "id": "a1b2c3", "kind": "scalp", "state": "IN_POSITION", "summary": "做多 觸發23150 TP50 SL30" }
  ],
  "feed": {
    "received": 18422, "evicted_overflow": 0,
    "max_latency_ms": 43.2, "last_tick_at": "2026-08-11T10:23:44+08:00"
  },
  "emergency": { "is_closing": false, "pending": null, "last_succeeded": null }
}
```

⛔ **絕不可寫入**：API Key、Secret、憑證路徑或密碼、身分證字號、帳號代碼。
`mode` 只寫 `"SIMULATION"` / `"LIVE"` 兩個字串。
新增測試：對含機密的 Settings 產生快照，斷言序列化結果不含任何機密值。

### 原子寫入（必須）

```python
tmp = self._path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
os.replace(tmp, self._path)      # POSIX 上為原子操作
```

理由：healthcheck 可能在任何時刻讀取。非原子寫入會讓它讀到半截 JSON 而誤判為異常。

### 降級寫入 —— 卡死時仍要留下訊號

`StatusWriter` 需要讀 `PositionTracker` 等共用狀態，但**絕不可無限期等鎖**
（否則它會變成又一條被拖死的路徑，而且是負責回報「有沒有被拖死」的那條）。

```python
acquired = self._lock.acquire(timeout=0.5)
try:
    payload = self._full_snapshot() if acquired else self._degraded_snapshot()
finally:
    if acquired:
        self._lock.release()
```

`_degraded_snapshot()` 只寫**不需要鎖**的欄位（`pid`、`engine_state`、
`written_at`、`mode`、`symbol`），並設 `degraded: true`、
`degraded_reason: "無法取得共用鎖"`。

這樣 healthcheck 能區分三種狀態，而不是只有「新鮮 / 過期」：

| status.json | 判定 |
|---|---|
| 檔案不存在 / PID 已死 | 引擎未運行 |
| 新鮮且 `degraded=false` | 正常 |
| 新鮮但 `degraded=true` | ⚠️ **引擎卡在共用鎖上** —— 建議立即 `microtx panic` |
| 過期（`written_at` 超過 3×間隔） | ⚠️ 引擎無回應 |

> 「新鮮但降級」比「過期」更有診斷價值：它明確指出卡在哪裡。
> 這個設計直接來自 06b 那個死鎖情境。

### 其他要求

- `config.py` 新增：
  ```python
  status_file: Path = Field(default=Path("runtime/status.json"))
  status_write_interval_sec: float = Field(default=5.0, ge=1.0, le=60.0)
  ```
  `.env.example` 一併補上
- `StatusWriter` 為 daemon 執行緒，`engine.stop()` 時停止並寫最後一次快照
  （`engine_state: "STOPPED"`）
- **寫檔失敗不得影響引擎**：整段包 `try/except Exception`，只記 WARNING
- `runtime/` 已在 `.gitignore`，`status.json` 不會進版控

### `microtx demo`

用 `PaperGateway` + `tests/fixtures/sample_ticks.csv` 重播，
**無需帳號、無需 `.env`**，完整跑一輪 scalp 策略並印出結果。

> 這個指令是給面試官看的。它必須在 `git clone && pip install -e . && microtx demo`
> 三步之內就能跑起來。

---

## CLI 設計要求

| 要求 | 說明 |
|---|---|
| 退出碼 | 成功 0、使用者錯誤 2、引擎未運行 1、內部錯誤 70 |
| 錯誤輸出 | 一律走 stderr，訊息用繁體中文 |
| 危險操作 | `panic` / `flatten` / 實盤 `run` 都需確認，除非帶 `--yes` |
| 機密 | CLI 任何輸出都不得包含金鑰；`--help` 不顯示任何預設金鑰值 |
| 無 `.env` | `demo` 與 `--help` 必須能在無 `.env` 時正常運作 |

---

## 部署：Mac Mini

### `scripts/com.jam.microtx.plist`

launchd 設定範本，要點：

```xml
<key>KeepAlive</key>
<dict><key>SuccessfulExit</key><false/></dict>   <!-- 崩潰自動重啟，正常退出不重啟 -->
<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>/path/to/logs/stdout.log</string>
<key>StandardErrorPath</key><string>/path/to/logs/stderr.log</string>
<key>WorkingDirectory</key><string>/path/to/Shioaji-MicroTX-Engine</string>
```

⚠️ plist 中**不可**寫入任何金鑰。金鑰一律留在 `.env`（權限設 `chmod 600`）。

### `scripts/install-macmini.sh`

安裝腳本需檢查並提示：

1. Python 版本 ≥ 3.10
2. 虛擬環境已建立、依賴已安裝
3. `.env` 存在且權限為 600（否則 `chmod 600` 並警告）
4. **系統時間自動同步已開啟**（`sudo systemsetup -getusingnetworktime`）
   —— 時間偏差會導致 `Sign data is timeout` 登入失敗
5. **自動睡眠已關閉**（`pmset -g | grep sleep`）
6. `runtime/`、`logs/` 目錄存在
7. 載入 launchd：`launchctl bootstrap gui/$(id -u) <plist>`

腳本必須是**冪等**的，重複執行不出錯。

### `scripts/healthcheck.sh`

依上表的四種判定輸出結果，可掛 cron 每 5 分鐘執行。退出碼：

| 退出碼 | 意義 |
|---|---|
| `0` | 正常 |
| `1` | 引擎未運行 |
| `2` | 快照過期（引擎無回應） |
| `3` | 快照降級（卡在共用鎖，建議立即 panic） |

只用 POSIX 工具（`python3 -c` 解析 JSON 即可，不要求 `jq`）。

---

## shellcheck 的替代方案：加入 CI

本機沒有 `shellcheck` 是合理的，**不要為此阻塞交付**。

- 交付時以 `bash -n <script>` 做語法檢查即可，並在回報中註明未跑 shellcheck
- 改為在 `.github/workflows/ci.yml` 加入 shellcheck 步驟 ——
  GitHub runner 內建，零安裝成本

`ci.yml` 內容（本任務新增）：

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[dev]"          # 刻意不裝 live extra
      - run: ruff format --check src tests
      - run: ruff check src tests
      - run: mypy src
      - run: pytest -m "not integration" --cov=microtx
      - run: microtx demo                      # 驗證離線 Demo 可跑
  shellcheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: shellcheck scripts/*.sh
```

> `pip install -e ".[dev]"` **刻意不裝 `live`**：
> 讓 CI 每次都驗證「未安裝券商 SDK 時全套測試與 Demo 仍可運行」，
> 把 07b 建立的隔離性變成持續驗證的保證，而不是一次性的宣稱。
>
> 對作品集而言，一個綠色 CI badge 也比任何自述都有說服力。
> 記得在 `README.md` 頂端加上 badge。

---

## 測試要求

| 測試 | 說明 |
|---|---|
| `panic` 引擎未運行 | 退出碼 1，stderr 含「引擎未運行」與手動平倉提醒 |
| `panic --yes` 正常路徑 | 用 mock 驗證 `os.kill` 以 `SIGUSR1` 被呼叫 |
| `flatten --yes` | 驗證送出的是 `SIGUSR2` |
| `panic` 未帶 `--yes` 且非 TTY | 不得靜默執行，應要求確認或報錯 |
| 實盤 `run` 無確認 | 拒絕啟動 |
| `--help` 無 `.env` | 正常輸出，不拋例外 |
| `demo` | 在無 `.env`、無網路、**且未安裝 shioaji** 的環境下完整跑完，退出碼 0 |
| 退出碼 | 各種錯誤情境的退出碼正確 |
| `status.json` 原子性 | 併發讀寫下讀到的一律是完整 JSON，不得出現半截檔案 |
| `status.json` 無機密 | 以含機密的 Settings 產生快照，序列化結果不含任何金鑰／密碼／身分證字號 |
| 降級快照 | 另一執行緒持鎖不放時，仍在 `status_write_interval_sec + 1` 秒內寫出 `degraded: true` 的新鮮快照 |
| `status` 三態顯示 | 正常／降級／過期三種 `status.json` 各對應正確的 CLI 輸出與退出碼 |
| 寫檔失敗 | 目錄不可寫時只記 WARNING，引擎繼續運行不崩潰 |

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] `microtx demo` 在乾淨環境（無 `.env`、無網路）可完整執行
- [ ] `microtx panic --help` 的說明文字清楚描述「會平掉所有部位並停機」
- [ ] `install-macmini.sh` 通過 `shellcheck`
- [ ] plist 與腳本中零金鑰
