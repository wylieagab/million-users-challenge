using System.Runtime.InteropServices;

namespace MillionReactor;

public readonly record struct PollEvent(int FileDescriptor, uint Events);
public interface IEventPoller : IDisposable
{
    void Watch(int fd, uint events);
    void Remove(int fd);
    int Wait(Span<PollEvent> events, int timeoutMs);
}

internal sealed unsafe class EpollPoller : IEventPoller
{
    private readonly int handle = Linux.Check(Linux.epoll_create1(0x80000), "epoll_create1");
    private readonly Dictionary<int, uint> watched = new();
    public void Watch(int fd, uint events)
    {
        var exists = watched.TryGetValue(fd, out uint previous);
        if (exists && previous == events) return;
        var ev = new Linux.EpollEvent { Events = events, Data = (uint)fd };
        Linux.Check(Linux.epoll_ctl(handle, exists ? 3 : 1, fd, &ev), "epoll_ctl");
        watched[fd] = events;
    }
    public void Remove(int fd)
    {
        if (watched.Remove(fd)) Linux.epoll_ctl(handle, 2, fd, null);
    }
    public int Wait(Span<PollEvent> events, int timeoutMs)
    {
        Span<Linux.EpollEvent> native = stackalloc Linux.EpollEvent[Math.Min(events.Length, 512)];
        int count;
        fixed (Linux.EpollEvent* p = native) count = Linux.epoll_wait(handle, p, native.Length, timeoutMs);
        if (count < 0 && Marshal.GetLastPInvokeError() == 4) return 0;
        Linux.Check(count, "epoll_wait");
        for (int i = 0; i < count; i++) events[i] = new((int)native[i].Data, native[i].Events);
        return count;
    }
    public void Dispose() => Linux.close(handle);
}
