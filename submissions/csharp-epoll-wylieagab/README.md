# C# per-worker reactor experiment

This is an experimental implementation, not an upstream challenge submission or
a claim of improved capacity. Benchmark it against the original Minimal API and
the two frozen Kestrel implementations before considering submission.

## Design

- One dedicated worker thread per available processor (up to 64); no CPU pinning
  inside the program. Each worker owns both HTTP listeners with `SO_REUSEPORT`,
  its clients, timers, buffers, and PostgreSQL connections.
- Linux epoll readiness notification. HTTP and libpq sockets are registered in
  the same worker event loop. No thread-pool dispatch in the request path.
- libpq prepared queries in nonblocking pipeline mode, four read connections
  and two write connections per worker; 384 connections maximum. Bounded queues
  and pipeline depth prevent unlimited query accumulation. Initially every
  command has its own Sync; writes succeed only after their synchronization
  result proves transaction completion. There are no uncertain-write retries.
- The same five endpoints, SQL, JWT checks, ordered JSON, Unicode body-length
  rules and commit-before-response contract as the challenge. No result caches.
- HTTP request buffers are rented on activity and returned after parsing. Idle
  clients retain connection state, not a large receive buffer. An intrusive
  timer wheel maintains the 130-second idle lifetime without scanning all clients.
- A synchronous gzip stream compresses negotiated responses of at least 1 KiB.
  Compression and JSON construction are intentionally ordinary BCL operations
  for the first measured prototype; no assumed benefit from replacing them.
- .NET SDK 10.0.403 / runtime 10.0.14, self-contained tiered JIT with PGO and
  Server GC. Runtime/SDK match the comparison builds.
- libpq 18.6, installed as the exact PGDG Ubuntu 24.04 packages
  `libpq5=18.6-1.pgdg24.04+2` and `libpq-dev=18.6-1.pgdg24.04+2` validated in CI.

`--io-uring` selects the optional io_uring **readiness polling** backend while
preserving the rest of the application. It does not use completion-based
accept/receive/send, registered files, provided buffers, or zero-copy send.
`--probe-io-uring` checks kernel/library support without opening a database.
There is no silent fallback to epoll when that mode fails.
`--probe-epoll` runs a local socket-pair check of readiness, unchanged interest,
stale registration rejection, and replacement without opening a database or HTTP
listener. Both probes exit after their checks.

## Build and tests

On Ubuntu 24.04 x86_64:

```sh
sudo bash install.sh
bash build.sh
./out/FeedApi --probe-epoll
DATABASE_URL=postgres://feed:password@127.0.0.1/feed JWT_SECRET=million-users-challenge \
  HOST=0.0.0.0 PORT=3000 EXTRA_PORT=3001 bash start.sh
```

`install.sh` uses the [official PostgreSQL Apt repository](https://www.postgresql.org/download/linux/ubuntu/)
and verifies its signing key fingerprint
`B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8` before configuring the signed source.
It installs and verifies the exact libpq package versions above, prints them for
the installation log, and refuses to downgrade a newer installed libpq package.
The SDK archive remains verified with its pinned SHA-512 digest.

The optional `install-uring.sh` builds the pinned liburing FFI library before
`build.sh`. The default epoll backend does not need liburing.

Run `dotnet run --project tests/http/ParserTests.csproj -c Release` for the
fragmentation/framing unit checks. The fork-only comparison harness runs the
unchanged official 42 checks, extended API checks, and transport regressions
against the Linux binary before measuring it. Raw artifacts and environment
metadata are kept by `experiments/reactor-comparison`.
That harness records native package versions, kernel information, and both
readiness-probe results in its environment metadata and logs alongside the
.NET SDK/runtime information.

## Current limitations

The prototype binds IPv4. No performance results are asserted until the
comparison completes. A hosted CI runner sharing its CPU/RAM with PostgreSQL and
the load generator cannot establish an official million-user score.
