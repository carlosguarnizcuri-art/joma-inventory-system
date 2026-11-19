"""
database.py - Capa de datos de JOMA S.A.C. (SQLite)

Responsabilidades:
  * Conexión segura a SQLite (claves foráneas, WAL para 3 usuarios concurrentes).
  * Creación del esquema relacional (5 tablas + índices).
  * Inicialización automática con usuarios por defecto y datos de prueba.
  * Funciones de autenticación y gestión básica de usuarios (bcrypt).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

import bcrypt
import pandas as pd

# ---------------------------------------------------------------------------
# CONFIGURACIÓN DE RUTAS
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "joma_inventory.db"
DOCS_DIR = BASE_DIR / "data" / "documentos"   # Carpeta raíz de documentos (Módulo 3)

ROLES_VALIDOS = ("Admin", "Lector")


# ---------------------------------------------------------------------------
# CONEXIÓN
# ---------------------------------------------------------------------------
def get_connection() -> sqlite3.Connection:
    """Crea una conexión nueva. Streamlit ejecuta en hilos distintos, por eso
    NO se comparte una conexión global: cada operación abre y cierra la suya."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row            # Acceso por nombre de columna
    conn.execute("PRAGMA foreign_keys = ON")  # Integridad referencial
    conn.execute("PRAGMA journal_mode = WAL") # Mejor concurrencia lectura/escritura
    return conn


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    """Context manager transaccional: commit si todo va bien, rollback si falla."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_df(query: str, params: tuple | dict = ()) -> pd.DataFrame:
    """Ejecuta un SELECT y devuelve un DataFrame de pandas."""
    conn = get_connection()
    try:
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def fetch_one(query: str, params: tuple | dict = ()) -> Optional[sqlite3.Row]:
    """Ejecuta un SELECT y devuelve la primera fila (o None)."""
    conn = get_connection()
    try:
        return conn.execute(query, params).fetchone()
    finally:
        conn.close()


def execute(query: str, params: tuple | dict = ()) -> int:
    """Ejecuta INSERT/UPDATE/DELETE y devuelve el lastrowid."""
    with db_session() as conn:
        cur = conn.execute(query, params)
        return cur.lastrowid


# ---------------------------------------------------------------------------
# ESQUEMA
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
-- ============================ USUARIOS ============================
CREATE TABLE IF NOT EXISTS usuarios (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT    NOT NULL UNIQUE,
    password_hash    TEXT    NOT NULL,
    nombre_completo  TEXT    NOT NULL,
    rol              TEXT    NOT NULL CHECK (rol IN ('Admin', 'Lector')),
    activo           INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1)),
    creado_en        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    ultimo_acceso    TEXT
);

-- ============================ PRODUCTOS ===========================
CREATE TABLE IF NOT EXISTS productos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo         TEXT    NOT NULL UNIQUE,
    nombre         TEXT    NOT NULL,
    descripcion    TEXT,
    categoria      TEXT,
    unidad_medida  TEXT    NOT NULL DEFAULT 'UND',
    stock_minimo   REAL    NOT NULL DEFAULT 0 CHECK (stock_minimo >= 0),
    precio_costo   REAL    NOT NULL DEFAULT 0 CHECK (precio_costo >= 0),
    precio_venta   REAL    NOT NULL DEFAULT 0 CHECK (precio_venta >= 0),
    activo         INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1)),
    creado_en      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ========================= ORDENES DE COMPRA ======================
CREATE TABLE IF NOT EXISTS ordenes_compra (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_oc               TEXT    NOT NULL UNIQUE,
    proveedor               TEXT    NOT NULL,
    fecha_emision           TEXT    NOT NULL,
    fecha_entrega_estimada  TEXT,
    monto_total             REAL    NOT NULL DEFAULT 0 CHECK (monto_total >= 0),
    moneda                  TEXT    NOT NULL DEFAULT 'PEN' CHECK (moneda IN ('PEN', 'USD')),
    estado                  TEXT    NOT NULL DEFAULT 'Abierto' CHECK (estado IN ('Abierto', 'Cerrado')),
    ruta_carpeta            TEXT,            -- Carpeta local con los documentos de la OC
    observaciones           TEXT,
    creado_por              INTEGER REFERENCES usuarios(id),
    creado_en               TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ============================= FACTURAS ===========================
CREATE TABLE IF NOT EXISTS facturas (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_factura          TEXT    NOT NULL,
    proveedor               TEXT    NOT NULL,
    orden_compra_id         INTEGER REFERENCES ordenes_compra(id) ON DELETE SET NULL,
    fecha_emision           TEXT    NOT NULL,
    fecha_vencimiento_pago  TEXT,
    monto_total             REAL    NOT NULL DEFAULT 0 CHECK (monto_total >= 0),
    moneda                  TEXT    NOT NULL DEFAULT 'PEN' CHECK (moneda IN ('PEN', 'USD')),
    estado                  TEXT    NOT NULL DEFAULT 'Abierto' CHECK (estado IN ('Abierto', 'Cerrado')),
    ruta_documento          TEXT,            -- Ruta local del PDF/imagen de la factura
    observaciones           TEXT,
    creado_por              INTEGER REFERENCES usuarios(id),
    creado_en               TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (numero_factura, proveedor)
);

-- ========================= LOTES DE INVENTARIO ====================
CREATE TABLE IF NOT EXISTS lotes_inventario (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    producto_id        INTEGER NOT NULL REFERENCES productos(id) ON DELETE RESTRICT,
    numero_lote        TEXT    NOT NULL,
    fecha_ingreso      TEXT    NOT NULL,
    fecha_vencimiento  TEXT,                 -- NULL = producto no perecible
    cantidad_inicial   REAL    NOT NULL CHECK (cantidad_inicial >= 0),
    cantidad_actual    REAL    NOT NULL CHECK (cantidad_actual >= 0),
    orden_compra_id    INTEGER REFERENCES ordenes_compra(id) ON DELETE SET NULL,
    factura_id         INTEGER REFERENCES facturas(id)       ON DELETE SET NULL,
    ubicacion          TEXT,
    activo             INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1)),
    creado_en          TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (producto_id, numero_lote)
);

-- Índices: el de vencimiento acelera la consulta FEFO (ORDER BY fecha_vencimiento)
CREATE INDEX IF NOT EXISTS idx_lotes_fefo     ON lotes_inventario (producto_id, fecha_vencimiento);
CREATE INDEX IF NOT EXISTS idx_lotes_venc     ON lotes_inventario (fecha_vencimiento);
CREATE INDEX IF NOT EXISTS idx_oc_estado      ON ordenes_compra   (estado);
CREATE INDEX IF NOT EXISTS idx_facturas_estado ON facturas        (estado);

-- ====================== ROLES MÚLTIPLES POR USUARIO (Fase 4) ==========
-- Catálogo de roles y tabla intermedia usuarios <-> roles (relación muchos a muchos).
-- La columna legacy usuarios.rol se conserva ('Admin' si tiene ese rol, 'Lector' en otro caso)
-- para no romper instalaciones anteriores; la fuente de verdad son estas dos tablas.
CREATE TABLE IF NOT EXISTS roles (
    rol          TEXT PRIMARY KEY,
    descripcion  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usuario_roles (
    usuario_id    INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    rol           TEXT    NOT NULL REFERENCES roles(rol)    ON UPDATE CASCADE ON DELETE RESTRICT,
    asignado_por  INTEGER REFERENCES usuarios(id),
    asignado_en   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (usuario_id, rol)
);
CREATE INDEX IF NOT EXISTS idx_usuario_roles_rol ON usuario_roles (rol);
"""


def crear_esquema() -> None:
    """Crea todas las tablas e índices si no existen (idempotente)."""
    with db_session() as conn:
        conn.executescript(SCHEMA_SQL)


# ---------------------------------------------------------------------------
# SEGURIDAD: HASH DE CONTRASEÑAS (bcrypt)
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Genera un hash bcrypt (con sal aleatoria) de la contraseña."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash) -> bool:
    """Verifica una contraseña contra su hash bcrypt."""
    if not password or not password_hash:
        return False
    pw_bytes = password.encode("utf-8") if isinstance(password, str) else password
    hash_bytes = password_hash.encode("utf-8") if isinstance(password_hash, str) else password_hash
    try:
        return bcrypt.checkpw(pw_bytes, hash_bytes)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# USUARIOS
# ---------------------------------------------------------------------------
def autenticar_usuario(username: str, password: str) -> Optional[dict[str, Any]]:
    """Valida credenciales. Devuelve dict con datos del usuario o None si falla.
    Nunca devuelve el hash de la contraseña."""
    username = (username or "").strip().lower()
    if not username or not password:
        return None

    fila = fetch_one(
        "SELECT id, username, password_hash, nombre_completo, rol "
        "FROM usuarios WHERE username = ? AND activo = 1",
        (username,),
    )
    if fila is None or not verify_password(password, fila["password_hash"]):
        return None

    execute(
        "UPDATE usuarios SET ultimo_acceso = ? WHERE id = ?",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), fila["id"]),
    )
    roles = roles_de_usuario(fila["id"]) or [fila["rol"]]
    return {
        "id": fila["id"],
        "username": fila["username"],
        "nombre_completo": fila["nombre_completo"],
        "rol": fila["rol"],          # legacy: 'Admin' o 'Lector'
        "roles": roles,              # lista completa (un usuario puede tener varios)
    }


def usa_password_por_defecto(username: str) -> bool:
    """True si el usuario aún conserva la contraseña inicial de fábrica."""
    defaults = {"admin": "admin123", "lector": "lector123"}
    fila = fetch_one("SELECT password_hash FROM usuarios WHERE username = ?", (username,))
    if fila is None or username not in defaults:
        return False
    return verify_password(defaults[username], fila["password_hash"])


def crear_usuario(username: str, password: str, nombre_completo: str, rol: str) -> int:
    """Crea un usuario nuevo (se usará desde el módulo de Administración)."""
    if rol not in ROLES_VALIDOS:
        raise ValueError(f"Rol inválido: {rol}. Use uno de {ROLES_VALIDOS}.")
    if len(password) < 6:
        raise ValueError("La contraseña debe tener al menos 6 caracteres.")
    with db_session() as conn:
        cur = conn.execute(
            "INSERT INTO usuarios (username, password_hash, nombre_completo, rol) VALUES (?, ?, ?, ?)",
            (username.strip().lower(), hash_password(password), nombre_completo.strip(), rol),
        )
        sembrar_roles_y_migrar(conn)
        return cur.lastrowid


def cambiar_password(user_id: int, nueva_password: str) -> None:
    """Actualiza la contraseña de un usuario."""
    if len(nueva_password) < 6:
        raise ValueError("La contraseña debe tener al menos 6 caracteres.")
    execute(
        "UPDATE usuarios SET password_hash = ? WHERE id = ?",
        (hash_password(nueva_password), user_id),
    )


def listar_usuarios() -> pd.DataFrame:
    """Lista usuarios (sin hash) para el panel de administración."""
    return fetch_df(
        "SELECT id, username, nombre_completo, rol, activo, creado_en, ultimo_acceso "
        "FROM usuarios ORDER BY id"
    )


# ---------------------------------------------------------------------------
# DATOS DE PRUEBA (solo se cargan si las tablas están vacías)
# ---------------------------------------------------------------------------
def _fecha(offset_dias: int) -> str:
    """Fecha relativa a hoy en formato ISO (YYYY-MM-DD)."""
    return (date.today() + timedelta(days=offset_dias)).isoformat()


def _cargar_usuarios_iniciales(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] > 0:
        return
    conn.executemany(
        "INSERT INTO usuarios (username, password_hash, nombre_completo, rol) VALUES (?, ?, ?, ?)",
        [
            ("admin", hash_password("admin123"), "Administrador JOMA", "Admin"),
            ("lector", hash_password("lector123"), "Usuario Consulta", "Lector"),
        ],
    )


def _cargar_datos_prueba(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM productos").fetchone()[0] > 0:
        return

    # --- Productos: (codigo, nombre, categoria, unidad, stock_minimo, costo, venta)
    productos = [
        ("PRD-001", "Aceite Vegetal 1L",           "Abarrotes",  "UND",   50,  6.50,  8.90),
        ("PRD-002", "Arroz Extra Saco 50 kg",      "Abarrotes",  "SACO",  20, 135.00, 168.00),
        ("PRD-003", "Leche Evaporada Caja x24",    "Lácteos",    "CAJA",  30, 62.00,  79.00),
        ("PRD-004", "Atún en Lata 170 g",          "Conservas",  "UND",  100,  3.80,  5.50),
        ("PRD-005", "Detergente en Polvo 2 kg",    "Limpieza",   "UND",   25, 14.20, 19.90),
        ("PRD-006", "Galletas de Soda Paquete x6", "Snacks",     "PAQ",   40,  2.90,  4.50),
    ]
    conn.executemany(
        "INSERT INTO productos (codigo, nombre, categoria, unidad_medida, stock_minimo, "
        "precio_costo, precio_venta) VALUES (?, ?, ?, ?, ?, ?, ?)",
        productos,
    )

    admin_id = conn.execute("SELECT id FROM usuarios WHERE username = 'admin'").fetchone()[0]

    # --- Órdenes de compra
    conn.executemany(
        "INSERT INTO ordenes_compra (numero_oc, proveedor, fecha_emision, fecha_entrega_estimada, "
        "monto_total, moneda, estado, ruta_carpeta, creado_por) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("OC-2026-001", "Alicorp S.A.A.",         _fecha(-60), _fecha(-50), 12500.00, "PEN", "Cerrado",
             str(DOCS_DIR / "OC-2026-001"), admin_id),
            ("OC-2026-002", "Gloria S.A.",            _fecha(-20), _fecha(-10),  8300.00, "PEN", "Abierto",
             str(DOCS_DIR / "OC-2026-002"), admin_id),
            ("OC-2026-003", "Distribuidora Andina",   _fecha(-5),  _fecha(10),   4720.50, "PEN", "Abierto",
             str(DOCS_DIR / "OC-2026-003"), admin_id),
        ],
    )
    oc = {r["numero_oc"]: r["id"] for r in conn.execute("SELECT id, numero_oc FROM ordenes_compra")}

    # --- Facturas
    conn.executemany(
        "INSERT INTO facturas (numero_factura, proveedor, orden_compra_id, fecha_emision, "
        "fecha_vencimiento_pago, monto_total, moneda, estado, creado_por) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("F001-000123", "Alicorp S.A.A.", oc["OC-2026-001"], _fecha(-48), _fecha(-18), 12500.00, "PEN", "Cerrado", admin_id),
            ("F002-000456", "Gloria S.A.",    oc["OC-2026-002"], _fecha(-9),  _fecha(21),   8300.00, "PEN", "Abierto", admin_id),
        ],
    )
    fac = {r["numero_factura"]: r["id"] for r in conn.execute("SELECT id, numero_factura FROM facturas")}

    # --- Lotes: variedad de escenarios (por vencer, vencido, stock bajo, no perecible)
    prod = {r["codigo"]: r["id"] for r in conn.execute("SELECT id, codigo FROM productos")}
    f1, f2 = fac["F001-000123"], fac["F002-000456"]
    o1, o2 = oc["OC-2026-001"], oc["OC-2026-002"]
    lotes = [
        # (producto, lote, ingreso, vencimiento, inicial, actual, oc, factura, ubicación)
        (prod["PRD-001"], "L2606-A", _fecha(-90), _fecha(20),   40,  30, o1, f1, "A-01"),   # por vencer
        (prod["PRD-001"], "L2609-B", _fecha(-50), _fecha(200), 120, 100, o1, f1, "A-02"),
        (prod["PRD-002"], "L-AR01",  _fecha(-50), _fecha(300),  60,  45, o1, f1, "B-01"),
        (prod["PRD-003"], "L-LE01",  _fecha(-30), _fecha(12),   50,  20, o2, f2, "C-01"),   # por vencer
        (prod["PRD-003"], "L-LE02",  _fecha(-9),  _fecha(150),  80,  80, o2, f2, "C-02"),
        (prod["PRD-004"], "L-AT01",  _fecha(-120), _fecha(-5),  60,  60, o1, f1, "D-01"),   # VENCIDO
        (prod["PRD-004"], "L-AT02",  _fecha(-50), _fecha(400), 200,  40, o1, f1, "D-02"),   # stock bajo
        (prod["PRD-005"], "L-DE01",  _fecha(-50), None,        100,  90, o1, f1, "E-01"),   # no perecible
        (prod["PRD-006"], "L-GA01",  _fecha(-40), _fecha(45),  150, 120, None, None, "F-01"),
    ]
    conn.executemany(
        "INSERT INTO lotes_inventario (producto_id, numero_lote, fecha_ingreso, fecha_vencimiento, "
        "cantidad_inicial, cantidad_actual, orden_compra_id, factura_id, ubicacion) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        lotes,
    )


# ---------------------------------------------------------------------------
# INICIALIZACIÓN PRINCIPAL
# ---------------------------------------------------------------------------
def sembrar_roles_y_migrar(conn: sqlite3.Connection) -> int:
    """
    Idempotente. (1) Carga/actualiza el catálogo de roles. (2) MIGRACIÓN: todo usuario que aún no tenga
    filas en usuario_roles recibe, como rol inicial, el que tenía en la columna legacy usuarios.rol
    (sus credenciales bcrypt no se tocan). Devuelve cuántos usuarios se migraron.
    """
    from modulos.permisos import ROLES          # import diferido: permisos importa este módulo
    conn.executemany(
        "INSERT INTO roles (rol, descripcion) VALUES (?, ?) "
        "ON CONFLICT(rol) DO UPDATE SET descripcion = excluded.descripcion", list(ROLES.items()))
    cur = conn.execute(
        """INSERT INTO usuario_roles (usuario_id, rol)
           SELECT u.id, u.rol FROM usuarios u
           WHERE NOT EXISTS (SELECT 1 FROM usuario_roles ur WHERE ur.usuario_id = u.id)""")
    return cur.rowcount


def roles_de_usuario(user_id: int) -> list[str]:
    """Roles asignados a un usuario (sin validar si está activo)."""
    conn = get_connection()
    try:
        return [f["rol"] for f in conn.execute(
            "SELECT rol FROM usuario_roles WHERE usuario_id = ? ORDER BY rol", (user_id,))]
    finally:
        conn.close()


def init_db() -> None:
    """Punto de entrada: crea carpetas, esquema, usuarios y datos de prueba.
    Es seguro llamarla en cada arranque de la app (idempotente)."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    crear_esquema()
    with db_session() as conn:
        _cargar_usuarios_iniciales(conn)
        _cargar_datos_prueba(conn)
        sembrar_roles_y_migrar(conn)


# ---------------------------------------------------------------------------
# CONSULTAS DE RESUMEN (usadas por la pantalla de Inicio)
# ---------------------------------------------------------------------------
def get_resumen_general(dias_alerta: int = 30) -> dict[str, int | float]:
    """Indicadores clave para la pantalla de bienvenida."""
    hoy = date.today().isoformat()
    limite = (date.today() + timedelta(days=dias_alerta)).isoformat()

    def _scalar(q: str, p: tuple = ()) -> Any:
        fila = fetch_one(q, p)
        return fila[0] if fila and fila[0] is not None else 0

    return {
        "productos_activos": _scalar("SELECT COUNT(*) FROM productos WHERE activo = 1"),
        "lotes_activos": _scalar("SELECT COUNT(*) FROM lotes_inventario WHERE activo = 1 AND cantidad_actual > 0"),
        "unidades_vigentes": _scalar(
            "SELECT SUM(cantidad_actual) FROM lotes_inventario WHERE activo = 1 "
            "AND (fecha_vencimiento IS NULL OR fecha_vencimiento >= ?)", (hoy,)),
        "oc_abiertas": _scalar("SELECT COUNT(*) FROM ordenes_compra WHERE estado = 'Abierto'"),
        "facturas_abiertas": _scalar("SELECT COUNT(*) FROM facturas WHERE estado = 'Abierto'"),
        "lotes_por_vencer": _scalar(
            "SELECT COUNT(*) FROM lotes_inventario WHERE activo = 1 AND cantidad_actual > 0 "
            "AND fecha_vencimiento BETWEEN ? AND ?", (hoy, limite)),
        "lotes_vencidos": _scalar(
            "SELECT COUNT(*) FROM lotes_inventario WHERE activo = 1 AND cantidad_actual > 0 "
            "AND fecha_vencimiento < ?", (hoy,)),
        "productos_stock_bajo": _scalar(
            """SELECT COUNT(*) FROM (
                   SELECT p.id, p.stock_minimo, COALESCE(SUM(l.cantidad_actual), 0) AS stock
                   FROM productos p
                   LEFT JOIN lotes_inventario l
                          ON l.producto_id = p.id AND l.activo = 1
                         AND (l.fecha_vencimiento IS NULL OR l.fecha_vencimiento >= ?)
                   WHERE p.activo = 1
                   GROUP BY p.id
                   HAVING stock < p.stock_minimo)""", (hoy,)),
    }


def get_lotes_criticos(dias_alerta: int = 30) -> pd.DataFrame:
    """Lotes vencidos o próximos a vencer, ordenados por criticidad (FEFO)."""
    limite = (date.today() + timedelta(days=dias_alerta)).isoformat()
    df = fetch_df(
        """SELECT p.codigo, p.nombre AS producto, l.numero_lote AS lote,
                  l.fecha_vencimiento, l.cantidad_actual AS cantidad, l.ubicacion
           FROM lotes_inventario l
           JOIN productos p ON p.id = l.producto_id
           WHERE l.activo = 1 AND l.cantidad_actual > 0
             AND l.fecha_vencimiento IS NOT NULL AND l.fecha_vencimiento <= ?
           ORDER BY l.fecha_vencimiento ASC""",
        (limite,),
    )
    if not df.empty:
        hoy = pd.Timestamp(date.today())
        df["dias_restantes"] = (pd.to_datetime(df["fecha_vencimiento"]) - hoy).dt.days
        df["estado"] = df["dias_restantes"].apply(
            lambda d: "🔴 Vencido" if d < 0 else ("🟠 Crítico (≤15 d)" if d <= 15 else "🟡 Por vencer")
        )
    return df


if __name__ == "__main__":
    # Permite inicializar la BD manualmente:  python database.py
    init_db()
    print(f"Base de datos lista en: {DB_PATH}")
