# nevina-mcp

Claude Code MCP-plugin som eksponerer NVEs **NEVINA4**-tjeneste som
verktøy. Hentes inn i alle prosjekter for å validere drainagearealer
og hydrologiske parametre mot NVEs autoritative nedbørfelt-delineering.

## Hvorfor

Small Hydro Prospect-motoren beregner nedbørfelt fra DEM + REGINE.
Forensic-auditen 2026-05-22 fant G1-drift på median 36 % mot NVE i
prod. Med denne pluginen kan Claude (eller du) be om en NEVINA-sjekk
direkte fra terminalen og få sannheten på 2-10 sekunder.

## Verktøy

### `nevina_delineate`
Inn: `lng_wgs84`, `lat_wgs84` (eller `x_utm33`, `y_utm33`), valgfritt
`include_polygon`. Ut: GUID, areal i km², full parameter-dict
(QNormal9120, høyder, arealklasser, klimaregion), valgfritt GeoJSON.

### `nevina_compare_to_engine`
Inn: koordinater + `engine_area_km2`. Ut: ratio, drift_pct og verdict
(`agree` <5 %, `drift_minor` 5-20 %, `drift_major` ≥20 %).

## Workflow under panseret

NEVINA4 er en asynkron ArcGIS GP-tjeneste i tre trinn:

1. `GenNedborFelt(punkt_utm33)` → `jobId` → poll → `GUID`
2. `GenNedborFeltParams(GUID)` → `jobId` → poll → (skriver til layer 4)
3. `MapServer/4/query?where=guID='<guid>'` → 341 felter (`areal_km2`,
   `QNormal9120_lskm2`, `heightMin/Max`, `sjoProsent`, `breProsent`, …)

Hele kjeden går via én klientmetode (`NevinaClient.delineate`).
Tilkobling skjer over `httpx.AsyncClient`. CRS-konvertering
(WGS84 → UTM33/EPSG:32633) gjøres med `pyproj`.

## Installasjon

Plugin auto-discoveres av Claude Code når den ligger under
`~/.claude/plugins/nevina-mcp/`. Avhengigheter:

```bash
cd ~/.claude/plugins/nevina-mcp
pip install -r requirements.txt
```

## Testing

```bash
# enhetstester (offline)
pytest tests/test_nevina_client.py tests/test_tools.py -v

# live mot NEVINA
RUN_NEVINA_LIVE=1 pytest tests/test_integration.py -v
```

## Datakilde

NVE NEVINA4 (`gis3.nve.no/arcgis/rest/services/.../Nevina4/...`),
åpent under Norsk lisens for offentlige data (NLOD). Ingen API-nøkkel.

## Bruksområder

- Punktvis G1-validering under feilsøking.
- Bulk-validering ved å sløyfe over et site-utvalg fra prod-DB.
- Sanity-sjekk av nye intake-koordinater før screening kjøres.
