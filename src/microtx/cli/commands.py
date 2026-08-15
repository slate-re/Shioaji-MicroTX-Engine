"""以 argparse 提供不含商業邏輯的命令列介面。"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from microtx.broker.base import FillEvent, OrderRequest, RawTick, new_client_id
from microtx.broker.paper_gateway import PaperGateway
from microtx.config import PROJECT_ROOT, Settings
from microtx.engine.engine import TradingEngine
from microtx.enums import Direction, ExecutionStyle, PriceType, TimeInForce
from microtx.exceptions import MicroTXError
from microtx.market.tick import TickEvent
from microtx.strategies.base import Signal
from microtx.strategies.oco import OcoStrategy
from microtx.strategies.scalp import ScalpStrategy
from microtx.utils.pidfile import PidFile

EXIT_OK = 0
EXIT_NOT_RUNNING = 1
EXIT_USER_ERROR = 2
EXIT_STALE = 2
EXIT_DEGRADED = 3
EXIT_INTERNAL = 70


def build_parser() -> argparse.ArgumentParser:
    """建立不需載入設定或券商 SDK 的 parser。"""
    parser = argparse.ArgumentParser(prog="microtx", description="台指期自動條件單引擎")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="啟動常駐交易引擎")
    run.add_argument("--yes", action="store_true", help="確認實盤啟動")
    run.add_argument(
        "--reset-daily-state",
        action="store_true",
        help="人工確認捨棄損毀的當日累計並從零開始",
    )
    run.add_argument("--strategy", choices=("scalp", "oco"))
    run.add_argument("--direction", choices=("long", "short"))
    run.add_argument("--trigger", type=float)
    run.add_argument("--upper", type=float)
    run.add_argument("--lower", type=float)
    run.add_argument("--tp", type=int)
    run.add_argument("--sl", type=int)
    run.add_argument("--tp-price", type=float)
    run.add_argument("--sl-price", type=float)
    run.add_argument("--long-tp-price", type=float)
    run.add_argument("--long-sl-price", type=float)
    run.add_argument("--short-tp-price", type=float)
    run.add_argument("--short-sl-price", type=float)
    for flag in ("entry", "tp", "sl"):
        run.add_argument(
            f"--{flag}-order",
            choices=("market", "limit"),
            default="market",
        )

    for name in ("scalp", "oco"):
        subparsers.add_parser(name, help="本版請改用 run --strategy 啟動策略")

    panic = subparsers.add_parser(
        "panic", help="平掉所有部位並停機", description="刪除委託、平掉所有部位並停機"
    )
    panic.add_argument("--yes", action="store_true", help="跳過人工確認")
    flatten = subparsers.add_parser(
        "flatten", help="平掉所有部位後繼續待命", description="刪除委託並平掉所有部位後待命"
    )
    flatten.add_argument("--yes", action="store_true", help="跳過人工確認")
    subparsers.add_parser("status", help="顯示引擎健康狀態")
    subparsers.add_parser("demo", help="免帳號重播離線行情範例")
    watch = subparsers.add_parser("watch", help="啟動獨立唯讀監看介面")
    watch.add_argument("--interval", type=float, default=0.25, help="刷新間隔秒數")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析並執行指令，將未預期錯誤轉為 sysexits 70。"""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return _dispatch(args, parser)
    except (ValidationError, ValueError) as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    except MicroTXError as exc:
        print(f"執行失敗：{exc}", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:
        print(f"內部錯誤：{exc}", file=sys.stderr)
        return EXIT_INTERNAL


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    command = str(args.command)
    if command == "demo":
        return _demo()
    if command in {"scalp", "oco"}:
        print("本版請使用 microtx run --strategy 啟動策略。", file=sys.stderr)
        return EXIT_USER_ERROR
    settings = Settings()
    if command == "watch":
        from microtx.tui.dashboard import watch

        watch(settings, interval=float(args.interval))
        return EXIT_OK
    if command == "run":
        return _run(args, settings)
    if command == "panic":
        return _signal_engine(settings, signal.SIGUSR1, "PANIC", bool(args.yes))
    if command == "flatten":
        return _signal_engine(settings, signal.SIGUSR2, "FLATTEN", bool(args.yes))
    if command == "status":
        return _status(settings)
    parser.error("未知指令")
    return EXIT_USER_ERROR


def _run(args: argparse.Namespace, settings: Settings) -> int:
    print(settings.summary())
    if settings.is_live and not _confirm_live(bool(args.yes)):
        print("未取得實盤確認，拒絕啟動。", file=sys.stderr)
        return EXIT_USER_ERROR
    strategy = _strategy_from_args(args, settings)
    if strategy is not None and getattr(args, "sl_order", "market") == "limit":
        print(
            f"WARNING: ⚠️ 停損採限價委託（{strategy.describe() if strategy else ''}）。"
            "快市穿價時可能不成交，部位將持續裸露；"
            "建議改用 --sl-order market（範圍市價，滑價有上限）。",
            file=sys.stderr,
        )
    from microtx.broker.shioaji_gateway import ShioajiGateway

    if bool(args.reset_daily_state):
        from microtx.engine.daily_state import DailyStateStore

        DailyStateStore(settings.daily_state_file, boundary=settings.trading_day_boundary).clear()

    engine = TradingEngine(settings, ShioajiGateway(settings), notifier=None)
    if strategy is not None:
        strategy.arm()
        engine.add_strategy(strategy)
    engine.run_forever()
    return EXIT_OK


def _confirm_live(yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        return os.getenv("MICROTX_CONFIRM_LIVE") == "YES"
    print("警告：即將啟動實盤交易。請輸入 YES 二次確認：", end="", flush=True)
    return input().strip() == "YES"


def _strategy_from_args(
    args: argparse.Namespace, settings: Settings
) -> ScalpStrategy | OcoStrategy | None:
    if args.strategy is None:
        return None
    entry_style = ExecutionStyle(str(args.entry_order).upper())
    take_profit_style = ExecutionStyle(str(args.tp_order).upper())
    stop_loss_style = ExecutionStyle(str(args.sl_order).upper())
    if args.strategy == "scalp":
        if args.direction is None or args.trigger is None:
            raise ValueError("scalp 必須提供 --direction 與 --trigger")
        if args.tp is not None and args.tp_price is not None:
            raise ValueError("--tp 與 --tp-price 只能擇一")
        if args.sl is not None and args.sl_price is not None:
            raise ValueError("--sl 與 --sl-price 只能擇一")
        return ScalpStrategy(
            spec=settings.spec,
            direction=Direction.LONG if args.direction == "long" else Direction.SHORT,
            trigger_price=args.trigger,
            take_profit_points=args.tp,
            stop_loss_points=args.sl,
            take_profit_price=args.tp_price,
            stop_loss_price=args.sl_price,
            quantity=settings.order_quantity,
            entry_style=entry_style,
            take_profit_style=take_profit_style,
            stop_loss_style=stop_loss_style,
        )
    if args.upper is None or args.lower is None:
        raise ValueError("oco 必須提供 --upper 與 --lower")
    return OcoStrategy(
        spec=settings.spec,
        upper_trigger=args.upper,
        lower_trigger=args.lower,
        take_profit_points=args.tp,
        stop_loss_points=args.sl,
        long_take_profit_price=args.long_tp_price,
        long_stop_loss_price=args.long_sl_price,
        short_take_profit_price=args.short_tp_price,
        short_stop_loss_price=args.short_sl_price,
        quantity=settings.order_quantity,
        entry_style=entry_style,
        take_profit_style=take_profit_style,
        stop_loss_style=stop_loss_style,
    )


def _signal_engine(settings: Settings, signum: int, label: str, yes: bool) -> int:
    pid = PidFile.read_pid(settings.pid_file)
    if pid is None:
        print("引擎未運行（PID 檔不存在或行程已結束）", file=sys.stderr)
        print("若確認仍有部位，請立即至永豐下單軟體手動平倉。", file=sys.stderr)
        return EXIT_NOT_RUNNING
    if not yes:
        if not sys.stdin.isatty():
            print("非互動環境必須加上 --yes 才能執行危險操作。", file=sys.stderr)
            return EXIT_USER_ERROR
        prompt = "這會平掉所有部位並停機" if label == "PANIC" else "這會平掉所有部位"
        if input(f"{prompt}，確定執行？[y/N] ").strip().lower() != "y":
            print("已取消。", file=sys.stderr)
            return EXIT_USER_ERROR
    os.kill(pid, signum)
    print(f"已送出 {label} 訊號至 PID {pid}，請查看引擎日誌確認結果")
    return EXIT_OK


def _status(settings: Settings) -> int:
    pid = PidFile.read_pid(settings.pid_file)
    if pid is None or not settings.status_file.exists():
        print("引擎未運行", file=sys.stderr)
        return EXIT_NOT_RUNNING
    try:
        payload: dict[str, Any] = json.loads(settings.status_file.read_text(encoding="utf-8"))
        written_at = datetime.fromisoformat(str(payload["written_at"]))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"狀態快照無法讀取：{exc}", file=sys.stderr)
        return EXIT_STALE
    age = (datetime.now(written_at.tzinfo) - written_at).total_seconds()
    if age > settings.status_write_interval_sec * 3:
        print(f"⚠️ 引擎無回應：快照已過期 {age:.1f} 秒", file=sys.stderr)
        return EXIT_STALE
    if payload.get("degraded") is True:
        print("⚠️ 引擎卡在共用鎖上，建議立即執行 microtx panic", file=sys.stderr)
        return EXIT_DEGRADED
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def _demo() -> int:
    settings = Settings(_env_file=None)
    gateway = PaperGateway(spec=settings.spec, initial_price=23_000.0)
    strategy = ScalpStrategy(
        spec=settings.spec,
        direction=Direction.LONG,
        trigger_price=23_001.0,
        take_profit_points=7,
        stop_loss_points=2,
    )
    strategy.arm()
    gateway.connect()

    def submit(signals: list[Signal]) -> None:
        for item in signals:
            gateway.place_order(
                OrderRequest(
                    settings.symbol,
                    item.action,
                    item.quantity,
                    item.limit_price,
                    PriceType.LMT if item.limit_price is not None else PriceType.MKP,
                    TimeInForce.ROD if item.limit_price is not None else TimeInForce.IOC,
                    item.intent,
                    new_client_id(),
                    "demo",
                )
            )

    def on_order(event: object) -> None:
        if isinstance(event, FillEvent):
            submit(strategy.on_fill(event))

    gateway.set_order_event_callback(on_order)
    gateway.subscribe_ticks(
        settings.symbol,
        lambda raw: (
            submit(strategy.on_tick(TickEvent.from_raw(raw, symbol=settings.symbol)))
            if not raw.simtrade
            else None
        ),
    )
    count = 0
    with (PROJECT_ROOT / "tests/fixtures/sample_ticks.csv").open(encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            raw = RawTick(
                str(row["code"]),
                datetime.fromisoformat(str(row["timestamp"])),
                float(row["price"]),
                int(row["volume"]),
                int(row["total_volume"]),
                int(row["tick_type"]),
                str(row["simtrade"]).lower() == "true",
            )
            gateway.replay((raw,))
            count += 1
    gateway.disconnect()
    print("MicroTX 離線 Demo 完成")
    print(f"重播 {count} 筆行情｜策略狀態 {strategy.state.value}｜最終部位 0 口")
    print(f"已實現損益 {gateway.realized_pnl:,.0f} NTD")
    return EXIT_OK
