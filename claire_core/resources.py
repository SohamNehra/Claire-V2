"""Conservative host-memory budgets; no dependencies and no model downloads.

Available RAM on macOS is the kernel's own free-memory level, the figure the
`memory_pressure` tool reports: it counts purgeable, speculative and file-backed
pages the system hands back at once, which free + inactive pages leave out.
(With 26 GiB free + inactive and a 9B model loaded, a 20 GiB worker loaded,
left 11 GiB free + inactive and the system at normal pressure: the narrower
count had refused that load.) Free + inactive is the fallback when the kernel
level cannot be read. Model weights and KV caches vary, so admission budgets
are estimates, not an OOM guarantee. Unknown telemetry never increases
concurrency.
"""
import os
import re
import subprocess
import sys
import threading
import time

from .host import windows_memory

GIB = 1024 ** 3
_cache = (0, None)
_lock = threading.Lock()


def _kernel_free_percent():
    """macOS's system-wide free-memory percentage, or None if unreadable."""
    try:
        level = int(subprocess.check_output(["sysctl", "-n", "kern.memorystatus_level"],
                                            text=True, timeout=2))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return level if 0 <= level <= 100 else None


def memory_snapshot():
    global _cache
    with _lock:
        if time.monotonic() - _cache[0] < 3 and _cache[1] is not None:
            return dict(_cache[1])
        result = {"total_bytes": None, "available_bytes": None, "estimated": True}
        try:
            if sys.platform == "darwin":
                total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=2))
                raw = subprocess.check_output(["vm_stat"], text=True, timeout=2)
                page = int(re.search(r"page size of (\d+) bytes", raw)[1])
                fields = {k.strip(): int(v) for k, v in re.findall(r"([^\n:]+):\s*(\d+)\.", raw)}
                available = (fields["Pages free"] + fields["Pages inactive"]) * page
                level = _kernel_free_percent()
                if level is not None:
                    available = total * level // 100
            elif sys.platform.startswith("linux"):
                with open('/proc/meminfo') as f:
                    fields = {k: int(v)*1024 for k,v in re.findall(r"^(\w+):\s+(\d+)", f.read(), re.M)}
                total, available = fields['MemTotal'], fields['MemAvailable']
            elif sys.platform == "win32":
                total, available = windows_memory()
            else:
                raise ValueError('memory telemetry unsupported')
            result.update(total_bytes=total, available_bytes=min(total, max(0, available)))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            pass
        _cache = (time.monotonic(), result)
        return dict(result)


def task_budget(settings, memory, slots):
    """Keep one inference slot for home; cap workers by RAM and server slots."""
    maximum = max(1, min(settings.max_task_workers, max(1, slots - 1)))
    if not settings.auto_parallel:
        return max(1, settings.task_workers), 'Fixed worker limit'
    available = memory.get('available_bytes')
    if available is None:
        return 1, 'Memory unknown; one task at a time'
    reserve = max(settings.ram_reserve_gb*GIB, (memory.get('total_bytes') or 0)*0.1)
    memory_limit = max(0, int((available-reserve) / (settings.task_ram_gb*GIB)))
    # A task loads no model: it uses the one already running, and costs little
    # memory of its own. So one task always runs; free RAM decides only how
    # many more may run beside it. (A limit of 0 here once left a task queued
    # forever on a machine whose model took most of its memory.)
    limit = max(1, min(maximum, memory_limit))
    if memory_limit < 1:
        reason = 'Low RAM: one task at a time'
    else:
        reason = 'Server slot limit' if maximum <= memory_limit else 'RAM budget'
    return limit, reason


def model_start_problem(settings, spec, memory=None):
    """Only gate new managed processes; never kill or resize a running server."""
    if not settings.auto_parallel:
        return None
    memory = memory if memory is not None else memory_snapshot()
    available = memory.get('available_bytes')
    if available is None:
        return 'RAM telemetry is unavailable; start the model manually or disable AUTO_PARALLEL in config.py.'
    # Weight overhead + a configurable allowance for KV cache/runtime buffers.
    needed = os.path.getsize(spec['gguf'])*1.25 + settings.model_runtime_gb*GIB
    reserve = max(settings.ram_reserve_gb*GIB, (memory.get('total_bytes') or 0)*0.1)
    if available < needed + reserve:
        return (f"Not starting {spec['role']}: approximately {needed/GIB:.1f} GB plus "
                f"{reserve/GIB:.1f} GB reserve needed; {available/GIB:.1f} GB available. "
                "Close another workload or use a smaller configured model.")
    return None
