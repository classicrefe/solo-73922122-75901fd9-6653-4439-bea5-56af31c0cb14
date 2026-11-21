"""FastAPI application: oscilloscope waveform acquisition web service."""
from __future__ import annotations

import os
import struct
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import db
from . import pwl
from .manager import AcquisitionManager

SCOPE_HOST = os.environ.get("SCOPE_HOST", "127.0.0.1")
SCOPE_PORT = int(os.environ.get("SCOPE_PORT", "5025"))
SCOPE_TIMEOUT = float(os.environ.get("SCOPE_TIMEOUT", "5"))
DB_PATH = os.environ.get("DB_PATH", "acquisitions.db")
STATIC_DIR = Path(__file__).parent / "static"

manager: AcquisitionManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    db.init(DB_PATH)
    db.sweep_interrupted()
    manager = AcquisitionManager(SCOPE_HOST, SCOPE_PORT, SCOPE_TIMEOUT)
    manager.start()
    yield
    await manager.stop()


app = FastAPI(title="Oscilloscope Acquisition", lifespan=lifespan)


class AcquireIn(BaseModel):
    channel: int = Field(..., ge=1, le=2)


class PwlIn(BaseModel):
    tolerance: float = Field(..., ge=0, allow_inf_nan=False)
    threshold: float = Field(..., allow_inf_nan=False)
    max_points: int = Field(..., ge=2)


def public(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k != "samples"}
    out["has_samples"] = row.get("samples") is not None
    return out


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "scope": f"{SCOPE_HOST}:{SCOPE_PORT}",
        "round_timeout_s": SCOPE_TIMEOUT,
    }


@app.get("/api/acquisitions")
async def list_acquisitions(limit: int = 50):
    return [public(r) for r in db.list_recent(limit)]


@app.post("/api/acquisitions", status_code=201)
async def create_acquisition(req: AcquireIn):
    aid = manager.submit(req.channel)
    return public(db.get(aid))


@app.get("/api/acquisitions/{aid}")
async def get_acquisition(aid: int):
    row = public(db.get(aid))
    if row is None:
        raise HTTPException(404, "acquisition not found")
    return row


@app.post("/api/acquisitions/{aid}/cancel")
async def cancel_acquisition(aid: int):
    row = manager.cancel(aid)
    if row is None:
        raise HTTPException(404, "acquisition not found")
    return public(row)


@app.get("/api/acquisitions/{aid}/csv")
async def acquisition_csv(aid: int):
    row = db.get(aid)
    if row is None:
        raise HTTPException(404, "acquisition not found")
    # Only a fully received block (parameters + samples stored together) may
    # be downloaded.
    if row["status"] != "done" or row["samples"] is None:
        raise HTTPException(409, "capture not complete; CSV unavailable")
    times, volts = waveform_arrays(row)
    lines = ["time_s,voltage_v"]
    append = lines.append
    for t, v in zip(times, volts):
        append(f"{t:.9e},{v:.6e}")
    filename = f"acquisition_{aid}_ch{row['channel']}.csv"
    return Response(
        "\n".join(lines) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def waveform_arrays(row: dict) -> tuple[list[float], list[float]]:
    """Original sample instants (s) and calibrated voltages (V)."""
    points = int(row["points"])
    raw = struct.unpack(f"<{points}h", row["samples"])
    dt = float(row["sample_interval"])
    scale = float(row["volt_scale"])
    offset = float(row["volt_offset"])
    times = [i * dt for i in range(points)]
    volts = [value * scale + offset for value in raw]
    return times, volts


@app.post("/api/acquisitions/{aid}/pwl")
def acquisition_pwl(aid: int, req: PwlIn):
    """Minimum-breakpoint SPICE PWL stimulus for a completed capture.

    Synchronous (runs in the threadpool): the DP over 4096 samples takes a
    fraction of a second and must not block the event loop.
    """
    row = db.get(aid)
    if row is None:
        raise HTTPException(404, "acquisition not found")
    if row["status"] != "done" or row["samples"] is None:
        raise HTTPException(409, "capture not complete; PWL unavailable")
    times, volts = waveform_arrays(row)
    indices = pwl.min_breakpoints(times, volts, req.tolerance, req.threshold)
    count = len(indices)
    out: dict = {"ok": count <= req.max_points, "points": count,
                 "max_points": req.max_points}
    if out["ok"]:
        out["pwl"] = pwl.pwl_text(times, volts, indices)
        out["filename"] = f"acquisition_{aid}_ch{row['channel']}_pwl.txt"
    return out
