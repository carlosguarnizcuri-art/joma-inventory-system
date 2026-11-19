#!/usr/bin/env python
"""
aplicar_mejoras.py - Migración "de golpe" a la FASE 4 (idempotente: se puede ejecutar varias veces)

Uso (con la aplicación CERRADA, desde la carpeta del proyecto):
        python aplicar_mejoras.py

Qué hace, en orden:
  1. Verifica las librerías necesarias (streamlit, pandas, plotly, bcrypt, openpyxl, reportlab).
  2. Genera un RESPALDO consistente de joma_inventory.db en data/respaldos/ (si la base ya existe).
  3. Roles múltiples: crea las tablas `roles` y `usuario_roles` y asigna a cada usuario existente el rol
     que ya tenía (sus contraseñas bcrypt NO se tocan).
  4. Crea/actualiza las tablas nuevas: Órdenes de Venta (+ detalle y documentos), facturas de venta
     (Cuentas por Cobrar), despachos con ov_id, y las columnas de kardex (guía, cliente, OV). No borra datos.
  5. Crea las carpetas: data/documentos, data/documentos_ov, data/respaldos y assets.
  6. Verifica integridad (integrity_check / foreign_key_check), que todo usuario tenga rol y que exista un
     administrador activo; avisa si falta el logotipo o la dirección fiscal.
"""

from __future__ import annotations

import importlib
import logging
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))
logging.getLogger("streamlit").setLevel(logging.ERROR)

OK, AVISO, ERROR = "  ✔", "  ⚠", "  ✖"
problemas = 0


def linea(marca: str, texto: str) -> None:
    print(f"{marca} {texto}")


def titulo(texto: str) -> None:
    print(f"\n== {texto}")


def main() -> int:
    global problemas
    print("JOMA S.A.C. · Migración a la Fase 4 (roles múltiples, Órdenes de Venta, Facturación dual)")

    titulo("1. Librerías")
    for modulo, paquete in (("streamlit", "streamlit"), ("pandas", "pandas"), ("plotly", "plotly"), ("bcrypt", "bcrypt"),
                            ("openpyxl", "openpyxl"), ("reportlab", "reportlab"), ("PIL", "pillow")):
        try:
            importlib.import_module(modulo)
            linea(OK, paquete)
        except ImportError:
            linea(ERROR, f"{paquete} no está instalado  ->  pip install {paquete}")
            problemas += 1
    if problemas:
        print("\nInstale las librerías faltantes (pip install -r requirements.txt) y vuelva a ejecutar este script.")
        return 1

    import database as db
    from modulos import admin_db, clientes_db, compras_db, despachos_db, empresa, plantillas_db, ventas_db
    from modulos import inventario as inv

    titulo("2. Respaldo previo")
    if db.DB_PATH.exists():
        destino = RAIZ / "data" / "respaldos" / f"antes_fase4_{datetime.now():%Y%m%d_%H%M%S}.db"
        destino.parent.mkdir(parents=True, exist_ok=True)
        origen = sqlite3.connect(db.DB_PATH)
        copia = sqlite3.connect(destino)
        try:
            origen.backup(copia)
        finally:
            copia.close()
            origen.close()
        linea(OK, f"Respaldo creado: {destino.relative_to(RAIZ)} ({destino.stat().st_size / 1024:.0f} KB)")
    else:
        linea(OK, "No hay base de datos previa: se creará una nueva (con los usuarios iniciales).")

    titulo("3. Roles múltiples")
    db.init_db()                                   # crea roles/usuario_roles y migra a los usuarios existentes
    with db.db_session() as conn:
        migrados = db.sembrar_roles_y_migrar(conn)
    usuarios = db.fetch_df("SELECT u.username, u.activo, GROUP_CONCAT(ur.rol, ' + ') AS roles FROM usuarios u "
                           "LEFT JOIN usuario_roles ur ON ur.usuario_id = u.id GROUP BY u.id ORDER BY u.id")
    for r in usuarios.itertuples():
        linea(OK if r.roles else ERROR, f"{r.username:<16} roles: {r.roles or 'SIN ROL'}{'' if r.activo else '  (inactivo)'}")
        problemas += 0 if r.roles else 1
    linea(OK, "Contraseñas bcrypt sin modificar.")

    titulo("4. Tablas y columnas nuevas")
    with db.db_session() as conn:                  # kardex base (si aún no existía)
        conn.executescript(inv.SQL_MOVIMIENTOS)
    añadidas = clientes_db.asegurar_esquema()
    linea(OK, "Clientes y columnas del kardex" + (f" (agregadas: {', '.join(añadidas)})" if añadidas else " (al día)"))
    compras_db.asegurar_esquema()
    linea(OK, "Órdenes de compra, detalle y documentos")
    plantillas_db.asegurar_esquema()
    linea(OK, "Plantillas de compra")
    ventas_db.asegurar_esquema()
    linea(OK, "Órdenes de Venta, detalle, documentos del expediente y facturas de venta (Cuentas por Cobrar)")
    despachos_db.asegurar_esquema()
    linea(OK, "Despachos vinculados a OV (columna ov_id)")
    admin_db.asegurar_tablas()
    linea(OK, "Auditoría")

    titulo("5. Carpetas")
    for sub in ("data/documentos", "data/documentos_ov", "data/respaldos", "assets"):
        (RAIZ / sub).mkdir(parents=True, exist_ok=True)
        linea(OK, sub)
    viejo = RAIZ / "modulos" / "ordenes_facturas.py"
    if viejo.exists():
        viejo.rename(viejo.with_suffix(".py.bak"))
        linea(AVISO, "modulos/ordenes_facturas.py (módulo antiguo) renombrado a .py.bak: ya no se usa.")

    titulo("6. Verificación")
    conn = db.get_connection()
    try:
        integridad = conn.execute("PRAGMA integrity_check").fetchone()[0]
        violaciones = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        sin_rol = conn.execute("SELECT COUNT(*) FROM usuarios u WHERE NOT EXISTS (SELECT 1 FROM usuario_roles r WHERE r.usuario_id = u.id)").fetchone()[0]
        admins = conn.execute("SELECT COUNT(DISTINCT u.id) FROM usuarios u JOIN usuario_roles r ON r.usuario_id = u.id "
                              "WHERE r.rol = 'Admin' AND u.activo = 1").fetchone()[0]
    finally:
        conn.close()
    for ok, texto in ((integridad == "ok" and violaciones == 0, f"Integridad de la base de datos ({integridad}; llaves foráneas: {violaciones} violaciones)"),
                      (sin_rol == 0, "Todos los usuarios tienen al menos un rol"),
                      (admins >= 1, f"Administradores activos: {admins}")):
        linea(OK if ok else ERROR, texto)
        problemas += 0 if ok else 1
    if empresa.ruta_logo() is None:
        linea(AVISO, "Falta el logotipo: copie assets/logo_joma.png (los PDF usan un logotipo genérico mientras tanto).")
    else:
        linea(OK, f"Logotipo oficial: {empresa.ruta_logo().relative_to(RAIZ)}")
    if not empresa.DIRECCION:
        linea(AVISO, "Complete la dirección fiscal en modulos/empresa.py (se imprime en el membrete de los PDF).")
    linea(OK, f"RUC en documentos: {empresa.RUC}")

    print("\n" + ("✔ Migración completada. Inicie la aplicación con:  streamlit run app.py" if not problemas
                  else f"✖ Migración terminada con {problemas} problema(s): revise los mensajes ✖ de arriba."))
    return 1 if problemas else 0


if __name__ == "__main__":
    sys.exit(main())
