"""
modulos/mantenimiento_db.py - Modo pruebas: iniciar con stock y OC en cero (FASE 3, sin interfaz)

Permite empezar (o repetir) pruebas desde cero SIN tocar usuarios ni contraseñas:

  Siempre se borran (datos operativos):
      lotes e inventario, kardex (movimientos), órdenes de compra y su detalle, facturas,
      despachos y su detalle, y el registro de documentos adjuntos.
  Opcionalmente:
      clientes, y productos (junto con las plantillas de compra).
  NUNCA se tocan:
      usuarios (credenciales bcrypt), auditoría, ni los archivos físicos de data/documentos.

Seguridad: solo Admin (validado contra la BD), requiere escribir la palabra REINICIAR, genera un
respaldo automático ANTES de borrar (data/respaldos/) y todo ocurre en una sola transacción.
Después del reinicio la numeración vuelve a empezar (OC-00001, DES-00001).
"""

from __future__ import annotations

from pathlib import Path

import database as db
from modulos import admin_db

PALABRA_CONFIRMACION = "REINICIAR"
DIR_RESPALDOS: Path = db.BASE_DIR / "data" / "respaldos"

# Orden seguro respecto a las llaves foráneas (primero los dependientes)
TABLAS_OPERATIVAS = ["despacho_detalle", "despachos", "movimientos_inventario", "documentos_adjuntos",
                     "lotes_inventario", "oc_detalle", "facturas", "ordenes_compra"]
TABLAS_CLIENTES = ["clientes"]
TABLAS_PRODUCTOS = ["plantilla_compra_detalle", "plantillas_compra", "productos"]


def _existe(conn, tabla: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (tabla,)).fetchone() is not None


def contar_datos() -> dict[str, int]:
    """Cantidad de filas de cada tabla que se puede reiniciar (para mostrar antes de confirmar)."""
    conn = db.get_connection()
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]     # nombres fijos (constantes)
                for t in TABLAS_OPERATIVAS + TABLAS_CLIENTES + TABLAS_PRODUCTOS if _existe(conn, t)}
    finally:
        conn.close()


def reiniciar_datos_operativos(admin: dict, *, confirmacion: str, borrar_clientes: bool = False,
                               borrar_productos: bool = False) -> dict:
    """Pone en cero stock, OC, facturas, despachos y kardex. Devuelve {'filas': {...}, 'respaldo': ruta}."""
    admin_db.exigir_admin(admin)
    if (confirmacion or "").strip() != PALABRA_CONFIRMACION:
        raise ValueError(f"Para confirmar escriba exactamente la palabra {PALABRA_CONFIRMACION}.")

    # 1) Respaldo automático ANTES de borrar
    datos, nombre = admin_db.crear_respaldo(admin)
    DIR_RESPALDOS.mkdir(parents=True, exist_ok=True)
    ruta = DIR_RESPALDOS / f"antes_de_reiniciar_{nombre}"
    ruta.write_bytes(datos)

    # 2) Borrado en una sola transacción
    tablas = list(TABLAS_OPERATIVAS)
    if borrar_clientes or borrar_productos:
        tablas += TABLAS_CLIENTES if borrar_clientes else []
    if borrar_productos:
     tablas += TABLAS_PRODUCTOS
     borradas: dict[str, int] = {}
    with db.db_session() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for t in tablas:
                if not _existe(conn, t):
                    continue
                borradas[t] = conn.execute(f"DELETE FROM {t}").rowcount
            if _existe(conn, "sqlite_sequence"):
                marcas = ",".join("?" for _ in borradas)
                if borradas:
                    conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({marcas})", tuple(borradas))
            detalle = ", ".join(f"{t}: {n}" for t, n in borradas.items() if n)
            admin_db.registrar_auditoria(conn, admin["id"], "DATOS_REINICIADOS", ruta.name,
                                         (detalle or "sin filas") + (" | clientes" if borrar_clientes else "") +
                                         (" | productos" if borrar_productos else ""))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    return {"filas": borradas, "respaldo": str(ruta)}