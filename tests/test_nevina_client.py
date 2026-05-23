"""Unit tests for the NEVINA HTTP client.

Network is fully mocked via httpx.MockTransport. Three concerns under test:

1. The async ArcGIS GP pattern: submitJob → poll → results.
2. The coordinate-normalisation contract (WGS84 ↔ UTM33).
3. Failure modes: job timeout, job failure, missing feature.

Live calls live in test_integration.py and are explicitly skipped without
network access.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from server.nevina_client import (
    GEN_NEDBORFELT_URL,
    GEN_PARAMS_URL,
    NEDBFELT_LAYER_URL,
    NevinaClient,
    NevinaJobFailed,
    NevinaJobTimeout,
    NevinaNoFeature,
    NevinaPointNotOnRiver,
    UTM33_WKID,
)


# ═══════════════════════════════════════════════════════════════════════
# Test fixtures
# ═══════════════════════════════════════════════════════════════════════

SAMPLE_GUID = "abcd1234-5678-90ab-cdef-1234567890ab"
SAMPLE_AREA_KM2 = 42.5


def _make_handler(
    scripted_responses: list[tuple[str, dict[str, Any]]],
) -> httpx.MockTransport:
    """Build a mock transport that returns scripted responses in order.

    Each element is ``(url_substring, json_body)``. The handler asserts
    the request's URL contains the substring — keeps tests as a checklist
    of the actual request sequence."""
    iterator = iter(scripted_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            expected_substring, body = next(iterator)
        except StopIteration:
            raise AssertionError(
                f"Unexpected extra request: {request.method} {request.url}"
            )
        assert expected_substring in str(request.url), (
            f"Expected URL containing {expected_substring!r}, "
            f"got {request.url!s}"
        )
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def _client_with(transport: httpx.MockTransport) -> NevinaClient:
    return NevinaClient(
        http_client=httpx.AsyncClient(transport=transport, timeout=5.0),
        poll_interval_s=0.0,  # tests should not actually sleep
        job_timeout_s=5.0,
    )


# ═══════════════════════════════════════════════════════════════════════
# Happy path
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_delineate_runs_full_three_step_pipeline() -> None:
    transport = _make_handler([
        # Step 1: GenNedborFelt submitJob → succeeded
        ("GenNedborFelt/submitJob", {"jobId": "job1"}),
        ("GenNedborFelt/jobs/job1", {"jobStatus": "esriJobSucceeded"}),
        ("GenNedborFelt/jobs/job1/results/GUID", {"value": SAMPLE_GUID}),
        # Step 2: GenNedborFeltParams submitJob → succeeded
        ("GenNedborFeltParams/submitJob", {"jobId": "job2"}),
        ("GenNedborFeltParams/jobs/job2", {"jobStatus": "esriJobSucceeded"}),
        # Step 3: MapServer/4 query
        ("MapServer/4/query", {
            "features": [{
                "attributes": {
                    "areal_km2": SAMPLE_AREA_KM2,
                    "QNormal9120_lskm2": 35.2,
                    "elvenavn": "Testelva",
                },
            }],
        }),
    ])
    client = _client_with(transport)

    try:
        result = await client.delineate(
            lng_wgs84=10.21, lat_wgs84=63.42  # Trondheim-ish
        )
    finally:
        await client.aclose()

    assert result.guid == SAMPLE_GUID
    assert result.area_km2 == SAMPLE_AREA_KM2
    assert result.parameters["elvenavn"] == "Testelva"
    assert result.polygon is None


@pytest.mark.asyncio
async def test_delineate_includes_polygon_when_requested() -> None:
    polygon_geom = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    transport = _make_handler([
        ("GenNedborFelt/submitJob", {"jobId": "j1"}),
        ("GenNedborFelt/jobs/j1", {"jobStatus": "esriJobSucceeded"}),
        ("GenNedborFelt/jobs/j1/results/GUID", {"value": SAMPLE_GUID}),
        ("GenNedborFeltParams/submitJob", {"jobId": "j2"}),
        ("GenNedborFeltParams/jobs/j2", {"jobStatus": "esriJobSucceeded"}),
        ("MapServer/4/query", {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"areal_km2": SAMPLE_AREA_KM2},
                "geometry": polygon_geom,
            }],
        }),
    ])
    client = _client_with(transport)

    try:
        result = await client.delineate(
            lng_wgs84=10.21, lat_wgs84=63.42, include_polygon=True
        )
    finally:
        await client.aclose()

    assert result.has_polygon
    assert result.polygon == polygon_geom


# ═══════════════════════════════════════════════════════════════════════
# Coordinate handling
# ═══════════════════════════════════════════════════════════════════════

def test_utm33_passed_through_without_transform() -> None:
    x, y = NevinaClient._normalise_coords(
        lng_wgs84=None, lat_wgs84=None, x_utm33=275000.0, y_utm33=7050000.0
    )
    assert (x, y) == (275000.0, 7050000.0)


def test_wgs84_is_converted_to_utm33() -> None:
    # Trondheim: 10.40 E, 63.43 N. UTM33's central meridian is 15° E,
    # so a point ~4.6° west of CM lands at easting ~270 km (false 500 km
    # minus ~230 km). Northing is hemisphere-positive ~7 034 km.
    # The WGS84↔ETRS89 datum shift in Norway is sub-metre, so the bounds
    # below are intentionally generous.
    x, y = NevinaClient._normalise_coords(
        lng_wgs84=10.40, lat_wgs84=63.43, x_utm33=None, y_utm33=None
    )
    assert 260_000 < x < 280_000, f"easting {x} out of expected band"
    assert 7_030_000 < y < 7_050_000, f"northing {y} out of expected band"


def test_utm33_wkid_is_etrs89_not_wgs84() -> None:
    # Regression guard. NEVINA's native CRS is EPSG:25833 (ETRS89/UTM33N),
    # not 32633 (WGS84/UTM33N). Sending 32633 silently slides points off
    # NEVINA's snap-to-network tolerance near the river edge.
    assert UTM33_WKID == 25833


def test_missing_coords_raises() -> None:
    with pytest.raises(ValueError, match="Provide either"):
        NevinaClient._normalise_coords(
            lng_wgs84=None, lat_wgs84=None, x_utm33=None, y_utm33=None
        )


def test_point_recordset_wraps_in_utm33() -> None:
    recordset = NevinaClient._build_point_recordset(275000.0, 7050000.0)
    feature = recordset["features"][0]
    assert feature["geometry"]["x"] == 275000.0
    assert feature["geometry"]["y"] == 7050000.0
    assert feature["geometry"]["spatialReference"]["wkid"] == UTM33_WKID
    assert recordset["spatialReference"]["wkid"] == UTM33_WKID
    assert recordset["geometryType"] == "esriGeometryPoint"


# ═══════════════════════════════════════════════════════════════════════
# Failure paths
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_job_failure_status_raises_nevina_job_failed() -> None:
    transport = _make_handler([
        ("GenNedborFelt/submitJob", {"jobId": "jbad"}),
        ("GenNedborFelt/jobs/jbad", {
            "jobStatus": "esriJobFailed",
            "messages": [{"type": "esriJobMessageTypeError", "description": "outside Norway"}],
        }),
    ])
    client = _client_with(transport)

    try:
        with pytest.raises(NevinaJobFailed, match="esriJobFailed"):
            await client.delineate(lng_wgs84=10.21, lat_wgs84=63.42)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_job_timeout_raises_nevina_job_timeout() -> None:
    # Always-pending status; client must eventually time out.
    pending_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "submitJob" in url:
            return httpx.Response(200, json={"jobId": "jslow"})
        if "/jobs/jslow" in url:
            pending_count[0] += 1
            return httpx.Response(200, json={"jobStatus": "esriJobExecuting"})
        raise AssertionError(f"Unexpected: {url}")

    transport = httpx.MockTransport(handler)
    client = NevinaClient(
        http_client=httpx.AsyncClient(transport=transport, timeout=5.0),
        poll_interval_s=0.0,
        job_timeout_s=0.05,  # ~50 ms — should trip on first or second poll
    )

    try:
        with pytest.raises(NevinaJobTimeout):
            await client.delineate(lng_wgs84=10.21, lat_wgs84=63.42)
    finally:
        await client.aclose()

    assert pending_count[0] >= 1


@pytest.mark.asyncio
async def test_off_network_point_raises_before_step_2() -> None:
    """NEVINA "succeeds" step 1 with the off-network marker in messages.
    We must catch it and raise NevinaPointNotOnRiver, not proceed to a
    misleading step-2 failure."""
    transport = _make_handler([
        ("GenNedborFelt/submitJob", {"jobId": "j1"}),
        ("GenNedborFelt/jobs/j1", {
            "jobStatus": "esriJobSucceeded",
            "messages": [
                {"type": "esriJobMessageTypeInformative", "description": "Beregner Avstand til elvenettet"},
                {"type": "esriJobMessageTypeInformative", "description": "Info: Punktet er for langt fra elvenettet. Lag et punkt på elvenettet og prøv igjen"},
            ],
        }),
    ])
    client = _client_with(transport)

    try:
        with pytest.raises(NevinaPointNotOnRiver, match="too far"):
            await client.delineate(lng_wgs84=10.21, lat_wgs84=63.42)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_empty_layer_query_raises_no_feature() -> None:
    transport = _make_handler([
        ("GenNedborFelt/submitJob", {"jobId": "j1"}),
        ("GenNedborFelt/jobs/j1", {"jobStatus": "esriJobSucceeded"}),
        ("GenNedborFelt/jobs/j1/results/GUID", {"value": SAMPLE_GUID}),
        ("GenNedborFeltParams/submitJob", {"jobId": "j2"}),
        ("GenNedborFeltParams/jobs/j2", {"jobStatus": "esriJobSucceeded"}),
        ("MapServer/4/query", {"features": []}),
    ])
    client = _client_with(transport)

    try:
        with pytest.raises(NevinaNoFeature):
            await client.delineate(lng_wgs84=10.21, lat_wgs84=63.42)
    finally:
        await client.aclose()


# ═══════════════════════════════════════════════════════════════════════
# Endpoint URLs (regression: prevents accidental rename)
# ═══════════════════════════════════════════════════════════════════════

def test_endpoints_point_to_nevina4() -> None:
    assert "Nevina4" in GEN_NEDBORFELT_URL and "GenNedborFelt" in GEN_NEDBORFELT_URL
    assert "Nevina4" in GEN_PARAMS_URL and "GenNedborFeltParams" in GEN_PARAMS_URL
    assert "Nevina4/MapServer/4/query" in NEDBFELT_LAYER_URL
