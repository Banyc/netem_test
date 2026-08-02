#!/usr/bin/env python3
"""Dependency-free host calibration probe: CPU loop time and sleep overshoot."""

import time

ITERATIONS = 20_000_000
SLEEPS = 200
SLEEP_S = 0.005

t0 = time.perf_counter()
acc = 0
for i in range(ITERATIONS):
    acc += i
cpu_loop_s = time.perf_counter() - t0

worst_5ms_sleep_overshoot_ms = 0.0
for _ in range(SLEEPS):
    t0 = time.perf_counter()
    time.sleep(SLEEP_S)
    actual = time.perf_counter() - t0
    worst_5ms_sleep_overshoot_ms = max(
        worst_5ms_sleep_overshoot_ms, (actual - SLEEP_S) * 1000.0
    )

print(
    f"cpu_loop_s={cpu_loop_s:.3f} "
    f"worst_5ms_sleep_overshoot_ms={worst_5ms_sleep_overshoot_ms:.2f}"
)
