# 任務 08 — CLI 與 Mac Mini 部署

## 目標

提供人類介面與部署工具。**CLI 是薄殼，不含任何商業邏輯。**

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/cli/__init__.py`、`cli/commands.py` | 新增 |
| `src/microtx/__main__.py` | 新增 |
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

讀取 PID 檔與最新狀態快照檔（`runtime/status.json`，由引擎每 5 秒寫入），
顯示：引擎狀態、目前部位、當日損益、交易次數、行情延遲、已武裝策略。

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

檢查引擎是否存活（PID + `runtime/status.json` 的時間戳新鮮度），
可掛在 cron 每 5 分鐘執行，異常時發通知。

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
| `demo` | 在無 `.env`、無網路下完整跑完並退出碼 0 |
| 退出碼 | 各種錯誤情境的退出碼正確 |

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] `microtx demo` 在乾淨環境（無 `.env`、無網路）可完整執行
- [ ] `microtx panic --help` 的說明文字清楚描述「會平掉所有部位並停機」
- [ ] `install-macmini.sh` 通過 `shellcheck`
- [ ] plist 與腳本中零金鑰
