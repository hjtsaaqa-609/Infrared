#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from stm32_position_target_capture import main as target_main  # noqa: E402


def main() -> int:
    argv = list(sys.argv[1:])
    if "--capture-mode" not in argv:
        argv = ["--capture-mode", "stream", *argv]
    return target_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
