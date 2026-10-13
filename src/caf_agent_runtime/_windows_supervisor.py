"""Gated CLI launcher: start the target only after Job Object assignment."""

import os
import subprocess
import sys


def main() -> int:
    if os.name != "nt" or len(sys.argv) < 2 or os.read(0, 1) != b"\x02":
        return 125
    try:
        child = subprocess.Popen(sys.argv[1:], stdin=0, stdout=1, stderr=2)
        return child.wait()
    except OSError:
        # No exception text, command, environment or paths in output.
        os.write(2, b"CAF supervised launch failed\n")
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
