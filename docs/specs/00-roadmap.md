# 實作路線圖（Roadmap）

> 給實作代理：**一次只做一份任務單**。每份都是可獨立驗收的交付單位。
> 開工前先讀 `AGENTS.md` 與 `docs/architecture.md`。

## 任務清單

| # | 任務單 | 產出 | 依賴 | 可離線測試 |
|---|---|---|---|---|
| 01 | [`01-foundation.md`](01-foundation.md) | `exceptions.py`、`broker/base.py`、`utils/retry.py` | 無 | ✅ |
| 02 | [`02-paper-gateway.md`](02-paper-gateway.md) | `broker/paper_gateway.py` | 01 | ✅ |
| 03 | [`03-market-feed.md`](03-market-feed.md) | `market/{tick,feed}.py` | 01 | ✅ |
| 04 | [`04-strategies.md`](04-strategies.md) | `strategies/{base,scalp,oco}.py` | 03 | ✅ |
| 05 | [`05-engine-core.md`](05-engine-core.md) | `engine/{position,risk,order_router,scheduler}.py` | 02,03,04 | ✅ |
| 06 | [`06-emergency-close.md`](06-emergency-close.md) | 🚨 `engine/emergency.py`、`engine/engine.py` | 05 | ✅ |
| 07 | [`07-shioaji-gateway.md`](07-shioaji-gateway.md) | `broker/shioaji_gateway.py` | 01,06 | ⚠️ 需帳號 |
| 08 | [`08-cli-deploy.md`](08-cli-deploy.md) | `cli/`、`__main__.py`、`scripts/` | 06,07 | ✅ |

## 已完成（不要改動其公開介面）

| 檔案 | 內容 |
|---|---|
| `src/microtx/config.py` | `Settings`（pydantic-settings）、`get_settings()` |
| `src/microtx/contracts.py` | `FuturesSpec`、`get_spec()`、TXF/MXF/TMF |
| `src/microtx/enums.py` | `Direction`、`TriggerMode`、`OrderIntent`、`StrategyState`、`EngineState`、`SessionType` |
| `src/microtx/utils/logger.py` | `setup_logging()`、`get_logger()`、`SecretMaskingFilter` |

> 這些檔案需要**擴充**時（例如 `enums.py` 要加 `CloseMode`、`PriceType`、`TimeInForce`），
> 各任務單會明確列出要新增的成員。新增可以，**修改或刪除既有成員需先提問**。

## 每份任務單的驗收流程

```bash
ruff format --check src tests && \
ruff check src tests && \
mypy src && \
pytest --cov=microtx --cov-report=term-missing
```

四項全綠才算完成，並在回覆中附上實際輸出與設計取捨說明。
