#!/usr/bin/env python3
"""Experiment-only Linux CI driver. Never invoke against an existing personal database.

The main submission and upstream test/seed scripts remain unchanged. Four private
source copies are compiled with the same SDK; only JsonBuffer and publish mode vary.
This is a same-host feed diagnostic, not the official mixed million-user workload.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote
from urllib.request import urlopen


SDK = Path('/opt/million-users-dotnet-10.0.403/dotnet')
RUNTIME = '10.0.14'
VARIANTS = ('baseline-aot', 'baseline-jit', 'utf8-aot', 'utf8-jit')
ORDERS = (VARIANTS, tuple(reversed(VARIANTS)),
          ('baseline-jit', 'baseline-aot', 'utf8-jit', 'utf8-aot'))
WARM_SECONDS, REFRESH_SECONDS, MEASURE_SECONDS = 60, 5, 30
CONNECTIONS, WORKERS = 128, 4


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def proc_snapshot(pid: int) -> dict:
    """Read process-wide CPU ticks (including its threads) and resident memory."""
    root = Path('/proc') / str(pid)
    stat = root.joinpath('stat').read_text().rsplit(') ', 1)[1].split()
    status = {}
    for line in root.joinpath('status').read_text().splitlines():
        key, _, value = line.partition(':')
        status[key] = value.strip()
    kb = lambda key: int(status.get(key, '0 kB').split()[0]) * 1024
    return {
        'pid': pid,
        'start_ticks': int(stat[19]),
        'cpu_seconds': (int(stat[11]) + int(stat[12])) / os.sysconf('SC_CLK_TCK'),
        'rss_bytes': kb('VmRSS'),
        'lifetime_peak_rss_bytes': kb('VmHWM'),
        'threads': int(status.get('Threads', '0')),
        'voluntary_context_switches': int(status.get('voluntary_ctxt_switches', '0')),
        'involuntary_context_switches': int(status.get('nonvoluntary_ctxt_switches', '0')),
    }


class Experiment:
    def __init__(self) -> None:
        if sys.platform != 'linux' or os.environ.get('GITHUB_ACTIONS') != 'true':
            raise RuntimeError('This destructive-database harness is restricted to disposable Linux GitHub Actions jobs.')
        self.repo = Path(os.environ['GITHUB_WORKSPACE']).resolve()
        self.results = Path(os.environ['PERF_RESULTS']).resolve()
        self.results.mkdir(parents=True, exist_ok=True)
        self.scratch = Path(tempfile.mkdtemp(prefix='csharp-perf-', dir=os.environ['RUNNER_TEMP']))
        self.source = self.repo / 'submissions/csharp-kestrel-wylieagab'
        self.sources = {}
        self.servers = {}
        self.files = []
        self.children = set()
        self.records = []
        self.started = time.monotonic()
        self.deadline = self.started + 19 * 60
        self.env = os.environ.copy()
        self.env.update({
            'DOTNET_ROOT': str(SDK.parent),
            'DOTNET_CLI_HOME': str(self.scratch / 'dotnet-home'),
            'NUGET_PACKAGES': str(self.scratch / 'nuget'),
            'DOTNET_CLI_TELEMETRY_OPTOUT': '1',
            'DOTNET_NOLOGO': '1',
            'DOTNET_GCHeapHardLimitPercent': '0x3c',
            'DOTNET_TieredCompilation': '1',
            'DOTNET_TieredPGO': '1',
            'PGCLIENTENCODING': 'UTF8',
            'JWT_SECRET': 'million-users-challenge',
            'HOST': '127.0.0.1',
            'ASPNETCORE_ENVIRONMENT': 'Production',
            'TZ': 'UTC',
        })

    def remaining(self, maximum=300) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Experiment reached its 19-minute driver deadline.')
        return min(maximum, remaining)

    def run(self, args, name, *, cwd=None, timeout=300, capture=False):
        # Do not print environment values: DATABASE_URL and PGPASSWORD stay private.
        with (self.results / (name + '.log')).open('w') as output:
            completed = subprocess.run(
                [str(arg) for arg in args], cwd=cwd or self.repo, env=self.env,
                stdout=subprocess.PIPE if capture else output,
                stderr=output, text=True, timeout=self.remaining(timeout),
                check=False,
            )
            if capture:
                output.write(completed.stdout)
        if completed.returncode:
            print((self.results / (name + '.log')).read_text()[-12000:], flush=True)
            raise RuntimeError(f'{name} exited {completed.returncode}; see its artifact log.')
        return completed.stdout if capture else None

    def sql(self, database, statement, *, name='sql', capture=False):
        return self.run(['psql', '-qXAt', '-v', 'ON_ERROR_STOP=1', '-d', database,
                         '-c', statement], name, timeout=90, capture=capture)

    def reset_database(self, name):
        if name not in ('feed_test', 'feed_bench'):
            raise ValueError('Only the two disposable experiment databases can be reset.')
        self.sql('postgres', f'DROP DATABASE IF EXISTS {name} WITH (FORCE)', name=f'{name}-drop')
        self.sql('postgres', f'CREATE DATABASE {name} TEMPLATE feed_seed', name=f'{name}-create')

    def database_url(self, database, readonly=False):
        user = 'perf_reader' if readonly else self.env['PGUSER']
        password = 'local-perf-only' if readonly else self.env.get('PGPASSWORD', '')
        auth = quote(user, safe='') + (':' + quote(password, safe='') if password else '')
        return f"postgres://{auth}@{self.env['PGHOST']}:{self.env['PGPORT']}/{database}"

    def stop(self, proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self.children.discard(proc)

    def launch(self, variant, database, *, ports, readonly=False):
        env = self.env | {
            'DATABASE_URL': self.database_url(database, readonly),
            'PORT': str(ports[0]), 'EXTRA_PORT': str(ports[1]),
        }
        log = (self.results / f'{variant}-{database}-server.log').open('w')
        self.files.append(log)
        executable = self.sources[variant] / 'out/FeedApi'
        proc = subprocess.Popen([str(executable)], cwd=executable.parent, env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        self.children.add(proc)
        targets = [f'http://127.0.0.1:{port}' for port in ports]
        limit = time.monotonic() + self.remaining(60)
        for target in targets:
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(f'{variant} exited before health succeeded.')
                try:
                    with urlopen(target + '/health', timeout=1) as response:
                        health = json.load(response)
                    if health.get('status') == 'ok' and health.get('db') == 'ok':
                        break
                except Exception:
                    pass
                if time.monotonic() > limit:
                    raise TimeoutError(f'No healthy response on {target} within 60 seconds.')
                time.sleep(0.2)
        return proc, targets

    def build(self):
        metadata = {
            'commit': self.run(['git', 'rev-parse', 'HEAD'], 'commit', capture=True).strip(),
            'sdk': self.run([SDK, '--info'], 'dotnet-info', capture=True),
            'os': platform.platform(), 'cpu_count': os.cpu_count(),
            'cpu_affinity_count': len(os.sched_getaffinity(0)),
            'cpu_info': Path('/proc/cpuinfo').read_text(),
            'memory_info': Path('/proc/meminfo').read_text(),
            'node': self.run(['node', '--version'], 'node-version', capture=True).strip(),
            'postgres': self.sql('postgres', 'SELECT version()', name='postgres-version', capture=True).strip(),
            'warm_seconds': WARM_SECONDS, 'refresh_seconds': REFRESH_SECONDS,
            'measure_seconds': MEASURE_SECONDS, 'connections': CONNECTIONS, 'workers': WORKERS,
            'orders': ORDERS, 'workload': 'feed only, gzip, both ports, closed-loop',
            'runtime_version': RUNTIME, 'gc_heap_hard_limit_percent': 60,
            'pg_client_encoding': 'UTF8',
            'notes': [
                'All variants use Release, Server GC, invariant globalization and the same SDK/runtime.',
                'Four warmed app processes remain alive; only one receives benchmark traffic at a time.',
                'The other app processes retain memory and database connections. Their CPU deltas are recorded.',
                'PostgreSQL and the validating Node generator share the runner with the applications.',
                'Warm-up is 60 seconds per process, shorter than the official three minutes.',
                'The fixed five-second refresh before each measurement reduces idle database-pool pruning effects.',
                'This is not the official mixed workload, million-connection memory test, or a capacity score.',
            ],
            'variants': {},
        }
        self.results.joinpath('environment.json').write_text(json.dumps(metadata, indent=2))
        for variant in VARIANTS:
            source = self.scratch / variant
            shutil.copytree(self.source, source,
                            ignore=shutil.ignore_patterns('bin', 'obj', 'out', '.git'))
            replacement = self.repo / '_perf' / (variant.split('-')[0] + '-JsonBuffer.cs')
            if not replacement.is_file():
                raise FileNotFoundError(replacement)
            shutil.copyfile(replacement, source / 'JsonBuffer.cs')
            self.sources[variant] = source
            mode = variant.endswith('-aot')
            props = [f'-p:PublishAot={str(mode).lower()}',
                     f'-p:RuntimeFrameworkVersion={RUNTIME}', '-p:SelfContained=true']
            if mode:
                props += ['-p:OptimizationPreference=Speed']
            else:
                props += ['-p:PublishTrimmed=false', '-p:TieredCompilation=true', '-p:TieredPGO=true']
            print(f'BUILD {variant}', flush=True)
            self.run([SDK, 'restore', 'FeedApi.csproj', '-r', 'linux-x64', '--force-evaluate',
                      '-p:RestoreLockedMode=false', *props], variant + '-restore', cwd=source)
            self.run([SDK, 'publish', 'FeedApi.csproj', '-c', 'Release', '-r', 'linux-x64',
                      '--no-restore', '-o', 'out', *props], variant + '-publish', cwd=source, timeout=480)
            shutil.copyfile(source / 'packages.lock.json', self.results / f'{variant}-packages.lock.json')
            runtime_config = source / 'out/FeedApi.runtimeconfig.json'
            if runtime_config.exists():
                shutil.copyfile(runtime_config, self.results / f'{variant}.runtimeconfig.json')
            metadata['variants'][variant] = {
                'publish_properties': props,
                'JsonBuffer_sha256': sha256(source / 'JsonBuffer.cs'),
                'project_sha256': sha256(source / 'FeedApi.csproj'),
                'lockfile_sha256': sha256(source / 'packages.lock.json'),
                'executable_sha256': sha256(source / 'out/FeedApi'),
                'executable_bytes': (source / 'out/FeedApi').stat().st_size,
                'publish_directory_bytes': sum(p.stat().st_size for p in (source / 'out').rglob('*') if p.is_file()),
            }
            self.results.joinpath('environment.json').write_text(json.dumps(metadata, indent=2))

    def validate(self):
        # test/run.sh invokes build.sh and start.sh, which force the AOT build.
        # Instead execute its unchanged assertion suite on each already-published
        # artifact, keeping its fresh-database and two-port-health requirements.
        for variant in VARIANTS:
            self.reset_database('feed_test')
            proc, targets = self.launch(variant, 'feed_test', ports=(3100, 3101))
            try:
                official = self.run(['bash', str(self.repo / 'test/test.sh'), targets[0]],
                                    variant + '-official-tests', timeout=90, capture=True)
                extended = self.run(['node', str(self.source / 'tests/extended-regression.mjs'), *targets],
                                    variant + '-extended-tests', timeout=90, capture=True)
                validation = {'variant': variant, 'official': official.strip().splitlines()[-1],
                              'extended': extended.strip().splitlines()[-1]}
                self.results.joinpath(variant + '-validation.json').write_text(json.dumps(validation, indent=2))
                print('VALIDATED ' + json.dumps(validation), flush=True)
            finally:
                self.stop(proc)

    def fingerprint(self, label):
        # Read by the harness outside timed runs; benchmark app role has SELECT only.
        query = '''SELECT
          (SELECT count(*) FROM users), (SELECT sum(id) FROM users),
          (SELECT count(*) FROM posts), (SELECT sum(id) FROM posts),
          (SELECT sum(octet_length(body)) FROM posts),
          (SELECT count(*) FROM likes), (SELECT sum(user_id) FROM likes),
          (SELECT sum(post_id) FROM likes)'''
        return self.sql('feed_bench', query, name=label, capture=True).strip()

    def bench(self, variant, seconds, label, measured=False):
        proc, targets = self.servers[variant]
        if proc.poll() is not None:
            raise RuntimeError(f'{variant} exited before {label}.')
        before = {name: proc_snapshot(server.pid) for name, (server, _) in self.servers.items()}
        args = ['node', str(self.source / 'tests/bench.mjs'), '--target', ','.join(targets),
                '--seconds', str(seconds), '--connections', str(CONNECTIONS),
                '--workers', str(WORKERS), '--workload', 'feed']
        output = (self.results / f'{label}.json').open('w')
        error = (self.results / f'{label}.stderr.log').open('w')
        worker = subprocess.Popen(args, cwd=self.repo, env=self.env, stdout=output, stderr=error)
        self.children.add(worker)
        began = time.monotonic()
        limit = began + self.remaining(seconds + 45)
        samples = []
        try:
            with (self.results / f'{label}-proc.jsonl').open('w') as series:
                while worker.poll() is None:
                    if time.monotonic() > limit:
                        raise TimeoutError(label + ' exceeded its measurement/drain deadline.')
                    if proc.poll() is not None:
                        raise RuntimeError(variant + ' died during benchmark.')
                    sample = proc_snapshot(proc.pid) | {
                        'elapsed_seconds': round(time.monotonic() - began, 3),
                        'host_load_average': os.getloadavg(),
                    }
                    samples.append(sample)
                    series.write(json.dumps(sample) + '\n')
                    time.sleep(0.5)
        finally:
            if worker.poll() is None:
                self.stop(worker)
            else:
                self.children.discard(worker)
            output.close()
            error.close()
        after = {name: proc_snapshot(server.pid) for name, (server, _) in self.servers.items()}
        data = json.loads((self.results / f'{label}.json').read_text())
        if worker.returncode or data.get('errors') or not data.get('successful'):
            print('FAILED_BENCHMARK ' + json.dumps(data), flush=True)
            raise RuntimeError(label + ' failed response validation.')
        if data.get('gzip_responses') != data['successful']:
            raise RuntimeError(label + ' did not compress every successful response.')
        for name in before:
            if before[name]['start_ticks'] != after[name]['start_ticks']:
                raise RuntimeError(name + ' PID identity changed during the measurement.')
        active_before, active_after = before[variant], after[variant]
        cpu = active_after['cpu_seconds'] - active_before['cpu_seconds']
        elapsed = time.monotonic() - began
        record = {
            'label': label, 'variant': variant, 'measured': measured,
            'benchmark': data,
            'process': {
                'window_seconds': round(elapsed, 3),
                'cpu_seconds': round(cpu, 6),
                'cpu_microseconds_per_successful_request': round(cpu * 1e6 / data['successful'], 3),
                'average_cpu_cores': round(cpu / elapsed, 4),
                'rss_start_bytes': active_before['rss_bytes'],
                'rss_end_bytes': active_after['rss_bytes'],
                'sampled_peak_rss_bytes': max(s['rss_bytes'] for s in [active_before, active_after, *samples]),
                'lifetime_peak_rss_bytes': active_after['lifetime_peak_rss_bytes'],
                'threads_end': active_after['threads'],
                'other_variant_cpu_seconds': {
                    name: round(after[name]['cpu_seconds'] - before[name]['cpu_seconds'], 6)
                    for name in before if name != variant
                },
                'notes': ['CPU window includes Node startup, synchronized start delay, request drain and final sample.',
                          'CPU time includes all app threads; RSS excludes kernel socket memory.',
                          'RSS samples are every 0.5 seconds; lifetime peak may include prior warm-up/runs.'],
            },
        }
        self.results.joinpath(label + '-result.json').write_text(json.dumps(record, indent=2))
        if measured:
            self.records.append(record)
            self.results.joinpath('measurements.json').write_text(json.dumps(self.records, indent=2))
            print('RESULT_JSON ' + json.dumps(record, separators=(',', ':')), flush=True)
            print(f"SUMMARY {label} req_s={data['successful_requests_per_second']:.2f} "
                  f"p95_ms={data['latency_ms']['p95']} p99_ms={data['latency_ms']['p99']} "
                  f"errors={data['errors']} cpu_us_req={record['process']['cpu_microseconds_per_successful_request']} "
                  f"rss_mib={active_after['rss_bytes'] / 1048576:.2f}", flush=True)
        return record

    def benchmark(self):
        self.reset_database('feed_bench')
        # A dedicated SELECT-only account makes accidental benchmark writes fail.
        # This is harness setup on the disposable CI DB, not application SQL.
        self.sql('postgres', "CREATE ROLE perf_reader LOGIN PASSWORD 'local-perf-only'", name='benchmark-role')
        self.sql('feed_bench', 'GRANT USAGE ON SCHEMA public TO perf_reader; '
                 'GRANT SELECT ON ALL TABLES IN SCHEMA public TO perf_reader', name='benchmark-grants')
        original = self.fingerprint('benchmark-before')
        try:
            for index, variant in enumerate(VARIANTS):
                self.servers[variant] = self.launch(
                    variant, 'feed_bench', ports=(3200 + index * 10, 3201 + index * 10), readonly=True)
            for variant in VARIANTS:
                print(f'WARM {variant} {WARM_SECONDS}s', flush=True)
                self.bench(variant, WARM_SECONDS, variant + '-warm')
            for round_number, order in enumerate(ORDERS, 1):
                for position, variant in enumerate(order, 1):
                    label = f'round-{round_number}-position-{position}-{variant}'
                    self.bench(variant, REFRESH_SECONDS, label + '-refresh')
                    self.bench(variant, MEASURE_SECONDS, label, measured=True)
        finally:
            for proc, _ in self.servers.values():
                self.stop(proc)
        final = self.fingerprint('benchmark-after')
        if final != original:
            raise RuntimeError('The benchmark database fingerprint changed.')
        self.results.joinpath('database-unchanged.json').write_text(json.dumps({
            'before': original, 'after': final, 'equal': True,
            'protection': 'All benchmark app connections used a SELECT-only database role.',
        }, indent=2))
        summaries = []
        for variant in VARIANTS:
            runs = [r for r in self.records if r['variant'] == variant]
            summaries.append({
                'variant': variant, 'runs': len(runs),
                'median_req_s': statistics.median(r['benchmark']['successful_requests_per_second'] for r in runs),
                'median_p95_ms': statistics.median(r['benchmark']['latency_ms']['p95'] for r in runs),
                'median_p99_ms': statistics.median(r['benchmark']['latency_ms']['p99'] for r in runs),
                'median_cpu_us_request': statistics.median(r['process']['cpu_microseconds_per_successful_request'] for r in runs),
                'median_rss_end_bytes': statistics.median(r['process']['rss_end_bytes'] for r in runs),
                'errors': sum(r['benchmark']['errors'] for r in runs),
            })
        self.results.joinpath('summary.json').write_text(json.dumps(summaries, indent=2))
        print('SUMMARY_JSON ' + json.dumps(summaries, separators=(',', ':')), flush=True)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                summary.write('## C# same-runner comparison\n\n')
                summary.write('Feed-only, gzip, 128 connections / 4 workers; three 30-second runs per variant. '
                              'Each process receives 60 seconds of initial warm-up and 5 seconds before each measured run. '
                              'Database and load generator share the runner; this is not a million-user capacity score.\n\n')
                summary.write('| Variant | Median req/s | Median p95 ms | Median p99 ms | CPU µs/request | End RSS MiB | Errors |\n')
                summary.write('|---|---:|---:|---:|---:|---:|---:|\n')
                for row in summaries:
                    summary.write(f"| {row['variant']} | {row['median_req_s']:.2f} | {row['median_p95_ms']:.2f} "
                                  f"| {row['median_p99_ms']:.2f} | {row['median_cpu_us_request']:.3f} "
                                  f"| {row['median_rss_end_bytes'] / 1048576:.2f} | {row['errors']} |\n")

    def close(self):
        for child in list(self.children):
            self.stop(child)
        for file in self.files:
            file.close()


def main():
    experiment = Experiment()
    def cancelled(signum, _frame):
        raise RuntimeError(f'Experiment interrupted by signal {signum}.')
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    try:
        experiment.build()
        experiment.validate()
        experiment.benchmark()
    except Exception as error:
        experiment.results.joinpath('failure.txt').write_text(repr(error))
        raise
    finally:
        experiment.close()


if __name__ == '__main__':
    main()
