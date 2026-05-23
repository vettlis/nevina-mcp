"""Live integration tests against NEVINA4.

Hits NVE's real GP service. Skipped if ``RUN_NEVINA_LIVE`` is unset, so
``pytest tests/`` stays offline-clean by default. Use:

    RUN_NEVINA_LIVE=1 pytest tests/test_integration.py -v
"""

from __future__ import annotations

import os

import pytest

from server.nevina_client import NevinaClient

LIVE = os.environ.get("RUN_NEVINA_LIVE") == "1"
pytestmark = pytest.mark.skipif(
    not LIVE, reason="Set RUN_NEVINA_LIVE=1 to enable live NEVINA tests"
)


# Tistedalsfoss measuring station 1.50.0 — verified end-to-end against
# the live NEVINA service on 2026-05-23 from this client's commit:
#   areal_km2=1571.89, elvenavn="Haldenvassdraget",
#   QNormal9120_lskm2=15.93, QNormal9120_mmAar=502.73.
# Coordinates pulled directly from MapServer/0 (NEVINA's own station layer),
# so they are guaranteed to be on the Elvenett river network.
TISTEDALSFOSS_X_UTM33 = 296460.0
TISTEDALSFOSS_Y_UTM33 = 6559658.0
TISTEDALSFOSS_EXPECTED_AREA_KM2 = 1571.89


@pytest.mark.asyncio
async def test_live_delineation_against_tistedalsfoss_station() -> None:
    client = NevinaClient()
    try:
        result = await client.delineate(
            x_utm33=TISTEDALSFOSS_X_UTM33, y_utm33=TISTEDALSFOSS_Y_UTM33
        )
    finally:
        await client.aclose()

    # Allow ±1 % tolerance — NEVINA snaps the point to the nearest stream,
    # which can land on a slightly different segment between deploys.
    expected = TISTEDALSFOSS_EXPECTED_AREA_KM2
    rel_drift = abs(result.area_km2 - expected) / expected
    assert rel_drift < 0.01, (
        f"areal_km2={result.area_km2} drifts {rel_drift:.2%} "
        f"from baseline {expected}"
    )
    assert result.parameters.get("elvenavn") == "Haldenvassdraget"
