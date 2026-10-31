using MillionReactor;

if (!OperatingSystem.IsLinux()) throw new PlatformNotSupportedException("Run this reactor on Linux x86_64.");
if (args.Contains("--probe-io-uring", StringComparer.Ordinal))
{
    try { UringPoller.Probe(); Console.WriteLine("io_uring poll available"); return; }
    catch (UringUnavailableException ex) { Console.Error.WriteLine(ex.Message); Environment.Exit(77); return; }
}
static string Env(string name, string? fallback = null) => Environment.GetEnvironmentVariable(name) is { Length: > 0 } value
    ? value : fallback ?? throw new InvalidOperationException($"Missing {name}");
var uring = args.Contains("--io-uring", StringComparer.Ordinal);
var workers = Math.Clamp(Environment.ProcessorCount, 1, 64);
var key = System.Text.Encoding.UTF8.GetBytes(Env("JWT_SECRET"));
var database = Env("DATABASE_URL");
var host = Env("HOST", "127.0.0.1");
int[] ports = [int.Parse(Env("PORT", "3000")), int.Parse(Env("EXTRA_PORT", "3001"))];
using var stopping = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; stopping.Cancel(); };
using var termination = System.Runtime.InteropServices.PosixSignalRegistration.Create(System.Runtime.InteropServices.PosixSignal.SIGTERM,
    ctx => { ctx.Cancel = true; stopping.Cancel(); });
Console.WriteLine($"reactor={(uring ? "io_uring-poll" : "epoll")} workers={workers} db_per_worker=6 pipeline_depth=32");
var threads = new Thread[workers];
for (int i = 0; i < workers; i++)
{
    int worker = i;
    threads[i] = new Thread(() =>
    {
        try { using var loop = new Worker(worker, workers, database, host, ports, key, uring); loop.Run(stopping.Token); }
        catch (Exception e) { Console.Error.WriteLine($"worker={worker} fatal={e}"); Environment.Exit(1); }
    }) { Name = $"reactor-{i}" };
    threads[i].Start();
}
foreach (var thread in threads) thread.Join();
