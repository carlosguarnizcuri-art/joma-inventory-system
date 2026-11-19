"""
modulos/despachos_db.py - Despachos a clientes contra una Orden de Venta (FASE 4, sin interfaz)

REGLA: toda salida física por venta se registra CONTRA UNA OV. Un despacho nace de una OV (el pedido se
autocompleta con lo pendiente), genera el Pre-picking con FEFO y, al confirmarse, en UNA transacción:
  descuenta el stock por lotes (FEFO) · escribe el kardex con OV, cliente y guía · suma lo despachado en la OV.
Después deposita el Packing List en el expediente de la OV. Flujo: Borrador -> Despachado | Anulado.
Numeración DES-00001. Escriben los roles con permiso en «Despachos» (Admin, Comercial, Almacén).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from typing import Optional

import pandas as pd

import database as db
from modulos import clientes_db, despachos_docs, identidad, permisos, ventas_db
from modulos import inventario as inv

DES_PREFIJO, DES_ANCHO = "DES-", 5
_DES_RE = re.compile(rf"^{re.escape(DES_PREFIJO)}(\d+)$")

SQL_DESPACHOS = """
CREATE TABLE IF NOT EXISTS despachos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_despacho  TEXT    NOT NULL UNIQUE,
    cliente_id       INTEGER NOT NULL REFERENCES clientes(id),
    oc_cliente       TEXT,
    fecha            TEXT    NOT NULL,
    estado           TEXT    NOT NULL DEFAULT 'Borrador' CHECK (estado IN ('Borrador', 'Despachado', 'Anulado')),
    guia_remision    TEXT,
    observaciones    TEXT,
    creado_por       INTEGER REFERENCES usuarios(id),
    creado_en        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    despachado_en    TEXT
);
CREATE TABLE IF NOT EXISTS despacho_detalle (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    despacho_id  INTEGER NOT NULL REFERENCES despachos(id)  ON DELETE CASCADE,
    producto_id  INTEGER NOT NULL REFERENCES productos(id)  ON DELETE RESTRICT,
    cantidad     REAL    NOT NULL CHECK (cantidad > 0),
    UNIQUE (despacho_id, producto_id)
);
CREATE INDEX IF NOT EXISTS idx_despachos_estado ON despachos (estado);
"""


def asegurar_esquema() -> None:
    """Crea las tablas y agrega (sin perder datos) la columna ov_id a despachos existentes."""
    ventas_db.asegurar_esquema()          # la FK ov_id -> ordenes_venta exige que la tabla exista
    with db.db_session() as conn:
        conn.executescript(SQL_DESPACHOS)
        if "ov_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(despachos)")}:
            conn.execute("ALTER TABLE despachos ADD COLUMN ov_id INTEGER REFERENCES ordenes_venta(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_despachos_ov ON despachos (ov_id)")


# ===========================================================================
# LECTURA
# ===========================================================================
def siguiente_numero(conn: sqlite3.Connection) -> str:
    mayor = 0
    for f in conn.execute("SELECT numero_despacho FROM despachos WHERE numero_despacho LIKE ?", (DES_PREFIJO + "%",)):
        m = _DES_RE.match(f["numero_despacho"])
        if m:
            mayor = max(mayor, int(m.group(1)))
    return f"{DES_PREFIJO}{mayor + 1:0{DES_ANCHO}d}"


def vista_previa_numero() -> str:
    conn = db.get_connection()
    try:
        return siguiente_numero(conn)
    finally:
        conn.close()


def listar_despachos() -> pd.DataFrame:
    return db.fetch_df(
        """SELECT d.id, d.numero_despacho, d.fecha, d.estado, d.oc_cliente, d.guia_remision, d.ov_id, ov.numero_ov,
                  c.razon_social AS cliente, c.tipo_documento || ' ' || c.numero_documento AS documento,
                  (SELECT COUNT(*) FROM despacho_detalle x WHERE x.despacho_id = d.id) AS lineas,
                  COALESCE((SELECT SUM(cantidad) FROM despacho_detalle x WHERE x.despacho_id = d.id), 0) AS unidades
           FROM despachos d JOIN clientes c ON c.id = d.cliente_id LEFT JOIN ordenes_venta ov ON ov.id = d.ov_id
           ORDER BY d.fecha DESC, d.id DESC""")


def obtener_despacho(despacho_id: int) -> Optional[sqlite3.Row]:
    return db.fetch_one(
        """SELECT d.*, ov.numero_ov, c.razon_social AS cliente_nombre, c.tipo_documento, c.numero_documento,
                  c.direccion AS cliente_direccion
           FROM despachos d JOIN clientes c ON c.id = d.cliente_id LEFT JOIN ordenes_venta ov ON ov.id = d.ov_id
           WHERE d.id = ?""", (despacho_id,))


def _disponible_sql() -> str:
    return ("COALESCE((SELECT SUM(l.cantidad_actual) FROM lotes_inventario l WHERE l.producto_id = p.id AND l.activo = 1 "
            "AND (l.fecha_vencimiento IS NULL OR l.fecha_vencimiento >= :hoy)), 0)")


def lineas_despacho(despacho_id: int) -> pd.DataFrame:
    df = db.fetch_df(
        f"""SELECT x.id, x.producto_id, p.codigo, p.nombre AS producto, p.unidad_medida AS unidad, x.cantidad,
                   p.precio_venta, {_disponible_sql()} AS disponible
            FROM despacho_detalle x JOIN productos p ON p.id = x.producto_id
            WHERE x.despacho_id = :did ORDER BY x.id""", {"hoy": date.today().isoformat(), "did": despacho_id})
    df["faltante"] = (df["cantidad"] - df["disponible"]).clip(lower=0)
    df["importe_ref"] = (df["cantidad"] * df["precio_venta"]).round(2)
    return df


def lineas_pendientes_ov(ov_id: int) -> pd.DataFrame:
    """Pedido AUTOCOMPLETADO: lo que aún falta despachar de la OV, con el stock vigente disponible."""
    df = ventas_db.lineas_ov(ov_id)
    df = df[df["pendiente"] > 0].copy()
    stock = db.fetch_df(
        f"""SELECT p.id AS producto_id, {_disponible_sql()} AS disponible FROM productos p""",
        {"hoy": date.today().isoformat()}).set_index("producto_id")["disponible"]
    df["disponible"] = df["producto_id"].map(stock).fillna(0.0)
    return df.reset_index(drop=True)


def plan_picking_lineas(lineas: list[dict]) -> tuple[pd.DataFrame, list[dict]]:
    """Plan FEFO (sin mover inventario) para [{producto_id, cantidad}]. Devuelve (plan, faltantes)."""
    columnas = ["codigo", "producto", "unidad", "lote", "vence", "ubicacion", "cantidad"]
    filas, faltantes = [], []
    conn = db.get_connection()
    try:
        for l in lineas:
            p = conn.execute("SELECT codigo, nombre, unidad_medida FROM productos WHERE id = ?", (l["producto_id"],)).fetchone()
            if p is None:
                continue
            plan, falta = inv.calcular_plan_fefo(conn, l["producto_id"], l["cantidad"])
            for paso in plan:
                filas.append({"codigo": p["codigo"], "producto": p["nombre"], "unidad": p["unidad_medida"], "lote": paso["lote"],
                              "vence": paso["vence"], "ubicacion": paso["ubicacion"] or "", "cantidad": paso["tomar"]})
            if falta > 0:
                faltantes.append({"codigo": p["codigo"], "producto": p["nombre"], "faltante": falta})
    finally:
        conn.close()
    return _ordenar(pd.DataFrame(filas, columns=columnas)), faltantes


def _ordenar(df: pd.DataFrame) -> pd.DataFrame:
    """Ruta de picking: por ubicación (las vacías al final), luego SKU y vencimiento."""
    if df.empty:
        return df
    return df.assign(_v=df["ubicacion"].eq("")).sort_values(["_v", "ubicacion", "codigo", "vence"], na_position="last") \
             .drop(columns="_v").reset_index(drop=True)


def plan_picking(despacho_id: int) -> tuple[pd.DataFrame, list[dict]]:
    """Borrador -> plan FEFO de hoy · Despachado -> los lotes realmente descontados (leídos del kardex)."""
    d = obtener_despacho(despacho_id)
    if d is None:
        raise ValueError("El despacho no existe.")
    if d["estado"] == "Despachado":
        df = db.fetch_df(
            """SELECT p.codigo, p.nombre AS producto, p.unidad_medida AS unidad, m.numero_lote AS lote,
                      l.fecha_vencimiento AS vence, COALESCE(l.ubicacion, '') AS ubicacion, -m.cantidad AS cantidad
               FROM movimientos_inventario m JOIN productos p ON p.id = m.producto_id
               LEFT JOIN lotes_inventario l ON l.id = m.lote_id
               WHERE m.tipo = 'SALIDA' AND m.referencia = ? ORDER BY m.id""", (d["numero_despacho"],))
        return _ordenar(df), []
    lineas = lineas_despacho(despacho_id)
    return plan_picking_lineas([{"producto_id": int(r.producto_id), "cantidad": float(r.cantidad)} for r in lineas.itertuples()])


# ===========================================================================
# ESCRITURA
# ===========================================================================
def _normalizar_lineas(lineas: list[dict]) -> list[dict]:
    vistos: set[int] = set()
    out = []
    for i, l in enumerate(lineas, start=1):
        cantidad = round(float(l.get("cantidad") or 0), inv.DECIMALES)
        if not cantidad > 0:
            raise ValueError(f"Línea {i}: la cantidad debe ser mayor que cero.")
        pid = int(l["producto_id"])
        if pid in vistos:
            raise ValueError(f"Línea {i}: el producto está repetido en el despacho.")
        vistos.add(pid)
        out.append({"producto_id": pid, "cantidad": cantidad})
    if not out:
        raise ValueError("El despacho debe tener al menos un producto.")
    return out


def _validar_contra_ov(conn: sqlite3.Connection, ov_id: int, lineas: list[dict]) -> sqlite3.Row:
    ov = conn.execute("SELECT id, estado, numero_ov, cliente_id, oc_cliente FROM ordenes_venta WHERE id = ?", (ov_id,)).fetchone()
    if ov is None:
        raise ValueError("La orden de venta no existe.")
    if ov["estado"] == "Anulada":
        raise ValueError(f"La orden de venta {ov['numero_ov']} está anulada.")
    for l in lineas:
        d = conn.execute("SELECT d.cantidad - d.cantidad_despachada AS pendiente, p.codigo FROM ov_detalle d "
                         "JOIN productos p ON p.id = d.producto_id WHERE d.ov_id = ? AND d.producto_id = ?",
                         (ov_id, l["producto_id"])).fetchone()
        if d is None:
            p = conn.execute("SELECT codigo FROM productos WHERE id = ?", (l["producto_id"],)).fetchone()
            raise ValueError(f"{p['codigo'] if p else 'El producto'} no forma parte de la OV {ov['numero_ov']}.")
        if l["cantidad"] > round(d["pendiente"], inv.DECIMALES) + 1e-9:
            raise ValueError(f"{d['codigo']}: se despachan {l['cantidad']:g} pero la OV solo tiene {d['pendiente']:g} pendientes.")
    return ov


def crear_despacho(usuario: dict, *, ov_id: int, fecha: date, guia_remision: str, observaciones: str,
                   lineas: list[dict]) -> int:
    """Crea el despacho (borrador) contra una OV. Cliente y OC del cliente se toman de la OV."""
    permisos.exigir_escritura(usuario, "despachos")
    guia = identidad.normalizar_guia_remision(guia_remision)
    lineas = _normalizar_lineas(lineas)
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ov = _validar_contra_ov(conn, ov_id, lineas)
        clientes_db.instantanea_cliente(conn, ov["cliente_id"])      # el cliente debe seguir activo
        numero = siguiente_numero(conn)
        cur = conn.execute(
            """INSERT INTO despachos (numero_despacho, cliente_id, oc_cliente, fecha, guia_remision, observaciones,
                                      creado_por, ov_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (numero, ov["cliente_id"], ov["oc_cliente"], fecha.isoformat(), guia or None,
             (observaciones or "").strip() or None, usuario["id"], ov_id))
        conn.executemany("INSERT INTO despacho_detalle (despacho_id, producto_id, cantidad) VALUES (?, ?, ?)",
                         [(cur.lastrowid, l["producto_id"], l["cantidad"]) for l in lineas])
        return cur.lastrowid


def _borrador(conn: sqlite3.Connection, despacho_id: int) -> sqlite3.Row:
    d = conn.execute("SELECT * FROM despachos WHERE id = ?", (despacho_id,)).fetchone()
    if d is None:
        raise ValueError("El despacho no existe.")
    if d["estado"] != "Borrador":
        raise ValueError(f"El despacho está {d['estado'].lower()}; solo los borradores pueden modificarse.")
    return d


def reemplazar_lineas(usuario: dict, *, despacho_id: int, lineas: list[dict]) -> None:
    permisos.exigir_escritura(usuario, "despachos")
    lineas = _normalizar_lineas(lineas)
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        d = _borrador(conn, despacho_id)
        if d["ov_id"] is not None:
            _validar_contra_ov(conn, d["ov_id"], lineas)
        conn.execute("DELETE FROM despacho_detalle WHERE despacho_id = ?", (despacho_id,))
        conn.executemany("INSERT INTO despacho_detalle (despacho_id, producto_id, cantidad) VALUES (?, ?, ?)",
                         [(despacho_id, l["producto_id"], l["cantidad"]) for l in lineas])


def confirmar_despacho(usuario: dict, *, despacho_id: int, guia_remision: str = "") -> int:
    """
    Salida FÍSICA registrada contra la OV, en una sola transacción (todo o nada): FEFO por lotes + kardex
    (OV, cliente, guía) + cantidades despachadas de la OV. Luego deposita el Packing List en su expediente.
    """
    permisos.exigir_escritura(usuario, "despachos")
    guia_nueva = identidad.normalizar_guia_remision(guia_remision)
    inv._asegurar_tabla_movimientos()
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        d = _borrador(conn, despacho_id)
        if d["ov_id"] is None:
            raise ValueError("Este despacho no está vinculado a una Orden de Venta. Toda salida debe registrarse contra "
                             "una OV: anúlelo y cree uno nuevo desde la OV.")
        lineas = conn.execute(
            """SELECT x.producto_id, x.cantidad, p.codigo FROM despacho_detalle x JOIN productos p ON p.id = x.producto_id
               WHERE x.despacho_id = ? ORDER BY x.id""", (despacho_id,)).fetchall()
        datos = [{"producto_id": l["producto_id"], "cantidad": l["cantidad"]} for l in lineas]
        ov = _validar_contra_ov(conn, d["ov_id"], _normalizar_lineas(datos))        # re-valida contra lo pendiente HOY
        guia = guia_nueva or (d["guia_remision"] or "")
        cliente = clientes_db.instantanea_cliente(conn, d["cliente_id"])
        for l in lineas:
            try:
                inv.salida_fefo_en_transaccion(conn, usuario_id=usuario["id"], producto_id=l["producto_id"],
                                               cantidad=l["cantidad"], motivo="Venta / Despacho",
                                               referencia=d["numero_despacho"], guia=guia, cliente=cliente,
                                               ov_numero=ov["numero_ov"])
            except ValueError as e:
                raise ValueError(f"{l['codigo']}: {e} No se descontó nada del pedido.") from None
            conn.execute("UPDATE ov_detalle SET cantidad_despachada = ROUND(cantidad_despachada + ?, ?) "
                         "WHERE ov_id = ? AND producto_id = ?", (l["cantidad"], inv.DECIMALES, d["ov_id"], l["producto_id"]))
        conn.execute("UPDATE despachos SET estado = 'Despachado', despachado_en = ?, guia_remision = ? WHERE id = ?",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), guia or None, despacho_id))
        ov_id = d["ov_id"]
    try:                       # autoguardado: Packing List en la carpeta nodriza de la OV
        from modulos import expediente_ov
        desp = obtener_despacho(despacho_id)
        plan, _ = plan_picking(despacho_id)
        expediente_ov.adjuntar_automatico(
            ov_id, "OTRO", f"PackingList_{desp['numero_despacho']}.pdf",
            despachos_docs.pdf_packing(desp, plan, usuario.get("nombre_completo", "")),
            referencia=desp["numero_despacho"], usuario_id=usuario["id"], descripcion="Packing List del despacho")
    except Exception:
        pass
    return len(lineas)


def anular_despacho(usuario: dict, despacho_id: int) -> None:
    permisos.exigir_escritura(usuario, "despachos")
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _borrador(conn, despacho_id)
        conn.execute("UPDATE despachos SET estado = 'Anulado' WHERE id = ?", (despacho_id,))


def eliminar_despacho(usuario: dict, despacho_id: int) -> None:
    """Elimina un borrador o anulado. Un despacho ya despachado queda como registro (consta en el kardex)."""
    permisos.exigir_escritura(usuario, "despachos")
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        d = conn.execute("SELECT estado FROM despachos WHERE id = ?", (despacho_id,)).fetchone()
        if d is None:
            raise ValueError("El despacho no existe.")
        if d["estado"] == "Despachado":
            raise ValueError("Un despacho ya despachado no se puede eliminar (su salida consta en el kardex).")
        conn.execute("DELETE FROM despachos WHERE id = ?", (despacho_id,))
