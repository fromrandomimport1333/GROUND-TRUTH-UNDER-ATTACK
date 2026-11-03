"""Explicit timing statistics and process resource observations without new dependencies."""
import math
import os
import platform
import time
import tracemalloc


def summarize_ns(samples):
    values=list(samples)
    if not values or any(type(v) is not int or v<0 for v in values):
        raise ValueError('nonempty nonnegative integer nanosecond observations required')
    values.sort()
    percentile=lambda q: values[max(0,math.ceil(q*len(values))-1)]/1e6
    return {'samples':len(values),'mean_ms':sum(values)/len(values)/1e6,
            'min_ms':values[0]/1e6,'max_ms':values[-1]/1e6,
            'p50_ms':percentile(.50),'p95_ms':percentile(.95),'p99_ms':percentile(.99),
            'percentile_method':'nearest_rank'}


def measure(operation, *, iterations, series, warmup):
    for _ in range(warmup): operation()
    groups=[];pooled=[]
    for _ in range(series):
        samples=[]
        for _ in range(iterations):
            start=time.perf_counter_ns();operation();samples.append(time.perf_counter_ns()-start)
        groups.append(summarize_ns(samples));pooled.extend(samples)
    return {'series':groups,'pooled':summarize_ns(pooled)}


def memory_snapshot():
    """Current RSS and lifetime peak where supported. No peak-minus-peak fiction."""
    try:
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),
                    ('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),
                    ('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t)]
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.GetCurrentProcess.restype=wintypes.HANDLE
            psapi=ctypes.WinDLL('psapi',use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype=wintypes.BOOL
            counters=Counters();counters.cb=ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
                raise OSError('GetProcessMemoryInfo failed')
            return {'rss_bytes':counters.WorkingSetSize,'lifetime_peak_rss_bytes':counters.PeakWorkingSetSize,'method':'Windows process memory counters'}
        if platform.system()=='Linux':
            import resource
            from pathlib import Path
            rss=int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
            return {'rss_bytes':rss,'lifetime_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,'method':'proc statm and getrusage'}
    except (OSError,ValueError,IndexError,AttributeError):
        return {'rss_bytes':None,'lifetime_peak_rss_bytes':None,'method':'unavailable'}
    return {'rss_bytes':None,'lifetime_peak_rss_bytes':None,'method':'unsupported platform'}


def resources(operation, *, iterations):
    if tracemalloc.is_tracing():
        raise RuntimeError('resource measurement requires exclusive ownership of tracemalloc')
    before=memory_snapshot()
    tracemalloc.start()
    start=time.perf_counter();cpu=time.process_time()
    try:
        for _ in range(iterations): operation()
        cpu_seconds=time.process_time()-cpu;wall_seconds=time.perf_counter()-start
        current,peak=tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {'iterations':iterations,'wall_seconds':wall_seconds,'process_cpu_seconds':cpu_seconds,
            'cpu_percent_one_core':100*cpu_seconds/wall_seconds if wall_seconds else None,
            'python_traced_current_bytes':current,'python_traced_peak_bytes':peak,
            'memory_before':before,'memory_after':memory_snapshot(),
            'scope':'Python process only; broker process excluded; tracemalloc enabled for this separate resource pass'}
