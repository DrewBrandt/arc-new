"""Small bench CLI for Controller video commands.

This is intentionally separate from the flight protocol. It talks to the
Controller's localhost-only bench command socket, which lets us switch layouts
and sources before an FC is wired in.
"""

from __future__ import annotations

import argparse
import asyncio

from arc.controller_main import BENCH_CONTROL_HOST, BENCH_CONTROL_PORT


async def _send(command: str, host: str, port: int) -> str:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write((command.rstrip() + "\n").encode("utf-8"))
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    return response.decode("utf-8", errors="replace").rstrip()


def _build_command(args: argparse.Namespace) -> str:
    if args.command == "raw":
        return " ".join(args.words)
    if args.command == "status":
        return "status"
    if args.command == "layout":
        return f"layout {args.name}"
    if args.command == "source":
        return f"source {args.slot} {args.source}"
    if args.command == "cycle":
        return f"cycle {args.slot} {args.interval} {' '.join(args.sources)}"
    if args.command == "stop-cycle":
        return "stop-cycle"
    raise SystemExit(f"unknown command {args.command}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ARC Controller bench CLI")
    parser.add_argument("--host", default=BENCH_CONTROL_HOST)
    parser.add_argument("--port", type=int, default=BENCH_CONTROL_PORT)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show desired/active sources and online senders")

    layout = sub.add_parser("layout", help="Set layout by name or index")
    layout.add_argument("name")

    source = sub.add_parser("source", help="Set a compositor slot source")
    source.add_argument("slot", type=int)
    source.add_argument("source", help="empty, local, 0x12, sender-c, sender-l1, ...")

    cycle = sub.add_parser("cycle", help="Loop a slot across sources")
    cycle.add_argument("slot", type=int)
    cycle.add_argument("interval", type=float, help="Seconds between switches")
    cycle.add_argument("sources", nargs="+")

    sub.add_parser("stop-cycle", help="Stop the running source loop")

    raw = sub.add_parser("raw", help="Send a raw bench command line")
    raw.add_argument("words", nargs="+")

    args = parser.parse_args()
    response = asyncio.run(_send(_build_command(args), args.host, args.port))
    print(response)


if __name__ == "__main__":
    main()
