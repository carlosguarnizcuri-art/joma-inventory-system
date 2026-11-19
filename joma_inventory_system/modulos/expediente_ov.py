"""
modulos/expediente_ov.py - Expediente digital de la Orden de Venta (FASE 4, sin código de interfaz)

Cada OV tiene una CARPETA NODRIZA  data/documentos_ov/<OV>/  (ej. data/documentos_ov/OV001-26/) que
centraliza: Cotización, Orden de Venta del sistema, OC del cliente, Guía de Remisión y Factura.

AUTOGUARDADO INTELIGENTE
  * Al crear o modificar la OV se genera su PDF corporativo y se deposita en la carpeta.
  * Al registrar una factura de cobro se genera su comprobante interno y se deposita en la carpeta.
  * Al confirmar un despacho se deposita el Packing List.
  * Todo documento cargado desde Órdenes de Venta, Despachos o Cuentas por Cobrar va a ESTA carpeta.

CIERRE DOCUMENTAL AUTOMÁTICO
  Checklist obligatorio: OC del cliente + Guía de Remisión + Factura FIRMADA.
  La OV permanece «Abierta» (con los faltantes a la vista) y pasa sola a «Cerrada» cuando el checklist
  está completo. Si luego se retira un documento obligatorio, vuelve a «Abierta».

Seguridad: nombres de archivo saneados, extensiones y tamaño controlados, rutas siempre dentro de
data/documentos_ov, y los documentos retirados se mueven a «_papelera» (no se borran).
"""

from __future__ import annotations

import mimetypes
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

import database as db
from modulos import documentos_pdf as pdf
from modulos import permisos, ventas_db
from modulos.documentos import EXTENSIONES_PERMITIDAS, MAX_BYTES, MAX_MB, slug

OV_DIR: Path = ventas_db.OV_DIR
TIPOS = ventas_db.TIPOS_DOCUMENTO
REQUERIDOS = ventas_db.REQUERIDOS
ETIQUETA_FALTANTE = {"OC_CLIENTE": "OC del cliente", "GUIA_REMISION": "Guía de Remisión", "FACTURA": "Factura firmada"}
MODULOS_PERMITIDOS = ("ov", "cxc", "despachos")     # desde qué módulo se puede cargar un documento
_NUMERO_RE = re.compile(r"^OV\d+-\d{2}$")


# ===========================================================================
# RUTAS
# ===========================================================================
def _dentro(ruta: Path) -> bool:
    try:
        ruta.resolve().relative_to(OV_DIR.resolve())
        return True
    except (ValueError, OSError):
        return False


def _numero_ov(conn_o_none, ov_id: int) -> str:
    fila = (conn_o_none.execute("SELECT numero_ov FROM ordenes_venta WHERE id = ?", (ov_id,)).fetchone()
            if conn_o_none is not None else db.fetch_one("SELECT numero_ov FROM ordenes_venta WHERE id = ?", (ov_id,)))
    if fila is None or not _NUMERO_RE.match(fila["numero_ov"]):
        raise ValueError("La orden de venta no existe.")
    return fila["numero_ov"]


def carpeta_ov(ov_id: int) -> Path:
    """Carpeta nodriza de la OV (la crea si falta)."""
    carpeta = OV_DIR / _numero_ov(None, ov_id)
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


def _nombre_unico(carpeta: Path, nombre: str) -> Path:
    destino = carpeta / nombre
    if not destino.exists():
        return destino
    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    base, ext = Path(nombre).stem, Path(nombre).suffix
    destino, n = carpeta / f"{base}_{marca}{ext}", 1
    while destino.exists():
        destino = carpeta / f"{base}_{marca}_{n}{ext}"
        n += 1
    return destino


def _ruta_real(ruta_rel: str) -> Optional[Path]:
    p = OV_DIR / ruta_rel
    return p if _dentro(p) else None


# ===========================================================================
# CHECKLIST Y CIERRE AUTOMÁTICO
# ===========================================================================
def _faltantes(conn, ov_id: int) -> list[str]:
    faltan = []
    for tipo in REQUERIDOS:
        sql = "SELECT COUNT(*) FROM ov_documentos WHERE ov_id = ? AND tipo_documento = ?"
        if tipo == "FACTURA":
            sql += " AND firmado = 1"
        if conn.execute(sql, (ov_id, tipo)).fetchone()[0] == 0:
            faltan.append(ETIQUETA_FALTANTE[tipo])
    return faltan


def checklist(ov_id: int) -> list[dict[str, Any]]:
    """Estado de cada documento obligatorio, para mostrar en pantalla."""
    conn = db.get_connection()
    try:
        out = []
        for tipo in REQUERIDOS:
            total = conn.execute("SELECT COUNT(*) FROM ov_documentos WHERE ov_id = ? AND tipo_documento = ?",
                                 (ov_id, tipo)).fetchone()[0]
            firmados = conn.execute("SELECT COUNT(*) FROM ov_documentos WHERE ov_id = ? AND tipo_documento = ? "
                                    "AND firmado = 1", (ov_id, tipo)).fetchone()[0]
            cumple = firmados > 0 if tipo == "FACTURA" else total > 0
            detalle = ("Cargado" if cumple else
                       ("Hay factura cargada pero NO firmada" if tipo == "FACTURA" and total else "Pendiente"))
            out.append({"tipo": tipo, "etiqueta": ETIQUETA_FALTANTE[tipo], "cumple": cumple, "detalle": detalle})
        return out
    finally:
        conn.close()


def evaluar_cierre(ov_id: int) -> dict[str, Any]:
    """
    Aplica la regla de cierre documental: completa -> 'Cerrada'; incompleta -> 'Abierta'.
    No toca las OV anuladas. Devuelve {'estado', 'faltantes', 'completo', 'cambio'}.
    """
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ov = conn.execute("SELECT estado FROM ordenes_venta WHERE id = ?", (ov_id,)).fetchone()
        if ov is None:
            raise ValueError("La orden de venta no existe.")
        faltan = _faltantes(conn, ov_id)
        estado, cambio = ov["estado"], None
        if estado == "Abierta" and not faltan:
            conn.execute("UPDATE ordenes_venta SET estado = 'Cerrada', cerrada_en = ? WHERE id = ?",
                         (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ov_id))
            estado, cambio = "Cerrada", "Cerrada"
        elif estado == "Cerrada" and faltan:
            conn.execute("UPDATE ordenes_venta SET estado = 'Abierta', cerrada_en = NULL WHERE id = ?", (ov_id,))
            estado, cambio = "Abierta", "Abierta"
    return {"estado": estado, "faltantes": faltan, "completo": not faltan, "cambio": cambio}


# ===========================================================================
# REGISTRO DE DOCUMENTOS
# ===========================================================================
def _insertar(conn, *, ov_id: int, tipo: str, nombre: str, ruta_rel: str, tamano: int, usuario_id: Optional[int],
              firmado: bool, origen: str, referencia: Optional[str], descripcion: Optional[str]) -> int:
    cur = conn.execute(
        """INSERT INTO ov_documentos (ov_id, tipo_documento, nombre, ruta, firmado, origen, referencia, descripcion,
                                      tamano_bytes, subido_por)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ov_id, tipo, nombre, ruta_rel, int(firmado), origen, referencia, descripcion, tamano, usuario_id))
    return cur.lastrowid


def adjuntar_documento(usuario: dict, ov_id: int, tipo: str, nombre_original: str, contenido: bytes, *,
                       descripcion: str = "", firmado: bool = False, modulo: str = "ov") -> dict[str, Any]:
    """
    Guarda un documento en la carpeta de la OV y re-evalúa el cierre automático.
    'modulo' indica desde dónde se carga (ov, cxc o despachos) para exigir el permiso correspondiente.
    """
    if modulo not in MODULOS_PERMITIDOS:
        raise ValueError("Origen de carga inválido.")
    permisos.exigir_escritura(usuario, modulo)
    if tipo not in TIPOS or tipo == "ORDEN_VENTA":
        raise ValueError("Tipo de documento inválido.")
    nombre_original = Path((nombre_original or "").replace("\\", "/")).name
    ext = Path(nombre_original).suffix.lower()
    if ext not in EXTENSIONES_PERMITIDAS:
        raise ValueError(f"Tipo de archivo no permitido ({ext or 'sin extensión'}). "
                         f"Permitidos: {', '.join(EXTENSIONES_PERMITIDAS)}.")
    if not contenido:
        raise ValueError(f"El archivo '{nombre_original}' está vacío.")
    if len(contenido) > MAX_BYTES:
        raise ValueError(f"'{nombre_original}' supera el máximo de {MAX_MB} MB.")

    ov = db.fetch_one("SELECT numero_ov, estado FROM ordenes_venta WHERE id = ?", (ov_id,))
    if ov is None:
        raise ValueError("La orden de venta no existe.")
    if ov["estado"] == "Anulada":
        raise ValueError("La orden de venta está anulada: no admite documentos.")
    carpeta = carpeta_ov(ov_id)
    destino = _nombre_unico(carpeta, f"{tipo}_{slug(Path(nombre_original).stem)}{ext}")
    try:
        destino.write_bytes(contenido)
    except OSError as e:
        raise ValueError(f"No se pudo guardar el archivo: {e}") from None
    try:
        with db.db_session() as conn:
            doc_id = _insertar(conn, ov_id=ov_id, tipo=tipo, nombre=destino.name, ruta_rel=f"{ov['numero_ov']}/{destino.name}",
                               tamano=len(contenido), usuario_id=usuario["id"], firmado=firmado and tipo == "FACTURA",
                               origen="MANUAL", referencia=None, descripcion=(descripcion or "").strip() or None)
    except Exception:
        destino.unlink(missing_ok=True)
        raise
    return {"doc_id": doc_id, "cierre": evaluar_cierre(ov_id)}


def adjuntar_automatico(ov_id: int, tipo: str, nombre: str, contenido: bytes, *, referencia: Optional[str],
                        usuario_id: Optional[int], firmado: bool = False, descripcion: str = "") -> int:
    """Deposita un documento GENERADO por el sistema. Si ya existe uno automático con la misma referencia, lo reemplaza."""
    numero = _numero_ov(None, ov_id)
    carpeta = carpeta_ov(ov_id)
    destino = carpeta / f"{slug(Path(nombre).stem)}{Path(nombre).suffix.lower()}"
    destino.write_bytes(contenido)
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previo = conn.execute(
            "SELECT id FROM ov_documentos WHERE ov_id = ? AND tipo_documento = ? AND origen = 'AUTO' "
            "AND COALESCE(referencia, '') = COALESCE(?, '')", (ov_id, tipo, referencia)).fetchone()
        if previo:
            conn.execute("UPDATE ov_documentos SET nombre = ?, ruta = ?, tamano_bytes = ?, subido_por = ?, "
                         "subido_en = datetime('now', 'localtime'), descripcion = ? WHERE id = ?",
                         (destino.name, f"{numero}/{destino.name}", len(contenido), usuario_id, descripcion or None,
                          previo["id"]))
            doc_id = previo["id"]
        else:
            doc_id = _insertar(conn, ov_id=ov_id, tipo=tipo, nombre=destino.name, ruta_rel=f"{numero}/{destino.name}",
                               tamano=len(contenido), usuario_id=usuario_id, firmado=firmado, origen="AUTO",
                               referencia=referencia, descripcion=descripcion or None)
    return doc_id


def retirar_documentos_automaticos(ov_id: int, tipo: str, referencia: str) -> None:
    """Retira (a la papelera) los documentos automáticos de una referencia, p. ej. al corregir una factura."""
    filas = db.fetch_df("SELECT id FROM ov_documentos WHERE ov_id = ? AND tipo_documento = ? AND origen = 'AUTO' "
                        "AND referencia = ?", (ov_id, tipo, referencia))
    for doc_id in filas["id"]:
        _retirar(int(doc_id))
    evaluar_cierre(ov_id)


def _retirar(doc_id: int) -> None:
    """Quita el registro y MUEVE el archivo a la papelera de la OV (nunca se borra definitivamente)."""
    with db.db_session() as conn:
        fila = conn.execute("SELECT ruta FROM ov_documentos WHERE id = ?", (doc_id,)).fetchone()
        if fila is None:
            return
        conn.execute("DELETE FROM ov_documentos WHERE id = ?", (doc_id,))
    p = _ruta_real(fila["ruta"])
    if p is not None and p.is_file():
        papelera = p.parent / "_papelera"
        papelera.mkdir(exist_ok=True)
        shutil.move(str(p), str(_nombre_unico(papelera, p.name)))


def eliminar_documento(usuario: dict, doc_id: int, modulo: str = "ov") -> dict[str, Any]:
    if modulo not in MODULOS_PERMITIDOS:
        raise ValueError("Origen inválido.")
    permisos.exigir_escritura(usuario, modulo)
    fila = db.fetch_one("SELECT ov_id, tipo_documento, origen FROM ov_documentos WHERE id = ?", (doc_id,))
    if fila is None:
        raise ValueError("El documento no existe.")
    if fila["tipo_documento"] == "ORDEN_VENTA":
        raise ValueError("El PDF de la Orden de Venta lo genera el sistema: use «Regenerar PDF» en lugar de eliminarlo.")
    _retirar(doc_id)
    return evaluar_cierre(fila["ov_id"])


def marcar_firmado(usuario: dict, doc_id: int, firmado: bool) -> dict[str, Any]:
    """Marca una factura como firmada / no firmada (requisito del cierre documental)."""
    permisos.exigir_escritura_alguno(usuario, ["ov", "cxc"])
    fila = db.fetch_one("SELECT ov_id, tipo_documento FROM ov_documentos WHERE id = ?", (doc_id,))
    if fila is None:
        raise ValueError("El documento no existe.")
    if fila["tipo_documento"] != "FACTURA":
        raise ValueError("Solo las facturas llevan marca de firma.")
    with db.db_session() as conn:
        conn.execute("UPDATE ov_documentos SET firmado = ? WHERE id = ?", (int(firmado), doc_id))
    return evaluar_cierre(fila["ov_id"])


# ===========================================================================
# LECTURA
# ===========================================================================
def listar_documentos(ov_id: int) -> pd.DataFrame:
    df = db.fetch_df(
        """SELECT d.id, d.tipo_documento, d.nombre, d.ruta, d.firmado, d.origen, d.referencia, d.descripcion,
                  d.tamano_bytes, d.subido_en, COALESCE(u.nombre_completo, 'Sistema') AS subido_por
           FROM ov_documentos d LEFT JOIN usuarios u ON u.id = d.subido_por
           WHERE d.ov_id = ? ORDER BY d.id DESC""", (ov_id,))
    df["etiqueta"] = df["tipo_documento"].map(TIPOS)

    def existe(ruta: str) -> bool:
        p = _ruta_real(ruta)
        return bool(p and p.is_file())

    df["disponible"] = [existe(r) for r in df["ruta"]]
    return df


def leer_documento(doc_id: int) -> tuple[bytes, str, str]:
    fila = db.fetch_one("SELECT nombre, ruta FROM ov_documentos WHERE id = ?", (doc_id,))
    if fila is None:
        raise ValueError("El documento no está registrado.")
    p = _ruta_real(fila["ruta"])
    if p is None or not p.is_file():
        raise ValueError("El archivo no está disponible (se movió o se eliminó).")
    if p.stat().st_size > MAX_BYTES:
        raise ValueError(f"El archivo supera {MAX_MB} MB.")
    return p.read_bytes(), p.name, mimetypes.guess_type(p.name)[0] or "application/octet-stream"


# ===========================================================================
# DOCUMENTOS GENERADOS POR EL SISTEMA
# ===========================================================================
def _fmt(iso: Any) -> str:
    try:
        return datetime.strptime(str(iso)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return "—"


def _cabecera_cliente(ov) -> list[tuple[str, str]]:
    return [("Cliente", ov["cliente_nombre"]), (ov["tipo_documento"], ov["numero_documento"]),
            ("Dirección", ov["cliente_direccion"] or "—"), ("OC del cliente", ov["oc_cliente"] or "—")]


def pdf_orden_venta_bytes(ov_id: int, generado_por: str = "") -> bytes:
    """PDF corporativo de la Orden de Venta: logotipo, datos fiscales, ítems, subtotal, IGV y total."""
    ov = ventas_db.obtener_ov(ov_id)
    if ov is None:
        raise ValueError("La orden de venta no existe.")
    lineas = ventas_db.lineas_ov(ov_id)
    simbolo = "S/" if ov["moneda"] == "PEN" else "US$"
    enc = [("N° de OV", ov["numero_ov"]), ("Fecha de emisión", _fmt(ov["fecha_emision"])), *_cabecera_cliente(ov),
           ("Entrega estimada", _fmt(ov["fecha_entrega_estimada"]) if ov["fecha_entrega_estimada"] else "—"),
           ("Moneda", ov["moneda"])]
    columnas = ["#", "SKU", "Descripción", "Unidad", "Cantidad", "P. unitario", "Importe"]
    filas = [[i, r.codigo, r.producto, r.unidad, f"{r.cantidad:g}", f"{r.precio_unitario:,.2f}", f"{r.subtotal:,.2f}"]
             for i, r in enumerate(lineas.itertuples(), start=1)]
    totales = [("Subtotal", f"{simbolo} {ov['monto_subtotal']:,.2f}"),
               (f"IGV ({ov['igv_porcentaje']:g}%)", f"{simbolo} {ov['monto_igv']:,.2f}"),
               ("TOTAL", f"{simbolo} {ov['monto_total']:,.2f}")]
    notas = ["Precios unitarios expresados sin IGV."]
    if ov["condiciones"]:
        notas.append(f"Condiciones comerciales: {ov['condiciones']}")
    if ov["observaciones"]:
        notas.append(f"Observaciones: {ov['observaciones']}")
    return pdf.pdf_documento(f"ORDEN DE VENTA {ov['numero_ov']}", encabezado=enc, columnas=columnas, filas=filas,
                             anchos=[0.5, 1.3, 4, 1, 1.2, 1.4, 1.5], alinear_derecha=[4, 5, 6], totales=totales,
                             notas=notas, firmas=["Por JOMA S.A.C.", "Conforme del cliente"],
                             generado_por=generado_por, membrete=True)


def generar_pdf_orden_venta(ov_id: int, usuario: Optional[dict] = None) -> int:
    """Genera el PDF de la OV y lo deposita (o actualiza) en su carpeta nodriza."""
    datos = pdf_orden_venta_bytes(ov_id, (usuario or {}).get("nombre_completo", ""))
    numero = _numero_ov(None, ov_id)
    doc_id = adjuntar_automatico(ov_id, "ORDEN_VENTA", f"{numero}.pdf", datos, referencia=numero,
                                 usuario_id=(usuario or {}).get("id"), descripcion="Orden de Venta emitida por el sistema")
    return doc_id


def regenerar_pdf(usuario: dict, ov_id: int) -> int:
    permisos.exigir_escritura(usuario, "ov")
    return generar_pdf_orden_venta(ov_id, usuario)


def generar_comprobante_factura(factura_id: int, usuario: Optional[dict] = None) -> int:
    """
    Comprobante INTERNO de la factura de cobro, depositado en la carpeta de la OV (sin firmar).
    El comprobante electrónico oficial se emite en SUNAT; la copia firmada se carga como «Factura firmada».
    """
    f = ventas_db.obtener_factura_venta(factura_id)
    if f is None:
        raise ValueError("La factura no existe.")
    ov = ventas_db.obtener_ov(f["ov_id"])
    simbolo = "S/" if f["moneda"] == "PEN" else "US$"
    enc = [("N° de factura", f["numero_factura"]), ("Fecha de emisión", _fmt(f["fecha_emision"])),
           *_cabecera_cliente(ov), ("Orden de Venta", f["numero_ov"]),
           ("Vencimiento de pago", _fmt(f["fecha_vencimiento_pago"]) if f["fecha_vencimiento_pago"] else "—"),
           ("Moneda", f["moneda"])]
    filas = [[1, f"Venta de mercadería según Orden de Venta {f['numero_ov']}", f"{simbolo} {f['monto_subtotal']:,.2f}"]]
    totales = [("Subtotal", f"{simbolo} {f['monto_subtotal']:,.2f}"), (f"IGV ({ov['igv_porcentaje']:g}%)", f"{simbolo} {f['monto_igv']:,.2f}"),
               ("TOTAL A COBRAR", f"{simbolo} {f['monto_total']:,.2f}")]
    datos = pdf.pdf_documento(
        f"FACTURA DE COBRO {f['numero_factura']}", encabezado=enc, columnas=["#", "Concepto", "Importe"], filas=filas,
        anchos=[0.5, 6, 1.6], alinear_derecha=[2], totales=totales,
        notas=["Documento interno de control. El comprobante electrónico oficial se emite en SUNAT; cargue aquí la copia "
               "FIRMADA por el cliente para completar el expediente."],
        firmas=["Por JOMA S.A.C.", "Recibí conforme (cliente)"], generado_por=(usuario or {}).get("nombre_completo", ""),
        membrete=True)
    doc_id = adjuntar_automatico(f["ov_id"], "FACTURA", f"{f['numero_factura']}_comprobante.pdf", datos,
                                 referencia=f["numero_factura"], usuario_id=(usuario or {}).get("id"),
                                 firmado=False, descripcion="Comprobante interno de la factura de cobro")
    evaluar_cierre(f["ov_id"])
    return doc_id
