# Interactive-latency oracle (`rtp_mux_jitter`)

The game-relay oracle: `game client -TCP-> access-server -rtp_mux(rtp+mux)-> proxy-server -TCP-> game server`.
It measures the interactive lane's per-message latency and the bulk lane's
throughput for a seeded, deterministic impairment link.

Binary: `tests/tests/rtp_mux_jitter.rs`. Support: `tests/tests/support/{rtp,mux,frame,dual,stats}.rs`.

> **Gate scope:** the interactive oracle below is the `perf` tier. The full
> scenario-gate scope, with every `#[ignore]`d test's tier, lives in
> `tests/GATE.md` and is verified by `python3 tools/check-gate.py`. A plain
> `cargo test -p tests` runs only the default tier and none of these arms.

## Running

```sh
cargo test --release -p tests --test rtp_mux_jitter -- --ignored --nocapture --test-threads=1
```

Arms: interactive latency (`solo` / `loss` / `bulk` / `bulk+loss`) in byte-stream,
frame-delivery, frame-reorder, and **dual-lane** modes; FEC arms at 2 % and 6 %
loss; the frame-reorder+FEC arms. The **dual-lane** arms are the deployment
topology: interactive lane on its own RTP connection (frame mode + fast-forward
+ FEC + fresh-tail armor), bulk lane separate and strict.

## Acceptance criteria (the optimization loop gate)

Every change to the interactive path must clear ALL of these, and the gate is
re-read every iteration:

1. **Interactive latency** — the dual-lane `bulk+loss` (`both`) p50/p90/p99/max
   must improve or stay at the one-way floor, with zero `>250 ms` spikes.
2. **Interactive throughput is normal** — the interactive lane's OWN wire
   (`interactive pair ... forwarded_bytes`, 30 s run) stays bounded and the
   message delivery ratio is `1.000` (all offered messages delivered at the
   expected rate). Redundancy may raise the wire modestly, never the delivery
   correctness.
3. **Redundancy is loss-adaptive and bounded (hostile-safe)** — the extra
   packets (armor duplicates / parity) may NOT grow with the wire loss rate.
   Under high loss the redundancy must back off toward the primary so it cannot
   amplify congestion / regress a hostile environment. A test asserts the
   packets-per-message does not increase as loss rises.
4. **Bulk-lane throughput untouched** — the 5-lane perf battery
   (`tools/perf-loop`, lanes `clean / controller-fat-pipe / hostile /
   lossy-400kib / hostile-fat-pipe`) must be `no_material_change` on every
   `ready` **verdict** lane. The interactive change is a no-op for the
   stock/bulk tuning (`fec_instream_flush == false`). `hostile` is
   **diagnostic-only** (see below): report its numbers, never retain/reject on
   it.
5. **Read the generated graph (MUST) and the summary** — render the perf-loop
   `comparison.html` with `tools/render_graph.py` and read the RTT-CDF /
   throughput plots (base ≈ cand for a neutral change), not just the text
   verdict. The tool asserts that panels exist and carry series data; a graph
   that cannot be produced is a non-zero-exit error, not an empty file to skim
   past.

```sh
python3 tools/render_graph.py $TMPDIR/<run-output>/comparison.html \
  --out $TMPDIR/<run-output>/graphs
```

`/Users/charliesmith/code/tmp/rtp-loop/loop-report.sh` prints (1)(2)(4). For
(5), use the in-repo `tools/render_graph.py`; the old manual `render-graph.sh`
that lived outside the repository in an unversioned scratch directory degraded
silently (a missing graph produced a blank PNG and still exited zero), so it
must not be relied on.

## Perf-loop lane roles: verdict vs diagnostic

A `tools/perf-loop` lane is either a **verdict** lane (a change may be retained
or rejected on its paired evidence) or **diagnostic-only** (its numbers are
reported and may guide follow-up work, but a change must never be retained or
rejected on it). The lane's role is stamped as `link_role` into `run.json` by
`perf_loop.lane_classification`.

**`hostile` is diagnostic-only.** In every one of the 70 recorded `hostile`
lane runs — both the 5 s and the 20 s warmup — the lane was `not_ready`
(`within_run_phase_not_stable`), and the spread was the lane's own stochastic
first/second-half goodput phase variance rather than a candidate effect (the
baseline and candidate trees were byte-identical in the control). The lane
cannot attribute a delta to a candidate, so it must not be read as a verdict.
Report its paired numbers as a diagnostic only.

The full lane-role table lives in `tests/GATE.md` (`gate-lane-roles`) and is
machine-checked by `python3 tools/check-gate.py` against
`perf_loop.lane_classification`, so a verdict lane cannot be mis-declared
diagnostic (or the reverse) without the checker failing.

## The rtp-side in-process oracle

The `rtp` crate carries its own deterministic in-process oracles for the
per-packet interactive repair path (faster and lower-noise than the netem
harness, and they read the sender/receiver FEC counters directly):

- `src/socket/stream.rs::probe_single_symbol_interactive_fec_repair` — the
  single-symbol interactive repair path for the depth-1 `interactive_prompt`
  preset vs depth-3 `max_diversity`.
- `src/socket/stream.rs::probe_fresh_tail_armor_latency` — the fresh-tail armor
  duplicate's repair latency vs the ARQ fallback.

```sh
cargo test --lib probe_ -- --ignored --nocapture   # from crates/rtp
```

Use these to attribute a repair-latency change before/after a fix; use the
netem harness above for the end-to-end dual-lane latency + throughput gate.

## Notes

- The oracle is stock byte-stream by default; the deployment runs frame mode +
  dual lane, so the `*_frame*` and `_duallane_*` arms are the ones that match
  production.
- FEC/redundancy counters (parity_sent, armor duplicates, recovered) and the
  per-lane wire counters are printed per arm for attribution.
