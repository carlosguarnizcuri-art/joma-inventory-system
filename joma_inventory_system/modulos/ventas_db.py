"""
modulos/ventas_db.py - Órdenes de Venta (OV) y Cuentas por Cobrar (FASE 4, sin código de interfaz)

ÓRDENES DE VENTA (espejo operativo de las Órdenes de Compra)
  * Correlativo automático por año: OV001-26, OV002-26... (reinicia cada año).
  * Totales con IGV (18 %): subtotal, IGV y total.
  * Estados: Abierta -> Cerrada (AUTOMÁTICO cuando el expediente digital está completo) | Anulada.
  * Cada OV tiene su carpeta nodriza data/documentos_ov/<OV>/ (la gestiona expediente_ov.py).
  * El PDF corporativo de la OV se genera automáticamente al crearla o modificarla.

CUENTAS POR COBRAR (facturas de venta vinculadas a una OV y a su cliente)
  * Estados: Abierto (por cobrar) / Cerrado (cobrada). La suma facturada no puede superar el total de la OV.
  * Al registrar la factura se genera su comprobante y se deposita en la carpeta de la OV.

Permisos: escriben OV los roles con escritura en «Órdenes de Venta» (Admin, Comercial) y facturas de
venta los de «Cuentas por Cobrar» (Admin, Comercial). Se valida contra la BD en cada función.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd

import database as db
from modulos import clientes_db, identidad, permisos
from modulos import compras_db as cdb
from modulos import inventario as inv

OV_DIR = db.BASE_DIR / "data" / "documentos_ov"
OV_PREFIJO = "OV"
IGV_PORC = 18.0
_OV_RE = re.compile(rf"^{OV_PREFIJO}(\d+)-(\d{{2}})$")
MONEDAS = ("PEN", "USD")

TIPOS_DOCUMENTO = {
    "COTIZACION": "Cotización",
    "ORDEN_VENTA": "Orden de Venta (sistema)",
    "OC_CLIENTE": "OC del cliente",
    "GUIA_REMISION": "Guía de Remisión",
    "FACTURA": "Factura",
    "OTRO": "Otro documento",
}
# Checklist de cierre documental automático
REQUERIDOS = ("OC_CLIENTE", "GUIA_REMISION", "FACTURA")

SQL_VENTAS = """
CREATE TABLE IF NOT EXISTS ordenes_venta (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_ov               TEXT    NOT NULL UNIQUE,
    cliente_id              INTEGER NOT NULL REFERENCES clientes(id),
    oc_cliente              TEXT,
    fecha_emision           TEXT    NOT NULL,
    fecha_entrega_estimada  TEXT,
    moneda                  TEXT    NOT NULL DEFAULT 'PEN' CHECK (moneda IN ('PEN', 'USD')),
    igv_porcentaje          REAL    NOT NULL DEFAULT 18 CHECK (igv_porcentaje >= 0),
    monto_subtotal          REAL    NOT NULL DEFAULT 0 CHECK (monto_subtotal >= 0),
    monto_igv               REAL    NOT NULL DEFAULT 0 CHECK (monto_igv >= 0),
    monto_total             REAL    NOT NULL DEFAULT 0 CHECK (monto_total >= 0),
    estado                  TEXT    NOT NULL DEFAULT 'Abierta' CHECK (estado IN ('Abierta', 'Cerrada', 'Anulada')),
    condiciones             TEXT,
    observaciones           TEXT,
    ruta_carpeta            TEXT,
    creado_por              INTEGER REFERENCES usuarios(id),
    creado_en               TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    cerrada_en              TEXT
);
CREATE INDEX IF NOT EXISTS idx_ov_estado  ON ordenes_venta (estado);
CREATE INDEX IF NOT EXISTS idx_ov_cliente ON ordenes_venta (cliente_id);

CREATE TABLE IF NOT EXISTS ov_detalle (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ov_id               INTEGER NOT NULL REFERENCES ordenes_venta(id) ON DELETE CASCADE,
    producto_id         INTEGER NOT NULL REFERENCES productos(id)      ON DELETE RESTRICT,
    cantidad            REAL    NOT NULL CHECK (cantidad > 0),
    cantidad_despachada REAL    NOT NULL DEFAULT 0 CHECK (cantidad_despachada >= 0),
    precio_unitario     REAL    NOT NULL DEFAULT 0 CHECK (precio_unitario >= 0),
    UNIQUE (ov_id, producto_id)
);

CREATE TABLE IF NOT EXISTS ov_documentos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ov_id           INTEGER NOT NULL REFERENCES ordenes_venta(id) ON DELETE CASCADE,
    tipo_documento  TEXT    NOT NULL CHECK (tipo_documento IN
                        ('COTIZACION', 'ORDEN_VENTA', 'OC_CLIENTE', 'GUIA_REMISION', 'FACTURA', 'OTRO')),
    nombre          TEXT    NOT NULL,
    ruta            TEXT    NOT NULL,           -- relativa a data/documentos_ov
    firmado         INTEGER NOT NULL DEFAULT 0 CHECK (firmado IN (0, 1)),
    origen          TEXT    NOT NULL DEFAULT 'MANUAL' CHECK (origen IN ('MANUAL', 'AUTO')),
    referencia      TEXT,                       -- p. ej. N° de factura o de despacho que originó el documento
    descripcion     TEXT,
    tamano_bytes    INTEGER,
    subido_por      INTEGER REFERENCES usuarios(id),
    subido_en       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_ovdoc_ov ON ov_documentos (ov_id, tipo_documento);

CREATE TABLE IF NOT EXISTS facturas_venta (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_factura          TEXT    NOT NULL UNIQUE,
    ov_id                   INTEGER NOT NULL REFERENCES ordenes_venta(id),
    cliente_id              INTEGER NOT NULL REFERENCES clientes(id),
    fecha_emision           TEXT    NOT NULL,
    fecha_vencimiento_pago  TEXT,
    moneda                  TEXT    NOT NULL CHECK (moneda IN ('PEN', 'USD')),
    monto_subtotal          REAL    NOT NULL CHECK (monto_subtotal >= 0),
    monto_igv               REAL    NOT NULL CHECK (monto_igv >= 0),
    monto_total             REAL    NOT NULL CHECK (monto_total > 0),
    estado                  TEXT    NOT NULL DEFAULT 'Abierto' CHECK (estado IN ('Abierto', 'Cerrado')),
    fecha_cobro             TEXT,
    observaciones           TEXT,
    creado_por              INTEGER REFERENCES usuarios(id),
    creado_en               TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_fv_estado ON facturas_venta (estado);
CREATE INDEX IF NOT EXISTS idx_fv_ov     ON facturas_venta (ov_id);
"""


def asegurar_esquema() -> None:
    """Crea las tablas de ventas (idempotente) y la carpeta raíz de expedientes."""
    clientes_db.asegurar_esquema()
    OV_DIR.mkdir(parents=True, exist_ok=True)
    with db.db_session() as conn:
        conn.executescript(SQL_VENTAS)


# ===========================================================================
# CORRELATIVO Y TOTALES
# ===========================================================================
def siguiente_numero_ov(conn: sqlite3.Connection, hoy: Optional[date] = None) -> str:
    """OV001-26: correlativo de 3 dígitos + año de 2 dígitos. La secuencia reinicia cada año."""
    yy = f"{(hoy or date.today()).year % 100:02d}"
    mayor = 0
    for f in conn.execute("SELECT numero_ov FROM ordenes_venta WHERE numero_ov LIKE ?", (OV_PREFIJO + "%",)):
        m = _OV_RE.match(f["numero_ov"])
        if m and m.group(2) == yy:
            mayor = max(mayor, int(m.group(1)))
    return f"{OV_PREFIJO}{mayor + 1:03d}-{yy}"


def vista_previa_numero_ov() -> str:
    conn = db.get_connection()
    try:
        return siguiente_numero_ov(conn)
    finally:
        conn.close()


def calcular_totales(subtotal: float, igv_porc: float = IGV_PORC) -> tuple[float, float, float]:
    """(subtotal, IGV, total) redondeados a 2 decimales."""
    subtotal = round(float(subtotal), 2)
    igv = round(subtotal * igv_porc / 100, 2)
    return subtotal, igv, round(subtotal + igv, 2)


# ===========================================================================
# LECTURA: ÓRDENES DE VENTA
# ===========================================================================
def estado_ov_visual(estado: str, entrega: Any, hoy_iso: str) -> str:
    if estado == "Cerrada":
        return "✅ Cerrada"
    if estado == "Anulada":
        return "⛔ Anulada"
    if isinstance(entrega, str) and entrega < hoy_iso:
        return "🔴 Atrasada"
    return "🟡 Abierta"


def listar_ov() -> pd.DataFrame:
    df = db.fetch_df(
        """SELECT ov.id, ov.numero_ov, ov.fecha_emision, ov.fecha_entrega_estimada, ov.estado, ov.oc_cliente,
                  ov.moneda, ov.monto_total, c.razon_social AS cliente,
                  c.tipo_documento || ' ' || c.numero_documento AS documento,
                  COALESCE((SELECT SUM(cantidad) FROM ov_detalle d WHERE d.ov_id = ov.id), 0) AS pedido,
                  COALESCE((SELECT SUM(cantidad_despachada) FROM ov_detalle d WHERE d.ov_id = ov.id), 0) AS despachado,
                  (SELECT COUNT(*) FROM ov_documentos x WHERE x.ov_id = ov.id) AS n_docs,
                  (SELECT COUNT(*) FROM ov_documentos x WHERE x.ov_id = ov.id AND x.tipo_documento = 'OC_CLIENTE') AS n_oc,
                  (SELECT COUNT(*) FROM ov_documentos x WHERE x.ov_id = ov.id AND x.tipo_documento = 'GUIA_REMISION') AS n_guia,
                  (SELECT COUNT(*) FROM ov_documentos x WHERE x.ov_id = ov.id AND x.tipo_documento = 'FACTURA'
                      AND x.firmado = 1) AS n_factura
           FROM ordenes_venta ov JOIN clientes c ON c.id = ov.cliente_id
           ORDER BY ov.id DESC""")
    hoy = date.today().isoformat()
    df["avance"] = (df["despachado"] / df["pedido"].where(df["pedido"] > 0) * 100).round(1)
    df["estado_visual"] = [estado_ov_visual(e, f, hoy) for e, f in zip(df["estado"], df["fecha_entrega_estimada"])]

    def faltantes(r) -> str:
        falta = [n for n, ok in (("OC del cliente", r.n_oc > 0), ("Guía de Remisión", r.n_guia > 0),
                                 ("Factura firmada", r.n_factura > 0)) if not ok]
        return ", ".join(falta)

    df["faltantes"] = [faltantes(r) for r in df.itertuples()]
    return df


def obtener_ov(ov_id: int) -> Optional[sqlite3.Row]:
    return db.fetch_one(
        """SELECT ov.*, c.razon_social AS cliente_nombre, c.tipo_documento, c.numero_documento,
                  c.direccion AS cliente_direccion, c.telefono AS cliente_telefono, c.email AS cliente_email
           FROM ordenes_venta ov JOIN clientes c ON c.id = ov.cliente_id WHERE ov.id = ?""", (ov_id,))


def lineas_ov(ov_id: int) -> pd.DataFrame:
    df = db.fetch_df(
        """SELECT d.id, d.producto_id, p.codigo, p.nombre AS producto, p.unidad_medida AS unidad,
                  d.cantidad, d.cantidad_despachada, d.precio_unitario
           FROM ov_detalle d JOIN productos p ON p.id = d.producto_id
           WHERE d.ov_id = ? ORDER BY d.id""", (ov_id,))
    df["pendiente"] = (df["cantidad"] - df["cantidad_despachada"]).round(inv.DECIMALES)
    df["subtotal"] = (df["cantidad"] * df["precio_unitario"]).round(2)
    return df


def ov_para_despacho(texto: str = "") -> pd.DataFrame:
    """OV con mercadería pendiente de despachar, filtradas por nombre de cliente, N° de OV, documento u OC del cliente."""
    df = db.fetch_df(
        """SELECT ov.id, ov.numero_ov, ov.oc_cliente, ov.estado, ov.fecha_emision, c.razon_social AS cliente,
                  c.tipo_documento || ' ' || c.numero_documento AS documento,
                  SUM(d.cantidad - d.cantidad_despachada) AS pendiente
           FROM ordenes_venta ov JOIN clientes c ON c.id = ov.cliente_id JOIN ov_detalle d ON d.ov_id = ov.id
           WHERE ov.estado <> 'Anulada'
           GROUP BY ov.id HAVING pendiente > 0
           ORDER BY ov.id DESC""")
    return inv._aplicar_busqueda(df, texto, ["numero_ov", "cliente", "documento", "oc_cliente"])


def resumen_ov() -> dict[str, float]:
    df = listar_ov()
    return {"abiertas": int((df["estado"] == "Abierta").sum()),
            "cerradas": int((df["estado"] == "Cerrada").sum()),
            "con_faltantes": int(((df["estado"] == "Abierta") & (df["faltantes"] != "")).sum()),
            "por_despachar": float((df.loc[df["estado"] != "Anulada", "pedido"] -
                                    df.loc[df["estado"] != "Anulada", "despachado"]).sum())}


# ===========================================================================
# ESCRITURA: ÓRDENES DE VENTA
# ===========================================================================
def _normalizar_lineas(lineas: list[dict]) -> list[dict]:
    vistos: set[int] = set()
    out = []
    for i, l in enumerate(lineas, start=1):
        cantidad = round(float(l.get("cantidad") or 0), inv.DECIMALES)
        precio = round(float(l.get("precio") or 0), 4)
        if not cantidad > 0:
            raise ValueError(f"Línea {i}: la cantidad debe ser mayor que cero.")
        if not precio >= 0:
            raise ValueError(f"Línea {i}: el precio no puede ser negativo.")
        pid = int(l["producto_id"])
        if pid in vistos:
            raise ValueError(f"Línea {i}: el producto está repetido en la OV.")
        vistos.add(pid)
        out.append({"producto_id": pid, "cantidad": cantidad, "precio": precio})
    if not out:
        raise ValueError("Agregue al menos un producto a la orden de venta.")
    return out


def _totales_de_lineas(lineas: list[dict], igv_porc: float = IGV_PORC) -> tuple[float, float, float]:
    return calcular_totales(sum(round(l["cantidad"] * l["precio"], 2) for l in lineas), igv_porc)


def _regenerar_pdf(ov_id: int, usuario: Optional[dict]) -> None:
    """El PDF corporativo se rehace de forma automática; un fallo no debe impedir la operación comercial."""
    try:
        from modulos import expediente_ov            # import diferido (expediente_ov depende de este módulo)
        expediente_ov.generar_pdf_orden_venta(ov_id, usuario)
    except Exception:
        pass


def crear_ov(usuario: dict, *, cliente_id: int, oc_cliente: str, fecha_emision: date, fecha_entrega: Optional[date],
             moneda: str, condiciones: str, observaciones: str, lineas: list[dict]) -> int:
    """Crea la OV con correlativo automático, su carpeta nodriza y su PDF corporativo."""
    permisos.exigir_escritura(usuario, "ov")
    if moneda not in MONEDAS:
        raise ValueError("Moneda inválida.")
    if fecha_entrega and fecha_entrega < fecha_emision:
        raise ValueError("La entrega estimada no puede ser anterior a la fecha de emisión.")
    oc_cliente = " ".join((oc_cliente or "").split())
    if len(oc_cliente) > 60:
        raise ValueError("El N° de OC del cliente no puede superar los 60 caracteres.")
    lineas = _normalizar_lineas(lineas)
    subtotal, igv, total = _totales_de_lineas(lineas)

    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        clientes_db.instantanea_cliente(conn, cliente_id)          # valida que el cliente exista y esté activo
        for l in lineas:
            p = conn.execute("SELECT activo FROM productos WHERE id = ?", (l["producto_id"],)).fetchone()
            if p is None or not p["activo"]:
                raise ValueError("Hay un producto inexistente o inactivo en el detalle.")
        numero = siguiente_numero_ov(conn, fecha_emision)
        carpeta = OV_DIR / numero
        cur = conn.execute(
            """INSERT INTO ordenes_venta (numero_ov, cliente_id, oc_cliente, fecha_emision, fecha_entrega_estimada,
                   moneda, igv_porcentaje, monto_subtotal, monto_igv, monto_total, condiciones, observaciones,
                   ruta_carpeta, creado_por)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (numero, cliente_id, oc_cliente or None, fecha_emision.isoformat(),
             fecha_entrega.isoformat() if fecha_entrega else None, moneda, IGV_PORC, subtotal, igv, total,
             (condiciones or "").strip() or None, (observaciones or "").strip() or None, numero, usuario["id"]))
        ov_id = cur.lastrowid
        conn.executemany("INSERT INTO ov_detalle (ov_id, producto_id, cantidad, precio_unitario) VALUES (?, ?, ?, ?)",
                         [(ov_id, l["producto_id"], l["cantidad"], l["precio"]) for l in lineas])
        try:                                                        # carpeta nodriza: si falla, se revierte todo
            (carpeta).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ValueError(f"No se pudo crear la carpeta del expediente '{carpeta}': {e}") from None
    _regenerar_pdf(ov_id, usuario)
    return ov_id


def _ov_editable(conn: sqlite3.Connection, ov_id: int) -> sqlite3.Row:
    ov = conn.execute("SELECT * FROM ordenes_venta WHERE id = ?", (ov_id,)).fetchone()
    if ov is None:
        raise ValueError("La orden de venta no existe.")
    if ov["estado"] == "Anulada":
        raise ValueError("La orden de venta está anulada.")
    return ov


def reemplazar_lineas_ov(usuario: dict, *, ov_id: int, lineas: list[dict]) -> None:
    """Cambia los productos de la OV (solo si aún no se despachó nada) y recalcula totales y PDF."""
    permisos.exigir_escritura(usuario, "ov")
    lineas = _normalizar_lineas(lineas)
    subtotal, igv, total = _totales_de_lineas(lineas)
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ov = _ov_editable(conn, ov_id)
        if conn.execute("SELECT 1 FROM ov_detalle WHERE ov_id = ? AND cantidad_despachada > 0", (ov_id,)).fetchone():
            raise ValueError("La OV ya tiene mercadería despachada: sus productos no se pueden cambiar.")
        if conn.execute("SELECT 1 FROM facturas_venta WHERE ov_id = ?", (ov_id,)).fetchone():
            raise ValueError("La OV ya tiene facturas emitidas: sus productos no se pueden cambiar.")
        for l in lineas:
            p = conn.execute("SELECT activo FROM productos WHERE id = ?", (l["producto_id"],)).fetchone()
            if p is None or not p["activo"]:
                raise ValueError("Hay un producto inexistente o inactivo en el detalle.")
        conn.execute("DELETE FROM ov_detalle WHERE ov_id = ?", (ov_id,))
        conn.executemany("INSERT INTO ov_detalle (ov_id, producto_id, cantidad, precio_unitario) VALUES (?, ?, ?, ?)",
                         [(ov_id, l["producto_id"], l["cantidad"], l["precio"]) for l in lineas])
        conn.execute("UPDATE ordenes_venta SET monto_subtotal = ?, monto_igv = ?, monto_total = ? WHERE id = ?",
                     (subtotal, igv, total, ov_id))
    _regenerar_pdf(ov_id, usuario)


def actualizar_cabecera_ov(usuario: dict, *, ov_id: int, oc_cliente: str, fecha_entrega: Optional[date],
                           condiciones: str, observaciones: str) -> None:
    permisos.exigir_escritura(usuario, "ov")
    oc_cliente = " ".join((oc_cliente or "").split())
    if len(oc_cliente) > 60:
        raise ValueError("El N° de OC del cliente no puede superar los 60 caracteres.")
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ov = _ov_editable(conn, ov_id)
        if fecha_entrega and fecha_entrega.isoformat() < ov["fecha_emision"]:
            raise ValueError("La entrega estimada no puede ser anterior a la fecha de emisión.")
        conn.execute("UPDATE ordenes_venta SET oc_cliente = ?, fecha_entrega_estimada = ?, condiciones = ?, "
                     "observaciones = ? WHERE id = ?",
                     (oc_cliente or None, fecha_entrega.isoformat() if fecha_entrega else None,
                      (condiciones or "").strip() or None, (observaciones or "").strip() or None, ov_id))
        if _tabla_existe(conn, "despachos") and "ov_id" in {r["name"] for r in conn.execute("PRAGMA table_info(despachos)")}:
            conn.execute("UPDATE despachos SET oc_cliente = ? WHERE ov_id = ? AND estado = 'Borrador'",
                         (oc_cliente or None, ov_id))      # los borradores de despacho heredan la OC del cliente
    _regenerar_pdf(ov_id, usuario)


def anular_ov(usuario: dict, ov_id: int) -> None:
    """Anula la OV (no se elimina: queda el registro). Solo si no tiene despachos ni facturas."""
    permisos.exigir_escritura(usuario, "ov")
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ov = _ov_editable(conn, ov_id)
        if conn.execute("SELECT 1 FROM ov_detalle WHERE ov_id = ? AND cantidad_despachada > 0", (ov_id,)).fetchone():
            raise ValueError("No se puede anular: la OV ya tiene mercadería despachada.")
        if conn.execute("SELECT 1 FROM facturas_venta WHERE ov_id = ?", (ov_id,)).fetchone():
            raise ValueError("No se puede anular: la OV ya tiene facturas emitidas.")
        if _tabla_existe(conn, "despachos") and conn.execute(
                "SELECT 1 FROM despachos WHERE ov_id = ? AND estado = 'Borrador'", (ov_id,)).fetchone():
            raise ValueError("No se puede anular: hay despachos en borrador vinculados. Anúlelos primero.")
        conn.execute("UPDATE ordenes_venta SET estado = 'Anulada', cerrada_en = NULL WHERE id = ?", (ov_id,))


def _tabla_existe(conn: sqlite3.Connection, nombre: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nombre,)).fetchone() is not None


# ===========================================================================
# CUENTAS POR COBRAR (facturas de venta)
# ===========================================================================
def listar_facturas_venta() -> pd.DataFrame:
    df = db.fetch_df(
        """SELECT f.id, f.numero_factura, f.ov_id, ov.numero_ov, c.razon_social AS cliente,
                  c.tipo_documento || ' ' || c.numero_documento AS documento, f.fecha_emision,
                  f.fecha_vencimiento_pago, f.fecha_cobro, f.moneda, f.monto_subtotal, f.monto_igv, f.monto_total,
                  f.estado, f.observaciones
           FROM facturas_venta f JOIN ordenes_venta ov ON ov.id = f.ov_id JOIN clientes c ON c.id = f.cliente_id
           ORDER BY f.fecha_emision DESC, f.id DESC""")
    hoy = date.today()
    limite = (hoy + timedelta(days=cdb.DIAS_FACTURA_POR_VENCER)).isoformat()
    df["estado_visual"] = [cdb.estado_factura_visual(e, v, hoy.isoformat(), limite)
                           for e, v in zip(df["estado"], df["fecha_vencimiento_pago"])]
    df["dias_para_cobro"] = (pd.to_datetime(df["fecha_vencimiento_pago"]) - pd.Timestamp(hoy)).dt.days
    return df


def obtener_factura_venta(factura_id: int) -> Optional[sqlite3.Row]:
    return db.fetch_one(
        """SELECT f.*, ov.numero_ov, c.razon_social AS cliente, c.tipo_documento, c.numero_documento,
                  c.direccion AS cliente_direccion
           FROM facturas_venta f JOIN ordenes_venta ov ON ov.id = f.ov_id JOIN clientes c ON c.id = f.cliente_id
           WHERE f.id = ?""", (factura_id,))


def facturado_de_ov(ov_id: int) -> tuple[float, float, float]:
    """(total de la OV, total facturado, pendiente por facturar)."""
    ov = db.fetch_one("SELECT monto_total FROM ordenes_venta WHERE id = ?", (ov_id,))
    if ov is None:
        raise ValueError("La orden de venta no existe.")
    fact = db.fetch_one("SELECT COALESCE(SUM(monto_total), 0) FROM facturas_venta WHERE ov_id = ?", (ov_id,))[0]
    return float(ov["monto_total"]), float(fact), round(float(ov["monto_total"]) - float(fact), 2)


def ov_facturables(texto: str = "") -> pd.DataFrame:
    """OV no anuladas con saldo pendiente por facturar."""
    df = db.fetch_df(
        """SELECT ov.id, ov.numero_ov, ov.oc_cliente, ov.moneda, ov.monto_total, c.razon_social AS cliente,
                  c.tipo_documento || ' ' || c.numero_documento AS documento,
                  ROUND(ov.monto_total - COALESCE((SELECT SUM(f.monto_total) FROM facturas_venta f WHERE f.ov_id = ov.id), 0), 2)
                      AS por_facturar
           FROM ordenes_venta ov JOIN clientes c ON c.id = ov.cliente_id
           WHERE ov.estado <> 'Anulada' ORDER BY ov.id DESC""")
    df = df[df["por_facturar"] > 0.005]
    return inv._aplicar_busqueda(df, texto, ["numero_ov", "cliente", "documento", "oc_cliente"])


def resumen_cxc() -> dict[str, float]:
    hoy = date.today().isoformat()
    limite = (date.today() + timedelta(days=cdb.DIAS_FACTURA_POR_VENCER)).isoformat()

    def esc(q: str, p: tuple = ()) -> float:
        fila = db.fetch_one(q, p)
        return fila[0] if fila and fila[0] is not None else 0

    return {
        "abiertas": esc("SELECT COUNT(*) FROM facturas_venta WHERE estado = 'Abierto'"),
        "vencidas": esc("SELECT COUNT(*) FROM facturas_venta WHERE estado = 'Abierto' AND fecha_vencimiento_pago < ?", (hoy,)),
        "por_vencer": esc("SELECT COUNT(*) FROM facturas_venta WHERE estado = 'Abierto' "
                          "AND fecha_vencimiento_pago BETWEEN ? AND ?", (hoy, limite)),
        "por_cobrar_pen": esc("SELECT SUM(monto_total) FROM facturas_venta WHERE estado = 'Abierto' AND moneda = 'PEN'"),
        "por_cobrar_usd": esc("SELECT SUM(monto_total) FROM facturas_venta WHERE estado = 'Abierto' AND moneda = 'USD'"),
    }


def crear_factura_venta(usuario: dict, *, ov_id: int, numero_factura: str, fecha_emision: date,
                        fecha_vencimiento_pago: Optional[date], monto_total: float, observaciones: str) -> int:
    """Registra una factura de venta (total con IGV) contra una OV y deposita su comprobante en el expediente."""
    permisos.exigir_escritura(usuario, "cxc")
    numero = identidad.normalizar_comprobante(numero_factura, "factura")
    monto = round(float(monto_total or 0), 2)
    if not monto > 0:
        raise ValueError("El monto de la factura debe ser mayor que cero.")
    if fecha_vencimiento_pago and fecha_vencimiento_pago < fecha_emision:
        raise ValueError("El vencimiento de cobro no puede ser anterior a la emisión.")
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ov = _ov_editable(conn, ov_id)
        if conn.execute("SELECT 1 FROM facturas_venta WHERE numero_factura = ?", (numero,)).fetchone():
            raise ValueError(f"Ya existe la factura {numero}.")
        facturado = conn.execute("SELECT COALESCE(SUM(monto_total), 0) FROM facturas_venta WHERE ov_id = ?",
                                 (ov_id,)).fetchone()[0]
        pendiente = round(ov["monto_total"] - facturado, 2)
        if monto > pendiente + 0.005:
            raise ValueError(f"El monto supera lo pendiente por facturar de la OV ({ov['moneda']} {pendiente:,.2f}).")
        subtotal = round(monto / (1 + ov["igv_porcentaje"] / 100), 2)
        cur = conn.execute(
            """INSERT INTO facturas_venta (numero_factura, ov_id, cliente_id, fecha_emision, fecha_vencimiento_pago,
                   moneda, monto_subtotal, monto_igv, monto_total, observaciones, creado_por)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (numero, ov_id, ov["cliente_id"], fecha_emision.isoformat(),
             fecha_vencimiento_pago.isoformat() if fecha_vencimiento_pago else None, ov["moneda"], subtotal,
             round(monto - subtotal, 2), monto, (observaciones or "").strip() or None, usuario["id"]))
        factura_id = cur.lastrowid
    try:                                                            # autoguardado en la carpeta de la OV
        from modulos import expediente_ov
        expediente_ov.generar_comprobante_factura(factura_id, usuario)
    except Exception:
        pass
    return factura_id


def actualizar_factura_venta(usuario: dict, *, factura_id: int, fecha_vencimiento_pago: Optional[date],
                             observaciones: str) -> None:
    permisos.exigir_escritura(usuario, "cxc")
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        f = conn.execute("SELECT estado, fecha_emision FROM facturas_venta WHERE id = ?", (factura_id,)).fetchone()
        if f is None:
            raise ValueError("La factura no existe.")
        if f["estado"] != "Abierto":
            raise ValueError("La factura está cobrada. Reábrala para modificarla.")
        if fecha_vencimiento_pago and fecha_vencimiento_pago.isoformat() < f["fecha_emision"]:
            raise ValueError("El vencimiento de cobro no puede ser anterior a la emisión.")
        conn.execute("UPDATE facturas_venta SET fecha_vencimiento_pago = ?, observaciones = ? WHERE id = ?",
                     (fecha_vencimiento_pago.isoformat() if fecha_vencimiento_pago else None,
                      (observaciones or "").strip() or None, factura_id))


def cambiar_estado_factura_venta(usuario: dict, *, factura_id: int, nuevo_estado: str,
                                 fecha_cobro: Optional[date] = None) -> None:
    """Abierto = por cobrar · Cerrado = cobrada."""
    permisos.exigir_escritura(usuario, "cxc")
    if nuevo_estado not in ("Abierto", "Cerrado"):
        raise ValueError("Estado inválido.")
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        f = conn.execute("SELECT estado, fecha_emision FROM facturas_venta WHERE id = ?", (factura_id,)).fetchone()
        if f is None:
            raise ValueError("La factura no existe.")
        if f["estado"] == nuevo_estado:
            raise ValueError(f"La factura ya está {'por cobrar' if nuevo_estado == 'Abierto' else 'cobrada'}.")
        if nuevo_estado == "Cerrado":
            cobro = fecha_cobro or date.today()
            if cobro.isoformat() < f["fecha_emision"]:
                raise ValueError("La fecha de cobro no puede ser anterior a la emisión.")
            conn.execute("UPDATE facturas_venta SET estado = 'Cerrado', fecha_cobro = ? WHERE id = ?",
                         (cobro.isoformat(), factura_id))
        else:
            conn.execute("UPDATE facturas_venta SET estado = 'Abierto', fecha_cobro = NULL WHERE id = ?", (factura_id,))


def eliminar_factura_venta(usuario: dict, factura_id: int) -> None:
    """Corrige un registro equivocado: solo facturas por cobrar. Retira también el comprobante automático."""
    permisos.exigir_escritura(usuario, "cxc")
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        f = conn.execute("SELECT numero_factura, ov_id, estado FROM facturas_venta WHERE id = ?", (factura_id,)).fetchone()
        if f is None:
            raise ValueError("La factura no existe.")
        if f["estado"] != "Abierto":
            raise ValueError("Una factura cobrada no se elimina; reábrala primero si necesita corregirla.")
        conn.execute("DELETE FROM facturas_venta WHERE id = ?", (factura_id,))
        ov_id, numero = f["ov_id"], f["numero_factura"]
    try:
        from modulos import expediente_ov
        expediente_ov.retirar_documentos_automaticos(ov_id, "FACTURA", numero)
    except Exception:
        pass
