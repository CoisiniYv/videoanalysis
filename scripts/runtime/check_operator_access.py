#!/usr/bin/env python3
"""Check an authenticated 8090 endpoint without exposing the password in argv."""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("auth_file")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    path = Path(args.auth_file)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"cannot read operator credential file: {exc}", file=sys.stderr)
        return 2
    if len(lines) != 2 or not lines[0] or not lines[1]:
        print("operator credential file has an invalid format", file=sys.stderr)
        return 2

    token = base64.b64encode(f"{lines[0]}:{lines[1]}".encode("utf-8")).decode("ascii")
    request = Request(args.url, headers={"Authorization": f"Basic {token}"})
    try:
        with urlopen(request, timeout=max(0.1, args.timeout)) as response:
            response.read(1)
            if 200 <= int(response.status) < 300:
                return 0
            print(f"operator endpoint returned HTTP {response.status}", file=sys.stderr)
            return 1
    except HTTPError as exc:
        print(f"operator endpoint returned HTTP {exc.code}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, OSError) as exc:
        print(f"operator endpoint unavailable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
