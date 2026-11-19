"""
modulos/reportes.py - MÓDULO 4 (Fase 1): Kardex y Reportes

Pestañas (Admin y Lector, SOLO LECTURA):
  1. 📜 Kardex (auditoría): historial completo de movimientos con trazabilidad
        (fecha y hora, tipo, producto, lote, vencimiento, OC y factura de origen,
         entradas, salidas, saldo acumulado, motivo, referencia y usuario).
        Exportable a CSV (Excel en español) y a Excel (.xlsx).
  2. 📦 Stock valorizado: inventario por producto con su valor a costo y a precio de venta.

El kardex NO se puede editar ni eliminar desde la aplicación: es un registro de auditoría.
La lógica de consulta vive en reportes_db.py y la exportación en exportacion.py.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from modulos import exportacion as exp
from modulos import inventario as inv
from modulos import reportes_db as rdb

TIPOS = ["ENTRADA", "SALIDA", "AJUSTE"]
ICONOS_TIPO = {"ENTRADA": "🟢 Entrada", "SALIDA": "🔴 Salida", "AJUSTE": "🟠 Ajuste"}

COLUMNAS_KARDEX = {
    "fecha": "Fecha y hora", "tipo": "Tipo", "codigo": "Código", "producto": "Producto", "lote": "Lote",
    "vence": "Vence", "oc": "OC de origen", "factura": "Factura de origen", "entrada": "Entrada",
    "salida": "Salida", "saldo": "Saldo", "motivo": "Motivo", "referencia": "Referencia",
    "guia": "Guía de remisión", "cliente": "Cliente", "cliente_doc": "RUC / DNI",
    "cliente_direccion": "Dirección del cliente", "usuario": "Usuario", "username": "Cuenta",
}
COLUMNAS_STOCK = {
    "estado_stock": "Estado", "codigo": "Código", "nombre": "Producto", "categoria": "Categoría", "unidad": "Unidad",
    "stock_vigente": "Stock vigente", "stock_minimo": "Mínimo", "reponer": "A reponer",
    "stock_por_vencer": "Por vencer (30 d)", "stock_vencido": "Vencido", "precio_costo": "Costo unit.",
    "valor_costo": "Valor a costo", "valor_venta": "Valor a precio venta", "valor_vencido": "Valor vencido",
}


@st.cache_resource(show_spinner=False)
def _preparar() -> bool:
    rdb.asegurar_tablas()
    return True


def _limpiar_filtros_kardex() -> None:
    hoy = date.today()
    st.session_state["kdx_desde"] = hoy - timedelta(days=30)
    st.session_state["kdx_hasta"] = hoy
    st.session_state["kdx_prod"] = 0
    st.session_state["kdx_tipos"] = list(TIPOS)
    st.session_state["kdx_user"] = 0
    st.session_state["kdx_q"] = ""


# ===========================================================================
# PESTAÑA 1: KARDEX
# ===========================================================================
def _tab_kardex(usuario: dict) -> None:
    hoy = date.today()
    prods = inv.cargar_productos(solo_activos=False)
    opt_prod = {0: "Todos los productos"}
    opt_prod.update({int(r.id): f"{r.codigo} · {r.nombre}" for r in prods.itertuples()})
    users = rdb.usuarios_para_filtro()
    opt_user = {0: "Todos los usuarios"}
    opt_user.update({int(r.id): f"{r.nombre_completo} ({r.username})" for r in users.itertuples()})

    with st.container(border=True):
        c1, c2, c3 = st.columns([1, 1, 3])
        desde = c1.date_input("Desde", value=hoy - timedelta(days=30), format="DD/MM/YYYY", key="kdx_desde")
        hasta = c2.date_input("Hasta", value=hoy, format="DD/MM/YYYY", key="kdx_hasta")
        pid = c3.selectbox("Producto", list(opt_prod), format_func=opt_prod.get, key="kdx_prod")
        c4, c5, c6, c7 = st.columns([2, 2, 2, 1.2], vertical_alignment="bottom")
        tipos = c4.multiselect("Tipo de movimiento", TIPOS, default=TIPOS, key="kdx_tipos",
                               format_func=lambda t: ICONOS_TIPO[t])
        uid = c5.selectbox("Usuario", list(opt_user), format_func=opt_user.get, key="kdx_user")
        texto = c6.text_input("🔎 Lote, OC, factura, guía, cliente…", key="kdx_q")
        c7.button("🧹 Limpiar", key="kdx_btn_limpiar", on_click=_limpiar_filtros_kardex)

    if desde > hasta:
        st.error("La fecha 'Desde' no puede ser posterior a 'Hasta'.")
        return
    if not tipos:
        st.warning("Seleccione al menos un tipo de movimiento.")
        return

    df = rdb.kardex(desde, hasta, producto_id=pid or None, usuario_id=uid or None, tipos=tipos, texto=texto)

    st.caption("🔒 Vista de solo lectura: el kardex es un registro de auditoría y no puede editarse ni eliminarse "
               "desde la aplicación.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Movimientos", f"{len(df):,}")
    c2.metric("Total entradas", f"{df['entrada'].sum():,.2f}")
    c3.metric("Total salidas", f"{df['salida'].sum():,.2f}")
    c4.metric("Neto del período", f"{df['entrada'].sum() - df['salida'].sum():+,.2f}")

    if df.empty:
        st.info("No hay movimientos con los filtros aplicados.")
        return

    con_saldo = bool(pid)
    cols = [c for c in COLUMNAS_KARDEX if c != "saldo" or con_saldo]
    vista = df.copy()
    vista["tipo"] = vista["tipo"].map(ICONOS_TIPO)
    vista["entrada"] = vista["entrada"].where(vista["entrada"] > 0)   # vacío en vez de 0
    vista["salida"] = vista["salida"].where(vista["salida"] > 0)
    st.dataframe(
        inv._fechas(vista[[c for c in cols if c != "username"]], ["fecha", "vence"]),
        use_container_width=True, hide_index=True, column_config={
            "fecha": st.column_config.DatetimeColumn("Fecha y hora", format="DD/MM/YYYY HH:mm"),
            "tipo": "Tipo", "codigo": "Código", "producto": "Producto", "lote": "Lote",
            "vence": st.column_config.DateColumn("Vence", format="DD/MM/YYYY"),
            "oc": "OC de origen", "factura": "Factura de origen",
            "entrada": st.column_config.NumberColumn("Entrada", format="%.2f"),
            "salida": st.column_config.NumberColumn("Salida", format="%.2f"),
            "saldo": st.column_config.NumberColumn("Saldo", format="%.2f",
                                                   help="Stock del producto tras el movimiento."),
            "motivo": "Motivo", "referencia": "Referencia", "guia": "Guía de remisión", "cliente": "Cliente",
            "cliente_doc": "RUC / DNI", "cliente_direccion": "Dirección del cliente", "usuario": "Usuario"})
    if con_saldo:
        st.caption(f"Saldo del producto antes de su primer movimiento registrado: "
                   f"{rdb.saldo_inicial_producto(pid):,.2f}. El saldo cuadra siempre con el stock actual.")
    else:
        st.caption("Seleccione un producto para ver el saldo acumulado tras cada movimiento. "
                   "'OC / Factura de origen' son las del lote que se movió.")

    # ----- Exportación
    rango = f"{desde:%Y%m%d}_a_{hasta:%Y%m%d}"
    export_cols = {k: v for k, v in COLUMNAS_KARDEX.items() if k != "saldo" or con_saldo}
    fechas = {"fecha": "%d/%m/%Y %H:%M", "vence": "%d/%m/%Y"}
    df_csv = exp.preparar_export(df, export_cols, fechas=fechas)
    exp.boton_descarga_csv(df_csv, f"kardex_{rango}", "kdx_csv")

    resumen = (df.groupby(["codigo", "producto"], as_index=False)
                 .agg(movimientos=("id", "count"), entradas=("entrada", "sum"), salidas=("salida", "sum")))
    resumen["neto"] = resumen["entradas"] - resumen["salidas"]
    resumen = resumen.rename(columns={"codigo": "Código", "producto": "Producto", "movimientos": "Movimientos",
                                      "entradas": "Entradas", "salidas": "Salidas", "neto": "Neto"})
    parametros = {
        "Reporte": "Kardex de movimientos de inventario",
        "Período": f"{desde.strftime('%d/%m/%Y')} al {hasta.strftime('%d/%m/%Y')}",
        "Producto": opt_prod[pid], "Tipos": ", ".join(tipos), "Usuario": opt_user[uid],
        "Búsqueda": texto or "—", "Filas": len(df),
        "Generado por": usuario.get("nombre_completo", ""),
        "Fecha de generación": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }
    exp.boton_descarga_xlsx(
        {"Kardex": exp.preparar_export(df, export_cols, fechas=fechas, como_fecha=True), "Resumen por producto": resumen},
        f"kardex_{rango}", "kdx_xlsx", parametros=parametros)


# ===========================================================================
# PESTAÑA 2: STOCK VALORIZADO
# ===========================================================================
def _tab_stock(usuario: dict) -> None:
    df = rdb.stock_valorizado()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Valor a costo (vigente)", f"S/ {df['valor_costo'].sum():,.2f}")
    c2.metric("Valor a precio de venta", f"S/ {df['valor_venta'].sum():,.2f}")
    c3.metric("Valor por vencer (30 d)", f"S/ {df['valor_por_vencer'].sum():,.2f}")
    c4.metric("Valor vencido", f"S/ {df['valor_vencido'].sum():,.2f}")

    with st.container(border=True):
        f1, f2 = st.columns([3, 2])
        buscar = f1.text_input("🔎 Buscar por código o nombre", key="stkv_q")
        categorias = ["Todas"] + sorted(df["categoria"].unique())
        categoria = f2.selectbox("Categoría", categorias, key="stkv_cat")
    dff = inv._aplicar_busqueda(df, buscar, ["codigo", "nombre"])
    if categoria != "Todas":
        dff = dff[dff["categoria"] == categoria]
    if dff.empty:
        st.info("Ningún producto coincide con los filtros.")
        return

    st.dataframe(dff[list(COLUMNAS_STOCK)], use_container_width=True, hide_index=True, column_config={
        **{k: v for k, v in COLUMNAS_STOCK.items() if k in ("estado_stock", "codigo", "nombre", "categoria", "unidad")},
        **{k: st.column_config.NumberColumn(v, format="%.2f") for k, v in COLUMNAS_STOCK.items()
           if k not in ("estado_stock", "codigo", "nombre", "categoria", "unidad")}})
    st.caption(f"{len(dff)} producto(s). Valores a costo sobre el stock vigente (sin vencidos). El archivo exporta lo filtrado.")

    export = exp.preparar_export(dff, COLUMNAS_STOCK, estados=["estado_stock"], dos_decimales=[
        "precio_costo", "valor_costo", "valor_venta", "valor_vencido"])
    exp.boton_descarga_csv(export, "stock_valorizado", "stkv_csv")
    exp.boton_descarga_xlsx(
        {"Stock valorizado": export}, "stock_valorizado", "stkv_xlsx",
        parametros={"Reporte": "Stock valorizado", "Generado por": usuario.get("nombre_completo", ""),
                    "Fecha de generación": datetime.now().strftime("%d/%m/%Y %H:%M"), "Filas": len(dff)})


# ===========================================================================
# PUNTO DE ENTRADA (invocado por app.py)
# ===========================================================================
def render(usuario: dict) -> None:
    """Kardex y reportes en solo lectura para Admin y Lector."""
    _preparar()
    st.title("📜 Kardex y Reportes")
    tab_kardex, tab_stock = st.tabs(["📜 Kardex (auditoría)", "📦 Stock valorizado"])
    with tab_kardex:
        _tab_kardex(usuario)
    with tab_stock:
        _tab_stock(usuario)
