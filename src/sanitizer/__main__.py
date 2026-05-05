"""Entry point so `python -m sanitizer ...` works."""

from sanitizer.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
