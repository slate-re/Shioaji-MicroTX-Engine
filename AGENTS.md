# AGENTS.md — 給 AI 編碼代理（Codex / Claude Code）的專案規範

> 本檔是實作者的**唯一行為準則**。動手前先讀本檔，再讀 `docs/architecture.md`，
> 最後讀 `docs/specs/` 底下對應的任務單。

---

## 0. 專案一句話

台指期（微台 TMF / 小台 MXF / 大台 TXF）當沖自動條件單引擎，
基於永豐 Shioaji API，**預設模擬模式**，開源作為個人技術作品集。

## 1. 資料來源優先序

1. `docs/architecture.md` —— 分層職責與**介面契約**（型別簽章以此為準）
2. `docs/specs/NN-*.md` —— 當前任務的實作規格與驗收條件
3. `docs/shioaji_guide.md` —— Shioaji API 用法（**已在地化，不要上網查官方文件**）
4. 既有程式碼 `src/microtx/{config,contracts,enums}.py` —— 已完成，是型別基礎

> ⛔ 不要為了查 Shioaji 用法而發起網路請求。所有需要的 API 細節都在 `docs/shioaji_guide.md`。

---

## 2. 絕對禁止（違反即視為交付失敗）

| # | 禁止事項 | 原因 |
|---|---|---|
| 1 | 任何形式的硬編碼金鑰、密碼、帳號、身分證字號 | 本專案開源，洩漏即事故 |
| 2 | 把預設值改成實盤（`simulation` 預設必須是 `True`） | 他人 clone 後可能誤下真單 |
| 3 | 在 Shioaji 行情 callback 內做重運算、阻塞 I/O 或下單 | 官方明文警告，會漏 tick |
| 4 | 用 `price == trigger` 做觸價判定 | 跳空時永遠不觸發 |
| 5 | 讓緊急平倉（EmergencyCloser）經過 RiskManager 檢查 | 風控可能擋下救命的平倉單 |
| 6 | 提交 `.env`、`*.pfx`、`*.log`、`logs/`、`data/`、`runtime/` | 已在 `.gitignore`，不要用 `git add -f` |
| 7 | 新增 `pyproject.toml` 以外的依賴管理檔或未經核可的第三方套件 | 保持依賴精簡可審計 |
| 8 | 用 `print()` 取代 logging | 日誌需經機密遮蔽 Filter |
| 9 | 為了讓測試通過而放寬斷言或加 `# type: ignore` 掩蓋型別錯誤 | 寧可回報問題也不要造假綠燈 |
| 10 | 未經確認就修改 `config.py` / `contracts.py` / `enums.py` 的既有公開介面 | 它們是全專案的型別基礎 |

---

## 3. 編碼規範

- **Python ≥ 3.10**，每個模組開頭 `from __future__ import annotations`
- **全面型別提示**，`mypy --strict` 必須零錯誤
- **註解與 docstring 使用繁體中文**，docstring 採 Google style（Args / Returns / Raises）
- 行長 100 字元，格式由 `ruff format` 決定，不手動排版
- 資料結構優先用 `@dataclass(frozen=True, slots=True)`；需要驗證時才用 pydantic
- 列舉一律放 `enums.py`，**不要在模組內散落字串字面值**
- 取得 logger 一律 `from microtx.utils.logger import get_logger` + `get_logger(__name__)`
- 例外一律定義在 `microtx/exceptions.py`，繼承自 `MicroTXError`；不要 raise 裸 `Exception`
- 禁止 `except Exception: pass`。捕捉後必須 log，並明確決定是恢復、重試還是升級

### 命名慣例

| 概念 | 命名 |
|---|---|
| 抽象基底 | `XxxGateway` / `XxxStrategy`（ABC） |
| 事件 / 訊息 | `XxxEvent`（frozen dataclass） |
| 請求 / 回報 | `XxxRequest` / `XxxReport` |
| 內部狀態機 | 用 `enums.py` 的 `StrategyState` / `EngineState` |

---

## 4. 測試要求

每個任務單交付時必須附測試，且滿足：

- 商業邏輯（觸價穿越、損益計算、風控閘門、狀態機轉換、緊急平倉流程）**必須有單元測試**
- 測試**不得**依賴真實 Shioaji 連線。用 `PaperGateway` 或 `pytest-mock`
- 需要真連線的測試標記 `@pytest.mark.integration`，預設不執行
- 涉及時間的測試用 `freezegun`，不要 `time.sleep()`
- 新增模組的測試覆蓋率 ≥ 85%
- 每個「已知陷阱」都要有對應的迴歸測試（例如：跳空觸發、試撮價過濾、成交回報早於委託回報）

---

## 5. 驗收指令（交付前必須全綠）

```bash
ruff format --check src tests
ruff check src tests
mypy src
pytest --cov=microtx --cov-report=term-missing
```

任一項失敗即不算完成。**不要用放寬設定的方式讓它變綠。**

---

## 6. 交付流程

1. 一次只做**一份** `docs/specs/NN-*.md` 任務單，不要跨任務改動
2. 只修改該任務單「檔案清單」列出的檔案；需要改動其他檔案時，先在回覆中說明理由
3. 完成後在回覆中提供：
   - 變更檔案清單
   - 驗收指令的實際輸出
   - 你做的**設計取捨**與**未解決的疑慮**（這比程式碼本身更重要）
4. 遇到規格矛盾或缺漏，**停下來提問**，不要自行臆測補完

---

## 7. 領域知識速記

| 項目 | 內容 |
|---|---|
| 商品 | `TXF` 大台 200 元/點、`MXF` 小台 50 元/點、`TMF` 微台 10 元/點 |
| 連續近月 | `TXFR1` / `MXFR1` / `TMFR1`（Python SDK 自動解析實際月份碼） |
| 最小跳動 | 1 點（三者相同） |
| 日盤 | 08:45–13:45，預設 13:40 強制平倉 |
| 夜盤 | 15:00–次日 05:00，預設關閉 |
| 試撮 | `tick.simtrade == True` 為假成交，**必須過濾** |
| 回報順序 | 成交回報可能**早於**委託回報，狀態機須容錯 |
| 改單/刪單 | 前置必須先 `update_status()` 取得 `ordno` |
| 減量 | `update_order` 只能減量，不能加量 |

> ⚠️ `MXF` 是**小台**、`TMF` 才是**微台**。這兩個常被混稱，程式與文件一律以本表為準。
