"""
modulos/ordenes_venta.py - Órdenes de Venta y expediente digital (FASE 4, interfaz)

  * Espejo operativo de las Órdenes de Compra: lista con filtros, detalle, edición y anulación.
  * Correlativo automático OV001-26 y PDF corporativo (logo + RUC) generado solo.
  * Expediente digital: carpeta data/documentos_ov/<OV>/ con checklist (OC del cliente, Guía de Remisión,
    Factura FIRMADA). La OV sigue «Abierta» advirtiendo los faltantes y se CIERRA SOLA al completarse.
Permisos: escriben Admin y Comercial; los demás roles consultan y descargan.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from modulos import clientes_db, despachos_db, expediente_ov, identidad, ventas_db
from modulos import exportacion as exp
from modulos import inventario as inv
from modulos.compras_ui import (MONEDAS, SIMBOLO, a_fecha as _a_fecha, ejecutar as _ejecutar, flash as _flash,
                                fmt_fecha as _fmt_fecha, intentar as _intentar, mensaje_cierre, mostrar_flash as _mostrar_flash,
                                puede_escribir)

TIPOS_CARGA = {k: v for k, v in ventas_db.TIPOS_DOCUMENTO.items() if k != "ORDEN_VENTA"}
COLUMNAS_EXPORT = {"estado_visual": "Estado", "numero_ov": "N° OV", "cliente": "Cliente", "documento": "RUC / DNI",
                   "oc_cliente": "OC del cliente", "fecha_emision": "Fecha de emisión", "fecha_entrega_estimada": "Entrega estimada",
                   "monto_total": "Total (con IGV)", "moneda": "Moneda", "avance": "Despachado (%)", "faltantes": "Documentos faltantes"}


@st.cache_resource(show_spinner=False)
def _preparar() -> bool:
    inv._asegurar_tabla_movimientos()
    despachos_db.asegurar_esquema()      # crea también las tablas de ventas
    return True


def _limpiar_filtros() -> None:
    st.session_state["ov_q"] = ""
    st.session_state["ov_estado"] = "Todas"
    st.session_state["ov_solo_falt"] = False


# ===========================================================================
# NUEVA OV (cliente por RUC/DNI -> datos -> productos)
# ===========================================================================
def _tab_nueva(usuario: dict) -> None:
    n = st.session_state.setdefault("ov_new_n", 0)
    st.info(f"🔢 Número asignado automáticamente: **{ventas_db.vista_previa_numero_ov()}** (correlativo anual; se confirma al guardar).")
    numero = st.text_input("RUC o DNI del cliente *", key=f"ov_cli_{n}", placeholder="Ej. 20100070970", max_chars=20)
    doc, msg = identidad.evaluar_documento(numero)
    cliente = None
    if numero.strip():
        if doc is None:
            st.error(f"⛔ {msg}")
        else:
            cliente = clientes_db.obtener_cliente_por_documento(doc.numero)
            if cliente is None:
                st.warning(f"⚠️ No existe un cliente con {doc.etiqueta}. Regístrelo primero en 👥 Clientes.")
                cliente = None
            elif not cliente["activo"]:
                st.error("⛔ El cliente está inactivo.")
                cliente = None
            else:
                st.success(f"✅ {cliente['razon_social']} · {cliente['direccion'] or 'sin dirección registrada'}")
    productos = inv.cargar_productos(solo_activos=True)
    etiquetas = {f"{r.codigo} · {r.nombre} (precio lista {r.precio_venta:,.2f})": (int(r.id), float(r.precio_venta))
                 for r in productos.itertuples()}
    k = f"{n}_{cliente['id'] if cliente is not None else 'x'}"
    monedas = list(MONEDAS)
    with st.form(f"ov_form_nueva_{k}"):
        sin = cliente is None
        c1, c2, c3, c4 = st.columns([1, 1, 1, 1.6])
        moneda = c1.selectbox("Moneda", monedas, format_func=MONEDAS.get, disabled=sin, key=f"ov_mon_{k}")
        emision = c2.date_input("Fecha de emisión", value=date.today(), format="DD/MM/YYYY", disabled=sin, key=f"ov_emi_{k}")
        entrega = c3.date_input("Entrega estimada", value=None, format="DD/MM/YYYY", disabled=sin, key=f"ov_ent_{k}")
        oc_cli = c4.text_input("N° de OC del cliente", disabled=sin, key=f"ov_occ_{k}")
        condiciones = st.text_input("Condiciones comerciales", placeholder="Ej. Pago a 30 días. Entrega en almacén del cliente.",
                                    disabled=sin, key=f"ov_cond_{k}")
        obs = st.text_input("Observaciones", disabled=sin, key=f"ov_obs_{k}")
        st.markdown("**Productos** — use ➕ al pie de la tabla. Precio vacío o 0 = precio de lista. Los precios no incluyen IGV (18 %).")
        lineas_df = st.data_editor(
            pd.DataFrame({"Producto": pd.Series(dtype="object"), "Cantidad": pd.Series(dtype="float64"),
                          "Precio unitario": pd.Series(dtype="float64")}),
            num_rows="dynamic", use_container_width=True, hide_index=True, key=f"ov_lin_{k}",
            column_config={"Producto": st.column_config.SelectboxColumn("Producto", options=list(etiquetas), required=True),
                           "Cantidad": st.column_config.NumberColumn("Cantidad", min_value=0.0, step=1.0, format="%.2f"),
                           "Precio unitario": st.column_config.NumberColumn("Precio unitario", min_value=0.0, step=0.1, format="%.2f")})
        crear = st.form_submit_button("💾 Crear orden de venta y generar PDF", type="primary", disabled=sin)
    if crear and cliente is not None:
        lineas = []
        for _, r in lineas_df.iterrows():
            if pd.notna(r["Producto"]) and r["Producto"] in etiquetas:
                pid, lista = etiquetas[r["Producto"]]
                precio = r["Precio unitario"]
                lineas.append({"producto_id": pid, "cantidad": 0.0 if pd.isna(r["Cantidad"]) else r["Cantidad"],
                               "precio": lista if (pd.isna(precio) or precio == 0) else precio})
        ok, ov_id = _ejecutar(ventas_db.crear_ov, usuario, cliente_id=int(cliente["id"]), oc_cliente=oc_cli,
                              fecha_emision=emision, fecha_entrega=entrega, moneda=moneda, condiciones=condiciones,
                              observaciones=obs, lineas=lineas)
        if ok:
            o = ventas_db.obtener_ov(ov_id)
            tiene_pdf = (expediente_ov.listar_documentos(ov_id)["tipo_documento"] == "ORDEN_VENTA").any()
            _flash(f"Orden de venta {o['numero_ov']} creada. " + ("PDF corporativo y carpeta del expediente listos."
                   if tiene_pdf else "La carpeta se creó; use «Regenerar PDF» en el detalle."), "success" if tiene_pdf else "warning")
            st.session_state["ov_new_n"] = n + 1
            st.session_state["ov_sel"] = ov_id
            st.rerun()


# ===========================================================================
# LISTA Y DETALLE
# ===========================================================================
def _tab_lista(usuario: dict, escribe: bool) -> None:
    df = ventas_db.listar_ov()
    if df.empty:
        st.info("Aún no hay órdenes de venta." + (" Cree la primera en la pestaña ➕ Nueva OV." if escribe else ""))
        return
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([3, 1.6, 1.8, 1.2], vertical_alignment="bottom")
        buscar = c1.text_input("🔎 Buscar por N° de OV, cliente, RUC/DNI u OC del cliente", key="ov_q")
        estado = c2.selectbox("Estado", ["Todas", "Abiertas", "Atrasadas", "Cerradas", "Anuladas"], key="ov_estado")
        solo_falt = c3.checkbox("Solo con documentos faltantes", key="ov_solo_falt")
        c4.button("🧹 Limpiar", key="ov_btn_limpiar", on_click=_limpiar_filtros)
    dff = inv._aplicar_busqueda(df, buscar, ["numero_ov", "cliente", "documento", "oc_cliente"])
    dff = {"Abiertas": dff[dff["estado"] == "Abierta"], "Atrasadas": dff[dff["estado_visual"] == "🔴 Atrasada"],
           "Cerradas": dff[dff["estado"] == "Cerrada"], "Anuladas": dff[dff["estado"] == "Anulada"]}.get(estado, dff)
    if solo_falt:
        dff = dff[(dff["faltantes"] != "") & (dff["estado"] == "Abierta")]
    if dff.empty:
        st.info("Ninguna orden de venta coincide con los filtros.")
        return
    cols = list(COLUMNAS_EXPORT)
    st.dataframe(inv._fechas(dff[cols], ["fecha_emision", "fecha_entrega_estimada"]), use_container_width=True, hide_index=True,
                 column_config={"estado_visual": "Estado", "numero_ov": "N° OV", "cliente": "Cliente", "documento": "RUC / DNI",
                                "oc_cliente": "OC del cliente",
                                "fecha_emision": st.column_config.DateColumn("Emisión", format="DD/MM/YYYY"),
                                "fecha_entrega_estimada": st.column_config.DateColumn("Entrega", format="DD/MM/YYYY"),
                                "monto_total": st.column_config.NumberColumn("Total", format="%.2f"), "moneda": "Mon.",
                                "avance": st.column_config.ProgressColumn("Despachado", min_value=0, max_value=100, format="%.0f%%"),
                                "faltantes": "Documentos faltantes"})
    st.caption(f"Mostrando {len(dff)} de {len(df)} orden(es). El CSV exporta lo filtrado.")
    exp.boton_descarga_csv(exp.preparar_export(dff, COLUMNAS_EXPORT, fechas={"fecha_emision": "%d/%m/%Y", "fecha_entrega_estimada": "%d/%m/%Y"},
                                               estados=["estado_visual"], dos_decimales=["monto_total", "avance"]), "ordenes_venta", "ov_csv")
    st.divider()
    opciones = {int(r.id): f"{r.numero_ov} · {r.cliente} · {r.estado_visual}" for r in dff.itertuples()}
    if st.session_state.get("ov_sel") not in opciones:
        st.session_state.pop("ov_sel", None)
    ov_id = st.selectbox("📂 Ver detalle de la orden de venta", list(opciones), format_func=opciones.get, key="ov_sel")
    _detalle(usuario, escribe, int(ov_id))


def _detalle(usuario: dict, escribe: bool, ov_id: int) -> None:
    ov = ventas_db.obtener_ov(ov_id)
    if ov is None:
        st.warning("La orden de venta ya no existe.")
        return
    lineas = ventas_db.lineas_ov(ov_id)
    sim = SIMBOLO[ov["moneda"]]
    visual = ventas_db.estado_ov_visual(ov["estado"], ov["fecha_entrega_estimada"], date.today().isoformat())
    st.subheader(f"{ov['numero_ov']} · {ov['cliente_nombre']}")
    pedido, desp = lineas["cantidad"].sum(), lineas["cantidad_despachada"].sum()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Estado", visual)
    c2.metric("Total (con IGV)", f"{sim} {ov['monto_total']:,.2f}")
    c3.metric("Despachado", f"{desp / pedido * 100:.0f} %" if pedido else "—")
    c4.metric("OC del cliente", ov["oc_cliente"] or "—")
    c5.metric("Entrega estimada", _fmt_fecha(ov["fecha_entrega_estimada"]))
    st.caption(f"{ov['tipo_documento']} {ov['numero_documento']} · 📍 {ov['cliente_direccion'] or 'sin dirección'}"
               + (f" · 📝 {ov['condiciones']}" if ov["condiciones"] else ""))
    st.dataframe(lineas[["codigo", "producto", "unidad", "cantidad", "cantidad_despachada", "pendiente", "precio_unitario", "subtotal"]],
                 use_container_width=True, hide_index=True, column_config={
                     "codigo": "SKU", "producto": "Descripción", "unidad": "Unidad",
                     "cantidad": st.column_config.NumberColumn("Pedido", format="%.2f"),
                     "cantidad_despachada": st.column_config.NumberColumn("Despachado", format="%.2f"),
                     "pendiente": st.column_config.NumberColumn("Pendiente", format="%.2f"),
                     "precio_unitario": st.column_config.NumberColumn("P. unit.", format="%.2f"),
                     "subtotal": st.column_config.NumberColumn("Importe", format="%.2f")})
    st.markdown(f"**Subtotal:** {sim} {ov['monto_subtotal']:,.2f} · **IGV ({ov['igv_porcentaje']:g}%):** {sim} {ov['monto_igv']:,.2f} · "
                f"**TOTAL:** {sim} {ov['monto_total']:,.2f}")
    _expediente(usuario, escribe, ov)
    if escribe and ov["estado"] != "Anulada":
        _acciones(usuario, ov, lineas)


def _expediente(usuario: dict, escribe: bool, ov) -> None:
    ov_id = int(ov["id"])
    st.markdown("##### 📁 Expediente digital")
    st.caption(f"Carpeta del servidor: `data/documentos_ov/{ov['numero_ov']}/`")
    ch = expediente_ov.checklist(ov_id)
    cols = st.columns(len(ch))
    for c, item in zip(cols, ch):
        c.metric(item["etiqueta"], "✅ Completo" if item["cumple"] else "❌ Falta", help=item["detalle"])
    faltan = [i["etiqueta"] for i in ch if not i["cumple"]]
    if ov["estado"] == "Cerrada":
        st.success("✅ Expediente completo: la OV está CERRADA (se cerró automáticamente).")
    elif ov["estado"] == "Abierta":
        st.warning("🟡 La OV permanece ABIERTA. Faltan: " + ", ".join(faltan) + ". Se cerrará sola al completar el expediente.")
    docs = expediente_ov.listar_documentos(ov_id)
    if docs.empty:
        st.info("Aún no hay documentos en el expediente.")
    else:
        vista = pd.DataFrame({"Tipo": docs["etiqueta"], "Documento": docs["nombre"], "Firmado": docs["firmado"].map({1: "✍️ Sí", 0: ""}),
                              "Origen": docs["origen"].map({"AUTO": "🤖 Sistema", "MANUAL": "👤 Cargado"}),
                              "Referencia": docs["referencia"].fillna(""), "Por": docs["subido_por"],
                              "Registrado": pd.to_datetime(docs["subido_en"]),
                              "Estado": docs["disponible"].map({True: "✅", False: "⚠️ No encontrado"})})
        st.dataframe(vista, use_container_width=True, hide_index=True,
                     column_config={"Registrado": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm")})
        opciones = {int(r.id): f"{r.etiqueta} · {r.nombre}" for r in docs.itertuples()}
        if st.session_state.get(f"ov_doc_{ov_id}") not in opciones:
            st.session_state.pop(f"ov_doc_{ov_id}", None)
        c1, c2 = st.columns([3, 1.4], vertical_alignment="bottom")
        doc_id = c1.selectbox("Documento", list(opciones), format_func=opciones.get, key=f"ov_doc_{ov_id}")
        try:
            datos, nombre, mime = expediente_ov.leer_documento(int(doc_id))
            c2.download_button("⬇️ Descargar", datos, file_name=nombre, mime=mime, key=f"ov_dl_{ov_id}")
        except ValueError as e:
            c2.caption(f"⚠️ {e}")
        fila = docs[docs["id"] == doc_id].iloc[0]
        if escribe and ov["estado"] != "Anulada":
            b1, b2, b3 = st.columns([1.4, 1.4, 3])
            if fila["tipo_documento"] == "FACTURA":
                firmado = bool(fila["firmado"])
                if b1.button("✍️ Quitar firma" if firmado else "✍️ Marcar como firmada", key=f"ov_firma_{ov_id}"):
                    ok, res = _ejecutar(expediente_ov.marcar_firmado, usuario, int(doc_id), not firmado)
                    if ok:
                        _flash(mensaje_cierre(res))
                        st.rerun()
            if fila["tipo_documento"] != "ORDEN_VENTA" and b2.button("🗑️ Retirar", key=f"ov_ret_{ov_id}",
                                                                      help="El archivo se mueve a la papelera de la carpeta."):
                ok, res = _ejecutar(expediente_ov.eliminar_documento, usuario, int(doc_id))
                if ok:
                    _flash("Documento retirado (queda en la papelera de la carpeta). " + mensaje_cierre(res))
                    st.rerun()
    if escribe and ov["estado"] != "Anulada":
        n = st.session_state.setdefault(f"ov_up_{ov_id}", 0)
        with st.container(border=True):
            st.markdown("**➕ Cargar documentos al expediente** (se guardan automáticamente en la carpeta de la OV)")
            c1, c2 = st.columns([1.5, 2])
            tipo = c1.selectbox("Tipo de documento", list(TIPOS_CARGA), format_func=TIPOS_CARGA.get, key=f"ov_tipo_{ov_id}_{n}")
            firmada = c2.checkbox("Factura firmada / con conformidad del cliente", value=True, disabled=tipo != "FACTURA",
                                  key=f"ov_firmada_{ov_id}_{n}")
            archivos = st.file_uploader("Archivos", accept_multiple_files=True, key=f"ov_files_{ov_id}_{n}")
            desc = st.text_input("Descripción (opcional)", key=f"ov_desc_{ov_id}_{n}")
            if st.button("📤 Guardar en el expediente", type="primary", disabled=not archivos, key=f"ov_save_{ov_id}_{n}"):
                res, errores = None, []
                for f in archivos or []:
                    ok, r = _ejecutar(expediente_ov.adjuntar_documento, usuario, ov_id, tipo, f.name, f.getvalue(),
                                      descripcion=desc, firmado=firmada and tipo == "FACTURA", modulo="ov")
                    res = r["cierre"] if ok else res
                if res:
                    _flash(mensaje_cierre(res))
                    st.session_state[f"ov_up_{ov_id}"] = n + 1
                    st.rerun()


def _acciones(usuario: dict, ov, lineas: pd.DataFrame) -> None:
    ov_id = int(ov["id"])
    st.markdown("##### 🛠️ Acciones")
    if st.button("🔄 Regenerar PDF de la OV", key=f"ov_regen_{ov_id}"):
        if _intentar(expediente_ov.regenerar_pdf, usuario, ov_id):
            _flash("PDF regenerado y guardado en el expediente.")
            st.rerun()
    with st.expander("✏️ Editar datos de la OV", expanded=False):
        with st.form(f"ov_form_edit_{ov_id}"):
            c1, c2 = st.columns(2)
            oc_cli = c1.text_input("N° de OC del cliente", value=ov["oc_cliente"] or "", key=f"ov_ed_oc_{ov_id}")
            entrega = c2.date_input("Entrega estimada", value=_a_fecha(ov["fecha_entrega_estimada"]), format="DD/MM/YYYY", key=f"ov_ed_ent_{ov_id}")
            cond = st.text_input("Condiciones comerciales", value=ov["condiciones"] or "", key=f"ov_ed_cond_{ov_id}")
            obs = st.text_input("Observaciones", value=ov["observaciones"] or "", key=f"ov_ed_obs_{ov_id}")
            guardar = st.form_submit_button("💾 Guardar", type="primary")
        if guardar and _intentar(ventas_db.actualizar_cabecera_ov, usuario, ov_id=ov_id, oc_cliente=oc_cli, fecha_entrega=entrega,
                                 condiciones=cond, observaciones=obs):
            _flash("Datos actualizados y PDF regenerado.")
            st.rerun()
    with st.expander("📋 Editar productos (solo si aún no hay despachos ni facturas)", expanded=False):
        productos = inv.cargar_productos(solo_activos=True)
        etiquetas = {f"{r.codigo} · {r.nombre}": int(r.id) for r in productos.itertuples()}
        por_id = {v: k for k, v in etiquetas.items()}
        inicial = pd.DataFrame({"Producto": [por_id.get(int(p)) for p in lineas["producto_id"]], "Cantidad": lineas["cantidad"].astype("float64"),
                                "Precio unitario": lineas["precio_unitario"].astype("float64")})
        with st.form(f"ov_form_lin_{ov_id}"):
            editado = st.data_editor(inicial, num_rows="dynamic", use_container_width=True, hide_index=True, key=f"ov_ed_lin_{ov_id}",
                                     column_config={"Producto": st.column_config.SelectboxColumn("Producto", options=list(etiquetas), required=True),
                                                    "Cantidad": st.column_config.NumberColumn("Cantidad", min_value=0.0, step=1.0, format="%.2f"),
                                                    "Precio unitario": st.column_config.NumberColumn("Precio unitario", min_value=0.0, step=0.1, format="%.2f")})
            guardar = st.form_submit_button("💾 Guardar productos", type="primary")
        if guardar:
            nuevas = [{"producto_id": etiquetas[r["Producto"]], "cantidad": 0.0 if pd.isna(r["Cantidad"]) else r["Cantidad"],
                       "precio": 0.0 if pd.isna(r["Precio unitario"]) else r["Precio unitario"]}
                      for _, r in editado.iterrows() if pd.notna(r["Producto"]) and r["Producto"] in etiquetas]
            if _intentar(ventas_db.reemplazar_lineas_ov, usuario, ov_id=ov_id, lineas=nuevas):
                _flash("Productos actualizados; totales y PDF recalculados.")
                st.rerun()
    with st.expander("⛔ Anular OV", expanded=False):
        st.caption("La OV no se elimina: queda como «Anulada». Solo si no tiene despachos ni facturas.")
        conf = st.checkbox("Confirmo que deseo anular esta orden de venta", key=f"ov_an_c_{ov_id}")
        if st.button("⛔ Anular", disabled=not conf, key=f"ov_an_b_{ov_id}") and _intentar(ventas_db.anular_ov, usuario, ov_id):
            _flash(f"OV {ov['numero_ov']} anulada.")
            st.rerun()


def render(usuario: dict) -> None:
    _preparar()
    escribe = puede_escribir(usuario, "ov")
    st.title("📑 Órdenes de Venta")
    if not escribe:
        st.caption("👁️ Modo solo consulta: puede revisar órdenes de venta, su expediente y descargar documentos.")
    _mostrar_flash()
    r = ventas_db.resumen_ov()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("OV abiertas", r["abiertas"])
    c2.metric("OV cerradas", r["cerradas"])
    c3.metric("Abiertas con documentos faltantes", r["con_faltantes"])
    c4.metric("Unidades por despachar", f"{r['por_despachar']:,.0f}")
    tabs = st.tabs(["📑 Órdenes de venta"] + (["➕ Nueva OV"] if escribe else []))
    with tabs[0]:
        _tab_lista(usuario, escribe)
    if escribe:
        with tabs[1]:
            _tab_nueva(usuario)
