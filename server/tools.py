"""High-level tool implementations and JSON-schemas for the NEVINA MCP.

Two tools in v1:

* ``nevina_delineate`` — delineate the upstream catchment at a point and
  return the full NEVINA parameter set (area, runoff normals, elevation
  bands, land cover, climate).
* ``nevina_compare_to_engine`` — same delineation, plus an explicit
  drift comparison against an engine-claimed area. This is the
  G1-validation workflow boiled down to one call.

Both tools accept WGS84 (lng, lat) by default and convert to UTM33
internally. UTM33 (x, y) is also accepted directly for callers that
already have NEVINA-native coordinates.
"""

from __future__ import annotations

from typing import Any

from .nevina_client import CatchmentResult, NevinaClient


# ═══════════════════════════════════════════════════════════════════════
# Verdict thresholds
# ═══════════════════════════════════════════════════════════════════════
#
# Calibrated against the forensic-audit 2026-05-22 G1 findings:
#   median G1 drift on prod was 36 %, P90 1383 %.
# The "agree" band is intentionally tight (5 %) so the tool flags any
# drift large enough to matter for investment decisions. Minor 5-20 %
# is "drift but recoverable" — proportional rescale territory. Major
# >20 % is "the engine has the wrong catchment" — usually a Phase J
# grid-snap aggregation into a downstream main river.

VERDICT_AGREE_PCT = 5.0
VERDICT_MINOR_PCT = 20.0

# NEVINA snaps the user-supplied point to the nearest river segment.
# Distances beyond this threshold mean the comparison is noisy — the
# engine may have snapped to a different segment, so an "agree" or
# "drift" verdict here says less about model accuracy than about
# coordinate precision.
SNAP_SUSPECT_THRESHOLD_M = 50.0


def _verdict_for_drift(drift_pct: float) -> str:
    abs_drift = abs(drift_pct)
    if abs_drift < VERDICT_AGREE_PCT:
        return "agree"
    if abs_drift < VERDICT_MINOR_PCT:
        return "drift_minor"
    return "drift_major"


# ═══════════════════════════════════════════════════════════════════════
# Shared input schema fragments
# ═══════════════════════════════════════════════════════════════════════

_COORD_FIELDS = {
    "lng_wgs84": {
        "type": "number",
        "description": "Longitude in WGS84 (EPSG:4326). Required unless x_utm33/y_utm33 are given.",
    },
    "lat_wgs84": {
        "type": "number",
        "description": "Latitude in WGS84 (EPSG:4326). Required unless x_utm33/y_utm33 are given.",
    },
    "x_utm33": {
        "type": "number",
        "description": "Easting in UTM33 / EPSG:32633 (NEVINA's native CRS). Alternative to WGS84.",
    },
    "y_utm33": {
        "type": "number",
        "description": "Northing in UTM33 / EPSG:32633. Alternative to WGS84.",
    },
}


# ═══════════════════════════════════════════════════════════════════════
# Tool: nevina_delineate
# ═══════════════════════════════════════════════════════════════════════

DELINEATE_SCHEMA = {
    "type": "object",
    "properties": {
        **_COORD_FIELDS,
        "include_polygon": {
            "type": "boolean",
            "description": "If true, return the catchment polygon as GeoJSON. Off by default to keep payloads small.",
            "default": False,
        },
    },
    "anyOf": [
        {"required": ["lng_wgs84", "lat_wgs84"]},
        {"required": ["x_utm33", "y_utm33"]},
    ],
    "additionalProperties": False,
}


async def delineate_tool(
    client: NevinaClient,
    *,
    lng_wgs84: float | None = None,
    lat_wgs84: float | None = None,
    x_utm33: float | None = None,
    y_utm33: float | None = None,
    include_polygon: bool = False,
) -> dict[str, Any]:
    result = await client.delineate(
        lng_wgs84=lng_wgs84,
        lat_wgs84=lat_wgs84,
        x_utm33=x_utm33,
        y_utm33=y_utm33,
        include_polygon=include_polygon,
    )
    return _result_to_dict(result, include_polygon=include_polygon)


# ═══════════════════════════════════════════════════════════════════════
# Tool: nevina_compare_to_engine
# ═══════════════════════════════════════════════════════════════════════

COMPARE_SCHEMA = {
    "type": "object",
    "properties": {
        **_COORD_FIELDS,
        "engine_area_km2": {
            "type": "number",
            "description": "The catchment area in km² that the engine claims for this point (typically drainage_area_km2 or intake_catchment_area_km2 from the screening DB).",
        },
        "include_polygon": {
            "type": "boolean",
            "description": "Include NEVINA's polygon (GeoJSON) in the response. Off by default.",
            "default": False,
        },
    },
    "anyOf": [
        {"required": ["lng_wgs84", "lat_wgs84", "engine_area_km2"]},
        {"required": ["x_utm33", "y_utm33", "engine_area_km2"]},
    ],
    "additionalProperties": False,
}


async def compare_tool(
    client: NevinaClient,
    *,
    engine_area_km2: float,
    lng_wgs84: float | None = None,
    lat_wgs84: float | None = None,
    x_utm33: float | None = None,
    y_utm33: float | None = None,
    include_polygon: bool = False,
) -> dict[str, Any]:
    result = await client.delineate(
        lng_wgs84=lng_wgs84,
        lat_wgs84=lat_wgs84,
        x_utm33=x_utm33,
        y_utm33=y_utm33,
        include_polygon=include_polygon,
    )
    nevina_km2 = result.area_km2
    if nevina_km2 <= 0:
        return {
            "error": "NEVINA returned a non-positive area; cannot compare.",
            "nevina": _result_to_dict(result, include_polygon=include_polygon),
        }
    ratio = engine_area_km2 / nevina_km2
    drift_pct = (engine_area_km2 - nevina_km2) / nevina_km2 * 100.0
    verdict = _verdict_for_drift(drift_pct)

    snap_suspect = (
        result.snap_distance_m is not None
        and result.snap_distance_m > SNAP_SUSPECT_THRESHOLD_M
    )

    return {
        "guid": result.guid,
        "nevina_km2": nevina_km2,
        "engine_km2": engine_area_km2,
        "ratio": ratio,
        "drift_pct": drift_pct,
        "verdict": verdict,
        "verdict_thresholds_pct": {
            "agree": VERDICT_AGREE_PCT,
            "minor": VERDICT_MINOR_PCT,
        },
        "snap_distance_m": result.snap_distance_m,
        "snap_suspect": snap_suspect,
        "snap_suspect_threshold_m": SNAP_SUSPECT_THRESHOLD_M,
        "polygon": result.polygon if include_polygon else None,
        # Pass through a few headline NEVINA fields for context — the
        # full parameter set is available via nevina_delineate when needed.
        "nevina_summary": _summary_fields(result.parameters),
    }


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _result_to_dict(
    result: CatchmentResult, *, include_polygon: bool
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "guid": result.guid,
        "area_km2": result.area_km2,
        "snap_distance_m": result.snap_distance_m,
        "parameters": result.parameters,
    }
    if include_polygon:
        out["polygon"] = result.polygon
    return out


# Fields surfaced as a quick-look summary on the compare endpoint. Names
# come from NEVINA's MapServer/4 schema (verified 2026-05-23).
_SUMMARY_KEYS = (
    "vassdragsNr",
    "ELVENAVN",
    "elvenavn",
    "fylkeNavn",
    "QNormal9120_lskm2",
    "QNormal6190_lskm2",
    "QNormal9120_mmAar",
    "heightMin",
    "heightMax",
    "sjoProsent",
    "breProsent",
)


def _summary_fields(parameters: dict[str, Any]) -> dict[str, Any]:
    """Return a tight subset of NEVINA fields for the compare response.

    Layer-4 attribute names use mixed casing; tolerant lookup with the
    keys we care about, skipping any that are absent on this row."""
    out: dict[str, Any] = {}
    for key in _SUMMARY_KEYS:
        if key in parameters and parameters[key] is not None:
            out[key] = parameters[key]
    return out


# ═══════════════════════════════════════════════════════════════════════
# Exposed registry
# ═══════════════════════════════════════════════════════════════════════

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "nevina_delineate": DELINEATE_SCHEMA,
    "nevina_compare_to_engine": COMPARE_SCHEMA,
}
