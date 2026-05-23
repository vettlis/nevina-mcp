"""Tests for the tool layer — verdict thresholds and response shape."""

from __future__ import annotations

from typing import Any

import pytest

from server.nevina_client import CatchmentResult, NevinaClient
from server.tools import (
    VERDICT_AGREE_PCT,
    VERDICT_MINOR_PCT,
    _verdict_for_drift,
    compare_tool,
    delineate_tool,
)


# ── Verdict thresholds ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "drift_pct, expected",
    [
        (0.0, "agree"),
        (4.99, "agree"),
        (-4.99, "agree"),
        (5.0, "drift_minor"),
        (-19.99, "drift_minor"),
        (20.0, "drift_major"),
        (-300.0, "drift_major"),
        (1383.0, "drift_major"),  # P90 from G1 audit
    ],
)
def test_verdict_thresholds_match_calibration(
    drift_pct: float, expected: str
) -> None:
    assert _verdict_for_drift(drift_pct) == expected


def test_thresholds_are_ordered() -> None:
    assert 0 < VERDICT_AGREE_PCT < VERDICT_MINOR_PCT


# ── Tool responses (with fake client) ───────────────────────────────

class _FakeClient:
    """Minimal stand-in for NevinaClient — returns a canned result."""

    def __init__(self, area_km2: float, parameters: dict[str, Any] | None = None) -> None:
        self._result = CatchmentResult(
            guid="fake-guid",
            area_km2=area_km2,
            parameters=parameters or {
                "areal_km2": area_km2,
                "elvenavn": "Testelva",
                "QNormal9120_lskm2": 35.2,
                "fylkeNavn": "Trøndelag",
            },
            polygon=None,
        )

    async def delineate(self, **_kwargs: Any) -> CatchmentResult:
        return self._result


@pytest.mark.asyncio
async def test_delineate_tool_returns_parameters() -> None:
    client = _FakeClient(area_km2=12.3)
    out = await delineate_tool(
        client,  # type: ignore[arg-type]
        lng_wgs84=10.21, lat_wgs84=63.42,
    )
    assert out["guid"] == "fake-guid"
    assert out["area_km2"] == 12.3
    assert out["parameters"]["elvenavn"] == "Testelva"
    assert "polygon" not in out  # polygon omitted by default


@pytest.mark.asyncio
async def test_compare_tool_returns_drift_and_verdict() -> None:
    client = _FakeClient(area_km2=10.0)
    out = await compare_tool(
        client,  # type: ignore[arg-type]
        lng_wgs84=10.21, lat_wgs84=63.42,
        engine_area_km2=11.5,
    )
    assert out["nevina_km2"] == 10.0
    assert out["engine_km2"] == 11.5
    assert out["ratio"] == pytest.approx(1.15)
    assert out["drift_pct"] == pytest.approx(15.0)
    assert out["verdict"] == "drift_minor"
    assert out["nevina_summary"]["elvenavn"] == "Testelva"
    assert out["nevina_summary"]["fylkeNavn"] == "Trøndelag"


@pytest.mark.asyncio
async def test_compare_tool_flags_agreement_for_engine_within_5pct() -> None:
    client = _FakeClient(area_km2=10.0)
    out = await compare_tool(
        client,  # type: ignore[arg-type]
        lng_wgs84=10.21, lat_wgs84=63.42,
        engine_area_km2=10.2,
    )
    assert out["verdict"] == "agree"


@pytest.mark.asyncio
async def test_compare_tool_flags_major_drift_for_g1_style_blowup() -> None:
    # Engine reports 14× NEVINA — typical G1 grid-snap into mainline.
    client = _FakeClient(area_km2=2.5)
    out = await compare_tool(
        client,  # type: ignore[arg-type]
        lng_wgs84=10.21, lat_wgs84=63.42,
        engine_area_km2=35.0,
    )
    assert out["verdict"] == "drift_major"
    assert out["drift_pct"] == pytest.approx(1300.0)


@pytest.mark.asyncio
async def test_compare_tool_handles_zero_nevina_area() -> None:
    client = _FakeClient(area_km2=0.0)
    out = await compare_tool(
        client,  # type: ignore[arg-type]
        lng_wgs84=10.21, lat_wgs84=63.42,
        engine_area_km2=5.0,
    )
    assert "error" in out
    assert "nevina" in out
