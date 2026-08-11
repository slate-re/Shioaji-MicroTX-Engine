"""``python -m microtx`` 與 ``microtx`` 的共同入口。"""

from __future__ import annotations

from microtx.cli.commands import main

if __name__ == "__main__":
    raise SystemExit(main())
