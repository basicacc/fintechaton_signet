"""Run a command under a thermal watchdog.

Pauses the process (SIGSTOP) when the GPU gets hot, resumes once it cools down,
and terminates it if temperatures reach the hard limit.

    python thermal_guard.py -- python train.py --config sentinel.yaml
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime


def gpu_stats() -> tuple[int, float]:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[0]
    temp, power = (x.strip() for x in out.split(","))
    return int(temp), float(power) if power not in ("[N/A]", "") else 0.0


def cpu_temp() -> float | None:
    try:
        out = subprocess.run(["sensors"], capture_output=True, text=True, timeout=5).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if line.startswith(("Package id 0:", "Tctl:")):
            return float(line.split("+")[1].split("°")[0])
    return None


def log(msg: str) -> None:
    print(f"[thermal {datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pause-at", type=int, default=80, help="GPU °C: pause the job")
    ap.add_argument("--resume-at", type=int, default=70, help="GPU °C: resume a paused job")
    ap.add_argument("--abort-at", type=int, default=85, help="GPU °C: terminate the job")
    ap.add_argument("--cpu-pause-at", type=int, default=92, help="CPU package °C: pause the job")
    ap.add_argument("--cpu-resume-at", type=int, default=80, help="CPU package °C: resume (with GPU below its resume)")
    ap.add_argument("--cpu-abort-at", type=int, default=99, help="CPU package °C: terminate")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        ap.error("no command given")

    proc = subprocess.Popen(cmd, start_new_session=True)
    paused = False
    peak = 0
    try:
        while proc.poll() is None:
            temp, power = gpu_stats()
            ctemp = cpu_temp()
            peak = max(peak, temp)
            if temp >= a.abort_at or (ctemp is not None and ctemp >= a.cpu_abort_at):
                log(f"ABORT gpu={temp}°C cpu={ctemp}°C, terminating job")
                os.killpg(proc.pid, signal.SIGCONT)
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=60)
                return 3
            hot = temp >= a.pause_at or (ctemp is not None and ctemp >= a.cpu_pause_at)
            cool = temp <= a.resume_at and (ctemp is None or ctemp <= a.cpu_resume_at)
            if not paused and hot:
                log(f"pause gpu={temp}°C cpu={ctemp}°C power={power:.0f}W")
                os.killpg(proc.pid, signal.SIGSTOP)
                paused = True
            elif paused and cool:
                log(f"resume gpu={temp}°C cpu={ctemp}°C")
                os.killpg(proc.pid, signal.SIGCONT)
                paused = False
            time.sleep(a.interval)
    except KeyboardInterrupt:
        os.killpg(proc.pid, signal.SIGCONT)
        os.killpg(proc.pid, signal.SIGTERM)
        raise
    log(f"job finished rc={proc.returncode}, peak gpu={peak}°C")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
