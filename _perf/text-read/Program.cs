using System.Buffers;
using System.Diagnostics;
using System.Globalization;
using System.Runtime;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using Npgsql;
using MillionUsers;

internal static class Program
{
    // Exact challenge read query; no casts, JSON in SQL, schema changes, or writes.
    private const string FeedSql = "SELECT p.id, p.body, p.created_at, u.username, (SELECT count(*) FROM likes l WHERE l.post_id = p.id)::int AS like_count FROM posts p JOIN users u ON u.id = p.user_id ORDER BY p.created_at DESC, p.id DESC LIMIT 20";
    private static readonly ReadPath[] Paths = Enum.GetValues<ReadPath>();
    private static long sink;

    private static void Main(string[] args)
    {
        string mode = Option(args, "--mode", "validate");
        int iterations = PositiveInt(args, "--iterations", 1000);
        int rounds = PositiveInt(args, "--rounds", 5);
        int warmup = PositiveInt(args, "--warmup", 100);
        if (mode is not ("validate" or "buffered" or "feed" or "all"))
            throw new ArgumentException("--mode must be validate, buffered, feed, or all");
        string connectionString = Environment.GetEnvironmentVariable("PERF_CONNECTION")
            ?? "Host=127.0.0.1;Port=55432;Username=postgres;Database=feed_perf_20261029";
        var settings = new NpgsqlConnectionStringBuilder(connectionString)
        {
            ApplicationName = "isolated-text-read-experiment",
            Enlist = false,
            NoResetOnClose = true,
            MaxPoolSize = 1,
            MaxAutoPrepare = 8,
            AutoPrepareMinUsages = 2,
            ConnectionLifetime = 0,
            GssEncryptionMode = GssEncryptionMode.Disable,
            Timeout = 5,
            CommandTimeout = 10,
            ClientEncoding = "UTF8"
        };
        var builder = new NpgsqlSlimDataSourceBuilder(settings.ConnectionString);
        builder.EnableTransportSecurity();
        using var dataSource = builder.Build();
        using var connection = dataSource.OpenConnection();
        Console.WriteLine($"runtime={Environment.Version} npgsql={typeof(NpgsqlConnection).Assembly.GetName().Version} server_gc={GCSettings.IsServerGC} cores={Environment.ProcessorCount}");
        Console.WriteLine("Diagnostic only. Buffered mode repeats one held row; feed mode executes the exact 20-row query per iteration. Neither mode is an HTTP benchmark.");
        Validate(connection);
        if (mode == "validate") return;
        Console.WriteLine("mode,path,round,feeds,elapsed_ms,ns_per_feed,current_thread_bytes_per_feed,total_managed_bytes_per_feed,gen0,gen1,gen2,json_bytes_per_feed");
        if (mode is "buffered" or "all") RunBuffered(connection, iterations, rounds, warmup);
        if (mode is "feed" or "all") RunFeed(connection, iterations, rounds, warmup);
        Console.WriteLine($"checksum={sink}");
    }

    private static string Option(string[] args, string name, string fallback)
    {
        int at = Array.IndexOf(args, name);
        if (at < 0) return fallback;
        if (at + 1 == args.Length) throw new ArgumentException($"Missing value for {name}");
        return args[at + 1];
    }
    private static int PositiveInt(string[] args, string name, int fallback)
    {
        int value = int.Parse(Option(args, name, fallback.ToString(CultureInfo.InvariantCulture)), CultureInfo.InvariantCulture);
        return value > 0 ? value : throw new ArgumentException($"{name} must be positive");
    }

    private static void Validate(NpgsqlConnection connection)
    {
        int rows = 0;
        var writers = Paths.Select(path => new FeedWriter(path)).ToArray();
        try
        {
            foreach (var writer in writers) writer.Begin();
            using (var command = new NpgsqlCommand(FeedSql, connection))
            using (var reader = command.ExecuteReader())
            {
                while (reader.Read())
                {
                    foreach (var writer in writers) writer.Add(reader);
                    rows++;
                }
            }
            foreach (var writer in writers) writer.End();
            EqualOutputs("actual feed", writers);
            if (rows != 20) throw new InvalidOperationException($"Expected seeded 20-row feed, got {rows}");
            Console.WriteLine($"validated actual feed: {rows} rows, {writers[0].Written.Length} exact matching bytes across {writers.Length} paths");

            var examples = new (string Name, string Body, string Author)[]
            {
                ("ascii", "plain ASCII <>&/", "user_1"),
                ("empty strings", "", ""),
                ("500 ASCII", new string('a', 500), "author"),
                ("500 astral scalars", string.Concat(Enumerable.Repeat("😀", 500)), "日本語 😀 café"),
                ("500 BMP scalars", new string('界', 500), "Δοκιμή"),
                ("mixed boundary", new string('b', 499) + "😀", "mix"),
                ("JSON escapes", "quote \" slash \\ tab\t newline\n return\r backspace\b formfeed\f control\u0001", "a\"b\\c\n"),
                ("Unicode separators", "\u2028\u2029\u0085 e\u0301 café", "\u2028\u2029"),
                ("author at byte buffer boundary", "short", new string('x', 2048)),
                ("author above byte buffer boundary", "short", new string('x', 2049)),
                ("author grows repeatedly", "short", string.Concat(Enumerable.Repeat("😀", 5000))),
                ("emoji crossing char buffer boundary", "short", new string('z', 1023) + "😀" + new string('z', 1024))
            };
            DateTime timestamp = new DateTime(2025, 12, 31, 23, 59, 19, DateTimeKind.Utc).AddTicks(4099990);
            // These parameter-only SELECTs test public reader behavior without modifying tables.
            using var values = new NpgsqlCommand("SELECT $1::bigint, $2::text, $3::timestamptz, $4::text, $5::int", connection);
            values.Parameters.Add(new NpgsqlParameter<long> { TypedValue = 17 });
            var body = new NpgsqlParameter<string>(); values.Parameters.Add(body);
            values.Parameters.Add(new NpgsqlParameter<DateTime> { TypedValue = timestamp });
            var author = new NpgsqlParameter<string>(); values.Parameters.Add(author);
            values.Parameters.Add(new NpgsqlParameter<int> { TypedValue = 9 });
            foreach (var example in examples)
            {
                body.TypedValue = example.Body;
                author.TypedValue = example.Author;
                foreach (var writer in writers) writer.Begin();
                using (var reader = values.ExecuteReader())
                {
                    if (!reader.Read()) throw new InvalidOperationException("Missing validation row");
                    foreach (var writer in writers) writer.Add(reader);
                    if (reader.Read()) throw new InvalidOperationException("Extra validation row");
                }
                foreach (var writer in writers) writer.End();
                EqualOutputs(example.Name, writers);
                using var document = JsonDocument.Parse(writers[0].Written);
                var post = document.RootElement.GetProperty("posts")[0];
                if (post.GetProperty("body").GetString() != example.Body ||
                    post.GetProperty("author").GetString() != example.Author ||
                    post.GetProperty("created_at").GetString() != "2025-12-31T23:59:19.409Z")
                    throw new InvalidOperationException($"Round-trip mismatch: {example.Name}");
            }
            Console.WriteLine($"validated {examples.Length} parameter-only Unicode/escaping/size cases; no database writes; NUL and invalid Unicode cannot be PostgreSQL TEXT");
        }
        finally { foreach (var writer in writers) writer.Dispose(); }
    }

    private static void EqualOutputs(string label, FeedWriter[] writers)
    {
        foreach (var writer in writers.Skip(1))
            if (!writers[0].Written.Span.SequenceEqual(writer.Written.Span))
                throw new InvalidOperationException($"Byte mismatch for {label}, {writer.Path}");
    }

    private static void RunBuffered(NpgsqlConnection connection, int iterations, int rounds, int warmup)
    {
        using var command = new NpgsqlCommand(FeedSql, connection);
        using var reader = command.ExecuteReader();
        if (!reader.Read()) throw new InvalidOperationException("Empty feed");
        Console.WriteLine($"# buffered sample id={reader.GetInt64(0)}, body_utf8_bytes={reader.GetBytes(1, 0, null, 0, 0)}, author_utf8_bytes={reader.GetBytes(3, 0, null, 0, 0)}");
        for (int round = 0; round < rounds; round++)
        for (int offset = 0; offset < Paths.Length; offset++)
        {
            ReadPath path = Paths[(offset + round) % Paths.Length];
            using var writer = new FeedWriter(path);
            for (int i = 0; i < warmup; i++) BufferedFeed(reader, writer);
            Measure("buffered20_same_row", path, round, iterations, () => BufferedFeed(reader, writer));
        }
        while (reader.Read()) { } // Drain the single real query; do not retain data across real requests.
    }

    private static int BufferedFeed(NpgsqlDataReader reader, FeedWriter writer)
    {
        writer.Begin();
        for (int row = 0; row < 20; row++) writer.Add(reader);
        writer.End();
        sink += writer.Written.Span[writer.Written.Length - 2];
        return writer.Written.Length;
    }

    private static void RunFeed(NpgsqlConnection connection, int iterations, int rounds, int warmup)
    {
        // Reusing the command intentionally isolates row reading/serialization. It does
        // not measure the production per-request command construction or pool checkout.
        using var command = new NpgsqlCommand(FeedSql, connection);
        command.Prepare();
        for (int round = 0; round < rounds; round++)
        for (int offset = 0; offset < Paths.Length; offset++)
        {
            ReadPath path = Paths[(offset + round) % Paths.Length];
            using var writer = new FeedWriter(path);
            for (int i = 0; i < warmup; i++) QueryFeed(command, writer);
            Measure("query20", path, round, iterations, () => QueryFeed(command, writer));
        }
    }

    private static int QueryFeed(NpgsqlCommand command, FeedWriter writer)
    {
        writer.Begin();
        using (var reader = command.ExecuteReader())
            while (reader.Read()) writer.Add(reader);
        writer.End();
        sink += writer.Written.Span[writer.Written.Length - 2];
        return writer.Written.Length;
    }

    private static void Measure(string mode, ReadPath path, int round, int iterations, Func<int> operation)
    {
        int gen0 = GC.CollectionCount(0), gen1 = GC.CollectionCount(1), gen2 = GC.CollectionCount(2);
        long localBefore = GC.GetAllocatedBytesForCurrentThread();
        long totalBefore = GC.GetTotalAllocatedBytes(precise: true);
        long start = Stopwatch.GetTimestamp();
        long lengths = 0;
        for (int i = 0; i < iterations; i++) lengths += operation();
        double seconds = Stopwatch.GetElapsedTime(start).TotalSeconds;
        long localBytes = GC.GetAllocatedBytesForCurrentThread() - localBefore;
        long totalBytes = GC.GetTotalAllocatedBytes(precise: true) - totalBefore;
        Console.WriteLine(FormattableString.Invariant($"{mode},{path},{round + 1},{iterations},{seconds * 1000:F3},{seconds * 1e9 / iterations:F1},{(double)localBytes / iterations:F1},{(double)totalBytes / iterations:F1},{GC.CollectionCount(0) - gen0},{GC.CollectionCount(1) - gen1},{GC.CollectionCount(2) - gen2},{(double)lengths / iterations:F1}"));
    }
}

internal enum ReadPath { GetString, GetChars, GetBytesUtf8 }

internal sealed class FeedWriter : IDisposable
{
    private readonly ReadPath path;
    private readonly BaselineJsonBuffer? baseline;
    private readonly Utf8JsonBuffer? utf8;
    private readonly ArrayBufferWriter<byte>? buffer;
    private readonly Utf8JsonWriter json;
    private char[] chars = [];
    private int charsUsed;
    public ReadPath Path => path;
    public ReadOnlyMemory<byte> Written => baseline is not null ? baseline.Written :
        utf8 is not null ? utf8.Written : buffer!.WrittenMemory;

    public FeedWriter(ReadPath path)
    {
        this.path = path;
        switch (path)
        {
            case ReadPath.GetString:
                baseline = new BaselineJsonBuffer();
                json = baseline.Writer;
                break;
            case ReadPath.GetBytesUtf8:
                utf8 = new Utf8JsonBuffer();
                json = utf8.Writer;
                break;
            default:
                buffer = new ArrayBufferWriter<byte>(4096);
                chars = new char[1024];
                json = new Utf8JsonWriter(buffer, new JsonWriterOptions
                {
                    Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
                    SkipValidation = true
                });
                break;
        }
    }
    public void Begin()
    {
        // Baseline and candidate are exact source copies with only the class name
        // changed. Their Reset routines are included in every measured iteration.
        if (baseline is not null) baseline.Reset();
        else if (utf8 is not null) utf8.Reset();
        else
        {
            buffer!.Clear();
            chars.AsSpan(0, charsUsed).Clear();
            charsUsed = 0;
            json.Reset(buffer);
        }
        json.WriteStartObject();
        json.WriteStartArray("posts"u8);
    }
    public void End()
    {
        json.WriteEndArray();
        json.WriteEndObject();
        json.Flush();
    }
    public void Add(NpgsqlDataReader reader)
    {
        if (baseline is not null) { baseline.Post(reader); return; }
        if (utf8 is not null) { utf8.Post(reader); return; }
        json.WriteStartObject();
        json.WriteNumber("id"u8, reader.GetInt64(0));
        WriteChars(reader, 1, "body"u8);
        DateTime created = reader.GetDateTime(2);
        Span<char> timestamp = stackalloc char[24];
        created.TryFormat(timestamp, out int length, "yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture);
        json.WriteString("created_at"u8, timestamp[..length]);
        WriteChars(reader, 3, "author"u8);
        json.WriteNumber("like_count"u8, reader.GetInt32(4));
        json.WriteEndObject();
    }
    private void WriteChars(NpgsqlDataReader reader, int ordinal, ReadOnlySpan<byte> property)
    {
        int count = 0;
        while (true)
        {
            int room = chars.Length - count;
            int read = checked((int)reader.GetChars(ordinal, count, chars, count, room));
            count += read;
            charsUsed = Math.Max(charsUsed, count);
            if (read < room) break;
            var bigger = new char[checked(chars.Length * 2)];
            chars.CopyTo(bigger, 0);
            chars.AsSpan(0, charsUsed).Clear();
            chars = bigger;
        }
        json.WriteString(property, chars.AsSpan(0, count));
    }
    public void Dispose() => json.Dispose();
}
