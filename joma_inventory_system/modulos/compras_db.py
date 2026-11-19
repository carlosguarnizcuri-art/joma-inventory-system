"""
modulos/compras_db.py - Capa de datos y reglas de negocio del MÓDULO 3

Contiene (sin código de interfaz):
  * Esquema adicional: `oc_detalle` (líneas de cada OC) y `documentos_adjuntos`.
  * Consultas para listados, detalle y resumen.
  * Operaciones de escritura (todas exigen rol Admin validado contra la BD):
      crear/editar/cerrar/eliminar OC, líneas de OC, recepción de mercadería
      (genera lote + kardex), crear/editar/cerrar/eliminar facturas.

La recepción reutiliza el registro de movimientos del Módulo 2 (kardex), de modo que
todo ingreso proveniente de una OC queda trazado como ENTRADA con referencia a la OC.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

import database as db
from modulos import documentos
from modulos import inventario as inv   # reutiliza kardex (tabla y registro de movimientos)

DECIMALES = inv.DECIMALES
ESTADOS = ("Abierto", "Cerrado")
MONEDAS = ("PEN", "USD")
DIAS_FACTURA_POR_VENCER = 7

SQL_ESQUEMA = """
CREATE TABLE IF NOT EXISTS oc_detalle (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    orden_compra_id   INTEGER NOT NULL REFERENCES ordenes_compra(id) ON DELETE CASCADE,
    producto_id       INTEGER NOT NULL REFERENCES productos(id)      ON DELETE RESTRICT,
    cantidad_pedida   REAL    NOT NULL CHECK (cantidad_pedida > 0),
    cantidad_recibida REAL    NOT NULL DEFAULT 0 CHECK (cantidad_recibida >= 0),
    precio_unitario   REAL    NOT NULL DEFAULT 0 CHECK (precio_unitario >= 0),
    UNIQUE (orden_compra_id, producto_id)
);
CREATE INDEX IF NOT EXISTS idx_ocdet_oc ON oc_detalle (orden_compra_id);

CREATE TABLE IF NOT EXISTS documentos_adjuntos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo_entidad  TEXT    NOT NULL CHECK (tipo_entidad IN ('OC', 'FACTURA')),
    entidad_id    INTEGER NOT NULL,
    tipo_vinculo  TEXT    NOT NULL CHECK (tipo_vinculo IN ('ARCHIVO', 'RUTA_EXTERNA')),
    nombre        TEXT    NOT NULL,
    ruta          TEXT    NOT NULL,        -- ARCHIVO: relativa a data/documentos | RUTA_EXTERNA: absoluta
    descripcion   TEXT,
    tamano_bytes  INTEGER,
    subido_por    INTEGER REFERENCES usuarios(id),
    subido_en     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_docs_entidad ON documentos_adjuntos (tipo_entidad, entidad_id);
"""


# ===========================================================================
# ESQUEMA E INICIALIZACIÓN
# ===========================================================================
def asegurar_esquema() -> None:
    """Crea las tablas del módulo (idempotente), siembra un detalle de ejemplo la primera vez
    y normaliza rutas heredadas a formato relativo."""
    inv._asegurar_tabla_movimientos()
    with db.db_session() as conn:
        es_nueva = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'oc_detalle'").fetchone() is None
        conn.executescript(SQL_ESQUEMA)
        if es_nueva:
            _sembrar_detalle_demo(conn)
        _normalizar_rutas(conn)


def _sembrar_detalle_demo(conn: sqlite3.Connection) -> None:
    """Agrega líneas de ejemplo a OC-2026-003 (datos de prueba) para poder probar la recepción."""
    oc = conn.execute("SELECT id FROM ordenes_compra WHERE numero_oc = 'OC-2026-003'").fetchone()
    if oc is None:
        return
    prod = {r["codigo"]: r["id"] for r in conn.execute("SELECT id, codigo FROM productos")}
    lineas = [("PRD-001", 200, 6.50), ("PRD-004", 300, 3.80), ("PRD-006", 150, 2.90)]
    if any(c not in prod for c, _, _ in lineas):
        return
    conn.executemany(
        "INSERT INTO oc_detalle (orden_compra_id, producto_id, cantidad_pedida, precio_unitario) VALUES (?, ?, ?, ?)",
        [(oc["id"], prod[c], cant, precio) for c, cant, precio in lineas],
    )
    _recalcular_monto(conn, oc["id"])


def _normalizar_rutas(conn: sqlite3.Connection) -> None:
    """Convierte rutas absolutas guardadas dentro de data/documentos a relativas (portables)."""
    for tabla, col in (("ordenes_compra", "ruta_carpeta"), ("facturas", "ruta_documento")):
        for fila in conn.execute(f"SELECT id, {col} AS ruta FROM {tabla} WHERE {col} IS NOT NULL").fetchall():
            p = Path(fila["ruta"])
            if p.is_absolute() and documentos.dentro_de_docs(p):
                rel = documentos.a_relativa(p)
                if rel != fila["ruta"]:
                    conn.execute(f"UPDATE {tabla} SET {col} = ? WHERE id = ?", (rel, fila["id"]))


# ===========================================================================
# SEMÁFOROS / ESTADOS VISUALES
# ===========================================================================
def estado_oc_visual(estado: str, entrega: Any, hoy_iso: str) -> str:
    if estado == "Cerrado":
        return "✅ Cerrada"
    if isinstance(entrega, str) and entrega < hoy_iso:
        return "🔴 Atrasada"
    return "🟡 Abierta"


def estado_factura_visual(estado: str, vence_pago: Any, hoy_iso: str, limite_iso: str) -> str:
    if estado == "Cerrado":
        return "✅ Cerrada"
    if isinstance(vence_pago, str):
        if vence_pago < hoy_iso:
            return "🔴 Vencida"
        if vence_pago <= limite_iso:
            return "🟠 Por vencer"
    return "🟡 Abierta"


# ===========================================================================
# LECTURA
# ===========================================================================
def resumen_compras() -> dict[str, float]:
    hoy = date.today().isoformat()
    limite = (date.today() + timedelta(days=DIAS_FACTURA_POR_VENCER)).isoformat()

    def esc(q: str, p: tuple = ()) -> float:
        fila = db.fetch_one(q, p)
        return fila[0] if fila and fila[0] is not None else 0

    return {
        "oc_abiertas": esc("SELECT COUNT(*) FROM ordenes_compra WHERE estado = 'Abierto'"),
        "oc_atrasadas": esc("SELECT COUNT(*) FROM ordenes_compra WHERE estado = 'Abierto' "
                            "AND fecha_entrega_estimada < ?", (hoy,)),
        "facturas_abiertas": esc("SELECT COUNT(*) FROM facturas WHERE estado = 'Abierto'"),
        "facturas_vencidas": esc("SELECT COUNT(*) FROM facturas WHERE estado = 'Abierto' "
                                 "AND fecha_vencimiento_pago < ?", (hoy,)),
        "facturas_por_vencer": esc("SELECT COUNT(*) FROM facturas WHERE estado = 'Abierto' "
                                   "AND fecha_vencimiento_pago BETWEEN ? AND ?", (hoy, limite)),
        "por_pagar_pen": esc("SELECT SUM(monto_total) FROM facturas WHERE estado = 'Abierto' AND moneda = 'PEN'"),
        "por_pagar_usd": esc("SELECT SUM(monto_total) FROM facturas WHERE estado = 'Abierto' AND moneda = 'USD'"),
    }


def listar_ocs() -> pd.DataFrame:
    df = db.fetch_df(
        """SELECT oc.id, oc.numero_oc, oc.proveedor, oc.fecha_emision, oc.fecha_entrega_estimada,
                  oc.monto_total, oc.moneda, oc.estado, oc.observaciones,
                  COALESCE((SELECT SUM(cantidad_pedida)   FROM oc_detalle d WHERE d.orden_compra_id = oc.id), 0) AS pedido,
                  COALESCE((SELECT SUM(cantidad_recibida) FROM oc_detalle d WHERE d.orden_compra_id = oc.id), 0) AS recibido,
                  (SELECT COUNT(*) FROM facturas f WHERE f.orden_compra_id = oc.id) AS n_facturas,
                  (SELECT COUNT(*) FROM documentos_adjuntos a
                    WHERE a.tipo_entidad = 'OC' AND a.entidad_id = oc.id) AS n_docs
           FROM ordenes_compra oc
           ORDER BY oc.fecha_emision DESC, oc.id DESC"""
    )
    hoy = date.today().isoformat()
    df["avance"] = (df["recibido"] / df["pedido"].where(df["pedido"] > 0) * 100).round(1)
    df["estado_visual"] = [estado_oc_visual(e, f, hoy) for e, f in zip(df["estado"], df["fecha_entrega_estimada"])]
    return df


def obtener_oc(oc_id: int) -> Optional[sqlite3.Row]:
    return db.fetch_one("SELECT * FROM ordenes_compra WHERE id = ?", (oc_id,))


def id_oc_por_numero(numero_oc: str) -> Optional[int]:
    fila = db.fetch_one("SELECT id FROM ordenes_compra WHERE numero_oc = ?", ((numero_oc or "").strip().upper(),))
    return fila["id"] if fila else None


def lineas_oc(oc_id: int) -> pd.DataFrame:
    df = db.fetch_df(
        """SELECT d.id, d.producto_id, p.codigo, p.nombre AS producto, p.unidad_medida AS unidad,
                  d.cantidad_pedida, d.cantidad_recibida, d.precio_unitario
           FROM oc_detalle d JOIN productos p ON p.id = d.producto_id
           WHERE d.orden_compra_id = ? ORDER BY d.id""", (oc_id,))
    df["pendiente"] = (df["cantidad_pedida"] - df["cantidad_recibida"]).round(DECIMALES)
    df["subtotal"] = (df["cantidad_pedida"] * df["precio_unitario"]).round(2)
    return df


def ocs_con_pendientes() -> pd.DataFrame:
    """OC abiertas que tienen líneas con cantidad pendiente de recibir."""
    return db.fetch_df(
        """SELECT oc.id, oc.numero_oc, oc.proveedor, oc.fecha_entrega_estimada,
                  ROUND(SUM(d.cantidad_pedida - d.cantidad_recibida), 3) AS pendiente
           FROM ordenes_compra oc JOIN oc_detalle d ON d.orden_compra_id = oc.id
           WHERE oc.estado = 'Abierto'
           GROUP BY oc.id HAVING pendiente > 0
           ORDER BY oc.fecha_emision DESC, oc.id DESC""")


def listar_facturas() -> pd.DataFrame:
    df = db.fetch_df(
        """SELECT f.id, f.numero_factura, f.proveedor, f.orden_compra_id, oc.numero_oc,
                  f.fecha_emision, f.fecha_vencimiento_pago, f.monto_total, f.moneda, f.estado,
                  oc.monto_total AS monto_oc, oc.moneda AS moneda_oc, f.observaciones,
                  (SELECT COUNT(*) FROM documentos_adjuntos a
                    WHERE a.tipo_entidad = 'FACTURA' AND a.entidad_id = f.id) AS n_docs
           FROM facturas f LEFT JOIN ordenes_compra oc ON oc.id = f.orden_compra_id
           ORDER BY f.fecha_emision DESC, f.id DESC""")
    hoy = date.today()
    limite = (hoy + timedelta(days=DIAS_FACTURA_POR_VENCER)).isoformat()
    df["estado_visual"] = [estado_factura_visual(e, v, hoy.isoformat(), limite)
                           for e, v in zip(df["estado"], df["fecha_vencimiento_pago"])]
    df["dias_para_pago"] = (pd.to_datetime(df["fecha_vencimiento_pago"]) - pd.Timestamp(hoy)).dt.days
    return df


def obtener_factura(factura_id: int) -> Optional[sqlite3.Row]:
    return db.fetch_one("SELECT * FROM facturas WHERE id = ?", (factura_id,))


def id_factura(numero: str, proveedor: str) -> Optional[int]:
    fila = db.fetch_one("SELECT id FROM facturas WHERE numero_factura = ? AND proveedor = ?",
                        ((numero or "").strip().upper(), _limpiar(proveedor)))
    return fila["id"] if fila else None


def facturas_de_oc(oc_id: int) -> pd.DataFrame:
    return db.fetch_df(
        "SELECT id, numero_factura, fecha_emision, fecha_vencimiento_pago, monto_total, moneda, estado "
        "FROM facturas WHERE orden_compra_id = ? ORDER BY fecha_emision DESC", (oc_id,))


# ===========================================================================
# ESCRITURA: ÓRDENES DE COMPRA
# ===========================================================================
def _limpiar(texto: Optional[str]) -> str:
    """Quita espacios sobrantes (inicio, fin y repetidos)."""
    return " ".join((texto or "").split())


def _recalcular_monto(conn: sqlite3.Connection, oc_id: int) -> None:
    """Si la OC tiene líneas, su monto total pasa a ser la suma de los subtotales."""
    n, total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(ROUND(cantidad_pedida * precio_unitario, 2)), 0) "
        "FROM oc_detalle WHERE orden_compra_id = ?", (oc_id,)).fetchone()
    if n:
        conn.execute("UPDATE ordenes_compra SET monto_total = ROUND(?, 2) WHERE id = ?", (total, oc_id))


def _oc_abierta(conn: sqlite3.Connection, oc_id: int) -> sqlite3.Row:
    oc = conn.execute("SELECT * FROM ordenes_compra WHERE id = ?", (oc_id,)).fetchone()
    if oc is None:
        raise ValueError("La orden de compra no existe.")
    if oc["estado"] != "Abierto":
        raise ValueError("La OC está cerrada. Reábrala para modificarla.")
    return oc


def _normalizar_lineas(lineas: list[dict]) -> list[dict]:
    """Valida y normaliza líneas [{producto_id, cantidad, precio}]."""
    vistos: set[int] = set()
    out = []
    for i, l in enumerate(lineas, start=1):
        cantidad = round(float(l.get("cantidad") or 0), DECIMALES)
        precio = round(float(l.get("precio") or 0), 4)
        if not cantidad > 0:
            raise ValueError(f"Línea {i}: la cantidad debe ser mayor que cero.")
        if not precio >= 0:
            raise ValueError(f"Línea {i}: el precio no puede ser negativo.")
        pid = int(l["producto_id"])
        if pid in vistos:
            raise ValueError(f"Línea {i}: el producto está repetido en la OC.")
        vistos.add(pid)
        out.append({"producto_id": pid, "cantidad": cantidad, "precio": precio})
    return out


# --- Numeración automática correlativa de OC (ej. OC-00046) -------------------------------------
OC_PREFIJO = "OC-"
OC_ANCHO = 5                                   # OC-00001 ... OC-99999 (crece solo si se supera)
_OC_CORRELATIVO_RE = re.compile(rf"^{re.escape(OC_PREFIJO)}(\d+)$")


def _ultimo_correlativo_oc(conn: sqlite3.Connection) -> int:
    """Mayor número existente con formato OC-<dígitos>. Los formatos antiguos (ej. OC-2026-001) se ignoran."""
    mayor = 0
    for fila in conn.execute("SELECT numero_oc FROM ordenes_compra WHERE numero_oc LIKE ?", (OC_PREFIJO + "%",)):
        m = _OC_CORRELATIVO_RE.match(fila["numero_oc"])
        if m:
            mayor = max(mayor, int(m.group(1)))
    return mayor


def siguiente_numero_oc(conn: sqlite3.Connection) -> str:
    """Siguiente número correlativo, continuando desde el último existente (OC-00045 -> OC-00046)."""
    return f"{OC_PREFIJO}{_ultimo_correlativo_oc(conn) + 1:0{OC_ANCHO}d}"


def vista_previa_numero_oc() -> str:
    """Número que se asignaría ahora (solo informativo: el definitivo se asigna al guardar)."""
    conn = db.get_connection()
    try:
        return siguiente_numero_oc(conn)
    finally:
        conn.close()


def crear_oc(usuario: dict, *, proveedor: str, fecha_emision: date,
             fecha_entrega: Optional[date], moneda: str, observaciones: str,
             lineas: list[dict], monto_manual: float = 0.0, numero_oc: Optional[str] = None) -> int:
    """
    Crea una OC con su detalle y su carpeta de documentos. Solo Admin.
    Si no se indica 'numero_oc' se asigna el siguiente correlativo AUTOMÁTICAMENTE, dentro de la
    misma transacción que inserta la OC (tres usuarios a la vez nunca obtienen el mismo número).
    """
    documentos.exigir_admin(usuario)
    numero = (numero_oc or "").strip().upper()
    proveedor = _limpiar(proveedor)
    if not proveedor:
        raise ValueError("Ingrese el proveedor.")
    if moneda not in MONEDAS:
        raise ValueError("Moneda inválida.")
    if fecha_entrega and fecha_entrega < fecha_emision:
        raise ValueError("La entrega estimada no puede ser anterior a la fecha de emisión.")
    lineas = _normalizar_lineas(lineas)
    if lineas:
        monto = round(sum(round(l["cantidad"] * l["precio"], 2) for l in lineas), 2)
    else:
        monto = round(float(monto_manual or 0), 2)
        if not monto >= 0:
            raise ValueError("El monto no puede ser negativo.")

    try:
        with db.db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not numero:
                numero = siguiente_numero_oc(conn)
            carpeta = documentos.carpeta_por_defecto("OC", numero)
            if conn.execute("SELECT 1 FROM ordenes_compra WHERE numero_oc = ?", (numero,)).fetchone():
                raise ValueError(f"Ya existe la orden de compra {numero}.")
            for l in lineas:
                p = conn.execute("SELECT activo FROM productos WHERE id = ?", (l["producto_id"],)).fetchone()
                if p is None or not p["activo"]:
                    raise ValueError("Hay un producto inexistente o inactivo en el detalle.")
            cur = conn.execute(
                """INSERT INTO ordenes_compra (numero_oc, proveedor, fecha_emision, fecha_entrega_estimada,
                       monto_total, moneda, estado, ruta_carpeta, observaciones, creado_por)
                   VALUES (?, ?, ?, ?, ?, ?, 'Abierto', ?, ?, ?)""",
                (numero, proveedor, fecha_emision.isoformat(),
                 fecha_entrega.isoformat() if fecha_entrega else None, monto, moneda,
                 documentos.a_relativa(carpeta), (observaciones or "").strip() or None, usuario["id"]))
            oc_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO oc_detalle (orden_compra_id, producto_id, cantidad_pedida, precio_unitario) "
                "VALUES (?, ?, ?, ?)", [(oc_id, l["producto_id"], l["cantidad"], l["precio"]) for l in lineas])
            documentos.crear_carpeta(carpeta)   # si falla, se revierte toda la transacción
            return oc_id
    except sqlite3.IntegrityError as e:
        raise ValueError(f"No se pudo crear la OC por una restricción de datos: {e}") from None


def actualizar_oc(usuario: dict, *, oc_id: int, proveedor: str, fecha_entrega: Optional[date],
                  observaciones: str, monto_manual: float) -> None:
    documentos.exigir_admin(usuario)
    proveedor = _limpiar(proveedor)
    if not proveedor:
        raise ValueError("El proveedor es obligatorio.")
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        oc = _oc_abierta(conn, oc_id)
        if fecha_entrega and fecha_entrega.isoformat() < oc["fecha_emision"]:
            raise ValueError("La entrega estimada no puede ser anterior a la fecha de emisión.")
        if proveedor != oc["proveedor"] and conn.execute(
                "SELECT 1 FROM facturas WHERE orden_compra_id = ?", (oc_id,)).fetchone():
            raise ValueError("No se puede cambiar el proveedor: la OC ya tiene facturas vinculadas.")
        tiene_lineas = conn.execute("SELECT 1 FROM oc_detalle WHERE orden_compra_id = ?", (oc_id,)).fetchone()
        monto = oc["monto_total"] if tiene_lineas else round(float(monto_manual or 0), 2)
        if not monto >= 0:
            raise ValueError("El monto no puede ser negativo.")
        conn.execute(
            "UPDATE ordenes_compra SET proveedor = ?, fecha_entrega_estimada = ?, observaciones = ?, "
            "monto_total = ? WHERE id = ?",
            (proveedor, fecha_entrega.isoformat() if fecha_entrega else None,
             (observaciones or "").strip() or None, monto, oc_id))


def agregar_linea_oc(usuario: dict, *, oc_id: int, producto_id: int, cantidad: float, precio_unitario: float) -> None:
    documentos.exigir_admin(usuario)
    linea = _normalizar_lineas([{"producto_id": producto_id, "cantidad": cantidad, "precio": precio_unitario}])[0]
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _oc_abierta(conn, oc_id)
        p = conn.execute("SELECT activo FROM productos WHERE id = ?", (producto_id,)).fetchone()
        if p is None or not p["activo"]:
            raise ValueError("El producto no existe o está inactivo.")
        if conn.execute("SELECT 1 FROM oc_detalle WHERE orden_compra_id = ? AND producto_id = ?",
                        (oc_id, producto_id)).fetchone():
            raise ValueError("Ese producto ya está en la OC. Elimine la línea y vuelva a agregarla para cambiarla.")
        conn.execute("INSERT INTO oc_detalle (orden_compra_id, producto_id, cantidad_pedida, precio_unitario) "
                     "VALUES (?, ?, ?, ?)", (oc_id, producto_id, linea["cantidad"], linea["precio"]))
        _recalcular_monto(conn, oc_id)


def eliminar_linea_oc(usuario: dict, linea_id: int) -> None:
    documentos.exigir_admin(usuario)
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        linea = conn.execute("SELECT orden_compra_id, cantidad_recibida FROM oc_detalle WHERE id = ?",
                             (linea_id,)).fetchone()
        if linea is None:
            raise ValueError("La línea no existe.")
        _oc_abierta(conn, linea["orden_compra_id"])
        if linea["cantidad_recibida"] > 0:
            raise ValueError("No se puede eliminar una línea que ya tiene mercadería recibida.")
        conn.execute("DELETE FROM oc_detalle WHERE id = ?", (linea_id,))
        _recalcular_monto(conn, linea["orden_compra_id"])


def cambiar_estado_oc(usuario: dict, *, oc_id: int, nuevo_estado: str, forzar_con_pendientes: bool = False) -> None:
    documentos.exigir_admin(usuario)
    if nuevo_estado not in ESTADOS:
        raise ValueError("Estado inválido.")
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        oc = conn.execute("SELECT estado FROM ordenes_compra WHERE id = ?", (oc_id,)).fetchone()
        if oc is None:
            raise ValueError("La orden de compra no existe.")
        if oc["estado"] == nuevo_estado:
            raise ValueError(f"La OC ya está {'abierta' if nuevo_estado == 'Abierto' else 'cerrada'}.")
        if nuevo_estado == "Cerrado":
            pend = conn.execute("SELECT COALESCE(SUM(cantidad_pedida - cantidad_recibida), 0) "
                                "FROM oc_detalle WHERE orden_compra_id = ?", (oc_id,)).fetchone()[0]
            if round(pend, DECIMALES) > 0 and not forzar_con_pendientes:
                raise ValueError(f"Aún hay {pend:g} unidades pendientes de recibir. "
                                 "Marque 'Cerrar de todos modos' para confirmar.")
        conn.execute("UPDATE ordenes_compra SET estado = ? WHERE id = ?", (nuevo_estado, oc_id))


def eliminar_oc(usuario: dict, oc_id: int) -> None:
    """Elimina una OC solo si no tiene mercadería recibida ni facturas vinculadas."""
    documentos.exigir_admin(usuario)
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM ordenes_compra WHERE id = ?", (oc_id,)).fetchone() is None:
            raise ValueError("La orden de compra no existe.")
        if conn.execute("SELECT 1 FROM lotes_inventario WHERE orden_compra_id = ?", (oc_id,)).fetchone() or \
           conn.execute("SELECT 1 FROM oc_detalle WHERE orden_compra_id = ? AND cantidad_recibida > 0",
                        (oc_id,)).fetchone():
            raise ValueError("No se puede eliminar: la OC tiene mercadería recibida en el inventario.")
        if conn.execute("SELECT 1 FROM facturas WHERE orden_compra_id = ?", (oc_id,)).fetchone():
            raise ValueError("No se puede eliminar: la OC tiene facturas vinculadas. Elimínelas primero.")
        conn.execute("DELETE FROM documentos_adjuntos WHERE tipo_entidad = 'OC' AND entidad_id = ?", (oc_id,))
        conn.execute("DELETE FROM ordenes_compra WHERE id = ?", (oc_id,))   # las líneas caen en cascada


# ===========================================================================
# ESCRITURA: RECEPCIÓN DE MERCADERÍA (OC -> INVENTARIO)
# ===========================================================================
def recibir_mercaderia(usuario: dict, *, oc_id: int, linea_id: int, cantidad: float, numero_lote: str,
                       fecha_vencimiento: Optional[date], ubicacion: str, factura_id: Optional[int],
                       cerrar_si_completa: bool = True) -> dict[str, Any]:
    """
    Recibe 'cantidad' de una línea de OC: crea el lote en inventario, actualiza lo recibido
    de la línea, registra la ENTRADA en el kardex y (opcional) cierra la OC si queda completa.
    Todo ocurre en UNA transacción: o se registra todo o nada.
    """
    documentos.exigir_admin(usuario)
    numero_lote = (numero_lote or "").strip().upper()
    cantidad = round(float(cantidad or 0), DECIMALES)
    hoy = date.today()
    if not numero_lote:
        raise ValueError("Ingrese el número de lote.")
    if not cantidad > 0:
        raise ValueError("La cantidad debe ser mayor que cero.")
    if fecha_vencimiento is not None and fecha_vencimiento < hoy:
        raise ValueError("El lote ya está vencido; no se puede ingresar como stock disponible.")

    inv._asegurar_tabla_movimientos()
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        oc = _oc_abierta(conn, oc_id)
        linea = conn.execute(
            """SELECT d.id, d.producto_id, d.cantidad_pedida, d.cantidad_recibida, p.activo
               FROM oc_detalle d JOIN productos p ON p.id = d.producto_id
               WHERE d.id = ? AND d.orden_compra_id = ?""", (linea_id, oc_id)).fetchone()
        if linea is None:
            raise ValueError("La línea no pertenece a esta OC.")
        if not linea["activo"]:
            raise ValueError("El producto está inactivo; actívelo antes de recibir.")
        pendiente = round(linea["cantidad_pedida"] - linea["cantidad_recibida"], DECIMALES)
        if cantidad > pendiente:
            raise ValueError(f"La cantidad ({cantidad:g}) excede lo pendiente de la línea ({pendiente:g}).")
        if factura_id is not None:
            f = conn.execute("SELECT orden_compra_id FROM facturas WHERE id = ?", (factura_id,)).fetchone()
            if f is None or f["orden_compra_id"] != oc_id:
                raise ValueError("La factura seleccionada no está vinculada a esta OC.")
        if conn.execute("SELECT 1 FROM lotes_inventario WHERE producto_id = ? AND numero_lote = ?",
                        (linea["producto_id"], numero_lote)).fetchone():
            raise ValueError(f"Ya existe el lote {numero_lote} para este producto.")

        cur = conn.execute(
            """INSERT INTO lotes_inventario (producto_id, numero_lote, fecha_ingreso, fecha_vencimiento,
                   cantidad_inicial, cantidad_actual, orden_compra_id, factura_id, ubicacion)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (linea["producto_id"], numero_lote, hoy.isoformat(),
             fecha_vencimiento.isoformat() if fecha_vencimiento else None,
             cantidad, cantidad, oc_id, factura_id, (ubicacion or "").strip() or None))
        lote_id = cur.lastrowid
        conn.execute("UPDATE oc_detalle SET cantidad_recibida = ROUND(cantidad_recibida + ?, ?) WHERE id = ?",
                     (cantidad, DECIMALES, linea_id))
        inv._registrar_movimiento(
            conn, lote_id=lote_id, producto_id=linea["producto_id"], numero_lote=numero_lote, tipo="ENTRADA",
            cantidad=cantidad, motivo="Recepción de OC", referencia=oc["numero_oc"], usuario_id=usuario["id"])

        pend_total = conn.execute("SELECT COALESCE(SUM(cantidad_pedida - cantidad_recibida), 0) "
                                  "FROM oc_detalle WHERE orden_compra_id = ?", (oc_id,)).fetchone()[0]
        completa = round(pend_total, DECIMALES) <= 0
        cerrada = False
        if completa and cerrar_si_completa:
            conn.execute("UPDATE ordenes_compra SET estado = 'Cerrado' WHERE id = ?", (oc_id,))
            cerrada = True
        return {"lote_id": lote_id, "completa": completa, "cerrada": cerrada}


# ===========================================================================
# ESCRITURA: FACTURAS
# ===========================================================================
def crear_factura(usuario: dict, *, numero_factura: str, proveedor: str, oc_id: Optional[int],
                  fecha_emision: date, fecha_vencimiento_pago: Optional[date], monto_total: float,
                  moneda: str, observaciones: str) -> int:
    documentos.exigir_admin(usuario)
    numero = (numero_factura or "").strip().upper()
    proveedor = _limpiar(proveedor)
    monto = round(float(monto_total or 0), 2)
    if not numero:
        raise ValueError("Ingrese el número de factura.")
    if not proveedor:
        raise ValueError("Ingrese el proveedor.")
    if not monto > 0:
        raise ValueError("El monto debe ser mayor que cero.")
    if moneda not in MONEDAS:
        raise ValueError("Moneda inválida.")
    if fecha_vencimiento_pago and fecha_vencimiento_pago < fecha_emision:
        raise ValueError("El vencimiento de pago no puede ser anterior a la emisión.")

    carpeta = documentos.carpeta_por_defecto("FACTURA", numero, proveedor)
    try:
        with db.db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if oc_id is not None:
                oc = conn.execute("SELECT proveedor, moneda FROM ordenes_compra WHERE id = ?", (oc_id,)).fetchone()
                if oc is None:
                    raise ValueError("La orden de compra seleccionada no existe.")
                if oc["proveedor"].casefold() != proveedor.casefold():
                    raise ValueError(f"El proveedor no coincide con el de la OC ({oc['proveedor']}).")
                if oc["moneda"] != moneda:
                    raise ValueError(f"La moneda no coincide con la de la OC ({oc['moneda']}).")
                proveedor = oc["proveedor"]   # nombre canónico (evita variantes de mayúsculas)
            if conn.execute("SELECT 1 FROM facturas WHERE numero_factura = ? AND proveedor = ?",
                            (numero, proveedor)).fetchone():
                raise ValueError(f"Ya existe la factura {numero} del proveedor {proveedor}.")
            cur = conn.execute(
                """INSERT INTO facturas (numero_factura, proveedor, orden_compra_id, fecha_emision,
                       fecha_vencimiento_pago, monto_total, moneda, estado, ruta_documento, observaciones, creado_por)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'Abierto', ?, ?, ?)""",
                (numero, proveedor, oc_id, fecha_emision.isoformat(),
                 fecha_vencimiento_pago.isoformat() if fecha_vencimiento_pago else None, monto, moneda,
                 documentos.a_relativa(carpeta), (observaciones or "").strip() or None, usuario["id"]))
            factura_id = cur.lastrowid
            documentos.crear_carpeta(carpeta)
            return factura_id
    except sqlite3.IntegrityError as e:
        raise ValueError(f"No se pudo crear la factura por una restricción de datos: {e}") from None


def actualizar_factura(usuario: dict, *, factura_id: int, fecha_vencimiento_pago: Optional[date],
                       monto_total: float, observaciones: str) -> None:
    documentos.exigir_admin(usuario)
    monto = round(float(monto_total or 0), 2)
    if not monto > 0:
        raise ValueError("El monto debe ser mayor que cero.")
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        f = conn.execute("SELECT estado, fecha_emision FROM facturas WHERE id = ?", (factura_id,)).fetchone()
        if f is None:
            raise ValueError("La factura no existe.")
        if f["estado"] != "Abierto":
            raise ValueError("La factura está cerrada. Reábrala para modificarla.")
        if fecha_vencimiento_pago and fecha_vencimiento_pago.isoformat() < f["fecha_emision"]:
            raise ValueError("El vencimiento de pago no puede ser anterior a la emisión.")
        conn.execute("UPDATE facturas SET fecha_vencimiento_pago = ?, monto_total = ?, observaciones = ? WHERE id = ?",
                     (fecha_vencimiento_pago.isoformat() if fecha_vencimiento_pago else None, monto,
                      (observaciones or "").strip() or None, factura_id))


def cambiar_estado_factura(usuario: dict, *, factura_id: int, nuevo_estado: str) -> None:
    documentos.exigir_admin(usuario)
    if nuevo_estado not in ESTADOS:
        raise ValueError("Estado inválido.")
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        f = conn.execute("SELECT estado FROM facturas WHERE id = ?", (factura_id,)).fetchone()
        if f is None:
            raise ValueError("La factura no existe.")
        if f["estado"] == nuevo_estado:
            raise ValueError(f"La factura ya está {'abierta' if nuevo_estado == 'Abierto' else 'cerrada'}.")
        conn.execute("UPDATE facturas SET estado = ? WHERE id = ?", (nuevo_estado, factura_id))


def eliminar_factura(usuario: dict, factura_id: int) -> None:
    """Elimina una factura solo si ningún lote de inventario depende de ella."""
    documentos.exigir_admin(usuario)
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM facturas WHERE id = ?", (factura_id,)).fetchone() is None:
            raise ValueError("La factura no existe.")
        if conn.execute("SELECT 1 FROM lotes_inventario WHERE factura_id = ?", (factura_id,)).fetchone():
            raise ValueError("No se puede eliminar: hay lotes de inventario asociados a esta factura.")
        conn.execute("DELETE FROM documentos_adjuntos WHERE tipo_entidad = 'FACTURA' AND entidad_id = ?", (factura_id,))
        conn.execute("DELETE FROM facturas WHERE id = ?", (factura_id,))
