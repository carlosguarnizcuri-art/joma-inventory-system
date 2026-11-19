"""
modulos/plantillas_db.py - Plantillas predefinidas de compra (FASE 3, sin código de interfaz)

Una plantilla guarda proveedor, moneda y un detalle de productos con cantidades y precios de
referencia, para autocompletar una nueva Orden de Compra con un solo clic.
Solo el rol Admin crea o elimina plantillas (validado contra la BD).
"""

from __future__ import annotations

import sqlite3
from typing import Optional

import pandas as pd

import database as db
from modulos import admin_db

SQL_PLANTILLAS = """
CREATE TABLE IF NOT EXISTS plantillas_compra (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre        TEXT    NOT NULL UNIQUE,
    proveedor     TEXT,
    moneda        TEXT    NOT NULL DEFAULT 'PEN' CHECK (moneda IN ('PEN', 'USD')),
    observaciones TEXT,
    creado_por    INTEGER REFERENCES usuarios(id),
    creado_en     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS plantilla_compra_detalle (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plantilla_id    INTEGER NOT NULL REFERENCES plantillas_compra(id) ON DELETE CASCADE,
    producto_id     INTEGER NOT NULL REFERENCES productos(id) ON DELETE RESTRICT,
    cantidad        REAL    NOT NULL CHECK (cantidad > 0),
    precio_unitario REAL    NOT NULL DEFAULT 0 CHECK (precio_unitario >= 0),
    UNIQUE (plantilla_id, producto_id)
);
"""


def asegurar_esquema() -> None:
    with db.db_session() as conn:
        conn.executescript(SQL_PLANTILLAS)


def listar_plantillas() -> pd.DataFrame:
    return db.fetch_df(
        """SELECT p.id, p.nombre, p.proveedor, p.moneda, p.observaciones, p.creado_en,
                  (SELECT COUNT(*) FROM plantilla_compra_detalle d WHERE d.plantilla_id = p.id) AS lineas
           FROM plantillas_compra p ORDER BY p.nombre""")


def lineas_plantilla(plantilla_id: int) -> pd.DataFrame:
    return db.fetch_df(
        """SELECT d.producto_id, pr.codigo, pr.nombre AS producto, pr.unidad_medida AS unidad,
                  d.cantidad, d.precio_unitario
           FROM plantilla_compra_detalle d JOIN productos pr ON pr.id = d.producto_id
           WHERE d.plantilla_id = ? ORDER BY d.id""", (plantilla_id,))


def obtener_plantilla(plantilla_id: int) -> Optional[sqlite3.Row]:
    return db.fetch_one("SELECT * FROM plantillas_compra WHERE id = ?", (plantilla_id,))


def _normalizar(lineas: list[dict]) -> list[dict]:
    vistos: set[int] = set()
    out = []
    for i, l in enumerate(lineas, start=1):
        cantidad = round(float(l.get("cantidad") or 0), 3)
        precio = round(float(l.get("precio") or 0), 4)
        if not cantidad > 0:
            raise ValueError(f"Línea {i}: la cantidad debe ser mayor que cero.")
        if not precio >= 0:
            raise ValueError(f"Línea {i}: el precio no puede ser negativo.")
        pid = int(l["producto_id"])
        if pid in vistos:
            raise ValueError(f"Línea {i}: el producto está repetido en la plantilla.")
        vistos.add(pid)
        out.append({"producto_id": pid, "cantidad": cantidad, "precio": precio})
    return out


def crear_plantilla(admin: dict, *, nombre: str, proveedor: str, moneda: str, observaciones: str,
                    lineas: list[dict]) -> int:
    admin_db.exigir_admin(admin)
    nombre = " ".join((nombre or "").split())
    proveedor = " ".join((proveedor or "").split())
    if not nombre:
        raise ValueError("Ingrese un nombre para la plantilla.")
    if moneda not in ("PEN", "USD"):
        raise ValueError("Moneda inválida.")
    lineas = _normalizar(lineas)
    if not lineas:
        raise ValueError("La plantilla debe tener al menos un producto.")
    try:
        with db.db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM plantillas_compra WHERE nombre = ? COLLATE NOCASE", (nombre,)).fetchone():
                raise ValueError(f"Ya existe una plantilla llamada '{nombre}'.")
            for l in lineas:
                p = conn.execute("SELECT activo FROM productos WHERE id = ?", (l["producto_id"],)).fetchone()
                if p is None or not p["activo"]:
                    raise ValueError("Hay un producto inexistente o inactivo en la plantilla.")
            cur = conn.execute(
                "INSERT INTO plantillas_compra (nombre, proveedor, moneda, observaciones, creado_por) "
                "VALUES (?, ?, ?, ?, ?)",
                (nombre, proveedor or None, moneda, (observaciones or "").strip() or None, admin["id"]))
            conn.executemany(
                "INSERT INTO plantilla_compra_detalle (plantilla_id, producto_id, cantidad, precio_unitario) "
                "VALUES (?, ?, ?, ?)", [(cur.lastrowid, l["producto_id"], l["cantidad"], l["precio"]) for l in lineas])
            return cur.lastrowid
    except sqlite3.IntegrityError as e:
        raise ValueError(f"No se pudo guardar la plantilla: {e}") from None


def plantilla_desde_oc(admin: dict, *, oc_id: int, nombre: str = "") -> int:
    """Convierte una OC existente en plantilla (un clic). Si no se da nombre usa 'Plantilla <proveedor> <OC>'."""
    admin_db.exigir_admin(admin)
    oc = db.fetch_one("SELECT numero_oc, proveedor, moneda FROM ordenes_compra WHERE id = ?", (oc_id,))
    if oc is None:
        raise ValueError("La orden de compra no existe.")
    df = db.fetch_df("SELECT producto_id, cantidad_pedida AS cantidad, precio_unitario AS precio "
                     "FROM oc_detalle WHERE orden_compra_id = ?", (oc_id,))
    if df.empty:
        raise ValueError("La OC no tiene líneas de detalle para guardar como plantilla.")
    nombre = nombre.strip() or f"Plantilla {oc['proveedor']} {oc['numero_oc']}"
    return crear_plantilla(admin, nombre=nombre, proveedor=oc["proveedor"], moneda=oc["moneda"],
                           observaciones=f"Creada desde {oc['numero_oc']}",
                           lineas=df.to_dict("records"))


def eliminar_plantilla(admin: dict, plantilla_id: int) -> None:
    admin_db.exigir_admin(admin)
    with db.db_session() as conn:
        if conn.execute("SELECT 1 FROM plantillas_compra WHERE id = ?", (plantilla_id,)).fetchone() is None:
            raise ValueError("La plantilla no existe.")
        conn.execute("DELETE FROM plantillas_compra WHERE id = ?", (plantilla_id,))   # el detalle cae en cascada
