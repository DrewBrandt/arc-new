#!/usr/bin/env python3
"""Plain-English helper for building ARC setup.sh commands."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path


DEFAULT_CONTROLLER_HOST = "arcpi1.local"
DEFAULT_SENDERS = [
    {"addr": "0x12", "name": "airbrake", "host": "arcpi2.local", "paired_fc": "0x03"},
    {"addr": "0x13", "name": "payload", "host": "arcpi3.local", "paired_fc": "0x04"},
    {"addr": "0x15", "name": "ground", "host": "arcpi5.local", "paired_fc": "none"},
]

SENDER_NAMES_BY_ADDR = {
    "0x11": "down",
    "17": "down",
    "0x12": "airbrake",
    "18": "airbrake",
    "0x13": "payload",
    "19": "payload",
    "0x15": "ground",
    "21": "ground",
}

SENDER_FC_BY_ADDR = {
    "0x11": "0x02",
    "17": "0x02",
    "0x12": "0x03",
    "18": "0x03",
    "0x13": "0x04",
    "19": "0x04",
}


def ask(prompt: str, default: str | None = None) -> str:
    if default is None:
        suffix = ": "
    elif default == "":
        suffix = " [press Enter to skip]: "
    else:
        suffix = f" [{default}]: "

    answer = input(prompt + suffix).strip()
    if answer:
        return answer
    return "" if default is None else default


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        answer = input(prompt + suffix).strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def choose_role() -> str:
    while True:
        answer = ask("What are you setting up, the controller or a sender?", "sender").lower()
        if answer in {"controller", "c"}:
            return "controller"
        if answer in {"sender", "s"}:
            return "sender"
        print('Please answer "controller" or "sender".')


def sender_name_for_addr(addr: str) -> str:
    return SENDER_NAMES_BY_ADDR.get(addr.lower(), f"sender-{addr}" if addr else "")


def default_paired_fc_for_addr(addr: str) -> str:
    return SENDER_FC_BY_ADDR.get(addr.lower(), "")


def sender_entry_to_arg(sender: dict[str, str]) -> str:
    paired_fc = sender["paired_fc"]
    if paired_fc.lower() == "none":
        paired_fc = ""
    return f'{sender["addr"]}:{sender["name"]}:{sender["host"]}:{paired_fc}'


def ask_controller_args() -> list[str]:
    args: list[str] = []

    default_list = ",".join(sender_entry_to_arg(sender) for sender in DEFAULT_SENDERS)
    print("\nController sender list:")
    print(f"  {default_list}")
    if not ask_yes_no("Use that default list of sender Pis?", True):
        if ask_yes_no("Do you want to paste the raw sender list instead?", False):
            sender_list = ask(
                "Paste addr:name:host:paired_fc entries separated by commas",
                default_list,
            )
        else:
            sender_list = build_sender_list()
        args.extend(["--senders", sender_list])

    if ask_yes_no("Rewrite /etc/arc/controller.toml if it already exists?", False):
        args.append("--force-config")

    return args


def build_sender_list() -> str:
    while True:
        count_text = ask("How many sender Pis should the controller know about?", str(len(DEFAULT_SENDERS)))
        try:
            count = int(count_text)
        except ValueError:
            print("Please enter a whole number.")
            continue
        if count >= 0:
            break
        print("Please enter zero or more.")

    senders: list[dict[str, str]] = []
    for index in range(count):
        default = DEFAULT_SENDERS[index] if index < len(DEFAULT_SENDERS) else {}
        print(f"\nSender {index + 1}:")
        addr = ask("  What ARC address does this sender use?", default.get("addr", ""))
        name_default = default.get("name") or sender_name_for_addr(addr)
        name = ask("  What plain name should the controller use for it?", name_default)
        host = ask("  What hostname or IP should the controller connect to?", default.get("host", ""))
        paired_default = default.get("paired_fc") or default_paired_fc_for_addr(addr) or "none"
        paired_fc = ask("  What FC address is paired with it? Type none for video-only", paired_default)
        senders.append({"addr": addr, "name": name, "host": host, "paired_fc": paired_fc})

    return ",".join(sender_entry_to_arg(sender) for sender in senders)


def ask_sender_args() -> list[str]:
    args: list[str] = []

    print("\nSender identity:")
    addr = ask("What ARC address should this sender use?", "")
    if addr:
        args.extend(["--addr", addr])

    name_default = sender_name_for_addr(addr) if addr else ""
    name = ask("What plain name should this sender use?", name_default)
    if name:
        args.extend(["--name", name])

    paired_default = default_paired_fc_for_addr(addr) if addr else ""
    paired_fc = ask("What FC address is wired to it? Type none for video-only", paired_default)
    if paired_fc:
        args.extend(["--paired-fc", paired_fc])

    controller_host = ask("What hostname should this sender use for the controller?", DEFAULT_CONTROLLER_HOST)
    if controller_host != DEFAULT_CONTROLLER_HOST:
        args.extend(["--controller-host", controller_host])

    if ask_yes_no("Rewrite /etc/arc/sender.toml if it already exists?", False):
        args.append("--force-config")

    return args


def build_command(setup_script: Path, setup_args: list[str]) -> list[str]:
    if os.name == "nt":
        return ["bash", setup_script.as_posix(), *setup_args]

    use_sudo = hasattr(os, "geteuid") and os.geteuid() != 0
    if use_sudo:
        return ["sudo", "bash", str(setup_script), *setup_args]
    return ["bash", str(setup_script), *setup_args]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ask plain-English questions and run the ARC setup script."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ask the questions and print the setup command without running it",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    setup_script = repo_root / "setup.sh"
    if not setup_script.exists():
        print(f"Could not find setup.sh next to {Path(__file__).name}.", file=sys.stderr)
        return 1

    print("ARC setup helper")
    print("Answer the questions and I will build the setup.sh command for you.\n")

    role = choose_role()
    setup_args = [role]
    if role == "controller":
        setup_args.extend(ask_controller_args())
    else:
        setup_args.extend(ask_sender_args())

    command = build_command(setup_script, setup_args)
    print("\nCommand:")
    print(f"  {shlex.join(command)}")

    if args.dry_run:
        return 0

    if not ask_yes_no("Run this setup command now?", True):
        print("Okay, leaving it unrun.")
        return 0

    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
