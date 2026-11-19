"""
modulos/compras_ui.py - Componentes de interfaz compartidos (FASE 3)

Usados por Órdenes de Compra, Facturación, Despachos y Carga masiva:
  * mensajes diferidos (flash) tras st.rerun(), ejecución de escrituras con errores amigables,
  * formato de dinero y fechas,
  * panel documental (carpeta, archivos adjuntos y rutas externas) de una OC o factura,
  * es_admin_sesion(): rol Admin exigido en el parámetro Y en st.session_state.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

import pandas as pd
import streamlit as st

from modulos import documentos as docs

# Si es False, el Lector ve la lista de documentos pero no puede descargarlos.
LECTOR_PUEDE_DESCARGAR = True

MONEDAS = {"PEN": "S/ (PEN)", "USD": "US$ (USD)"}
SIMBOLO = {"PEN": "S/", "USD": "US$"}


def es_admin_sesion(usuario: dict) -> bool:
    """Edición exclusiva de Admin: se exige el rol en el parámetro y en st.session_state."""
    return usuario.get("rol") == "Admin" and st.session_state.get("usuario", {}).get("rol") == "Admin"


def flash(mensaje: str, nivel: str = "success") -> None:
    """Mensaje que se muestra tras st.rerun() (con los datos ya refrescados)."""
    st.session_state["_oc_flash"] = (nivel, mensaje)


def mostrar_flash() -> None:
    f = st.session_state.pop("_oc_flash", None)
    if f:
        nivel, mensaje = f
        (st.success if nivel == "success" else st.warning)(f"{'✅' if nivel == 'success' else '⚠️'} {mensaje}")


def ejecutar(funcion, *args, **kwargs) -> tuple[bool, Any]:
    """Ejecuta una operación de escritura mostrando los errores de forma amigable."""
    try:
        return True, funcion(*args, **kwargs)
    except PermissionError as e:
        st.error(f"⛔ {e}")
    except ValueError as e:
        st.error(f"⚠️ {e}")
    except OSError as e:
        st.error(f"⚠️ Error del sistema de archivos: {e}")
    except Exception as e:   # sqlite3.OperationalError / IntegrityError u otros
        if e.__class__.__module__ == "sqlite3":
            st.error("🔒 La base de datos está ocupada o rechazó la operación. Intente de nuevo en unos segundos.")
        else:
            raise
    return False, None


def intentar(funcion, *args, **kwargs) -> bool:
    return ejecutar(funcion, *args, **kwargs)[0]


def dinero(monto: float, moneda: str) -> str:
    return f"{SIMBOLO.get(moneda, moneda)} {monto:,.2f}"


def a_fecha(valor: Any) -> Optional[date]:
    return date.fromisoformat(valor) if isinstance(valor, str) and valor else None


def fmt_fecha(valor: Any) -> str:
    f = a_fecha(valor)
    return f.strftime("%d/%m/%Y") if f else "—"


def panel_documentos(usuario: dict, es_admin: bool, tipo: str, entidad_id: int) -> None:
    pref = f"doc_{tipo}_{entidad_id}"
    carpeta = docs.carpeta_entidad(tipo, entidad_id)
    existe = carpeta.is_dir()

    if es_admin:
        st.markdown(f"**📁 Carpeta:** `{carpeta}` {'✅' if existe else '⚠️ aún no existe'}")
    else:
        st.markdown("**📁 Documentos adjuntos**")
    if es_admin:
        b1, b2, b3, _ = st.columns([1.5, 1.5, 1.6, 2])
        if b1.button("📁 Crear / reparar carpeta", key=f"{pref}_mk"):
            if intentar(docs.asegurar_carpeta_entidad, usuario, tipo, entidad_id):
                flash("Carpeta lista y registrada.")
                st.rerun()
        if b2.button("🔄 Sincronizar carpeta", key=f"{pref}_sync",
                     help="Registra los archivos que copió manualmente a la carpeta."):
            ok, n = ejecutar(docs.escanear_carpeta, usuario, tipo, entidad_id)
            if ok:
                flash(f"Sincronización completa: {n} archivo(s) nuevo(s) registrado(s)."
                       if n else "Sincronización completa: no hay archivos nuevos.")
                st.rerun()
        if b3.button("🗂️ Abrir en el explorador", key=f"{pref}_open",
                     help="Abre la carpeta en el equipo donde se ejecuta el sistema."):
            if intentar(docs.abrir_en_explorador, usuario, carpeta):
                st.toast("Carpeta abierta en este equipo.", icon="📂")

    df = docs.listar_documentos(tipo, entidad_id)
    if df.empty:
        st.info("Aún no hay documentos adjuntos." +
                (" Suba un archivo, vincule una ruta o sincronice la carpeta." if es_admin else ""))
    else:
        vista = pd.DataFrame({
            "Documento": df["nombre"],
            "Origen": df["tipo_vinculo"].map({"ARCHIVO": "📄 En carpeta", "RUTA_EXTERNA": "🔗 Ruta externa"}),
            "Descripción": df["descripcion"].fillna(""),
            "Tamaño": df["tamano_bytes"].map(docs.formatear_tamano),
            "Estado": df["disponible"].map({True: "✅ Disponible", False: "⚠️ No encontrado"}),
            "Registrado": pd.to_datetime(df["subido_en"]),
            "Por": df["subido_por"],
        })
        st.dataframe(vista, use_container_width=True, hide_index=True, column_config={
            "Registrado": st.column_config.DatetimeColumn("Registrado", format="DD/MM/YYYY HH:mm")})

        opciones = {int(r.id): r.nombre for r in df.itertuples()}
        if st.session_state.get(f"{pref}_sel") not in opciones:
            st.session_state.pop(f"{pref}_sel", None)
        c1, c2 = st.columns([3, 1.6], vertical_alignment="bottom")
        doc_id = c1.selectbox("Documento seleccionado", list(opciones), format_func=opciones.get, key=f"{pref}_sel")
        fila = df[df["id"] == doc_id].iloc[0]
        if fila["tipo_vinculo"] == "RUTA_EXTERNA":
            st.caption(f"Ruta vinculada: `{fila['ruta']}`")

        if es_admin or LECTOR_PUEDE_DESCARGAR:
            try:
                datos, nombre, mime = docs.leer_documento(int(doc_id))
                c2.download_button("⬇️ Descargar", datos, file_name=nombre, mime=mime, key=f"{pref}_dl")
            except ValueError as e:
                c2.caption(f"⚠️ {e}")

        if es_admin:
            d1, d2 = st.columns([3, 1.6], vertical_alignment="bottom")
            borrar_fisico = d1.checkbox("Eliminar también el archivo del disco (solo los guardados en la carpeta del sistema)",
                                        key=f"{pref}_delfis")
            if d2.button("🗑️ Quitar documento", key=f"{pref}_del"):
                if intentar(docs.eliminar_documento, usuario, int(doc_id), borrar_fisico):
                    flash("Documento quitado del sistema." + (" Archivo eliminado del disco." if borrar_fisico else ""))
                    st.rerun()

    if not es_admin:
        return

    n = st.session_state.setdefault(f"{pref}_n", 0)    # Cambiar 'n' reinicia los formularios
    st.markdown("**➕ Adjuntar documentos**")
    with st.container(border=True):   # (no se usa expander: Streamlit no permite expanders anidados)
        t_subir, t_vincular = st.tabs(["📤 Subir archivos", "🔗 Vincular ruta existente"])
        with t_subir:
            with st.form(f"{pref}_form_up_{n}"):
                archivos = st.file_uploader(
                    f"Archivos (máx. {docs.MAX_MB} MB c/u)", accept_multiple_files=True,
                    type=[e.lstrip(".") for e in docs.EXTENSIONES_PERMITIDAS], key=f"{pref}_files_{n}")
                desc = st.text_input("Descripción (opcional)", key=f"{pref}_desc_{n}",
                                     placeholder="Ej. Factura escaneada, guía de remisión")
                subir = st.form_submit_button("📤 Subir y registrar", type="primary")
            if subir:
                if not archivos:
                    st.error("Seleccione al menos un archivo.")
                else:
                    ok, errores = 0, []
                    for f in archivos:
                        try:
                            docs.guardar_archivo(usuario, tipo, entidad_id, f.name, f.getvalue(), desc)
                            ok += 1
                        except (ValueError, PermissionError, OSError) as e:
                            errores.append(f"{f.name}: {e}")
                    if ok:
                        msg = f"{ok} archivo(s) adjuntado(s)."
                        if errores:
                            msg += " No se pudieron subir: " + " | ".join(errores)
                        flash(msg, "success" if not errores else "warning")
                        st.session_state[f"{pref}_n"] = n + 1
                        st.rerun()
                    else:
                        for e in errores:
                            st.error(f"⚠️ {e}")
        with t_vincular:
            st.caption("Registra la ubicación de un archivo o carpeta que ya existe (disco local o carpeta de red) "
                       "sin copiarlo. Si el archivo se mueve o se borra, aparecerá como 'No encontrado'.")
            with st.form(f"{pref}_form_link_{n}"):
                ruta = st.text_input("Ruta completa", key=f"{pref}_ruta_{n}",
                                     placeholder=r"C:\Compras\2026\F001-000125.pdf   o   \\servidor\compras\2026")
                desc2 = st.text_input("Descripción (opcional)", key=f"{pref}_desc2_{n}")
                vincular = st.form_submit_button("🔗 Vincular ruta", type="primary")
            if vincular and intentar(docs.vincular_ruta_externa, usuario, tipo, entidad_id, ruta, desc2):
                flash("Ruta vinculada correctamente.")
                st.session_state[f"{pref}_n"] = n + 1
                st.rerun()
def mensaje_cierre(texto: str = "") -> None:
    """Muestra un mensaje de cierre o aviso en la interfaz."""
    if texto:
        st.info(texto)
def _roles_usuario(usuario: dict) -> list[str]:
    from modulos import permisos
    if isinstance(usuario, dict):
        return permisos.roles_de_usuario_dict(usuario)
    return list(usuario or [])


def puede_escribir(usuario: dict, modulo: str) -> bool:
    """Verifica si el usuario tiene permiso de escritura en el módulo."""
    from modulos import permisos
    return permisos.puede_escribir(_roles_usuario(usuario), modulo)


def puede_ver(usuario: dict, modulo: str) -> bool:
    """Verifica si el usuario tiene permiso de lectura o escritura en el módulo."""
    from modulos import permisos
    return permisos.puede_ver(_roles_usuario(usuario), modulo)