#!/usr/bin/env python3
"""lmstop — Keep LM Studio from running out of unified GPU/system memory.

Polls ``lms ps --json`` and the OS's real memory numbers, and (with --auto-eject)
unloads the least-recently-used idle models whenever system memory in use exceeds
the usable budget. Model sizes from lms are weights only, so they are used just to
estimate what an eviction will free and to pre-empt a newly appearing load.

    lmstop                          live monitor (default)
    lmstop --once                   one snapshot, then exit
    lmstop --bare                   scrolling output, like ``top -b``
    lmstop --daemon                 headless: log decisions only
    lmstop --daemon --verbose       headless, but still show the status table
    lmstop --auto-eject             any of the above, and actually unload models
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple, Optional

GIB = 1024 ** 3
LOG_FILE = "lmstop.log"
UNLOAD_TIMEOUT = 60   # seconds to wait for an unload to show up in `lms ps` before retrying
LOAD_WINDOW = 120     # seconds a new model is assumed to still be loading
USAGE_LABEL = "committed" if sys.platform == "win32" else "in use"
CYAN, GREEN, YELLOW, RED = "\033[96m", "\033[92m", "\033[93m", "\033[91m"
BOLD, RESET = "\033[1m", "\033[0m"

logger = logging.getLogger("lmstop")


# ---------------------------------------------------------------------------
# System memory
# ---------------------------------------------------------------------------

class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class Memory(NamedTuple):
    total: int       # physical RAM
    available: int   # free + reclaimable RAM
    committed: int   # commit charge: memory promised to processes (Windows only; 0 elsewhere)

    @property
    def in_use(self) -> int:
        return self.total - self.available

    @property
    def demand(self) -> int:
        """Worst case resident memory: everything committed could be touched, so never
        report less than the commit charge. Memory that's merely committed isn't swapped
        yet, but it will be if it gets touched and RAM is short."""
        return max(self.in_use, self.committed)


def memory_status() -> Memory:
    """Physical RAM and commit charge, in bytes.

    On unified-memory APUs (Strix Halo) RAM is also the GPU pool. Unlike lms model
    sizes (weights only) these include KV caches, compute buffers and every other process.
    """
    if sys.platform == "win32":
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        # "TotalPageFile" is the commit limit (RAM + pagefile); limit - avail = Task Manager's "Committed"
        return Memory(status.ullTotalPhys, status.ullAvailPhys,
                      status.ullTotalPageFile - status.ullAvailPageFile)
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    values[key] = int(rest.split()[0]) * 1024
    except OSError:
        pass
    return Memory(values.get("MemTotal", 0), values.get("MemAvailable", 0), 0)


def gpu_name() -> str:
    """Display name of the first video adapter (cosmetic only)."""
    if sys.platform == "win32":
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_VideoController | Select-Object -First 1).Name"],
                capture_output=True, text=True, timeout=15,
            )
            if res.stdout.strip():
                return res.stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug("GPU name lookup failed: %s", e)
    return "Unified Memory"


def usable_bytes(total: int, reserve_bytes: int, reserve_fraction: float) -> int:
    """Memory budget: total minus a fractional reserve minus an absolute reserve."""
    return max(int(total * (1 - reserve_fraction)) - reserve_bytes, 0)


# ---------------------------------------------------------------------------
# LM Studio
# ---------------------------------------------------------------------------

@dataclass
class Model:
    identifier: str
    status: str                                  # IDLE, GENERATING, ...
    size_bytes: int                              # as reported by lms
    footprint_bytes: int                         # size * footprint multiplier
    state_changed_at: Optional[datetime] = None  # None => age unknown (N/A)
    unloading: bool = False                      # unload requested, still in ps
    never_used: bool = False                     # loaded but not yet prompted: its KV cache and
                                                 # compute buffers are allocated on first use

    @property
    def size_str(self) -> str:
        return f"{self.size_bytes / GIB:.1f} GB"


def find_lms_path() -> str:
    exe = shutil.which("lms.exe") or shutil.which("lms")
    if exe:
        return exe
    for candidate in (r"C:\Program Files\LMStudio\bin\lms.exe",
                      r"C:\Users\Public\LMStudio\bin\lms.exe"):
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("Cannot find lms.exe. Install LM Studio or pass --lms-path.")


def run_lms(lms_path: str, *args: str) -> str:
    """Run an lms subcommand and return stdout; raise on failure."""
    res = subprocess.run([lms_path, *args], capture_output=True, text=True, timeout=15)
    if res.returncode != 0:
        raise RuntimeError(f"lms {' '.join(args)} failed: {res.stderr.strip()}")
    logger.debug("lms %s => %r", " ".join(args), res.stdout[:200])
    return res.stdout


def list_models(lms_path: str, footprint_multiplier: float) -> list[Model]:
    models = []
    for item in json.loads(run_lms(lms_path, "ps", "--json")):
        size = int(item.get("sizeBytes") or 0)
        last_used_ms = item.get("lastUsedTime")
        status = (item.get("status") or "UNKNOWN").upper()
        item_never_used = status == "IDLE" and not (last_used_ms and last_used_ms > 0)
        models.append(Model(
            identifier=item["identifier"],
            status=status,
            size_bytes=size,
            footprint_bytes=int(size * footprint_multiplier),
            never_used=item_never_used,
            state_changed_at=(datetime.fromtimestamp(last_used_ms / 1000)
                              if last_used_ms and last_used_ms > 0 else None),
        ))
    return models


def track_state_changes(models: list[Model], previous: Optional[list[Model]]) -> None:
    """Carry ``state_changed_at`` across polls (``previous`` is None on the first poll).

    New models and models whose status changed are stamped "now"; unchanged models
    keep the newer of their previous stamp and what lms reports.
    """
    if previous is None:
        return
    now = datetime.now()
    before = {m.identifier: m for m in previous}
    for m in models:
        old = before.get(m.identifier)
        if old is None or old.status != m.status:
            m.state_changed_at = now
        else:
            stamps = [t for t in (old.state_changed_at, m.state_changed_at) if t]
            m.state_changed_at = max(stamps, default=None)


# ---------------------------------------------------------------------------
# Eviction policy
# ---------------------------------------------------------------------------

def pick_victims(models: list[Model], over_bytes: int, min_idle_seconds: int) -> list[Model]:
    """Least-recently-used idle models that together free at least ``over_bytes``.

    Never touches busy models, models already unloading, or models whose idle age
    is unknown or shorter than ``min_idle_seconds`` (which also shields a model
    that is still loading). Returns [] if the overage can't be fully covered.
    """
    cutoff = datetime.now() - timedelta(seconds=min_idle_seconds)
    evictable = sorted(
        (m for m in models
         if m.status == "IDLE" and not m.unloading
         and m.state_changed_at and m.state_changed_at <= cutoff),
        key=lambda m: (m.state_changed_at, m.identifier),
    )
    victims: list[Model] = []
    freed = 0
    for m in evictable:
        if freed >= over_bytes:
            break
        victims.append(m)
        freed += m.footprint_bytes
    return victims if freed >= over_bytes else []


def decide(models: list[Model], used: int, usable: int, auto_eject: bool,
           min_idle_seconds: int, projected: bool = False) -> tuple[list[Model], str, bool]:
    """Return (models to unload now, human-readable decision line, within_budget).

    ``used`` is measured system memory in use, or the projection when ``projected``
    (see monitor()).
    """
    over = used - usable
    summary = (f"{'projected' if projected else USAGE_LABEL}: {used / GIB:.1f} / "
               f"{usable / GIB:.1f} GB   headroom: {-over / GIB:+.1f} GB")
    if over <= 0:
        return [], f"{summary} -- OK", True

    pending = [m.identifier for m in models if m.unloading]
    if pending:  # one eviction wave at a time: let the OS numbers catch up first
        return [], f"{summary} -- OVER, waiting for unload of {', '.join(pending)}", False

    victims = pick_victims(models, over, min_idle_seconds)
    if not victims:
        return [], f"{summary} -- OVER, no idle model can be ejected", False
    names = ", ".join(v.identifier for v in victims)
    if auto_eject:
        return victims, f"{summary} -- OVER, ejecting {names}", False
    return [], f"{summary} -- OVER, would eject {names} (add --auto-eject)", False


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def format_age(since: Optional[datetime]) -> str:
    if since is None:
        return "N/A"
    secs = max(0, int((datetime.now() - since).total_seconds()))
    if secs < 60:
        return f"{secs:02d}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins:02d}m {secs:02d}s"
    return f"{mins // 60}h {mins % 60:02d}m"


def render(models: list[Model], used: int, in_use: int, usable: int, gpu: str, decision: str, *,
           heading: Optional[str], trailer: bool, redraw: bool, first_pass: bool) -> None:
    # Defensive: if stdout is somehow absent (e.g. a host that detaches it),
    # write to the null device rather than crash.
    stream = sys.stdout or open(os.devnull, "w")
    isatty = getattr(stream, "isatty", lambda: False)()
    # Box-drawing glyphs need a UTF-8-capable stream: a cp1252 Windows console
    # (or Linux unterminated encoding) cannot encode "█" and would crash on
    # write. Use them only on an interactive stream that handles UTF-8.
    encoding = (getattr(stream, "encoding", "") or "").lower()
    utf8_capable = "utf" in encoding or "utf-8" in encoding or "utf_8" in encoding
    weights = sum(m.size_bytes for m in models)
    pct = min(100.0, used / usable * 100) if usable else 0.0
    color = GREEN if pct < 60 else YELLOW if pct < 85 else RED
    filled = int(32 * pct / 100)
    full, empty = ("█", "░") if (isatty and utf8_capable) else ("#", "-")
    bar = full * filled + empty * (32 - filled)

    rule = "  " + "-" * 80
    lines = []
    if heading:
        lines.append(f"{CYAN}{BOLD}lmstop{RESET}   {heading}")
    lines += [
        f"GPU 0  {gpu}",
        f" MEM    [{color}{bar}{RESET}] {pct:5.1f} %   {used / GIB:.1f} / {usable / GIB:.1f} GB"
        f"   (in use {in_use / GIB:.1f}, weights {weights / GIB:.1f})",
        "",
        f"  {BOLD}{'MODEL':<40} {'AGE':<8} {'SIZE':<10} {'STATE':<12}{RESET}",
        rule,
    ]
    for m in models:
        state = "UNLOADING" if m.unloading else m.status
        state_color = RED if m.unloading else GREEN if m.status == "IDLE" else YELLOW
        lines.append(f"  {m.identifier:<40} {format_age(m.state_changed_at):<8} "
                     f"{m.size_str:<10} {state_color}{state:<12}{RESET}")
    if not models:
        lines.append("  (No models currently loaded)")
    lines += [rule, "", f"Auto-Eject Decision:  {decision}"]
    if trailer:
        lines += ["", "  Press Ctrl+C to exit."]
    lines.append("")

    text = "\n".join(lines) + "\n"
    if redraw:  # repaint in place: home cursor, erase stale line tails and leftover rows
        text = (("\033[2J\033[H" if first_pass else "\033[H")
                + text.replace("\n", "\033[K\n") + "\033[J")
    stream.write(text)
    stream.flush()


# ---------------------------------------------------------------------------
# Main loop (the only loop: --once, --daemon, --bare are just gates on it)
# ---------------------------------------------------------------------------

def monitor(args: argparse.Namespace) -> int:
    lms_path = args.lms_path or find_lms_path()
    usable = usable_bytes(memory_status().total, args.safety_absolute, args.safety_percent)
    gpu = gpu_name()

    show_status = args.verbose or not args.daemon       # table + decision line
    live = not args.once                                # heading timestamp + Ctrl+C hint
    redraw = show_status and live and not args.bare     # repaint in place vs. scroll

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    logger.info("started: lms=%s usable=%.1fGB poll=%ds auto_eject=%s",
                lms_path, usable / GIB, args.poll_interval, args.auto_eject)

    previous: Optional[list[Model]] = None
    last_measured: Optional[int] = None
    expected_peak = 0             # where memory will land once models still loading finish
    peak_expires = 0.0
    unloading: dict[str, float] = {}   # id -> when unload was requested (lms lists it for a while)
    last_logged = ""
    first_pass = True

    while not stop.is_set():
        try:
            models = list_models(lms_path, args.footprint_multiplier)
        except Exception as e:
            logger.warning("lms ps failed: %s", e)
            if args.once:
                return 1
            stop.wait(args.poll_interval)
            continue

        track_state_changes(models, previous)
        now = time.monotonic()
        listed = {m.identifier for m in models}
        unloading = {i: t for i, t in unloading.items()
                     if i in listed and now - t < UNLOAD_TIMEOUT}
        for m in models:
            m.unloading = m.identifier in unloading

        mem = memory_status()
        measured = mem.demand

        # Projection, so we can evict *before* memory runs out rather than after:
        #  1. A model seen for the first time is still loading: expect usage to reach
        #     (usage before it appeared) + its weights, until measured catches up.
        seen = {m.identifier for m in previous} if previous is not None else listed
        new = [m for m in models if m.identifier not in seen]
        if new:
            base = max(min(last_measured if last_measured is not None else measured, measured),
                       expected_peak)
            expected_peak = base + sum(m.size_bytes for m in new)
            peak_expires = now + LOAD_WINDOW
        if measured >= expected_peak or now > peak_expires:
            expected_peak = 0
        #  2. A model nobody has prompted yet will allocate KV cache / compute buffers
        #     on first use; reserve that now (footprint - weights) instead of being
        #     surprised later.
        first_use = sum(max(0, m.footprint_bytes - m.size_bytes)
                        for m in models if m.never_used and not m.unloading)
        used = max(measured, expected_peak) + first_use

        victims, decision, within_budget = decide(
            models, used, usable, args.auto_eject, args.active_window,
            projected=used > measured)

        action = decision.split(" -- ", 1)[1]
        if within_budget:
            last_logged = ""
        elif action != last_logged:        # don't repeat the same warning every poll
            logger.warning("%s", decision)
            last_logged = action

        for v in victims:
            try:
                run_lms(lms_path, "unload", v.identifier)
            except Exception as e:
                logger.error("unload %s failed: %s", v.identifier, e)
            else:
                logger.info("unload requested: %s", v.identifier)
                unloading[v.identifier] = now
                v.unloading = True
                expected_peak = max(0, expected_peak - v.footprint_bytes)

        if show_status:
            render(
                models, measured, mem.in_use, usable, gpu, decision,
                heading=f"{time.strftime('%Y-%m-%d %H:%M:%S')}   refresh {args.poll_interval}s" if live else None,
                trailer=live and (redraw or first_pass),
                redraw=redraw, first_pass=first_pass,
            )

        previous = models
        last_measured = measured
        first_pass = False
        if args.once:
            break
        stop.wait(args.poll_interval)

    logger.info("stopped")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lmstop",
        description="Keep LM Studio from running out of unified GPU/system memory "
                    "by ejecting idle models.",
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="one pass, then exit (no heading or Ctrl+C hint)")
    mode.add_argument("--daemon", action="store_true",
                      help="headless: no status table unless --verbose")
    p.add_argument("--bare", action="store_true",
                   help="scroll instead of repainting in place (like top -b)")
    p.add_argument("--verbose", action="store_true", help="debug logging; shows status in --daemon")
    p.add_argument("--auto-eject", action="store_true",
                   help="actually unload idle models when over budget")
    p.add_argument("--poll-interval", type=int, default=5,
                   help="seconds between polls (default: 5)")
    p.add_argument("--lms-path", help="path to lms.exe (default: search PATH)")
    p.add_argument("--safety-absolute", type=int, default=4 * GIB,
                   help="bytes to reserve (default: 4 GiB)")
    p.add_argument("--safety-percent", type=float, default=0.05,
                   help="fraction of total memory to reserve (default: 0.05)")
    p.add_argument("--active-window", type=int, default=30,
                   help="an idle model is only evictable once its state is at least "
                        "this many seconds old (default: 30)")
    p.add_argument("--footprint-multiplier", type=float, default=1.2,
                   help="real memory per model as a multiple of its reported size once "
                        "prompted (KV cache etc.); measured ~1.18 on Strix Halo "
                        "(default: 1.2)")
    return p


def setup_logging(verbose: bool, console: bool) -> None:
    """Always log to LOG_FILE; also to stderr unless a live repaint would be clobbered."""
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False


def _silence_broken_pipe() -> None:
    """Redirect stdout/stderr to the null device.

    Called when a downstream pipe (e.g. ``lmstop | less`` where ``less`` was
    quit early) has closed: without this, Python re-tries the final flush at
    interpreter shutdown, raises again, and prints a spurious traceback.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        for name in ("stdout", "stderr"):
            fd = getattr(sys, name).fileno()
            os.dup2(devnull, fd)
    except Exception:
        pass  # nothing sane to fall back to; just avoid crashing


def main() -> int:
    args = build_parser().parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")   # never die on an odd console encoding
    show_status = args.verbose or not args.daemon
    redrawing = show_status and not args.once and not args.bare
    setup_logging(args.verbose, console=not redrawing)
    try:
        return monitor(args)
    except BrokenPipeError:
        # Downstream closed the pipe (e.g. `lmstop | less`, then `less` quit).
        _silence_broken_pipe()
        logger.info("stdout pipe closed; exiting")
        return 0


if __name__ == "__main__":
    sys.exit(main())
