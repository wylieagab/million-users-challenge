# Isolated PostgreSQL text read experiment

This console harness compares the production `GetString` path, `GetChars` with reusable
UTF-16 scratch, and `GetBytes` with reusable UTF-8 scratch. It does not edit the API,
create database objects, insert rows, or start PostgreSQL. It uses .NET SDK 10.0.403,
Npgsql 10.0.3, and the shared ASP.NET Core framework solely for `ObjectPool` types.

`BaselineJsonBuffer.cs` and `Utf8JsonBuffer.cs` are the exact serializer implementations
from `../perf-followup/baseline-JsonBuffer.cs` and `../perf-followup/utf8-JsonBuffer.cs`,
with only their `JsonBuffer` identifiers renamed to allow both in one executable
(and normal file newline handling). Every measured feed includes their real `Reset`
routine: output-buffer clearing, writer reset, and the candidate's scratch clearing.
The `GetChars` comparator reads into reusable growing scratch, then writes its span.

## Build and validate

From this directory, with the portable SDK on PATH if needed:

```powershell
$env:DOTNET_ROOT = 'C:\Users\wylie\Documents\Codex\2026-10-27\alr\work\dotnet'
$env:DOTNET_CLI_HOME = 'C:\Users\wylie\Documents\Codex\2026-10-27\alr\work\dotnet-home'
$env:NUGET_PACKAGES = 'C:\Users\wylie\Documents\Codex\2026-10-27\alr\work\nuget'
$env:PERF_CONNECTION = 'Host=127.0.0.1;Port=55432;Username=postgres;Database=feed_perf_20261029'
& "$env:DOTNET_ROOT\dotnet.exe" build -c Release --configfile NuGet.Config
& "$env:DOTNET_ROOT\dotnet.exe" bin/Release/net10.0/TextReadExperiment.dll --mode validate
```

`PERF_CONNECTION` is optional; the default is the local connection shown above.
The harness sets `ClientEncoding=UTF8` explicitly because raw text bytes must match
the encoding expected by `Utf8JsonWriter`. It does not print the connection string.

Validation is the default mode. It compares exact complete JSON bytes for the live
20-row feed and twelve parameter-only SELECT cases: empty strings; 500 ASCII,
BMP, and astral Unicode characters; mixed boundaries; JSON control/quote/backslash
escaping; Unicode separators; and authors that cross scratch boundaries or grow to
20,000 UTF-8 bytes. It also verifies round trips and truncation of a microsecond
PostgreSQL timestamp to exactly `2025-12-31T23:59:19.409Z`. PostgreSQL TEXT cannot store
NUL or malformed Unicode, so those are not generated as database values.

Validation passed locally: the fresh seeded feed produced 5,102 identical bytes for
all three paths, and all twelve additional cases matched exactly.

## Run measurements deliberately

The harness never starts measurements without a mode argument. Stop unrelated load
first. Use enough warmup for JIT tiering, repeat rounds, and compare medians:

```powershell
# One actual query, repeatedly serialize its first buffered row 20 times per iteration.
& "$env:DOTNET_ROOT\dotnet.exe" bin/Release/net10.0/TextReadExperiment.dll --mode buffered --warmup 1000 --iterations 10000 --rounds 6

# Actual exact 20-row challenge query on every measured iteration.
& "$env:DOTNET_ROOT\dotnet.exe" bin/Release/net10.0/TextReadExperiment.dll --mode feed --warmup 100 --iterations 1000 --rounds 6
```

`--mode all` runs both measurements after validation. All modes run validation first.
Default measured settings are 1,000 iterations, 100 warmup feeds, and five rounds;
the mode defaults to `validate`. Variant order rotates each round. Timed loops do not
print, and the writer/scratch/command objects are warmed before measurement.

CSV rows include elapsed milliseconds, nanoseconds per feed, current-thread managed
bytes per feed, whole-process managed bytes per feed, GC collection counts, and JSON
length. The checksum consumes output so serialization cannot be eliminated. The
full-query case prepares and reuses the same command and connection for all variants.

## Interpretation and limitations

- `buffered20_same_row` isolates column access and JSON serialization. It repeats one
  held database row twenty times, so its output size and text distribution differ
  from a real feed. This diagnostic reuse never occurs in the production API.
- `query20` reads the actual twenty rows with the exact challenge SQL on every
  iteration. It includes network/database work, but reuses one prepared command and
  open connection, unlike the application's per-request command/pool operations.
- Both are synchronous, single-caller diagnostics with no HTTP, TLS, compression,
  concurrency, idle sockets, or load-generator traffic. Do not turn these timings
  into requests-per-second capacity or million-user claims.
- Current-thread allocation excludes Npgsql/background thread work; whole-process
  allocation includes that work but can be noisier. Neither metric includes native
  buffers or kernel/socket memory. No forced GC occurs inside or between variants.
- These commands run a managed Release build. Native AOT can change inlining and code
  generation; the production AOT binary still needs an end-to-end comparison.
- Body size is bounded by the schema to 500 Unicode scalars, up to 2,000 UTF-8 bytes.
  `users.username` is unbounded TEXT. Both scratch paths handle growth; a fixed seed-
  derived username limit would be incorrect. Large buffer-growth allocations happen
  during warmup for repeated workloads, so inspect exceptional workloads separately.

Npgsql 10.0.3 public source confirms that `GetBytes` exposes a column's raw wire bytes
without restricting the PostgreSQL type, while `GetChars` obtains a text reader that
allocates an intermediate string for small buffered text. The candidate still copies
bytes into scratch; it avoids UTF-16 strings and transcoding, and is not a zero-copy
claim. Relevant pinned sources:

- https://github.com/npgsql/npgsql/blob/v10.0.3/src/Npgsql/NpgsqlDataReader.cs
- https://github.com/npgsql/npgsql/blob/v10.0.3/src/Npgsql/Internal/PgReader.cs
