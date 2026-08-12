# 部署指南（macOS 常駐）

> 本文為長期無人值守運行的實務筆記。若你只是要試跑，`microtx demo` 即可，不需要看這份。

## 前提

- macOS（使用 `launchd`）
- 已完成 [README](../README.md#安裝與設定) 的安裝步驟
- 若要連線券商，需額外 `pip install -e ".[live]"`

## 安裝

```bash
./scripts/install-macmini.sh
```

腳本是冪等的，重複執行不會出錯。它會檢查並提示：

1. Python ≥ 3.10
2. 虛擬環境與依賴
3. `.env` 存在且權限為 `600`
4. 系統時間自動同步已開啟
5. 電源自動睡眠已關閉
6. `runtime/`、`logs/` 目錄
7. 載入 launchd service

## 三個容易踩的坑

### 1. 系統時間必須自動同步

Shioaji 登入會驗證時間戳，機器時間偏差過大會直接失敗：

```
Sign data is timeout
```

```bash
sudo systemsetup -getusingnetworktime      # 應為 On
```

### 2. 關閉自動睡眠

機器睡著，引擎就停了 —— 而你的部位還在市場上。

```bash
pmset -g | grep sleep
```

### 3. `.env` 不同步、權限收緊

`.env` 與憑證檔**永遠不進 Git**，每台機器各自維護一份。

```bash
chmod 600 .env
```

開發機與正式機的設定本來就該不同（例如口數上限），共用反而危險。

## 常駐設定

`scripts/com.jam.microtx.plist` 是 launchd 範本，重點：

```xml
<key>KeepAlive</key>
<dict><key>SuccessfulExit</key><false/></dict>
```

崩潰時自動重啟，正常退出（例如你手動 `microtx panic`）則不重啟 ——
否則停機指令會被 launchd 反覆拉起來，變成打不死的殭屍。

⚠️ plist 中不可寫入任何金鑰，一律留在 `.env`。

## 監控

```bash
./scripts/healthcheck.sh
```

可掛 cron 每 5 分鐘執行。退出碼：

| 退出碼 | 意義 | 建議動作 |
|---|---|---|
| `0` | 正常 | — |
| `1` | 引擎未運行 | 檢查 launchd 與日誌 |
| `2` | 快照過期，引擎無回應 | 檢查是否卡死，考慮 `microtx panic` |
| `3` | 快照降級，卡在共用鎖 | **立即 `microtx panic`** |

退出碼 `2` 與 `3` 的區別是刻意設計的：

- `2` = 連狀態快照都寫不出來，行程可能整個僵住
- `3` = 行程還活著、還在寫快照，但拿不到共用鎖 —— **問題定位到鎖上**

PID 存活不代表引擎健康。一個徹底卡死的行程 PID 依然在，
只有靠 `runtime/status.json` 的**新鮮度**才分辨得出來。

## 緊急停止

引擎常駐時，另開終端機（或 SSH 進去）：

```bash
microtx flatten    # 平掉所有部位，引擎待命
microtx panic      # 平掉所有部位並停機
```

人不在電腦前也能止損 —— 這是把 kill switch 做成獨立 CLI 而非 GUI 按鈕的理由。
