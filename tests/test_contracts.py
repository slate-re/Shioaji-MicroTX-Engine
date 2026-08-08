"""商品規格表單元測試。"""

from __future__ import annotations

import pytest

from microtx.contracts import MXF, TMF, TXF, FuturesSpec, get_spec


class TestFuturesSpec:
    """契約規格換算邏輯。"""

    @pytest.mark.parametrize(
        ("spec", "expected_point_value"),
        [(TXF, 200), (MXF, 50), (TMF, 10)],
    )
    def test_point_value(self, spec: FuturesSpec, expected_point_value: int) -> None:
        """三種台指期的每點價值必須正確——這是損益計算的根基。"""
        assert spec.point_value == expected_point_value

    def test_points_to_ntd(self) -> None:
        """微台賺 50 點 = 500 元。"""
        assert TMF.points_to_ntd(50) == 500.0
        assert MXF.points_to_ntd(50) == 2500.0
        assert TXF.points_to_ntd(50) == 10_000.0

    def test_points_to_ntd_negative(self) -> None:
        """虧損（負點數）換算同樣成立。"""
        assert TMF.points_to_ntd(-30) == -300.0

    def test_ntd_to_points_roundtrip(self) -> None:
        """金額與點數雙向換算應可還原。"""
        for spec in (TXF, MXF, TMF):
            assert spec.ntd_to_points(spec.points_to_ntd(37.0)) == pytest.approx(37.0)

    def test_round_to_tick(self) -> None:
        """台指期最小跳動為 1 點，小數應對齊為整數。"""
        assert TMF.round_to_tick(23150.4) == 23150
        assert TMF.round_to_tick(23150.6) == 23151

    def test_spec_is_immutable(self) -> None:
        """規格為凍結 dataclass，防止執行期被誤改。"""
        with pytest.raises(AttributeError):
            TMF.point_value = 999  # type: ignore[misc]


class TestGetSpec:
    """代碼查詢。"""

    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("TMFR1", TMF),
            ("tmfr1", TMF),
            ("  TMF  ", TMF),
            ("TMFR2", TMF),
            ("MXFR1", MXF),
            ("TXFR1", TXF),
        ],
    )
    def test_lookup_variants(self, symbol: str, expected: FuturesSpec) -> None:
        """支援大小寫、空白、類別碼與次月連續等多種寫法。"""
        assert get_spec(symbol) is expected

    def test_mxf_is_not_tmf(self) -> None:
        """MXF 是小台、TMF 才是微台——這兩者常被混淆，明確鎖死。"""
        assert get_spec("MXFR1").point_value == 50
        assert get_spec("TMFR1").point_value == 10
        assert get_spec("MXFR1") is not get_spec("TMFR1")

    def test_unknown_symbol_raises(self) -> None:
        """不支援的代碼應在啟動階段就明確報錯。"""
        with pytest.raises(ValueError, match="不支援的商品代碼"):
            get_spec("2330")
