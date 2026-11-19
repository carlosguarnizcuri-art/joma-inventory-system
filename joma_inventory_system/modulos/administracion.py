"""
modulos/administracion.py - MÓDULO 4 (Fase 1): Administración, Auditoría y Respaldo

Páginas:
  * render(usuario)            -> "⚙️ Administración" (SOLO Admin)
        👥 Usuarios · 🔐 Contraseñas · 📋 Auditoría · 💾 Sistema y respaldo
  * render_mi_cuenta(usuario)  -> "🔑 Mi cuenta" (Admin y Lector): cambio de contraseña propio.

Seguridad:
  * La interfaz solo se muestra a Admin y, además, cada función de admin_db.py vuelve a
    validar el rol contra la BD.
  * Las contraseñas se guardan con bcrypt; nunca se registran en la auditoría.
  * Una contraseña generada se muestra UNA sola vez (en memoria de la sesión) y desaparece
    en la siguiente interacción.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

import database as db
from modulos import admin_db, mantenimiento_db, permisos
from modulos.compras_ui import es_admin_sesion
from modulos import exportacion as exp
from modulos import inventario as inv   # reutiliza _intentar (manejo amigable de errores)

ROLES = list(permisos.ROLES)
ICONO_ROL = {"Admin": "🛡️ Admin", "Comercial": "💼 Comercial", "Compras": "🛒 Compras", "Almacén": "📦 Almacén", "Lector": "👁️ Lector"}


@st.cache_resource(show_spinner=False)
def _preparar() -> bool:
    admin_db.asegurar_tablas()
    return True


# ---------------------------------------------------------------------------
# Utilidades de interfaz
# ---------------------------------------------------------------------------
def _flash(mensaje: str) -> None:
    st.session_state["_adm_flash"] = mensaje


def _mostrar_mensajes() -> None:
    msg = st.session_state.pop("_adm_flash", None)
    if msg:
        st.success(f"✅ {msg}")
    unico = st.session_state.pop("_adm_pw_unico", None)
    if unico:
        usuario, password = unico
        st.warning(f"🔑 Contraseña temporal para **{usuario}**. Cópiela y entréguela ahora: "
                   "**no se volverá a mostrar**. Se recomienda que el usuario la cambie en 'Mi cuenta'.")
        st.code(password, language=None)


# ---------------------------------------------------------------------------
# PESTAÑA: USUARIOS
# ---------------------------------------------------------------------------
def _etiqueta_rol(r: str) -> str:
    return f"{ICONO_ROL[r]} — {permisos.ROLES[r]}"


def _tab_usuarios(admin: dict) -> None:
    df = admin_db.listar_usuarios()
    vista = df.assign(activo=df["activo"].map({1: "✅ Activo", 0: "⛔ Inactivo"}))
    st.dataframe(vista[["username", "nombre_completo", "roles", "activo", "creado_en", "ultimo_acceso", "movimientos"]],
                 use_container_width=True, hide_index=True, column_config={
                     "username": "Usuario", "nombre_completo": "Nombre", "roles": "Roles asignados", "activo": "Estado",
                     "creado_en": "Creado", "ultimo_acceso": "Último acceso",
                     "movimientos": st.column_config.NumberColumn("Movimientos en kardex", format="%d")})
    st.caption("Un usuario puede tener VARIOS roles: sus permisos son la suma de todos. Los usuarios no se eliminan (sus "
               "movimientos quedan en el kardex): se desactivan. Los cambios de roles o de estado se aplican de inmediato, "
               "incluso si el usuario tiene la sesión abierta.")
    with st.expander("🔐 Matriz de permisos por rol", expanded=False):
        st.dataframe(permisos.matriz_permisos(), use_container_width=True, hide_index=True)

    # ----- Roles, nombre y estado de un usuario
    st.markdown("#### 🎭 Asignar o retirar roles")
    opt = {int(r.id): f"{r.username} · {r.nombre_completo} ({r.roles}{'' if r.activo else ', inactivo'})" for r in df.itertuples()}
    uid = st.selectbox("Usuario", list(opt), format_func=opt.get, key="adm_edit_sel")
    u = df[df["id"] == uid].iloc[0]
    es_yo = uid == admin["id"]
    with st.form(f"adm_form_edit_{uid}"):
        roles = st.multiselect("Roles del usuario (puede elegir varios)", ROLES, default=list(u["lista_roles"]),
                               format_func=_etiqueta_rol, key=f"adm_ed_roles_{uid}")
        c1, c2 = st.columns([3, 1])
        nombre = c1.text_input("Nombre completo", value=u["nombre_completo"], key=f"adm_ed_nom_{uid}")
        activo = c2.checkbox("Cuenta activa", value=bool(u["activo"]), disabled=es_yo, key=f"adm_ed_act_{uid}")
        if es_yo:
            st.caption("Su propia cuenta no se puede desactivar ni quitarle el rol Admin (evita bloquear el sistema).")
        guardar = st.form_submit_button("💾 Guardar cambios", type="primary")
    if guardar:
        if inv._intentar(admin_db.asignar_roles, admin, user_id=int(uid), roles=roles) and \
                inv._intentar(admin_db.actualizar_usuario, admin, user_id=int(uid), nombre_completo=nombre, activo=activo):
            _flash(f"Usuario '{u['username']}' actualizado: {permisos.texto_roles(roles)}.")
            st.rerun()

    # ----- Crear usuario
    with st.expander("➕ Crear usuario", expanded=False):
        n = st.session_state.setdefault("adm_new_n", 0)
        with st.form(f"adm_form_nuevo_{n}"):
            c1, c2 = st.columns(2)
            username = c1.text_input("Usuario *", placeholder="mperez", key=f"adm_new_user_{n}",
                                     help="3-30 caracteres: minúsculas, números, punto, guion o guion bajo.")
            nombre = c2.text_input("Nombre completo *", key=f"adm_new_nom_{n}")
            roles = st.multiselect("Roles *", ROLES, default=["Lector"], format_func=_etiqueta_rol, key=f"adm_new_roles_{n}")
            generar = st.checkbox("Generar una contraseña temporal segura", value=True, key=f"adm_new_gen_{n}")
            p1 = st.text_input("Contraseña (si no la genera)", type="password", key=f"adm_new_p1_{n}")
            p2 = st.text_input("Confirmar contraseña", type="password", key=f"adm_new_p2_{n}")
            st.caption("Política: mínimo 8 caracteres, con letras y números.")
            crear = st.form_submit_button("💾 Crear usuario", type="primary")
        if crear:
            if not generar and p1 != p2:
                st.error("⚠️ La contraseña y su confirmación no coinciden.")
            else:
                password = admin_db.generar_password_temporal() if generar else p1
                if inv._intentar(admin_db.crear_usuario, admin, username=username, nombre_completo=nombre, roles=roles,
                                 password=password):
                    _flash(f"Usuario '{username.strip().lower()}' creado con roles: {permisos.texto_roles(roles)}.")
                    if generar:
                        st.session_state["_adm_pw_unico"] = (username.strip().lower(), password)
                    st.session_state["adm_new_n"] = n + 1
                    st.rerun()


# ---------------------------------------------------------------------------
# PESTAÑA: CONTRASEÑAS
# ---------------------------------------------------------------------------
def _tab_passwords(admin: dict) -> None:
    st.markdown("Restablezca la contraseña de otro usuario. Se guarda cifrada con **bcrypt** y la acción queda "
                "registrada en la auditoría (sin guardar la contraseña).")
    df = admin_db.listar_usuarios()
    df = df[df["id"] != admin["id"]]
    if df.empty:
        st.info("No hay otros usuarios. Para cambiar su propia contraseña use 'Mi cuenta'.")
        return
    opt = {int(r.id): f"{r.username} · {r.nombre_completo}" for r in df.itertuples()}
    uid = st.selectbox("Usuario", list(opt), format_func=opt.get, key="adm_pw_sel")
    modo = st.radio("Método", ["Generar contraseña temporal segura", "Definir manualmente"],
                    horizontal=True, key="adm_pw_modo")
    manual = modo.startswith("Definir")
    n = st.session_state.setdefault("adm_pw_n", 0)
    with st.form(f"adm_form_pw_{uid}_{n}"):
        p1 = p2 = ""
        if manual:
            p1 = st.text_input("Nueva contraseña", type="password", key=f"adm_pw1_{uid}_{n}")
            p2 = st.text_input("Confirmar contraseña", type="password", key=f"adm_pw2_{uid}_{n}")
            st.caption("Política: mínimo 8 caracteres, con letras y números.")
        enviar = st.form_submit_button("🔐 Restablecer contraseña", type="primary")
    if enviar:
        if manual and p1 != p2:
            st.error("⚠️ La contraseña y su confirmación no coinciden.")
            return
        nueva = p1 if manual else admin_db.generar_password_temporal()
        if inv._intentar(admin_db.restablecer_password, admin, user_id=int(uid), nueva_password=nueva):
            _flash(f"Contraseña de '{df[df['id'] == uid].iloc[0]['username']}' restablecida.")
            if not manual:
                st.session_state["_adm_pw_unico"] = (df[df["id"] == uid].iloc[0]["username"], nueva)
            st.session_state["adm_pw_n"] = n + 1
            st.rerun()


# ---------------------------------------------------------------------------
# PESTAÑA: AUDITORÍA
# ---------------------------------------------------------------------------
def _tab_auditoria(admin: dict) -> None:
    df = admin_db.listar_auditoria(limite=1000)
    st.caption("Registro de acciones administrativas (altas, cambios de rol, bloqueos, restablecimientos de "
               "contraseña y respaldos). Es de solo lectura.")
    if df.empty:
        st.info("Aún no hay acciones registradas.")
        return
    with st.container(border=True):
        c1, c2 = st.columns([2, 3])
        acciones = ["Todas"] + sorted(df["accion"].unique())
        accion = c1.selectbox("Acción", acciones, key="aud_accion")
        texto = c2.text_input("🔎 Buscar (usuario, objetivo, detalle)", key="aud_q")
    dff = df if accion == "Todas" else df[df["accion"] == accion]
    dff = inv._aplicar_busqueda(dff, texto, ["admin", "accion", "objetivo", "detalle"])

    st.dataframe(inv._fechas(dff, ["fecha"]), use_container_width=True, hide_index=True, column_config={
        "fecha": st.column_config.DatetimeColumn("Fecha y hora", format="DD/MM/YYYY HH:mm:ss"),
        "admin": "Realizada por", "accion": "Acción", "objetivo": "Usuario afectado", "detalle": "Detalle"})
    st.caption(f"Mostrando {len(dff)} de {len(df)} registro(s) (máximo los últimos 1000).")
    cols = {"fecha": "Fecha y hora", "admin": "Realizada por", "accion": "Acción", "objetivo": "Usuario afectado",
            "detalle": "Detalle"}
    exp.boton_descarga_csv(exp.preparar_export(dff, cols, fechas={"fecha": "%d/%m/%Y %H:%M:%S"}),
                           "auditoria", "aud_csv")
    exp.boton_descarga_xlsx(
        {"Auditoría": exp.preparar_export(dff, cols, fechas={"fecha": "%d/%m/%Y %H:%M:%S"}, como_fecha=True)},
        "auditoria", "aud_xlsx",
        parametros={"Reporte": "Auditoría de acciones administrativas", "Generado por": admin.get("nombre_completo", ""),
                    "Fecha de generación": datetime.now().strftime("%d/%m/%Y %H:%M"), "Filas": len(dff)})


# ---------------------------------------------------------------------------
# PESTAÑA: SISTEMA Y RESPALDO
# ---------------------------------------------------------------------------
def _tab_sistema(admin: dict) -> None:
    est = admin_db.estadisticas_sistema()
    c = est["conteos"]
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Tamaño de la BD", f"{est['tamano_mb']:.2f} MB")
    m2.metric("Productos", f"{c['productos']:,}")
    m3.metric("Lotes", f"{c['lotes_inventario']:,}")
    m4.metric("Movimientos (kardex)", f"{c['movimientos_inventario']:,}")
    m5.metric("Usuarios", f"{c['usuarios']:,}")
    st.caption(f"Archivo: `{est['ruta']}`")

    st.markdown("#### 💾 Respaldo de la base de datos")
    st.markdown("Genera una **copia consistente** de `joma_inventory.db` (segura aunque otros usuarios estén "
                "trabajando). Guárdela en un lugar seguro: **contiene datos del negocio y las contraseñas cifradas**.")
    b1, b2 = st.columns([1, 1])
    if b1.button("🗄️ Generar respaldo", type="primary", key="bk_generar"):
        try:
            st.session_state["adm_backup"] = admin_db.crear_respaldo(admin)
        except (PermissionError, OSError) as e:
            st.error(f"⚠️ No se pudo generar el respaldo: {e}")
    respaldo = st.session_state.get("adm_backup")
    if respaldo:
        datos, nombre = respaldo
        b2.download_button(f"⬇️ Descargar {nombre} ({len(datos) / 1024:.0f} KB)", datos, file_name=nombre,
                           mime="application/octet-stream", key="bk_descargar")
        st.success("Respaldo listo. La generación quedó registrada en la auditoría.")

    st.markdown("#### 🩺 Integridad")
    if st.button("Verificar integridad de la base de datos", key="bk_integridad"):
        ok, mensaje = admin_db.verificar_integridad()
        (st.success if ok else st.error)(("✅ " if ok else "⛔ ") + mensaje)

    st.markdown("#### ♻️ Cómo restaurar")
    st.markdown("1. Cierre la aplicación.  \n2. Renombre el archivo actual `joma_inventory.db` (por si acaso).  \n"
                "3. Copie el respaldo a la carpeta del proyecto con el nombre `joma_inventory.db`.  \n"
                "4. Elimine `joma_inventory.db-wal` y `joma_inventory.db-shm` si existen y abra la aplicación.")


# ---------------------------------------------------------------------------
# PESTAÑA: MODO PRUEBAS (stock y OC en cero)
# ---------------------------------------------------------------------------
NOMBRES_TABLAS = {
    "lotes_inventario": "Lotes de inventario (stock)", "movimientos_inventario": "Movimientos del kardex",
    "ordenes_compra": "Órdenes de compra", "oc_detalle": "Líneas de OC", "facturas": "Facturas",
    "despachos": "Despachos", "despacho_detalle": "Líneas de despacho", "documentos_adjuntos": "Documentos adjuntos (registros)",
    "clientes": "Clientes", "productos": "Productos", "plantillas_compra": "Plantillas de compra",
    "plantilla_compra_detalle": "Líneas de plantillas", "ordenes_venta": "Órdenes de venta", "ov_detalle": "Líneas de OV",
    "ov_documentos": "Documentos de expedientes OV (registros)", "facturas_venta": "Facturas de venta (por cobrar)",
}


def _tab_pruebas(admin: dict) -> None:
    st.markdown("Pone en **cero** el stock y las órdenes de compra (además de facturas, despachos y kardex) para "
                "**iniciar pruebas desde cero**. No toca usuarios ni contraseñas, ni la auditoría, ni los archivos "
                "físicos de `data/documentos`. Antes de borrar se genera un **respaldo automático** en `data/respaldos/` "
                "y la numeración vuelve a empezar (OC-00001, DES-00001).")
    conteo = mantenimiento_db.contar_datos()
    filas = [(NOMBRES_TABLAS.get(t, t), n) for t, n in conteo.items() if t in mantenimiento_db.TABLAS_OPERATIVAS]
    st.dataframe(pd.DataFrame(filas, columns=["Se borrará", "Registros actuales"]), use_container_width=True,
                 hide_index=True)
    n = st.session_state.setdefault("prueba_n", 0)
    c1, c2 = st.columns(2)
    clientes = c1.checkbox(f"También borrar los clientes ({conteo.get('clientes', 0)})", key=f"prueba_cli_{n}")
    productos = c2.checkbox(f"También borrar los productos y plantillas ({conteo.get('productos', 0)} productos)",
                            key=f"prueba_prod_{n}")
    st.error("⚠️ Esta acción no se puede deshacer desde la aplicación (solo restaurando el respaldo).")
    palabra = st.text_input(f"Para confirmar escriba {mantenimiento_db.PALABRA_CONFIRMACION}", key=f"prueba_txt_{n}")
    listo = palabra.strip() == mantenimiento_db.PALABRA_CONFIRMACION
    if st.button("🧪 Reiniciar datos operativos", type="primary", disabled=not listo, key=f"prueba_btn_{n}"):
        try:
            res = mantenimiento_db.reiniciar_datos_operativos(admin, confirmacion=palabra, borrar_clientes=clientes,
                                                              borrar_productos=productos)
        except (ValueError, PermissionError, OSError) as e:
            st.error(f"⚠️ {e}")
            return
        total = sum(res["filas"].values())
        _flash(f"Datos reiniciados ({total:,} registros). Respaldo previo guardado en: {res['respaldo']}")
        st.session_state["prueba_n"] = n + 1
        st.rerun()


# ---------------------------------------------------------------------------
# PUNTOS DE ENTRADA (invocados por app.py)
# ---------------------------------------------------------------------------
def render(usuario: dict) -> None:
    """⚙️ Administración: exclusivo para el rol Admin."""
    _preparar()
    st.title("⚙️ Administración")
    if not es_admin_sesion(usuario):
        st.error("⛔ Esta sección es exclusiva para administradores.")
        return
    _mostrar_mensajes()
    t1, t2, t3, t4, t5 = st.tabs(["👥 Usuarios", "🔐 Contraseñas", "📋 Auditoría", "💾 Sistema y respaldo",
                                  "🧪 Modo pruebas"])
    with t1:
        _tab_usuarios(usuario)
    with t2:
        _tab_passwords(usuario)
    with t3:
        _tab_auditoria(usuario)
    with t4:
        _tab_sistema(usuario)
    with t5:
        _tab_pruebas(usuario)


def render_mi_cuenta(usuario: dict) -> None:
    """🔑 Mi cuenta: disponible para Admin y Lector (cambio de contraseña propio)."""
    _preparar()
    st.title("🔑 Mi cuenta")
    _mostrar_mensajes()
    fila = db.fetch_one("SELECT username, nombre_completo, rol, creado_en, ultimo_acceso FROM usuarios WHERE id = ?",
                        (usuario["id"],))
    if fila is None:
        st.error("No se encontró su cuenta.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Usuario", fila["username"])
    c2.metric("Roles", permisos.texto_roles(db.roles_de_usuario(usuario["id"])))
    c3.metric("Último acceso", (fila["ultimo_acceso"] or "—")[:16])

    st.markdown("#### Cambiar mi contraseña")
    n = st.session_state.setdefault("cuenta_n", 0)
    with st.form(f"cuenta_form_pw_{n}"):
        actual = st.text_input("Contraseña actual", type="password", key=f"cuenta_act_{n}")
        nueva = st.text_input("Nueva contraseña", type="password", key=f"cuenta_new_{n}")
        conf = st.text_input("Confirmar nueva contraseña", type="password", key=f"cuenta_conf_{n}")
        st.caption("Mínimo 8 caracteres, con letras y números. No use su nombre de usuario ni contraseñas comunes.")
        enviar = st.form_submit_button("🔐 Cambiar contraseña", type="primary")
    if enviar and inv._intentar(admin_db.cambiar_mi_password, usuario, actual=actual, nueva=nueva, confirmacion=conf):
        _flash("Su contraseña fue actualizada correctamente.")
        st.session_state["cuenta_n"] = n + 1
        st.rerun()
