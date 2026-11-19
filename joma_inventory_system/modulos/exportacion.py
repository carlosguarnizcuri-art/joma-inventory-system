"""
modulos/exportacion.py - Exportación universal a CSV compatible con Excel en español (MÓDULO 4)

Toda tabla o reporte exportable del sistema se genera con las mismas reglas:
  * Separador de columnas: punto y coma (;)   -> cada campo cae en su propia columna.
  * Codificación: utf-8-sig (UTF-8 con BOM)   -> Excel respeta tildes y la "ñ".
  * Saltos de línea \\r\\n                       -> formato nativo de Windows/Excel.
  * Separador decimal configurable (',' o '.') -> debe coincidir con el Excel del usuario.
  * Encabezados legibles, fechas dd/mm/aaaa, estados sin emojis.
  * Protección contra "inyección de fórmulas": un texto que empiece con = + @ o - se
    exporta con un apóstrofo delante para que Excel NUNCA lo ejecute como fórmula.

Uso típico:
    df_exp = preparar_export(df, {"codigo": "Código", ...}, fechas={"fecha": "%d/%m/%Y"})
    boton_descarga_csv(df_exp, "kardex", "clave_unica")
"""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from typing import Optional

import pandas as pd
import streamlit as st

SEPARADOR_CSV = ";"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CODIFICACION_CSV = "utf-8-sig"
DECIMALES = 3
_PREFIJOS_PELIGROSOS = ("=", "+", "@", "-", "\t", "\r")


def sin_emoji(texto):
    """'🔴 Stock bajo' -> 'Stock bajo' (los emojis no se exportan a Excel)."""
    if not isinstance(texto, str):
        return texto
    partes = texto.split(" ", 1)
    return partes[1] if len(partes) == 2 and not partes[0].isalnum() else texto


def _es_numero(texto: str) -> bool:
    try:
        float(texto.replace(",", ".").replace(" ", ""))
        return True
    except ValueError:
        return False


def neutralizar_formulas(df: pd.DataFrame) -> pd.DataFrame:
    """Antepone ' a los textos que Excel interpretaría como fórmula (seguridad)."""
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object or str(out[col].dtype).startswith("str"):
            out[col] = out[col].map(
                lambda v: "'" + v if isinstance(v, str) and v.startswith(_PREFIJOS_PELIGROSOS)
                and not _es_numero(v) else v)
    return out


def preparar_export(df: pd.DataFrame, columnas: dict[str, str], *, fechas: Optional[dict[str, str]] = None,
                    estados: tuple | list = (), enteros: tuple | list = (),
                    dos_decimales: tuple | list = (), como_fecha: bool = False) -> pd.DataFrame:
    """
    Devuelve una copia lista para exportar: solo las columnas pedidas (en ese orden) con
    encabezados legibles.
      fechas        -> {columna: formato strftime}   (ej. {"fecha": "%d/%m/%Y %H:%M"})
      estados       -> columnas a las que se les quita el emoji
      enteros       -> columnas que se exportan como entero (admite vacíos)
      dos_decimales -> columnas monetarias redondeadas a 2 decimales
      como_fecha    -> True para Excel (.xlsx): deja fechas REALES (ordenan y filtran bien) en lugar de texto
    """
    out = df[list(columnas)].copy()
    for col, formato in (fechas or {}).items():
        convertida = pd.to_datetime(out[col], errors="coerce")
        out[col] = convertida if como_fecha else convertida.dt.strftime(formato).fillna("")
    for col in estados:
        out[col] = out[col].map(sin_emoji)
    for col in enteros:
        out[col] = pd.to_numeric(out[col], errors="coerce").round().astype("Int64")
    numericas = out.select_dtypes(include="number").columns   # (no redondear fechas ni textos)
    out[numericas] = out[numericas].round(DECIMALES)
    for col in dos_decimales:
        out[col] = out[col].round(2)
    return out.rename(columns=columnas)


def csv_para_excel(df: pd.DataFrame, decimal: str = ",") -> bytes:
    """Serializa a CSV (sep=';', utf-8-sig, \\r\\n) listo para abrir en Excel en español."""
    texto = neutralizar_formulas(df).to_csv(index=False, sep=SEPARADOR_CSV, decimal=decimal,
                                            lineterminator="\r\n")
    return texto.encode(CODIFICACION_CSV)


def nombre_archivo(base: str) -> str:
    """'kardex' -> 'kardex_2026-11-12.csv'"""
    return f"{base}_{date.today().isoformat()}.csv"


def boton_descarga_csv(df_export: pd.DataFrame, base_archivo: str, clave: str, *,
                       etiqueta: str = "⬇️ Exportar CSV (Excel)") -> None:
    """Botón de descarga + selector del separador decimal. 'clave' debe ser única en la pantalla."""
    c_boton, c_decimal, _ = st.columns([1.4, 2.4, 3])
    decimal = c_decimal.radio(
        "Separador decimal", [",", "."], horizontal=True, key=f"{clave}_dec",
        help="Use ',' si su Excel muestra decimales con coma (ej. 12,50) o '.' si usa punto (ej. 12.50). "
             "Debe coincidir con la configuración regional de su Windows para que los números se lean bien.")
    c_boton.download_button(etiqueta, csv_para_excel(df_export, decimal), file_name=nombre_archivo(base_archivo),
                            mime="text/csv", key=clave)


# ---------------------------------------------------------------------------
# EXCEL (.xlsx) con pandas + BytesIO
# ---------------------------------------------------------------------------
def _nombre_hoja(nombre: str) -> str:
    """Excel limita los nombres de hoja a 31 caracteres y prohíbe []:*?/\\ ."""
    limpio = "".join(c for c in nombre if c not in "[]:*?/\\").strip()
    return (limpio or "Hoja")[:31]


def _dar_formato(ws) -> None:
    """Encabezado destacado, primera fila fija, filtros, anchos de columna y formatos numéricos/fecha."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    relleno = PatternFill("solid", fgColor="1F3A5F")
    for celda in ws[1]:
        celda.font = Font(bold=True, color="FFFFFF")
        celda.fill = relleno
        celda.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    if ws.max_row > 1 and ws.max_column >= 1:
        ws.auto_filter.ref = ws.dimensions

    anchos: dict[int, int] = {}
    for fila in ws.iter_rows(min_row=1):
        for c in fila:
            v = c.value
            if v is None:
                continue
            if isinstance(v, datetime):
                c.number_format = "dd/mm/yyyy hh:mm" if (v.hour or v.minute or v.second) else "dd/mm/yyyy"
                largo = 16
            elif isinstance(v, float):
                c.number_format = "#,##0.00"
                largo = len(f"{v:,.2f}")
            elif isinstance(v, int) and not isinstance(v, bool):
                c.number_format = "#,##0"
                largo = len(str(v))
            else:
                largo = len(str(v))
            anchos[c.column] = max(anchos.get(c.column, 0), largo)
    for col, largo in anchos.items():
        ws.column_dimensions[get_column_letter(col)].width = min(max(largo + 2, 10), 55)


def excel_para_descarga(hojas: dict[str, pd.DataFrame], *, parametros: Optional[dict[str, str]] = None) -> bytes:
    """
    Genera un libro .xlsx en memoria (BytesIO) con una hoja por DataFrame y, opcionalmente,
    una hoja final 'Parámetros' con los datos del reporte (filtros, quién lo generó, fecha).
    Los textos peligrosos se neutralizan: openpyxl convertiría '=...' en una fórmula real.
    """
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for nombre, df in hojas.items():
            neutralizar_formulas(df).to_excel(writer, sheet_name=_nombre_hoja(nombre), index=False)
        if parametros:
            pd.DataFrame({"Parámetro": list(parametros), "Valor": [str(v) for v in parametros.values()]}) \
                .pipe(neutralizar_formulas).to_excel(writer, sheet_name="Parámetros", index=False)
        for ws in writer.book.worksheets:
            _dar_formato(ws)
    return buffer.getvalue()


def boton_descarga_xlsx(hojas: dict[str, pd.DataFrame], base_archivo: str, clave: str, *,
                        parametros: Optional[dict[str, str]] = None,
                        etiqueta: str = "⬇️ Exportar Excel (.xlsx)") -> None:
    """Botón de descarga de Excel. 'clave' debe ser única en la pantalla."""
    st.download_button(etiqueta, excel_para_descarga(hojas, parametros=parametros),
                       file_name=nombre_archivo(base_archivo).replace(".csv", ".xlsx"),
                       mime=MIME_XLSX, key=clave)
