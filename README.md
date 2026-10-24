# lmstop

**Keep LM Studio from running out of unified GPU/system memory by ejecting idle models.**

A small, standalone daemon that watches the OS's real memory numbers and automatically unloads least-recently-used idle models when memory in use exceeds the usable budget — before an OOM, not after.

## What it does

- Reads actual system memory from the OS: on Windows via `GlobalMemoryStatusEx` (RAM total/available plus the commit charge Task Manager shows as "Committed"), on Linux/macOS via `/proc/meminfo`. On unified-memory APUs like Strix Halo, RAM *is* the GPU pool.
- Polls ``lms ps --json`` to track which models are loaded and their reported (weights-only) sizes.
- Projects memory demand so it can evict **before** memory runs out: a model seen for the first time is assumed still loading (usage expected to reach pre-load usage + its weights), and a model nobody has prompted yet gets its KV-cache/compute-buffer overhead reserved up front.
- When over budget, unloads least-recently-used idle models — never an actively serving one, never one that's already unloading or too young to be sure it's idle.
- Provides a live terminal monitor and always logs decisions to `lmstop.log`.

## Key features

| Feature | Detail |
|---|---|
| **CLI-based LM Studio interaction** | Uses ``lms ps --json`` and ``lms unload`` via subprocess — no HTTP API, no log following |
| **OS-level memory accounting** | Budgets against real RAM/commit-charge numbers (which include KV caches, compute buffers, and every other process), not against summed model sizes |
| **Projection-based pre-emption** | Evicts ahead of a newly appearing load and reserves first-use overhead for freshly loaded models, instead of reacting after the OOM |
| **Footprint multiplier** | A prompted model consumes more than its reported weights (KV cache etc.). ``--footprint-multiplier`` estimates what an eviction frees; measured ~1.18 on Strix Halo (default: 1.2) |
| **LRU eviction policy** | Protects busy models and recent state changes (`--active-window`); picks victims by oldest ``state_changed_at``, and only acts when idle victims can fully cover the overage |
| **One wave at a time** | After requesting unloads, waits for the OS numbers to catch up before starting another eviction wave |
| **Live monitor** | Terminal display showing loaded models, memory bar (demand vs budget, with in-use/weights breakdown), UNLOADING states, and current decisions |

## Usage

```bash
# Live monitor (default mode) — repaints in place
lmstop [--poll-interval SECONDS] [--lms-path PATH] ...

# One-shot status snapshot
lmstop --once [--auto-eject] [--footprint-multiplier F] ...

# Daemon mode — log decisions only, no display
lmstop --daemon [--auto-eject] [--poll-interval SECONDS] ...

# Daemon but still show the status table
lmstop --daemon --verbose

# Scrolling output (like top -b) — appends each poll instead of overwriting
lmstop --bare [--poll-interval SECONDS] ...
```

## Installation

### From source:

1. Clone this repository:
   ```bash
   git clone https://github.com/shsmith/lmstop.git
   cd lmstop
   ```

2. Install dependencies:
   ```bash
   pip install -e .
   ```

3. Run the application:
   ```bash
   lmstop
   ```

### Windows standalone executable:

1. Install PyInstaller (if not already installed):
   ```bash
   pip install pyinstaller
   ```

2. Build the executable:
   ```bash
   make buildwin
   ```

3. Run the generated executable:
   ```bash
   dist/lmstop.exe
   ```

### Using Makefile:

The included Makefile provides convenient targets:

- `make run` - Run the Python script directly
- `make buildwin` - Build Windows standalone executable
- `make help` - Show all available targets

### Direct usage (recommended for personal use):

For **Linux/WSL**: Simply rename `lmstop.py` to `lmstop`, make it executable, and run:
```bash
cp lmstop.py lmstop
chmod +x lmstop
./lmstop  # or move to ~/bin/ and run from anywhere
```
The shebang (`#!/usr/bin/env python3`) ensures it runs correctly without installation.

For **Windows**: Create a `lmstop.bat` file in your PATH with:
```bat
@echo off
python3 -m lmstop %*
```
This avoids the complexity of packaging while working reliably from any terminal.

### Options (all flags apply to every mode)

| Flag | Description |
|---|---|
| `--once` | One pass, then exit (no heading or Ctrl+C hint) |
| `--daemon` | Headless: no status table unless `--verbose` |
| `--bare` | Scroll instead of repainting in place (like `top -b`) |
| `--poll-interval SECONDS` | Seconds between polls (default: 5) |
| `--lms-path PATH` | Path to ``lms`` / ``lms.exe`` (default: search PATH) |
| `--safety-absolute BYTES` | Bytes to reserve above the budget (default: 4 GiB) |
| `--safety-percent FRACTION` | Fraction of total memory to reserve (default: 0.05 = 5%) |
| `--active-window SECONDS` | An idle model is only evictable once its state is at least this many seconds old — also shields a model that is still loading (default: 30) |
| `--auto-eject` | Actually unload idle models when over budget (otherwise decisions are proposed only) |
| `--verbose` | Debug logging; shows the status table in `--daemon` mode |
| `--footprint-multiplier F` | Real memory per model as a multiple of its reported size once prompted, e.g. 1.2 treats a 75 GB model as ~90 GB (default: 1.2) |

### Example usage

```bash
# Live monitor in terminal (Ctrl+C to stop)
lmstop

# Scrolling output — good for piping or recording
lmstop --bare > lmstop.out 2>&1 &

# One-shot check, pipe into watch for periodic status
watch -n 5 "python3.exe -m lmstop --once"

# Headless daemon with auto-ejection and a longer poll interval
lmstop --daemon --auto-eject --poll-interval 10
```

Decisions are always appended to `lmstop.log` in the working directory; console logging is suppressed only while the live monitor repaints, so log lines never clobber the display.

## Example monitor output

```
lmstop   2026-10-23 15:42:21   refresh 5s
GPU 0  AMD Radeon(TM) 8060S Graphics
 MEM    [##########################------]  83.5 %   97.5 / 116.8 GB   (in use 91.0, weights 88.2)

  MODEL                                    AGE      SIZE       STATE
  --------------------------------------------------------------------------------
  qwen3.8-27b@q4_k_s                       20m 00s  20.3 GB    IDLE
  nvidia-nemotron-3.5-super-vl-120b@q4     12m 00s  44.8 GB    IDLE
  qwen3.6-35b-a3b@q4                       02m 00s  23.1 GB    GENERATING
  --------------------------------------------------------------------------------

Auto-Eject Decision:  committed: 120.0 / 116.8 GB   headroom: -3.2 GB -- OVER, would eject qwen3.8-27b@q4_k_s (add --auto-eject)

  Press Ctrl+C to exit.
```

The `MEM` bar tracks the worst-case demand the daemon budgets against — on Windows the commit charge when it exceeds RAM in use (`committed:` in the decision line), otherwise RAM in use (`in use:`). A model whose unload has been requested but which ``lms`` still lists shows as `UNLOADING`.

## Platform requirements

- **OS:** Windows 10/11 primary target (AMD Strix Halo is first-class); memory reading falls back to `/proc/meminfo` on Linux/macOS
- **Python:** 3.11+
- **LM Studio:** installed and accessible via ``lms`` / ``lms.exe`` on PATH

## Non-goals

lmstop is **not**:

- A general GPU monitor or process manager
- An LM Studio launcher, model downloader, or chat client
- A log viewer or replays logs
- A task-manager replacement or service manager
- A plugin architecture or event bus

It does one thing: keep LM Studio from OOM-ing on unified memory by ejecting idle models.
