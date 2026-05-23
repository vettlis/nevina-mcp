"""HTTP client for NVE's NEVINA4 watershed-delineation service.

The NEVINA4 workflow is three steps:

1. ``GenNedborFelt`` (async ArcGIS GP task): submit a point in UTM33,
   poll until the job completes, harvest a GUID.
2. ``GenNedborFeltParams`` (async ArcGIS GP task): submit the GUID,
   poll until the job completes. This populates NEVINA's feature layer
   with the catchment's parameters; the task itself returns nothing.
3. ``MapServer/4`` feature query (``WHERE guID='<guid>'``): pulls every
   attribute the layer holds (341 fields) — area, runoff normals,
   elevation bands, land cover, climate. Optionally the polygon
   geometry as GeoJSON.

The client is purely async (``httpx.AsyncClient``) and stateless. Each
call to :meth:`NevinaClient.delineate` runs the full three-step pipeline
end to end.

Endpoint sources: NEVINA4 spec provided by the user, verified against
the live service metadata on 2026-05-23
(``gis3.nve.no/arcgis/rest/services/.../Nevina4/...``).

No authentication is required — these are open NVE services under NLOD.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx
from pyproj import Transformer

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════════

GEN_NEDBORFELT_URL = (
    "https://gis3.nve.no/arcgis/rest/services/geoprocessing/"
    "Nevina4/GPServer/GenNedborFelt"
)
GEN_PARAMS_URL = (
    "https://gis3.nve.no/arcgis/rest/services/geoprocessing/"
    "Nevina4/GPServer/GenNedborFeltParams"
)
NEDBFELT_LAYER_URL = (
    "https://gis3.nve.no/map/rest/services/Mapservices/"
    "Nevina4/MapServer/4/query"
)

# NEVINA's native CRS is ETRS89/UTM33N (EPSG:25833), NOT WGS84/UTM33N
# (EPSG:32633). The two share the UTM-33 projection but differ in datum;
# the offset is a few metres in Norway, which matters because NEVINA only
# snaps points to its river network within ~90 m. Sending a 32633 point
# silently lands "for langt fra elvenettet" near the snap boundary — a
# class of intermittent failure that's miserable to debug. We commit to
# 25833 end to end.
UTM33_WKID = 25833
WGS84_WKID = 4326

# WGS84 → UTM33 (ETRS89). always_xy so call signature is (lng, lat).
_TO_UTM33 = Transformer.from_crs(
    f"EPSG:{WGS84_WKID}", f"EPSG:{UTM33_WKID}", always_xy=True
)


# ═══════════════════════════════════════════════════════════════════════
# Tunables
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_HTTP_TIMEOUT_S = 30.0
DEFAULT_POLL_INTERVAL_S = 1.5
# GenNedborFeltParams can take 2-3 minutes for large catchments (verified
# on Tistedalsfoss / Haldenvassdraget 1572 km²: ~3 minutes). Give it ample
# headroom; the poll cost itself is trivial.
DEFAULT_JOB_TIMEOUT_S = 360.0

# String that NEVINA logs (as an Informative message, never as an error)
# when the supplied point is too far from its river network. Step 1 still
# "succeeds" and returns a GUID, but step 2 then fails with a
# nedborfelt-not-found error. We catch the marker after step 1 so callers
# get a useful exception instead of the cryptic step-2 failure.
_OFF_NETWORK_MARKER = "for langt fra elvenettet"

# NEVINA logs the snap distance in step 1 with this line:
#   "Avstand til elvenettet = 89.28647433936263"
# Capturing it lets callers tell whether engine-vs-NEVINA comparisons are
# clean (both systems looked at the same river segment) or noisy (NEVINA
# snapped tens of metres further than the engine did).
_SNAP_DISTANCE_RE = re.compile(
    r"Avstand til elvenettet\s*=\s*([0-9]+(?:\.[0-9]+)?)"
)


# ═══════════════════════════════════════════════════════════════════════
# Result type
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CatchmentResult:
    """The full NEVINA result for one delineated catchment."""

    guid: str
    area_km2: float
    parameters: dict[str, Any]
    polygon: dict[str, Any] | None = None  # GeoJSON polygon if requested
    # How far NEVINA had to move the user-supplied point to reach a river
    # segment. Surfaced because callers comparing engine vs NEVINA areas
    # need to know whether the two systems even agree on which river the
    # intake is on. >50 m usually means the comparison is noisy.
    snap_distance_m: float | None = None

    @property
    def has_polygon(self) -> bool:
        return self.polygon is not None


# ═══════════════════════════════════════════════════════════════════════
# Error types
# ═══════════════════════════════════════════════════════════════════════

class NevinaError(RuntimeError):
    """Base class for NEVINA-client failures."""


class NevinaJobFailed(NevinaError):
    """ArcGIS GP job ended in a non-success status."""


class NevinaJobTimeout(NevinaError):
    """ArcGIS GP job did not complete within the polling deadline."""


class NevinaNoFeature(NevinaError):
    """The layer-4 query returned no feature for the GUID — usually means
    the GenNedborFeltParams step did not populate the row, or the point
    is outside NEVINA's coverage (i.e., outside Norway)."""


class NevinaPointNotOnRiver(NevinaError):
    """The submitted point is too far from NEVINA's river network for the
    service to delineate a catchment. Move the point onto an actual river
    segment (within ~90 m) and retry. Often happens when a user clicks on
    a fjord, lake or land far from the nearest stream."""


class NevinaInternalError(NevinaError):
    """NEVINA's GenNedborFeltParams script crashed mid-way through computing
    a parameter. Most common cause observed in production: micro-catchments
    smaller than ~0.1 km² that trigger division-by-zero in NEVINA's
    gradient.py (``(hohTopp - hohPkt) / (elvLengd / 1000)``) when the
    upstream stream length is zero. The point is on the network and a
    catchment was delineated, but the parameter pass failed and Layer 4
    will not be populated for this GUID."""


# Substring NEVINA logs in the GP-job messages when its own parameter
# computation crashes (most commonly the gradient division-by-zero). We
# pattern-match generously since the exact wording can vary by which
# parameter step failed.
_INTERNAL_ERROR_MARKERS = (
    "Failed script GenNedborFeltParams",
    "Failed to execute (GenNedborFeltParams)",
)


# ═══════════════════════════════════════════════════════════════════════
# Client
# ═══════════════════════════════════════════════════════════════════════

class NevinaClient:
    """Async client for NEVINA4. Constructable with defaults; injectable
    for tests via the ``http_client`` argument."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        job_timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
    ) -> None:
        self._owned_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=http_timeout_s)
        self._poll_interval_s = poll_interval_s
        self._job_timeout_s = job_timeout_s

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    # ── public API ────────────────────────────────────────────────

    async def delineate(
        self,
        *,
        lng_wgs84: float | None = None,
        lat_wgs84: float | None = None,
        x_utm33: float | None = None,
        y_utm33: float | None = None,
        include_polygon: bool = False,
    ) -> CatchmentResult:
        """Delineate the upstream catchment at a point.

        Accepts either WGS84 (lng, lat) or UTM33 (x, y). UTM33 is
        NEVINA's native CRS; WGS84 is converted internally. Returns the
        full feature record with optional GeoJSON polygon.
        """
        x, y = self._normalise_coords(
            lng_wgs84=lng_wgs84,
            lat_wgs84=lat_wgs84,
            x_utm33=x_utm33,
            y_utm33=y_utm33,
        )
        logger.info("NEVINA delineate at UTM33 (%.2f, %.2f)", x, y)

        guid, snap_distance_m = await self._gen_nedborfelt(x, y)
        await self._gen_nedborfelt_params(guid)
        feature = await self._query_feature(guid, include_polygon=include_polygon)

        attributes = feature.get("attributes") or {}
        area_km2 = float(attributes.get("areal_km2") or 0.0)
        polygon: dict[str, Any] | None = None
        if include_polygon and feature.get("geometry"):
            polygon = feature["geometry"]

        return CatchmentResult(
            guid=guid,
            area_km2=area_km2,
            parameters=attributes,
            polygon=polygon,
            snap_distance_m=snap_distance_m,
        )

    # ── coordinate handling ───────────────────────────────────────

    @staticmethod
    def _normalise_coords(
        *,
        lng_wgs84: float | None,
        lat_wgs84: float | None,
        x_utm33: float | None,
        y_utm33: float | None,
    ) -> tuple[float, float]:
        if x_utm33 is not None and y_utm33 is not None:
            return float(x_utm33), float(y_utm33)
        if lng_wgs84 is not None and lat_wgs84 is not None:
            x, y = _TO_UTM33.transform(lng_wgs84, lat_wgs84)
            return float(x), float(y)
        raise ValueError(
            "Provide either (lng_wgs84, lat_wgs84) or (x_utm33, y_utm33)."
        )

    # ── GenNedborFelt ─────────────────────────────────────────────

    async def _gen_nedborfelt(
        self, x_utm33: float, y_utm33: float
    ) -> tuple[str, float | None]:
        """Step 1: submit point to GenNedborFelt; return (GUID, snap_distance_m).

        Step 1 is misleadingly cheerful: it can finish with status
        ``esriJobSucceeded`` while logging "Punktet er for langt fra
        elvenettet" — in which case no catchment was actually persisted
        and step 2 will fail. We scan the job messages and raise a clear
        :class:`NevinaPointNotOnRiver` before the user wastes time
        polling step 2.

        We also harvest the "Avstand til elvenettet = X" line from the
        job log so callers can judge whether the engine and NEVINA are
        comparing the same physical intake or two close-but-different
        river segments.
        """
        utlopspunkt = self._build_point_recordset(x_utm33, y_utm33)
        params = {
            "Utlopspunkt": json.dumps(utlopspunkt),
            "geometri_endret": "false",
            "f": "json",
        }
        job_id = await self._submit_job(GEN_NEDBORFELT_URL, params)
        job_data = await self._poll_job(GEN_NEDBORFELT_URL, job_id)
        self._check_off_network(job_data, x_utm33, y_utm33)
        snap_distance_m = self._extract_snap_distance(job_data)
        guid_value = await self._fetch_result(
            GEN_NEDBORFELT_URL, job_id, output_param="GUID"
        )
        if not guid_value or not isinstance(guid_value, str):
            raise NevinaError(f"GenNedborFelt returned no GUID: {guid_value!r}")
        return guid_value, snap_distance_m

    @staticmethod
    def _check_off_network(
        job_data: dict[str, Any], x: float, y: float
    ) -> None:
        for msg in job_data.get("messages") or ():
            if _OFF_NETWORK_MARKER in (msg.get("description") or ""):
                raise NevinaPointNotOnRiver(
                    f"Point ({x:.1f}, {y:.1f}) UTM33 is too far from "
                    f"NEVINA's river network. Move it onto an actual "
                    f"river segment (typically within ~90 m) and retry."
                )

    @staticmethod
    def _extract_snap_distance(job_data: dict[str, Any]) -> float | None:
        for msg in job_data.get("messages") or ():
            match = _SNAP_DISTANCE_RE.search(msg.get("description") or "")
            if match:
                return float(match.group(1))
        return None

    @staticmethod
    def _build_point_recordset(x: float, y: float) -> dict[str, Any]:
        """Build the ArcGIS feature-recordset JSON the GP task expects."""
        return {
            "features": [
                {
                    "geometry": {
                        "x": x,
                        "y": y,
                        "spatialReference": {"wkid": UTM33_WKID},
                    },
                    "attributes": {},
                }
            ],
            "geometryType": "esriGeometryPoint",
            "spatialReference": {"wkid": UTM33_WKID},
        }

    # ── GenNedborFeltParams ───────────────────────────────────────

    async def _gen_nedborfelt_params(self, guid: str) -> None:
        """Step 2: tell NEVINA to compute the field parameters. No return
        value — the task populates the feature layer in place."""
        params = {"GUID": guid, "f": "json"}
        job_id = await self._submit_job(GEN_PARAMS_URL, params)
        await self._poll_job(GEN_PARAMS_URL, job_id)

    # ── Layer-4 query ─────────────────────────────────────────────

    async def _query_feature(
        self, guid: str, *, include_polygon: bool
    ) -> dict[str, Any]:
        """Step 3: pull the populated feature for this GUID."""
        params = {
            "where": f"guID='{guid}'",
            "outFields": "*",
            "returnGeometry": "true" if include_polygon else "false",
            "outSR": str(WGS84_WKID) if include_polygon else str(UTM33_WKID),
            "f": "geojson" if include_polygon else "json",
        }
        resp = await self._client.get(NEDBFELT_LAYER_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

        if include_polygon:
            features = data.get("features") or []
            if not features:
                raise NevinaNoFeature(
                    f"Layer 4 returned no feature for GUID={guid!r}"
                )
            feature = features[0]
            return {
                "attributes": feature.get("properties") or {},
                "geometry": feature.get("geometry"),
            }
        features = data.get("features") or []
        if not features:
            raise NevinaNoFeature(
                f"Layer 4 returned no feature for GUID={guid!r}"
            )
        return features[0]

    # ── ArcGIS GP async pattern ───────────────────────────────────

    async def _submit_job(self, base_url: str, params: dict[str, str]) -> str:
        url = f"{base_url}/submitJob"
        resp = await self._client.post(url, data=params)
        resp.raise_for_status()
        data = resp.json()
        job_id = data.get("jobId")
        if not job_id:
            raise NevinaError(
                f"submitJob to {base_url} returned no jobId: {data!r}"
            )
        return job_id

    async def _poll_job(self, base_url: str, job_id: str) -> dict[str, Any]:
        """Poll until ``esriJobSucceeded``; raise on failure or timeout.

        Failure path: NEVINA distinguishes "real failure" (off-network,
        bad GUID) from "internal crash" (gradient div-by-zero etc.) only
        in the job messages, not in the status. Both arrive as
        ``esriJobFailed``. We separate them so callers can tell the
        difference between user-correctable errors and NEVINA bugs we
        just have to log around.
        """
        url = f"{base_url}/jobs/{job_id}"
        deadline = asyncio.get_event_loop().time() + self._job_timeout_s
        while True:
            resp = await self._client.get(url, params={"f": "json"})
            resp.raise_for_status()
            data = resp.json()
            status = data.get("jobStatus", "")
            if status == "esriJobSucceeded":
                return data
            if status in ("esriJobFailed", "esriJobCancelled", "esriJobTimedOut"):
                if self._is_internal_crash(data):
                    raise NevinaInternalError(
                        f"NEVINA's GenNedborFeltParams crashed for job "
                        f"{job_id}. Common cause: catchment < 0.1 km² "
                        f"triggers div-by-zero in NEVINA's gradient.py. "
                        f"Last messages: "
                        f"{self._tail_descriptions(data, n=5)}"
                    )
                raise NevinaJobFailed(
                    f"GP job {job_id} ended with status {status!r}. "
                    f"Last messages: {self._tail_descriptions(data, n=5)}"
                )
            if asyncio.get_event_loop().time() > deadline:
                raise NevinaJobTimeout(
                    f"GP job {job_id} at {base_url} did not finish within "
                    f"{self._job_timeout_s:.0f} s (last status: {status!r})."
                )
            await asyncio.sleep(self._poll_interval_s)

    @staticmethod
    def _is_internal_crash(job_data: dict[str, Any]) -> bool:
        joined = " ".join(
            msg.get("description") or "" for msg in job_data.get("messages") or ()
        )
        return any(marker in joined for marker in _INTERNAL_ERROR_MARKERS)

    @staticmethod
    def _tail_descriptions(job_data: dict[str, Any], *, n: int) -> str:
        msgs = job_data.get("messages") or []
        return " | ".join(
            (m.get("description") or "")[:200] for m in msgs[-n:]
        )

    async def _fetch_result(
        self, base_url: str, job_id: str, *, output_param: str
    ) -> Any:
        url = f"{base_url}/jobs/{job_id}/results/{output_param}"
        resp = await self._client.get(url, params={"f": "json"})
        resp.raise_for_status()
        data = resp.json()
        return data.get("value")
