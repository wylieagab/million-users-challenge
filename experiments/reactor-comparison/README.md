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

| Mode | Required app builds | Users | Repetitions | Ramp / hold per trial |
|---|---|---|---|---|
| smoke | New reactor | 15,000 | 1 | 60 s / 60 s |
| full | Original + Kestrel AOT + Kestrel JIT + reactor | 30,000 and 80,000 | 3 | 60 s / 120 s |
| capacity | Same four | 10,000, 30,000 and 100,000 | 3 | 60 s / 120 s |

The optional io_uring variant is added when its probe succeeds. Exit code 77
records an unsupported platform and skips that variant. Other probe failures
are errors. No sysctl, seccomp setting, or privilege is changed to enable it.
The required epoll probe and optional io_uring probe exercise real socket
readiness and rejection of stale events after descriptor re-registration.
Full mode can take about 90–110 minutes when all five variants run. Capacity
mode can take about 150 minutes. The job timeout is 180 minutes; additional
levels or confirmations need a correspondingly bounded schedule.

The driver also accepts `--levels 30000,80000`, `--rounds 3`, `--ramp 60`,
`--hold 120`, `--app-cores 1`, and `--confirm`. Confirmation is a fresh process/database trial
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
  their verified archives. PostgreSQL uses the `postgres:18` service image;
  the actual image ID, server settings/version, and host libpq 18 package
  version are recorded. The PostgreSQL image/package patch is not frozen
  across separate workflow runs. Comparisons belong within one job.
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
  The unchanged original example is a control: extra-test failures are
  preserved and displayed without patching its code. Its official tests
  must pass. All extra tests are mandatory for our implementations.
- The mixed workload uses `-gzip -think-scale 2.5` and four loopback IPs with
  two listening ports. At 100,000 users, each destination IP/port receives
  12,500 sockets, below the typical ephemeral-port range. Ramp speed never
  exceeds roughly 3,300 new users/s. The RNG uses the official generator's
  time-based seed; repeated runs preserve the user model, not an identical
  request sequence.
- A seeded shuffle followed by rotated variant order limits systematic
  ordering bias; the entire order is saved. Three repeats are not a complete
  Latin square with four or five variants. Median and range are descriptive;
  no statistically significant winner or maximum capacity is declared.

## Evidence and interpretation

Artifacts contain setup/build/test/server/generator logs, environment metadata,
source hashes, resolved package locks/assets, schedules, raw mload histograms,
endpoint counts/errors, connection and request latency, and one-second telemetry.
Every raw result and compact summary is also printed in the job log. The final
table reports all repetitions, including failed or resource-limited ones.

App CPU time is read from process-wide `/proc/PID/stat` counters; app RSS is
sampled with the process high-water mark. PostgreSQL CPU/memory comes from the
whole Docker cgroup, so query-worker CPU is included. Generator CPU/RSS, socket
memory, and memory/I/O pressure are captured as well. CPU per successful request
includes all app work and divides by successful responses; failures are shown
separately. Overall mload percentiles include ramp time, exactly as the official
generator reports them. Hold-period window percentiles are retained individually;
they are never averaged to fabricate an aggregate percentile.

A diagnostic pass requires the generator's latency/error thresholds, a live
app, at least 98% of requested connections throughout measured hold windows,
and neither generator nor database averaging 85% of its CPU allocation during
the hold. That CPU heuristic flags an obvious confounder; it cannot prove the
absence of I/O, lock, network, or noisy-neighbor bottlenecks. Inspect telemetry,
endpoint errors and repeat variance before attributing a tail-latency difference
to the application. Low-load wins and high-variance measurements establish no
winner. The official separate-machine 16/8/4-core trials remain necessary for
a challenge score.
