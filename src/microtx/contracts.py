"""台指期商品規格表。

台灣期交所的臺股指數期貨共有三種規格，差異只在契約乘數（每點價值）：

===========  ======  ==========  ==============
商品         代碼    連續近月     每點價值 (NTD)
===========  ======  ==========  ==============
臺股期貨     TXF     TXFR1       200  (大台)
小型臺指期貨 MXF     MXFR1       50   (小台)
微型臺指期貨 TMF     TMFR1       10   (微台)
===========  ======  ==========  ==============

.. warning::
   ``MXF`` 是**小台**、``TMF`` 才是**微台**，兩者常被混稱，請勿弄錯。

本模組讓引擎僅憑 ``.env`` 的 ``SYMBOL`` 參數即可在三種商品間切換，
所有損益計算一律透過 :meth:`FuturesSpec.points_to_ntd` 換算，
避免把乘數硬編碼在策略邏輯裡。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class FuturesSpec:
    """單一期貨商品的靜態規格。

    Attributes:
        category: Shioaji ``api.Contracts.Futures`` 底下的類別代碼，如 ``TMF``。
        symbol: 訂閱與下單使用的代碼，通常為連續近月，如 ``TMFR1``。
        display_name: 中文顯示名稱。
        point_value: 每點價值（新台幣）。
        tick_size: 最小跳動點數，台指期三種商品皆為 1 點。
    """

    category: str
    symbol: str
    display_name: str
    point_value: int
    tick_size: int = 1

    def points_to_ntd(self, points: float) -> float:
        """將點數換算為新台幣金額。

        Args:
            points: 點數（可為負，代表虧損）。

        Returns:
            對應的新台幣金額。

        Examples:
            >>> TMF.points_to_ntd(50)
            500.0
        """
        return points * self.point_value

    def ntd_to_points(self, ntd: float) -> float:
        """將新台幣金額換算為點數。

        Args:
            ntd: 新台幣金額。

        Returns:
            對應的點數。
        """
        return ntd / self.point_value

    def round_to_tick(self, price: float) -> int:
        """將價格對齊至合法的最小跳動單位。

        Args:
            price: 原始價格。

        Returns:
            對齊後的整數價格。
        """
        return int(round(price / self.tick_size) * self.tick_size)


# --------------------------------------------------------------------------
#  商品定義
# --------------------------------------------------------------------------

TXF: Final = FuturesSpec(
    category="TXF",
    symbol="TXFR1",
    display_name="臺股期貨（大台）",
    point_value=200,
)

MXF: Final = FuturesSpec(
    category="MXF",
    symbol="MXFR1",
    display_name="小型臺指期貨（小台）",
    point_value=50,
)

TMF: Final = FuturesSpec(
    category="TMF",
    symbol="TMFR1",
    display_name="微型臺指期貨（微台）",
    point_value=10,
)


_REGISTRY: Final[dict[str, FuturesSpec]] = {}
for _spec in (TXF, MXF, TMF):
    # 同時支援連續近月代碼（TMFR1）與類別代碼（TMF）兩種寫法
    _REGISTRY[_spec.symbol.upper()] = _spec
    _REGISTRY[_spec.category.upper()] = _spec
    # 次月連續合約
    _REGISTRY[f"{_spec.category}R2".upper()] = _spec

SUPPORTED_SYMBOLS: Final = MappingProxyType(_REGISTRY)
"""唯讀的商品查詢表，避免執行期被意外修改。"""


def get_spec(symbol: str) -> FuturesSpec:
    """依代碼取得商品規格。

    Args:
        symbol: 商品代碼，支援 ``TMFR1`` / ``TMF`` / ``tmfr1`` 等寫法。

    Returns:
        對應的 :class:`FuturesSpec`。

    Raises:
        ValueError: 代碼不在支援清單中。
    """
    key = symbol.strip().upper()
    try:
        return SUPPORTED_SYMBOLS[key]
    except KeyError as exc:
        supported = ", ".join(sorted({s.symbol for s in (TXF, MXF, TMF)}))
        raise ValueError(f"不支援的商品代碼：{symbol!r}。目前支援：{supported}") from exc
