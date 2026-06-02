import streamlit as st
import pandas as pd, math, warnings
warnings.filterwarnings("ignore")
import matplotlib.pyplot as plt
from ortools.sat.python import cp_model

st.set_page_config(page_title="Dimensionador WFM", page_icon="🎧", layout="wide")
st.title("🎧 Dimensionador WFM — inbound 24/7")
st.caption("Carga tu previsión y tu archivo de agentes para dimensionar el mes.")

# ============ MOTOR ============
def erlang_b(a, c):
    b = 1.0
    for k in range(1, a + 1):
        b = (c * b) / (k + c * b)
    return b

def erlang_c(a, c):
    b = erlang_b(a, c); r = c / a
    return b / (1 - r + r * b)

def nivel_servicio(a, c, aht, obj):
    if a <= c: return 0.0
    return 1 - erlang_c(a, c) * math.exp(-(a - c) * obj / aht)

def agentes_necesarios(carga, aht, obj, sla):
    if carga <= 0: return 0
    a = int(carga) + 1
    while nivel_servicio(a, carga, aht, obj) < sla:
        a += 1
    return a

# ============ PARÁMETROS (barra lateral) ============
st.sidebar.header("⚙️ Parámetros")
AHT           = st.sidebar.number_input("AHT (segundos)", 60, 1800, 420)
SLA_OBJ       = st.sidebar.slider("Nivel de servicio objetivo", 0.50, 0.99, 0.80)
TARGET_SEG    = st.sidebar.number_input("Responder dentro de (seg)", 5, 120, 20)
INTERVALO_MIN = st.sidebar.selectbox("Intervalo (min)", [60, 30, 15], 0)
AUSENTISMO    = st.sidebar.slider("Ausentismo", 0.0, 0.50, 0.15)
TURNO_HORAS   = st.sidebar.number_input("Horas por turno", 4, 12, 9)
PROD_HORAS    = st.sidebar.number_input("Horas productivas/turno", 4.0, 12.0, 8.0)
MES           = st.sidebar.number_input("Mes a procesar", 1, 12, 6)

BREAK_MIN   = {"Colombia": 40, "Espana": 75}
CENTRO_PAIS = {"BOGOTA": "Colombia", "SEVILLA": "Espana", "BARCELONA": "Espana"}

# ============ CARGA DE ARCHIVOS ============
c1, c2 = st.columns(2)
prev_file = c1.file_uploader("📈 Previsión (.xlsx)", type=["xlsx"])
ag_file   = c2.file_uploader("👥 Agentes (.xlsx)", type=["xlsx"])

if prev_file is not None:
    raw = pd.read_excel(prev_file)
    fechas = raw.iloc[0]
    datos = raw.drop(index=0)
    datos = datos[datos["Unnamed: 0"] != "total"].rename(columns={"Unnamed: 0": "intervalo"})
    largo = datos.melt(id_vars="intervalo", var_name="columna", value_name="volumen")
    largo["fecha"] = largo["columna"].map(fechas)
    largo = largo[largo["fecha"].apply(lambda d: isinstance(d, pd.Timestamp))]
    largo["volumen"] = pd.to_numeric(largo["volumen"], errors="coerce")
    largo = largo.dropna(subset=["volumen"])
    largo = largo[largo["fecha"].dt.month == MES]
    largo["intervalo"] = largo["intervalo"].astype(int)
    largo = largo[["fecha", "intervalo", "volumen"]].sort_values(["fecha", "intervalo"])

    dur = INTERVALO_MIN * 60
    largo["carga"]   = largo["volumen"] * AHT / dur
    largo["agentes"] = largo.apply(lambda f: agentes_necesarios(f["carga"], AHT, TARGET_SEG, SLA_OBJ), axis=1)

    demanda = largo["agentes"].sum()
    ocup_global = largo["carga"].sum() / demanda * 100 if demanda else 0

    k1, k2, k3 = st.columns(3)
    k1.metric("Demanda (agente-horas)", f"{int(demanda):,}")
    k2.metric("Ocupación global", f"{ocup_global:.0f}%")
    k3.metric("Días procesados", largo["fecha"].dt.day.nunique())

    # Ocupación por hora
    st.subheader("Ocupación por hora (rojo = hora muerta)")
    ocup = largo.groupby("intervalo").apply(
        lambda g: g["carga"].sum() / g["agentes"].sum() * 100 if g["agentes"].sum() > 0 else 0)
    colores = ["#e63946" if v < 35 else "#2a9d8f" for v in ocup]
    fig, ax = plt.subplots(figsize=(11, 3.5))
    ax.bar(ocup.index.astype(str), ocup.values, color=colores)
    ax.axhline(85, color="gray", ls="--", lw=1)
    ax.set_ylim(0, 100); ax.set_xlabel("Hora"); ax.set_ylabel("Ocupación %")
    st.pyplot(fig)

    # Right-sizing
    porfecha = largo.groupby("fecha")["agentes"].sum()
    dow = porfecha.groupby(porfecha.index.dayofweek).mean()
    req_dia = {d: math.ceil(dow[d] / PROD_HORAS) for d in range(7)}
    model = cp_model.CpModel()
    patrones = {off: [d for d in range(7) if d not in {off, (off + 1) % 7}] for off in range(7)}
    x = {p: model.NewIntVar(0, 1000, f"p{p}") for p in patrones}
    for d in range(7):
        model.Add(sum(x[p] for p, tr in patrones.items() if d in tr) >= req_dia[d])
    model.Minimize(sum(x[p] for p in patrones))
    solver = cp_model.CpSolver(); solver.Solve(model)
    head = sum(solver.Value(x[p]) for p in patrones)
    head_aus = math.ceil(head / (1 - AUSENTISMO))

    st.subheader("Right-sizing")
    r1, r2 = st.columns(2)
    r1.metric("Headcount mínimo", head)
    r2.metric("Con ausentismo", head_aus)

    # Capacidad (si hay archivo de agentes)
    if ag_file is not None:
        ag = pd.read_excel(ag_file)
        m = ag[(ag["MODO"] == "MULTISKILL") & (ag["ESTADO"] == "ACTIVO")].copy()
        m["pais"] = m["CENTRO"].map(CENTRO_PAIS)
        m["prod"] = m["pais"].map(lambda p: (TURNO_HORAS * 60 - BREAK_MIN[p]) / (TURNO_HORAS * 60))
        m["horas_prod_mes"] = m["Jornada"] * (30 / 7) * m["prod"]
        capacidad = m["horas_prod_mes"].sum() * (1 - AUSENTISMO)
        st.subheader("Capacidad")
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("Multiskill activos", len(m))
        cc2.metric("Capacidad (h)", f"{int(capacidad):,}")
        cc3.metric("Cobertura", f"{capacidad / demanda * 100:.0f}%")

    # Descarga
    st.subheader("Detalle")
    st.dataframe(largo.head(50))
    csv = largo.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Descargar detalle (CSV)", csv, "dimensionamiento.csv", "text/csv")
else:
    st.info("Sube tu archivo de previsión para empezar.")
