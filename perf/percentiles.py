"""The median and nearest-rank p95 the in-process Hosted and registry perf probes report, in milliseconds."""

import math
import statistics


def median_p95(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
    }
