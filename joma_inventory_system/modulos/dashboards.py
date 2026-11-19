"""
modulos/dashboards.py - Dashboards interactivos con Plotly (FASE 3)

Pestañas (Admin y Lector, solo lectura; los datos se leen de la BD en cada carga):
  1. 📦 Inventario            : valor del almacén, valor por categoría, top productos, stock vs mínimo
                                (alertas de stock bajo) y vencimientos por tramo.
  2. 🔄 Rotación y movimientos: rotación y cobertura por producto, tendencia de entradas/salidas y
                                productos sin salidas (según el kardex del período elegido).
  3. 🛒 Compras               : estado de órdenes de compra, avance de recepción y cuentas por pagar.

Las consultas viven en reportes_db.py; cada tabla se puede exportar a CSV (Excel) y a .xlsx.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from modulos import compras_db as cdb
from modulos import exportacion as exp
from modulos import inventario as inv
from modulos import reportes_db as rdb

COLOR_ESTADO_STOCK = {"⛔ Sin stock": "#7F1D1D", "🔴 Stock bajo": "#DC2626", "🟡 Cerca del mínimo": "#F59E0B", "🟢 OK": "#16A34A"}
COLOR_TRAMO = {"Vencido": "#DC2626", "0-15 días": "#EA580C", "16-30 días": "#F59E0B", "31-60 días": "#FACC15",
               "61-90 días": "#84CC16", "Más de 90 días": "#16A34A", "No perecible": "#94A3B8"}
COLOR_CLASIF = {"🟢 Alta rotación": "#16A34A", "🟡 Rotación media": "#F59E0B", "🟠 Baja rotación": "#EA580C",
                "🧊 Sin salidas": "#64748B", "— Sin stock ni salidas": "#CBD5E1"}
COLOR_OC = {"🟡 Abierta": "#F59E0B", "🔴 Atrasada": "#DC2626", "✅ Cerrada": "#16A34A"}


@st.cache_resource(show_spinner=False)
def _preparar() -> bool:
    rdb.asegurar_tablas()
    return True


def _mostrar(fig: go.Figure, titulo: str, alto: int = 340) -> None:
    fig.update_layout(title=titulo, height=alto, margin=dict(l=10, r=10, t=50, b=10),
                      legend=dict(orientation="h", y=-0.2), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig, use_container_width=True)


def _exportar(df: pd.DataFrame, columnas: dict[str, str], base: str, clave: str, usuario: dict, **kw) -> None:
    """CSV (Excel en español) y .xlsx de una tabla del dashboard."""
    exp.boton_descarga_csv(exp.preparar_export(df, columnas, **kw), base, f"{clave}_csv")
    exp.boton_descarga_xlsx(
        {base[:31]: exp.preparar_export(df, columnas, como_fecha=True, **kw)}, base, f"{clave}_xlsx",
        parametros={"Reporte": base, "Generado por": usuario.get("nombre_completo", ""),
                    "Fecha de generación": datetime.now().strftime("%d/%m/%Y %H:%M"), "Filas": len(df)})


# ===========================================================================
# KPIs
# ===========================================================================
def _kpis(sv: pd.DataFrame) -> None:
    en_alerta = sv[sv["estado_stock"].isin(["⛔ Sin stock", "🔴 Stock bajo"])]
    c1, c2, c3 = st.columns(3)
    c1.metric("Valor del almacén (costo)", f"S/ {sv['valor_costo'].sum():,.2f}")
    c2.metric("Valor a precio de venta", f"S/ {sv['valor_venta'].sum():,.2f}")
    c3.metric("Unidades vigentes", f"{sv['stock_vigente'].sum():,.0f}")
    c4, c5, c6 = st.columns(3)
    c4.metric("Productos en alerta de stock", len(en_alerta),
              delta="Reponer" if len(en_alerta) else None, delta_color="inverse")
    c5.metric(f"Valor por vencer ({rdb.DIAS_ALERTA} d)", f"S/ {sv['valor_por_vencer'].sum():,.2f}")
    c6.metric("Valor vencido", f"S/ {sv['valor_vencido'].sum():,.2f}",
              delta="Dar de baja" if sv["valor_vencido"].sum() > 0 else None, delta_color="inverse")


# ===========================================================================
# PESTAÑA 1: INVENTARIO
# ===========================================================================
def _tab_inventario(sv: pd.DataFrame, usuario: dict) -> None:
    if sv.empty:
        st.info("Aún no hay productos registrados.")
        return
    c1, c2 = st.columns(2)
    with c1:
        por_cat = sv.groupby("categoria")["valor_costo"].sum()
        por_cat = por_cat[por_cat > 0]
        if por_cat.empty:
            st.info("No hay stock valorizado todavía.")
        else:
            _mostrar(go.Figure(go.Pie(labels=list(por_cat.index), values=list(por_cat.values), hole=0.5)),
                     "Valor del inventario por categoría (S/)")
    with c2:
        top = sv.sort_values("valor_costo", ascending=False).head(10)
        top = top[top["valor_costo"] > 0].iloc[::-1]
        if top.empty:
            st.info("No hay stock valorizado todavía.")
        else:
            _mostrar(go.Figure(go.Bar(x=list(top["valor_costo"]), y=list(top["codigo"] + " · " + top["nombre"]),
                                      orientation="h", marker_color="#1F3A5F")), "Top 10 productos por valor (S/)")

    colores = [COLOR_ESTADO_STOCK.get(e, "#94A3B8") for e in sv["estado_stock"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=list(sv["codigo"]), y=list(sv["stock_vigente"]), marker_color=colores, name="Stock vigente",
                         customdata=list(sv["estado_stock"]), hovertemplate="%{x}<br>Stock: %{y}<br>%{customdata}<extra></extra>"))
    fig.add_trace(go.Scatter(x=list(sv["codigo"]), y=list(sv["stock_minimo"]), mode="markers", name="Stock mínimo",
                             marker=dict(symbol="line-ew", size=26, line=dict(width=3, color="black"))))
    _mostrar(fig, "Stock vigente vs. mínimo (rojo = stock bajo, ámbar = cerca del mínimo)")

    venc = rdb.vencimientos_por_tramo()
    medida = st.radio("Vencimientos medidos en", ["Valor (S/)", "Unidades"], horizontal=True, key="dash_venc_medida")
    col = "valor" if medida.startswith("Valor") else "unidades"
    _mostrar(go.Figure(go.Bar(x=list(venc["tramo"]), y=list(venc[col]),
                              marker_color=[COLOR_TRAMO[t] for t in venc["tramo"]])),
             f"Mercadería por tramo de vencimiento ({medida})", 320)

    st.markdown("#### 🚨 Alertas")
    a1, a2 = st.columns(2)
    with a1:
        st.markdown("**Stock bajo o agotado**")
        alerta = sv[sv["estado_stock"].isin(["⛔ Sin stock", "🔴 Stock bajo"])]
        if alerta.empty:
            st.success("Sin productos en alerta.")
        else:
            st.dataframe(alerta[["estado_stock", "codigo", "nombre", "stock_vigente", "stock_minimo", "reponer"]],
                         use_container_width=True, hide_index=True, column_config={
                             "estado_stock": "Estado", "codigo": "SKU", "nombre": "Producto",
                             "stock_vigente": st.column_config.NumberColumn("Stock", format="%.2f"),
                             "stock_minimo": st.column_config.NumberColumn("Mínimo", format="%.2f"),
                             "reponer": st.column_config.NumberColumn("A reponer", format="%.2f")})
            _exportar(alerta, {"estado_stock": "Estado", "codigo": "SKU", "nombre": "Producto", "stock_vigente": "Stock vigente",
                               "stock_minimo": "Mínimo", "reponer": "A reponer"}, "alertas_stock", "dash_stock", usuario,
                      estados=["estado_stock"])
    with a2:
        st.markdown(f"**Lotes vencidos o por vencer ({rdb.DIAS_ALERTA} días)**")
        lotes = inv.cargar_lotes()
        crit = lotes[(lotes["cantidad_actual"] > 0) & (lotes["dias_restantes"] <= rdb.DIAS_ALERTA)] \
            .sort_values("dias_restantes")
        if crit.empty:
            st.success("Sin lotes críticos.")
        else:
            st.dataframe(crit[["estado", "codigo", "producto", "lote", "fecha_vencimiento", "dias_restantes", "cantidad_actual",
                               "ubicacion"]], use_container_width=True, hide_index=True, column_config={
                                   "estado": "Estado", "codigo": "SKU", "producto": "Producto", "lote": "Lote",
                                   "fecha_vencimiento": "Vence", "dias_restantes": st.column_config.NumberColumn("Días", format="%d"),
                                   "cantidad_actual": st.column_config.NumberColumn("Cantidad", format="%.2f"),
                                   "ubicacion": "Ubicación"})
            _exportar(crit, {"estado": "Estado", "codigo": "SKU", "producto": "Producto", "lote": "Lote",
                             "fecha_vencimiento": "Vence", "dias_restantes": "Días restantes", "cantidad_actual": "Cantidad",
                             "ubicacion": "Ubicación"}, "lotes_criticos", "dash_lotes", usuario,
                      fechas={"fecha_vencimiento": "%d/%m/%Y"}, estados=["estado"], enteros=["dias_restantes"])


# ===========================================================================
# PESTAÑA 2: ROTACIÓN Y MOVIMIENTOS
# ===========================================================================
def _tab_rotacion(dias: int, usuario: dict) -> None:
    rot = rdb.rotacion(dias)
    tend = rdb.tendencia_movimientos(dias)
    st.caption(f"Basado en los movimientos del kardex de los últimos {dias} días. "
               "Rotación = unidades salidas / stock promedio · Cobertura = días de stock vigente al ritmo de salida actual.")
    if tend[["entradas", "salidas", "ajustes"]].to_numpy().sum() == 0:
        st.info("No hay movimientos registrados en el período. A medida que se registren entradas y salidas, "
                "aquí verá la rotación y la tendencia.")

    fig = go.Figure()
    fig.add_trace(go.Bar(x=list(tend["dia"]), y=list(tend["entradas"]), name="Entradas", marker_color="#16A34A"))
    fig.add_trace(go.Bar(x=list(tend["dia"]), y=list(tend["salidas"]), name="Salidas", marker_color="#DC2626"))
    fig.update_layout(barmode="group")
    _mostrar(fig, "Entradas y salidas por día (unidades)")

    con_salidas = rot[rot["salidas"] > 0]
    c1, c2 = st.columns(2)
    with c1:
        top = con_salidas.sort_values("salidas", ascending=False).head(10).iloc[::-1]
        if top.empty:
            st.info("Aún no hay salidas en el período.")
        else:
            _mostrar(go.Figure(go.Bar(x=list(top["salidas"]), y=list(top["codigo"] + " · " + top["nombre"]),
                                      orientation="h", marker_color="#1F3A5F")), "Top 10 productos por unidades salidas")
    with c2:
        if con_salidas.empty:
            st.info("La rotación se calculará cuando haya salidas.")
        else:
            r = con_salidas.sort_values("rotacion", ascending=False).head(10)
            _mostrar(go.Figure(go.Bar(x=list(r["codigo"]), y=list(r["rotacion"]),
                                      marker_color=[COLOR_CLASIF.get(c, "#94A3B8") for c in r["clasificacion"]])),
                     "Rotación (veces en el período)")

    sin = rot[rot["clasificacion"] == "🧊 Sin salidas"]
    if not sin.empty:
        st.warning(f"🧊 {len(sin)} producto(s) con stock y SIN salidas en {dias} días: " +
                   ", ".join(sin["codigo"].head(8)) + ("…" if len(sin) > 8 else ""))

    cols = {"clasificacion": "Clasificación", "codigo": "SKU", "nombre": "Producto", "categoria": "Categoría",
            "salidas": "Unidades salidas", "entradas": "Unidades entradas", "stock_promedio": "Stock promedio",
            "rotacion": "Rotación", "cobertura_dias": "Cobertura (días)", "salidas_valor": "Salidas a costo"}
    st.dataframe(rot[list(cols)].sort_values("salidas", ascending=False), use_container_width=True, hide_index=True,
                 column_config={**{k: v for k, v in cols.items() if k in ("clasificacion", "codigo", "nombre", "categoria")},
                                **{k: st.column_config.NumberColumn(v, format="%.2f") for k, v in cols.items()
                                   if k not in ("clasificacion", "codigo", "nombre", "categoria")}})
    _exportar(rot.sort_values("salidas", ascending=False), cols, f"rotacion_{dias}d", "dash_rot", usuario,
              estados=["clasificacion"], dos_decimales=["salidas_valor"])


# ===========================================================================
# PESTAÑA 3: COMPRAS
# ===========================================================================
def _tab_compras(usuario: dict) -> None:
    r = cdb.resumen_compras()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("OC abiertas", int(r["oc_abiertas"]))
    c2.metric("OC atrasadas", int(r["oc_atrasadas"]), delta="Revisar" if r["oc_atrasadas"] else None, delta_color="inverse")
    c3.metric("Facturas vencidas", int(r["facturas_vencidas"]),
              delta="Acción requerida" if r["facturas_vencidas"] else None, delta_color="inverse")
    c4.metric("Por pagar (S/)", f"S/ {r['por_pagar_pen']:,.2f}", help=f"Además en dólares: US$ {r['por_pagar_usd']:,.2f}")

    a, b = st.columns(2)
    with a:
        est = rdb.ordenes_por_estado()
        if est.empty:
            st.info("Aún no hay órdenes de compra.")
        else:
            _mostrar(go.Figure(go.Pie(labels=list(est["estado"]), values=list(est["ordenes"]), hole=0.5,
                                      marker=dict(colors=[COLOR_OC.get(e, "#94A3B8") for e in est["estado"]]))),
                     "Órdenes de compra por estado")
    with b:
        ocs = cdb.listar_ocs()
        abiertas = ocs[(ocs["estado"] == "Abierto") & ocs["avance"].notna()]
        if abiertas.empty:
            st.info("No hay OC abiertas con detalle para medir su recepción.")
        else:
            _mostrar(go.Figure(go.Bar(x=list(abiertas["numero_oc"]), y=list(abiertas["avance"]), marker_color="#1F3A5F")),
                     "Avance de recepción de OC abiertas (%)")

    cxp = rdb.cuentas_por_pagar()
    if cxp.empty:
        st.info("No hay facturas abiertas por pagar.")
    else:
        fig = go.Figure()
        for moneda in sorted(cxp["moneda"].unique()):
            montos = cxp[cxp["moneda"] == moneda].set_index("tramo")["monto"].reindex(rdb.TRAMOS_FACTURA, fill_value=0)
            fig.add_trace(go.Bar(x=list(montos.index), y=list(montos.values), name=moneda))
        fig.update_layout(barmode="stack")
        _mostrar(fig, "Cuentas por pagar por antigüedad")

    fac = cdb.listar_facturas()
    fac = fac[fac["estado"] == "Abierto"].sort_values("fecha_vencimiento_pago", na_position="last")
    st.markdown("**Facturas abiertas (por fecha de pago)**")
    if fac.empty:
        st.success("No hay facturas abiertas.")
    else:
        st.dataframe(fac[["estado_visual", "numero_factura", "proveedor", "fecha_vencimiento_pago", "dias_para_pago",
                          "monto_total", "moneda"]], use_container_width=True, hide_index=True, column_config={
                              "estado_visual": "Estado", "numero_factura": "N° factura", "proveedor": "Proveedor",
                              "fecha_vencimiento_pago": "Vence pago",
                              "dias_para_pago": st.column_config.NumberColumn("Días", format="%d"),
                              "monto_total": st.column_config.NumberColumn("Monto", format="%.2f"), "moneda": "Mon."})
        _exportar(fac, {"estado_visual": "Estado", "numero_factura": "N° factura", "proveedor": "Proveedor",
                        "fecha_vencimiento_pago": "Vence pago", "dias_para_pago": "Días para el pago",
                        "monto_total": "Monto", "moneda": "Moneda"}, "facturas_abiertas", "dash_fac", usuario,
                  fechas={"fecha_vencimiento_pago": "%d/%m/%Y"}, estados=["estado_visual"], enteros=["dias_para_pago"],
                  dos_decimales=["monto_total"])


# ===========================================================================
# PUNTO DE ENTRADA (invocado por app.py)
# ===========================================================================
def render(usuario: dict) -> None:
    """Dashboards en solo lectura para Admin y Lector."""
    _preparar()
    st.title("📊 Dashboards")
    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    dias = c1.select_slider("Período de análisis (rotación y movimientos)", options=[30, 60, 90, 180, 365], value=90,
                            format_func=lambda d: f"{d} días", key="dash_dias")
    c2.button("🔄 Actualizar datos", key="dash_refrescar")      # cualquier interacción recarga los datos
    sv = rdb.stock_valorizado()
    _kpis(sv)
    t1, t2, t3 = st.tabs(["📦 Inventario", "🔄 Rotación y movimientos", "🛒 Compras"])
    with t1:
        _tab_inventario(sv, usuario)
    with t2:
        _tab_rotacion(int(dias), usuario)
    with t3:
        _tab_compras(usuario)
