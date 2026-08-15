# 運維手冊

> 日常操作、狀態判讀與問題排查。安裝與部署見 [`deployment.md`](deployment.md)。

## 目錄

1. [委託與策略狀態對照](#1-委託與策略狀態對照)
2. [查看日誌](#2-查看日誌)
3. [執行期檔案](#3-執行期檔案)
4. [疑難排解](#4-疑難排解)

---

## 1. 委託與策略狀態對照

### 策略狀態機（`StrategyState`）

```
IDLE ──arm()──> ARMED ──觸價──> ENTRY_PENDING ──成交──> IN_POSITION
                  │                   │                      │
             cancel()            on_reject()          停利/停損/強平
                  ▼                   ▼                      ▼
             CANCELLED           CANCELLED             EXIT_PENDING
                                                             │成交
                                                             ▼
                                                          CLOSED
        任何非終態 ──microtx panic / flatten──> ABORTED
```

| 狀態 | 意義 | 此時該做什麼 |
|---|---|---|
| `IDLE` | 尚未啟動 | — |
| `ARMED` | 監控中，等待觸價 | 正常，可耐心等 |
| `ENTRY_PENDING` | 進場單已送出，等成交回報 | 停在這超過幾秒 → 查日誌看是否被拒 |
| `IN_POSITION` | 持倉中，監控停利停損 | 正常 |
| `EXIT_PENDING` | 出場單已送出，等成交 | 停在這太久 → 可能漲跌停鎖死，考慮 `panic` |
| `CLOSED` | 正常完成一輪並平倉 | — |
| `CANCELLED` | 主動取消，**從未進場** | — |
| `ABORTED` | 被緊急平倉／強制停機中止，**可能曾持有部位** | 查日誌確認部位已平 |
| `ERROR` | 不可恢復錯誤 | **需人工介入** |

> `CANCELLED` 與 `ABORTED` 的差別是稽核關鍵：前者從未進場，後者可能有過部位。

### 引擎狀態（`EngineState`）

| 狀態 | 意義 |
|---|---|
| `STARTING` | 啟動中 |
| `RUNNING` | 正常運行 |
| `HALTED` | **被風控或 panic 停機** —— 只准平倉，不准開新倉 |
| `SHUTTING_DOWN` | 關機流程中 |
| `STOPPED` | 已停止 |

引擎進入 `HALTED` 的三種原因：

1. 當日虧損達 `MAX_DAILY_LOSS`
2. 執行過 `microtx panic`
3. `daily_state.json` 損毀，風控狀態未知（見 §4）

`HALTED` **不會自動恢復**，需重啟引擎。

### 委託狀態（Shioaji `OrderStatus`）

日誌中會出現的券商端狀態：

| 狀態 | 意義 |
|---|---|
| `PendingSubmit` | 傳送中 |
| `PreSubmitted` | 預約單 |
| `Submitted` | 已送達交易所 |
| `PartFilled` | 部分成交 |
| `Filled` | 完全成交 |
| `Cancelled` | 已刪除 |
| `Failed` | 失敗（日誌會有 `op_msg` 說明原因） |

### 委託意圖（`OrderIntent`）

| 意圖 | 送出方式 | 可否選限價 | 是否經過風控 |
|---|---|---|---|
| `ENTRY` | `MKP+IOC` 或 `LMT+ROD` | ✅ `--entry-order` | ✅ 全部規則 |
| `TAKE_PROFIT` | `MKP+IOC` 或 `LMT+ROD` | ✅ `--tp-order` | ✅（不受虧損上限阻擋） |
| `STOP_LOSS` | `MKP+IOC` 或 `LMT+ROD` | ⚠️ `--sl-order` | ✅（不受虧損上限阻擋） |
| `FORCE_CLOSE` | `MKP` + `IOC` | ❌ 固定 | ✅ |
| `EMERGENCY` | `MKP+IOC`，或 `LMT+IOC` @ 漲跌停價 | ❌ 固定 | ⛔ **完全繞過** |

策略啟動參數可用 `--entry-order`、`--tp-order`、`--sl-order` 分別選擇
`market` 或 `limit`。`limit` 對應 `LMT + ROD`，其中停利與停損限價只允許搭配
固定的絕對價格模式；點數模式仍使用範圍市價。`FORCE_CLOSE` 與 `EMERGENCY`
是安全裝置，無論策略設定為何都固定送出 `MKP + IOC`。

⚠️ `--sl-order limit` 可能在快市穿價後無法成交，讓部位持續裸露。引擎啟動時會
輸出 WARNING，策略摘要也會標示 `SL:LIMIT`；若沒有非常明確的理由，請維持預設
`--sl-order market`。

---

## 2. 查看日誌

日誌位於 `logs/microtx.log`，**每日午夜輪替**，預設保留 30 天
（舊檔為 `microtx.log.2026-08-13` 這種形式）。

```bash
# 即時跟看
tail -f logs/microtx.log

# 只看警告以上
grep -E "WARNING|ERROR|CRITICAL" logs/microtx.log

# 追一筆委託的完整生命週期（client_id 在下單日誌裡）
grep "a1b2c3d4e5f6" logs/microtx.log

# 今天所有成交
grep "成交" logs/microtx.log

# 風控拒絕了什麼
grep "已達\|超過\|停機\|節流" logs/microtx.log

# 緊急平倉紀錄
grep -E "緊急平倉|panic|PANIC|FLATTEN" logs/microtx.log
```

### 日誌格式

```
2026-08-14 10:23:45 | INFO     | microtx.engine.order_router | 已送出委託 client_id=a1b2c3 ...
    ↑ 時間              ↑ 等級     ↑ 模組                        ↑ 訊息
```

### 該注意的等級

| 等級 | 代表 |
|---|---|
| `WARNING` | 可自行恢復，但值得看一眼（撤單失敗、部位不同步、寫檔失敗） |
| `ERROR` | 單次操作失敗（下單被拒、查詢失敗） |
| `CRITICAL` | **需要你立刻處理**（緊急平倉未完成、無法取得共用鎖、狀態檔損毀） |

> 🔒 **日誌不會包含金鑰。** `SecretMaskingFilter` 會把疑似金鑰的字串改寫為
> `***MASKED***` 後才寫檔，因此日誌可以安全地貼給別人看。

---

## 3. 執行期檔案

全部位於 `runtime/`，**已被 `.gitignore` 排除**。

| 檔案 | 用途 | 遺失的後果 |
|---|---|---|
| `microtx.pid` | CLI 靠它找到常駐行程送訊號 | `panic` / `flatten` / `status` 會說「引擎未運行」 |
| `status.json` | 引擎健康快照，每 5 秒 | `status` 與 `watch` 無資料 |
| `daily_state.json` | **當日累計損益與交易次數** | ⚠️ 重啟後單日停損上限歸零 |
| `quote.json` | 報價快照，每 250ms | `watch` 沒有價格顯示 |

⛔ **不要手動編輯 `daily_state.json`。** 若真的需要重設，用
`microtx run --reset-daily-state`，那是有意識的動作且會留下日誌。

---

## 4. 疑難排解

### 登入相關

**`Sign data is timeout`**

系統時間與券商伺服器差異過大。

```bash
sudo systemsetup -getusingnetworktime        # 應為 On
sudo systemsetup -setusingnetworktime on
```

**`Shioaji 登入失敗`**

依序確認：

1. `.env` 的 `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY` 有沒有填錯（注意結尾空白）
2. API Key 的**權限**是否勾了「行情/資料」「帳務」「交易」
3. 是否設了 **IP 限制**而你的對外 IP 變了
4. Key 是否已過期

**`期貨帳號未簽署或不存在`**

- 沒有期貨帳戶 → 需另外開戶
- 有帳戶但 `signed=False` → 到[簽署中心](https://www.sinotrade.com.tw/newweb/signCenter/signCenterIndex/)完成**期貨** API 簽署，再跑一次模擬環境測試

**`未安裝 shioaji`**

```bash
pip install -e ".[live]"
```

只想跑離線 Demo 的話不需要它。

---

### 執行相關

**`microtx panic` 說「引擎未運行」但你確定它在跑**

`runtime/microtx.pid` 可能是崩潰後的殘留檔，或引擎其實已經死了。

```bash
ps aux | grep microtx          # 確認行程是否存在
cat runtime/microtx.pid
```

⚠️ **若確認仍有部位，先到永豐下單軟體手動平倉**，再處理程式問題。

**`microtx status` 顯示 `DEGRADED`**

引擎活著，但拿不到共用鎖 —— 有其他路徑卡住了（通常是網路停滯的下單呼叫）。

```bash
microtx panic       # 緊急平倉會以「無鎖模式」強制執行，這條路徑不會被卡住
```

**`microtx status` 顯示 `NO RESPONSE`**

PID 還在但 `status.json` 超過 15 秒沒更新 —— 行程可能整個僵住。

```bash
microtx panic       # 先試這個
kill -9 <PID>       # 無效再強制終止，然後到下單軟體確認部位
```

**引擎啟動後直接進入 `HALTED`**

看日誌開頭的 CRITICAL。最常見是 `daily_state.json` 損毀 ——
此時「今天已虧多少」是未知數，引擎刻意不開新倉。

```bash
cat runtime/daily_state.json          # 看看壞成什麼樣
microtx run --reset-daily-state       # 確認可從 0 起算後才用這個
```

⚠️ 這個旗標會把當日累計歸零。**若你今天已經虧損接近上限，用它等於拆掉風控天花板。**

---

### 交易相關

**策略停在 `ARMED` 一直不觸發**

- 目前是不是非交易時段？（`microtx status` 看 `session`）
- 觸發價設得離現價太遠？
- 行情有進來嗎？看日誌的 `feed` 統計，`received` 有沒有在增加

**策略停在 `ENTRY_PENDING` 不動**

委託可能被拒。

```bash
grep -E "拒絕|Failed|op_msg" logs/microtx.log | tail -20
```

常見原因：保證金不足、委託價超出漲跌停、帳號未簽署。

**新倉一直被風控拒絕**

日誌會寫明原因：

| 訊息 | 處置 |
|---|---|
| `已達單日停損` | 今天不該再交易了 |
| `已達單日交易上限` | 同上，或調整 `MAX_DAILY_TRADES` |
| `將超過最大持倉` | 檢查 `MAX_POSITION_SIZE` |
| `下單節流中` | 正常，等 `ORDER_COOLDOWN_SEC` 秒 |
| `非交易時段` | 等開盤 |
| `引擎已停機` | 見上方 `HALTED` |

**部位與券商不一致的告警**

```
緊急平倉發現部位不同步：...
```

引擎每 60 秒比對一次。**以券商為準** —— 所有平倉動作都是直接查券商部位，
所以這個告警代表引擎內部帳有問題，但不影響平倉正確性。請把日誌保留下來回報。

---

### 沒有出現在這裡的問題

1. 先看 `logs/microtx.log` 的 CRITICAL 與 ERROR
2. 日誌已自動遮蔽金鑰，可以安全貼出
3. 到 [Issues](https://github.com/slate-re/Shioaji-MicroTX-Engine/issues) 回報，附上日誌片段與 `microtx status` 輸出

> ⚠️ 任何情況下，**只要不確定部位狀態，就先到永豐下單軟體確認並手動平倉**。
> 程式的問題可以慢慢查，裸露的部位不行。
