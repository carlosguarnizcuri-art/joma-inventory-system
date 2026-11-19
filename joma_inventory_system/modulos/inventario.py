"""
modulos/inventario.py - MÓDULO 2: Gestión de Inventario (JOMA S.A.C.)

Pestañas:
  1. "Consulta de Stock y Lotes"  -> visible para Admin y Lector (solo lectura).
  2. "Gestión / Entradas / Salidas" -> SOLO Admin (formularios st.form -> SQLite).

Lógica clave:
  * FEFO: las salidas descuentan primero del lote que vence antes. Los lotes
    vencidos NO participan en las salidas; los no perecibles (sin fecha de
    vencimiento) se despachan al final.
  * Stock bajo: stock VIGENTE (sin vencidos) comparado con productos.stock_minimo.
  * Seguridad: el rol se valida en la interfaz Y de nuevo contra la BD en cada
    función de escritura (_exigir_admin), por lo que un Lector no puede escribir
    aunque manipule el estado de la sesión.

Exportación CSV: separador ';' + codificación utf-8-sig (compatible con Excel en español).

Nota: este módulo crea (si no existe) la tabla `movimientos_inventario`, usada
como historial (kardex) de entradas, salidas y ajustes.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd
import streamlit as st

import database as db
from modulos import clientes_db, identidad

# ---------------------------------------------------------------------------
# CONSTANTES
# ---------------------------------------------------------------------------
DIAS_ALERTA = 30          # Un lote "por vencer" vence en <= 30 días
DIAS_CRITICO = 15         # Un lote "crítico" vence en <= 15 días
MARGEN_STOCK = 1.25       # "Cerca del mínimo" = hasta 25 % por encima del mínimo
DECIMALES = 3             # Precisión de cantidades (evita errores de coma flotante)

UNIDADES = ["UND", "CAJA", "SACO", "PAQ", "KG", "LT", "DOC", "BOLSA"]
# Motivos de salida que exigen identificar al cliente (RUC/DNI, dirección)
MOTIVOS_REQUIEREN_CLIENTE = {"Venta / Despacho"}
MOTIVOS_SALIDA = ["Venta / Despacho", "Uso interno", "Merma / Daño", "Muestra", "Devolución a proveedor", "Otro"]
MOTIVOS_AJUSTE = ["Producto vencido", "Merma / Daño", "Error de conteo (inventario físico)", "Otro"]

ESTADOS_STOCK_ALERTA = ("⛔ Sin stock", "🔴 Stock bajo")

SQL_MOVIMIENTOS = """
CREATE TABLE IF NOT EXISTS movimientos_inventario (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lote_id      INTEGER REFERENCES lotes_inventario(id) ON DELETE SET NULL,
    producto_id  INTEGER NOT NULL REFERENCES productos(id),
    numero_lote  TEXT,
    tipo         TEXT    NOT NULL CHECK (tipo IN ('ENTRADA', 'SALIDA', 'AJUSTE')),
    cantidad     REAL    NOT NULL,           -- con signo: ENTRADA (+), SALIDA (-), AJUSTE (+/-)
    motivo       TEXT,
    referencia   TEXT,
    usuario_id   INTEGER REFERENCES usuarios(id),
    fecha        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_mov_fecha    ON movimientos_inventario (fecha);
CREATE INDEX IF NOT EXISTS idx_mov_producto ON movimientos_inventario (producto_id);
"""


@st.cache_resource(show_spinner=False)
def _asegurar_tabla_movimientos() -> bool:
    """Crea la tabla de movimientos una sola vez por proceso (idempotente)."""
    with db.db_session() as conn:
        conn.executescript(SQL_MOVIMIENTOS)
    clientes_db.asegurar_esquema()   # tabla clientes + columnas nuevas del kardex (migración segura)
    return True


# ===========================================================================
# 1. CAPA DE DATOS (lectura)
# ===========================================================================
def estado_stock(stock_vigente: float, stock_minimo: float) -> str:
    """Clasifica el nivel de stock de un producto respecto a su mínimo."""
    if stock_vigente <= 0:
        return "⛔ Sin stock"
    if stock_vigente < stock_minimo:
        return "🔴 Stock bajo"
    if stock_minimo > 0 and stock_vigente <= stock_minimo * MARGEN_STOCK:
        return "🟡 Cerca del mínimo"
    return "🟢 OK"


def estado_lote(cantidad: float, dias: float) -> str:
    """Clasifica un lote según su cantidad y días restantes para vencer."""
    if cantidad <= 0:
        return "⚫ Agotado"
    if pd.isna(dias):
        return "⚪ No perecible"
    if dias < 0:
        return "🔴 Vencido"
    if dias <= DIAS_CRITICO:
        return "🟠 Crítico"
    if dias <= DIAS_ALERTA:
        return "🟡 Por vencer"
    return "🟢 Vigente"


def cargar_stock_productos() -> pd.DataFrame:
    """Stock consolidado por producto. 'Vigente' excluye lotes vencidos."""
    sql = """
        SELECT p.id, p.codigo, p.nombre, p.categoria, p.unidad_medida AS unidad,
               p.stock_minimo, p.precio_costo,
               COALESCE(SUM(CASE WHEN l.activo = 1
                                  AND (l.fecha_vencimiento IS NULL OR l.fecha_vencimiento >= :hoy)
                                 THEN l.cantidad_actual END), 0)            AS stock_vigente,
               COALESCE(SUM(CASE WHEN l.activo = 1 AND l.fecha_vencimiento < :hoy
                                 THEN l.cantidad_actual END), 0)            AS stock_vencido,
               MIN(CASE WHEN l.activo = 1 AND l.cantidad_actual > 0
                         AND l.fecha_vencimiento >= :hoy
                        THEN l.fecha_vencimiento END)                       AS prox_vencimiento
        FROM productos p
        LEFT JOIN lotes_inventario l ON l.producto_id = p.id
        WHERE p.activo = 1
        GROUP BY p.id
        ORDER BY p.nombre
    """
    df = db.fetch_df(sql, {"hoy": date.today().isoformat()})
    df["estado_stock"] = [estado_stock(s, m) for s, m in zip(df["stock_vigente"], df["stock_minimo"])]
    df["reponer"] = (df["stock_minimo"] - df["stock_vigente"]).clip(lower=0)
    df["valor_costo"] = df["stock_vigente"] * df["precio_costo"]
    return df


def cargar_lotes() -> pd.DataFrame:
    """Todos los lotes activos con días restantes, estado y prioridad FEFO."""
    sql = """
        SELECT l.id, l.producto_id, p.codigo, p.nombre AS producto, p.unidad_medida AS unidad,
               l.numero_lote AS lote, l.fecha_ingreso, l.fecha_vencimiento,
               l.cantidad_inicial, l.cantidad_actual, l.ubicacion,
               oc.numero_oc, f.numero_factura
        FROM lotes_inventario l
        JOIN productos p          ON p.id  = l.producto_id
        LEFT JOIN ordenes_compra oc ON oc.id = l.orden_compra_id
        LEFT JOIN facturas f        ON f.id  = l.factura_id
        WHERE l.activo = 1
        ORDER BY p.nombre, p.id, (l.fecha_vencimiento IS NULL), l.fecha_vencimiento,
                 l.fecha_ingreso, l.id
    """
    df = db.fetch_df(sql)
    venc = pd.to_datetime(df["fecha_vencimiento"])
    df["dias_restantes"] = (venc - pd.Timestamp(date.today())).dt.days
    df["estado"] = [estado_lote(c, d) for c, d in zip(df["cantidad_actual"], df["dias_restantes"])]

    # Prioridad FEFO: el DataFrame ya viene ordenado por vencimiento dentro de cada
    # producto, así que basta numerar los lotes elegibles (con stock y no vencidos).
    df["orden_fefo"] = float("nan")
    elegibles = (df["cantidad_actual"] > 0) & (df["dias_restantes"].isna() | (df["dias_restantes"] >= 0))
    if elegibles.any():
        df.loc[elegibles, "orden_fefo"] = df[elegibles].groupby("producto_id").cumcount() + 1
    return df


def cargar_productos(solo_activos: bool = True) -> pd.DataFrame:
    return db.fetch_df(
        "SELECT * FROM productos WHERE (:solo = 0 OR activo = 1) ORDER BY nombre",
        {"solo": int(solo_activos)},
    )


def cargar_movimientos(producto_id: Optional[int] = None, limite: int = 300) -> pd.DataFrame:
    return db.fetch_df(
        """SELECT m.fecha, m.tipo, p.codigo, p.nombre AS producto, m.numero_lote AS lote,
                  m.cantidad, m.motivo, m.referencia, m.guia_remision AS guia, m.cliente_nombre AS cliente,
                  m.cliente_documento AS cliente_doc, COALESCE(u.nombre_completo, '—') AS usuario
           FROM movimientos_inventario m
           JOIN productos p ON p.id = m.producto_id
           LEFT JOIN usuarios u ON u.id = m.usuario_id
           WHERE (:pid IS NULL OR m.producto_id = :pid)
           ORDER BY m.id DESC LIMIT :lim""",
        {"pid": producto_id, "lim": limite},
    )


# ===========================================================================
# 2. LÓGICA FEFO
# ===========================================================================
def calcular_plan_fefo(conn: sqlite3.Connection, producto_id: int, cantidad: float) -> tuple[list[dict], float]:
    """
    Calcula de qué lotes se despacharía 'cantidad' aplicando FEFO.

    Orden: vence primero -> sale primero; empate por fecha de ingreso (FIFO);
    los no perecibles (NULL) van al final. Excluye lotes vencidos y agotados.

    Devuelve (plan, faltante). 'faltante' > 0 indica stock vigente insuficiente.
    """
    filas = conn.execute(
        """SELECT id, numero_lote, fecha_vencimiento, cantidad_actual, ubicacion
           FROM lotes_inventario
           WHERE producto_id = ? AND activo = 1 AND cantidad_actual > 0
             AND (fecha_vencimiento IS NULL OR fecha_vencimiento >= ?)
           ORDER BY (fecha_vencimiento IS NULL), fecha_vencimiento ASC, fecha_ingreso ASC, id ASC""",
        (producto_id, date.today().isoformat()),
    ).fetchall()

    restante = round(float(cantidad), DECIMALES)
    plan: list[dict] = []
    for f in filas:
        if restante <= 0:
            break
        tomar = round(min(restante, f["cantidad_actual"]), DECIMALES)
        plan.append({
            "lote_id": f["id"],
            "lote": f["numero_lote"],
            "vence": f["fecha_vencimiento"],
            "disponible": f["cantidad_actual"],
            "tomar": tomar,
            "queda": round(f["cantidad_actual"] - tomar, DECIMALES),
            "ubicacion": f["ubicacion"],
        })
        restante = round(restante - tomar, DECIMALES)
    return plan, max(restante, 0.0)


def _plan_lectura(producto_id: int, cantidad: float) -> tuple[list[dict], float]:
    """Calcula el plan FEFO con una conexión de solo lectura (para previsualizar)."""
    conn = db.get_connection()
    try:
        return calcular_plan_fefo(conn, producto_id, cantidad)
    finally:
        conn.close()


def _plan_a_df(plan: list[dict]) -> pd.DataFrame:
    """Convierte el plan FEFO en una tabla amigable para mostrar."""
    df = pd.DataFrame(plan)
    if df.empty:
        return df
    df.insert(0, "prioridad", range(1, len(df) + 1))
    df["vence"] = pd.to_datetime(df["vence"])
    return df.drop(columns=["lote_id"])


CONFIG_PLAN = {
    "prioridad": st.column_config.NumberColumn("Prioridad", format="%d"),
    "lote": "Lote",
    "vence": st.column_config.DateColumn("Vence", format="DD/MM/YYYY"),
    "disponible": st.column_config.NumberColumn("Disponible", format="%.2f"),
    "tomar": st.column_config.NumberColumn("Se descuenta", format="%.2f"),
    "queda": st.column_config.NumberColumn("Quedará", format="%.2f"),
    "ubicacion": "Ubicación",
}


# ===========================================================================
# 3. CAPA DE DATOS (escritura) - todas exigen rol Admin validado contra la BD
# ===========================================================================
def _exigir_admin(usuario: dict[str, Any]) -> None:
    """Verifica en la BD (no solo en la sesión) que el usuario sea Admin activo."""
    fila = db.fetch_one("SELECT rol, activo FROM usuarios WHERE id = ?", (usuario.get("id"),))
    if fila is None or not fila["activo"] or fila["rol"] != "Admin":
        raise PermissionError("Acción permitida solo para usuarios con rol Admin.")


def _registrar_movimiento(conn: sqlite3.Connection, *, lote_id: Optional[int], producto_id: int,
                          numero_lote: str, tipo: str, cantidad: float, motivo: Optional[str],
                          referencia: Optional[str], usuario_id: int, guia_remision: Optional[str] = None,
                          cliente_id: Optional[int] = None, cliente_nombre: Optional[str] = None,
                          cliente_documento: Optional[str] = None, cliente_direccion: Optional[str] = None) -> None:
    conn.execute(
        """INSERT INTO movimientos_inventario
           (lote_id, producto_id, numero_lote, tipo, cantidad, motivo, referencia, usuario_id,
            guia_remision, cliente_id, cliente_nombre, cliente_documento, cliente_direccion)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (lote_id, producto_id, numero_lote, tipo, cantidad, motivo or None, referencia or None, usuario_id,
         guia_remision or None, cliente_id, cliente_nombre, cliente_documento, cliente_direccion),
    )


def registrar_entrada(usuario: dict, *, producto_id: int, numero_lote: str, cantidad: float,
                      fecha_ingreso: date, fecha_vencimiento: Optional[date], ubicacion: str,
                      orden_compra_id: Optional[int], factura_id: Optional[int]) -> int:
    """Registra el ingreso de un lote nuevo y su movimiento de ENTRADA."""
    _exigir_admin(usuario)

    numero_lote = (numero_lote or "").strip().upper()
    cantidad = round(float(cantidad), DECIMALES)
    hoy = date.today()
    if not numero_lote:
        raise ValueError("Ingrese el número de lote.")
    if cantidad <= 0:
        raise ValueError("La cantidad debe ser mayor que cero.")
    if fecha_ingreso > hoy:
        raise ValueError("La fecha de ingreso no puede ser futura.")
    if fecha_vencimiento is not None:
        if fecha_vencimiento < fecha_ingreso:
            raise ValueError("El vencimiento no puede ser anterior a la fecha de ingreso.")
        if fecha_vencimiento < hoy:
            raise ValueError("El lote ya está vencido; no se puede ingresar como stock disponible.")

    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")   # Bloquea escritura concurrente durante la operación
        prod = conn.execute("SELECT id FROM productos WHERE id = ? AND activo = 1", (producto_id,)).fetchone()
        if prod is None:
            raise ValueError("El producto no existe o está inactivo.")
        if conn.execute("SELECT 1 FROM lotes_inventario WHERE producto_id = ? AND numero_lote = ?",
                        (producto_id, numero_lote)).fetchone():
            raise ValueError(f"Ya existe el lote {numero_lote} para este producto. "
                             "Use 'Ajuste / Baja de lote' para corregir su cantidad.")
        cur = conn.execute(
            """INSERT INTO lotes_inventario
               (producto_id, numero_lote, fecha_ingreso, fecha_vencimiento, cantidad_inicial,
                cantidad_actual, orden_compra_id, factura_id, ubicacion)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (producto_id, numero_lote, fecha_ingreso.isoformat(),
             fecha_vencimiento.isoformat() if fecha_vencimiento else None,
             cantidad, cantidad, orden_compra_id, factura_id, (ubicacion or "").strip() or None),
        )
        lote_id = cur.lastrowid
        ref = None
        if orden_compra_id:
            fila = conn.execute("SELECT numero_oc FROM ordenes_compra WHERE id = ?", (orden_compra_id,)).fetchone()
            ref = fila["numero_oc"] if fila else None
        _registrar_movimiento(conn, lote_id=lote_id, producto_id=producto_id, numero_lote=numero_lote,
                              tipo="ENTRADA", cantidad=cantidad, motivo="Ingreso de mercadería",
                              referencia=ref, usuario_id=usuario["id"])
        return lote_id


def salida_fefo_en_transaccion(conn: sqlite3.Connection, *, usuario_id: int, producto_id: int, cantidad: float,
                               motivo: str, referencia: str, guia: str = "",
                               cliente: Optional[dict] = None,
                               ov_numero: str = "") -> list[dict]:
                               
    """
    Núcleo de la salida FEFO. NO abre ni cierra la transacción: el llamador debe haber ejecutado
    BEGIN IMMEDIATE (y hace commit/rollback). Así varias salidas pueden registrarse en UNA sola
    transacción (carga masiva, confirmación de despachos): o salen todas o no sale ninguna.
    'guia' ya debe venir normalizada y 'cliente' ser la instantánea de clientes_db.instantanea_cliente.
    """
    cantidad = round(float(cantidad), DECIMALES)
    plan, faltante = calcular_plan_fefo(conn, producto_id, cantidad)
    if faltante > 0:
        disponible = round(cantidad - faltante, DECIMALES)
        raise ValueError(f"Stock vigente insuficiente. Disponible: {disponible:g}; solicitado: {cantidad:g}.")
    for paso in plan:
        cur = conn.execute(
            """UPDATE lotes_inventario
               SET cantidad_actual = ROUND(cantidad_actual - ?, ?)
               WHERE id = ? AND cantidad_actual >= ?""",
            (paso["tomar"], DECIMALES, paso["lote_id"], paso["tomar"]),
        )
        if cur.rowcount != 1:
            raise ValueError("El stock cambió durante la operación. Vuelva a previsualizar la salida.")
        _registrar_movimiento(conn, lote_id=paso["lote_id"], producto_id=producto_id,
                              numero_lote=paso["lote"], tipo="SALIDA", cantidad=-paso["tomar"],
                              motivo=motivo, referencia=(referencia or "").strip(),
                              usuario_id=usuario_id, guia_remision=guia, **(cliente or {}))
    return plan


def registrar_salida_fefo(usuario: dict, *, producto_id: int, cantidad: float,
                          motivo: str, referencia: str, guia_remision: str = "",
                          cliente_id: Optional[int] = None) -> list[dict]:
    """
    Descuenta 'cantidad' del producto aplicando FEFO, de forma atómica.
    El plan se recalcula DENTRO de la transacción para evitar errores por cambios
    de stock hechos por otro usuario entre la previsualización y la confirmación.

    Fase 2: cada movimiento guarda la guía de remisión (formato SERIE-NÚMERO, opcional) y una
    INSTANTÁNEA del cliente (nombre, RUC/DNI y dirección). Para los motivos de
    MOTIVOS_REQUIEREN_CLIENTE (venta / despacho) el cliente es obligatorio.
    """
    _exigir_admin(usuario)
    cantidad = round(float(cantidad), DECIMALES)
    if cantidad <= 0:
        raise ValueError("La cantidad debe ser mayor que cero.")
    guia = identidad.normalizar_guia_remision(guia_remision)      # ValueError si el formato es inválido
    if motivo in MOTIVOS_REQUIEREN_CLIENTE and not cliente_id:
        raise ValueError("Para una venta o despacho seleccione el cliente "
                         "(si no existe, regístrelo en la sección 'Clientes').")

    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cliente = clientes_db.instantanea_cliente(conn, cliente_id)   # None si no hay cliente
        return salida_fefo_en_transaccion(conn, usuario_id=usuario["id"], producto_id=producto_id,
                                          cantidad=cantidad, motivo=motivo, referencia=referencia,
                                          guia=guia, cliente=cliente)


def ajustar_lote(usuario: dict, *, lote_id: int, nueva_cantidad: float, ubicacion: str,
                 motivo: str, baja_total: bool = False) -> None:
    """Corrige la cantidad (conteo físico) o da de baja un lote; actualiza ubicación."""
    _exigir_admin(usuario)
    nueva = 0.0 if baja_total else round(float(nueva_cantidad), DECIMALES)
    if nueva < 0:
        raise ValueError("La cantidad no puede ser negativa.")

    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lote = conn.execute(
            "SELECT id, producto_id, numero_lote, cantidad_actual FROM lotes_inventario WHERE id = ? AND activo = 1",
            (lote_id,),
        ).fetchone()
        if lote is None:
            raise ValueError("El lote no existe.")
        delta = round(nueva - lote["cantidad_actual"], DECIMALES)
        if delta != 0 and not (motivo or "").strip():
            raise ValueError("Indique el motivo del ajuste.")
        conn.execute("UPDATE lotes_inventario SET cantidad_actual = ?, ubicacion = ? WHERE id = ?",
                     (nueva, (ubicacion or "").strip() or None, lote_id))
        if delta != 0:
            _registrar_movimiento(conn, lote_id=lote_id, producto_id=lote["producto_id"],
                                  numero_lote=lote["numero_lote"], tipo="AJUSTE", cantidad=delta,
                                  motivo=motivo, referencia="Baja total" if baja_total else "Ajuste",
                                  usuario_id=usuario["id"])


def _validar_producto(nombre: str, stock_minimo: float, costo: float, venta: float) -> None:
    if not (nombre or "").strip():
        raise ValueError("El nombre del producto es obligatorio.")
    if stock_minimo < 0 or costo < 0 or venta < 0:
        raise ValueError("Stock mínimo y precios no pueden ser negativos.")


def crear_producto(usuario: dict, *, codigo: str, nombre: str, categoria: str, unidad: str,
                   stock_minimo: float, costo: float, venta: float, descripcion: str) -> int:
    _exigir_admin(usuario)
    codigo = (codigo or "").strip().upper()
    if not codigo:
        raise ValueError("El código del producto es obligatorio.")
    _validar_producto(nombre, stock_minimo, costo, venta)
    try:
        with db.db_session() as conn:
            cur = conn.execute(
                """INSERT INTO productos (codigo, nombre, descripcion, categoria, unidad_medida,
                                          stock_minimo, precio_costo, precio_venta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (codigo, nombre.strip(), (descripcion or "").strip() or None,
                 (categoria or "").strip() or None, unidad, stock_minimo, costo, venta),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"Ya existe un producto con el código {codigo}.") from None


def actualizar_producto(usuario: dict, *, producto_id: int, nombre: str, categoria: str, unidad: str,
                        stock_minimo: float, costo: float, venta: float, descripcion: str,
                        activo: bool) -> None:
    _exigir_admin(usuario)
    _validar_producto(nombre, stock_minimo, costo, venta)
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not activo:
            stock = conn.execute(
                "SELECT COALESCE(SUM(cantidad_actual), 0) FROM lotes_inventario WHERE producto_id = ? AND activo = 1",
                (producto_id,),
            ).fetchone()[0]
            if stock > 0:
                raise ValueError(f"No se puede desactivar: aún hay {stock:g} unidades en stock. "
                                 "Dé de baja los lotes primero.")
        conn.execute(
            """UPDATE productos SET nombre = ?, descripcion = ?, categoria = ?, unidad_medida = ?,
                      stock_minimo = ?, precio_costo = ?, precio_venta = ?, activo = ?
               WHERE id = ?""",
            (nombre.strip(), (descripcion or "").strip() or None, (categoria or "").strip() or None,
             unidad, stock_minimo, costo, venta, int(activo), producto_id),
        )


# ===========================================================================
# 4. UTILIDADES DE INTERFAZ
# ===========================================================================
def _flash(mensaje: str) -> None:
    """Guarda un mensaje que se mostrará tras el st.rerun() (los datos ya refrescados)."""
    st.session_state["_inv_flash"] = mensaje


def _mostrar_flash() -> None:
    mensaje = st.session_state.pop("_inv_flash", None)
    if mensaje:
        st.success(f"✅ {mensaje}")


def _intentar(funcion, *args, **kwargs) -> bool:
    """Ejecuta una operación de escritura mostrando errores de forma amigable."""
    try:
        funcion(*args, **kwargs)
        return True
    except PermissionError as e:
        st.error(f"⛔ {e}")
    except ValueError as e:
        st.error(f"⚠️ {e}")
    except sqlite3.OperationalError:
        st.error("🔒 La base de datos está ocupada por otro usuario. Intente nuevamente en unos segundos.")
    except sqlite3.IntegrityError as e:
        st.error(f"⚠️ Restricción de integridad de la base de datos: {e}")
    return False


def _txt(valor: Any) -> str:
    """Texto seguro (None/NaN -> cadena vacía)."""
    return "" if valor is None or (isinstance(valor, float) and pd.isna(valor)) else str(valor)


def _fechas(df: pd.DataFrame, columnas: list[str]) -> pd.DataFrame:
    """Copia del DataFrame con columnas de fecha convertidas (solo para mostrar)."""
    out = df.copy()
    for c in columnas:
        out[c] = pd.to_datetime(out[c])
    return out


# ---------------------------------------------------------------------------
# EXPORTACIÓN A CSV (compatible con Excel en configuración regional hispana)
#   * Separador de columnas: punto y coma (;)  -> cada campo cae en su columna.
#   * Codificación utf-8-sig (UTF-8 con BOM)   -> Excel respeta tildes y la "ñ".
#   * Saltos de línea \r\n                      -> formato nativo de Windows/Excel.
# ---------------------------------------------------------------------------
SEPARADOR_CSV = ";"

# Mapa  columna_interna -> encabezado que verá el usuario en Excel
COLUMNAS_EXPORT_STOCK = {
    "estado_stock": "Estado de stock", "codigo": "Código", "nombre": "Nombre", "categoria": "Categoría",
    "unidad": "Unidad", "stock_vigente": "Stock vigente", "stock_minimo": "Mínimo", "reponer": "Reponer",
    "stock_vencido": "Stock vencido", "prox_vencimiento": "Próximo vencimiento", "valor_costo": "Valor costo",
}
COLUMNAS_EXPORT_LOTES = {
    "orden_fefo": "Orden FEFO", "estado": "Estado", "codigo": "Código", "producto": "Producto", "lote": "Lote",
    "fecha_ingreso": "Fecha de ingreso", "fecha_vencimiento": "Fecha de vencimiento",
    "dias_restantes": "Días restantes", "cantidad_actual": "Cantidad actual", "cantidad_inicial": "Cantidad inicial",
    "ubicacion": "Ubicación", "numero_oc": "OC", "numero_factura": "Factura",
}
COLUMNAS_EXPORT_MOVIMIENTOS = {
    "fecha": "Fecha", "tipo": "Tipo", "codigo": "Código", "producto": "Producto", "lote": "Lote",
    "cantidad": "Cantidad", "motivo": "Motivo", "referencia": "Referencia", "guia": "Guía de remisión",
    "cliente": "Cliente", "cliente_doc": "RUC / DNI", "usuario": "Usuario",
}


def _sin_emoji(texto: Any) -> Any:
    """'🔴 Stock bajo' -> 'Stock bajo' (los emojis no se exportan a Excel)."""
    if not isinstance(texto, str):
        return texto
    partes = texto.split(" ", 1)
    return partes[1] if len(partes) == 2 and not partes[0].isalnum() else texto


def _preparar_export(df: pd.DataFrame, columnas: dict[str, str], *, fechas: Optional[dict[str, str]] = None,
                     estados: tuple | list = (), enteros: tuple | list = (),
                     dos_decimales: tuple | list = ()) -> pd.DataFrame:
    """
    Devuelve una copia lista para exportar: solo las columnas pedidas, con
    encabezados legibles, fechas dd/mm/aaaa, estados sin emoji y números redondeados.
    """
    out = df[list(columnas)].copy()
    for col, formato in (fechas or {}).items():
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.strftime(formato).fillna("")
    for col in estados:
        out[col] = out[col].map(_sin_emoji)
    for col in enteros:
        out[col] = pd.to_numeric(out[col], errors="coerce").round().astype("Int64")
    out = out.round(DECIMALES)
    for col in dos_decimales:
        out[col] = out[col].round(2)
    return out.rename(columns=columnas)


def _csv_para_excel(df: pd.DataFrame, decimal: str = ",") -> bytes:
    """Serializa a CSV con ';' y UTF-8 con BOM (utf-8-sig), listo para abrir en Excel."""
    texto = df.to_csv(index=False, sep=SEPARADOR_CSV, decimal=decimal, lineterminator="\r\n")
    return texto.encode("utf-8-sig")


def _boton_csv(df_export: pd.DataFrame, nombre_archivo: str, clave: str) -> None:
    """Botón de descarga CSV + selector del separador decimal según el Excel del usuario."""
    c_boton, c_decimal, _ = st.columns([1.4, 2.4, 3])
    decimal = c_decimal.radio(
        "Separador decimal", [",", "."], horizontal=True, key=f"{clave}_dec",
        help="Use ',' si su Excel muestra decimales con coma (ej. 12,50) o '.' si usa punto (ej. 12.50). "
             "Debe coincidir con la configuración regional de su Windows para que los números se lean bien.",
    )
    c_boton.download_button(
        "⬇️ Exportar CSV (Excel)", _csv_para_excel(df_export, decimal),
        file_name=f"{nombre_archivo}_{date.today().isoformat()}.csv", mime="text/csv", key=clave,
    )


# ===========================================================================
# 5. PESTAÑA 1: CONSULTA DE STOCK Y LOTES (Admin y Lector)
# ===========================================================================
FILTROS_ESTADO_LOTE: dict[str, Optional[list[str]]] = {
    "Todos": None,
    "🔴 Vencidos": ["🔴 Vencido"],
    "🟠🟡 Críticos / por vencer": ["🟠 Crítico", "🟡 Por vencer"],
    "🟢 Vigentes": ["🟢 Vigente"],
    "⚪ No perecibles": ["⚪ No perecible"],
}


def _aplicar_busqueda(df: pd.DataFrame, texto: str, columnas: list[str]) -> pd.DataFrame:
    """
    Búsqueda tolerante: cada palabra escrita debe aparecer en alguna de las columnas
    (ej. 'aceite 1l' encuentra 'Aceite Vegetal 1L'). Sin regex, por lo que caracteres
    como ( ) + * no provocan errores.
    """
    for termino in (texto or "").split():
        mascara = pd.Series(False, index=df.index)
        for col in columnas:
            mascara |= df[col].astype(str).str.contains(termino, case=False, na=False, regex=False)
        df = df[mascara]
    return df


def _limpiar_filtros_stock() -> None:
    st.session_state["inv_q_prod"] = ""
    st.session_state["inv_f_cat"] = "Todas"
    st.session_state["inv_f_alerta"] = False


def _limpiar_filtros_lotes() -> None:
    st.session_state["inv_q_lote"] = ""
    st.session_state["inv_f_lote_est"] = "Todos"
    st.session_state["inv_f_lote_agot"] = True


def _resumen_y_alertas(df_prod: pd.DataFrame, df_lotes: pd.DataFrame, es_admin: bool) -> None:
    """Indicadores superiores y banners de alerta (semáforos de stock y vencimiento)."""
    con_stock = df_lotes[df_lotes["cantidad_actual"] > 0]
    vencidos = con_stock[con_stock["dias_restantes"] < 0]
    por_vencer = con_stock[(con_stock["dias_restantes"] >= 0) & (con_stock["dias_restantes"] <= DIAS_ALERTA)]
    en_alerta = df_prod[df_prod["estado_stock"].isin(ESTADOS_STOCK_ALERTA)]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Productos en alerta de stock", len(en_alerta))
    c2.metric(f"Lotes por vencer ({DIAS_ALERTA} d)", len(por_vencer))
    c3.metric("Lotes vencidos con stock", len(vencidos))
    c4.metric("Valor inventario vigente (costo)", f"S/ {df_prod['valor_costo'].sum():,.2f}")

    if not vencidos.empty:
        accion = "Dé de baja el lote en 'Gestión > Ajuste / Baja de lote'." if es_admin \
            else "Notifique al administrador para su baja."
        st.error(f"🔴 {len(vencidos)} lote(s) vencido(s) con {vencidos['cantidad_actual'].sum():,.0f} "
                 f"unidades. No participan en las salidas FEFO. {accion}")
    if not en_alerta.empty:
        nombres = ", ".join(en_alerta["nombre"].head(5))
        extra = f" y {len(en_alerta) - 5} más" if len(en_alerta) > 5 else ""
        st.warning(f"🟠 Stock bajo o agotado: {nombres}{extra}.")


def _barra_filtros_stock(df_prod: pd.DataFrame) -> pd.DataFrame:
    """Barra de filtros de 'Stock por producto'. Devuelve el DataFrame ya filtrado."""
    categorias = ["Todas"] + sorted(df_prod["categoria"].dropna().unique())
    if st.session_state.get("inv_f_cat") not in categorias:   # la categoría pudo desaparecer
        st.session_state["inv_f_cat"] = "Todas"

    with st.container(border=True):
        c1, c2 = st.columns([3, 2])
        buscar = c1.text_input("🔎 Buscar por código o nombre", key="inv_q_prod",
                               placeholder="Ej. aceite, PRD-003, leche 24")
        categoria = c2.selectbox("Categoría", categorias, key="inv_f_cat")
        c3, c4, _ = st.columns([2, 1.3, 3])
        solo_alerta = c3.checkbox("Solo con alerta de stock", key="inv_f_alerta",
                                  help="Muestra únicamente productos 'Sin stock' o con 'Stock bajo'.")
        c4.button("🧹 Limpiar filtros", key="inv_btn_limpiar_prod", on_click=_limpiar_filtros_stock)

    df = _aplicar_busqueda(df_prod, buscar, ["codigo", "nombre"])
    if categoria != "Todas":
        df = df[df["categoria"] == categoria]
    if solo_alerta:
        df = df[df["estado_stock"].isin(ESTADOS_STOCK_ALERTA)]
    return df


def _vista_stock_productos(df_prod: pd.DataFrame) -> None:
    df = _barra_filtros_stock(df_prod)

    columnas = ["estado_stock", "codigo", "nombre", "categoria", "unidad", "stock_vigente", "stock_minimo",
                "reponer", "stock_vencido", "prox_vencimiento", "valor_costo"]
    if df.empty:
        st.info("Ningún producto coincide con los filtros aplicados.")
    else:
        st.dataframe(
            _fechas(df[columnas], ["prox_vencimiento"]), use_container_width=True, hide_index=True,
            column_config={
                "estado_stock": "Estado", "codigo": "Código", "nombre": "Producto", "categoria": "Categoría",
                "unidad": "Unidad",
                "stock_vigente": st.column_config.NumberColumn("Stock vigente", format="%.2f"),
                "stock_minimo": st.column_config.NumberColumn("Mínimo", format="%.2f"),
                "reponer": st.column_config.NumberColumn("A reponer", format="%.2f"),
                "stock_vencido": st.column_config.NumberColumn("Vencido", format="%.2f"),
                "prox_vencimiento": st.column_config.DateColumn("Próx. vencimiento", format="DD/MM/YYYY"),
                "valor_costo": st.column_config.NumberColumn("Valor (S/)", format="%.2f"),
            },
        )
    st.caption(f"Mostrando {len(df)} de {len(df_prod)} producto(s). 'Stock vigente' excluye lotes vencidos; "
               "'A reponer' = unidades que faltan para alcanzar el mínimo. El CSV exporta lo filtrado.")
    _boton_csv(
        _preparar_export(df, COLUMNAS_EXPORT_STOCK, fechas={"prox_vencimiento": "%d/%m/%Y"},
                         estados=["estado_stock"], dos_decimales=["valor_costo"]),
        "stock_productos", "inv_csv_prod",
    )


def _barra_filtros_lotes(df_lotes: pd.DataFrame) -> pd.DataFrame:
    """Barra de filtros de 'Lotes'. Devuelve el DataFrame ya filtrado."""
    with st.container(border=True):
        c1, c2 = st.columns([3, 2])
        buscar = c1.text_input("🔎 Buscar por código, producto o lote", key="inv_q_lote",
                               placeholder="Ej. aceite, L-AT02, PRD-001")
        estado = c2.selectbox("Estado del lote", list(FILTROS_ESTADO_LOTE), key="inv_f_lote_est")
        c3, c4, _ = st.columns([2, 1.3, 3])
        ocultar_agotados = c3.checkbox("Ocultar lotes agotados", value=True, key="inv_f_lote_agot")
        c4.button("🧹 Limpiar filtros", key="inv_btn_limpiar_lotes", on_click=_limpiar_filtros_lotes)

    df = _aplicar_busqueda(df_lotes, buscar, ["codigo", "producto", "lote"])
    if ocultar_agotados:
        df = df[df["cantidad_actual"] > 0]
    estados_validos = FILTROS_ESTADO_LOTE[estado]
    if estados_validos:
        df = df[df["estado"].isin(estados_validos)]
    return df


def _vista_lotes(df_lotes: pd.DataFrame) -> None:
    df = _barra_filtros_lotes(df_lotes)

    columnas = ["orden_fefo", "estado", "codigo", "producto", "lote", "fecha_ingreso", "fecha_vencimiento",
                "dias_restantes", "cantidad_actual", "cantidad_inicial", "ubicacion", "numero_oc", "numero_factura"]
    if df.empty:
        st.info("Ningún lote coincide con los filtros aplicados.")
    else:
        st.dataframe(
            _fechas(df[columnas], ["fecha_ingreso", "fecha_vencimiento"]), use_container_width=True,
            hide_index=True,
            column_config={
                "orden_fefo": st.column_config.NumberColumn(
                    "Orden FEFO", format="%d", help="1 = el primero en despacharse. Vacío = no elegible."),
                "estado": "Estado", "codigo": "Código", "producto": "Producto", "lote": "Lote",
                "fecha_ingreso": st.column_config.DateColumn("Ingreso", format="DD/MM/YYYY"),
                "fecha_vencimiento": st.column_config.DateColumn("Vence", format="DD/MM/YYYY"),
                "dias_restantes": st.column_config.NumberColumn("Días restantes", format="%d"),
                "cantidad_actual": st.column_config.NumberColumn("Cantidad actual", format="%.2f"),
                "cantidad_inicial": st.column_config.NumberColumn("Cant. inicial", format="%.2f"),
                "ubicacion": "Ubicación", "numero_oc": "OC", "numero_factura": "Factura",
            },
        )
    st.caption(f"Mostrando {len(df)} de {len(df_lotes)} lote(s). Orden FEFO: prioridad de despacho dentro de cada "
               "producto (vence antes = sale antes); los lotes vencidos y agotados no participan. "
               "El CSV exporta lo filtrado.")
    _boton_csv(
        _preparar_export(df, COLUMNAS_EXPORT_LOTES,
                         fechas={"fecha_ingreso": "%d/%m/%Y", "fecha_vencimiento": "%d/%m/%Y"},
                         estados=["estado"], enteros=["orden_fefo", "dias_restantes"]),
        "lotes_inventario", "inv_csv_lotes",
    )


def _vista_simulador() -> None:
    st.markdown("Calcule de qué lotes saldría una cantidad bajo FEFO. **No modifica el inventario.**")
    df = cargar_stock_productos()
    df = df[df["stock_vigente"] > 0]
    if df.empty:
        st.info("No hay productos con stock vigente.")
        return
    opciones = {int(r.id): f"{r.codigo} · {r.nombre} (disp.: {r.stock_vigente:g} {r.unidad})"
                for r in df.itertuples()}
    c1, c2 = st.columns([3, 1])
    pid = c1.selectbox("Producto", list(opciones), format_func=opciones.get, key="inv_sim_prod")
    cantidad = c2.number_input("Cantidad", min_value=0.0, value=1.0, step=1.0, format="%.2f", key="inv_sim_cant")
    if cantidad <= 0:
        return
    plan, faltante = _plan_lectura(pid, cantidad)
    if plan:
        st.dataframe(_plan_a_df(plan), use_container_width=True, hide_index=True, column_config=CONFIG_PLAN)
    if faltante > 0:
        st.error(f"Stock vigente insuficiente: faltarían {faltante:g} unidades.")
    else:
        st.success(f"Se cubre la cantidad solicitada usando {len(plan)} lote(s).")


def _vista_historial() -> None:
    df_p = cargar_productos(solo_activos=False)
    opciones = {0: "Todos los productos"}
    opciones.update({int(r.id): f"{r.codigo} · {r.nombre}" for r in df_p.itertuples()})
    with st.container(border=True):
        pid = st.selectbox("Filtrar por producto", list(opciones), format_func=opciones.get, key="inv_hist_prod")
    df = cargar_movimientos(pid or None)
    if df.empty:
        st.info("Aún no hay movimientos registrados. Se mostrarán las entradas, salidas y ajustes desde ahora.")
        return

    df_export = _preparar_export(df, COLUMNAS_EXPORT_MOVIMIENTOS, fechas={"fecha": "%d/%m/%Y %H:%M"})
    iconos = {"ENTRADA": "🟢 Entrada", "SALIDA": "🔴 Salida", "AJUSTE": "🟠 Ajuste"}
    df["tipo"] = df["tipo"].map(iconos)
    st.dataframe(_fechas(df, ["fecha"]), use_container_width=True, hide_index=True, column_config={
        "fecha": st.column_config.DatetimeColumn("Fecha", format="DD/MM/YYYY HH:mm"),
        "tipo": "Tipo", "codigo": "Código", "producto": "Producto", "lote": "Lote",
        "cantidad": st.column_config.NumberColumn("Cantidad", format="%+.2f"),
        "motivo": "Motivo", "referencia": "Referencia", "guia": "Guía de remisión",
        "cliente": "Cliente", "cliente_doc": "RUC / DNI", "usuario": "Usuario",
    })
    st.caption("Muestra los últimos 300 movimientos.")
    _boton_csv(df_export, "historial_movimientos", "inv_csv_mov")


def _tab_consulta(es_admin: bool) -> None:
    df_prod = cargar_stock_productos()
    df_lotes = cargar_lotes()
    _resumen_y_alertas(df_prod, df_lotes, es_admin)

    vista = st.radio("Vista", ["📊 Stock por producto", "🧾 Lotes (orden FEFO)",
                               "🧮 Simulador FEFO", "📜 Historial de movimientos"],
                     horizontal=True, key="inv_vista", label_visibility="collapsed")
    if vista.startswith("📊"):
        _vista_stock_productos(df_prod)
    elif vista.startswith("🧾"):
        _vista_lotes(df_lotes)
    elif vista.startswith("🧮"):
        _vista_simulador()
    else:
        _vista_historial()


# ===========================================================================
# 6. PESTAÑA 2: GESTIÓN / ENTRADAS / SALIDAS (solo Admin)
# ===========================================================================
def _seccion_entrada(usuario: dict) -> None:
    prods = cargar_productos(solo_activos=True)
    if prods.empty:
        st.warning("Primero registre un producto en la sección 'Productos'.")
        return

    opt_prod = {int(r.id): f"{r.codigo} · {r.nombre} ({r.unidad_medida})" for r in prods.itertuples()}
    opt_oc: dict[Optional[int], str] = {None: "— Sin orden de compra —"}
    for r in db.fetch_df("SELECT id, numero_oc, proveedor, estado FROM ordenes_compra ORDER BY fecha_emision DESC").itertuples():
        opt_oc[int(r.id)] = f"{r.numero_oc} · {r.proveedor} [{r.estado}]"
    opt_fac: dict[Optional[int], str] = {None: "— Sin factura —"}
    for r in db.fetch_df("SELECT id, numero_factura, proveedor, estado FROM facturas ORDER BY fecha_emision DESC").itertuples():
        opt_fac[int(r.id)] = f"{r.numero_factura} · {r.proveedor} [{r.estado}]"

    from modulos import carga_masiva   # import diferido: carga_masiva depende de este módulo
    with st.expander("📥 Cargar entradas desde Excel / CSV (con plantilla)", expanded=False):
        carga_masiva.panel(usuario, "entradas")

    n = st.session_state.setdefault("inv_ent_n", 0)   # Cambiar 'n' reinicia el formulario
    hoy = date.today()
    with st.form(f"inv_form_entrada_{n}"):
        st.markdown("#### Registrar ingreso de un lote")
        c1, c2, c3 = st.columns(3)
        producto_id = c1.selectbox("Producto *", list(opt_prod), format_func=opt_prod.get, key=f"inv_ent_prod_{n}")
        numero_lote = c2.text_input("Número de lote *", key=f"inv_ent_lote_{n}")
        cantidad = c3.number_input("Cantidad *", min_value=0.0, value=0.0, step=1.0, format="%.2f",
                                   key=f"inv_ent_cant_{n}")
        c4, c5, c6 = st.columns(3)
        fecha_ingreso = c4.date_input("Fecha de ingreso", value=hoy, max_value=hoy, format="DD/MM/YYYY",
                                      key=f"inv_ent_fing_{n}")
        fecha_venc = c5.date_input("Fecha de vencimiento", value=hoy + timedelta(days=180), min_value=hoy,
                                   format="DD/MM/YYYY", key=f"inv_ent_fven_{n}")
        no_perecible = c6.checkbox("No perecible (sin vencimiento)", key=f"inv_ent_np_{n}",
                                   help="Si se marca, se ignora la fecha de vencimiento.")
        c7, c8, c9 = st.columns(3)
        ubicacion = c7.text_input("Ubicación (almacén / estante)", key=f"inv_ent_ubi_{n}")
        oc_id = c8.selectbox("Orden de compra", list(opt_oc), format_func=opt_oc.get, key=f"inv_ent_oc_{n}")
        factura_id = c9.selectbox("Factura", list(opt_fac), format_func=opt_fac.get, key=f"inv_ent_fac_{n}")
        enviar = st.form_submit_button("➕ Registrar entrada", type="primary")

    if enviar:
        ok = _intentar(
            registrar_entrada, usuario, producto_id=producto_id, numero_lote=numero_lote, cantidad=cantidad,
            fecha_ingreso=fecha_ingreso, fecha_vencimiento=None if no_perecible else fecha_venc,
            ubicacion=ubicacion, orden_compra_id=oc_id, factura_id=factura_id,
        )
        if ok:
            _flash(f"Entrada registrada: lote {numero_lote.strip().upper()} ({cantidad:g} unidades).")
            st.session_state["inv_ent_n"] = n + 1
            st.rerun()


def _seccion_salida(usuario: dict) -> None:
    df = cargar_stock_productos()
    disponibles = df[df["stock_vigente"] > 0]
    if disponibles.empty:
        st.info("No hay productos con stock vigente para despachar.")
        return

    opciones = {int(r.id): f"{r.codigo} · {r.nombre} — disp.: {r.stock_vigente:g} {r.unidad}"
                for r in disponibles.itertuples()}
    from modulos import carga_masiva   # import diferido: carga_masiva depende de este módulo
    with st.expander("📥 Cargar salidas desde Excel / CSV (con plantilla)", expanded=False):
        carga_masiva.panel(usuario, "salidas")

    clientes = clientes_db.listar_clientes(solo_activos=True)
    opt_cli: dict[int, str] = {0: "— Sin cliente —"}
    opt_cli.update({int(r.id): f"{r.documento} · {r.razon_social}" for r in clientes.itertuples()})
    n = st.session_state.setdefault("inv_sal_n", 0)
    with st.form(f"inv_form_salida_{n}"):
        st.markdown("#### Registrar salida (FEFO automático)")
        c1, c2 = st.columns([3, 1])
        producto_id = c1.selectbox("Producto *", list(opciones), format_func=opciones.get, key=f"inv_sal_prod_{n}")
        cantidad = c2.number_input("Cantidad *", min_value=0.0, value=0.0, step=1.0, format="%.2f",
                                   key=f"inv_sal_cant_{n}")
        c3, c4 = st.columns(2)
        motivo = c3.selectbox("Motivo", MOTIVOS_SALIDA, key=f"inv_sal_mot_{n}")
        referencia = c4.text_input("Referencia (pedido, orden, observación)", key=f"inv_sal_ref_{n}")
        c5, c6 = st.columns([3, 1.5])
        cliente_id = c5.selectbox("Cliente (obligatorio en ventas / despachos)", list(opt_cli),
                                  format_func=opt_cli.get, key=f"inv_sal_cli_{n}",
                                  help="Si el cliente no existe, regístrelo antes en la sección 'Clientes'.")
        guia = c6.text_input("Guía de remisión", placeholder="T001-00001234", key=f"inv_sal_guia_{n}")
        previsualizar = st.form_submit_button("🔍 Previsualizar salida FEFO")

    if previsualizar:
        cantidad = round(float(cantidad), DECIMALES)
        st.session_state.pop("inv_salida_pendiente", None)
        try:
            guia_ok = identidad.normalizar_guia_remision(guia)
        except ValueError as e:
            guia_ok = None
            st.error(f"⚠️ {e}")
        if guia_ok is not None:
            if cantidad <= 0:
                st.error("Ingrese una cantidad mayor que cero.")
            elif motivo in MOTIVOS_REQUIEREN_CLIENTE and not cliente_id:
                st.error("⚠️ Para una venta o despacho seleccione el cliente. "
                         "Si no existe, regístrelo en la sección 'Clientes'.")
            else:
                _, faltante = _plan_lectura(producto_id, cantidad)
                if faltante > 0:
                    st.error(f"Stock vigente insuficiente: faltarían {faltante:g} unidades.")
                else:
                    st.session_state["inv_salida_pendiente"] = {
                        "producto_id": producto_id, "cantidad": cantidad, "motivo": motivo,
                        "referencia": referencia, "guia": guia_ok, "cliente_id": cliente_id or None}

    pendiente = st.session_state.get("inv_salida_pendiente")
    if pendiente:
        _confirmar_salida(usuario, pendiente, df, n)


def _confirmar_salida(usuario: dict, pend: dict, df_prod: pd.DataFrame, n: int) -> None:
    """Muestra el plan FEFO recalculado y pide confirmación explícita."""
    plan, faltante = _plan_lectura(pend["producto_id"], pend["cantidad"])
    if faltante > 0:
        st.session_state.pop("inv_salida_pendiente", None)
        st.error("El stock cambió y ya no alcanza. Vuelva a previsualizar la salida.")
        return

    fila = df_prod[df_prod["id"] == pend["producto_id"]]
    nombre = fila.iloc[0]["nombre"] if not fila.empty else f"ID {pend['producto_id']}"
    st.markdown(f"##### Confirmar salida de **{pend['cantidad']:g}** × {nombre}")
    cli = clientes_db.obtener_cliente(pend["cliente_id"]) if pend.get("cliente_id") else None
    if cli is not None:
        st.markdown(f"**Cliente:** {cli['razon_social']} · **{cli['tipo_documento']}:** {cli['numero_documento']}  \n"
                    f"**Dirección:** {cli['direccion'] or '—'}")
    if pend.get("guia"):
        st.markdown(f"**Guía de remisión:** {pend['guia']}")
    st.dataframe(_plan_a_df(plan), use_container_width=True, hide_index=True, column_config=CONFIG_PLAN)

    if not fila.empty:
        restante = fila.iloc[0]["stock_vigente"] - pend["cantidad"]
        if restante < fila.iloc[0]["stock_minimo"]:
            st.warning(f"⚠️ Tras esta salida quedarán {restante:g} unidades, por debajo del mínimo "
                       f"({fila.iloc[0]['stock_minimo']:g}).")

    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("✅ Confirmar salida", type="primary", key="inv_sal_ok"):
        if _intentar(registrar_salida_fefo, usuario, producto_id=pend["producto_id"], cantidad=pend["cantidad"],
                     motivo=pend["motivo"], referencia=pend["referencia"], guia_remision=pend.get("guia", ""),
                     cliente_id=pend.get("cliente_id")):
            st.session_state.pop("inv_salida_pendiente", None)
            st.session_state["inv_sal_n"] = n + 1
            _flash(f"Salida registrada: {pend['cantidad']:g} × {nombre} (FEFO, {len(plan)} lote(s)).")
            st.rerun()
    if b2.button("Cancelar", key="inv_sal_cancel"):
        st.session_state.pop("inv_salida_pendiente", None)
        st.rerun()


def _seccion_productos(usuario: dict) -> None:
    # ----- Crear producto
    with st.expander("➕ Crear producto nuevo", expanded=False):
        n = st.session_state.setdefault("inv_prod_n", 0)
        with st.form(f"inv_form_prod_nuevo_{n}"):
            c1, c2, c3 = st.columns(3)
            codigo = c1.text_input("Código *", placeholder="PRD-007", key=f"inv_np_cod_{n}")
            nombre = c2.text_input("Nombre *", key=f"inv_np_nom_{n}")
            categoria = c3.text_input("Categoría", key=f"inv_np_cat_{n}")
            c4, c5, c6, c7 = st.columns(4)
            unidad = c4.selectbox("Unidad", UNIDADES, key=f"inv_np_uni_{n}")
            stock_min = c5.number_input("Stock mínimo", min_value=0.0, value=0.0, step=1.0, format="%.2f",
                                        key=f"inv_np_min_{n}")
            costo = c6.number_input("Precio costo (S/)", min_value=0.0, value=0.0, step=0.1, format="%.2f",
                                    key=f"inv_np_cos_{n}")
            venta = c7.number_input("Precio venta (S/)", min_value=0.0, value=0.0, step=0.1, format="%.2f",
                                    key=f"inv_np_ven_{n}")
            descripcion = st.text_area("Descripción", key=f"inv_np_des_{n}", height=70)
            crear = st.form_submit_button("💾 Crear producto", type="primary")
        if crear:
            if venta and costo and venta < costo:
                st.warning("El precio de venta es menor al costo; verifique los valores.")
            if _intentar(crear_producto, usuario, codigo=codigo, nombre=nombre, categoria=categoria, unidad=unidad,
                         stock_minimo=stock_min, costo=costo, venta=venta, descripcion=descripcion):
                _flash(f"Producto {codigo.strip().upper()} creado.")
                st.session_state["inv_prod_n"] = n + 1
                st.rerun()

    # ----- Editar producto
    with st.expander("✏️ Editar / desactivar producto", expanded=True):
        prods = cargar_productos(solo_activos=False)
        if prods.empty:
            st.info("No hay productos registrados.")
            return
        opciones = {int(r.id): f"{r.codigo} · {r.nombre}{'' if r.activo else ' (inactivo)'}"
                    for r in prods.itertuples()}
        pid = st.selectbox("Producto a editar", list(opciones), format_func=opciones.get, key="inv_edit_sel")
        p = prods[prods["id"] == pid].iloc[0]

        with st.form(f"inv_form_edit_{pid}"):
            c1, c2, c3 = st.columns(3)
            c1.text_input("Código (no editable)", value=p["codigo"], disabled=True, key=f"inv_ed_cod_{pid}")
            nombre = c2.text_input("Nombre *", value=_txt(p["nombre"]), key=f"inv_ed_nom_{pid}")
            categoria = c3.text_input("Categoría", value=_txt(p["categoria"]), key=f"inv_ed_cat_{pid}")
            c4, c5, c6, c7 = st.columns(4)
            unidades = UNIDADES if p["unidad_medida"] in UNIDADES else UNIDADES + [p["unidad_medida"]]
            unidad = c4.selectbox("Unidad", unidades, index=unidades.index(p["unidad_medida"]),
                                  key=f"inv_ed_uni_{pid}")
            stock_min = c5.number_input("Stock mínimo", min_value=0.0, value=float(p["stock_minimo"]), step=1.0,
                                        format="%.2f", key=f"inv_ed_min_{pid}")
            costo = c6.number_input("Precio costo (S/)", min_value=0.0, value=float(p["precio_costo"]), step=0.1,
                                    format="%.2f", key=f"inv_ed_cos_{pid}")
            venta = c7.number_input("Precio venta (S/)", min_value=0.0, value=float(p["precio_venta"]), step=0.1,
                                    format="%.2f", key=f"inv_ed_ven_{pid}")
            descripcion = st.text_area("Descripción", value=_txt(p["descripcion"]), height=70,
                                       key=f"inv_ed_des_{pid}")
            activo = st.checkbox("Producto activo", value=bool(p["activo"]), key=f"inv_ed_act_{pid}",
                                 help="Solo se puede desactivar si no tiene stock.")
            guardar = st.form_submit_button("💾 Guardar cambios", type="primary")
        if guardar:
            if _intentar(actualizar_producto, usuario, producto_id=pid, nombre=nombre, categoria=categoria,
                         unidad=unidad, stock_minimo=stock_min, costo=costo, venta=venta,
                         descripcion=descripcion, activo=activo):
                _flash(f"Producto {p['codigo']} actualizado.")
                st.rerun()


def _seccion_ajuste(usuario: dict) -> None:
    df = cargar_lotes()
    df = df[df["cantidad_actual"] > 0]
    if df.empty:
        st.info("No hay lotes con stock para ajustar.")
        return

    st.caption("Use esta sección para corregir conteos físicos, cambiar la ubicación o dar de baja "
               "lotes vencidos o dañados (los lotes vencidos no participan en FEFO).")
    opciones = {}
    for r in df.itertuples():
        vence = r.fecha_vencimiento if pd.notna(r.fecha_vencimiento) else "sin venc."
        opciones[int(r.id)] = f"{r.codigo} · lote {r.lote} · {vence} · {r.cantidad_actual:g} {r.unidad} · {r.estado}"
    lote_id = st.selectbox("Lote", list(opciones), format_func=opciones.get, key="inv_aj_sel")
    lote = df[df["id"] == lote_id].iloc[0]

    with st.form(f"inv_form_ajuste_{lote_id}"):
        c1, c2 = st.columns(2)
        tipo = c1.selectbox("Tipo de operación", ["Corregir cantidad (conteo físico)", "Dar de baja el lote completo"],
                            key=f"inv_aj_tipo_{lote_id}")
        nueva = c2.number_input("Nueva cantidad (si corrige)", min_value=0.0, value=float(lote["cantidad_actual"]),
                                step=1.0, format="%.2f", key=f"inv_aj_cant_{lote_id}")
        c3, c4 = st.columns(2)
        ubicacion = c3.text_input("Ubicación", value=_txt(lote["ubicacion"]), key=f"inv_aj_ubi_{lote_id}")
        motivo_cat = c4.selectbox("Motivo", MOTIVOS_AJUSTE, key=f"inv_aj_mot_{lote_id}")
        detalle = st.text_input("Detalle (opcional)", key=f"inv_aj_det_{lote_id}")
        aplicar = st.form_submit_button("🛠️ Aplicar", type="primary")

    if aplicar:
        baja = tipo.startswith("Dar de baja")
        motivo = f"{motivo_cat}: {detalle.strip()}" if detalle.strip() else motivo_cat
        if _intentar(ajustar_lote, usuario, lote_id=lote_id, nueva_cantidad=nueva, ubicacion=ubicacion,
                     motivo=motivo, baja_total=baja):
            _flash(f"Lote {lote['lote']} {'dado de baja' if baja else 'ajustado'} correctamente.")
            st.rerun()


def _tab_gestion(usuario: dict) -> None:
    seccion = st.radio("Sección", ["➕ Entrada de mercadería", "➖ Salida (FEFO)", "🏷️ Productos",
                                   "🛠️ Ajuste / Baja de lote"],
                       horizontal=True, key="inv_seccion", label_visibility="collapsed")
    if seccion.startswith("➕"):
        _seccion_entrada(usuario)
    elif seccion.startswith("➖"):
        _seccion_salida(usuario)
    elif seccion.startswith("🏷️"):
        _seccion_productos(usuario)
    else:
        _seccion_ajuste(usuario)


# ===========================================================================
# 7. PUNTO DE ENTRADA (invocado por app.py)
# ===========================================================================
def render(usuario: dict) -> None:
    """Renderiza el módulo. Admin: 2 pestañas (consulta + gestión). Lector: solo consulta."""
    _asegurar_tabla_movimientos()
    es_admin = usuario.get("rol") == "Admin"

    st.title("📦 Gestión de Inventario" if es_admin else "🔎 Consulta de Stock")
    _mostrar_flash()

    if es_admin:
        tab_consulta, tab_gestion = st.tabs(["📋 Consulta de Stock y Lotes", "🛠️ Gestión / Entradas / Salidas"])
        with tab_consulta:
            _tab_consulta(es_admin=True)
        with tab_gestion:
            _tab_gestion(usuario)
    else:
        # Lector: no se renderiza ningún formulario de escritura.
        st.caption("👁️ Modo solo consulta: puede revisar stock, lotes, vencimientos e historial.")
        (tab_consulta,) = st.tabs(["📋 Consulta de Stock y Lotes"])
        with tab_consulta:
            _tab_consulta(es_admin=False)
