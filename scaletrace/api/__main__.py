"""Run the API locally:  python -m scaletrace.api [--host 127.0.0.1] [--port 8000]"""

from __future__ import annotations

import argparse


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="python -m scaletrace.api")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: local only).")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run("scaletrace.api.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
