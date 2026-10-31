# C# reactor comparison

This fork-only experiment compares the original Minimal API example, frozen
Kestrel NativeAOT and JIT submissions, and the new epoll/libpq-pipeline server.
The optional io_uring variant changes only readiness notification, using the same
reactor binary, HTTP parser, database pipeline, and application logic. It is not
a claim about completion-based io_uring send/receive or registered buffers.

The workflow runs on an ordinary public `ubuntu-24.04` GitHub-hosted runner. It
does not provision a paid runner or modify an upstream submission. These are
**shared-host diagnostics, not official challenge scores**: PostgreSQL and the
generator share this VM with the app, traffic uses loopback, the VM/kernel and
memory differ from the challenge machines, and noisy-neighbor effects remain.

## Running

Commit `smoke`, `full`, or `capacity` to `mode.txt` on the fork experiment PR.
The path-filtered workflow triggers on that change. It can also be run through
GitHub's manual workflow button. All correctness checks precede any benchmark.

| Mode | Required app builds on every runner | Users | Replication | Ramp / hold per trial |
|---|---|---|---|---|
| smoke | New reactor | 15,000 | 1 runner, 1 round | 60 s / 60 s |
| full | Original + Kestrel AOT + Kestrel JIT + reactor | 30,000 and 80,000 | 3 parallel runners, 1 round each | 60 s / 120 s |
| capacity | Same four | 10,000, 30,000 and 100,000 | 3 parallel runners, 1 round each | 60 s / 120 s |

The optional io_uring variant is added when its probe succeeds. Exit code 77
records an unsupported platform and skips that variant. Other probe failures
are errors. No sysctl, seccomp setting, or privilege is changed to enable it.
The required epoll probe and optional io_uring probe exercise real socket
readiness and rejection of stale events after descriptor re-registration.
Every replica compares all available variants on its own host, with at most
three jobs running in parallel. Full mode should take roughly 35–45 minutes;
capacity mode roughly 50–65 minutes. Actual queue, download, build, and test
times vary. The per-job timeout is 180 minutes. Total runner minutes increase
because setup and correctness checks repeat, but no paid/larger runner is used.

The driver also accepts `--levels 30000,80000`, `--rounds 3`, `--ramp 60`,
`--hold 120`, `--app-cores 1`, `--replicate-index 0`, and `--confirm`. Direct full
mode defaults to three serial rounds; the workflow explicitly uses one round
on each of three hosts. Confirmation is a fresh process/database trial
with a 60-second ramp and a five-minute hold at each variant's highest level
that passed all repeated trials without a detected generator/database CPU
bottleneck. This bounded grid is not the challenge's maximum-user search.

On an existing disposable Ubuntu host with Docker, use the same environment
variables as the workflow, including `PG_CONTAINER`, `RUNNER_TEMP`, and a
scratch `GITHUB_PATH` file. Set `ALLOW_LOCAL_DISPOSABLE_DB=1` before running
`prepare.sh`; setup replaces only the designated server's `feed_seed`, and the
driver drops/recreates only `reactor_trial`. Then run:

```sh
bash experiments/reactor-comparison/prepare.sh
export PATH="/opt/million-users-dotnet-10.0.403:/usr/lib/postgresql/18/bin:$RUNNER_TEMP/reactor-go/go/bin:$PATH"
python3 -u experiments/reactor-comparison/run.py --mode full --allow-disposable-database
```

## Controls and limitations

- The original source is frozen at `954719a0411aedf9f65884e810d2deefd8db7cdb`,
  Kestrel AOT at `3cff0a51d19018639af2614be2308762a738133e`, and Kestrel JIT
  at `a4af720c410b29b030fbec16fb37617c14d34d21`. Each is extracted into an
  isolated directory. The new server is copied from the tested commit. Source
  hashes, resolved dependencies, and build logs are saved.
- SDK 10.0.403/runtime 10.0.14 and Go 1.27.1 are pinned. The setup downloads
  their verified archives. PostgreSQL uses the `postgres:18.6` service image,
  pinned to digest `sha256:979bccfde35445db618c65107d27d08c6c21339d8dc3ad4c830c6633947be0e4`;
  the actual image ID, server settings/version, and host libpq package version
  are recorded. The installer pins both `libpq5` and `libpq-dev` to
  `18.6-1.pgdg24.04+2`, validated on all three Ubuntu replicas. Each raw
  comparison belongs within one runner.
- Exactly one app and one generator run at a time. On a four-CPU runner,
  external `taskset` gives the app two logical CPUs and the generator one;
  Docker limits PostgreSQL to the fourth. This exercises per-core sharding.
  Additional host CPUs go to PostgreSQL. `--app-cores 1` permits a separate
  follow-up when PostgreSQL limits the two-worker comparison. Submission code contains no
  affinity policy. All variants receive the same 60% GC heap limit, process
  descriptor limit, host memory, and request configuration. This overrides
  the original example's 70% GC default. It does not impose a separate
  per-app memory cgroup; peak RSS is measured and reported.
- PostgreSQL receives the repository configuration with explicit diagnostic
  overrides: 1 GiB shared buffers, 4 GiB effective cache, 256 MiB maintenance
  memory, 2 GiB maximum WAL and 256 MiB minimum WAL. `fsync`, synchronous
  commits, and full-page writes remain enabled. The unchanged deterministic
  seed and its checksum run once. Every correctness session and load trial
  starts with a fresh clone, a completed database checkpoint, and fresh app process. Benchmark writes never
  affect the seed or another trial. There is no separate application warm-up.
- The official `bench/mload` source and `test/test.sh` remain unchanged; the
  driver checks their Git diff against the frozen repository base. Every
  variant runs the 42 official assertions, the existing extended HTTP suite,
  and independent raw TCP checks for fragmented headers/bodies, UTF-8,
  chunking, pipelining, framing, write-half-close with queued responses,
  connection-close, and 76-second idle reuse. Reactor variants additionally
  run their database-pipeline regression, including failed writes followed by
  successful commands and concurrent mixed outcomes with readback.
  Separate child-process regressions check 4,096 pipelined synchronous responses
  with one queued continuation, then descriptor-exhaustion backoff, service to
  existing connections, and recovery of new accepts. Only the test child has a
  reduced descriptor limit; benchmark process limits stay identical.
  Official assertions are mandatory for every variant. The pre-existing
  extended HTTP suite is mandatory for frozen Kestrel AOT/JIT and the reactors;
  the unchanged original example's extended failures remain observational.
  Every new transport case runs independently on each port, even after another
  case fails, and writes its outcome to `transport-cases.json`. New transport
  differences in frozen baselines are observational; every transport and
  pipeline assertion remains mandatory for reactors. No assertion is disabled
  or converted into a pass. Correctness logs, JSON, and the final report identify
  blocking failures separately from observational failures.
- The mixed workload uses `-gzip -think-scale 2.5` and four loopback IPs with
  two listening ports. At 100,000 users, each destination IP/port receives
  12,500 sockets, below the typical ephemeral-port range. Ramp speed never
  exceeds roughly 3,300 new users/s. The RNG uses the official generator's
  time-based seed; repeated runs preserve the user model, not an identical
  request sequence.
- A seeded shuffle followed by rotated variant order limits systematic
  ordering bias; the entire order and replica rotation are saved. Replicas
  0/1/2 use the same three orders as three serial rounds when the same variants
  are available. Three repeats are not a complete Latin square with four or
  five variants. This exchanges within-host repetition for independent-host
  replication; it does not remove hardware variation or establish statistical
  significance from three observations.

## Evidence and interpretation

Artifacts contain setup/build/test/server/generator logs, environment metadata,
source hashes, resolved package locks/assets, schedules, raw mload histograms,
endpoint counts/errors, connection and request latency, and one-second telemetry.
Every raw result and compact summary is also printed in the job log. The final
table reports all repetitions, including failed or resource-limited ones.

The [initial full comparison](https://github.com/wylieagab/million-users-challenge/actions/runs/41221323833)
observed the original example's extended suite stop at wildcard gzip precedence
(`*;q=1, gzip;q=0`). Assertions after that failure did not run; the UTF-16 length
issue found by source inspection is not an observed failure from that run.
The original example and frozen Kestrel AOT passed the initial transport group,
then closed before draining the write-half-closed pipeline. That earlier suite
stopped at the failure, so it supplied no outcome for later cases. The independent
case runner now records those later cases instead of inferring their results.

Each artifact name, trial ID, and result identifies its replica. A unique host
block ID prevents accidental pairing across runners, even if replica numbers
are reused. `paired-ratios.json` and `PAIRED_RATIO` log lines compare candidate
CPU per successful request, p99 latency, and peak RSS to the corresponding
baseline at the same user count and round on the same runner. Ratios are
candidate / baseline, so values below one mean a smaller measured value.
They are emitted only when both trials pass diagnostic validity and the
generator's latency/error thresholds. Excluded pairs retain both observations,
status flags, and exclusion reasons; all underlying data remains available.

Across hosts, summarize eligible paired ratios with their sample count, median,
and range. Do not pool raw CPU times, RSS, or latency histograms across different
machines. Optional io_uring pairs exist only on hosts where its probe succeeds.
A ratio is evidence about one metric, not an automatic overall winner. Mixed
directions, few valid pairs, or large variation require further measurement.

App CPU time is read from process-wide `/proc/PID/stat` counters; app RSS is
sampled with the process high-water mark. PostgreSQL CPU/memory comes from the
whole Docker cgroup, so query-worker CPU is included. Generator CPU/RSS, socket
memory, and memory/I/O pressure are captured as well. CPU per successful request
includes all app work and divides by successful responses; failures are shown
separately. Overall mload percentiles include ramp time, exactly as the official
generator reports them. Hold-period window percentiles are retained individually;
they are never averaged to fabricate an aggregate percentile.

The unchanged generator initially sleeps `between(0, 5) * think-scale` before
each user's first request. With think-scale 2.5, the last ramped users may wait
up to another 12.5 seconds before connecting. Diagnostic CPU-utilization and
connection-count checks therefore start 15 seconds after the ramp. Latency
ticks summarize the preceding five seconds, so their diagnostic window check
starts at ramp + 20 seconds to cover only complete windows after settling.
For a 60-second ramp, these boundaries are 75 and 80 seconds respectively.
Early connection counts, all request errors, overall latency histograms, and
total app CPU per successful request remain recorded unchanged. This grace
period is not a workload modification or a separate application warm-up.

A diagnostic pass requires the generator's latency/error thresholds, a live
app, at least 98% of requested connections throughout the post-settling hold windows,
and neither generator nor database averaging 85% of its CPU allocation during
the post-settling hold. That CPU heuristic flags an obvious confounder; it cannot prove the
absence of I/O, lock, network, or noisy-neighbor bottlenecks. Inspect telemetry,
endpoint errors and repeat variance before attributing a tail-latency difference
to the application. Low-load wins and high-variance measurements establish no
winner. The official separate-machine 16/8/4-core trials remain necessary for
a challenge score.
