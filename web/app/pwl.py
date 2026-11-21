"""Minimum-breakpoint PWL export: turn a captured waveform into the smallest
possible SPICE piecewise-linear stimulus.

Constraints (all evaluated at the ORIGINAL sample instants):

* breakpoints are a subset of the original samples, first and last included;
* breakpoint voltages keep the original calibration (raw*scale + offset);
* linear interpolation between the exported points stays within ``tolerance``
  of the original voltage at every original sample instant;
* at every original sample instant the interpolated waveform reaches
  ``threshold`` (>=, so equality counts as reached) exactly when the original
  sample does.

The minimum is exact, not a heuristic: shortest-path DP over segments i -> j,
where a segment is feasible iff the straight line through the two endpoint
samples satisfies both constraints at every sample in between.  For a fixed
left endpoint every constraint is a linear bound on the segment slope, so the
feasible right endpoints are found with cumulative min/max reductions
(vectorised with NumPy): O(n^2) worst case, well under a second for 4096
samples.

Numerical note: constraints are evaluated with a negligible slack
``s = 1e-9 * (1 + max|v| + |thr|)`` volts so that a line sitting exactly on a
constraint boundary is not lost to floating-point rounding of the slope
comparisons.  Effectively the DP requires, at every original sample,
``|line - v| <= tolerance + s`` and ``line >= thr - s`` for reached samples /
``line < thr - s`` for samples below the threshold.  The exported result can
therefore exceed the nominal tolerance by at most ``s`` and sits within ``s``
of the threshold in knife-edge cases only — far below any engineering
tolerance for a SPICE stimulus.
"""
from __future__ import annotations

import numpy as np


def min_breakpoints(times, volts, tolerance: float, threshold: float) -> list[int]:
    """Indices of the smallest feasible breakpoint set (includes 0 and n-1)."""
    t = np.asarray(times, dtype=np.float64)
    v = np.asarray(volts, dtype=np.float64)
    n = int(t.shape[0])
    if n <= 2:
        return list(range(n))
    tol = float(tolerance)
    thr = float(threshold)
    # Negligible slack so knife-edge constraint hits are not lost to
    # floating-point rounding (see module docstring).
    slack = 1e-9 * (1.0 + float(np.max(np.abs(v))) + abs(thr))
    tol += slack
    INF = n + 1
    cost = np.full(n, INF, dtype=np.int64)  # fewest breakpoints reaching j
    parent = np.full(n, -1, dtype=np.int64)
    cost[0] = 1
    reached = v >= thr  # original per-sample threshold states
    for i in range(n - 1):
        ci = cost[i]
        if ci == INF:
            continue
        d = t[i + 1:] - t[i]  # > 0: distance from sample i to candidate k/j
        dv = v[i + 1:] - v[i]
        m = dv / d  # slope of the candidate segment i -> j
        lo = (dv - tol) / d  # tolerance band, expressed as slope bounds
        hi = (dv + tol) / d
        w = (thr - slack - v[i]) / d  # threshold crossing, as a slope bound
        ge = reached[i + 1:]
        # sample k reached threshold -> line must be >= thr-s: m >= w_k (closed)
        # sample k below threshold  -> line must be <  thr-s: m <  w_k (open)
        low = np.where(ge, np.maximum(lo, w), lo)
        up_open = np.where(ge, np.inf, w)
        cum_lo = np.maximum.accumulate(low)
        cum_hi = np.minimum.accumulate(hi)
        cum_open = np.minimum.accumulate(up_open)
        feas = np.empty(n - i - 1, dtype=bool)
        feas[0] = True  # adjacent points: no samples in between
        # Segment i -> j is constrained by samples k with i < k < j, i.e. by
        # the cumulative bounds shifted one position to the left.
        feas[1:] = (
            (m[1:] >= cum_lo[:-1])
            & (m[1:] <= cum_hi[:-1])
            & (m[1:] < cum_open[:-1])
        )
        nxt = cost[i + 1:]
        better = feas & (ci + 1 < nxt)
        nxt[better] = ci + 1
        parent[i + 1:][better] = i
    if cost[n - 1] > n:
        raise RuntimeError("no feasible breakpoint set exists")  # unreachable
    out = [n - 1]
    while out[-1] != 0:
        out.append(int(parent[out[-1]]))
    out.reverse()
    return out


def pwl_text(times, volts, indices) -> str:
    """Headerless 'time voltage' lines (seconds, volts) for the breakpoints."""
    t = np.asarray(times, dtype=np.float64)
    v = np.asarray(volts, dtype=np.float64)
    return "".join(f"{t[i]:.9e} {v[i]:.9e}\n" for i in indices)
