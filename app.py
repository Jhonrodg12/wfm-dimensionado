import io, math
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt
from ortools.sat.python import cp_model
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.chart import BarChart, LineChart, Reference

st.set_page_config(page_title="WFM · Dimensionado y Roster", layout="wide")
st.title("📞 WFM — Dimensionamiento y Roster")

NOM = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

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

st.sidebar.header("Parámetros")
AHT = st.sidebar.number_input("AHT (seg)", 60, 1200, 420, 10)
SLA = st.sidebar.slider("SLA objetivo", 0.50, 0.99, 0.80, 0.01)
ASA = st.sidebar.number_input("ASA objetivo (seg)", 5, 120, 20, 5)
OCC = st.sidebar.slider("Ocupación tope (OCC)", 0.40, 0.95, 0.65, 0.01)
UTL = st.sidebar.slider("Utilización (UTL)", 0.60, 1.00, 0.88, 0.01)
ABS = st.sidebar.slider("Absentismo", 0.00, 0.40, 0.15, 0.01)
ESP_MAX = st.sidebar.number_input("Agentes España (máx, solo L–V)", 0, 200, 12, 1)
LARGO = int(st.sidebar.number_input("Duración turno (h)", 6, 12, 9, 1))

@st.cache_data(show_spinner="Dimensionando y optimizando turnos…")
def resolver(file_bytes, AHT, SLA, ASA, OCC, UTL, ESP_MAX, LARGO):
    def agentes(carga):
        if carga <= 0:
            return 0
        a = int(carga) + 1
        while nivel_servicio(a, carga, AHT, ASA) < SLA:
            a += 1
        return math.ceil(max(a, math.ceil(carga / OCC)) / UTL)

    raw = pd.read_excel(io.BytesIO(file_bytes))
    fechas = raw.iloc[0]
    first = raw.columns[0]
    d = raw.drop(index=0)
    d = d[d[first].astype(str).str.lower() != "total"].rename(columns={first: "intervalo"})
    L = d.melt(id_vars="intervalo", var_name="col", value_name="vol")
    L["fecha"] = L["col"].map(fechas)
    L = L[L["fecha"].apply(lambda x: isinstance(x, pd.Timestamp))]
    L["vol"] = pd.to_numeric(L["vol"], errors="coerce")
    L = L.dropna(subset=["vol"])

    def hh(x):
        try:
            return int(x)
        except Exception:
            return int(str(x).split(":")[0])
    L["intervalo"] = L["intervalo"].apply(hh)
    L = L[(L["intervalo"] >= 0) & (L["intervalo"] <= 23)]
    L["dow"] = L["fecha"].dt.dayofweek

    peak = {dw: [0] * 24 for dw in range(7)}
    for (dw, h), g in L.groupby(["dow", "intervalo"]):
        peak[dw][h] = max(agentes(v * AHT / 3600) for v in g["vol"])

    H = 24
    turnos = {ini: [(ini + k) % H for k in range(LARGO)] for ini in range(H)}
    libres = {p: {p, (p + 1) % 7} for p in range(7)}

    m = cp_model.CpModel()
    xc = {(t, p): m.NewIntVar(0, 300, f"c{t}_{p}") for t in turnos for p in range(7)}
    xe = {t: m.NewIntVar(0, 300, f"e{t}") for t in turnos}
    for dw in range(7):
        for h in range(H):
            col = sum(xc[(t, p)] for t in turnos for p in range(7) if dw not in libres[p] and h in turnos[t])
            esp = sum(xe[t] for t in turnos if dw not in libres[5] and h in turnos[t])
            m.Add(col + esp >= peak[dw][h])
    TE = sum(xe.values()); TC = sum(xc.values())
    m.Add(TE <= int(ESP_MAX))
    m.Minimize(TC * 100 - TE)
    sol = cp_model.CpSolver(); sol.Solve(m)

    xe_s = {str(t): sol.Value(xe[t]) for t in turnos if sol.Value(xe[t]) > 0}
    xc_s = {f"{t}_{p}": sol.Value(xc[(t, p)]) for t in turnos for p in range(7) if sol.Value(xc[(t, p)]) > 0}
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
    return {"peak": peak, "xe": xe_s, "xc": xc_s, "te": te, "tc": tc, "crew": cw, "rot": cw * 4 // 2}

def turnos_dict(largo):
    return {ini: [(ini + k) % 24 for k in range(largo)] for ini in range(24)}

LIBRES = {p: {p, (p + 1) % 7} for p in range(7)}

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
            if j > 1: x.alignment = CEN
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
            if j in (2, 3, 4): x.alignment = CEN
            if pais == "España": x.fill = PatternFill("solid", start_color="FCE4D6")
        return r + 1
    for k in sorted(S["xe"], key=int):
        r = add(r, "España", int(k), S["xe"][k], {5, 6})
    for k in sorted(S["xc"], key=lambda x: (int(x.split("_")[0]), int(x.split("_")[1]))):
        t, p = map(int, k.split("_")); r = add(r, "Colombia", t, S["xc"][k], LIBRES[p])
    ws.cell(r + 1, 1, "TOTAL").font = BOLD; ws.cell(r + 1, 4, f"=SUM(D2:D{r-1})").font = BOLD

    wsg = wb.create_sheet("Grafico")
    wsg["A1"] = "Programados vs Requerido — por día"; wsg["A1"].font = TITLE
    hdr = ["Hora"]
    for dw in range(7): hdr += [f"{NOM[dw]} Req", f"{NOM[dw]} Cub"]
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

st.subheader("1) Sube tu pronóstico mensual")
up = st.file_uploader("Excel con los días en columnas y los intervalos (0–23) en filas", type=["xlsx", "xls"])
if up is None:
    st.info("Sube el archivo de pronóstico para continuar.")
    st.stop()

S = resolver(up.getvalue(), AHT, SLA, ASA, OCC, UTL, ESP_MAX, LARGO)
turnos = turnos_dict(LARGO)

st.subheader("2) Plantilla requerida")
c1, c2, c3, c4 = st.columns(4)
c1.metric("España (L–V)", S["te"])
c2.metric("Colombia", S["tc"])
c3.metric("TOTAL presentes", S["te"] + S["tc"])
c4.metric("En nómina (+absentismo)", math.ceil((S["te"] + S["tc"]) / (1 - ABS)))
st.caption(f"Rotación de findes (Colombia): cuadrilla {S['crew']}/finde · {S['rot']} en rotación (2 grupos de {S['crew']}, máx. 2 findes/mes).")

st.subheader("3) Programados vs Requerido")
dsel = st.selectbox("Día", list(range(7)), format_func=lambda i: NOM[i])
req = [S["peak"][dsel][h] for h in range(24)]
cob = [cubierto(S, turnos, dsel, h) for h in range(24)]
fig, ax = plt.subplots(figsize=(10, 3.5))
ax.bar(range(24), req, color="#C9D6D3", label="Requerido")
ax.step(range(24), cob, where="mid", color="#1F6F66", linewidth=2, label="Programado")
ax.set_xlabel("Hora"); ax.set_ylabel("Agentes"); ax.set_xticks(range(0, 24, 2)); ax.legend()
st.pyplot(fig)

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
ag = st.file_uploader("Sube tu Excel de agentes (con columnas MODO, CENTRO, ESTADO…)",
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
    st.caption("Centros: SEVILLA y BARCELONA → España; el resto → Colombia. Ajusta si tienes otros centros.")

st.subheader("6) Descargar")
st.download_button("⬇️ Roster en Excel (con gráficos)",
                   build_excel(S, turnos, (AHT, SLA, ASA, OCC, UTL, ABS)),
                   file_name="roster.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
