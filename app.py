import io, math
import numpy as np
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt
import holidays
from ortools.sat.python import cp_model
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.chart import BarChart, LineChart, Reference

st.set_page_config(page_title="WFM · Pronóstico y Roster", layout="wide")
st.title("📞 WFM — Pronóstico, Dimensionamiento y Roster")

NOM = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
LIBRES = {p: {p, (p + 1) % 7} for p in range(7)}


def erlang_b(a, c):
    b = 1.0
    for k in range(1, a + 1):
        b = (c * b) / (k + c * b)
    return b


def erlang_c(a, c):
    b = erlang_b(a, c); r = c / a
    return b / (1 - r + r * b)


def nivel_servicio(a, c, aht, asa):
    return 0.0 if a <= c else 1 - erlang_c(a, c) * math.exp(-(a - c) * asa / aht)


def nivel_atencion(ag, carga, aht, pac, NMAX=250):
    # Erlang A (con abandono): fracción de llamadas atendidas (no abandonadas)
    if carga <= 0:
        return 1.0
    lam = carga / aht; mu = 1.0 / aht; theta = 1.0 / pac
    p = [1.0]
    for n in range(1, NMAX + 1):
        muerte = n * mu if n <= ag else ag * mu + (n - ag) * theta
        p.append(p[-1] * lam / muerte)
    S = sum(p)
    aband = sum(p[n] * max(0, (n - ag)) * theta for n in range(len(p))) / S
    return 1 - aband / lam


def agregar_crudo(file_bytes):
    # Agrega un Excel CRUDO (estructura del histórico) al formato único.
    raw = pd.read_excel(io.BytesIO(file_bytes), sheet_name="HISTORICO")
    raw.columns = [str(c).strip() for c in raw.columns]
    cols = {"Fecha": "fecha", "Hora": "hora", "Cola Corta": "cola",
            "Entrantes": "entrantes", "Atendidas": "atendidas", "Abandonadas": "abandonadas"}
    falta = [c for c in cols if c not in raw.columns]
    if falta:
        raise ValueError("Faltan columnas en el Excel: " + ", ".join(falta))
    d = raw[list(cols)].rename(columns=cols)
    d["fecha"] = pd.to_datetime(d["fecha"], errors="coerce").dt.normalize()
    d["hora"] = pd.to_numeric(d["hora"], errors="coerce")
    for c in ["entrantes", "atendidas", "abandonadas"]:
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0)
    d = d.dropna(subset=["fecha", "hora"])
    d["hora"] = d["hora"].astype(int)
    d = d[(d["hora"] >= 0) & (d["hora"] <= 23)]
    agg = d.groupby(["fecha", "hora", "cola"])[["entrantes", "atendidas", "abandonadas"]].sum().reset_index()
    return agg[agg["entrantes"] > 0]


# ---------------- Selector de vista ----------------
vista = st.sidebar.radio("Vista", ["Planificación (pronóstico y roster)", "Dashboard histórico",
                                   "Proyección anual", "Plan de capacidad anual", "Actualizar histórico"])

# Histórico compartido: se sube UNA vez y persiste al cambiar de vista
_hf = st.sidebar.file_uploader("Histórico único (historico.csv)", type=["csv"], key="hist")
if _hf is not None:
    st.session_state["hist_bytes"] = _hf.getvalue()
HIST = st.session_state.get("hist_bytes")

if vista == "Dashboard histórico":
    st.header("📊 Dashboard histórico por cola")
    if HIST is None:
        st.info("Sube el histórico único en la barra lateral para ver el dashboard.")
        st.stop()
    d = pd.read_csv(io.BytesIO(HIST))
    d.columns = [c.strip().lower() for c in d.columns]
    d["fecha"] = pd.to_datetime(d["fecha"], errors="coerce")
    d = d.dropna(subset=["fecha"])
    for c in ["entrantes", "atendidas", "abandonadas"]:
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0)
    d["año"] = d["fecha"].dt.year
    d["mes"] = d["fecha"].dt.month
    d["semana"] = d["fecha"].dt.isocalendar().week.astype(int)
    d["dow"] = d["fecha"].dt.dayofweek

    f1, f2, f3, f4 = st.columns(4)
    año = f1.selectbox("Año", ["Todos"] + sorted(d["año"].unique()))
    mes = f2.selectbox("Mes", ["Todos"] + list(range(1, 13)))
    sem = f3.selectbox("Semana ISO", ["Todas"] + sorted(d["semana"].unique()))
    dias_sel = f4.multiselect("Día de semana", NOM, default=NOM)
    colas = st.multiselect("Colas", sorted(d["cola"].unique()), default=sorted(d["cola"].unique()))

    f = d.copy()
    if año != "Todos":
        f = f[f["año"] == año]
    if mes != "Todos":
        f = f[f["mes"] == mes]
    if sem != "Todas":
        f = f[f["semana"] == sem]
    f = f[f["dow"].isin([NOM.index(x) for x in dias_sel])]
    f = f[f["cola"].isin(colas)]
    if f.empty:
        st.warning("No hay datos para esos filtros.")
        st.stop()

    ent, at, ab = f["entrantes"].sum(), f["atendidas"].sum(), f["abandonadas"].sum()
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Entrantes", f"{int(ent):,}")
    k2.metric("Atendidas", f"{int(at):,}")
    k3.metric("Abandonadas", f"{int(ab):,}")
    k4.metric("% Atención", f"{at / ent * 100:.1f}%" if ent else "—")

    st.subheader("Por cola")
    by = f.groupby("cola")[["entrantes", "atendidas", "abandonadas"]].sum().reset_index()
    by["% atención"] = (by["atendidas"] / by["entrantes"].replace(0, np.nan) * 100).round(1)
    by = by.sort_values("entrantes", ascending=False)
    st.dataframe(by, use_container_width=True)

    x = np.arange(len(by)); w = 0.4
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.bar(x - w / 2, by["atendidas"], w, label="Atendidas", color="#2A9D8F")
    ax.bar(x + w / 2, by["abandonadas"], w, label="Abandonadas", color="#E76F51")
    ax.set_xticks(x); ax.set_xticklabels(by["cola"], rotation=30, ha="right"); ax.legend()
    ax.set_ylabel("Llamadas")
    st.pyplot(fig)

    st.subheader("% Atención en el tiempo")
    serie = f.groupby("fecha")[["entrantes", "atendidas"]].sum()
    serie["pat"] = serie["atendidas"] / serie["entrantes"].replace(0, np.nan) * 100
    fig2, ax2 = plt.subplots(figsize=(10, 3))
    ax2.plot(serie.index, serie["pat"], color="#1F6F66", linewidth=1.5)
    ax2.axhline(96, color="#E76F51", linestyle="--", linewidth=1, label="Objetivo 96%")
    ax2.set_ylabel("% Atención"); ax2.legend()
    st.pyplot(fig2)

    if "hora" in f.columns:
        st.subheader("Por franja horaria")
        fr = f.groupby("hora")[["entrantes", "atendidas", "abandonadas"]].sum()
        fr["pat"] = fr["atendidas"] / fr["entrantes"].replace(0, np.nan) * 100
        fig3, ax3 = plt.subplots(figsize=(10, 3.5))
        ax3.bar(fr.index, fr["entrantes"], color="#C9D6D3", label="Entrantes")
        ax3.set_xlabel("Hora"); ax3.set_ylabel("Entrantes"); ax3.set_xticks(range(0, 24, 2))
        ax3b = ax3.twinx()
        ax3b.plot(fr.index, fr["pat"], color="#1F6F66", linewidth=2, label="% Atención")
        ax3b.set_ylabel("% Atención"); ax3b.set_ylim(0, 100)
        ax3.legend(loc="upper left"); ax3b.legend(loc="upper right")
        st.pyplot(fig3)
        st.caption("Barras = entrantes por hora · línea = % atención por hora.")
    st.stop()


if vista == "Actualizar histórico":
    st.header("➕ Actualizar histórico")
    st.write("Sube el histórico maestro en la barra lateral y aquí un Excel con **solo los días nuevos** "
             "(misma estructura que tu histórico). Se fusionan y descargas el maestro actualizado.")
    if HIST is None:
        st.info("Primero sube el histórico maestro (historico.csv) en la barra lateral.")
        st.stop()
    maestro = pd.read_csv(io.BytesIO(HIST))
    maestro.columns = [c.strip().lower() for c in maestro.columns]
    maestro["fecha"] = pd.to_datetime(maestro["fecha"], errors="coerce").dt.normalize()
    st.caption(f"Maestro actual: {len(maestro):,} filas · {maestro['fecha'].min().date()} → {maestro['fecha'].max().date()}")
    nuevos = st.file_uploader("Días nuevos (Excel crudo, hoja HISTORICO)", type=["xlsx", "xls"], key="nuevos")
    if nuevos is None:
        st.stop()
    try:
        nuevo_agg = agregar_crudo(nuevos.getvalue())
    except Exception as e:
        st.error(f"No pude leer el Excel: {e}")
        st.stop()
    st.caption(f"Días nuevos: {nuevo_agg['fecha'].min().date()} → {nuevo_agg['fecha'].max().date()} ({nuevo_agg['fecha'].nunique()} días)")
    comb = pd.concat([maestro, nuevo_agg], ignore_index=True)
    comb["fecha"] = pd.to_datetime(comb["fecha"]).dt.normalize()
    comb = comb.drop_duplicates(["fecha", "hora", "cola"], keep="last").sort_values(["fecha", "hora", "cola"])
    st.success(f"Maestro actualizado: {len(comb):,} filas · hasta {comb['fecha'].max().date()} "
               f"(+{len(comb) - len(maestro):,} filas)")
    csv = comb.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Descargar historico.csv actualizado", csv, "historico.csv", "text/csv")
    st.session_state["hist_bytes"] = csv
    st.caption("Ya quedó cargado en la sesión: las otras vistas usan el maestro actualizado. "
               "Igual descárgalo para reemplazar tu copia local.")
    st.stop()


if vista == "Proyección anual":
    st.header("📅 Proyección anual (escenarios)")
    if HIST is None:
        st.info("Sube el histórico único en la barra lateral para ver la proyección.")
        st.stop()
    d = pd.read_csv(io.BytesIO(HIST))
    d.columns = [c.strip().lower() for c in d.columns]
    d["fecha"] = pd.to_datetime(d["fecha"], errors="coerce")
    vc = "entrantes" if "entrantes" in d.columns else "vol"
    d[vc] = pd.to_numeric(d[vc], errors="coerce").fillna(0)
    d = d.dropna(subset=["fecha"])
    d["fecha"] = d["fecha"].dt.normalize()
    meses_nom = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    if "cola" not in d.columns:
        d["cola"] = "TOTAL"

    # Filtro por colas (con su % de entrantes)
    share = d.groupby("cola")[vc].sum().sort_values(ascending=False)
    spct = (share / share.sum() * 100).round(1)
    ops = [f"{c}  ({spct[c]}%)" for c in share.index]
    sel = st.multiselect("Colas a incluir (con su % de entrantes)", ops, default=ops)
    colas_sel = [o.rsplit("  (", 1)[0] for o in sel] or list(share.index)
    d = d[d["cola"].isin(colas_sel)]
    st.caption(f"Incluyes el {round(share[colas_sel].sum() / share.sum() * 100, 1)}% del tráfico total.")

    dia = d.groupby("fecha")[vc].sum()
    if dia.empty:
        st.warning("No hay datos con esos filtros.")
        st.stop()
    años = sorted({t.year for t in dia.index})

    st.subheader("Parámetros de proyección")
    c1, c2, c3 = st.columns(3)
    año = c1.selectbox("Año a proyectar", años, index=len(años) - 1)
    scope_a = c2.selectbox("Festivos", ["Nacional España", "Cataluña (Barcelona)"])
    K = int(c3.number_input("Semanas de historia (K)", 2, 12, 4, 1))
    recencia = st.slider("Peso a lo reciente", 0.0, 0.9, 0.0, 0.1,
                         help="0 = todas las semanas pesan igual · más alto = las recientes mandan")
    with st.expander("Ajuste por mes (%) — aplica a todas las colas ese mes"):
        mc = st.columns(6)
        adj_mes = [mc[i % 6].number_input(meses_nom[i], -50, 100, 0, 5, key=f"am{i}") / 100.0 for i in range(12)]
    with st.expander("Ajuste por cola (%) — aplica a esa cola todo el año"):
        cc = st.columns(3)
        adj_cola = {c: cc[i % 3].number_input(c, -50, 100, 0, 5, key=f"ac{i}") / 100.0
                    for i, c in enumerate(colas_sel)}

    # Peso de cada cola -> el ajuste por cola escala el total según su participación
    sh = share[colas_sel] / share[colas_sel].sum()
    cola_factor = float(sum(sh[c] * (1 + adj_cola.get(c, 0.0)) for c in colas_sel))

    if scope_a == "Cataluña (Barcelona)":
        ES = holidays.Spain(years=range(min(años) - 1, max(años) + 2), subdiv="CT")
    else:
        ES = holidays.Spain(years=range(min(años) - 1, max(años) + 2))

    def fest(t):
        return t.date() in ES

    def proj_dia(t):
        wd = 6 if fest(t) else t.dayofweek
        s = dia[(dia.index < t) & (dia.index.dayofweek == wd)]
        if wd != 6:
            s = s[[not fest(x) for x in s.index]]
        s = s.tail(K)
        if len(s) == 0:
            return 0.0
        v = s.values[::-1]
        w = np.array([(1 - recencia) ** j for j in range(len(v))])
        return float((v * w).sum() / w.sum())

    # Promedios históricos
    st.subheader("Promedios históricos (colas seleccionadas)")
    g1, g2 = st.columns(2)
    pdow = dia.groupby(dia.index.dayofweek).mean().reindex(range(7)).fillna(0)
    figd, axd = plt.subplots(figsize=(5, 3))
    axd.bar([NOM[i] for i in range(7)], pdow.values, color="#2A9D8F")
    axd.set_ylabel("Entrantes/día"); axd.set_title("Promedio por día de semana")
    g1.pyplot(figd)
    dia_año = dia[dia.index.year == año]
    sem = dia_año.resample("W").sum()
    import matplotlib.dates as mdates
    figs, axs = plt.subplots(figsize=(5, 3))
    axs.plot(sem.index, sem.values, color="#1F6F66", marker="o", markersize=3)
    axs.set_ylabel("Entrantes/semana"); axs.set_title(f"Volumen por semana {año}")
    axs.grid(True, alpha=0.3)
    axs.xaxis.set_major_locator(mdates.MonthLocator()); axs.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    g2.pyplot(figs)

    # Proyección mensual: Base (sin ajustes) vs Escenario (con tus ajustes)
    filas = []
    efectivo = {}
    for mth in range(1, 13):
        ini = pd.Timestamp(year=año, month=mth, day=1); fin = ini + pd.offsets.MonthEnd(0)
        dias_mes = pd.date_range(ini, fin)
        real_part = pbase = 0.0
        for t in dias_mes:
            if t in dia.index:
                real_part += float(dia.loc[t])
            else:
                pbase += proj_dia(t)
        n_real = sum(1 for t in dias_mes if t in dia.index)
        estado = "REAL" if n_real >= len(dias_mes) else ("EN CURSO" if n_real > 0 else "PROYECTADO")
        f_mes = cola_factor * (1 + adj_mes[mth - 1])          # factor del escenario para días proyectados
        base_tot = real_part + pbase
        scn_tot = real_part + pbase * f_mes
        efectivo[mth] = f_mes - 1.0
        delta = (scn_tot / base_tot - 1) * 100 if base_tot > 0 else 0.0
        filas.append({"Mes": meses_nom[mth - 1], "Estado": estado, "Real": int(round(real_part)),
                      "Base": int(round(base_tot)), "Escenario": int(round(scn_tot)), "Δ%": round(delta, 1)})
    st.session_state["ajuste_mes"] = efectivo
    tab = pd.DataFrame(filas)

    st.subheader("Comparador: Base vs Escenario")
    m1, m2, m3 = st.columns(3)
    tb = int(tab["Base"].sum()); tsc = int(tab["Escenario"].sum())
    m1.metric(f"Base {año}", f"{tb:,}")
    m2.metric(f"Escenario {año}", f"{tsc:,}")
    m3.metric("Diferencia", f"{tsc - tb:+,}", f"{(tsc / tb - 1) * 100:+.1f}%" if tb else "—")
    st.dataframe(tab, use_container_width=True)

    x = np.arange(12); w = 0.38
    figp, axp = plt.subplots(figsize=(11, 4))
    axp.bar(x - w / 2, tab["Base"], w, label="Base", color="#C9D6D3")
    axp.bar(x + w / 2, tab["Escenario"], w, label="Escenario", color="#2A9D8F")
    axp.set_xticks(x); axp.set_xticklabels(tab["Mes"]); axp.set_ylabel("Llamadas"); axp.legend()
    axp.set_title(f"Volumen mensual {año}: base vs escenario")
    st.pyplot(figp)
    st.caption("Base = proyección sin ajustes · Escenario = con tus ajustes. El ajuste por cola escala según el "
               "peso (%) de cada cola. El ajuste efectivo de cada mes se usa también en Planificación.")
    st.stop()


# ---------------- Parámetros ----------------
st.sidebar.header("Parámetros")
AHT = st.sidebar.number_input("AHT (seg)", 60, 1200, 420, 10)
SLA = st.sidebar.slider("SLA objetivo", 0.50, 0.99, 0.80, 0.01)
ASA = st.sidebar.number_input("ASA objetivo (seg)", 5, 120, 20, 5)
OCC = st.sidebar.slider("Ocupación tope (OCC)", 0.40, 0.95, 0.65, 0.01)
UTL = st.sidebar.slider("Utilización (UTL)", 0.60, 1.00, 0.88, 0.01)
ABS = st.sidebar.slider("Absentismo", 0.00, 0.40, 0.15, 0.01)
ESP_MAX = st.sidebar.number_input("Agentes España (máx, solo L–V)", 0, 200, 12, 1)
LARGO = int(st.sidebar.number_input("Duración turno (h)", 6, 12, 9, 1))
NDA_OBJ = st.sidebar.slider("NDA objetivo (nivel de atención)", 0.80, 0.999, 0.96, 0.005)
PACIENCIA = st.sidebar.number_input("Paciencia media (seg)", 20, 600, 90, 10)


# ---------------- Núcleo: dimensionar + roster ----------------
def dimension_roster(largo, AHT, SLA, ASA, OCC, UTL, ESP_MAX, LARGO, NDA_OBJ, PACIENCIA):
    largo = largo.copy()
    largo["dow"] = pd.to_datetime(largo["fecha"]).dt.dayofweek
    volmax = {dw: [0.0] * 24 for dw in range(7)}
    for (dw, h), g in largo.groupby(["dow", "intervalo"]):
        volmax[dw][int(h)] = float(g["volumen"].max())
    peak = {dw: [0] * 24 for dw in range(7)}
    occ = {dw: [0.0] * 24 for dw in range(7)}
    nda = {dw: [1.0] * 24 for dw in range(7)}
    for dw in range(7):
        for h in range(24):
            ca = volmax[dw][h] * AHT / 3600
            if ca <= 0:
                continue
            a = int(ca) + 1
            while nivel_servicio(a, ca, AHT, ASA) < SLA:
                a += 1
            en = max(a, math.ceil(ca / OCC))
            while nivel_atencion(en, ca, AHT, PACIENCIA) < NDA_OBJ:   # piso por NDA
                en += 1
            peak[dw][h] = math.ceil(en / UTL)
            occ[dw][h] = ca / en
            nda[dw][h] = nivel_atencion(en, ca, AHT, PACIENCIA)

    H = 24
    turnos = {ini: [(ini + k) % H for k in range(LARGO)] for ini in range(H)}
    m = cp_model.CpModel()
    xc = {(t, p): m.NewIntVar(0, 300, f"c{t}_{p}") for t in turnos for p in range(7)}
    xe = {t: m.NewIntVar(0, 300, f"e{t}") for t in turnos}
    for dw in range(7):
        for h in range(H):
            col = sum(xc[(t, p)] for t in turnos for p in range(7) if dw not in LIBRES[p] and h in turnos[t])
            esp = sum(xe[t] for t in turnos if dw not in LIBRES[5] and h in turnos[t])
            m.Add(col + esp >= peak[dw][h])
    m.Add(sum(xe.values()) <= int(ESP_MAX))
    m.Minimize(sum(xc.values()) * 100 - sum(xe.values()))
    sv = cp_model.CpSolver(); sv.Solve(m)

    xe_s = {str(t): sv.Value(xe[t]) for t in turnos if sv.Value(xe[t]) > 0}
    xc_s = {f"{t}_{p}": sv.Value(xc[(t, p)]) for t in turnos for p in range(7) if sv.Value(xc[(t, p)]) > 0}
    te = sum(xe_s.values()); tc = sum(xc_s.values())

    def crew(req):
        mm = cp_model.CpModel()
        xx = {t: mm.NewIntVar(0, 200, str(t)) for t in turnos}
        for h in range(H):
            mm.Add(sum(xx[t] for t in turnos if h in turnos[t]) >= req[h])
        mm.Minimize(sum(xx.values()))
        ss = cp_model.CpSolver(); ss.Solve(mm)
        return sum(ss.Value(v) for v in xx.values())
    cw = max(crew(peak[5]), crew(peak[6]))
    vals = [nda[dw][h] for dw in range(7) for h in range(24) if peak[dw][h] > 0]
    return {"peak": peak, "occ": occ, "nda": nda, "nda_min": min(vals) if vals else 1.0,
            "xe": xe_s, "xc": xc_s, "te": te, "tc": tc,
            "crew": cw, "rot": cw * 4 // 2, "total": int(largo["volumen"].sum())}


# ---------------- Producir 'largo' (dos orígenes) ----------------
def largo_desde_crosstab(file_bytes):
    raw = pd.read_excel(io.BytesIO(file_bytes))
    fechas = raw.iloc[0]; first = raw.columns[0]
    d = raw.drop(index=0)
    d = d[d[first].astype(str).str.lower() != "total"].rename(columns={first: "intervalo"})
    L = d.melt(id_vars="intervalo", var_name="col", value_name="volumen")
    L["fecha"] = pd.to_datetime(L["col"].map(fechas), errors="coerce")
    L = L.dropna(subset=["fecha"])
    L["volumen"] = pd.to_numeric(L["volumen"], errors="coerce")
    L = L.dropna(subset=["volumen"])

    def hh(x):
        try:
            return int(x)
        except Exception:
            return int(str(x).split(":")[0])
    L["intervalo"] = L["intervalo"].apply(hh)
    L = L[(L["intervalo"] >= 0) & (L["intervalo"] <= 23)]
    return L[["fecha", "intervalo", "volumen"]]


def largo_desde_historico(file_bytes, mes, scope, K, semanas, ajuste=0.0):
    # Archivo ÚNICO (CSV: fecha, hora, cola, entrantes, atendidas, abandonadas)
    df = pd.read_csv(io.BytesIO(file_bytes))
    df.columns = [c.strip().lower() for c in df.columns]
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df["hora"] = pd.to_numeric(df["hora"], errors="coerce")
    volcol = "entrantes" if "entrantes" in df.columns else "vol"
    df[volcol] = pd.to_numeric(df[volcol], errors="coerce").fillna(0)
    df = df.dropna(subset=["fecha", "hora"])
    df["hora"] = df["hora"].astype(int)
    hh = df.groupby(["fecha", "hora"])[volcol].sum().reset_index()
    hh.columns = ["fecha", "hora", "vol"]
    dia = hh.groupby("fecha")["vol"].sum()

    if scope == "Cataluña (Barcelona)":
        ES = holidays.Spain(years=range(2023, 2028), subdiv="CT")
    else:
        ES = holidays.Spain(years=range(2023, 2028))

    def fest(t):
        return t.date() in ES

    def total_diario(t):
        wd = 6 if fest(t) else t.dayofweek
        s = dia[(dia.index < t) & (dia.index.dayofweek == wd)]
        if wd != 6:
            s = s[[not fest(d) for d in s.index]]
        return s.tail(K).mean()

    ini = pd.Timestamp(mes + "-01")
    fin_datos = hh["fecha"].max()
    ref = min(ini, fin_datos + pd.Timedelta(days=1))   # no mirar más allá de los datos
    win = hh[(hh["fecha"] < ref) & (hh["fecha"] >= ref - pd.Timedelta(weeks=semanas))]
    win = win[[not fest(d) for d in win["fecha"]]]
    if win.empty:
        win = hh[hh["fecha"] >= fin_datos - pd.Timedelta(weeks=semanas)]
        win = win[[not fest(d) for d in win["fecha"]]]
    perfil = win.groupby([win["fecha"].dt.dayofweek, "hora"])["vol"].mean().unstack(fill_value=0)
    perfil = perfil.div(perfil.sum(axis=1), axis=0)

    filas = []
    for t in pd.date_range(ini, ini + pd.offsets.MonthEnd(0)):
        dt = total_diario(t)
        wd = 6 if fest(t) else t.dayofweek
        for hr in range(24):
            filas.append({"fecha": t, "intervalo": hr, "volumen": round(dt * perfil.loc[wd].get(hr, 0) * (1 + ajuste))})
    return pd.DataFrame(filas)


@st.cache_data(show_spinner="Dimensionando y optimizando…")
def resolver_crosstab(file_bytes, AHT, SLA, ASA, OCC, UTL, ESP_MAX, LARGO, NDA_OBJ, PACIENCIA):
    return dimension_roster(largo_desde_crosstab(file_bytes), AHT, SLA, ASA, OCC, UTL, ESP_MAX, LARGO, NDA_OBJ, PACIENCIA)


@st.cache_data(show_spinner="Pronosticando, dimensionando y optimizando…")
def resolver_historico(file_bytes, mes, scope, K, semanas, AHT, SLA, ASA, OCC, UTL, ESP_MAX, LARGO, NDA_OBJ, PACIENCIA, ajuste=0.0):
    return dimension_roster(largo_desde_historico(file_bytes, mes, scope, K, semanas, ajuste),
                            AHT, SLA, ASA, OCC, UTL, ESP_MAX, LARGO, NDA_OBJ, PACIENCIA)


def turnos_dict(largo):
    return {ini: [(ini + k) % 24 for k in range(largo)] for ini in range(24)}


def cubierto(S, turnos, dw, h):
    cob = sum(v for k, v in S["xc"].items()
              if dw not in LIBRES[int(k.split("_")[1])] and h in turnos[int(k.split("_")[0])])
    cob += sum(v for k, v in S["xe"].items()
               if dw not in LIBRES[5] and h in turnos[int(k)])
    return cob


def build_excel(S, turnos, params):
    AHT, SLA, ASA, OCC, UTL, ABS = params
    FT = "Arial"
    HEAD = PatternFill("solid", start_color="1F6F66")
    HF = Font(name=FT, bold=True, color="FFFFFF")
    TITLE = Font(name=FT, bold=True, size=14, color="1F6F66")
    BOLD = Font(name=FT, bold=True); REG = Font(name=FT)
    CEN = Alignment(horizontal="center")
    thin = Side(style="thin", color="D9D9D9"); BORD = Border(thin, thin, thin, thin)

    def hrow(ws, row, cols):
        for j, c in enumerate(cols, 1):
            x = ws.cell(row, j, c); x.fill = HEAD; x.font = HF; x.alignment = CEN; x.border = BORD

    wb = Workbook()
    te, tc = S["te"], S["tc"]; peak = S["peak"]

    ws = wb.active; ws.title = "Resumen"
    ws["A1"] = "Roster de dimensionamiento"; ws["A1"].font = TITLE
    r = 3; ws.cell(r, 1, "PARÁMETROS").font = BOLD; r += 1
    for k, v, f in [("AHT (seg)", AHT, "0"), ("SLA", SLA, "0%"), ("ASA (seg)", ASA, "0"),
                    ("OCC", OCC, "0%"), ("UTL", UTL, "0%"), ("Absentismo", ABS, "0%")]:
        ws.cell(r, 1, k).font = REG; c = ws.cell(r, 2, v); c.font = REG; c.number_format = f; r += 1
    r += 1; ws.cell(r, 1, "PLANTILLA").font = BOLD; r += 1
    for k, v in [("España (L–V)", te), ("Colombia", tc), ("TOTAL presentes", te + tc),
                 ("En nómina (+absentismo)", math.ceil((te + tc) / (1 - ABS)))]:
        b = "TOTAL" in k or "nómina" in k
        ws.cell(r, 1, k).font = BOLD if b else REG
        ws.cell(r, 2, v).font = BOLD if b else REG; r += 1
    r += 1; hrow(ws, r, ["Día", "Trabajan", "Pico requerido"]); r += 1
    for dw in range(7):
        trab = sum(v for k, v in S["xc"].items() if dw not in LIBRES[int(k.split("_")[1])])
        trab += sum(v for k, v in S["xe"].items() if dw not in LIBRES[5])
        for j, v in enumerate([NOM[dw], trab, max(peak[dw])], 1):
            x = ws.cell(r, j, v); x.font = REG; x.border = BORD
            if j > 1:
                x.alignment = CEN
        r += 1
    ws.column_dimensions["A"].width = 30; ws.column_dimensions["B"].width = 14; ws.column_dimensions["C"].width = 16

    ws = wb.create_sheet("Plan_Turnos")
    hrow(ws, 1, ["País", "Inicio", "Fin", "Cantidad", "Días libres"])
    for w, col in zip([12, 9, 9, 11, 18], "ABCDE"):
        ws.column_dimensions[col].width = w
    r = 2
    L = len(turnos[0])

    def add(r, pais, t, cant, off):
        o = sorted(off)
        vals = [pais, f"{t:02d}:00", f"{(t + L) % 24:02d}:00", cant, f"{NOM[o[0]]}, {NOM[o[1]]}"]
        for j, v in enumerate(vals, 1):
            x = ws.cell(r, j, v); x.font = REG; x.border = BORD
            if j in (2, 3, 4):
                x.alignment = CEN
            if pais == "España":
                x.fill = PatternFill("solid", start_color="FCE4D6")
        return r + 1
    for k in sorted(S["xe"], key=int):
        r = add(r, "España", int(k), S["xe"][k], {5, 6})
    for k in sorted(S["xc"], key=lambda x: (int(x.split("_")[0]), int(x.split("_")[1]))):
        t, p = map(int, k.split("_")); r = add(r, "Colombia", t, S["xc"][k], LIBRES[p])
    ws.cell(r + 1, 1, "TOTAL").font = BOLD; ws.cell(r + 1, 4, f"=SUM(D2:D{r-1})").font = BOLD

    wsg = wb.create_sheet("Grafico")
    wsg["A1"] = "Programados vs Requerido — por día"; wsg["A1"].font = TITLE
    hdr = ["Hora"]
    for dw in range(7):
        hdr += [f"{NOM[dw]} Req", f"{NOM[dw]} Cub"]
    for j, c in enumerate(hdr, 1):
        x = wsg.cell(3, j, c); x.fill = HEAD; x.font = HF; x.alignment = CEN
    for h in range(24):
        wsg.cell(4 + h, 1, f"{h:02d}:00").font = REG
        for dw in range(7):
            wsg.cell(4 + h, 2 + dw * 2, peak[dw][h]).font = REG
            wsg.cell(4 + h, 3 + dw * 2, cubierto(S, turnos, dw, h)).font = REG
    wsg.column_dimensions["A"].width = 8
    arow = 30
    for dw in range(7):
        bar = BarChart(); bar.type = "col"; bar.title = f"{NOM[dw]} — programados vs requerido"
        bar.height = 6.5; bar.width = 20; bar.y_axis.title = "Agentes"
        bar.add_data(Reference(wsg, min_col=2 + dw * 2, min_row=3, max_row=27), titles_from_data=True)
        bar.set_categories(Reference(wsg, min_col=1, min_row=4, max_row=27))
        bar.series[0].graphicalProperties.solidFill = "C9D6D3"
        ln = LineChart()
        ln.add_data(Reference(wsg, min_col=3 + dw * 2, min_row=3, max_row=27), titles_from_data=True)
        ln.series[0].graphicalProperties.line.solidFill = "1F6F66"
        ln.series[0].graphicalProperties.line.width = 22000
        ln.series[0].smooth = False
        bar += ln
        wsg.add_chart(bar, f"A{arow}"); arow += 14
    wsg.sheet_view.showGridLines = False

    bio = io.BytesIO(); wb.save(bio); return bio.getvalue()


# ================== Plan de capacidad anual ==================
if vista == "Plan de capacidad anual":
    st.header("👥 Plan de capacidad anual (agentes por mes)")
    if HIST is None:
        st.info("Sube el histórico único en la barra lateral.")
        st.stop()
    MESES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    p1, p2, p3 = st.columns(3)
    año_cap = int(p1.number_input("Año", 2024, 2030, 2026, 1))
    scope_cap = p2.selectbox("Festivos", ["Nacional España", "Cataluña (Barcelona)"])
    K_cap = int(p3.number_input("Semanas promedio", 2, 12, 4, 1))
    st.caption("Usa los parámetros de la barra lateral (AHT, SLA, OCC, UTL, absentismo…) y los ajustes por mes "
               "definidos en la Proyección anual. Puede tardar ~1 min la primera vez (dimensiona los 12 meses).")
    ajuste_mes = st.session_state.get("ajuste_mes", {})
    filas = []
    prog = st.progress(0.0, text="Dimensionando meses…")
    for mth in range(1, 13):
        mes_str = f"{año_cap}-{mth:02d}"
        aj = ajuste_mes.get(mth, 0.0)
        try:
            Sx = resolver_historico(HIST, mes_str, scope_cap, K_cap, 6,
                                    AHT, SLA, ASA, OCC, UTL, ESP_MAX, LARGO, NDA_OBJ, PACIENCIA, aj)
            presentes = Sx["te"] + Sx["tc"]
            nomina = math.ceil(presentes / (1 - ABS)) if ABS < 1 else presentes
            filas.append({"Mes": MESES[mth - 1], "Volumen": Sx["total"], "España": Sx["te"],
                          "Colombia": Sx["tc"], "Presentes": presentes,
                          "En nómina": nomina, "Ajuste": f"{int(round(aj * 100)):+d}%"})
        except Exception as e:
            filas.append({"Mes": MESES[mth - 1], "Volumen": 0, "España": 0, "Colombia": 0,
                          "Presentes": 0, "En nómina": 0, "Ajuste": "—"})
        prog.progress(mth / 12, text=f"Dimensionando {MESES[mth - 1]}…")
    prog.empty()
    cap = pd.DataFrame(filas)

    k1, k2, k3 = st.columns(3)
    pico = cap.loc[cap["En nómina"].idxmax()]
    k1.metric("Mes pico", f"{pico['Mes']}", f"{int(pico['En nómina'])} en nómina")
    k2.metric("Promedio en nómina", f"{int(round(cap['En nómina'].mean()))}")
    k3.metric("Tu plantilla actual", "92", "MULTISKILL")
    st.dataframe(cap, use_container_width=True)

    x = np.arange(12); w = 0.4
    fig, ax = plt.subplots(figsize=(11, 4))
    ax.bar(x - w / 2, cap["Presentes"], w, label="Presentes (roster)", color="#C9D6D3")
    ax.bar(x + w / 2, cap["En nómina"], w, label="En nómina (con absentismo)", color="#2A9D8F")
    ax.axhline(92, color="#E76F51", linestyle="--", linewidth=1, label="Plantilla actual (92)")
    ax.set_xticks(x); ax.set_xticklabels(cap["Mes"]); ax.set_ylabel("Agentes"); ax.legend()
    ax.set_title(f"Plantilla necesaria por mes {año_cap}")
    st.pyplot(fig)
    st.caption("Presentes = agentes que cubren la operación 24/7 en el roster. "
               "En nómina = presentes ÷ (1 − absentismo). La línea roja es tu plantilla actual de referencia.")
    st.stop()


# ================== UI ==================
st.subheader("1) Origen de la demanda")
modo = st.radio("¿Cómo obtenemos el pronóstico?",
                ["Generar desde histórico", "Subir pronóstico ya hecho"])

S = None
if modo == "Generar desde histórico":
    st.caption("Usa el mismo archivo del Dashboard y la Proyección anual (se sube una vez en la barra lateral). "
               "Se genera en Colab a partir del histórico crudo.")
    c1, c2, c3, c4 = st.columns(4)
    mes = c1.text_input("Mes (AAAA-MM)", "2026-06")
    scope = c2.selectbox("Festivos", ["Nacional España", "Cataluña (Barcelona)"])
    K = int(c3.number_input("Semanas promedio", 2, 12, 4, 1))
    semanas = int(c4.number_input("Ventana perfil (sem.)", 2, 12, 6, 1))
    # Ajuste del mes definido en la Proyección anual (si existe)
    try:
        mnum = int(mes.split("-")[1])
    except Exception:
        mnum = 0
    ajuste_mes = st.session_state.get("ajuste_mes", {}).get(mnum, 0.0)
    if ajuste_mes:
        st.caption(f"Aplicando ajuste de {int(ajuste_mes * 100):+d}% definido para ese mes en la Proyección anual.")
    if HIST is None:
        st.info("Sube el histórico único en la barra lateral.")
    else:
        try:
            S = resolver_historico(HIST, mes, scope, K, semanas, AHT, SLA, ASA, OCC, UTL, ESP_MAX, LARGO, NDA_OBJ, PACIENCIA, ajuste_mes)
        except Exception as e:
            st.error(f"No pude procesar el histórico: {e}")
else:
    up = st.file_uploader("Sube tu pronóstico (días en columnas, intervalos en filas)", type=["xlsx", "xls"])
    if up is not None:
        S = resolver_crosstab(up.getvalue(), AHT, SLA, ASA, OCC, UTL, ESP_MAX, LARGO, NDA_OBJ, PACIENCIA)

if S is None:
    st.info("Sube el archivo para continuar.")
    st.stop()

turnos = turnos_dict(LARGO)

st.subheader("2) Plantilla requerida")
c1, c2, c3, c4 = st.columns(4)
c1.metric("España (L–V)", S["te"])
c2.metric("Colombia", S["tc"])
c3.metric("TOTAL presentes", S["te"] + S["tc"])
c4.metric("En nómina (+absentismo)", math.ceil((S["te"] + S["tc"]) / (1 - ABS)))
st.caption(f"Volumen del mes: {S['total']:,} llamadas · Rotación findes Colombia: cuadrilla {S['crew']}/finde, "
           f"{S['rot']} en rotación (2 grupos de {S['crew']}, máx. 2 findes/mes).")
st.caption(f"NDA previsto (nivel de atención): mínimo {S['nda_min']:.1%} · objetivo {NDA_OBJ:.0%}. "
           f"El tope de OCC ya garantiza un NDA alto; solo aprieta en horas de muy bajo volumen.")

st.subheader("3) Programado vs Requerido y Ocupación")
dsel = st.selectbox("Día", list(range(7)), format_func=lambda i: NOM[i])
req = [S["peak"][dsel][h] for h in range(24)]
cob = [cubierto(S, turnos, dsel, h) for h in range(24)]
fig, ax = plt.subplots(figsize=(10, 3.5))
ax.bar(range(24), req, color="#C9D6D3", label="Requerido")
ax.step(range(24), cob, where="mid", color="#1F6F66", linewidth=2, label="Programado")
ax.set_xlabel("Hora"); ax.set_ylabel("Agentes"); ax.set_xticks(range(0, 24, 2)); ax.legend()
st.pyplot(fig)

st.markdown("**Ocupación por hora**")
occv = [S["occ"][dsel][h] * 100 for h in range(24)]
fig2, ax2 = plt.subplots(figsize=(10, 3))
colors = ["#E76F51" if o < 30 else ("#E9C46A" if o < OCC * 100 * 0.85 else "#2A9D8F") for o in occv]
ax2.bar(range(24), occv, color=colors)
ax2.axhline(OCC * 100, color="#264653", linestyle="--", linewidth=1, label=f"Objetivo OCC {OCC:.0%}")
ax2.set_xlabel("Hora"); ax2.set_ylabel("Ocupación %"); ax2.set_xticks(range(0, 24, 2))
ax2.set_ylim(0, 100); ax2.legend()
st.pyplot(fig2)
st.caption("Rojo = horas de baja ocupación (poca demanda). La línea marca tu objetivo de OCC.")

st.markdown("**Nivel de atención (NDA) por hora**")
ndav = [S["nda"][dsel][h] * 100 for h in range(24)]
cols3 = ["#E76F51" if (S["nda"][dsel][h] < NDA_OBJ and S["peak"][dsel][h] > 0) else "#2A9D8F" for h in range(24)]
fig3, ax3 = plt.subplots(figsize=(10, 3))
ax3.bar(range(24), ndav, color=cols3)
ax3.axhline(NDA_OBJ * 100, color="#264653", linestyle="--", linewidth=1, label=f"Objetivo {NDA_OBJ:.0%}")
ax3.set_xlabel("Hora"); ax3.set_ylabel("NDA %"); ax3.set_xticks(range(0, 24, 2))
ax3.set_ylim(0, 100); ax3.legend()
st.pyplot(fig3)
st.caption("Nivel de atención previsto (Erlang con abandono). Rojo = por debajo del objetivo.")

st.subheader("4) Plan de turnos")
filas = []
for k in sorted(S["xe"], key=int):
    t = int(k); filas.append(["España", f"{t:02d}:00", f"{(t+LARGO)%24:02d}:00", S["xe"][k], "Sáb, Dom"])
for k in sorted(S["xc"], key=lambda x: (int(x.split("_")[0]), int(x.split("_")[1]))):
    t, p = map(int, k.split("_")); o = sorted(LIBRES[p])
    filas.append(["Colombia", f"{t:02d}:00", f"{(t+LARGO)%24:02d}:00", S["xc"][k], f"{NOM[o[0]]}, {NOM[o[1]]}"])
st.dataframe(pd.DataFrame(filas, columns=["País", "Inicio", "Fin", "Cantidad", "Días libres"]),
             use_container_width=True)

st.subheader("5) Comparar con tu plantilla actual (opcional)")
ag = st.file_uploader("Sube tu Excel de agentes (columnas MODO, CENTRO, ESTADO…)",
                      type=["xlsx", "xls"], key="agentes")
if ag is not None:
    A = pd.read_excel(ag)
    if "ESTADO" in A.columns:
        A = A[A["ESTADO"].astype(str).str.upper() == "ACTIVO"]
    if "MODO" in A.columns:
        modos = sorted(A["MODO"].dropna().astype(str).unique())
        default = ["MULTISKILL"] if "MULTISKILL" in modos else modos
        sel = st.multiselect("Skill(s) a contar (MODO)", modos, default=default)
        if sel:
            A = A[A["MODO"].astype(str).isin(sel)]
    if "CENTRO" in A.columns:
        A["pais"] = A["CENTRO"].astype(str).str.upper().apply(
            lambda c: "España" if c in ("SEVILLA", "BARCELONA") else "Colombia")
        act_e = int((A["pais"] == "España").sum())
        act_c = int((A["pais"] == "Colombia").sum())
    else:
        act_e, act_c = 0, len(A)

    def nomina(x):
        return math.ceil(x / (1 - ABS))

    comp = pd.DataFrame({
        "País": ["España", "Colombia", "TOTAL"],
        "Requerido (presentes)": [S["te"], S["tc"], S["te"] + S["tc"]],
        "Necesarios en nómina (+abs)": [nomina(S["te"]), nomina(S["tc"]), nomina(S["te"]) + nomina(S["tc"])],
        "Agentes actuales": [act_e, act_c, act_e + act_c],
    })
    comp["Gap (actual − nómina)"] = comp["Agentes actuales"] - comp["Necesarios en nómina (+abs)"]
    st.dataframe(comp, use_container_width=True)
    g = int(comp.iloc[2]["Gap (actual − nómina)"])
    if g >= 0:
        st.success(f"Tienes {g} agentes de margen sobre lo necesario en nómina.")
    else:
        st.warning(f"Te faltan {-g} agentes respecto a lo necesario en nómina.")
    st.caption("Centros: SEVILLA y BARCELONA → España; el resto → Colombia.")

st.subheader("6) Descargar")
st.download_button("⬇️ Roster en Excel (con gráficos)",
                   build_excel(S, turnos, (AHT, SLA, ASA, OCC, UTL, ABS)),
                   file_name="roster.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
