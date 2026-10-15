"""The percentiles the perf probes report, in milliseconds."""

import math
import statistics


def median_p95(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
    }


def nearest_rank(values: list[float]) -> dict[str, float | int]:
    """The sample count with the nearest-rank p50 and p95."""
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50_ms": round(ordered[math.ceil(len(ordered) * 0.50) - 1], 2),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2),
    }
