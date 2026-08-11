"""MicroTX 與 Shioaji 列舉的集中式雙向轉換。

``CancelEvent.reason`` 並非 Shioaji SDK 欄位，而是本專案依下列順序推導：
主動取消、IOC 到期、FOK 到期、ROD 收盤作廢。若缺少原始 request 則回傳空字串，
不可猜測。這是 MicroTX 的稽核語意，不應被誤認為券商原生資訊。
"""

from __future__ import annotations

from collections.abc import Set
from importlib import import_module
from types import ModuleType
from typing import cast

from microtx.broker.base import OrderRequest
from microtx.enums import Direction, PriceType, TimeInForce
from microtx.exceptions import BrokerError

_INSTALL_MESSAGE = (
    '未安裝 shioaji。若要連線永豐請執行 pip install -e ".[live]"；'
    "若只是要跑離線 Demo 或測試，請改用 PaperGateway。"
)


def _require_shioaji() -> ModuleType:
    """延遲載入選用 SDK，缺少時提供可立即操作的下一步。"""
    try:
        return import_module("shioaji")
    except ImportError as exc:
        raise BrokerError(_INSTALL_MESSAGE) from exc


def _lookup(mapping: dict[object, object], value: object, label: str) -> object:
    try:
        return mapping[value]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"未知的 {label}：{value!r}") from exc


def to_shioaji_action(value: Direction) -> object:
    """將交易方向轉為 Shioaji Action。"""
    sj = _require_shioaji()
    return _lookup({Direction.LONG: sj.Action.Buy, Direction.SHORT: sj.Action.Sell}, value, "方向")


def from_shioaji_action(value: object) -> Direction:
    """將 Shioaji Action 轉為交易方向。"""
    sj = _require_shioaji()
    return cast(
        Direction,
        _lookup({sj.Action.Buy: Direction.LONG, sj.Action.Sell: Direction.SHORT}, value, "Action"),
    )


def to_shioaji_price_type(value: PriceType) -> object:
    """將價格別轉為 Shioaji FuturesPriceType。"""
    sj = _require_shioaji()
    return _lookup(
        {
            PriceType.LMT: sj.FuturesPriceType.LMT,
            PriceType.MKP: sj.FuturesPriceType.MKP,
            PriceType.MKT: sj.FuturesPriceType.MKT,
        },
        value,
        "價格別",
    )


def from_shioaji_price_type(value: object) -> PriceType:
    """將 Shioaji FuturesPriceType 轉為價格別。"""
    sj = _require_shioaji()
    return cast(
        PriceType,
        _lookup(
            {
                sj.FuturesPriceType.LMT: PriceType.LMT,
                sj.FuturesPriceType.MKP: PriceType.MKP,
                sj.FuturesPriceType.MKT: PriceType.MKT,
            },
            value,
            "FuturesPriceType",
        ),
    )


def to_shioaji_time_in_force(value: TimeInForce) -> object:
    """將委託時效轉為 Shioaji OrderType。"""
    sj = _require_shioaji()
    return _lookup(
        {
            TimeInForce.ROD: sj.OrderType.ROD,
            TimeInForce.IOC: sj.OrderType.IOC,
            TimeInForce.FOK: sj.OrderType.FOK,
        },
        value,
        "委託時效",
    )


def from_shioaji_time_in_force(value: object) -> TimeInForce:
    """將 Shioaji OrderType 轉為委託時效。"""
    sj = _require_shioaji()
    return cast(
        TimeInForce,
        _lookup(
            {
                sj.OrderType.ROD: TimeInForce.ROD,
                sj.OrderType.IOC: TimeInForce.IOC,
                sj.OrderType.FOK: TimeInForce.FOK,
            },
            value,
            "OrderType",
        ),
    )


def to_shioaji_octype(value: str) -> object:
    """將設定中的統一期貨倉別轉為 Shioaji FuturesOCType。"""
    sj = _require_shioaji()
    return _lookup(
        {"Auto": sj.FuturesOCType.Auto, "DayTrade": sj.FuturesOCType.DayTrade},
        value,
        "期貨倉別",
    )


def from_shioaji_octype(value: object) -> str:
    """將 Shioaji FuturesOCType 轉為設定字串。"""
    sj = _require_shioaji()
    return cast(
        str,
        _lookup(
            {sj.FuturesOCType.Auto: "Auto", sj.FuturesOCType.DayTrade: "DayTrade"},
            value,
            "FuturesOCType",
        ),
    )


def infer_cancel_reason(
    client_id: str | None,
    pending_cancels: Set[str],
    original_request: OrderRequest | None,
) -> str:
    """依本專案規則推導取消原因；Shioaji 本身不提供此欄位。"""
    if client_id is None or original_request is None:
        return ""
    if client_id in pending_cancels:
        return "user"
    if original_request.time_in_force is TimeInForce.IOC:
        return "ioc_expired"
    if original_request.time_in_force is TimeInForce.FOK:
        return "fok_expired"
    return "session_end"
