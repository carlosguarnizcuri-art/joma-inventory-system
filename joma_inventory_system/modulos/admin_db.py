"""
modulos/admin_db.py - Lógica de administración de usuarios y sistema (MÓDULO 4)

Funciones (sin código de interfaz):
  * Crear usuarios, cambiar rol / estado, restablecer contraseñas (bcrypt).
  * Política de contraseñas y generación de contraseñas temporales seguras.
  * Cambio de contraseña propio (verifica la contraseña actual).
  * Auditoría: registro de cada acción administrativa (NUNCA se guardan contraseñas).
  * Respaldo consistente de la base de datos y diagnóstico de integridad.

Reglas de seguridad:
  * Todas las operaciones administrativas exigen rol Admin validado contra la BD.
  * No se puede quitar el rol Admin ni desactivar al ÚLTIMO administrador activo.
  * Un administrador no puede quitarse su propio rol ni desactivar su propia cuenta
    (evita bloqueos accidentales).
  * Los usuarios no se eliminan (el kardex y los documentos los referencian): se desactivan.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import tempfile
from datetime import datetime
from typing import Any, Optional

import pandas as pd

import database as db
from modulos import permisos

SQL_AUDITORIA = """
CREATE TABLE IF NOT EXISTS auditoria_admin (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    admin_id  INTEGER REFERENCES usuarios(id),
    accion    TEXT    NOT NULL,
    objetivo  TEXT,
    detalle   TEXT
);
CREATE INDEX IF NOT EXISTS idx_auditoria_fecha ON auditoria_admin (fecha);
"""

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,29}$")
CONTRASENAS_COMUNES = {"admin123", "lector123", "password", "contrasena", "12345678", "123456789",
                       "qwerty123", "joma1234", "joma2026", "admin1234", "password1"}
ALFABETO_TEMPORAL = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # sin 0/O/1/l/I


def asegurar_tablas() -> None:
    """Crea la tabla de auditoría (idempotente)."""
    with db.db_session() as conn:
        conn.executescript(SQL_AUDITORIA)


# ---------------------------------------------------------------------------
# SEGURIDAD
# ---------------------------------------------------------------------------
def exigir_admin(usuario: dict[str, Any]) -> None:
    """Verifica en la BD (no solo en la sesión) que el usuario esté activo y tenga el rol Admin."""
    permisos.exigir_admin(usuario)


def validar_password(password: str, username: str = "") -> None:
    """Política: mínimo 8 caracteres, con letras y números, distinta del usuario y no trivial."""
    if len(password or "") < 8:
        raise ValueError("La contraseña debe tener al menos 8 caracteres.")
    if len(password) > 72:
        raise ValueError("La contraseña no puede superar los 72 caracteres.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ValueError("La contraseña debe combinar letras y números.")
    if username and password.lower() == username.lower():
        raise ValueError("La contraseña no puede ser igual al nombre de usuario.")
    if password.lower() in CONTRASENAS_COMUNES:
        raise ValueError("Esa contraseña es demasiado común. Elija otra.")


def generar_password_temporal(longitud: int = 10) -> str:
    """Contraseña aleatoria criptográficamente segura (siempre cumple la política)."""
    while True:
        pw = "".join(secrets.choice(ALFABETO_TEMPORAL) for _ in range(longitud))
        if re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw):
            return pw


def registrar_auditoria(conn: sqlite3.Connection, admin_id: Optional[int], accion: str,
                        objetivo: str = "", detalle: str = "") -> None:
    """Registra una acción administrativa dentro de la transacción en curso."""
    conn.execute("INSERT INTO auditoria_admin (admin_id, accion, objetivo, detalle) VALUES (?, ?, ?, ?)",
                 (admin_id, accion, objetivo or None, detalle or None))


# ---------------------------------------------------------------------------
# CONSULTAS
# ---------------------------------------------------------------------------
def estado_usuario(user_id: int) -> Optional[dict[str, Any]]:
    """Estado vigente en BD de un usuario, con TODOS sus roles (la app lo consulta en cada recarga para
    aplicar de inmediato cambios de roles o desactivaciones)."""
    fila = db.fetch_one("SELECT id, username, nombre_completo, rol, activo FROM usuarios WHERE id = ?", (user_id,))
    if fila is None:
        return None
    d = dict(fila)
    d["roles"] = permisos.normalizar_roles(db.roles_de_usuario(user_id)) or [d["rol"]]
    return d


def listar_usuarios() -> pd.DataFrame:
    # La tabla del kardex la crea el módulo de Inventario: si aún no existe, se informa 0 movimientos.
    hay_kardex = db.fetch_one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'movimientos_inventario'")
    movimientos = ("(SELECT COUNT(*) FROM movimientos_inventario m WHERE m.usuario_id = u.id)"
                   if hay_kardex else "0")
    df = db.fetch_df(
        f"""SELECT u.id, u.username, u.nombre_completo, u.activo, u.creado_en, u.ultimo_acceso,
                   (SELECT GROUP_CONCAT(ur.rol, ',') FROM usuario_roles ur WHERE ur.usuario_id = u.id) AS roles,
                   {movimientos} AS movimientos
            FROM usuarios u ORDER BY u.activo DESC, u.username""")
    df["lista_roles"] = df["roles"].map(lambda t: permisos.normalizar_roles((t or "").split(",")))
    df["roles"] = df["lista_roles"].map(lambda r: ", ".join(r))
    return df


def listar_auditoria(limite: int = 300) -> pd.DataFrame:
    return db.fetch_df(
        """SELECT a.fecha, COALESCE(u.username, '—') AS admin, a.accion, COALESCE(a.objetivo, '') AS objetivo,
                  COALESCE(a.detalle, '') AS detalle
           FROM auditoria_admin a LEFT JOIN usuarios u ON u.id = a.admin_id
           ORDER BY a.id DESC LIMIT ?""", (limite,))


# ---------------------------------------------------------------------------
# GESTIÓN DE USUARIOS (solo Admin)
# ---------------------------------------------------------------------------
def crear_usuario(admin: dict, *, username: str, nombre_completo: str, password: str,
                  roles: Optional[list[str]] = None, rol: Optional[str] = None) -> int:
    """Crea un usuario con uno o VARIOS roles (p. ej. Admin + Comercial)."""
    exigir_admin(admin)
    username = (username or "").strip().lower()
    nombre = " ".join((nombre_completo or "").split())
    pedidos = list(roles or ([rol] if rol else []))
    asignados = permisos.normalizar_roles(pedidos)
    if not USERNAME_RE.match(username):
        raise ValueError("El usuario debe tener 3 a 30 caracteres: letras minúsculas, números, punto, guion o "
                         "guion bajo (y empezar con letra o número).")
    if not nombre:
        raise ValueError("Ingrese el nombre completo.")
    if not asignados:
        raise ValueError("Asigne al menos un rol al usuario.")
    if len(asignados) != len({str(r).strip() for r in pedidos}):
        raise ValueError("Hay un rol inválido. Roles válidos: " + ", ".join(permisos.ROLES) + ".")
    validar_password(password, username)

    try:
        with db.db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM usuarios WHERE username = ?", (username,)).fetchone():
                raise ValueError(f"Ya existe el usuario '{username}'.")
            cur = conn.execute(
                "INSERT INTO usuarios (username, password_hash, nombre_completo, rol) VALUES (?, ?, ?, ?)",
                (username, db.hash_password(password), nombre, "Admin" if "Admin" in asignados else "Lector"))
            conn.executemany("INSERT INTO usuario_roles (usuario_id, rol, asignado_por) VALUES (?, ?, ?)",
                             [(cur.lastrowid, r, admin["id"]) for r in asignados])
            registrar_auditoria(conn, admin["id"], "USUARIO_CREADO", username, "Roles: " + ", ".join(asignados))
            return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"Ya existe el usuario '{username}'.") from None


def _admins_activos_distintos(conn: sqlite3.Connection, excluir_id: int) -> int:
    return conn.execute(
        """SELECT COUNT(DISTINCT u.id) FROM usuarios u JOIN usuario_roles ur ON ur.usuario_id = u.id
           WHERE ur.rol = 'Admin' AND u.activo = 1 AND u.id <> ?""", (excluir_id,)).fetchone()[0]


def _sincronizar_rol_legacy(conn: sqlite3.Connection, user_id: int) -> None:
    """Mantiene la columna legacy usuarios.rol: 'Admin' si tiene ese rol, 'Lector' en otro caso."""
    conn.execute(
        """UPDATE usuarios SET rol = CASE WHEN EXISTS (
               SELECT 1 FROM usuario_roles WHERE usuario_id = ? AND rol = 'Admin') THEN 'Admin' ELSE 'Lector' END
           WHERE id = ?""", (user_id, user_id))


def actualizar_usuario(admin: dict, *, user_id: int, nombre_completo: str, activo: bool) -> None:
    """Cambia nombre y estado (activo/inactivo), con salvaguardas contra bloqueos. Los roles se asignan
    con asignar_roles()."""
    exigir_admin(admin)
    nombre = " ".join((nombre_completo or "").split())
    if not nombre:
        raise ValueError("Ingrese el nombre completo.")

    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        u = conn.execute("SELECT username, nombre_completo, activo FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        if u is None:
            raise ValueError("El usuario no existe.")
        era_admin = conn.execute("SELECT 1 FROM usuario_roles WHERE usuario_id = ? AND rol = 'Admin'",
                                 (user_id,)).fetchone() is not None
        if user_id == admin["id"] and not activo:
            raise ValueError("No puede desactivar su propia cuenta. Pida a otro administrador que lo haga.")
        if era_admin and bool(u["activo"]) and not activo and _admins_activos_distintos(conn, user_id) == 0:
            raise ValueError("Debe existir al menos un administrador activo en el sistema.")

        conn.execute("UPDATE usuarios SET nombre_completo = ?, activo = ? WHERE id = ?", (nombre, int(activo), user_id))
        if bool(u["activo"]) != bool(activo):
            registrar_auditoria(conn, admin["id"], "USUARIO_ACTIVADO" if activo else "USUARIO_DESACTIVADO",
                                u["username"])
        if u["nombre_completo"] != nombre:
            registrar_auditoria(conn, admin["id"], "NOMBRE_CAMBIADO", u["username"],
                                f"{u['nombre_completo']} → {nombre}")


def asignar_roles(admin: dict, *, user_id: int, roles: list[str]) -> None:
    """
    Reemplaza el conjunto de roles de un usuario (uno o varios). Salvaguardas:
      * al menos un rol por usuario;
      * nadie puede quitarse su propio rol de Admin;
      * siempre debe quedar al menos un administrador activo.
    """
    exigir_admin(admin)
    pedidos = list(roles or [])
    nuevos = permisos.normalizar_roles(pedidos)
    if not nuevos:
        raise ValueError("El usuario debe tener al menos un rol.")
    if len(nuevos) != len({str(r).strip() for r in pedidos}):
        raise ValueError("Hay un rol inválido. Roles válidos: " + ", ".join(permisos.ROLES) + ".")

    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        u = conn.execute("SELECT username, activo FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        if u is None:
            raise ValueError("El usuario no existe.")
        actuales = permisos.normalizar_roles(
            [f["rol"] for f in conn.execute("SELECT rol FROM usuario_roles WHERE usuario_id = ?", (user_id,))])
        if actuales == nuevos:
            return
        pierde_admin = "Admin" in actuales and "Admin" not in nuevos
        if pierde_admin and user_id == admin["id"]:
            raise ValueError("No puede quitarse su propio rol de Admin. Pida a otro administrador que lo haga.")
        if pierde_admin and bool(u["activo"]) and _admins_activos_distintos(conn, user_id) == 0:
            raise ValueError("Debe existir al menos un administrador activo en el sistema.")

        marcas = ",".join("?" for _ in nuevos)
        conn.execute(f"DELETE FROM usuario_roles WHERE usuario_id = ? AND rol NOT IN ({marcas})", (user_id, *nuevos))
        conn.executemany("INSERT OR IGNORE INTO usuario_roles (usuario_id, rol, asignado_por) VALUES (?, ?, ?)",
                         [(user_id, r, admin["id"]) for r in nuevos])
        _sincronizar_rol_legacy(conn, user_id)
        registrar_auditoria(conn, admin["id"], "ROLES_CAMBIADOS", u["username"],
                            f"{' + '.join(actuales) or '—'} → {' + '.join(nuevos)}")


def restablecer_password(admin: dict, *, user_id: int, nueva_password: str) -> None:
    """El administrador define una contraseña nueva para otro usuario (se guarda con bcrypt)."""
    exigir_admin(admin)
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        u = conn.execute("SELECT username FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        if u is None:
            raise ValueError("El usuario no existe.")
        if user_id == admin["id"]:
            raise ValueError("Para cambiar su propia contraseña use la sección 'Mi cuenta'.")
        validar_password(nueva_password, u["username"])
        conn.execute("UPDATE usuarios SET password_hash = ? WHERE id = ?", (db.hash_password(nueva_password), user_id))
        registrar_auditoria(conn, admin["id"], "PASSWORD_RESTABLECIDA", u["username"])


def cambiar_mi_password(usuario: dict, *, actual: str, nueva: str, confirmacion: str) -> None:
    """Cambio de contraseña propio (cualquier rol). Exige la contraseña actual."""
    fila = db.fetch_one("SELECT username, password_hash, activo FROM usuarios WHERE id = ?", (usuario.get("id"),))
    if fila is None or not fila["activo"]:
        raise PermissionError("Su cuenta no está activa.")
    if not db.verify_password(actual or "", fila["password_hash"]):
        raise ValueError("La contraseña actual es incorrecta.")
    if nueva != confirmacion:
        raise ValueError("La nueva contraseña y su confirmación no coinciden.")
    if nueva == actual:
        raise ValueError("La nueva contraseña debe ser distinta de la actual.")
    validar_password(nueva, fila["username"])
    with db.db_session() as conn:
        conn.execute("UPDATE usuarios SET password_hash = ? WHERE id = ?", (db.hash_password(nueva), usuario["id"]))
        registrar_auditoria(conn, usuario["id"], "PASSWORD_PROPIA", fila["username"])


# ---------------------------------------------------------------------------
# SISTEMA: ESTADÍSTICAS, INTEGRIDAD Y RESPALDO
# ---------------------------------------------------------------------------
def estadisticas_sistema() -> dict[str, Any]:
    tablas = ["usuarios", "productos", "lotes_inventario", "ordenes_compra", "facturas",
              "movimientos_inventario", "oc_detalle", "documentos_adjuntos", "auditoria_admin"]
    conteos = {}
    for t in tablas:   # nombres fijos (constantes), no provienen del usuario
        try:
            conteos[t] = db.fetch_one(f"SELECT COUNT(*) FROM {t}")[0]
        except sqlite3.OperationalError:
            conteos[t] = 0
    tam = os.path.getsize(db.DB_PATH) / (1024 * 1024) if db.DB_PATH.exists() else 0.0
    return {"conteos": conteos, "tamano_mb": round(tam, 2), "ruta": str(db.DB_PATH)}


def verificar_integridad() -> tuple[bool, str]:
    conn = db.get_connection()
    try:
        resultado = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    if resultado == "ok" and not fk:
        return True, "La base de datos está íntegra (integrity_check y foreign_key_check sin errores)."
    return False, f"integrity_check: {resultado}; violaciones de llaves foráneas: {len(fk)}"


def crear_respaldo(admin: dict) -> tuple[bytes, str]:
    """Copia consistente de la BD (API de respaldo de SQLite, segura con modo WAL). Devuelve (bytes, nombre)."""
    exigir_admin(admin)
    destino = None
    try:
        fd, destino = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        origen = db.get_connection()
        copia = sqlite3.connect(destino)
        try:
            origen.backup(copia)
        finally:
            copia.close()
            origen.close()
        with open(destino, "rb") as f:
            datos = f.read()
        with db.db_session() as conn:
            registrar_auditoria(conn, admin["id"], "RESPALDO_GENERADO", "joma_inventory.db",
                                f"{len(datos) / 1024:.0f} KB")
        return datos, f"joma_inventory_respaldo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    finally:
        if destino and os.path.exists(destino):
            os.remove(destino)
