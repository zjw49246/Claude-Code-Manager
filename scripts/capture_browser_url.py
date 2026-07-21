#!/usr/bin/env python3
"""Capture Claude CLI's browser URL without launching a second Chrome."""
import os
import sys
from pathlib import Path


def main() -> int:
    target = os.environ.get("CCM_BROWSER_URL_FILE")
    if not target or len(sys.argv) < 2:
        return 2
    path = Path(target)
    path.write_text(sys.argv[-1], encoding="utf-8")
    path.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
