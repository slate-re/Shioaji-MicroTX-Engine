# 任務 01 — 型別基礎與券商抽象介面

## 目標

建立全專案共用的例外體系、券商閘道抽象介面與重試工具。
**本任務不含任何實際券商邏輯**，只定義契約。

## 檔案清單

| 檔案 | 動作 |
|---|---|
| `src/microtx/exceptions.py` | 新增 |
| `src/microtx/broker/base.py` | 新增 |
| `src/microtx/utils/retry.py` | 新增 |
| `src/microtx/enums.py` | **擴充**（見下方） |
| `tests/test_exceptions.py`、`tests/test_retry.py` | 新增 |

## enums.py 需新增

```python
class PriceType(str, Enum):
    """委託價格別，對應 Shioaji FuturesPriceType。"""
    LMT = "LMT"   # 限價
    MKP = "MKP"   # 範圍市價（本專案的市價首選，滑價有上限）
    MKT = "MKT"   # 市價（不建議，極端行情滑價無上限）

class TimeInForce(str, Enum):
    """委託時效，對應 Shioaji OrderType。"""
    ROD = "ROD"
    IOC = "IOC"
    FOK = "FOK"

class CloseMode(str, Enum):
    """緊急平倉語意。"""
    FLATTEN = "FLATTEN"
    PANIC = "PANIC"
```

並在 `OrderIntent` 新增成員：

```python
EMERGENCY = "EMERGENCY"
"""緊急平倉，繞過 RiskManager。"""
```

## exceptions.py

```python
class MicroTXError(Exception):
    """本專案所有例外的根。"""

class ConfigError(MicroTXError): ...
class BrokerError(MicroTXError): ...
class ConnectionLostError(BrokerError): ...
class OrderRejectedError(BrokerError):
    """含 broker 回傳的 op_code / op_msg。"""
    def __init__(self, message: str, *, code: str = "", client_id: str = "") -> None: ...
class RiskViolationError(MicroTXError): ...
class StrategyError(MicroTXError): ...

class EmergencyCloseError(MicroTXError):
    """緊急平倉流程中無法繼續的錯誤。

    ⚠️ 注意：``EmergencyCloser.execute()`` **不會**把本例外拋給呼叫端
    （見 `06-emergency-close.md`：execute() 在任何情況下都不得向外拋例外）。
    本例外僅用於 execute() 的**內部**流程控制，以及 CLI 端的錯誤回報。

    平倉結果一律透過**回傳值** ``CloseReport`` 傳遞，不透過例外攜帶。
    因此本例外**不持有** ``CloseReport``，只帶純量診斷欄位，
    讓 ``exceptions.py`` 維持零專案內部依賴（僅 import 標準庫）。
    """

    def __init__(
        self,
        message: str,
        *,
        mode: str = "",              # CloseMode 的字串值，避免對 enums 產生依賴
        source: str = "",            # 觸發來源，如 "SIGUSR1"
        residual_quantity: int = 0,  # 仍未平掉的口數
    ) -> None: ...
```

要求：

- 每個例外都要有繁中 docstring 說明「什麼情況會拋」
- `OrderRejectedError` 的 `str()` 必須包含 client_id 與 code，方便日誌追查
- **例外訊息中禁止帶入任何金鑰或帳號**
- `exceptions.py` **只 import 標準庫**，不 import 專案內任何模組
  （它是最底層，任何人都可以 import 它而不必擔心循環依賴）

## broker/base.py

完整型別簽章見 `docs/architecture.md` §4.1，逐字實作，包含：

`Position`、`OrderRequest`、`OrderAck`、`OpenOrder`、`RawTick`、`OrderEvent`、
`FillEvent`、`RejectEvent`、`BrokerGateway(ABC)`。

補充定義：

```python
@dataclass(frozen=True, slots=True)
class RawTick:
    """券商原生 tick 的最小共通表示。simtrade 尚未過濾。"""
    code: str
    timestamp: datetime
    price: float
    volume: int
    total_volume: int
    tick_type: int
    simtrade: bool

@dataclass(frozen=True, slots=True)
class FillEvent:
    """成交回報。"""
    client_id: str | None
    broker_order_id: str
    code: str
    action: Direction
    price: float
    quantity: int          # 本次成交量
    timestamp: datetime

@dataclass(frozen=True, slots=True)
class RejectEvent:
    """委託被拒。對應 Shioaji 委託回報 operation.op_code != "00"。"""
    client_id: str | None
    broker_order_id: str | None
    code: str              # 券商錯誤碼（op_code）
    message: str           # 券商錯誤訊息（op_msg）
    timestamp: datetime

@dataclass(frozen=True, slots=True)
class AckEvent:
    """委託已被交易所接受。

    對應 Shioaji 委託回報 ``operation.op_type == "New"`` 且 ``op_code == "00"``。

    ``exchange_order_no``（Shioaji 的 ``ordno``）是本事件存在的主要理由：
    依 `docs/shioaji_guide.md`，**改單與刪單前必須先取得 ordno**。
    由 AckEvent 帶出來讓 OrderRouter 快取，
    緊急平倉時就不必臨時呼叫 ``update_status()`` 等待往返 —— 省下的是救命的秒數。
    """
    client_id: str | None
    broker_order_id: str        # Shioaji order.id / status.id
    exchange_order_no: str      # Shioaji ordno；尚未取得時為空字串
    code: str                   # 商品代碼（含月份，如 TMFF6）
    action: Direction
    price: float
    quantity: int               # 委託總量
    timestamp: datetime

@dataclass(frozen=True, slots=True)
class CancelEvent:
    """委託被刪除或失效。

    涵蓋三種情況，由 ``reason`` 區分：
    使用者主動刪單、IOC/FOK 未成交自動失效、收盤未成交作廢。
    """
    client_id: str | None
    broker_order_id: str
    code: str
    cancelled_quantity: int     # 本次取消口數
    remaining_quantity: int     # 取消後仍在市場上的口數（部分刪除時 > 0）
    timestamp: datetime
    reason: str = ""            # "user" / "ioc_expired" / "fok_expired" / "session_end" / ""

OrderEvent = FillEvent | RejectEvent | AckEvent | CancelEvent
```

### 四個 event 的設計約束

| 約束 | 理由 |
|---|---|
| **不要用繼承抽共同基底** | `frozen=True, slots=True` 的 dataclass 繼承會踩到「有預設值的欄位不能排在無預設值之前」的陷阱，且 `slots` 繼承行為易出錯。四個型別各自扁平定義，重複幾個欄位是可接受的代價 |
| 消費端用 `match` 分派 | `match event: case FillEvent(): ...`，mypy 能做窮盡性檢查；新增 event 型別時漏處理會被靜態抓出來 |
| 共同欄位命名必須一致 | 四者的 `client_id` / `broker_order_id` / `code` / `timestamp` 語意與型別完全相同，方便日誌統一處理 |
| `client_id` 皆為 `str \| None` | 成交回報可能**早於**委託回報抵達，此時還來不及建立 client_id 對映。消費端必須容忍 `None`，以 `broker_order_id` 為主鍵 |

實作要點：

- `BrokerGateway` 用 `abc.ABC` + `@abstractmethod`，**不提供任何預設實作**
- `OrderRequest.client_id` 提供 `new_client_id()` 工廠函式（UUID4 hex 前 16 碼）
- 所有 dataclass 為 `frozen=True, slots=True`
- 加入 `__post_init__` 驗證：`quantity > 0`、`price` 為 `LMT` 時不得為 `None`

## utils/retry.py

```python
def retry(
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
    exceptions: tuple[type[Exception], ...] = (BrokerError,),
    logger_name: str = "microtx.retry",
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """指數退避重試裝飾器。"""
```

要求：

- 退避公式 `min(base_delay * 2 ** (n-1), max_delay)`，加入 ±20% jitter 避免同步重試
- 每次重試寫 WARNING 日誌（含第幾次、下次等待秒數）
- 最後一次仍失敗則原樣拋出原例外（保留 traceback，不要包一層）
- 必須保留被裝飾函式的簽章型別（用 `ParamSpec` / `TypeVar`）
- **不可**捕捉 `KeyboardInterrupt` 或 `SystemExit`

## 測試要求

- `retry`：成功、第 N 次成功、全失敗拋出、退避秒數正確（用 mock 掉 `time.sleep`）
- `retry`：不在 `exceptions` 清單中的例外**不重試**，直接拋出
- 例外訊息不含機密
- `EmergencyCloseError` 的 `str()` 含 mode / source / residual_quantity
- `OrderRequest` 的 `__post_init__` 驗證：`quantity=0` 應拋 `ValueError`
- dataclass 不可變性
- `OrderEvent` 的 `match` 分派窮盡性：四種 event 各建一個實例，
  確認 `match` 都能正確落到對應分支（並讓 mypy 檢出遺漏分支）

## 驗收條件

- [ ] 四項驗收指令全綠
- [ ] `broker/base.py` 沒有 import 任何 shioaji 相關模組
- [ ] `mypy --strict` 對 `retry` 裝飾器的型別保留無誤
- [ ] 新測試覆蓋率 ≥ 85%
