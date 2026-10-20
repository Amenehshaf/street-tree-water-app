#!/usr/bin/env python3
"""Download HOSTRADA hourly data for a location and calculate daily ET0.

The ET0 equations and assumptions intentionally follow calculate_ET0_2025.py
in the project's sources directory. User interaction is in English.
"""

from __future__ import annotations

import calendar
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import numpy as np
    import requests
    from osgeo import gdal, osr
    from openpyxl import Workbook
except ImportError as exc:
    print(f"Missing dependency: {exc.name}. Install requirements from README_ET0.md.")
    raise SystemExit(2)

gdal.UseExceptions()

BASE_URL = "https://opendata.dwd.de/climate_environment/CDC/grids_germany/hourly/hostrada/"
PARAMETERS = {
    "tas": "air_temperature_mean",
    "hurs": "humidity_relative",
    "sfcWind": "wind_speed",
    "rsds": "radiation_downwelling",
}
ELEVATION_M = 55.0  # Same provisional elevation assumption as the reference script.
MIN_VALID_HOURS = 22
WIND_HEIGHT_M = 10.0
OUTPUT_COLUMNS = [
    "date", "ET0_mm_day", "Tmean_C", "Tmin_C", "Tmax_C", "RHmean_pct", "RHmin_pct",
    "wind10_mean_ms", "wind2_mean_ms", "Rs_MJ_m2_day", "Rn_MJ_m2_day",
    "valid_hours", "status",
]


def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def ask_float(prompt: str, minimum: float, maximum: float) -> float:
    while True:
        raw = input(prompt).strip()
        try:
            value = float(raw.replace(",", "."))
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError
            return value
        except ValueError:
            print(f"Enter a number from {minimum} to {maximum}.")


def ask_date(prompt: str) -> date:
    while True:
        raw = input(prompt).strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print("Enter a valid date in YYYY-MM-DD format (for example, 2025-06-01).")


def ask_keep_files() -> bool:
    while True:
        answer = input("Keep downloaded HOSTRADA NetCDF files after processing? (Yes/No): ").strip().lower()
        if answer in {"yes", "y"}:
            return True
        if answer in {"no", "n"}:
            return False
        print("Please answer Yes or No.")


def choose_output_folder() -> Path:
    default = Path.home() / "Desktop" / "HOSTRADA_ET0"
    print(f"Output folder (press Enter to use {default}):")
    supplied = input("> ").strip().strip('"')
    folder = Path(supplied).expanduser() if supplied else default
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(f"Cannot create or write to output folder {folder}: {exc}") from exc
    return folder


def point_to_grid(latitude: float, longitude: float) -> tuple[float, float, int, int]:
    """Transform WGS84 coordinates to HOSTRADA's EPSG:3034 grid and find cell."""
    source = osr.SpatialReference()
    source.ImportFromEPSG(4326)
    target = osr.SpatialReference()
    target.ImportFromEPSG(3034)
    # Ensure input tuple is interpreted as longitude, latitude on GDAL 3+.
    if hasattr(source, "SetAxisMappingStrategy"):
        source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(source, target)
    x, y, _ = transform.TransformPoint(longitude, latitude)
    return x, y, math.floor(x), math.floor(y)


def make_filename(variable: str, year: int, month: int) -> str:
    last_day = calendar.monthrange(year, month)[1]
    return (f"{variable}_1hr_HOSTRADA-v1-0_BE_gn_{year}{month:02d}0100-"
            f"{year}{month:02d}{last_day:02d}23.nc")


def download_file(variable: str, year: int, month: int, raw_dir: Path) -> tuple[Path, bool]:
    filename = make_filename(variable, year, month)
    destination = raw_dir / filename
    if destination.is_file() and destination.stat().st_size > 0:
        log(f"Using existing download: {filename}")
        return destination, False
    url = f"{BASE_URL}{PARAMETERS[variable]}/{filename}"
    temporary = destination.with_name(destination.name + ".part")
    if temporary.exists():
        temporary.unlink()
    for attempt in range(1, 4):
        try:
            log(f"Downloading {filename} (attempt {attempt}/3)")
            with requests.get(url, stream=True, timeout=(30, 180)) as response:
                response.raise_for_status()
                with temporary.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            stream.write(chunk)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("The downloaded file is empty.")
            temporary.replace(destination)
            return destination, True
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            if attempt == 3:
                raise RuntimeError(f"Could not download {filename}: {exc}") from exc
            log(f"Download failed: {exc}; retrying in 5 seconds.")
            time.sleep(5)
    raise RuntimeError(f"Could not download {filename}.")


def find_variable_dataset(filepath: Path, variable: str):
    root = gdal.Open(str(filepath))
    if root is None:
        raise RuntimeError(f"GDAL cannot open {filepath}")
    candidates = root.GetSubDatasets() or []
    selected = next((name for name, _ in candidates
                     if re.search(rf":{re.escape(variable)}(?=[:\s]|$)", name, re.I)), None)
    if selected:
        ds = gdal.Open(selected)
    elif not candidates:
        ds = root
    else:
        ds = None
    if ds is None:
        root = None
        raise RuntimeError(f"Cannot locate variable {variable} in {filepath.name}")
    return root, ds


def metadata(ds, band, key: str):
    for obj in (ds, band):
        value = obj.GetMetadataItem(key)
        if value is not None:
            return value
        for domain in obj.GetMetadataDomainList() or []:
            value = obj.GetMetadataItem(key, domain)
            if value is not None:
                return value
    return None


def read_hourly_pixel(filepath: Path, variable: str, x: float, y: float,
                      expected_start: datetime, expected_count: int) -> np.ndarray:
    root, ds = find_variable_dataset(filepath, variable)
    try:
        gt = ds.GetGeoTransform()
        if not gt or gt[2] != 0 or gt[4] != 0 or gt[1] == 0 or gt[5] == 0:
            raise RuntimeError(f"Unexpected grid transform in {filepath.name}")
        col = math.floor((x - gt[0]) / gt[1])
        row = math.floor((y - gt[3]) / gt[5])
        if not (0 <= col < ds.RasterXSize and 0 <= row < ds.RasterYSize):
            raise RuntimeError(f"Selected location falls outside {filepath.name}")
        if ds.RasterCount != expected_count:
            raise RuntimeError(f"{filepath.name} has {ds.RasterCount} hourly bands; expected {expected_count}")

        band1 = ds.GetRasterBand(1)
        scale_text = metadata(ds, band1, "scale_factor")
        offset_text = metadata(ds, band1, "add_offset")
        scale = band1.GetScale()
        offset = band1.GetOffset()
        if scale is None:
            scale = float(scale_text) if scale_text is not None else 1.0
        if offset is None:
            offset = float(offset_text) if offset_text is not None else 0.0
        units = metadata(ds, band1, "units")
        # Match the reference scripts' physical units; fail instead of silently
        # applying formulas to differently scaled variables.
        if units:
            normalized = units.lower().replace(" ", "").replace("°", "")
            allowed = {
                "tas": {"k", "kelvin", "degc", "c", "degree_celsius", "degrees_celsius"},
                "hurs": {"%", "percent", "1", "fraction"},
                "sfcWind": {"m/s", "ms-1", "m.s-1", "m*s-1"},
                "rsds": {"w/m2", "wm-2", "w.m-2", "w*m-2"},
            }[variable]
            if normalized not in allowed:
                raise RuntimeError(f"Unexpected {variable} units '{units}' in {filepath.name}")
        nodata = {float(v) for v in (
            metadata(ds, band1, "_FillValue"), metadata(ds, band1, "missing_value"),
            band1.GetNoDataValue()) if v is not None}
        values = np.full(expected_count, np.nan, dtype=float)
        for index in range(expected_count):
            raw = float(ds.GetRasterBand(index + 1).ReadAsArray(col, row, 1, 1)[0, 0])
            if not math.isfinite(raw) or raw in nodata:
                continue
            value = raw * float(scale) + float(offset)
            # The established calculation consumes Celsius. HOSTRADA tas may
            # be stored in Kelvin; convert only when units explicitly say Kelvin.
            if variable == "tas" and units and units.lower().strip() in {"k", "kelvin"}:
                value -= 273.15
            if variable == "hurs" and units and units.strip() == "1":
                value *= 100.0
            if math.isfinite(value):
                values[index] = value
        return values
    finally:
        ds = None
        root = None


def saturation_vapour_pressure(temp_c: float) -> float:
    return 0.6108 * math.exp((17.27 * temp_c) / (temp_c + 237.3))


def extraterrestrial_radiation(latitude_deg: float, day_of_year: int) -> float:
    phi = math.radians(latitude_deg)
    dr = 1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.0)
    delta = 0.409 * math.sin(2.0 * math.pi * day_of_year / 365.0 - 1.39)
    argument = max(-1.0, min(1.0, -math.tan(phi) * math.tan(delta)))
    ws = math.acos(argument)
    gsc = 0.0820
    ra = (24.0 * 60.0 / math.pi) * gsc * dr * (
        ws * math.sin(phi) * math.sin(delta)
        + math.cos(phi) * math.cos(delta) * math.sin(ws))
    return max(0.0, ra)


def calculate_daily_et0(temperatures, humidities, wind_speeds, radiation_wm2,
                        day: date, latitude: float, elevation_m: float = ELEVATION_M):
    arrays = [np.asarray(a, dtype=float) for a in
              (temperatures, humidities, wind_speeds, radiation_wm2)]
    valid = np.ones(len(arrays[0]), dtype=bool)
    for arr in arrays:
        valid &= np.isfinite(arr)
    t, rh, wind, rs = [arr[valid] for arr in arrays]
    n = len(t)
    if n < MIN_VALID_HOURS:
        return None, n
    if np.any((rh < 0) | (rh > 100)) or np.any(wind < 0) or np.any(rs < 0):
        raise ValueError(f"Unexpected input values on {day.isoformat()}")
    tmin, tmax, tmean = float(np.min(t)), float(np.max(t)), float(np.mean(t))
    ea = float(np.mean([saturation_vapour_pressure(float(temp)) * float(h) / 100.0
                        for temp, h in zip(t, rh)]))
    es = (saturation_vapour_pressure(tmax) + saturation_vapour_pressure(tmin)) / 2.0
    vpd = max(0.0, es - ea)
    u10 = float(np.mean(wind))
    u2 = u10 * 4.87 / math.log(67.8 * WIND_HEIGHT_M - 5.42)
    rs_day = float(np.sum(rs) * 3600.0 / 1_000_000.0)
    rns = (1.0 - 0.23) * rs_day
    pressure = 101.3 * ((293.0 - 0.0065 * elevation_m) / 293.0) ** 5.26
    gamma = 0.000665 * pressure
    delta = 4098.0 * saturation_vapour_pressure(tmean) / ((tmean + 237.3) ** 2)
    tmax_k, tmin_k = tmax + 273.16, tmin + 273.16
    sigma = 4.903e-9
    ra = extraterrestrial_radiation(latitude, day.timetuple().tm_yday)
    rso = (0.75 + 2.0e-5 * elevation_m) * ra
    cloud_ratio = max(0.3, min(1.0, rs_day / rso)) if rso > 0 else 0.3
    rnl = (sigma * ((tmax_k ** 4 + tmin_k ** 4) / 2.0)
           * (0.34 - 0.14 * math.sqrt(max(0.0, ea)))
           * (1.35 * cloud_ratio - 0.35))
    rn = rns - rnl
    numerator = 0.408 * delta * rn + gamma * (900.0 / (tmean + 273.0)) * u2 * vpd
    denominator = delta + gamma * (1.0 + 0.34 * u2)
    et0 = max(0.0, numerator / denominator)
    return {
        "ET0_mm_day": et0, "Tmean_C": tmean, "Tmin_C": tmin, "Tmax_C": tmax,
        "RHmean_pct": float(np.mean(rh)), "RHmin_pct": float(np.min(rh)), "wind10_mean_ms": u10,
        "wind2_mean_ms": u2, "Rs_MJ_m2_day": rs_day, "Rn_MJ_m2_day": rn,
        "valid_hours": n,
    }, n


def process(latitude: float, longitude: float, start: date, end: date,
            keep_files: bool, output_dir: Path, elevation_m: float = ELEVATION_M) -> Path:
    x, y, _, _ = point_to_grid(latitude, longitude)
    raw_dir = output_dir / "HOSTRADA_NetCDF"
    raw_dir.mkdir(parents=True, exist_ok=True)
    log(f"Location: latitude {latitude:.6f}, longitude {longitude:.6f}")
    log(f"HOSTRADA projected coordinate (EPSG:3034): X={x:.2f}, Y={y:.2f}")
    log(f"Grid cell will be determined from the downloaded raster geotransform.")
    log(f"Period: {start.isoformat()} through {end.isoformat()}")

    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    hourly_by_variable = {v: np.full(len(days) * 24, np.nan, dtype=float) for v in PARAMETERS}
    months = sorted({(d.year, d.month) for d in days})
    created_files: list[Path] = []
    try:
        for month_index, (year, month) in enumerate(months, 1):
            log(f"Month {month_index}/{len(months)}: {year}-{month:02d}")
            month_start = datetime(year, month, 1)
            count = calendar.monthrange(year, month)[1] * 24
            for variable in PARAMETERS:
                path, downloaded_now = download_file(variable, year, month, raw_dir)
                if downloaded_now:
                    created_files.append(path)
                monthly = read_hourly_pixel(path, variable, x, y, month_start, count)
                for idx in range(count):
                    stamp = month_start + timedelta(hours=idx)
                    if start <= stamp.date() <= end:
                        target = (stamp.date() - start).days * 24 + stamp.hour
                        hourly_by_variable[variable][target] = monthly[idx]

        results = []
        for day_index, day in enumerate(days):
            a, b = day_index * 24, (day_index + 1) * 24
            result, valid_hours = calculate_daily_et0(
                hourly_by_variable["tas"][a:b], hourly_by_variable["hurs"][a:b],
                hourly_by_variable["sfcWind"][a:b], hourly_by_variable["rsds"][a:b],
                day, latitude, elevation_m)
            if result is None:
                results.append({"date": day.isoformat(), "ET0_mm_day": None,
                                "Tmean_C": None, "Tmin_C": None, "Tmax_C": None,
                                "RHmean_pct": None, "RHmin_pct": None, "wind10_mean_ms": None,
                                "wind2_mean_ms": None, "Rs_MJ_m2_day": None,
                                "Rn_MJ_m2_day": None, "valid_hours": valid_hours,
                                "status": "INSUFFICIENT_VALID_HOURS"})
            else:
                results.append({"date": day.isoformat(), **result, "status": "OK"})
            if day_index == 0 or (day_index + 1) % 25 == 0 or day_index + 1 == len(days):
                log(f"Calculated {day_index + 1}/{len(days)} days")
        out = output_dir / f"ET0_daily_{start:%Y%m%d}_{end:%Y%m%d}.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Daily ET0"
        sheet.append(OUTPUT_COLUMNS)
        for row in results:
            sheet.append([row.get(column) for column in OUTPUT_COLUMNS])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        widths = [14, 14, 12, 12, 12, 14, 17, 16, 17, 17, 13, 30]
        for index, width in enumerate(widths, 1):
            sheet.column_dimensions[chr(64 + index)].width = width
        workbook.save(out)
        log(f"Excel output created: {out}")
        log(f"Elevation used in calculation: {elevation_m:.1f} m (user-selected; verify before reporting)")
        return out
    finally:
        if not keep_files:
            for path in created_files:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    log(f"Could not remove downloaded file {path.name}: {exc}")
            # Keep directory if it contains files from earlier runs; remove only if empty.
            try:
                raw_dir.rmdir()
            except OSError:
                pass


def main() -> int:
    print("HOSTRADA daily reference evapotranspiration (ET0)")
    latitude = ask_float("Latitude in decimal degrees (-90 to 90): ", -90, 90)
    longitude = ask_float("Longitude in decimal degrees (-180 to 180): ", -180, 180)
    start = ask_date("Start date (YYYY-MM-DD): ")
    end = ask_date("End date (YYYY-MM-DD): ")
    if end < start:
        print("Error: the end date must be on or after the start date.")
        return 2
    if start.year < 1995 or end.year > datetime.now().year:
        print("Error: HOSTRADA monthly hourly files are available from 1995; future dates cannot be requested.")
        return 2
    keep = ask_keep_files()
    try:
        output_dir = choose_output_folder()
        process(latitude, longitude, start, end, keep, output_dir)
    except KeyboardInterrupt:
        log("Cancelled by user.")
        return 130
    except Exception as exc:
        log(f"ERROR: {exc}")
        print("Processing stopped. Check the message above, verify the dates and coordinates, and try again.")
        return 1
    print("Finished successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
