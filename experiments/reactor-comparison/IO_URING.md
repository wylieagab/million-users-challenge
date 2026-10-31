# io_uring comparison scope and feasibility

Research and implementation notes, 2026-10-31. The initial production candidate
uses epoll. `--io-uring` selects an experimental readiness backend in the same
C# worker, HTTP parser, authentication, serializer, compression and libpq
pipeline implementation. It must pass the same correctness checks before its
measurements are compared with the epoll build.

## What the optional backend measures

`UringPoller` submits one-shot `IORING_OP_POLL_ADD` requests and receives socket
readiness through the completion queue. The application still calls its normal
nonblocking socket reads/writes; libpq still owns its PostgreSQL I/O. Polls are
rearmed on the next `Wait`, after the worker handles the preceding events.
Unchanged interest masks do not trigger cancellation. Changed masks and removals
cancel the previous registration; generation tags discard late completions from
old registrations or reused file descriptors. Completion-queue backpressure is
drained without discarding readiness.

This is **io_uring readiness polling**, not a completion-based HTTP transport.
It does not use `RECV`, `SEND`, multishot accept/receive, fixed descriptors,
provided buffers, zero-copy sends, SQPOLL, or DEFER_TASKRUN. Its one-shot rearming
and interop calls can cost more than epoll's persistent registrations. Shared
submission/completion queues can batch poll operations, but normal socket I/O
syscalls remain. A result for this adapter cannot establish the potential of a
complete io_uring server.

The narrow scope is deliberate: it allows one backend choice to change while
the rest of the application stays the same. A full completion transport would
need a different interface from `PollEvent(fd, readinessMask)`: receive results
must identify buffer ownership and byte counts, sends must keep their storage
alive until completion, and close/cancel must account for outstanding operations.

## Dependency and interop

The implementation uses **liburing 2.5**, built from the upstream source archive:

    https://github.com/axboe/liburing/archive/refs/tags/liburing-2.5.tar.gz
    SHA-256: 456f5f882165630f0dc7b75e8fd53bd01a955d5d4720729b4323097e6e9f2a98

`install-uring.sh` verifies that digest and builds
`.native/lib/liburing-ffi.so.2`; the application build copies it beside the
published executable. The archive was fetched from the official tag and its
digest checked locally. This is a pinned source build, not a shipped prebuilt
library. GCC, make, curl, tar and sha256sum are prerequisites.

The ordinary `liburing.so.2` does not export all of its inline helpers. Upstream's
separate FFI library exports `io_uring_get_sqe`, poll preparation, user-data and
completion helpers. That avoids handwritten SQ/CQ memory barriers and a custom
shim. The source still binds the pinned Linux x86-64 ABI for an opaque 216-byte
ring and 16-byte completion record. The installer uses C static assertions to
check the sizes and feature/result offsets; the application checks library
version 2.5 at startup.

Primary sources:

- [liburing 2.5 FFI export map](https://github.com/axboe/liburing/blob/liburing-2.5/src/liburing-ffi.map)
- [Library and FFI build targets](https://github.com/axboe/liburing/blob/liburing-2.5/src/Makefile)
- [Pinned C ABI and helper implementations](https://github.com/axboe/liburing/blob/liburing-2.5/src/include/liburing.h)
- [One-shot poll behavior and completion results](https://github.com/axboe/liburing/blob/liburing-2.5/man/io_uring_prep_poll_add.3)

## Kernel and runner checks

The challenge permits io_uring: existing scored entries use it. Its application
service runs as the `challenge` user with network-bind capability and a file
descriptor limit; it does not promise arbitrary kernel settings or elevated
privileges. This implementation does not change either. It uses ordinary ring
setup flags and requires `POLL_ADD`, `POLL_REMOVE`, and `IORING_FEAT_NODROP`.

The operating-system label alone is insufficient. On 2026-10-28 the maintainer
reported that Zig PR #4 passed GitHub CI but failed provided-buffer registration
on the challenge's Ubuntu kernel **6.8.0-147**. On 2026-10-29 the maintainer
identified the kernel as the cause, booted **6.8.0-117**, and successfully scored
the unchanged commit. The readiness adapter does not use provided-buffer
registration, so it does not depend on that particular operation. This does not
remove the need to test its own operations on the actual kernel.

- [Initial kernel failure](https://github.com/arjaythedev/million-users-challenge/pull/4#issuecomment-6476810397)
- [Maintainer's correction and kernel change](https://github.com/arjaythedev/million-users-challenge/pull/4#issuecomment-6502215730)
- [Successful score on 6.8.0-117](https://github.com/arjaythedev/million-users-challenge/pull/4#issuecomment-6507400109)
- [Application service configuration](https://github.com/arjaythedev/million-users-challenge/blob/main/box/challenge.service)

The comparison should record `uname -a`, process identity, seccomp state,
`kernel.io_uring_disabled`, `kernel.io_uring_group` when present, and the open-file
and locked-memory limits. A runner VM and a container on that VM can expose
different capabilities. The actual unprivileged probe is decisive; no sysctl,
seccomp or privilege changes should be made just to make this variant pass.

`--probe-io-uring` uses a local socket pair, not a database or listener. It checks
ring initialization, required opcodes, input readiness, interest replacement and
removal. Exit 0 means the probe passed. Exit 77 means the dependency/kernel is
unavailable or restricted; the comparison may report this variant as unavailable.
Unexpected probe failures are errors, not skipped measurements. Starting with
`--io-uring` never silently substitutes epoll.

## What the leading implementations add

The [Zig PR #4 description](https://github.com/arjaythedev/million-users-challenge/pull/4)
describes a substantially different transport: two workers, fixed descriptors,
completion batching, custom HTTP and PostgreSQL clients, packed prepared reads,
group-commit writes and custom gzip. Its author identifies read packing as the
largest local saving. Its official score therefore does not isolate io_uring's
effect. The scored version also had an outstanding JWT-expiration correctness
issue, which the maintainer kept separate from the performance result.

The [C io_uring entry](https://github.com/arjaythedev/million-users-challenge/blob/main/submissions/c-iouring-einhornimmond/README.md)
uses multishot accepts/receives, direct descriptors, provided buffers and queued
sends for HTTP. It nevertheless uses **one-shot readiness polls for libpq**,
because libpq can leave unread bytes in a socket. That is a useful precedent for
keeping the database adapter on readiness even if HTTP later moves to completion
I/O.

The [Java epoll entry](https://github.com/arjaythedev/million-users-challenge/blob/main/submissions/java-epoll-stohirov/README.md)
has an optional full io_uring transport and reports that it was not cheaper than
epoll in its local comparison. Batching changed its costs substantially. These
are author-reported local results on different hardware, useful for forming a
hypothesis rather than predicting this C# implementation's result.

## Validation and interpretation

The C# root project compiles with the readiness backend in Release mode with zero
warnings/errors on the Windows development host. This verifies managed source
and generated interop declarations, **not Linux ABI or runtime behavior**. Linux
probe, correctness, descriptor churn and load results must be recorded separately
by the comparison harness.

Before accepting performance results, cover both listeners, partial requests and
writes, repeated interest changes, reconnects and descriptor reuse, cancellation,
PostgreSQL pipeline failures, and every existing official/extended correctness
check. Run both backends with the same worker count, SQL, pool size, pipeline
depth, compression, GC settings and publish mode. Compare them with the original
Minimal API and both previous Kestrel submissions under the same conditions.
Keep failed and unsupported variants visible; do not report measurements from a
fallback as io_uring measurements.

Report repeated throughput, p95/p99, errors, application CPU per request, process
memory and connection counts. A shared CI runner with local PostgreSQL and load
generation provides a diagnostic comparison, not an official connection-capacity
score. Establish the epoll baseline first. Consider a completion-based HTTP
transport only if measurements justify the additional lifetime and cancellation
complexity; this adapter's result alone cannot prove it would help or hurt.
