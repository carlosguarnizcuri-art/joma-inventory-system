"""
modulos/clientes_db.py - Clientes y ampliación del kardex (FASE 2, sin código de interfaz)

  * Tabla `clientes` (DNI / RUC validados offline con identidad.py).
  * MIGRACIÓN SEGURA de `movimientos_inventario`: agrega columnas con ALTER TABLE ADD COLUMN
    (guía de remisión y datos del cliente). Los movimientos anteriores se conservan intactos y
    simplemente quedan con esos campos vacíos. Es idempotente: puede ejecutarse N veces.
  * Los datos del cliente se guardan en cada movimiento como INSTANTÁNEA (nombre, documento y
    dirección del momento de la salida): si luego se edita el cliente, el kardex histórico no cambia.
  * Solo el rol Admin crea o modifica clientes (validado contra la BD).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

import pandas as pd

import database as db
from modulos import admin_db, identidad

SQL_CLIENTES = """
CREATE TABLE IF NOT EXISTS clientes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo_documento    TEXT    NOT NULL CHECK (tipo_documento IN ('DNI', 'RUC')),
    numero_documento  TEXT    NOT NULL UNIQUE,
    razon_social      TEXT    NOT NULL,          -- razón social (RUC) o nombres y apellidos (DNI)
    direccion         TEXT,
    telefono          TEXT,
    email             TEXT,
    activo            INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1)),
    fuente_datos      TEXT    NOT NULL DEFAULT 'MANUAL' CHECK (fuente_datos IN ('MANUAL', 'API')),
    creado_por        INTEGER REFERENCES usuarios(id),
    creado_en         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_clientes_razon ON clientes (razon_social);
"""

# Columnas nuevas del kardex (todas opcionales: los registros previos quedan en NULL)
COLUMNAS_KARDEX_FASE2 = {
    "guia_remision":     "TEXT",
    "cliente_id":        "INTEGER REFERENCES clientes(id)",
    "cliente_nombre":    "TEXT",
    "cliente_documento": "TEXT",
    "cliente_direccion": "TEXT",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TELEFONO_RE = re.compile(r"^[0-9+()\-\s]{6,20}$")


# ===========================================================================
# ESQUEMA / MIGRACIÓN
# ===========================================================================
def migrar_movimientos(conn: sqlite3.Connection) -> list[str]:
    """Agrega a movimientos_inventario las columnas que falten. Devuelve las columnas añadidas."""
    existentes = {r["name"] for r in conn.execute("PRAGMA table_info(movimientos_inventario)")}
    if not existentes:          # la tabla aún no existe (la crea el Módulo 2); se reintenta después
        return []
    agregadas = []
    for columna, ddl in COLUMNAS_KARDEX_FASE2.items():
        if columna not in existentes:
            try:
                conn.execute(f"ALTER TABLE movimientos_inventario ADD COLUMN {columna} {ddl}")
                agregadas.append(columna)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():   # otro proceso la agregó primero
                    raise
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mov_cliente ON movimientos_inventario (cliente_id)")
    return agregadas


def asegurar_esquema() -> list[str]:
    """Crea `clientes` y migra el kardex (idempotente). Devuelve las columnas agregadas al kardex."""
    with db.db_session() as conn:
        conn.executescript(SQL_CLIENTES)
        return migrar_movimientos(conn)


# ===========================================================================
# LECTURA
# ===========================================================================
def listar_clientes(solo_activos: bool = False) -> pd.DataFrame:
    df = db.fetch_df(
        """SELECT c.id, c.tipo_documento, c.numero_documento, c.razon_social, c.direccion, c.telefono,
                  c.email, c.activo, c.fuente_datos, c.creado_en,
                  (SELECT COUNT(*) FROM movimientos_inventario m WHERE m.cliente_id = c.id) AS salidas
           FROM clientes c
           WHERE (:solo = 0 OR c.activo = 1)
           ORDER BY c.razon_social""", {"solo": int(solo_activos)})
    df["documento"] = df["tipo_documento"] + " " + df["numero_documento"]
    return df


def obtener_cliente(cliente_id: int) -> Optional[sqlite3.Row]:
    return db.fetch_one("SELECT * FROM clientes WHERE id = ?", (cliente_id,))


def obtener_cliente_por_documento(numero_documento: str) -> Optional[sqlite3.Row]:
    """Cliente por DNI/RUC (solo dígitos). None si no existe."""
    return db.fetch_one("SELECT * FROM clientes WHERE numero_documento = ?", (numero_documento,))


def instantanea_cliente(conn: sqlite3.Connection, cliente_id: Optional[int]) -> Optional[dict[str, Any]]:
    """Datos del cliente para guardar en el kardex (dentro de la transacción en curso)."""
    if not cliente_id:
        return None
    c = conn.execute("SELECT id, tipo_documento, numero_documento, razon_social, direccion, activo "
                     "FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    if c is None:
        raise ValueError("El cliente seleccionado no existe.")
    if not c["activo"]:
        raise ValueError("El cliente seleccionado está inactivo.")
    return {"cliente_id": c["id"], "cliente_nombre": c["razon_social"],
            "cliente_documento": f"{c['tipo_documento']} {c['numero_documento']}",
            "cliente_direccion": c["direccion"]}


# ===========================================================================
# ESCRITURA (solo Admin)
# ===========================================================================
def _validar_datos(tipo: str, razon_social: str, direccion: str, telefono: str, email: str) -> dict[str, Optional[str]]:
    razon = " ".join((razon_social or "").split())
    direccion = " ".join((direccion or "").split())
    telefono = (telefono or "").strip()
    email = (email or "").strip().lower()
    if not razon:
        raise ValueError("Ingrese la razón social o el nombre del cliente.")
    if len(razon) > 200:
        raise ValueError("La razón social no puede superar los 200 caracteres.")
    if tipo == "RUC" and not direccion:
        raise ValueError("La dirección es obligatoria para clientes con RUC.")
    if len(direccion) > 250:
        raise ValueError("La dirección no puede superar los 250 caracteres.")
    if telefono and not _TELEFONO_RE.match(telefono):
        raise ValueError("Teléfono inválido: use solo números, espacios y los símbolos + - ( ).")
    if email and not _EMAIL_RE.match(email):
        raise ValueError("El correo electrónico no tiene un formato válido.")
    return {"razon": razon, "direccion": direccion or None, "telefono": telefono or None, "email": email or None}


def crear_cliente(admin: dict, *, numero_documento: str, razon_social: str, direccion: str = "",
                  telefono: str = "", email: str = "", fuente: str = "MANUAL") -> int:
    admin_db.exigir_admin(admin)
    doc = identidad.validar_documento(numero_documento)          # ValueError si el formato es inválido
    if fuente not in ("MANUAL", "API"):
        raise ValueError("Fuente de datos inválida.")
    d = _validar_datos(doc.tipo, razon_social, direccion, telefono, email)
    try:
        with db.db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM clientes WHERE numero_documento = ?", (doc.numero,)).fetchone():
                raise ValueError(f"Ya existe un cliente con el documento {doc.etiqueta}.")
            cur = conn.execute(
                """INSERT INTO clientes (tipo_documento, numero_documento, razon_social, direccion, telefono,
                                         email, fuente_datos, creado_por)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc.tipo, doc.numero, d["razon"], d["direccion"], d["telefono"], d["email"], fuente, admin["id"]))
            return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"Ya existe un cliente con el documento {doc.etiqueta}.") from None


def actualizar_cliente(admin: dict, *, cliente_id: int, razon_social: str, direccion: str = "",
                       telefono: str = "", email: str = "", activo: bool = True) -> None:
    """Edita los datos del cliente. El documento no se modifica. El kardex histórico no cambia."""
    admin_db.exigir_admin(admin)
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        c = conn.execute("SELECT tipo_documento FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
        if c is None:
            raise ValueError("El cliente no existe.")
        d = _validar_datos(c["tipo_documento"], razon_social, direccion, telefono, email)
        conn.execute("UPDATE clientes SET razon_social = ?, direccion = ?, telefono = ?, email = ?, activo = ? "
                     "WHERE id = ?", (d["razon"], d["direccion"], d["telefono"], d["email"], int(activo), cliente_id))
