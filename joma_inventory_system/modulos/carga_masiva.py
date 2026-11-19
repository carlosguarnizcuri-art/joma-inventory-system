"""
modulos/carga_masiva.py - Panel de carga masiva desde Excel / CSV (FASE 3, interfaz)

Componente REUTILIZABLE: se incrusta en
  * Gestión de Inventario > Entradas   -> panel(usuario, "entradas")
  * Gestión de Inventario > Salidas    -> panel(usuario, "salidas")
  * Órdenes de Compra > Nueva OC       -> panel(usuario, "oc")

Flujo en pantalla:  descargar plantilla -> llenarla -> subir -> revisar el reporte de validación
-> importar. Nada se escribe en la base de datos hasta pulsar "Importar", y solo si el archivo
pasó la validación (o si el administrador elige importar únicamente las filas válidas).
Solo el rol Admin puede importar (se valida contra la BD en importacion.py).

Para iniciar pruebas con stock y OC en cero: use la plantilla vacía (no hay que cargar nada) o,
si ya hay datos de prueba, el modo "Reiniciar datos operativos" en Administración.
"""

from __future__ import annotations

import streamlit as st

from modulos import admin_db
from modulos import compras_ui as ui
from modulos import exportacion as exp
from modulos import importacion


@st.cache_resource(show_spinner=False)
def _preparar() -> bool:
    admin_db.asegurar_tablas()     # tabla de auditoría (cada carga queda registrada)
    return True


def _mensaje_resultado(tipo: str, res: dict) -> tuple[str, str]:
    """(nivel, texto) para mostrar tras importar."""
    omitidos = f" Se omitieron {res['omitidos']} fila(s) con errores." if res.get("omitidos") else ""
    if tipo == "entradas":
        extra = f" Se actualizaron los precios de {res['precios']} producto(s)." if res.get("precios") else ""
        return "success", f"Se importaron {res['importados']} lote(s) ({res['unidades']:g} unidades).{extra}{omitidos}"
    if tipo == "salidas":
        return "success", f"Se registraron {res['importados']} salida(s) FEFO ({res['unidades']:g} unidades).{omitidos}"
    texto = f"Se crearon {res['importados']} orden(es) de compra: " + \
            ", ".join(f"{num} (ref. {ref})" for ref, num in res["creadas"]) + "." + omitidos
    if res["fallidas"]:
        texto += " No se pudieron crear: " + " | ".join(f"{ref}: {msg}" for ref, msg in res["fallidas"])
        return "warning", texto
    return "success", texto


def panel(usuario: dict, tipo: str) -> None:
    """Interfaz de carga masiva para 'entradas', 'salidas' u 'oc'."""
    _preparar()
    esp = importacion.ESPECIFICACIONES[tipo]
    es_admin = usuario.get("rol") == "Admin" and st.session_state.get("usuario", {}).get("rol") == "Admin"
    if not es_admin:
        st.info("🔒 La carga masiva solo está disponible para el administrador.")
        return

    pref = f"cm_{tipo}"
    n = st.session_state.setdefault(f"{pref}_n", 0)     # Cambiar 'n' reinicia el cargador de archivos

    previo = st.session_state.pop(f"{pref}_resultado", None)
    if previo:
        (st.success if previo[0] == "success" else st.warning)(f"{'✅' if previo[0] == 'success' else '⚠️'} {previo[1]}")

    st.markdown(f"**{esp['titulo']}**")
    obligatorias = ", ".join(f"`{c.nombre}`" for c in esp["columnas"] if c.requerida)
    opcionales = ", ".join(f"`{c.nombre}`" for c in esp["columnas"] if not c.requerida)
    st.caption(f"Obligatorias: {obligatorias}. Opcionales: {opcionales}. "
               "La plantilla incluye la hoja 'Instrucciones' con el detalle de cada columna.")

    b1, b2, _ = st.columns([1.4, 1.4, 3])
    b1.download_button("⬇️ Plantilla Excel (.xlsx)", importacion.plantilla_xlsx(tipo), file_name=f"{esp['archivo']}.xlsx",
                       mime=exp.MIME_XLSX, key=f"{pref}_tpl_xlsx")
    b2.download_button("⬇️ Plantilla CSV", importacion.plantilla_csv(tipo), file_name=f"{esp['archivo']}.csv",
                       mime="text/csv", key=f"{pref}_tpl_csv")

    permitir_vencidos = actualizar_precios = False
    if tipo == "entradas":
        o1, o2 = st.columns(2)
        permitir_vencidos = o1.checkbox("Permitir lotes ya vencidos", key=f"{pref}_venc",
                                        help="Útil para cargar un inventario físico que incluye mercadería vencida.")
        actualizar_precios = o2.checkbox("Actualizar costo y precio de venta del producto", value=True,
                                         key=f"{pref}_precios",
                                         help="Si el archivo trae costo y/o margen, actualiza precio_costo y precio_venta.")

    archivo = st.file_uploader("Archivo .xlsx o .csv", type=["xlsx", "csv"], key=f"{pref}_file_{n}")
    if archivo is None:
        return

    try:
        df = importacion.leer_archivo(archivo.name, archivo.getvalue(), tipo)
    except ValueError as e:
        st.error(f"⚠️ {e}")
        return

    if tipo == "entradas":
        val = importacion.validar_entradas(df, permitir_vencidos=permitir_vencidos)
    elif tipo == "salidas":
        val = importacion.validar_salidas(df)
    else:
        val = importacion.validar_oc(df)

    # ----- Reporte de validación (antes de escribir nada)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Filas leídas", len(val.filas))
    c2.metric("OK", val.n_ok)
    c3.metric("Con aviso", val.n_aviso)
    c4.metric("Con errores", val.n_error)
    if tipo == "oc":
        st.caption(f"{len(val.grupos)} orden(es) detectada(s); {val.n_importables} lista(s) para importar.")

    reporte = val.reporte()
    solo_problemas = st.checkbox("Mostrar solo filas con errores o avisos", value=val.n_error > 0,
                                 key=f"{pref}_solo_prob_{n}")
    vista = reporte[~reporte["Estado"].str.contains("OK")] if solo_problemas else reporte
    if vista.empty and solo_problemas:
        st.success("✅ El archivo no tiene errores ni avisos.")
    else:
        st.dataframe(vista, use_container_width=True, hide_index=True)
    exp.boton_descarga_csv(reporte, f"validacion_{tipo}", f"{pref}_rep_{n}", etiqueta="⬇️ Descargar reporte de validación")

    # ----- Confirmación
    solo_validos = False
    if val.n_error:
        st.error(f"⛔ Hay {val.n_error} fila(s) con errores. Corrija el archivo y vuelva a subirlo, o importe solo lo válido.")
        solo_validos = st.checkbox("Importar solo los registros válidos (omitir los que tienen errores)",
                                   key=f"{pref}_solo_validos_{n}")
    puede = val.n_importables > 0 and (val.n_error == 0 or solo_validos)
    if st.button(f"✅ Importar {val.n_importables} registro(s)", type="primary", disabled=not puede,
                 key=f"{pref}_importar_{n}"):
        ok, res = ui.ejecutar(importacion.ejecutar_importacion, usuario, val, solo_validos=solo_validos,
                              nombre_archivo=archivo.name, actualizar_precios=actualizar_precios)
        if ok:
            st.session_state[f"{pref}_resultado"] = _mensaje_resultado(tipo, res)
            st.session_state[f"{pref}_n"] = n + 1
            st.rerun()
