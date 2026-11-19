"""
modulos/cuentas_cobrar.py - Cuentas por Cobrar (FASE 4): pestaña «Cuentas por Cobrar» de Facturación

Facturas de venta vinculadas a una Orden de Venta y a su cliente (estado Abierto = por cobrar,
Cerrado = cobrada). Al registrar la factura el sistema genera su comprobante y lo deposita en la carpeta
de la OV; la copia FIRMADA cargada aquí también va a esa carpeta y completa el checklist de cierre.
Permisos: escriben Admin y Comercial; los demás roles consultan.
"""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from modulos import exportacion as exp
from modulos import expediente_ov, ventas_db
from modulos import inventario as inv
from modulos.compras_ui import (SIMBOLO, a_fecha as _a_fecha, ejecutar as _ejecutar, flash as _flash, fmt_fecha as _fmt_fecha,
                                intentar as _intentar, mensaje_cierre, puede_escribir)

COLUMNAS_EXPORT = {"estado_visual": "Estado", "numero_factura": "N° factura", "numero_ov": "Orden de Venta", "cliente": "Cliente",
                   "documento": "RUC / DNI", "fecha_emision": "Fecha de emisión", "fecha_vencimiento_pago": "Vence cobro",
                   "dias_para_cobro": "Días para el cobro", "monto_subtotal": "Subtotal", "monto_igv": "IGV",
                   "monto_total": "Total", "moneda": "Moneda", "fecha_cobro": "Fecha de cobro"}


def _limpiar_filtros() -> None:
    st.session_state["cxc_q"] = ""
    st.session_state["cxc_estado"] = "Todas"


def _nueva(usuario: dict) -> None:
    n = st.session_state.setdefault("cxc_new_n", 0)
    with st.expander("➕ Registrar factura de venta (cobro)", expanded=False):
        buscar = st.text_input("🔎 Buscar OV por N° de OV, cliente, RUC/DNI u OC del cliente", key=f"cxc_q_ov_{n}")
        df = ventas_db.ov_facturables(buscar)
        if df.empty:
            st.info("No hay órdenes de venta con saldo pendiente por facturar.")
            return
        opciones = {int(r.id): f"{r.numero_ov} · {r.cliente} · por facturar {SIMBOLO[r.moneda]} {r.por_facturar:,.2f}" for r in df.itertuples()}
        ov_id = st.selectbox("Orden de Venta", list(opciones), format_func=opciones.get, key=f"cxc_ov_{n}")
        fila = df[df["id"] == ov_id].iloc[0]
        k = f"{n}_{ov_id}"
        with st.form(f"cxc_form_nueva_{k}"):
            c1, c2, c3, c4 = st.columns(4)
            numero = c1.text_input("N° de factura *", placeholder="F001-00000123", key=f"cxc_num_{k}")
            emision = c2.date_input("Fecha de emisión", value=date.today(), format="DD/MM/YYYY", key=f"cxc_emi_{k}")
            vence = c3.date_input("Vence el cobro", value=date.today() + timedelta(days=30), format="DD/MM/YYYY", key=f"cxc_ven_{k}")
            monto = c4.number_input(f"Total con IGV ({fila['moneda']}) *", min_value=0.0, value=float(fila["por_facturar"]),
                                    step=10.0, format="%.2f", key=f"cxc_monto_{k}")
            obs = st.text_input("Observaciones", key=f"cxc_obs_{k}")
            crear = st.form_submit_button("💾 Registrar factura", type="primary")
    if crear:
        ok, fid = _ejecutar(ventas_db.crear_factura_venta, usuario, ov_id=int(ov_id), numero_factura=numero, fecha_emision=emision,
                            fecha_vencimiento_pago=vence, monto_total=monto, observaciones=obs)
        if ok:
            _flash(f"Factura {ventas_db.obtener_factura_venta(fid)['numero_factura']} registrada. Su comprobante se guardó en la "
                   "carpeta de la OV; cargue la copia firmada para completar el expediente.")
            st.session_state["cxc_new_n"] = n + 1
            st.session_state["cxc_sel"] = fid
            st.rerun()


def tab(usuario: dict) -> None:
    escribe = puede_escribir(usuario, "cxc")
    r = ventas_db.resumen_cxc()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Facturas por cobrar", int(r["abiertas"]))
    c2.metric("Vencidas", int(r["vencidas"]), delta="Gestionar cobro" if r["vencidas"] else None, delta_color="inverse")
    c3.metric("Por vencer (7 d)", int(r["por_vencer"]))
    c4.metric("Por cobrar (S/)", f"S/ {r['por_cobrar_pen']:,.2f}", help=f"Además en dólares: US$ {r['por_cobrar_usd']:,.2f}")
    if r["vencidas"]:
        st.error(f"🔴 {int(r['vencidas'])} factura(s) de venta con el cobro vencido.")
    if escribe:
        _nueva(usuario)
    df = ventas_db.listar_facturas_venta()
    with st.container(border=True):
        c1, c2, c3 = st.columns([3, 2, 1.2], vertical_alignment="bottom")
        buscar = c1.text_input("🔎 Buscar por N° de factura, OV, cliente o RUC/DNI", key="cxc_q")
        estado = c2.selectbox("Estado", ["Todas", "Por cobrar", "Vencidas", "Por vencer", "Cobradas"], key="cxc_estado")
        c3.button("🧹 Limpiar", key="cxc_btn_limpiar", on_click=_limpiar_filtros)
    dff = inv._aplicar_busqueda(df, buscar, ["numero_factura", "numero_ov", "cliente", "documento"])
    dff = {"Por cobrar": dff[dff["estado"] == "Abierto"], "Vencidas": dff[dff["estado_visual"] == "🔴 Vencida"],
           "Por vencer": dff[dff["estado_visual"] == "🟠 Por vencer"], "Cobradas": dff[dff["estado"] == "Cerrado"]}.get(estado, dff).copy()
    dff.loc[dff["estado"] == "Cerrado", "dias_para_cobro"] = float("nan")
    if dff.empty:
        st.info("No hay facturas de venta que coincidan." if not df.empty else "Aún no hay facturas de venta.")
        return
    st.dataframe(inv._fechas(dff[list(COLUMNAS_EXPORT)], ["fecha_emision", "fecha_vencimiento_pago", "fecha_cobro"]),
                 use_container_width=True, hide_index=True, column_config={
                     "estado_visual": "Estado", "numero_factura": "N° factura", "numero_ov": "OV", "cliente": "Cliente", "documento": "RUC / DNI",
                     "fecha_emision": st.column_config.DateColumn("Emisión", format="DD/MM/YYYY"),
                     "fecha_vencimiento_pago": st.column_config.DateColumn("Vence cobro", format="DD/MM/YYYY"),
                     "fecha_cobro": st.column_config.DateColumn("Cobrada", format="DD/MM/YYYY"),
                     "dias_para_cobro": st.column_config.NumberColumn("Días", format="%d", help="Negativo = ya venció."),
                     "monto_subtotal": st.column_config.NumberColumn("Subtotal", format="%.2f"), "monto_igv": st.column_config.NumberColumn("IGV", format="%.2f"),
                     "monto_total": st.column_config.NumberColumn("Total", format="%.2f"), "moneda": "Mon."})
    exp.boton_descarga_csv(exp.preparar_export(dff, COLUMNAS_EXPORT, fechas={"fecha_emision": "%d/%m/%Y", "fecha_vencimiento_pago": "%d/%m/%Y", "fecha_cobro": "%d/%m/%Y"},
                                               estados=["estado_visual"], enteros=["dias_para_cobro"], dos_decimales=["monto_subtotal", "monto_igv", "monto_total"]),
                           "cuentas_por_cobrar", "cxc_csv")
    st.divider()
    opciones = {int(r.id): f"{r.numero_factura} · {r.cliente} · {r.estado_visual}" for r in dff.itertuples()}
    if st.session_state.get("cxc_sel") not in opciones:
        st.session_state.pop("cxc_sel", None)
    fid = st.selectbox("📂 Ver detalle de la factura", list(opciones), format_func=opciones.get, key="cxc_sel")
    _detalle(usuario, escribe, int(fid))


def _detalle(usuario: dict, escribe: bool, fid: int) -> None:
    f = ventas_db.obtener_factura_venta(fid)
    if f is None:
        st.warning("La factura ya no existe.")
        return
    sim = SIMBOLO[f["moneda"]]
    st.subheader(f"{f['numero_factura']} · {f['cliente']}")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Estado", "✅ Cobrada" if f["estado"] == "Cerrado" else "🟡 Por cobrar")
    c2.metric("Total", f"{sim} {f['monto_total']:,.2f}", help=f"Subtotal {sim} {f['monto_subtotal']:,.2f} + IGV {sim} {f['monto_igv']:,.2f}")
    c3.metric("Orden de Venta", f["numero_ov"])
    c4.metric("Vence el cobro", _fmt_fecha(f["fecha_vencimiento_pago"]))
    c5.metric("Cobrada el", _fmt_fecha(f["fecha_cobro"]) if f["fecha_cobro"] else "—")
    total, facturado, por_facturar = ventas_db.facturado_de_ov(int(f["ov_id"]))
    st.caption(f"Facturado de {f['numero_ov']}: {sim} {facturado:,.2f} de {sim} {total:,.2f} (pendiente {sim} {por_facturar:,.2f}).")
    ch = expediente_ov.checklist(int(f["ov_id"]))
    st.caption("📁 Expediente de la OV: " + " · ".join(f"{'✅' if c['cumple'] else '❌'} {c['etiqueta']}" for c in ch))
    if not escribe:
        return
    st.markdown("##### 🛠️ Acciones")
    with st.expander("📎 Cargar factura firmada (al expediente de la OV)", expanded=not ch[2]["cumple"]):
        n = st.session_state.setdefault(f"cxc_up_{fid}", 0)
        archivos = st.file_uploader("Factura firmada (PDF o foto)", accept_multiple_files=True, key=f"cxc_files_{fid}_{n}")
        firmada = st.checkbox("Está firmada / con conformidad del cliente", value=True, key=f"cxc_firmada_{fid}_{n}")
        if st.button("📤 Guardar en el expediente", type="primary", disabled=not archivos, key=f"cxc_save_{fid}_{n}"):
            res = None
            for a in archivos or []:
                ok, r = _ejecutar(expediente_ov.adjuntar_documento, usuario, int(f["ov_id"]), "FACTURA", a.name, a.getvalue(),
                                  descripcion=f"Factura {f['numero_factura']}", firmado=firmada, modulo="cxc")
                res = r["cierre"] if ok else res
            if res:
                _flash(mensaje_cierre(res))
                st.session_state[f"cxc_up_{fid}"] = n + 1
                st.rerun()
    if f["estado"] == "Abierto":
        with st.expander("✏️ Editar vencimiento y observaciones", expanded=False):
            with st.form(f"cxc_form_edit_{fid}"):
                vence = st.date_input("Vence el cobro", value=_a_fecha(f["fecha_vencimiento_pago"]), format="DD/MM/YYYY", key=f"cxc_ed_ven_{fid}")
                obs = st.text_input("Observaciones", value=f["observaciones"] or "", key=f"cxc_ed_obs_{fid}")
                guardar = st.form_submit_button("💾 Guardar", type="primary")
            if guardar and _intentar(ventas_db.actualizar_factura_venta, usuario, factura_id=fid, fecha_vencimiento_pago=vence, observaciones=obs):
                _flash("Factura actualizada.")
                st.rerun()
        with st.expander("💰 Registrar cobro", expanded=False):
            cobro = st.date_input("Fecha de cobro", value=date.today(), format="DD/MM/YYYY", key=f"cxc_cobro_{fid}")
            if st.button("✅ Marcar como cobrada", key=f"cxc_cobrar_{fid}") and _intentar(
                    ventas_db.cambiar_estado_factura_venta, usuario, factura_id=fid, nuevo_estado="Cerrado", fecha_cobro=cobro):
                _flash(f"Factura {f['numero_factura']} cobrada.")
                st.rerun()
        with st.expander("🗑️ Eliminar (corregir un registro equivocado)", expanded=False):
            conf = st.checkbox("Confirmo que deseo eliminar esta factura", key=f"cxc_del_c_{fid}")
            if st.button("🗑️ Eliminar factura", disabled=not conf, key=f"cxc_del_b_{fid}") and _intentar(ventas_db.eliminar_factura_venta, usuario, fid):
                _flash(f"Factura {f['numero_factura']} eliminada (su comprobante automático se retiró a la papelera).")
                st.session_state.pop("cxc_sel", None)
                st.rerun()
    elif st.button("🔓 Reabrir (volver a por cobrar)", key=f"cxc_reabrir_{fid}") and _intentar(
            ventas_db.cambiar_estado_factura_venta, usuario, factura_id=fid, nuevo_estado="Abierto"):
        _flash(f"Factura {f['numero_factura']} reabierta.")
        st.rerun()
