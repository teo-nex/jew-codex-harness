#!/usr/bin/env python3
"""Small argv-only RTK helper for common read/search/git inspection commands."""
import argparse
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    read = sub.add_parser("read"); read.add_argument("files", nargs="+")
    search = sub.add_parser("search"); search.add_argument("pattern"); search.add_argument("paths", nargs="+")
    sub.add_parser("status")
    sub.add_parser("diff")
    args = parser.parse_args()
    if args.action == "read":
        argv = ["rtk", "read", "--level", "minimal", "--max-lines", "400", "--", *args.files]
    elif args.action == "search":
        argv = ["rg", "-n", "--", args.pattern, *args.paths]
    else:
        argv = ["rtk", "git", args.action]
    return subprocess.run(argv, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
