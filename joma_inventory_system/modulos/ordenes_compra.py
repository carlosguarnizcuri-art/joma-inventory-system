"""
modulos/ordenes_compra.py - Órdenes de Compra (FASE 3: sección independiente del menú)

Pestañas:
  1. 📦 Órdenes de compra    -> Admin y Lector (el Lector solo consulta)
       * numeración automática correlativa (OC-00046), detalle de productos, estados Abierto / Cerrado,
       * ⚡ plantillas de compra para autocompletar con un clic,
       * 👁️ vista previa de la OC (1 clic) con descarga en PDF y Excel,
       * 📥 importación masiva desde Excel / CSV con plantilla descargable.
  2. 📋 Plantillas de compra -> Admin y Lector (el Lector solo consulta)
  3. 📥 Recepción de mercadería (OC -> inventario) -> SOLO Admin

Permisos: los formularios de escritura solo se muestran a Admin (rol exigido en el parámetro y en
st.session_state) y cada función de escritura vuelve a validarlo contra la BD.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd
import streamlit as st

from modulos import carga_masiva
from modulos import compras_db as cdb
from modulos import documentos as docs
from modulos import documentos_pdf as pdf
from modulos import exportacion as exp
from modulos import inventario as inv   # reutiliza helpers validados (búsqueda, fechas, productos)
from modulos import plantillas_db
from modulos.compras_ui import (MONEDAS, SIMBOLO, a_fecha as _a_fecha, dinero as _dinero, ejecutar as _ejecutar,
                                flash as _flash, fmt_fecha as _fmt_fecha, intentar as _intentar,
                                mostrar_flash as _mostrar_flash, panel_documentos as _panel_documentos, es_admin_sesion)

COLUMNAS_EXPORT_OC = {
    "estado_visual": "Estado", "numero_oc": "N° OC", "proveedor": "Proveedor", "fecha_emision": "Fecha de emisión",
    "fecha_entrega_estimada": "Entrega estimada", "monto_total": "Monto", "moneda": "Moneda",
    "avance": "Recepción (%)", "n_facturas": "Facturas", "n_docs": "Documentos",
}


@st.cache_resource(show_spinner=False)
def _preparar_modulo() -> bool:
    cdb.asegurar_esquema()
    plantillas_db.asegurar_esquema()
    docs.inicializar_estructura()
    return True


def _resumen_oc() -> None:
    r = cdb.resumen_compras()
    c1, c2 = st.columns(2)
    c1.metric("OC abiertas", int(r["oc_abiertas"]))
    c2.metric("OC atrasadas", int(r["oc_atrasadas"]),
              delta="Revisar" if r["oc_atrasadas"] else None, delta_color="inverse")
    if r["oc_atrasadas"]:
        st.warning(f"🟠 {int(r['oc_atrasadas'])} orden(es) de compra pasaron su fecha de entrega estimada.")


# ===========================================================================
# NUEVA OC: PLANTILLAS (autocompletar) + IMPORTACIÓN MASIVA
# ===========================================================================
def _lineas_vacias() -> pd.DataFrame:
    return pd.DataFrame({"Producto": pd.Series(dtype="object"), "Cantidad": pd.Series(dtype="float64"),
                         "Precio unitario": pd.Series(dtype="float64")})


def _form_nueva_oc(usuario: dict) -> None:
    productos = inv.cargar_productos(solo_activos=True)
    etiquetas = {f"{r.codigo} · {r.nombre}": int(r.id) for r in productos.itertuples()}
    por_id = {v: k for k, v in etiquetas.items()}
    plantillas = plantillas_db.listar_plantillas()
    opt_tpl: dict[int, str] = {0: "— Sin plantilla —"}
    opt_tpl.update({int(r.id): f"{r.nombre} ({int(r.lineas)} líneas)" for r in plantillas.itertuples()})
    n = st.session_state.setdefault("oc_new_n", 0)     # Cambiar 'n' reinicia el formulario
    hoy = date.today()

    with st.expander("➕ Nueva orden de compra", expanded=False):
        tpl_id = st.selectbox("⚡ Plantilla de compra (autocompleta proveedor, moneda y productos)", list(opt_tpl),
                              format_func=opt_tpl.get, key=f"oc_tpl_sel_{n}")
        tpl = plantillas_db.obtener_plantilla(tpl_id) if tpl_id else None
        if tpl is not None:
            lt = plantillas_db.lineas_plantilla(tpl_id)
            inicial = pd.DataFrame({"Producto": [por_id.get(int(p)) for p in lt["producto_id"]],
                                    "Cantidad": lt["cantidad"].astype("float64"),
                                    "Precio unitario": lt["precio_unitario"].astype("float64")})
            st.caption(f"✅ Plantilla aplicada: {len(lt)} producto(s). Puede modificar cantidades y precios antes de guardar.")
        else:
            inicial = _lineas_vacias()
        k = f"{n}_{tpl_id}"      # al cambiar de plantilla el formulario se rearma con los valores sugeridos
        monedas = list(MONEDAS)
        with st.form(f"oc_form_nueva_{k}"):
            st.info(f"🔢 Número asignado automáticamente: **{cdb.vista_previa_numero_oc()}** "
                    "(correlativo; se confirma al guardar).")
            c2, c3, c4 = st.columns([2, 1, 1])
            proveedor = c2.text_input("Proveedor *", value=(tpl["proveedor"] or "") if tpl is not None else "",
                                      key=f"oc_new_prov_{k}")
            moneda = c3.selectbox("Moneda", monedas, format_func=MONEDAS.get,
                                  index=monedas.index(tpl["moneda"]) if tpl is not None else 0, key=f"oc_new_mon_{k}")
            emision = c4.date_input("Fecha de emisión", value=hoy, format="DD/MM/YYYY", key=f"oc_new_emi_{k}")
            c5, c6 = st.columns([1, 3])
            entrega = c5.date_input("Entrega estimada", value=None, format="DD/MM/YYYY", key=f"oc_new_ent_{k}")
            obs = c6.text_input("Observaciones", key=f"oc_new_obs_{k}")

            st.markdown("**Detalle del pedido** — use ➕ al pie de la tabla para agregar filas.")
            lineas_df = st.data_editor(
                inicial, num_rows="dynamic", use_container_width=True, hide_index=True, key=f"oc_new_lin_{k}",
                column_config={
                    "Producto": st.column_config.SelectboxColumn("Producto", options=list(etiquetas), required=True),
                    "Cantidad": st.column_config.NumberColumn("Cantidad", min_value=0.0, step=1.0, format="%.2f"),
                    "Precio unitario": st.column_config.NumberColumn("Precio unitario", min_value=0.0, step=0.1,
                                                                     format="%.2f"),
                })
            monto_manual = st.number_input("Monto total (solo si NO detalla productos)", min_value=0.0, value=0.0,
                                           step=10.0, format="%.2f", key=f"oc_new_monto_{k}")
            crear = st.form_submit_button("💾 Crear orden de compra", type="primary")

    with st.expander("📥 Importar órdenes de compra desde Excel / CSV (con plantilla)", expanded=False):
        carga_masiva.panel(usuario, "oc")

    if crear:
        lineas = [{"producto_id": etiquetas[r["Producto"]],
                   "cantidad": 0.0 if pd.isna(r["Cantidad"]) else r["Cantidad"],
                   "precio": 0.0 if pd.isna(r["Precio unitario"]) else r["Precio unitario"]}
                  for _, r in lineas_df.iterrows() if pd.notna(r["Producto"]) and r["Producto"] in etiquetas]
        ok, oc_id = _ejecutar(cdb.crear_oc, usuario, proveedor=proveedor, fecha_emision=emision,
                              fecha_entrega=entrega, moneda=moneda, observaciones=obs, lineas=lineas,
                              monto_manual=monto_manual)
        if ok:
            _flash(f"Orden de compra {cdb.obtener_oc(oc_id)['numero_oc']} creada con {len(lineas)} línea(s) y su carpeta de documentos.")
            st.session_state["oc_new_n"] = n + 1
            st.session_state["oc_sel"] = oc_id
            st.rerun()


# ===========================================================================
# VISTA PREVIA / DOCUMENTO DE LA OC (PDF y Excel)
# ===========================================================================
def _documento_oc(oc, lineas: pd.DataFrame):
    """Datos del documento: (encabezado, columnas, filas para PDF, filas para Excel, totales)."""
    moneda = oc["moneda"]
    encabezado = [("N° de OC", oc["numero_oc"]), ("Estado", oc["estado"]), ("Proveedor", oc["proveedor"]),
                  ("Moneda", moneda), ("Fecha de emisión", _fmt_fecha(oc["fecha_emision"])),
                  ("Entrega estimada", _fmt_fecha(oc["fecha_entrega_estimada"]))]
    if oc["observaciones"]:
        encabezado.append(("Observaciones", oc["observaciones"]))
    columnas = ["#", "SKU", "Descripción", "Unidad", "Cantidad", "P. unitario", "Subtotal"]
    filas_pdf, filas_xlsx = [], []
    for i, r in enumerate(lineas.itertuples(), start=1):
        filas_pdf.append([i, r.codigo, r.producto, r.unidad, f"{r.cantidad_pedida:,.2f}",
                          f"{r.precio_unitario:,.2f}", f"{r.subtotal:,.2f}"])
        filas_xlsx.append([i, r.codigo, r.producto, r.unidad, float(r.cantidad_pedida),
                           float(r.precio_unitario), float(r.subtotal)])
    totales = [("TOTAL", _dinero(oc["monto_total"], moneda))]
    return encabezado, columnas, filas_pdf, filas_xlsx, totales


def _vista_previa_oc(usuario: dict, oc, lineas: pd.DataFrame, clave: str) -> None:
    enc, cols, filas_pdf, filas_xlsx, totales = _documento_oc(oc, lineas)
    nombre = usuario.get("nombre_completo", "")
    with st.container(border=True):
        st.markdown(f"### 🧾 ORDEN DE COMPRA {oc['numero_oc']}")
        st.markdown(f"**Proveedor:** {oc['proveedor']}  \n**Emisión:** {_fmt_fecha(oc['fecha_emision'])} · "
                    f"**Entrega estimada:** {_fmt_fecha(oc['fecha_entrega_estimada'])} · **Moneda:** {oc['moneda']}")
        if oc["observaciones"]:
            st.caption(f"📝 {oc['observaciones']}")
        if lineas.empty:
            st.info("Esta OC no tiene detalle de productos.")
        else:
            st.dataframe(pd.DataFrame(filas_xlsx, columns=cols), use_container_width=True, hide_index=True,
                         column_config={"Cantidad": st.column_config.NumberColumn(format="%.2f"),
                                        "P. unitario": st.column_config.NumberColumn(format="%.2f"),
                                        "Subtotal": st.column_config.NumberColumn(format="%.2f")})
        st.markdown(f"**TOTAL: {_dinero(oc['monto_total'], oc['moneda'])}**")
        b1, b2, b3 = st.columns([1.3, 1.3, 1])
        b1.download_button(
            "⬇️ Descargar PDF", pdf.pdf_documento(
                f"Orden de Compra {oc['numero_oc']}", encabezado=enc, columnas=cols, filas=filas_pdf,
                anchos=[0.5, 1.3, 4, 1, 1.2, 1.3, 1.4], alinear_derecha=[4, 5, 6], totales=totales,
                firmas=["Elaborado por", "Aprobado por"], generado_por=nombre),
            file_name=f"{oc['numero_oc']}.pdf", mime=pdf.MIME_PDF, key=f"{clave}_pdf")
        b2.download_button(
            "⬇️ Descargar Excel", pdf.excel_documento(
                f"Orden de Compra {oc['numero_oc']}", encabezado=enc, columnas=cols, filas=filas_xlsx,
                totales=totales, nombre_hoja="Orden de compra", generado_por=nombre),
            file_name=f"{oc['numero_oc']}.xlsx", mime=exp.MIME_XLSX, key=f"{clave}_xlsx")
        if b3.button("✖ Cerrar vista previa", key=f"{clave}_cerrar"):
            st.session_state[clave] = False
            st.rerun()


# ===========================================================================
# PESTAÑA: PLANTILLAS DE COMPRA
# ===========================================================================
def _form_nueva_plantilla(usuario: dict) -> None:
    productos = inv.cargar_productos(solo_activos=True)
    etiquetas = {f"{r.codigo} · {r.nombre}": int(r.id) for r in productos.itertuples()}
    n = st.session_state.setdefault("tpl_new_n", 0)
    with st.expander("➕ Nueva plantilla de compra", expanded=False):
        with st.form(f"tpl_form_nueva_{n}"):
            c1, c2, c3 = st.columns([2, 2, 1])
            nombre = c1.text_input("Nombre de la plantilla *", placeholder="Reposición mensual abarrotes", key=f"tpl_new_nom_{n}")
            proveedor = c2.text_input("Proveedor", key=f"tpl_new_prov_{n}")
            moneda = c3.selectbox("Moneda", list(MONEDAS), format_func=MONEDAS.get, key=f"tpl_new_mon_{n}")
            obs = st.text_input("Observaciones", key=f"tpl_new_obs_{n}")
            st.markdown("**Productos** — use ➕ al pie de la tabla para agregar filas.")
            lineas_df = st.data_editor(
                _lineas_vacias(), num_rows="dynamic", use_container_width=True, hide_index=True, key=f"tpl_new_lin_{n}",
                column_config={
                    "Producto": st.column_config.SelectboxColumn("Producto", options=list(etiquetas), required=True),
                    "Cantidad": st.column_config.NumberColumn("Cantidad", min_value=0.0, step=1.0, format="%.2f"),
                    "Precio unitario": st.column_config.NumberColumn("Precio unitario", min_value=0.0, step=0.1,
                                                                     format="%.2f")})
            crear = st.form_submit_button("💾 Guardar plantilla", type="primary")
    if crear:
        lineas = [{"producto_id": etiquetas[r["Producto"]],
                   "cantidad": 0.0 if pd.isna(r["Cantidad"]) else r["Cantidad"],
                   "precio": 0.0 if pd.isna(r["Precio unitario"]) else r["Precio unitario"]}
                  for _, r in lineas_df.iterrows() if pd.notna(r["Producto"]) and r["Producto"] in etiquetas]
        if _intentar(plantillas_db.crear_plantilla, usuario, nombre=nombre, proveedor=proveedor, moneda=moneda,
                     observaciones=obs, lineas=lineas):
            _flash(f"Plantilla '{' '.join(nombre.split())}' guardada.")
            st.session_state["tpl_new_n"] = n + 1
            st.rerun()


def _tab_plantillas(usuario: dict, es_admin: bool) -> None:
    st.markdown("Las **plantillas de compra** guardan proveedor, moneda y productos con cantidades y precios de "
                "referencia. En *Nueva orden de compra* elija una plantilla y el formulario se autocompleta.")
    df = plantillas_db.listar_plantillas()
    if es_admin:
        _form_nueva_plantilla(usuario)
    if df.empty:
        st.info("Aún no hay plantillas." + (" Cree una arriba o use 'Guardar como plantilla' en el detalle de una OC."
                                            if es_admin else ""))
        return
    st.dataframe(df[["nombre", "proveedor", "moneda", "lineas", "creado_en"]], use_container_width=True,
                 hide_index=True, column_config={
                     "nombre": "Plantilla", "proveedor": "Proveedor", "moneda": "Mon.",
                     "lineas": st.column_config.NumberColumn("Productos", format="%d"), "creado_en": "Creada"})
    opt = {int(r.id): r.nombre for r in df.itertuples()}
    tid = st.selectbox("Ver detalle de la plantilla", list(opt), format_func=opt.get, key="tpl_sel")
    lin = plantillas_db.lineas_plantilla(int(tid))
    st.dataframe(lin[["codigo", "producto", "unidad", "cantidad", "precio_unitario"]], use_container_width=True,
                 hide_index=True, column_config={
                     "codigo": "SKU", "producto": "Producto", "unidad": "Unidad",
                     "cantidad": st.column_config.NumberColumn("Cantidad", format="%.2f"),
                     "precio_unitario": st.column_config.NumberColumn("P. unitario", format="%.2f")})
    if es_admin:
        conf = st.checkbox("Confirmo que deseo eliminar esta plantilla", key=f"tpl_delconf_{tid}")
        if st.button("🗑️ Eliminar plantilla", disabled=not conf, key=f"tpl_del_{tid}"):
            if _intentar(plantillas_db.eliminar_plantilla, usuario, int(tid)):
                _flash(f"Plantilla '{opt[tid]}' eliminada.")
                st.rerun()


# ===========================================================================
# ÓRDENES (lista, detalle y acciones) Y RECEPCIÓN — código validado en el Módulo 3
# ===========================================================================
def _limpiar_filtros_oc() -> None:
    st.session_state["oc_q"] = ""
    st.session_state["oc_f_estado"] = "Todas"


def _tab_oc(usuario: dict, es_admin: bool) -> None:
    if es_admin:
        _form_nueva_oc(usuario)

    df = cdb.listar_ocs()
    with st.container(border=True):
        c1, c2, c3 = st.columns([3, 2, 1.3], vertical_alignment="bottom")
        buscar = c1.text_input("🔎 Buscar por N° de OC o proveedor", key="oc_q", placeholder="Ej. OC-2026-003, Gloria")
        estado = c2.selectbox("Estado", ["Todas", "Abiertas", "Atrasadas", "Cerradas"], key="oc_f_estado")
        c3.button("🧹 Limpiar filtros", key="oc_btn_limpiar", on_click=_limpiar_filtros_oc)

    dff = inv._aplicar_busqueda(df, buscar, ["numero_oc", "proveedor"])
    if estado == "Abiertas":
        dff = dff[dff["estado"] == "Abierto"]
    elif estado == "Atrasadas":
        dff = dff[dff["estado_visual"] == "🔴 Atrasada"]
    elif estado == "Cerradas":
        dff = dff[dff["estado"] == "Cerrado"]

    cols = ["estado_visual", "numero_oc", "proveedor", "fecha_emision", "fecha_entrega_estimada", "monto_total",
            "moneda", "avance", "n_facturas", "n_docs"]
    if dff.empty:
        st.info("Ninguna orden de compra coincide con los filtros.")
    else:
        st.dataframe(
            inv._fechas(dff[cols], ["fecha_emision", "fecha_entrega_estimada"]), use_container_width=True,
            hide_index=True, column_config={
                "estado_visual": "Estado", "numero_oc": "N° OC", "proveedor": "Proveedor",
                "fecha_emision": st.column_config.DateColumn("Emisión", format="DD/MM/YYYY"),
                "fecha_entrega_estimada": st.column_config.DateColumn("Entrega est.", format="DD/MM/YYYY"),
                "monto_total": st.column_config.NumberColumn("Monto", format="%.2f"),
                "moneda": "Mon.",
                "avance": st.column_config.ProgressColumn("Recepción", min_value=0, max_value=100, format="%.0f%%",
                                                          help="% de unidades pedidas ya recibidas en inventario."),
                "n_facturas": st.column_config.NumberColumn("Facturas", format="%d"),
                "n_docs": st.column_config.NumberColumn("Docs", format="%d"),
            })
    st.caption(f"Mostrando {len(dff)} de {len(df)} orden(es). El CSV exporta lo filtrado.")
    exp.boton_descarga_csv(
        exp.preparar_export(dff, COLUMNAS_EXPORT_OC,
                             fechas={"fecha_emision": "%d/%m/%Y", "fecha_entrega_estimada": "%d/%m/%Y"},
                             estados=["estado_visual"], dos_decimales=["monto_total", "avance"]),
        "ordenes_compra", "oc_csv")

    if dff.empty:
        return
    st.divider()
    opciones = {int(r.id): f"{r.numero_oc} · {r.proveedor} · {r.estado_visual}" for r in dff.itertuples()}
    if st.session_state.get("oc_sel") not in opciones:
        st.session_state.pop("oc_sel", None)
    oc_id = st.selectbox("📂 Ver detalle de la orden de compra", list(opciones), format_func=opciones.get, key="oc_sel")
    _detalle_oc(usuario, es_admin, int(oc_id))


def _detalle_oc(usuario: dict, es_admin: bool, oc_id: int) -> None:
    oc = cdb.obtener_oc(oc_id)
    if oc is None:
        st.warning("La orden de compra ya no existe.")
        return
    lineas = cdb.lineas_oc(oc_id)
    facturas = cdb.facturas_de_oc(oc_id)
    visual = cdb.estado_oc_visual(oc["estado"], oc["fecha_entrega_estimada"], date.today().isoformat())

    st.subheader(f"{oc['numero_oc']} · {oc['proveedor']}")
    pedido, recibido = lineas["cantidad_pedida"].sum(), lineas["cantidad_recibida"].sum()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Estado", visual)
    c2.metric("Monto", _dinero(oc["monto_total"], oc["moneda"]))
    c3.metric("Recepción", f"{recibido / pedido * 100:.0f} %" if pedido else "Sin detalle")
    c4.metric("Entrega estimada", _fmt_fecha(oc["fecha_entrega_estimada"]))
    c5.metric("Emisión", _fmt_fecha(oc["fecha_emision"]))
    if oc["observaciones"]:
        st.caption(f"📝 {oc['observaciones']}")

    st.markdown("##### Líneas del pedido")
    if lineas.empty:
        st.info("Esta OC no tiene detalle de productos" +
                (" (puede agregarlo en 'Líneas del pedido' más abajo)." if es_admin else "."))
    else:
        st.dataframe(lineas[["codigo", "producto", "unidad", "cantidad_pedida", "cantidad_recibida", "pendiente",
                             "precio_unitario", "subtotal"]],
                     use_container_width=True, hide_index=True, column_config={
                         "codigo": "Código", "producto": "Producto", "unidad": "Unidad",
                         "cantidad_pedida": st.column_config.NumberColumn("Pedido", format="%.2f"),
                         "cantidad_recibida": st.column_config.NumberColumn("Recibido", format="%.2f"),
                         "pendiente": st.column_config.NumberColumn("Pendiente", format="%.2f"),
                         "precio_unitario": st.column_config.NumberColumn("P. unitario", format="%.2f"),
                         "subtotal": st.column_config.NumberColumn("Subtotal", format="%.2f")})

    if not facturas.empty:
        st.markdown("##### Facturas vinculadas")
        st.dataframe(inv._fechas(facturas.drop(columns=["id"]), ["fecha_emision", "fecha_vencimiento_pago"]),
                     use_container_width=True, hide_index=True, column_config={
                         "numero_factura": "N° factura",
                         "fecha_emision": st.column_config.DateColumn("Emisión", format="DD/MM/YYYY"),
                         "fecha_vencimiento_pago": st.column_config.DateColumn("Vence pago", format="DD/MM/YYYY"),
                         "monto_total": st.column_config.NumberColumn("Monto", format="%.2f"),
                         "moneda": "Mon.", "estado": "Estado"})

    # Vista previa de la OC (1 clic) con descarga en PDF y Excel
    pk = f"oc_prev_{oc_id}"
    if st.session_state.get(pk):
        _vista_previa_oc(usuario, oc, lineas, pk)
    elif st.button("👁️ Vista previa de la OC (1 clic)", key=f"{pk}_btn"):
        st.session_state[pk] = True
        st.rerun()

    with st.expander("📎 Documentos adjuntos y carpeta", expanded=False):
        _panel_documentos(usuario, es_admin, "OC", oc_id)

    if es_admin:
        _acciones_oc(usuario, oc, lineas, len(facturas))


def _acciones_oc(usuario: dict, oc, lineas: pd.DataFrame, n_facturas: int) -> None:
    oc_id = int(oc["id"])
    abierta = oc["estado"] == "Abierto"
    st.markdown("##### 🛠️ Acciones de administración")
    if not abierta:
        st.info("La OC está cerrada. Reábrala para editarla o recibir mercadería.")

    # ----- Editar cabecera
    with st.expander("✏️ Editar cabecera", expanded=False):
        if not abierta:
            st.caption("Reabra la OC para editar.")
        else:
            with st.form(f"oc_form_edit_{oc_id}"):
                c1, c2 = st.columns(2)
                proveedor = c1.text_input("Proveedor *", value=oc["proveedor"], key=f"oc_ed_prov_{oc_id}")
                entrega = c2.date_input("Entrega estimada", value=_a_fecha(oc["fecha_entrega_estimada"]),
                                        format="DD/MM/YYYY", key=f"oc_ed_ent_{oc_id}")
                monto = st.number_input("Monto total", min_value=0.0, value=float(oc["monto_total"]), step=10.0,
                                        format="%.2f", disabled=not lineas.empty, key=f"oc_ed_monto_{oc_id}",
                                        help="Si la OC tiene líneas, el monto se calcula automáticamente.")
                obs = st.text_area("Observaciones", value=inv._txt(oc["observaciones"]), key=f"oc_ed_obs_{oc_id}")
                guardar = st.form_submit_button("💾 Guardar cambios", type="primary")
            if guardar and _intentar(cdb.actualizar_oc, usuario, oc_id=oc_id, proveedor=proveedor,
                                     fecha_entrega=entrega, observaciones=obs, monto_manual=monto):
                _flash("Cabecera actualizada.")
                st.rerun()

    # ----- Líneas
    with st.expander("📋 Líneas del pedido", expanded=False):
        if not abierta:
            st.caption("Reabra la OC para modificar sus líneas.")
        else:
            productos = inv.cargar_productos(solo_activos=True)
            disponibles = productos[~productos["id"].isin(set(lineas["producto_id"]))]
            opt = {int(r.id): f"{r.codigo} · {r.nombre} ({r.unidad_medida})" for r in disponibles.itertuples()}
            n = st.session_state.setdefault(f"oc_lin_n_{oc_id}", 0)
            if opt:
                with st.form(f"oc_form_lin_{oc_id}_{n}"):
                    c1, c2, c3 = st.columns([3, 1, 1])
                    pid = c1.selectbox("Producto", list(opt), format_func=opt.get, key=f"oc_lin_prod_{oc_id}_{n}")
                    cant = c2.number_input("Cantidad", min_value=0.0, value=0.0, step=1.0, format="%.2f",
                                           key=f"oc_lin_cant_{oc_id}_{n}")
                    precio = c3.number_input("Precio unit.", min_value=0.0, value=0.0, step=0.1, format="%.2f",
                                             key=f"oc_lin_prec_{oc_id}_{n}")
                    agregar = st.form_submit_button("➕ Agregar línea", type="primary")
                if agregar and _intentar(cdb.agregar_linea_oc, usuario, oc_id=oc_id, producto_id=pid,
                                         cantidad=cant, precio_unitario=precio):
                    _flash("Línea agregada; el monto de la OC se recalculó.")
                    st.session_state[f"oc_lin_n_{oc_id}"] = n + 1
                    st.rerun()
            else:
                st.caption("Todos los productos activos ya están en la OC.")

            borrables = lineas[lineas["cantidad_recibida"] == 0]
            if not borrables.empty:
                optb = {int(r.id): f"{r.codigo} · {r.producto} (pedido: {r.cantidad_pedida:g})"
                        for r in borrables.itertuples()}
                with st.form(f"oc_form_dellin_{oc_id}"):
                    lid = st.selectbox("Eliminar línea (solo sin mercadería recibida)", list(optb),
                                       format_func=optb.get, key=f"oc_dellin_sel_{oc_id}")
                    eliminar = st.form_submit_button("🗑️ Eliminar línea")
                if eliminar and _intentar(cdb.eliminar_linea_oc, usuario, int(lid)):
                    _flash("Línea eliminada; el monto de la OC se recalculó.")
                    st.rerun()

    # ----- Estado
    with st.expander("🔒 Estado de la OC", expanded=False):
        if abierta:
            forzar = st.checkbox("Cerrar de todos modos (aunque haya unidades pendientes)", key=f"oc_forzar_{oc_id}")
            if st.button("🔒 Cerrar OC", key=f"oc_cerrar_{oc_id}"):
                if _intentar(cdb.cambiar_estado_oc, usuario, oc_id=oc_id, nuevo_estado="Cerrado",
                             forzar_con_pendientes=forzar):
                    _flash(f"OC {oc['numero_oc']} cerrada.")
                    st.rerun()
        else:
            if st.button("🔓 Reabrir OC", key=f"oc_reabrir_{oc_id}"):
                if _intentar(cdb.cambiar_estado_oc, usuario, oc_id=oc_id, nuevo_estado="Abierto"):
                    _flash(f"OC {oc['numero_oc']} reabierta.")
                    st.rerun()

    # ----- Guardar como plantilla
    with st.expander("📋 Guardar como plantilla", expanded=False):
        st.caption("Guarda proveedor, moneda y productos de esta OC para reutilizarlos con un clic en 'Nueva orden de compra'.")
        nombre_tpl = st.text_input("Nombre de la plantilla (opcional)", key=f"oc_tplname_{oc_id}")
        if st.button("📋 Guardar como plantilla", disabled=lineas.empty, key=f"oc_tplsave_{oc_id}"):
            if _intentar(plantillas_db.plantilla_desde_oc, usuario, oc_id=oc_id, nombre=nombre_tpl):
                _flash("Plantilla creada. Ya puede usarla al crear una nueva OC.")
                st.rerun()

    # ----- Eliminar
    with st.expander("🗑️ Eliminar OC", expanded=False):
        st.caption("Solo es posible si no tiene mercadería recibida ni facturas vinculadas. "
                   "Los archivos físicos de su carpeta NO se borran.")
        conf = st.checkbox("Confirmo que deseo eliminar esta orden de compra", key=f"oc_delconf_{oc_id}")
        if st.button("🗑️ Eliminar OC", disabled=not conf, key=f"oc_del_{oc_id}"):
            if _intentar(cdb.eliminar_oc, usuario, oc_id):
                _flash(f"OC {oc['numero_oc']} eliminada.")
                st.session_state.pop("oc_sel", None)
                st.rerun()


def _tab_recepcion(usuario: dict) -> None:
    st.markdown("Reciba mercadería contra una orden de compra: se crea el **lote en inventario**, se actualiza "
                "lo recibido de la OC y se registra la **entrada en el kardex**, todo en una sola operación.")
    df = cdb.ocs_con_pendientes()
    if df.empty:
        st.info("No hay órdenes de compra abiertas con unidades pendientes de recibir. "
                "Cree una OC con detalle de productos o agregue líneas a una existente.")
        return

    opt = {int(r.id): f"{r.numero_oc} · {r.proveedor} · pendiente: {r.pendiente:g}" for r in df.itertuples()}
    if st.session_state.get("rec_oc") not in opt:
        st.session_state.pop("rec_oc", None)
    oc_id = st.selectbox("Orden de compra", list(opt), format_func=opt.get, key="rec_oc")

    lineas = cdb.lineas_oc(int(oc_id))
    st.dataframe(lineas[["codigo", "producto", "unidad", "cantidad_pedida", "cantidad_recibida", "pendiente"]],
                 use_container_width=True, hide_index=True, column_config={
                     "codigo": "Código", "producto": "Producto", "unidad": "Unidad",
                     "cantidad_pedida": st.column_config.NumberColumn("Pedido", format="%.2f"),
                     "cantidad_recibida": st.column_config.NumberColumn("Recibido", format="%.2f"),
                     "pendiente": st.column_config.NumberColumn("Pendiente", format="%.2f")})

    pend = lineas[lineas["pendiente"] > 0]
    opt_l = {int(r.id): f"{r.codigo} · {r.producto} — pendiente: {r.pendiente:g} {r.unidad}" for r in pend.itertuples()}
    if st.session_state.get(f"rec_lin_{oc_id}") not in opt_l:
        st.session_state.pop(f"rec_lin_{oc_id}", None)
    linea_id = st.selectbox("Línea a recibir", list(opt_l), format_func=opt_l.get, key=f"rec_lin_{oc_id}")
    linea = pend[pend["id"] == linea_id].iloc[0]

    facturas = cdb.facturas_de_oc(int(oc_id))
    opt_f: dict[Optional[int], str] = {None: "— Sin factura —"}
    opt_f.update({int(r.id): f"{r.numero_factura} [{r.estado}]" for r in facturas.itertuples()})

    hoy = date.today()
    n = st.session_state.setdefault("rec_n", 0)
    k = f"{oc_id}_{linea_id}_{n}"
    with st.form(f"rec_form_{k}"):
        c1, c2, c3 = st.columns(3)
        cantidad = c1.number_input("Cantidad recibida *", min_value=0.0, max_value=float(linea["pendiente"]),
                                   value=float(linea["pendiente"]), step=1.0, format="%.2f", key=f"rec_cant_{k}")
        lote = c2.text_input("Número de lote *", key=f"rec_lote_{k}")
        ubicacion = c3.text_input("Ubicación (almacén / estante)", key=f"rec_ubi_{k}")
        c4, c5, c6 = st.columns(3)
        no_perecible = c4.checkbox("No perecible (sin vencimiento)", key=f"rec_np_{k}")
        vence = c5.date_input("Fecha de vencimiento", value=hoy + timedelta(days=180), min_value=hoy,
                              format="DD/MM/YYYY", key=f"rec_ven_{k}")
        factura_id = c6.selectbox("Factura asociada", list(opt_f), format_func=opt_f.get, key=f"rec_fac_{k}")
        cerrar = st.checkbox("Cerrar la OC automáticamente si queda totalmente recibida", value=True,
                             key=f"rec_cerrar_{k}")
        enviar = st.form_submit_button("📥 Recibir e ingresar al inventario", type="primary")

    if enviar:
        ok, res = _ejecutar(cdb.recibir_mercaderia, usuario, oc_id=int(oc_id), linea_id=int(linea_id),
                            cantidad=cantidad, numero_lote=lote, fecha_vencimiento=None if no_perecible else vence,
                            ubicacion=ubicacion, factura_id=factura_id, cerrar_si_completa=cerrar)
        if ok:
            msg = f"Recibidas {cantidad:g} × {linea['producto']} (lote {lote.strip().upper()}) en el inventario."
            if res["cerrada"]:
                msg += " La OC quedó completa y se cerró."
            elif res["completa"]:
                msg += " La OC quedó completamente recibida; puede cerrarla desde el detalle."
            _flash(msg)
            st.session_state["rec_n"] = n + 1
            st.rerun()



# ===========================================================================
# PUNTO DE ENTRADA (invocado por app.py)
# ===========================================================================
def render(usuario: dict) -> None:
    """Admin: gestiona OC, plantillas y recepción. Lector: solo consulta."""
    _preparar_modulo()
    es_admin = es_admin_sesion(usuario)
    st.title("🛒 Órdenes de Compra")
    if not es_admin:
        st.caption("👁️ Modo solo consulta: puede revisar órdenes, plantillas y ver/descargar la vista previa de cada OC.")
    _mostrar_flash()
    _resumen_oc()

    nombres = ["📦 Órdenes de compra", "📋 Plantillas de compra"] + (["📥 Recepción de mercadería"] if es_admin else [])
    tabs = st.tabs(nombres)
    with tabs[0]:
        _tab_oc(usuario, es_admin)
    with tabs[1]:
        _tab_plantillas(usuario, es_admin)
    if es_admin:
        with tabs[2]:
            _tab_recepcion(usuario)
