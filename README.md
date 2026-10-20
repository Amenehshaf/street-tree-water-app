# Street Tree Water Demand Calculator (prototype)

A Streamlit web application based on the existing HOSTRADA ET0 script. It provides:

- automatic download of DWD HOSTRADA hourly NetCDF files for a point and date range;
- an alternative upload mode for hourly CSV data;
- daily FAO-56 Penman–Monteith ET0 results, charts, and CSV export;
- editable elevation, with 55 m retained only as an unverified default.

## Important scientific boundary

This prototype calculates **reference evapotranspiration (ET0)**. It does not claim that ET0 alone equals the actual water consumption or irrigation requirement of an isolated street tree. The Kcb script in the source project uses provisional canopy-height/climate adjustments and labels its result as a full-cover estimate, not a validated isolated-tree demand. Keep tree demand and irrigation calculations separate until the relevant model assumptions are established.

## Run locally

Use Python 3.11 or 3.12 in an environment with GDAL's command-line/system libraries and matching Python bindings installed. GDAL is the main platform-sensitive dependency.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

If GDAL cannot be installed by pip on your system, install GDAL through OSGeo4W (Windows) or your operating-system package manager and install the matching Python bindings.

## Inputs for upload mode

Upload an hourly CSV with these exact columns and units:

- `timestamp`: parseable date and time, e.g. `2025-01-01 00:00`
- `tas_C`: air temperature in °C
- `hurs_pct`: relative humidity in % (0–100)
- `sfcWind_ms`: wind speed in m/s measured at 10 m
- `rsds_W_m2`: hourly mean downward shortwave radiation in W/m²

One row per hour is recommended. Days with fewer than 22 valid hourly records are marked insufficient.

## Deploy a public URL

1. Create a GitHub repository and upload all files in this folder.
2. Open Streamlit Community Cloud and connect the repository.
3. Select `app.py` as the entry point and deploy.
4. Check the build logs carefully. GDAL can be platform-sensitive; the `packages.txt` file installs system GDAL libraries, but the Python GDAL wheel must match the available GDAL version. If the cloud build fails, pin a compatible GDAL version for that image or move raster extraction into a separate service/container.
5. Test both modes with a short date range before sharing the URL.

A public URL does not exist until deployment succeeds.
