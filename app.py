from __future__ import annotations
import io, math, tempfile, traceback
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Street Tree Water Demand", page_icon="🌳", layout="wide")
st.title("🌳 Street Tree Water Demand Calculator")
st.caption("Research prototype · Cologne, Germany · FAO-56 reference evapotranspiration")
st.info("This is a research prototype. ET₀ follows the existing HOSTRADA/FAO-56 workflow. Tree-water-demand coefficients and substrate parameters must be documented and validated separately.")

with st.sidebar:
    st.header("Location and period")
    lat = st.number_input("Latitude (°)", value=50.94518949280149, format="%.8f", min_value=-90.0, max_value=90.0)
    lon = st.number_input("Longitude (°)", value=7.00761713267849, format="%.8f", min_value=-180.0, max_value=180.0)
    elevation = st.number_input("Elevation above sea level (m)", min_value=-500.0, max_value=9000.0, value=55.0, step=1.0)
    st.caption("55 m is retained from the previous script as an unverified default. Check the actual site elevation before using results in the thesis.")
    tree_height = st.number_input("Tree height for optional Kcb proxy (m)", min_value=0.5, max_value=30.0, value=5.0, step=0.5)
    calculate_kcb = st.checkbox("Also calculate provisional FAO-56 Kcb full-cover proxy", value=True)
    st.caption("This Kcb calculation is a provisional full-cover estimate, not validated water use of an isolated tree.")
    start = st.date_input("Start date", value=date(2025,1,1), min_value=date(1995,1,1), max_value=date.today())
    end = st.date_input("End date", value=date(2025,12,31), min_value=date(1995,1,1), max_value=date.today())

mode = st.radio("Weather-data input", ["Automatic HOSTRADA download", "Upload existing HOSTRADA NetCDF files", "Upload existing hourly CSV"], horizontal=True)

# The ET0 calculation is intentionally kept separate from the optional Kcb adjustment.
def saturation_vapour_pressure(t):
    return 0.6108 * np.exp((17.27 * t) / (t + 237.3))

def ra_fao56(latitude_deg, doy):
    phi = np.radians(latitude_deg)
    dr = 1 + 0.033 * np.cos(2*np.pi*doy/365)
    delta = 0.409 * np.sin(2*np.pi*doy/365 - 1.39)
    arg = np.clip(-np.tan(phi)*np.tan(delta), -1, 1)
    ws = np.arccos(arg)
    return np.maximum(0, (24*60/np.pi)*0.0820*dr*(ws*np.sin(phi)*np.sin(delta)+np.cos(phi)*np.cos(delta)*np.sin(ws)))

def calculate_daily(group, latitude, elevation_m):
    # Required hourly CSV columns and units are explicitly stated below.
    cols = ["tas_C", "hurs_pct", "sfcWind_ms", "rsds_W_m2"]
    g = group[cols].apply(pd.to_numeric, errors="coerce")
    valid = g.notna().all(axis=1)
    g = g.loc[valid]
    n = len(g)
    if n < 22:
        return {"ET0_mm_day": np.nan, "RHmin_pct": np.nan, "wind2_mean_ms": np.nan, "valid_hours": n, "status": "INSUFFICIENT_VALID_HOURS"}
    t, rh, wind, rs = [g[c].to_numpy(float) for c in cols]
    if np.any((rh < 0)|(rh > 100)) or np.any(wind < 0) or np.any(rs < 0):
        raise ValueError("Humidity, wind, or radiation is outside its valid range.")
    tmin, tmax, tmean = float(t.min()), float(t.max()), float(t.mean())
    ea = float(np.mean(saturation_vapour_pressure(t)*rh/100))
    es = float((saturation_vapour_pressure(tmax)+saturation_vapour_pressure(tmin))/2)
    vpd = max(0, es-ea)
    u10 = float(wind.mean())
    u2 = u10 * 4.87 / math.log(67.8*10-5.42)
    rs_day = float(rs.sum()*3600/1_000_000)
    rns = (1-0.23)*rs_day
    pressure = 101.3*((293-0.0065*elevation_m)/293)**5.26
    gamma = 0.000665*pressure
    delta = 4098*float(saturation_vapour_pressure(tmean))/((tmean+237.3)**2)
    tmax_k, tmin_k = tmax+273.16, tmin+273.16
    ra = float(ra_fao56(latitude, pd.Timestamp(group["timestamp"].iloc[0]).dayofyear))
    rso = (0.75+2e-5*elevation_m)*ra
    cloud_ratio = max(0.3, min(1.0, rs_day/rso)) if rso > 0 else 0.3
    rnl = 4.903e-9*((tmax_k**4+tmin_k**4)/2)*(0.34-0.14*math.sqrt(max(0,ea)))*(1.35*cloud_ratio-0.35)
    rn = rns-rnl
    et0 = max(0, (0.408*delta*rn + gamma*(900/(tmean+273))*u2*vpd)/(delta+gamma*(1+0.34*u2)))
    return {"ET0_mm_day": et0, "Tmean_C":tmean, "Tmin_C":tmin, "Tmax_C":tmax,
            "RHmean_pct":float(rh.mean()), "RHmin_pct":float(rh.min()), "wind10_mean_ms":u10,
            "wind2_mean_ms":u2, "Rs_MJ_m2_day":rs_day, "Rn_MJ_m2_day":rn,
            "valid_hours":n, "status":"OK"}

def run_uploaded(upload, latitude, elevation_m, start_date, end_date):
    df = pd.read_csv(upload)
    required = {"timestamp", "tas_C", "hurs_pct", "sfcWind_ms", "rsds_W_m2"}
    missing = required-set(df.columns)
    if missing:
        raise ValueError("Missing CSV columns: " + ", ".join(sorted(missing)))
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    df = df[(df.timestamp.dt.date >= start_date)&(df.timestamp.dt.date <= end_date)].copy()
    if df.empty: raise ValueError("No hourly records fall within the selected date range.")
    df["date"] = df.timestamp.dt.date
    rows=[]
    for day, grp in df.groupby("date", sort=True):
        rows.append({"date":day.isoformat(), **calculate_daily(grp, latitude, elevation_m)})
    return pd.DataFrame(rows)

result = None
if mode == "Automatic HOSTRADA download":
    st.write("The app downloads the four monthly HOSTRADA NetCDF variables (tas, hurs, sfcWind, rsds), extracts the selected grid cell, and calculates daily ET₀.")
    st.caption("Automatic mode requires the GDAL system library used by the original script. It may take several minutes for long date ranges.")
    if st.button("Download data and calculate ET₀", type="primary", disabled=end < start):
        try:
            from hostrada_et0 import process
            with tempfile.TemporaryDirectory() as td:
                outdir = Path(td)
                with st.spinner("Downloading HOSTRADA files and calculating daily ET₀…"):
                    xlsx = process(float(lat), float(lon), start, end, False, outdir, float(elevation))
                result = pd.read_excel(xlsx, sheet_name="Daily ET0")
                st.session_state["result_df"] = result
                st.session_state["run_meta"] = {"latitude":lat,"longitude":lon,"elevation":elevation,"start":str(start),"end":str(end),"mode":mode,"tree_height_m":tree_height,"calculate_kcb":calculate_kcb}
        except Exception as exc:
            st.error(f"Automatic calculation failed: {exc}")
            st.exception(exc)
elif mode == "Upload existing HOSTRADA NetCDF files":
    st.write("Upload the four HOSTRADA NetCDF variables for every month in the selected date range: tas, hurs, sfcWind, and rsds. Keep the original DWD filenames so the app can identify variable and month.")
    nc_files = st.file_uploader("HOSTRADA monthly NetCDF files", type=["nc"], accept_multiple_files=True)
    if st.button("Calculate from uploaded NetCDF files", type="primary", disabled=(not nc_files or end < start)):
        try:
            import calendar, re, tempfile
            from hostrada_et0 import point_to_grid, read_hourly_pixel, calculate_daily_et0, PARAMETERS
            from datetime import datetime, timedelta
            from pathlib import Path
            with tempfile.TemporaryDirectory() as td:
                temp = Path(td)
                paths = {}
                pattern = re.compile(r"^(tas|hurs|sfcWind|rsds)_1hr_HOSTRADA-v1-0_BE_gn_(\d{4})(\d{2})0100-", re.I)
                for uf in nc_files:
                    m = pattern.match(uf.name)
                    if not m: continue
                    var, yy, mm = m.group(1), int(m.group(2)), int(m.group(3))
                    dest = temp / uf.name
                    dest.write_bytes(uf.getvalue())
                    paths[(var,yy,mm)] = dest
                x, y, _, _ = point_to_grid(float(lat), float(lon))
                days = [start + timedelta(days=i) for i in range((end-start).days+1)]
                rows=[]
                with st.spinner("Reading uploaded NetCDF data and calculating ET₀…"):
                    for day in days:
                        key=(day.year,day.month)
                        if not all((v,*key) in paths for v in PARAMETERS):
                            rows.append({"date":day.isoformat(),"ET0_mm_day":np.nan,"valid_hours":0,"status":"MISSING_MONTHLY_VARIABLE_FILE"})
                            continue
                        arrays={}
                        month_start=datetime(day.year,day.month,1)
                        expected=calendar.monthrange(day.year,day.month)[1]*24
                        for var in PARAMETERS:
                            arr=read_hourly_pixel(paths[(var,*key)],var,x,y,month_start,expected)
                            a=(day.day-1)*24; arrays[var]=arr[a:a+24]
                        val,n=calculate_daily_et0(arrays["tas"],arrays["hurs"],arrays["sfcWind"],arrays["rsds"],day,float(lat),float(elevation))
                        rows.append({"date":day.isoformat(),**(val if val else {"ET0_mm_day":np.nan,"valid_hours":n}),"status":"OK" if val else "INSUFFICIENT_VALID_HOURS"})
                result=pd.DataFrame(rows)
                st.session_state["result_df"] = result
                st.session_state["run_meta"] = {"latitude":lat,"longitude":lon,"elevation":elevation,"start":str(start),"end":str(end),"mode":mode,"tree_height_m":tree_height,"calculate_kcb":calculate_kcb}
        except Exception as exc:
            st.error(f"Could not process uploaded NetCDF files: {exc}")
            st.exception(exc)
else:
    st.write("Upload a CSV containing one row per hour. Use the exact column names and units shown below.")
    st.code("timestamp,tas_C,hurs_pct,sfcWind_ms,rsds_W_m2\n2025-01-01 00:00,4.2,85,2.1,0", language="csv")
    uploaded = st.file_uploader("Hourly weather CSV", type=["csv"])
    if st.button("Calculate daily ET₀", type="primary", disabled=(uploaded is None or end < start)):
        try:
            with st.spinner("Calculating daily ET₀…"):
                result = run_uploaded(uploaded, float(lat), float(elevation), start, end)
            st.session_state["result_df"] = result
            st.session_state["run_meta"] = {"latitude":lat,"longitude":lon,"elevation":elevation,"start":str(start),"end":str(end),"mode":mode,"tree_height_m":tree_height,"calculate_kcb":calculate_kcb}
        except Exception as exc:
            st.error(str(exc))

if "result_df" in st.session_state:
    df = st.session_state["result_df"].copy()
    if "date" not in df.columns and "Date" in df.columns: df = df.rename(columns={"Date":"date"})
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if "ET0_mm_day" in df.columns:
        df["ET0_mm_day"] = pd.to_numeric(df["ET0_mm_day"], errors="coerce")
    # Optional adaptation of the existing project Kcb script; explicitly a full-cover proxy.
    if st.session_state.get("run_meta", {}).get("calculate_kcb") and {"ET0_mm_day", "RHmin_pct", "wind2_mean_ms"}.issubset(df.columns):
        h = float(st.session_state["run_meta"].get("tree_height_m", 5.0))
        kcb_h = min(1.0 + 0.1 * (h - 2.0), 1.20)
        climate = (0.04 * (pd.to_numeric(df["wind2_mean_ms"], errors="coerce") - 2.0)
                   - 0.004 * (pd.to_numeric(df["RHmin_pct"], errors="coerce") - 45.0)) * (h / 3.0) ** 0.3
        df["Kcb_full_cover_proxy"] = kcb_h + climate
        df["ETcb_full_cover_proxy_mm_day"] = df["ET0_mm_day"] * df["Kcb_full_cover_proxy"]
    st.subheader("Daily results")
    c1,c2,c3 = st.columns(3)
    c1.metric("Valid days", int(df["ET0_mm_day"].notna().sum()) if "ET0_mm_day" in df else 0)
    c2.metric("Mean ET₀ (mm/day)", f"{df['ET0_mm_day'].mean():.2f}" if "ET0_mm_day" in df and df["ET0_mm_day"].notna().any() else "—")
    c3.metric("Total ET₀ (mm)", f"{df['ET0_mm_day'].sum():.1f}" if "ET0_mm_day" in df else "—")
    if "date" in df.columns and "ET0_mm_day" in df.columns:
        chart = px.line(df, x="date", y="ET0_mm_day", title="Daily reference evapotranspiration (ET₀)", labels={"date":"Date","ET0_mm_day":"ET₀ (mm/day)"})
        st.plotly_chart(chart, use_container_width=True)
        monthly = df.set_index("date")["ET0_mm_day"].resample("MS").sum(min_count=1).rename("ET0_monthly_sum_mm").reset_index()
        st.plotly_chart(px.bar(monthly, x="date", y="ET0_monthly_sum_mm", title="Monthly sum of daily ET₀", labels={"date":"Month","ET0_monthly_sum_mm":"ET₀ (mm/month)"}), use_container_width=True)
    if "ETcb_full_cover_proxy_mm_day" in df.columns:
        st.plotly_chart(px.line(df, x="date", y="ETcb_full_cover_proxy_mm_day", title="Provisional full-cover ETcb proxy (not isolated-tree demand)", labels={"date":"Date","ETcb_full_cover_proxy_mm_day":"ETcb proxy (mm/day)"}), use_container_width=True)
    st.dataframe(df, use_container_width=True, hide_index=True)
    meta = st.session_state.get("run_meta", {})
    st.caption(f"Elevation used: {meta.get('elevation','—')} m (verify) · Method: daily FAO-56 Penman–Monteith · Input mode: {meta.get('mode','—')}")
    csv = df.to_csv(index=False, date_format="%Y-%m-%d").encode("utf-8-sig")
    st.download_button("Download daily results (CSV)", data=csv, file_name="ET0_daily_results.csv", mime="text/csv")
    st.download_button("Download run settings (JSON)", data=__import__('json').dumps(meta, indent=2).encode(), file_name="calculation_settings.json", mime="application/json")

with st.expander("Method and limitations"):
    st.markdown("""
- **ET₀** is reference evapotranspiration calculated with the project's daily FAO-56 Penman–Monteith workflow.
- Automatic mode reads HOSTRADA hourly variables: air temperature, relative humidity, wind speed at 10 m, and downward shortwave radiation.
- At least 22 valid hourly records are required per day.
- Wind speed is converted from 10 m to 2 m using the FAO-56 logarithmic conversion.
- Elevation affects atmospheric pressure and clear-sky radiation. The default 55 m is unverified.
- **ET₀ is not the tree's actual water consumption or irrigation requirement.** The optional Kcb output reproduces a provisional full-cover climate-adjustment approach from the project script; it is not validated for an isolated street tree. Effective rainfall, substrate storage, and irrigation assumptions are still required for irrigation demand.
- Uploaded CSV mode expects hourly values already converted to °C, %, m/s, and W/m² respectively.
""")
