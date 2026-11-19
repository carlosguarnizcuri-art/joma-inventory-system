"""
modulos/despachos.py - Despachos contra Órdenes de Venta (FASE 4, interfaz)

  * Buscador instantáneo por NOMBRE DE CLIENTE o N° DE OV. Al elegir la OV el pedido se autocompleta
    (lo pendiente de despachar) y el Pre-picking FEFO se puede descargar de inmediato.
  * Toda salida se registra CONTRA LA OV: al confirmar descuenta stock por lotes (FEFO), escribe el kardex
    con OV/cliente/guía y suma lo despachado en la OV; el Packing List va al expediente de la OV.
Permisos: escriben Admin, Comercial y Almacén; los demás roles consultan y descargan documentos.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from modulos import despachos_db, despachos_docs as dc, expediente_ov, ventas_db
from modulos import documentos_pdf as pdf
from modulos import exportacion as exp
from modulos import inventario as inv
from modulos.compras_ui import (ejecutar as _ejecutar, flash as _flash, fmt_fecha as _fmt_fecha, intentar as _intentar,
                                mensaje_cierre, mostrar_flash as _mostrar_flash, puede_escribir)

ICONO_ESTADO = {"Borrador": "🟡 Borrador", "Despachado": "✅ Despachado", "Anulado": "⛔ Anulado"}


@st.cache_resource(show_spinner=False)
def _preparar() -> bool:
    inv._asegurar_tabla_movimientos()
    despachos_db.asegurar_esquema()
    return True


def _limpiar_filtros() -> None:
    st.session_state["des_q"] = ""
    st.session_state["des_estado"] = "Todos"


# ===========================================================================
# DOCUMENTOS DE ALMACÉN (1 clic)
# ===========================================================================
def _descargas(usuario: dict, d, plan: pd.DataFrame, faltantes: list[dict], clave: str) -> None:
    nombre = usuario.get("nombre_completo", "")
    if plan.empty:
        st.info("No hay stock disponible para generar el picking.")
        return
    cols, _, pr_xl, pr_tot, _n = dc.doc_prepicking(plan, faltantes)
    kcols, _, pk_xl, pk_tot = dc.doc_packing(plan)
    enc = dc.cabecera(d)
    c1, c2, c3, c4 = st.columns(4)
    c1.download_button("⬇️ Pre-picking (PDF)", dc.pdf_prepicking(d, plan, faltantes, nombre),
                       file_name=f"prepicking_{d['numero_despacho']}.pdf", mime=pdf.MIME_PDF, key=f"{clave}_pp_pdf")
    c2.download_button("⬇️ Pre-picking (Excel)", pdf.excel_documento(
        f"Pre-picking {d['numero_despacho']}", encabezado=enc, columnas=cols, filas=pr_xl, totales=pr_tot,
        nombre_hoja="Pre-picking", generado_por=nombre), file_name=f"prepicking_{d['numero_despacho']}.xlsx",
        mime=exp.MIME_XLSX, key=f"{clave}_pp_xl")
    c3.download_button("⬇️ Packing List (PDF)", dc.pdf_packing(d, plan, nombre),
                       file_name=f"packing_{d['numero_despacho']}.pdf", mime=pdf.MIME_PDF, key=f"{clave}_pk_pdf")
    c4.download_button("⬇️ Packing List (Excel)", pdf.excel_documento(
        f"Packing List {d['numero_despacho']}", encabezado=enc, columnas=kcols, filas=pk_xl, totales=pk_tot,
        nombre_hoja="Packing List", generado_por=nombre), file_name=f"packing_{d['numero_despacho']}.xlsx",
        mime=exp.MIME_XLSX, key=f"{clave}_pk_xl")


# ===========================================================================
# NUEVO DESPACHO: buscar OV -> pedido autocompletado -> picking inmediato
# ===========================================================================
def _tab_nuevo(usuario: dict) -> None:
    n = st.session_state.setdefault("des_n", 0)
    ultimo = st.session_state.get("des_ultimo")
    if ultimo:
        d = despachos_db.obtener_despacho(ultimo)
        if d is not None:
            st.success(f"✅ Despacho {d['numero_despacho']} creado contra la OV {d['numero_ov']}. Picking listo:")
            plan, falt = despachos_db.plan_picking(ultimo)
            _descargas(usuario, d, plan, falt, f"des_ult_{ultimo}")
            if st.button("➕ Crear otro despacho", key="des_otro"):
                st.session_state.pop("des_ultimo", None)
                st.rerun()
            return

    st.info(f"🔢 Número que se asignará: **{despachos_db.vista_previa_numero()}**. Toda salida se registra contra una OV.")
    busqueda = st.text_input("🔎 Buscar por nombre de cliente o N° de OV", key=f"des_q_{n}",
                             placeholder="Ej. Supermercados, OV001-26, 20100070970, OC del cliente")
    df = ventas_db.ov_para_despacho(busqueda)
    if df.empty:
        st.warning("No hay órdenes de venta con mercadería pendiente que coincidan. Cree la OV en 📑 Órdenes de Venta.")
        return
    opciones = {int(r.id): f"{r.numero_ov} · {r.cliente} ({r.documento}) · OC cliente: {r.oc_cliente or '—'} · "
                           f"pendiente: {r.pendiente:g}" for r in df.itertuples()}
    ov_id = st.selectbox(f"Orden de Venta ({len(df)} resultado(s))", list(opciones), format_func=opciones.get, key=f"des_ov_{n}")
    ov = ventas_db.obtener_ov(ov_id)
    pend = despachos_db.lineas_pendientes_ov(ov_id)
    st.success(f"📋 Pedido autocompletado desde {ov['numero_ov']}: {len(pend)} producto(s) pendientes · "
               f"Cliente: {ov['cliente_nombre']} ({ov['tipo_documento']} {ov['numero_documento']}) · "
               f"Dirección: {ov['cliente_direccion'] or '—'}")

    # Pre-picking inmediato con todo lo pendiente (antes de crear el despacho)
    plan, falt = despachos_db.plan_picking_lineas([{"producto_id": int(r.producto_id), "cantidad": float(r.pendiente)}
                                                    for r in pend.itertuples()])
    prev = {"id": 0, "numero_despacho": "(vista previa)", "fecha": date.today().isoformat(), "numero_ov": ov["numero_ov"],
            "oc_cliente": ov["oc_cliente"], "cliente_nombre": ov["cliente_nombre"], "tipo_documento": ov["tipo_documento"],
            "numero_documento": ov["numero_documento"], "cliente_direccion": ov["cliente_direccion"],
            "guia_remision": None, "estado": "Vista previa"}
    st.markdown("**📋 Picking inmediato** (con todo lo pendiente de la OV)")
    _descargas(usuario, prev, plan, falt, f"des_prev_{n}_{ov_id}")
    if falt:
        st.warning("⚠️ Stock insuficiente para: " + "; ".join(f"{f['codigo']} (faltan {f['faltante']:g})" for f in falt))

    etiquetas = {f"{r.codigo} · {r.producto} (pend. {r.pendiente:g} · disp. {r.disponible:g})": int(r.producto_id)
                 for r in pend.itertuples()}
    inicial = pd.DataFrame({"Producto": list(etiquetas), "Cantidad": [float(r.pendiente) for r in pend.itertuples()]})
    k = f"{n}_{ov_id}"
    with st.form(f"des_form_nuevo_{k}"):
        c1, c2, c3 = st.columns([1, 1.4, 2.6])
        fecha = c1.date_input("Fecha", value=date.today(), format="DD/MM/YYYY", key=f"des_fec_{k}")
        guia = c2.text_input("Guía de remisión (opcional)", placeholder="T001-00001234", key=f"des_guia_{k}")
        obs = c3.text_input("Observaciones", key=f"des_obs_{k}")
        st.markdown("**Cantidades a despachar** (reduzca para un despacho parcial)")
        lineas_df = st.data_editor(inicial, num_rows="dynamic", use_container_width=True, hide_index=True, key=f"des_lin_{k}",
                                   column_config={"Producto": st.column_config.SelectboxColumn("Producto", options=list(etiquetas), required=True),
                                                  "Cantidad": st.column_config.NumberColumn("Cantidad", min_value=0.0, step=1.0, format="%.2f")})
        crear = st.form_submit_button("📋 Crear despacho y generar Picking", type="primary")
    if crear:
        lineas = [{"producto_id": etiquetas[r["Producto"]], "cantidad": 0.0 if pd.isna(r["Cantidad"]) else r["Cantidad"]}
                  for _, r in lineas_df.iterrows() if pd.notna(r["Producto"]) and r["Producto"] in etiquetas]
        ok, did = _ejecutar(despachos_db.crear_despacho, usuario, ov_id=int(ov_id), fecha=fecha, guia_remision=guia,
                            observaciones=obs, lineas=lineas)
        if ok:
            st.session_state["des_ultimo"] = did
            st.session_state["des_sel"] = did
            st.session_state["des_n"] = n + 1
            st.rerun()


# ===========================================================================
# LISTADO Y DETALLE
# ===========================================================================
def _tab_lista(usuario: dict, escribe: bool) -> None:
    df = despachos_db.listar_despachos()
    if df.empty:
        st.info("Aún no hay despachos." + (" Cree el primero en la pestaña ➕ Nuevo despacho." if escribe else ""))
        return
    with st.container(border=True):
        c1, c2, c3 = st.columns([3, 2, 1.3], vertical_alignment="bottom")
        buscar = c1.text_input("🔎 Buscar por cliente, N° de OV, N° de despacho, documento u OC del cliente", key="des_q")
        estado = c2.selectbox("Estado", ["Todos", "Borrador", "Despachado", "Anulado"], key="des_estado")
        c3.button("🧹 Limpiar filtros", key="des_btn_limpiar", on_click=_limpiar_filtros)
    dff = inv._aplicar_busqueda(df, buscar, ["numero_despacho", "numero_ov", "cliente", "documento", "oc_cliente"])
    if estado != "Todos":
        dff = dff[dff["estado"] == estado]
    if dff.empty:
        st.info("Ningún despacho coincide con los filtros.")
        return
    vista = dff.assign(estado=dff["estado"].map(ICONO_ESTADO), numero_ov=dff["numero_ov"].fillna("—"))
    st.dataframe(inv._fechas(vista[["estado", "numero_despacho", "numero_ov", "fecha", "cliente", "documento", "oc_cliente",
                                    "guia_remision", "lineas", "unidades"]], ["fecha"]), use_container_width=True,
                 hide_index=True, column_config={
                     "estado": "Estado", "numero_despacho": "N° despacho", "numero_ov": "Orden de Venta", "cliente": "Cliente",
                     "documento": "RUC / DNI", "fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"),
                     "oc_cliente": "OC del cliente", "guia_remision": "Guía",
                     "lineas": st.column_config.NumberColumn("Ítems", format="%d"),
                     "unidades": st.column_config.NumberColumn("Unidades", format="%.2f")})
    st.divider()
    opciones = {int(r.id): f"{r.numero_despacho} · {r.numero_ov or 'sin OV'} · {r.cliente} · {r.estado}" for r in dff.itertuples()}
    if st.session_state.get("des_sel") not in opciones:
        st.session_state.pop("des_sel", None)
    did = st.selectbox("📂 Ver detalle del despacho", list(opciones), format_func=opciones.get, key="des_sel")
    _detalle(usuario, escribe, int(did))


def _detalle(usuario: dict, escribe: bool, did: int) -> None:
    d = despachos_db.obtener_despacho(did)
    if d is None:
        st.warning("El despacho ya no existe.")
        return
    lineas = despachos_db.lineas_despacho(did)
    plan, faltantes = despachos_db.plan_picking(did)
    st.subheader(f"{d['numero_despacho']} · {d['cliente_nombre']}")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Estado", ICONO_ESTADO[d["estado"]])
    c2.metric("Orden de Venta", d["numero_ov"] or "—")
    c3.metric(d["tipo_documento"], d["numero_documento"])
    c4.metric("OC del cliente", d["oc_cliente"] or "—")
    c5.metric("Guía", d["guia_remision"] or "—")
    st.caption(f"📍 {d['cliente_direccion'] or 'Sin dirección registrada'} · Fecha: {_fmt_fecha(d['fecha'])}"
               + (f" · 📝 {d['observaciones']}" if d["observaciones"] else ""))
    if d["ov_id"] is None and d["estado"] == "Borrador":
        st.warning("⚠️ Despacho antiguo sin Orden de Venta: no se puede confirmar. Anúlelo y créelo desde la OV.")
    vista = lineas.assign(situacion=lineas["faltante"].map(lambda f: "✅ Disponible" if f <= 0 else f"⚠️ Faltan {f:g}"))
    if d["estado"] == "Despachado":
        vista["situacion"] = "✅ Despachado"
    st.dataframe(vista[["codigo", "producto", "unidad", "cantidad", "disponible", "situacion", "importe_ref"]],
                 use_container_width=True, hide_index=True, column_config={
                     "codigo": "SKU", "producto": "Descripción", "unidad": "Unidad",
                     "cantidad": st.column_config.NumberColumn("Cantidad", format="%.2f"),
                     "disponible": st.column_config.NumberColumn("Stock vigente hoy", format="%.2f"),
                     "situacion": "Situación", "importe_ref": st.column_config.NumberColumn("Importe ref.", format="%.2f")})
    if faltantes and d["estado"] == "Borrador":
        st.warning("⚠️ Stock vigente insuficiente para: " + "; ".join(f"{f['codigo']} (faltan {f['faltante']:g})" for f in faltantes))
    st.markdown("##### 📄 Documentos para almacén")
    _descargas(usuario, d, plan, faltantes, f"des_det_{did}")
    if d["ov_id"]:
        ch = expediente_ov.checklist(int(d["ov_id"]))
        st.caption("📁 Expediente de la OV: " + " · ".join(f"{'✅' if c['cumple'] else '❌'} {c['etiqueta']}" for c in ch))
    if escribe:
        _acciones(usuario, d, faltantes)


def _acciones(usuario: dict, d, faltantes: list[dict]) -> None:
    did = int(d["id"])
    st.markdown("##### 🛠️ Acciones")
    if d["estado"] == "Borrador":
        with st.expander("✏️ Editar cantidades del borrador", expanded=False):
            lineas = despachos_db.lineas_despacho(did)
            etiquetas = {f"{r.codigo} · {r.producto}": int(r.producto_id) for r in lineas.itertuples()}
            with st.form(f"des_form_lin_{did}"):
                editado = st.data_editor(pd.DataFrame({"Producto": list(etiquetas), "Cantidad": lineas["cantidad"].astype("float64")}),
                                         num_rows="dynamic", use_container_width=True, hide_index=True, key=f"des_ed_{did}",
                                         column_config={"Producto": st.column_config.SelectboxColumn("Producto", options=list(etiquetas), required=True),
                                                        "Cantidad": st.column_config.NumberColumn("Cantidad", min_value=0.0, step=1.0, format="%.2f")})
                guardar = st.form_submit_button("💾 Guardar", type="primary")
            if guardar:
                nuevas = [{"producto_id": etiquetas[r["Producto"]], "cantidad": 0.0 if pd.isna(r["Cantidad"]) else r["Cantidad"]}
                          for _, r in editado.iterrows() if pd.notna(r["Producto"]) and r["Producto"] in etiquetas]
                if _intentar(despachos_db.reemplazar_lineas, usuario, despacho_id=did, lineas=nuevas):
                    _flash("Cantidades actualizadas.")
                    st.rerun()
        with st.expander("✅ Confirmar despacho (salida física registrada contra la OV)", expanded=False):
            st.caption("Descuenta TODO el pedido con FEFO en una sola operación, actualiza el kardex (OV, cliente, guía) y "
                       "lo despachado de la OV. Si algo no alcanza, no se descuenta nada.")
            guia = st.text_input("Guía de remisión", value=d["guia_remision"] or "", placeholder="T001-00001234", key=f"des_cg_{did}")
            conf = st.checkbox("Confirmo que la mercadería fue preparada y despachada", key=f"des_cc_{did}")
            if st.button("✅ Confirmar despacho", type="primary", disabled=not conf or bool(faltantes) or d["ov_id"] is None,
                         key=f"des_cb_{did}"):
                ok, n = _ejecutar(despachos_db.confirmar_despacho, usuario, despacho_id=did, guia_remision=guia)
                if ok:
                    _flash(f"Despacho {d['numero_despacho']} confirmado contra {d['numero_ov']}: {n} producto(s) descontados con FEFO. "
                           "Packing List guardado en el expediente de la OV.")
                    st.rerun()
        with st.expander("⛔ Anular", expanded=False):
            if st.button("Anular este borrador", key=f"des_an_{did}") and _intentar(despachos_db.anular_despacho, usuario, did):
                _flash(f"Despacho {d['numero_despacho']} anulado.")
                st.rerun()
    if d["ov_id"] and d["estado"] != "Anulado":
        with st.expander("📎 Cargar Guía de Remisión (al expediente de la OV)", expanded=d["estado"] == "Despachado"):
            n = st.session_state.setdefault(f"des_gn_{did}", 0)
            archivos = st.file_uploader("Guía de remisión (PDF o foto)", accept_multiple_files=True,
                                        type=["pdf", "png", "jpg", "jpeg"], key=f"des_gf_{did}_{n}")
            if st.button("📤 Guardar en el expediente", key=f"des_gb_{did}_{n}", disabled=not archivos):
                res = None
                for f in archivos or []:
                    ok, res = _ejecutar(expediente_ov.adjuntar_documento, usuario, int(d["ov_id"]), "GUIA_REMISION", f.name,
                                        f.getvalue(), modulo="despachos", descripcion=f"Guía {d['guia_remision'] or ''}".strip())
                if res:
                    _flash(mensaje_cierre(res["cierre"]))
                    st.session_state[f"des_gn_{did}"] = n + 1
                    st.rerun()
    if d["estado"] != "Despachado":
        with st.expander("🗑️ Eliminar", expanded=False):
            conf = st.checkbox("Confirmo que deseo eliminar este despacho", key=f"des_dc_{did}")
            if st.button("🗑️ Eliminar despacho", disabled=not conf, key=f"des_db_{did}") and _intentar(despachos_db.eliminar_despacho, usuario, did):
                _flash(f"Despacho {d['numero_despacho']} eliminado.")
                st.session_state.pop("des_sel", None)
                st.rerun()


def render(usuario: dict) -> None:
    _preparar()
    escribe = puede_escribir(usuario, "despachos")
    st.title("🚚 Despachos")
    if not escribe:
        st.caption("👁️ Modo solo consulta: puede revisar despachos y descargar Pre-picking y Packing List.")
    _mostrar_flash()
    df = despachos_db.listar_despachos()
    c1, c2, c3 = st.columns(3)
    c1.metric("Borradores", int((df["estado"] == "Borrador").sum()) if not df.empty else 0)
    c2.metric("Despachados", int((df["estado"] == "Despachado").sum()) if not df.empty else 0)
    c3.metric("Unidades por despachar", f"{df.loc[df['estado'] == 'Borrador', 'unidades'].sum():,.0f}" if not df.empty else "0")
    tabs = st.tabs(["🚚 Despachos"] + (["➕ Nuevo despacho"] if escribe else []))
    with tabs[0]:
        _tab_lista(usuario, escribe)
    if escribe:
        with tabs[1]:
            _tab_nuevo(usuario)
