#!/usr/bin/env python3
"""Flash-status helpers for the PX4 console broker (see console.py / SKILL.md).

Pure and importable: no tkinter, no broker. Parses `make <target> upload`
output into short status strings and runs the flash subprocess.
"""

import os
import re
import subprocess

_PCT = re.compile(r"^(Erase|Program|Verify)\s*:.*?(\d+)(?:\.\d+)?%")
_VERB = {"Erase": "erasing", "Program": "programming", "Verify": "verifying"}
_BOARD = re.compile(r"Found board id[: ]+(\w+)", re.IGNORECASE)


def parse_flash_line(line):
    """Map one raw uploader output token to a short status, or None."""
    line = line.strip()
    if not line:
        return None
    m = _PCT.match(line)
    if m:
        return "%s %s%%" % (_VERB[m.group(1)], m.group(2))
    low = line.lower()
    if "waiting for bootloader" in low:
        return "waiting for bootloader"
    m = _BOARD.search(line)
    if m:
        return "found board %s" % m.group(1)
    if low.startswith("loaded firmware"):
        return "loaded firmware"
    if low.startswith("rebooting"):
        return "rebooting"
    if low.startswith("error") or "not responding" in low or "no such file" in low:
        return "error: " + line
    return None


_TARGET_RE = re.compile(r"[A-Za-z0-9_.\-]+")


def valid_target(target):
    """True if `target` is a safe make goal: no leading dash (flag smuggling),
    no shell/whitespace metacharacters. PX4 board targets are all word chars."""
    return (bool(target) and not target.startswith("-")
            and _TARGET_RE.fullmatch(target) is not None)


def build_flash_cmd(target, repo_root):
    if not valid_target(target):
        raise ValueError("invalid flash target: %r" % (target,))
    # `--` stops make from treating a hostile target as an option (argv flag
    # smuggling, e.g. `--eval=$(shell ...)` which runs commands at parse time);
    # valid_target already rejects leading dashes as belt-and-suspenders.
    return ["make", "-C", repo_root, "--", target, "upload"]


def run_flash(cmd, on_status, on_done, cwd=None, env=None):
    """Run `cmd`, streaming parsed statuses to on_status, then on_done once."""
    e = dict(os.environ)
    e["PYTHONUNBUFFERED"] = "1"          # force px_uploader progress to flush
    if env:
        e.update(env)
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=e, bufsize=0,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as ex:
        on_done("error: %s" % ex)
        return
    buf = ""
    try:
        while True:
            chunk = p.stdout.read(256)   # raw stream (bufsize=0): returns what's available
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            parts = re.split(r"[\r\n]", buf)
            buf = parts.pop()            # keep the incomplete tail
            for part in parts:
                st = parse_flash_line(part)
                if st:
                    on_status(st)
    finally:
        p.wait()
        tail = parse_flash_line(buf)
        if tail:
            on_status(tail)
        on_done("done" if p.returncode == 0 else "error: exit %d" % p.returncode)
