"""
modulos/facturacion.py - Facturación dual (FASE 4)

Facturas de proveedores con estado Abierto / Cerrado, vinculación con OC, conciliación del
monto facturado, documentos adjuntos y exportación a CSV / Excel.
Permisos: Admin crea, edita, cierra y elimina; Lector solo consulta (y descarga documentos).
La lógica de negocio vive en compras_db.py y la gestión de archivos en documentos.py.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd
import streamlit as st

from modulos import compras_db as cdb
from modulos import cuentas_cobrar, despachos_db
from modulos import documentos as docs
from modulos import exportacion as exp
from modulos import inventario as inv   # reutiliza helpers validados (búsqueda, fechas)
from modulos.compras_ui import (MONEDAS, SIMBOLO, a_fecha as _a_fecha, dinero as _dinero, ejecutar as _ejecutar,
                                flash as _flash, fmt_fecha as _fmt_fecha, intentar as _intentar,
                                mostrar_flash as _mostrar_flash, panel_documentos as _panel_documentos, puede_escribir, puede_ver)

COLUMNAS_EXPORT_FACTURAS = {
    "estado_visual": "Estado", "numero_factura": "N° factura", "proveedor": "Proveedor", "numero_oc": "OC vinculada",
    "fecha_emision": "Fecha de emisión", "fecha_vencimiento_pago": "Vencimiento de pago",
    "dias_para_pago": "Días para el pago", "monto_total": "Monto", "moneda": "Moneda", "n_docs": "Documentos",
}


@st.cache_resource(show_spinner=False)
def _preparar_modulo() -> bool:
    cdb.asegurar_esquema()
    despachos_db.asegurar_esquema()      # crea también las tablas de ventas (OV y facturas de venta)
    docs.inicializar_estructura()
    return True


def _resumen_facturas() -> None:
    r = cdb.resumen_compras()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Facturas abiertas", int(r["facturas_abiertas"]))
    c2.metric("Facturas vencidas", int(r["facturas_vencidas"]),
              delta="Acción requerida" if r["facturas_vencidas"] else None, delta_color="inverse")
    c3.metric("Por vencer (7 d)", int(r["facturas_por_vencer"]))
    c4.metric("Por pagar (S/)", f"S/ {r['por_pagar_pen']:,.2f}",
              help=f"Facturas abiertas en soles. Además en dólares: US$ {r['por_pagar_usd']:,.2f}.")
    if r["facturas_vencidas"]:
        st.error(f"🔴 {int(r['facturas_vencidas'])} factura(s) abierta(s) con el pago vencido.")
    if r["facturas_por_vencer"]:
        st.warning(f"🟠 {int(r['facturas_por_vencer'])} factura(s) vencen en los próximos "
                   f"{cdb.DIAS_FACTURA_POR_VENCER} días.")


def _limpiar_filtros_fac() -> None:
    st.session_state["fac_q"] = ""
    st.session_state["fac_f_estado"] = "Todas"


def _form_nueva_factura(usuario: dict) -> None:
    df_oc = cdb.listar_ocs()
    opt_oc: dict[int, str] = {0: "— Sin orden de compra —"}
    opt_oc.update({int(r.id): f"{r.numero_oc} · {r.proveedor} · {r.moneda} {r.monto_total:,.2f} [{r.estado}]"
                   for r in df_oc.itertuples()})
    hoy = date.today()
    n = st.session_state.setdefault("fac_new_n", 0)

    with st.expander("➕ Nueva factura", expanded=False):
        oc_id = st.selectbox("Orden de compra vinculada (opcional)", list(opt_oc), format_func=opt_oc.get,
                             key="fac_new_oc", help="Al vincular una OC se toman su proveedor y moneda.")
        oc = df_oc[df_oc["id"] == oc_id].iloc[0] if oc_id else None
        k = f"{n}_{oc_id}"
        with st.form(f"fac_form_nueva_{k}"):
            c1, c2, c3 = st.columns(3)
            numero = c1.text_input("N° de factura *", placeholder="F001-000125", key=f"fac_new_num_{k}")
            proveedor = c2.text_input("Proveedor *", value=oc["proveedor"] if oc is not None else "",
                                      disabled=oc is not None, key=f"fac_new_prov_{k}")
            monedas = list(MONEDAS)
            moneda = c3.selectbox("Moneda", monedas, format_func=MONEDAS.get,
                                  index=monedas.index(oc["moneda"]) if oc is not None else 0,
                                  disabled=oc is not None, key=f"fac_new_mon_{k}")
            c4, c5, c6 = st.columns(3)
            emision = c4.date_input("Fecha de emisión", value=hoy, format="DD/MM/YYYY", key=f"fac_new_emi_{k}")
            vence = c5.date_input("Vencimiento de pago", value=None, format="DD/MM/YYYY", key=f"fac_new_ven_{k}")
            monto = c6.number_input("Monto total *", min_value=0.0, step=10.0, format="%.2f",
                                    value=float(oc["monto_total"]) if oc is not None else 0.0, key=f"fac_new_monto_{k}")
            obs = st.text_input("Observaciones", key=f"fac_new_obs_{k}")
            crear = st.form_submit_button("💾 Registrar factura", type="primary")

    if crear:
        prov_final = oc["proveedor"] if oc is not None else proveedor
        ok, _ = _ejecutar(cdb.crear_factura, usuario, numero_factura=numero, proveedor=prov_final,
                          oc_id=int(oc_id) if oc_id else None, fecha_emision=emision, fecha_vencimiento_pago=vence,
                          monto_total=monto, moneda=moneda, observaciones=obs)
        if ok:
            _flash(f"Factura {numero.strip().upper()} registrada con su carpeta de documentos.")
            st.session_state["fac_new_n"] = n + 1
            nuevo_id = cdb.id_factura(numero, prov_final)
            if nuevo_id:
                st.session_state["fac_sel"] = nuevo_id
            st.rerun()


def _tab_facturas(usuario: dict, es_admin: bool) -> None:
    if es_admin:
        _form_nueva_factura(usuario)

    df = cdb.listar_facturas()
    with st.container(border=True):
        c1, c2, c3 = st.columns([3, 2, 1.3], vertical_alignment="bottom")
        buscar = c1.text_input("🔎 Buscar por N° de factura, proveedor u OC", key="fac_q",
                               placeholder="Ej. F001-000123, Alicorp, OC-2026-002")
        estado = c2.selectbox("Estado", ["Todas", "Abiertas", "Vencidas", "Por vencer", "Cerradas"],
                              key="fac_f_estado")
        c3.button("🧹 Limpiar filtros", key="fac_btn_limpiar", on_click=_limpiar_filtros_fac)

    dff = inv._aplicar_busqueda(df, buscar, ["numero_factura", "proveedor", "numero_oc"])
    if estado == "Abiertas":
        dff = dff[dff["estado"] == "Abierto"]
    elif estado == "Vencidas":
        dff = dff[dff["estado_visual"] == "🔴 Vencida"]
    elif estado == "Por vencer":
        dff = dff[dff["estado_visual"] == "🟠 Por vencer"]
    elif estado == "Cerradas":
        dff = dff[dff["estado"] == "Cerrado"]
    dff = dff.copy()
    dff.loc[dff["estado"] == "Cerrado", "dias_para_pago"] = float("nan")   # no aplica a facturas cerradas

    cols = ["estado_visual", "numero_factura", "proveedor", "numero_oc", "fecha_emision", "fecha_vencimiento_pago",
            "dias_para_pago", "monto_total", "moneda", "n_docs"]
    if dff.empty:
        st.info("Ninguna factura coincide con los filtros.")
    else:
        st.dataframe(
            inv._fechas(dff[cols], ["fecha_emision", "fecha_vencimiento_pago"]), use_container_width=True,
            hide_index=True, column_config={
                "estado_visual": "Estado", "numero_factura": "N° factura", "proveedor": "Proveedor",
                "numero_oc": "OC",
                "fecha_emision": st.column_config.DateColumn("Emisión", format="DD/MM/YYYY"),
                "fecha_vencimiento_pago": st.column_config.DateColumn("Vence pago", format="DD/MM/YYYY"),
                "dias_para_pago": st.column_config.NumberColumn("Días p/ pago", format="%d",
                                                                help="Negativo = ya venció."),
                "monto_total": st.column_config.NumberColumn("Monto", format="%.2f"),
                "moneda": "Mon.", "n_docs": st.column_config.NumberColumn("Docs", format="%d"),
            })
    st.caption(f"Mostrando {len(dff)} de {len(df)} factura(s). El CSV exporta lo filtrado.")
    exp.boton_descarga_csv(
        exp.preparar_export(dff, COLUMNAS_EXPORT_FACTURAS,
                             fechas={"fecha_emision": "%d/%m/%Y", "fecha_vencimiento_pago": "%d/%m/%Y"},
                             estados=["estado_visual"], enteros=["dias_para_pago"], dos_decimales=["monto_total"]),
        "facturas", "fac_csv")

    if dff.empty:
        return
    st.divider()
    opciones = {int(r.id): f"{r.numero_factura} · {r.proveedor} · {r.estado_visual}" for r in dff.itertuples()}
    if st.session_state.get("fac_sel") not in opciones:
        st.session_state.pop("fac_sel", None)
    fid = st.selectbox("📂 Ver detalle de la factura", list(opciones), format_func=opciones.get, key="fac_sel")
    _detalle_factura(usuario, es_admin, int(fid))


def _detalle_factura(usuario: dict, es_admin: bool, factura_id: int) -> None:
    f = cdb.obtener_factura(factura_id)
    if f is None:
        st.warning("La factura ya no existe.")
        return
    hoy = date.today()
    limite = (hoy + timedelta(days=cdb.DIAS_FACTURA_POR_VENCER)).isoformat()
    visual = cdb.estado_factura_visual(f["estado"], f["fecha_vencimiento_pago"], hoy.isoformat(), limite)
    oc = cdb.obtener_oc(f["orden_compra_id"]) if f["orden_compra_id"] else None

    st.subheader(f"{f['numero_factura']} · {f['proveedor']}")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Estado", visual)
    c2.metric("Monto", _dinero(f["monto_total"], f["moneda"]))
    c3.metric("Emisión", _fmt_fecha(f["fecha_emision"]))
    c4.metric("Vence pago", _fmt_fecha(f["fecha_vencimiento_pago"]))
    c5.metric("OC vinculada", oc["numero_oc"] if oc else "—")
    if f["observaciones"]:
        st.caption(f"📝 {f['observaciones']}")

    if oc is not None:
        # Conciliación: total facturado de la OC vs. monto de la OC (puede haber facturas parciales)
        hermanas = cdb.facturas_de_oc(int(oc["id"]))
        facturado = hermanas.loc[hermanas["moneda"] == oc["moneda"], "monto_total"].sum()
        pct = facturado / oc["monto_total"] * 100 if oc["monto_total"] else 0
        texto = (f"Facturado de la OC {oc['numero_oc']}: {_dinero(facturado, oc['moneda'])} de "
                 f"{_dinero(oc['monto_total'], oc['moneda'])} ({pct:.0f} %).")
        (st.success if abs(facturado - oc["monto_total"]) < 0.01 else st.info)(texto)

    with st.expander("📎 Documentos adjuntos y carpeta", expanded=False):
        _panel_documentos(usuario, es_admin, "FACTURA", factura_id)

    if not es_admin:
        return
    abierta = f["estado"] == "Abierto"
    st.markdown("##### 🛠️ Acciones de administración")

    with st.expander("✏️ Editar factura", expanded=False):
        if not abierta:
            st.caption("Reabra la factura para editarla.")
        else:
            with st.form(f"fac_form_edit_{factura_id}"):
                c1, c2 = st.columns(2)
                vence = c1.date_input("Vencimiento de pago", value=_a_fecha(f["fecha_vencimiento_pago"]),
                                      format="DD/MM/YYYY", key=f"fac_ed_ven_{factura_id}")
                monto = c2.number_input("Monto total", min_value=0.0, value=float(f["monto_total"]), step=10.0,
                                        format="%.2f", key=f"fac_ed_monto_{factura_id}")
                obs = st.text_area("Observaciones", value=inv._txt(f["observaciones"]), key=f"fac_ed_obs_{factura_id}")
                guardar = st.form_submit_button("💾 Guardar cambios", type="primary")
            if guardar and _intentar(cdb.actualizar_factura, usuario, factura_id=factura_id,
                                     fecha_vencimiento_pago=vence, monto_total=monto, observaciones=obs):
                _flash("Factura actualizada.")
                st.rerun()

    with st.expander("🔒 Estado de la factura", expanded=False):
        if abierta:
            st.caption("Cierre la factura cuando esté pagada / conciliada.")
            if st.button("🔒 Cerrar factura", key=f"fac_cerrar_{factura_id}"):
                if _intentar(cdb.cambiar_estado_factura, usuario, factura_id=factura_id, nuevo_estado="Cerrado"):
                    _flash(f"Factura {f['numero_factura']} cerrada.")
                    st.rerun()
        elif st.button("🔓 Reabrir factura", key=f"fac_reabrir_{factura_id}"):
            if _intentar(cdb.cambiar_estado_factura, usuario, factura_id=factura_id, nuevo_estado="Abierto"):
                _flash(f"Factura {f['numero_factura']} reabierta.")
                st.rerun()

    with st.expander("🗑️ Eliminar factura", expanded=False):
        st.caption("No se puede eliminar si hay lotes de inventario asociados. Los archivos físicos NO se borran.")
        conf = st.checkbox("Confirmo que deseo eliminar esta factura", key=f"fac_delconf_{factura_id}")
        if st.button("🗑️ Eliminar factura", disabled=not conf, key=f"fac_del_{factura_id}"):
            if _intentar(cdb.eliminar_factura, usuario, factura_id):
                _flash(f"Factura {f['numero_factura']} eliminada.")
                st.session_state.pop("fac_sel", None)
                st.rerun()



def render(usuario: dict) -> None:
    """Un solo módulo, dos pestañas: Cuentas por Pagar (proveedores / OC) y Cuentas por Cobrar (clientes / OV)."""
    _preparar_modulo()
    st.title("🧾 Facturación")
    _mostrar_flash()
    nombres, secciones = [], []
    if puede_ver(usuario, "cxp"):
        nombres.append("💳 Cuentas por Pagar (Proveedores · OC)")
        secciones.append("cxp")
    if puede_ver(usuario, "cxc"):
        nombres.append("💰 Cuentas por Cobrar (Clientes · OV)")
        secciones.append("cxc")
    if not secciones:
        st.error("⛔ Sus roles no incluyen acceso a Facturación.")
        return
    for tab_, seccion in zip(st.tabs(nombres), secciones):
        with tab_:
            if seccion == "cxp":
                if not puede_escribir(usuario, "cxp"):
                    st.caption("👁️ Modo solo consulta: puede revisar facturas de proveedores y descargar sus documentos.")
                _resumen_facturas()
                _tab_facturas(usuario, puede_escribir(usuario, "cxp"))
            else:
                if not puede_escribir(usuario, "cxc"):
                    st.caption("👁️ Modo solo consulta: puede revisar facturas de venta.")
                cuentas_cobrar.tab(usuario)
