"""
modulos/modulo_clientes.py - FASE 2: Clientes (interfaz)

  * Registro de clientes con validación OFFLINE estricta del documento:
        DNI = 8 dígitos · RUC = 11 dígitos iniciando en 10 o 20 (con dígito de control).
  * Autocompletado MODULAR: el botón "Consultar datos" usa el proveedor configurado en
    identidad.py. Sin proveedor (estado actual) el registro es manual, sin perder funcionalidad.
  * Edición con el MISMO flujo: se escribe el DNI/RUC, se valida y se cargan los datos para editarlos.
  * Listado con filtros y exportación a CSV (Excel en español) y Excel (.xlsx).

Permisos: Admin crea y edita; Lector solo consulta. La escritura se valida contra la BD
en clientes_db.py (y la interfaz verifica el rol de la sesión en st.session_state).
"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from modulos import clientes_db, identidad
from modulos import exportacion as exp
from modulos import inventario as inv

COLUMNAS_EXPORT = {
    "documento": "Documento", "razon_social": "Razón social / Nombre", "direccion": "Dirección",
    "telefono": "Teléfono", "email": "Correo", "estado": "Estado", "fuente_datos": "Fuente de datos",
    "salidas": "Salidas registradas",
}


@st.cache_resource(show_spinner=False)
def _preparar() -> bool:
    inv._asegurar_tabla_movimientos()   # kardex + migración de columnas (llama a clientes_db.asegurar_esquema)
    clientes_db.asegurar_esquema()
    return True


def _flash(mensaje: str) -> None:
    st.session_state["_cli_flash"] = mensaje


def _mostrar_flash() -> None:
    msg = st.session_state.pop("_cli_flash", None)
    if msg:
        st.success(f"✅ {msg}")


def _limpiar_filtros() -> None:
    st.session_state["cli_q"] = ""
    st.session_state["cli_estado"] = "Activos"


# ===========================================================================
# REGISTRO
# ===========================================================================
def _nuevo_cliente(usuario: dict) -> None:
    n = st.session_state.setdefault("cli_n", 0)    # Cambiar 'n' reinicia el formulario
    with st.expander("➕ Registrar cliente", expanded=False):
        numero = st.text_input("N° de DNI (8 dígitos) o RUC (11 dígitos) *", key=f"cli_num_{n}",
                               placeholder="Ej. 20100070970", max_chars=20)
        doc, mensaje = identidad.evaluar_documento(numero)
        if numero.strip():
            (st.success if doc else st.error)(f"{'✅' if doc else '⛔'} {mensaje}")

        if st.button("🔎 Consultar datos", disabled=doc is None, key=f"cli_consultar_{n}",
                     help="Autocompleta razón social y dirección si hay un servicio de consulta configurado."):
            res = identidad.consultar_documento(numero)
            st.session_state[f"cli_consulta_{n}"] = (res.documento.numero, res.datos, res.mensaje)

        consulta = st.session_state.get(f"cli_consulta_{n}")
        datos = None
        if consulta and doc and consulta[0] == doc.numero:
            datos = consulta[1]
            st.info(f"ℹ️ {consulta[2]}")
        elif not identidad.autocompletado_disponible() and doc:
            st.caption("Modo de registro manual: no hay un servicio de consulta de RUC/DNI configurado.")

        k = f"{n}_{doc.numero if doc else 'x'}"     # al cambiar el documento se refrescan los valores sugeridos
        with st.form(f"cli_form_nuevo_{k}"):
            c1, c2 = st.columns([2, 3])
            razon = c1.text_input("Razón social / Nombres y apellidos *", value=datos.nombre if datos else "",
                                  disabled=doc is None, key=f"cli_razon_{k}")
            direccion = c2.text_input("Dirección" + (" *" if doc and doc.tipo == "RUC" else ""),
                                      value=datos.direccion if datos else "", disabled=doc is None,
                                      key=f"cli_dir_{k}", help="Obligatoria para clientes con RUC.")
            c3, c4 = st.columns(2)
            telefono = c3.text_input("Teléfono", disabled=doc is None, key=f"cli_tel_{k}")
            email = c4.text_input("Correo electrónico", disabled=doc is None, key=f"cli_mail_{k}")
            guardar = st.form_submit_button("💾 Registrar cliente", type="primary", disabled=doc is None)

    if guardar and doc and inv._intentar(
            clientes_db.crear_cliente, usuario, numero_documento=doc.numero, razon_social=razon,
            direccion=direccion, telefono=telefono, email=email, fuente="API" if datos else "MANUAL"):
        _flash(f"Cliente {doc.etiqueta} registrado.")
        st.session_state["cli_n"] = n + 1
        st.session_state.pop(f"cli_consulta_{n}", None)
        st.rerun()


# ===========================================================================
# LISTADO
# ===========================================================================
def _listado(usuario: dict):
    df = clientes_db.listar_clientes()
    with st.container(border=True):
        c1, c2, c3 = st.columns([3, 1.5, 1.2], vertical_alignment="bottom")
        buscar = c1.text_input("🔎 Buscar por documento, nombre o dirección", key="cli_q")
        estado = c2.selectbox("Estado", ["Activos", "Inactivos", "Todos"], key="cli_estado")
        c3.button("🧹 Limpiar filtros", key="cli_btn_limpiar", on_click=_limpiar_filtros)

    dff = inv._aplicar_busqueda(df, buscar, ["documento", "razon_social", "direccion"])
    if estado == "Activos":
        dff = dff[dff["activo"] == 1]
    elif estado == "Inactivos":
        dff = dff[dff["activo"] == 0]
    dff = dff.assign(estado=dff["activo"].map({1: "Activo", 0: "Inactivo"}))

    if dff.empty:
        st.info("No hay clientes que coincidan con los filtros." if not df.empty
                else "Aún no hay clientes registrados.")
    else:
        vista = dff.assign(estado=dff["activo"].map({1: "✅ Activo", 0: "⛔ Inactivo"}))
        st.dataframe(vista[list(COLUMNAS_EXPORT)], use_container_width=True, hide_index=True, column_config={
            "documento": "Documento", "razon_social": "Razón social / Nombre", "direccion": "Dirección",
            "telefono": "Teléfono", "email": "Correo", "estado": "Estado", "fuente_datos": "Fuente",
            "salidas": st.column_config.NumberColumn("Salidas", format="%d",
                                                     help="Movimientos de salida asociados a este cliente.")})
    st.caption(f"Mostrando {len(dff)} de {len(df)} cliente(s). El archivo exporta lo filtrado.")
    if not dff.empty:
        export = exp.preparar_export(dff, COLUMNAS_EXPORT)
        exp.boton_descarga_csv(export, "clientes", "cli_csv")
        exp.boton_descarga_xlsx(
            {"Clientes": export}, "clientes", "cli_xlsx",
            parametros={"Reporte": "Listado de clientes", "Generado por": usuario.get("nombre_completo", ""),
                        "Fecha de generación": datetime.now().strftime("%d/%m/%Y %H:%M"), "Filas": len(dff)})
    return df


# ===========================================================================
# EDICIÓN (mismo flujo que el registro: documento -> validación -> formulario)
# ===========================================================================
def _editar_cliente(usuario: dict) -> None:
    m = st.session_state.setdefault("cli_m", 0)    # Cambiar 'm' reinicia el formulario
    with st.expander("✏️ Editar cliente", expanded=False):
        numero = st.text_input("N° de DNI (8 dígitos) o RUC (11 dígitos) del cliente *", key=f"cli_ed_num_{m}",
                               placeholder="Ej. 20100070970", max_chars=20)
        doc, mensaje = identidad.evaluar_documento(numero)
        cliente = None
        if numero.strip():
            if doc is None:
                st.error(f"⛔ {mensaje}")
            else:
                cliente = clientes_db.obtener_cliente_por_documento(doc.numero)
                if cliente is None:
                    st.warning(f"⚠️ No existe un cliente con {doc.etiqueta}. Use '➕ Registrar cliente' para crearlo.")
                else:
                    estado = "activo" if cliente["activo"] else "inactivo"
                    st.success(f"✅ {doc.descripcion} válido · Cliente encontrado ({estado}): {cliente['razon_social']}")

        k = f"{m}_{cliente['id'] if cliente is not None else 'x'}"   # al cambiar de cliente se recargan los datos
        sin_cliente = cliente is None
        with st.form(f"cli_form_edit_{k}"):
            c1, c2 = st.columns([2, 3])
            razon = c1.text_input("Razón social / Nombres y apellidos *",
                                  value=cliente["razon_social"] if cliente is not None else "",
                                  disabled=sin_cliente, key=f"cli_ed_razon_{k}")
            direccion = c2.text_input("Dirección" + (" *" if cliente is not None and cliente["tipo_documento"] == "RUC" else ""),
                                      value=(cliente["direccion"] or "") if cliente is not None else "",
                                      disabled=sin_cliente, key=f"cli_ed_dir_{k}", help="Obligatoria para clientes con RUC.")
            c3, c4 = st.columns(2)
            telefono = c3.text_input("Teléfono", value=(cliente["telefono"] or "") if cliente is not None else "",
                                     disabled=sin_cliente, key=f"cli_ed_tel_{k}")
            email = c4.text_input("Correo electrónico", value=(cliente["email"] or "") if cliente is not None else "",
                                  disabled=sin_cliente, key=f"cli_ed_mail_{k}")
            activo = st.checkbox("Cliente activo", value=bool(cliente["activo"]) if cliente is not None else True,
                                 disabled=sin_cliente, key=f"cli_ed_act_{k}",
                                 help="Un cliente inactivo no aparece al registrar salidas ni despachos.")
            guardar = st.form_submit_button("💾 Actualizar cliente", type="primary", disabled=sin_cliente)
        st.caption("Los cambios no alteran los movimientos ya registrados: el kardex conserva los datos del cliente tal "
                   "como estaban al momento de cada salida. Los clientes no se eliminan; se desactivan.")

    if guardar and cliente is not None and inv._intentar(
            clientes_db.actualizar_cliente, usuario, cliente_id=int(cliente["id"]), razon_social=razon,
            direccion=direccion, telefono=telefono, email=email, activo=activo):
        _flash(f"Cliente {doc.etiqueta} actualizado.")
        st.session_state["cli_m"] = m + 1
        st.rerun()


# ===========================================================================
# PUNTO DE ENTRADA (invocado por app.py)
# ===========================================================================
def render(usuario: dict) -> None:
    """Admin: registra y edita. Lector: solo consulta."""
    _preparar()
    es_admin = usuario.get("rol") == "Admin" and st.session_state.get("usuario", {}).get("rol") == "Admin"

    st.title("👥 Clientes")
    if not es_admin:
        st.caption("👁️ Modo solo consulta.")
    _mostrar_flash()
    if es_admin:
        _nuevo_cliente(usuario)
        _editar_cliente(usuario)
    _listado(usuario)
