# 給 Codex 的 Prompt 範本

> 專案規範與規格都已在 repo 內，所以 prompt 只需**指路 + 設界線**，不需要重述內容。
> 在專案根目錄啟動 Codex，讓它能讀到 `AGENTS.md`（Codex 會自動載入同名檔案）。

---

## A. 第一次啟動（任務 01）

```
這是 Shioaji-MicroTX-Engine，一個台指期當沖自動條件單引擎，會開源作為我的技術作品集。

請先依序讀完這三份文件，再動手：
1. AGENTS.md              ← 專案規範與硬性禁令，這是你的行為準則
2. docs/architecture.md   ← 分層職責、介面契約、執行緒模型
3. docs/specs/01-foundation.md  ← 本次任務

然後只實作任務 01。

規則：
- 只改動任務單「檔案清單」裡列出的檔案。需要動其他檔案時先停下來問我。
- src/microtx/{config,contracts,enums,utils/logger}.py 已完成且有測試在跑，
  只能「新增」成員，不可修改或刪除既有公開介面。
- 不要上網查 Shioaji 文件，需要的都在 docs/shioaji_guide.md。
- 完成後跑這四項，全綠才算完成：
    ruff format --check src tests
    ruff check src tests
    mypy src
    pytest --cov=microtx --cov-report=term-missing

交付時請回報：
1. 變更檔案清單
2. 上述四項指令的實際輸出
3. 你做的設計取捨，以及任何你覺得規格有矛盾或缺漏的地方

不確定的地方寧可問我，不要自行臆測補完。
```

---

## B. 後續每個任務（02–08 通用，替換編號即可）

```
繼續下一個任務：docs/specs/02-paper-gateway.md

開工前請先確認你記得 AGENTS.md 的禁令與 docs/architecture.md 的介面契約，
必要時重讀。規則與上次相同：

- 只改動任務單列出的檔案
- 四項驗收指令全綠
- 交付時回報變更清單、指令輸出、設計取捨與疑慮
- 不要為了讓測試過而放寬斷言、加 type: ignore 或降低覆蓋率標準

另外：這次任務有沒有跟前面已完成的模組產生介面衝突？有的話先講。
```

---

## C. 任務 06（緊急平倉）專用強化版

> 這是安全裝置，值得多花一段 prompt 把重點釘死。

```
接下來做 docs/specs/06-emergency-close.md，這是整個專案最重要的模組，請特別謹慎。

設計前提是「引擎自己可能已經壞了」，所以這條路徑要盡可能少依賴引擎內部狀態。
以下三點是設計核心，實作與測試都不可打折：

1. 平倉部位一律來自 gateway.list_positions()，不可用 PositionTracker 的內部狀態
2. 必須先 cancel_all_orders() 再送平倉單。順序反了的話，殘留的進場單成交會讓
   部位從空手變成反向持倉，比觸發 panic 前更危險
3. 平倉單必須走 router.submit_unchecked() 繞過 RiskManager。若走正常 submit()，
   會被「單日虧損已達上限」擋下，變成「因為虧太多所以不准你停損」

另外務必確認：
- execute() 在任何情況下都不得向外拋例外，失敗要反映在 CloseReport.succeeded
- 重試次數必須有上限（漲跌停鎖死時避免無窮迴圈）
- 訊號處理器只能設 Event，實際工作交給 EmergencyWorker 執行緒

任務單裡的 15 項邊界情境每一項都要有測試，其中第 2、3、4 項是設計核心，
請在測試裡用繁中註解說明為什麼要這樣測。emergency.py 覆蓋率要求 95%。

交付時請額外回答一個問題：
「如果引擎的 StrategyWorker 執行緒卡死了，microtx panic 還能不能平掉部位？為什麼？」
```

---

## D. 每個任務完成後，你自己要做的檢查

Codex 說「全綠」不代表真的全綠。合併前跑一次：

```bash
# 1. 獨立驗證（不信 Codex 的輸出）
ruff format --check src tests && ruff check src tests && mypy src && pytest

# 2. 機密掃描
pre-commit run --all-files

# 3. 確認沒有偷偷放寬設定
git diff pyproject.toml

# 4. 確認沒有多改檔案
git status --short
```

重點看第 3 項。最常見的作弊方式是把 `mypy strict = false`、
往 `ruff.lint.ignore` 塞規則、或降低 `--cov-fail-under`。

---

## E. 若 Codex 卡住或偏離

```
停。回去重讀 AGENTS.md 的「絕對禁止」那一節，然後告訴我：
你目前的做法違反了哪一條？如果沒有違反，那是規格哪裡沒寫清楚？

先不要改程式碼，我們先把問題講清楚。
```
