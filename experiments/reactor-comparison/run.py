#!/usr/bin/env python3
"""Fresh-process/fresh-database, shared-host diagnostic; never an official score."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import random
try:
    import resource
except ImportError:  # Allows pure measurement/scheduling unit tests on Windows.
    resource = None
import shutil
import signal
import statistics
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SDK = Path('/opt/million-users-dotnet-10.0.403')
BASE = '954719a0411aedf9f65884e810d2deefd8db7cdb'
AOT = '3cff0a51d19018639af2614be2308762a738133e'
JIT = 'a4af720c410b29b030fbec16fb37617c14d34d21'
TRIAL_DB = 'reactor_trial'
CPUTICKS = os.sysconf('SC_CLK_TCK') if sys.platform == 'linux' else 100


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def capture(args, *, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=check).stdout.strip()


def logged(args, path, *, cwd=None, env=None, timeout=1800, check=True):
    print('COMMAND ' + json.dumps([str(x) for x in args]), flush=True)
    with Path(path).open('w') as log:
        result = subprocess.run(args, cwd=cwd, env=env, stdout=log,
                                stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode and check:
        print(Path(path).read_text(errors='replace')[-18000:], flush=True)
        raise RuntimeError(f'Command failed ({result.returncode}); see {path}')
    return result.returncode


def sql(statement):
    return capture(['psql', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                    '-d', 'postgres', '-c', statement])


def reset_database():
    # The only database this driver ever drops. The immutable seed is never served.
    sql(f'DROP DATABASE IF EXISTS {TRIAL_DB} WITH (FORCE)')
    sql(f'CREATE DATABASE {TRIAL_DB} TEMPLATE feed_seed')
    # Do not include asynchronous clone writes from setup in the measured trial.
    sql('CHECKPOINT')


def db_stats():
    return json.loads(sql("SELECT row_to_json(s) FROM (SELECT xact_commit,xact_rollback,"
                          "blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,"
                          "tup_updated,temp_bytes,deadlocks FROM pg_stat_database "
                          f"WHERE datname='{TRIAL_DB}') s") or '{}')


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def process_stats(pid):
    """Process-wide CPU totals (all threads); do not double count thread stat files."""
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        status = {}
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            key, _, value = line.partition(':')
            status[key] = value.strip()
        return {
            'cpu_s': (int(stat[11]) + int(stat[12])) / CPUTICKS,
            'rss_bytes': int(status.get('VmRSS', '0 kB').split()[0]) * 1024,
            'rss_highwater_bytes': int(status.get('VmHWM', '0 kB').split()[0]) * 1024,
            'threads': int(status.get('Threads', '0')),
            'voluntary_context_switches': int(status.get('voluntary_ctxt_switches', '0')),
            'involuntary_context_switches': int(status.get('nonvoluntary_ctxt_switches', '0')),
        }
    except (OSError, ValueError, IndexError):
        return None


def cgroup_stats(path):
    if path is None:
        return None
    cpu = read_text(path / 'cpu.stat')
    if cpu is None:
        return None
    values = dict(line.split() for line in cpu.splitlines())
    return {'cpu_s': int(values['usage_usec']) / 1_000_000,
            'memory_bytes': int(read_text(path / 'memory.current') or 0),
            'memory_events': read_text(path / 'memory.events'),
            'cpu_pressure': read_text(path / 'cpu.pressure'),
            'io_pressure': read_text(path / 'io.pressure'),
            'io_stat': read_text(path / 'io.stat')}


def cpu_seconds(samples, role):
    values = [s[role] for s in samples if s.get(role)]
    return max(0, values[-1]['cpu_s'] - values[0]['cpu_s']) if len(values) >= 2 else None


def cpu_utilization(samples, role, cores):
    values = [s for s in samples if s.get(role)]
    elapsed = values[-1]['t'] - values[0]['t'] if len(values) >= 2 else 0
    return 100 * cpu_seconds(values, role) / elapsed / cores if elapsed > 0 else None


def trial_summary(raw, samples, *, ramp, app_cores, db_cores, generator_cores, app_alive):
    hold = [s for s in samples if s['t'] >= ramp]
    steady = [t for t in raw.get('timeline', []) if t['t'] >= ramp + 5]
    successful = raw.get('reqs', 0) - raw.get('fails', 0)
    cpu = cpu_seconds(samples, 'app')
    gen_util = cpu_utilization(hold, 'generator', generator_cores)
    db_util = cpu_utilization(hold, 'database', db_cores)
    min_conns = min((t['conns'] for t in steady), default=0)
    full_connections = bool(steady) and min_conns >= raw.get('users', 1) * .98
    limited = (gen_util is not None and gen_util >= 85) or (db_util is not None and db_util >= 85)
    # mload's pass flag alone does not prove the requested connections were reached.
    valid = app_alive and full_connections and not limited and bool(raw.get('reqs'))
    return {
        'users': raw.get('users'), 'requests': raw.get('reqs'), 'failures': raw.get('fails'),
        'requests_per_s_including_ramp': raw.get('req_per_s'),
        'error_pct': raw.get('err_pct'), 'p50_ms_including_ramp': raw.get('p50_ms'),
        'p95_ms_including_ramp': raw.get('p95_ms'), 'p99_ms_including_ramp': raw.get('p99_ms'),
        'connect_p99_ms': raw.get('connect_p99_ms'),
        'max_hold_window_p99_ms': max((t['p99_ms'] for t in steady), default=None),
        'min_hold_connections': min_conns, 'full_connection_target_reached': full_connections,
        'app_cpu_s': cpu,
        'app_cpu_us_per_successful_request': cpu * 1e6 / successful if cpu is not None and successful else None,
        'app_peak_rss_bytes': max((s['app']['rss_highwater_bytes'] for s in samples if s.get('app')), default=0),
        'generator_peak_rss_bytes': max((s['generator']['rss_highwater_bytes'] for s in samples if s.get('generator')), default=0),
        'app_hold_cpu_pct_of_allocation': cpu_utilization(hold, 'app', app_cores),
        'database_hold_cpu_pct_of_allocation': db_util,
        'generator_hold_cpu_pct_of_allocation': gen_util,
        'generator_or_database_cpu_limited': limited,
        'app_alive_after_load': app_alive, 'mload_thresholds_passed': bool(raw.get('pass')),
        'diagnostic_trial_valid': valid,
        'valid_diagnostic_pass': valid and bool(raw.get('pass')),
    }


def schedule(names, levels, rounds, seed):
    shuffled = list(names)
    random.Random(seed).shuffle(shuffled)
    result = []
    for level_index, users in enumerate(levels):
        for round_index in range(rounds):
            offset = (round_index + level_index) % len(shuffled)
            order = shuffled[offset:] + shuffled[:offset]
            for name in order:
                result.append({'variant': name, 'users': users, 'round': round_index + 1})
    return result


def extract(ref, source, destination):
    archive = subprocess.check_output(['git', 'archive', ref, source], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            relative = Path(member.name).relative_to(source)
            if not relative.parts:
                continue
            if member.issym() or member.islnk() or '..' in relative.parts:
                raise RuntimeError('Unexpected link/path in source archive')
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())


def source_hashes(folder):
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob('*')) if p.is_file()
            and not any(x in p.relative_to(folder).parts for x in ('bin', 'obj', 'out', '.native'))}


class Comparison:
    def __init__(self, args):
        self.args = args
        self.out = args.output.resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.work = Path(os.environ.get('RUNNER_TEMP', '/tmp')) / ('reactor-comparison-' + str(os.getpid()))
        self.work.mkdir()
        self.env = dict(os.environ)
        self.env.update({'PATH': str(SDK) + ':' + os.environ['PATH'], 'DOTNET_ROOT': str(SDK),
                         'DOTNET_GCHeapHardLimitPercent': '0x3c',
                         'ASPNETCORE_ENVIRONMENT': 'Production', 'PGCLIENTENCODING': 'UTF8',
                         'HOST': '0.0.0.0', 'PORT': '3000', 'EXTRA_PORT': '3001',
                         'JWT_SECRET': 'million-users-challenge'})
        auth = urllib.parse.quote(self.env.get('PGUSER', 'postgres'), safe='')
        if self.env.get('PGPASSWORD'):
            auth += ':' + urllib.parse.quote(self.env['PGPASSWORD'], safe='')
        self.env['DATABASE_URL'] = (f"postgres://{auth}@{self.env.get('PGHOST', '127.0.0.1')}:"
                                    f"{self.env.get('PGPORT', '5432')}/{TRIAL_DB}")
        self.container = os.environ['PG_CONTAINER']
        allowed = sorted(os.sched_getaffinity(0))
        if len(allowed) < 4:
            raise RuntimeError('At least four visible logical CPUs are required; use a 4-vCPU public runner')
        if args.app_cores < 1 or args.app_cores > len(allowed) - 2:
            raise RuntimeError('App CPUs must leave at least one CPU each for PostgreSQL and the generator')
        self.cpus = {'app': allowed[:args.app_cores],
                     'database': allowed[args.app_cores:-1], 'generator': allowed[-1:]}
        self.pg_group = None
        self.variants = {}
        self.results = []
        self.correctness = {}

    def prepare(self):
        # Refuse accidental edits to the workload, schema, seed, or official suite.
        capture(['git', 'diff', '--exit-code', BASE, '--', 'bench/mload', 'test', 'seed', 'schema.sql'], cwd=ROOT)
        for ref in (BASE, AOT, JIT):
            capture(['git', 'cat-file', '-e', ref + '^{commit}'], cwd=ROOT)
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        descriptor_limit = 2_097_152 if hard == resource.RLIM_INFINITY else min(hard, 2_097_152)
        resource.setrlimit(resource.RLIMIT_NOFILE, (descriptor_limit, hard))
        if resource.getrlimit(resource.RLIMIT_NOFILE)[0] < 262144:
            raise RuntimeError(f'Need a process file-descriptor limit of at least 262144; '
                               f'inherited soft={soft}, hard={hard}; '
                               f'current={resource.getrlimit(resource.RLIMIT_NOFILE)}')
        capture(['docker', 'update', '--cpuset-cpus', ','.join(map(str, self.cpus['database'])), self.container])
        pgpid = capture(['docker', 'inspect', '--format', '{{.State.Pid}}', self.container])
        for line in Path(f'/proc/{pgpid}/cgroup').read_text().splitlines():
            if line.startswith('0::'):
                self.pg_group = Path('/sys/fs/cgroup') / line[3:].lstrip('/')
        if not cgroup_stats(self.pg_group):
            raise RuntimeError('PostgreSQL cgroup v2 CPU accounting is required for bottleneck detection')
        chosen = [('reactor-epoll', None, 'submissions/csharp-epoll-wylieagab')]
        if self.args.mode != 'smoke':
            chosen = [('minimal-api', BASE, 'examples/csharp'),
                      ('kestrel-aot', AOT, 'submissions/csharp-kestrel-wylieagab'),
                      ('kestrel-jit', JIT, 'submissions/csharp-kestrel-jit-wylieagab')] + chosen
        metadata = {
            'classification': 'Shared-runner diagnostic, not an official challenge score',
            'mode': self.args.mode, 'commit': capture(['git', 'rev-parse', 'HEAD'], cwd=ROOT),
            'runner': {k: os.environ.get(k) for k in ('RUNNER_OS', 'RUNNER_ARCH', 'ImageOS', 'ImageVersion', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')},
            'uname': capture(['uname', '-a']), 'lscpu': capture(['lscpu']),
            'cpu_affinity': self.cpus, 'memory': read_text('/proc/meminfo'),
            'fd_limit': resource.getrlimit(resource.RLIMIT_NOFILE),
            'memlock_limit': resource.getrlimit(resource.RLIMIT_MEMLOCK),
            'runtime': capture([str(SDK / 'dotnet'), '--info']),
            'go': capture(['go', 'version']), 'node': capture(['node', '--version']),
            'libpq': capture(['dpkg-query', '-W', 'libpq5', 'libpq-dev', 'postgresql-client-18']),
            'postgres_image': capture(['docker', 'inspect', '--format', '{{.Image}}', self.container]),
            'postgres_config': json.loads(sql("SELECT json_object_agg(name, setting) FROM pg_settings WHERE name IN "
                "('server_version','max_connections','shared_buffers','effective_cache_size','maintenance_work_mem',"
                "'fsync','synchronous_commit','full_page_writes','wal_compression','checkpoint_timeout',"
                "'max_wal_size','min_wal_size','random_page_cost','jit','enable_memoize','ssl')")),
            'io_uring': {'disabled': read_text('/proc/sys/kernel/io_uring_disabled'),
                         'group': read_text('/proc/sys/kernel/io_uring_group'),
                         'probe_runs_as_app_user': True,
                         'process_security': [x for x in Path('/proc/self/status').read_text().splitlines() if x.startswith(('Seccomp:', 'NoNewPrivs:', 'Uid:', 'Gid:'))]},
            'network': {'targets': '127.0.0.1,127.0.0.2,127.0.0.3,127.0.0.4', 'ports': '3000,3001',
                        'ephemeral_ports': read_text('/proc/sys/net/ipv4/ip_local_port_range'),
                        'somaxconn': read_text('/proc/sys/net/core/somaxconn'),
                        'syn_backlog': read_text('/proc/sys/net/ipv4/tcp_max_syn_backlog')},
            'seed_database': 'feed_seed', 'trial_database': TRIAL_DB,
            'gc_heap_hard_limit_percent': 60, 'no_separate_warmup': True,
            'workload_sha256': source_hashes(ROOT / 'bench/mload'),
            'test_sha256': source_hashes(ROOT / 'test'), 'sources': {},
        }
        if metadata['postgres_config']['fsync'] != 'on' or metadata['postgres_config']['synchronous_commit'] != 'on':
            raise RuntimeError('Durable PostgreSQL writes are required')
        write_json(self.out / 'environment.json', metadata)
        for name, ref, source in chosen:
            folder = self.work / name
            if ref:
                extract(ref, source, folder)
            else:
                shutil.copytree(ROOT / source, folder, ignore=shutil.ignore_patterns('bin', 'obj', 'out', '.native'))
            metadata['sources'][name] = {'commit': ref or metadata['commit'], 'path': source,
                                          'sha256': source_hashes(folder)}
            self.variants[name] = {'folder': folder, 'args': []}
            if name == 'reactor-epoll':
                installer = folder / 'install-uring.sh'
                if installer.exists():
                    logged(['bash', str(installer)], self.out / 'install-uring.log', cwd=folder, env=self.env)
            logged(['bash', str(folder / 'build.sh')], self.out / f'build-{name}.log', cwd=folder, env=self.env)
            for lock in folder.rglob('packages.lock.json'):
                if not any(p in ('bin', 'obj', 'out') for p in lock.relative_to(folder).parts):
                    dest = self.out / 'locks' / name / lock.relative_to(folder)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(lock, dest)
            resolved = folder / 'obj/project.assets.json'
            if resolved.exists():
                shutil.copyfile(resolved, self.out / f'{name}-project.assets.json')
        reactor = self.variants['reactor-epoll']['folder']
        epoll_probe = logged([str(reactor / 'out/FeedApi'), '--probe-epoll'], self.out / 'epoll-probe.log',
                             cwd=reactor, env=self.env, timeout=30, check=False)
        metadata['epoll_probe'] = {'exit_code': epoll_probe,
                                   'output': (self.out / 'epoll-probe.log').read_text(errors='replace')}
        print('EPOLL_PROBE ' + json.dumps(metadata['epoll_probe']), flush=True)
        write_json(self.out / 'environment.json', metadata)
        if epoll_probe != 0:
            raise RuntimeError('Required epoll readiness/stale-event probe failed')
        probe = logged([str(reactor / 'out/FeedApi'), '--probe-io-uring'], self.out / 'io-uring-probe.log',
                       cwd=reactor, env=self.env, timeout=30, check=False)
        metadata['io_uring']['probe_exit_code'] = probe
        metadata['io_uring']['probe_output'] = (self.out / 'io-uring-probe.log').read_text(errors='replace')
        print('IO_URING_PROBE ' + json.dumps(metadata['io_uring']), flush=True)
        if probe == 0:
            self.variants['reactor-uring'] = {'folder': reactor, 'args': ['--io-uring']}
        elif probe != 77:
            raise RuntimeError('io_uring probe failed unexpectedly; unsupported systems must return 77')
        write_json(self.out / 'environment.json', metadata)
        # Always use the existing frozen black-box tests without changing assertions.
        self.extended = self.work / 'extended-regression.mjs'
        self.extended.write_bytes(subprocess.check_output(['git', 'show', AOT + ':submissions/csharp-kestrel-wylieagab/tests/extended-regression.mjs'], cwd=ROOT))
        self.mload = self.work / 'mload'
        logged(['go', 'build', '-trimpath', '-o', str(self.mload), '.'], self.out / 'mload-build.log', cwd=ROOT / 'bench/mload')

    def start(self, name, log_path):
        variant = self.variants[name]
        log = log_path.open('w')
        started = time.monotonic()
        command = ['taskset', '-c', ','.join(map(str, self.cpus['app'])),
                   'bash', str(variant['folder'] / 'start.sh'), *variant['args']]
        process = subprocess.Popen(command, cwd=variant['folder'], env=self.env,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while time.monotonic() - started < 60:
                if process.poll() is not None:
                    raise RuntimeError(f'{name} exited during startup ({process.returncode})')
                try:
                    for port in (3000, 3001):
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                            health = json.load(response)
                            if health.get('status') != 'ok' or health.get('db') != 'ok':
                                raise ValueError('Unhealthy response')
                    return process, log, time.monotonic() - started
                except (OSError, ValueError):
                    time.sleep(.1)
            raise RuntimeError(f'{name} not healthy on both ports within 60 seconds')
        except BaseException:
            self.stop(process, log)
            print(log_path.read_text(errors='replace')[-12000:])
            raise

    @staticmethod
    def stop(process, log):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        log.close()

    def verify(self):
        for name in self.variants:
            reset_database()
            folder = self.out / 'correctness' / name
            folder.mkdir(parents=True)
            process, log, startup = self.start(name, folder / 'server.log')
            result = {'startup_seconds': startup}
            try:
                result['official_exit'] = logged(['bash', str(ROOT / 'test/test.sh'), 'http://127.0.0.1:3000'],
                    folder / 'official.log', env=self.env, timeout=180, check=False)
                result['extended_exit'] = logged(['node', str(self.extended), 'http://127.0.0.1:3000', 'http://127.0.0.1:3001'],
                    folder / 'extended.log', env=self.env, timeout=180, check=False)
                result['transport_exit'] = logged([sys.executable, str(HERE / 'transport-check.py')],
                    folder / 'transport.log', env=self.env, timeout=180, check=False)
                tests = ['official', 'extended', 'transport']
                if name.startswith('reactor-'):
                    pipeline = self.variants[name]['folder'] / 'tests/pipeline-check.py'
                    result['pipeline_exit'] = logged([sys.executable, str(pipeline)],
                        folder / 'pipeline.log', env=dict(self.env, BASE_URL='http://127.0.0.1:3000'), timeout=180, check=False)
                    tests.append('pipeline')
                for test in tests:
                    print(f'CORRECTNESS_LOG {name} {test}\n' + (folder / f'{test}.log').read_text(errors='replace')[-16000:], flush=True)
                self.correctness[name] = result
                write_json(self.out / 'correctness.json', self.correctness)
                # The unchanged example is a control. Preserve its extra-test
                # failures explicitly instead of silently changing its source.
                if result['official_exit'] or (name != 'minimal-api' and (result['extended_exit'] or result['transport_exit'] or result.get('pipeline_exit', 0))):
                    raise RuntimeError(f'Correctness failed for {name}; benchmark not started')
            finally:
                self.stop(process, log)

    def trial(self, entry, index):
        name, users = entry['variant'], entry['users']
        trial_id = f'{index:03d}-{name}-{users}-r{entry["round"]}'
        folder = self.out / 'trials' / trial_id
        folder.mkdir(parents=True)
        reset_database()
        process, log, startup = self.start(name, folder / 'server.log')
        generator = None
        samples = []
        raw = {}
        ramp = max(self.args.ramp, (users + 3299) // 3300)
        hold = entry.get('hold', self.args.hold)
        before = db_stats()
        started = time.monotonic()
        command = ['taskset', '-c', ','.join(map(str, self.cpus['generator'])), str(self.mload),
                   '-target', '127.0.0.1,127.0.0.2,127.0.0.3,127.0.0.4', '-ports', '3000,3001',
                   '-users', str(users), '-ramp', f'{ramp}s', '-hold', f'{hold}s',
                   '-tokens', str(ROOT / 'seed/tokens.json'), '-gzip', '-think-scale', '2.5',
                   '-out', str(folder / 'mload.json')]
        config = {**entry, 'id': trial_id, 'ramp_s': ramp, 'hold_s': hold,
                  'startup_seconds': startup, 'command': command}
        write_json(folder / 'config.json', config)
        try:
            with (folder / 'mload.log').open('w') as loadlog, (folder / 'telemetry.jsonl').open('w') as telemetry:
                # The first CPU snapshot precedes the first request.
                app_start = process_stats(process.pid)
                db_start = cgroup_stats(self.pg_group)
                gen_env = dict(self.env, GOMAXPROCS=str(len(self.cpus['generator'])))
                generator = subprocess.Popen(command, env=gen_env, stdout=loadlog, stderr=subprocess.STDOUT, start_new_session=True)
                samples.append({'t': 0, 'app': app_start, 'database': db_start,
                                'generator': {'cpu_s': 0, 'rss_bytes': 0, 'rss_highwater_bytes': 0}})
                while True:
                    sample = {'t': time.monotonic() - started, 'app': process_stats(process.pid),
                              'generator': process_stats(generator.pid), 'database': cgroup_stats(self.pg_group),
                              'socket_memory': read_text('/proc/net/sockstat'),
                              'memory_pressure': read_text('/proc/pressure/memory'),
                              'io_pressure': read_text('/proc/pressure/io')}
                    samples.append(sample)
                    telemetry.write(json.dumps(sample) + '\n')
                    telemetry.flush()
                    if generator.poll() is not None:
                        break
                    if process.poll() is not None:
                        raise RuntimeError(f'{name} exited during load ({process.returncode})')
                    if sample['t'] > ramp + hold + 120:
                        raise RuntimeError('Load generator exceeded its trial deadline')
                    time.sleep(1)
                if generator.returncode:
                    raise RuntimeError(f'mload exited {generator.returncode}')
            raw = json.loads((folder / 'mload.json').read_text())
            summary = trial_summary(raw, samples, ramp=ramp, app_cores=len(self.cpus['app']),
                                    db_cores=len(self.cpus['database']), generator_cores=len(self.cpus['generator']),
                                    app_alive=process.poll() is None)
            summary.update(config)
            summary['database_stats_before'] = before
            summary['database_stats_after'] = db_stats()
            self.results.append(summary)
            write_json(folder / 'summary.json', summary)
            print('RAW_RESULT ' + json.dumps({'id': trial_id, 'result': raw}, separators=(',', ':')), flush=True)
            print('SUMMARY ' + json.dumps(summary, separators=(',', ':')), flush=True)
        finally:
            if generator is not None and generator.poll() is None:
                os.killpg(generator.pid, signal.SIGTERM)
                try:
                    generator.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(generator.pid, signal.SIGKILL)
                    generator.wait(timeout=5)
            self.stop(process, log)
            write_json(self.out / 'results.json', self.results)
        return self.results[-1]

    def report(self):
        lines = ['# Shared-runner diagnostic', '',
                 'These results are not official challenge scores. CPU partitions, loopback traffic, co-hosted PostgreSQL, reduced database memory, and shared VM variability limit their interpretation.', '',
                 '| Variant | Users | Trials | Valid passes | Median p99 ms | p99 range ms | Median app CPU us/success | Peak app RSS MiB |',
                 '|---|---:|---:|---:|---:|---:|---:|---:|']
        for name in self.variants:
            for users in sorted({r['users'] for r in self.results}):
                rows = [r for r in self.results if r['variant'] == name and r['users'] == users and not r.get('confirmation')]
                if not rows:
                    continue
                p99 = [r['p99_ms_including_ramp'] for r in rows]
                cpu = [r['app_cpu_us_per_successful_request'] for r in rows if r['app_cpu_us_per_successful_request'] is not None]
                lines.append(f'| {name} | {users} | {len(rows)} | {sum(r["valid_diagnostic_pass"] for r in rows)} | '
                             f'{statistics.median(p99):.1f} | {min(p99):.1f}–{max(p99):.1f} | '
                             f'{statistics.median(cpu) if cpu else float("nan"):.1f} | {max(r["app_peak_rss_bytes"] for r in rows)/1048576:.1f} |')
        lines += ['', 'Inspect individual trial validity, errors, endpoint histograms, steady-state connection counts, and database/generator CPU before drawing performance conclusions. No winner is automatically selected. Overall percentiles include the ramp, as in the unchanged official generator. Window percentiles are retained separately and never averaged into an aggregate percentile.', '']
        report = '\n'.join(lines)
        (self.out / 'report.md').write_text(report)
        print(report, flush=True)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as file:
                file.write(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'reactor-results')
    parser.add_argument('--mode', choices=('smoke', 'full', 'capacity'))
    parser.add_argument('--levels', help='Override comma-separated concurrent-user levels')
    parser.add_argument('--rounds', type=int)
    parser.add_argument('--app-cores', type=int, default=2, help='Default exercises two reactor workers; remaining non-generator CPUs go to PostgreSQL')
    parser.add_argument('--ramp', type=int, default=60)
    parser.add_argument('--hold', type=int)
    parser.add_argument('--seed', type=int, default=20261031)
    parser.add_argument('--confirm', action='store_true', help='Fresh 60-second ramp + 5-minute confirmation at each variant\'s highest all-round valid pass')
    parser.add_argument('--allow-disposable-database', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'linux' or not args.allow_disposable_database:
        parser.error('Linux and explicit --allow-disposable-database are required')
    args.mode = args.mode or (HERE / 'mode.txt').read_text().strip()
    if args.mode not in ('smoke', 'full', 'capacity'):
        parser.error('mode.txt must be smoke, full, or capacity')
    args.rounds = args.rounds or (1 if args.mode == 'smoke' else 3)
    args.hold = args.hold or (60 if args.mode == 'smoke' else 120)
    levels = [int(x) for x in args.levels.split(',')] if args.levels else ([15000] if args.mode == 'smoke' else [30000, 80000] if args.mode == 'full' else [10000, 30000, 100000])
    if not levels or min(levels) < 1 or max(levels) > 100000 or args.rounds < 1 or args.ramp < 1 or args.hold < 1:
        parser.error('Use positive timings/rounds and 1–100000 users')
    compare = Comparison(args)
    try:
        compare.prepare()
        compare.verify()
        plan = schedule(compare.variants, levels, args.rounds, args.seed)
        write_json(compare.out / 'schedule.json', plan)
        print('SCHEDULE ' + json.dumps(plan), flush=True)
        for index, entry in enumerate(plan, 1):
            compare.trial(entry, index)
        if args.confirm:
            for name in compare.variants:
                passed = [level for level in levels if sum(r['valid_diagnostic_pass'] for r in compare.results if r['variant'] == name and r['users'] == level) == args.rounds]
                if passed:
                    compare.trial({'variant': name, 'users': max(passed), 'round': 0, 'hold': 300, 'confirmation': True}, len(compare.results) + 1)
        compare.report()
    except BaseException as error:
        write_json(compare.out / 'failure.json', {'type': type(error).__name__, 'error': str(error), 'traceback': traceback.format_exc()})
        compare.report()
        raise


if __name__ == '__main__':
    main()
