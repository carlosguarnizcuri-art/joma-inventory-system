"""
modulos/reportes_db.py - Consultas analíticas del MÓDULO 4 (solo lectura, sin código de interfaz)

Alimenta a dashboards.py y reportes.py:
  * Stock valorizado por producto (vigente / por vencer / vencido).
  * Vencimientos agrupados por tramos.
  * Rotación de inventario y tendencia de movimientos.
  * Estado de órdenes de compra y cuentas por pagar por antigüedad.
  * Kardex con trazabilidad (usuario, fecha, tipo, lote) y saldo acumulado por producto.

Todas las funciones leen la BD en el momento de la llamada, por lo que los dashboards
siempre muestran datos actualizados.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import pandas as pd

import database as db
from modulos import compras_db as cdb
from modulos import inventario as inv

DIAS_ALERTA = inv.DIAS_ALERTA   # lote "por vencer" = vence en <= 30 días

TRAMOS_VENCIMIENTO = ["Vencido", "0-15 días", "16-30 días", "31-60 días", "61-90 días",
                      "Más de 90 días", "No perecible"]
TRAMOS_FACTURA = ["Vencida", "0-7 días", "8-30 días", "Más de 30 días", "Sin fecha de pago"]


def asegurar_tablas() -> None:
    """Garantiza que existan todas las tablas que consultan los reportes (idempotente)."""
    cdb.asegurar_esquema()   # incluye kardex (movimientos_inventario), oc_detalle y documentos_adjuntos


# ===========================================================================
# INVENTARIO VALORIZADO
# ===========================================================================
def stock_valorizado() -> pd.DataFrame:
    """Una fila por producto activo con stock vigente / por vencer / vencido y su valor a costo."""
    hoy = date.today()
    limite = hoy + timedelta(days=DIAS_ALERTA)
    df = db.fetch_df(
        """SELECT p.id, p.codigo, p.nombre, COALESCE(p.categoria, 'Sin categoría') AS categoria,
                  p.unidad_medida AS unidad, p.stock_minimo, p.precio_costo, p.precio_venta,
                  COALESCE(SUM(CASE WHEN l.activo = 1 AND (l.fecha_vencimiento IS NULL OR l.fecha_vencimiento >= :hoy)
                                    THEN l.cantidad_actual END), 0) AS stock_vigente,
                  COALESCE(SUM(CASE WHEN l.activo = 1 AND l.fecha_vencimiento < :hoy
                                    THEN l.cantidad_actual END), 0) AS stock_vencido,
                  COALESCE(SUM(CASE WHEN l.activo = 1 AND l.fecha_vencimiento BETWEEN :hoy AND :limite
                                    THEN l.cantidad_actual END), 0) AS stock_por_vencer
           FROM productos p
           LEFT JOIN lotes_inventario l ON l.producto_id = p.id
           WHERE p.activo = 1
           GROUP BY p.id
           ORDER BY p.nombre""",
        {"hoy": hoy.isoformat(), "limite": limite.isoformat()},
    )
    df["estado_stock"] = [inv.estado_stock(s, m) for s, m in zip(df["stock_vigente"], df["stock_minimo"])]
    df["reponer"] = (df["stock_minimo"] - df["stock_vigente"]).clip(lower=0)
    df["valor_costo"] = df["stock_vigente"] * df["precio_costo"]
    df["valor_venta"] = df["stock_vigente"] * df["precio_venta"]
    df["valor_por_vencer"] = df["stock_por_vencer"] * df["precio_costo"]
    df["valor_vencido"] = df["stock_vencido"] * df["precio_costo"]
    return df


def _tramo_vencimiento(dias) -> str:
    if pd.isna(dias):
        return "No perecible"
    if dias < 0:
        return "Vencido"
    if dias <= 15:
        return "0-15 días"
    if dias <= 30:
        return "16-30 días"
    if dias <= 60:
        return "31-60 días"
    if dias <= 90:
        return "61-90 días"
    return "Más de 90 días"


def vencimientos_por_tramo() -> pd.DataFrame:
    """Lotes con stock agrupados por tramo de vencimiento (siempre devuelve los 7 tramos)."""
    df = db.fetch_df(
        """SELECT l.id, l.fecha_vencimiento, l.cantidad_actual, p.precio_costo
           FROM lotes_inventario l JOIN productos p ON p.id = l.producto_id
           WHERE l.activo = 1 AND l.cantidad_actual > 0""")
    dias = (pd.to_datetime(df["fecha_vencimiento"]) - pd.Timestamp(date.today())).dt.days
    df["tramo"] = [_tramo_vencimiento(d) for d in dias]
    df["valor"] = df["cantidad_actual"] * df["precio_costo"]
    res = (df.groupby("tramo").agg(lotes=("id", "count"), unidades=("cantidad_actual", "sum"), valor=("valor", "sum"))
             .reindex(TRAMOS_VENCIMIENTO, fill_value=0).rename_axis("tramo").reset_index())
    return res


# ===========================================================================
# ROTACIÓN Y MOVIMIENTOS
# ===========================================================================
def rotacion(dias: int) -> pd.DataFrame:
    """
    Rotación por producto en los últimos 'dias' días, calculada con el kardex:
      stock inicial  = stock actual - movimiento neto del período
      stock promedio = (stock inicial + stock actual) / 2
      rotación       = unidades salidas / stock promedio        (veces en el período)
      cobertura      = stock vigente / (salidas por día)         (días de inventario)
    """
    base = stock_valorizado()
    desde = (date.today() - timedelta(days=dias)).isoformat()
    mov = db.fetch_df(
        """SELECT producto_id,
                  SUM(CASE WHEN tipo = 'SALIDA'  THEN -cantidad ELSE 0 END) AS salidas,
                  SUM(CASE WHEN tipo = 'ENTRADA' THEN  cantidad ELSE 0 END) AS entradas,
                  SUM(cantidad) AS neto
           FROM movimientos_inventario WHERE date(fecha) >= :desde GROUP BY producto_id""",
        {"desde": desde})
    df = base.merge(mov, left_on="id", right_on="producto_id", how="left")
    for col in ("salidas", "entradas", "neto"):
        df[col] = df[col].fillna(0.0)

    df["stock_total"] = df["stock_vigente"] + df["stock_vencido"]
    df["stock_inicio"] = (df["stock_total"] - df["neto"]).clip(lower=0)
    df["stock_promedio"] = (df["stock_inicio"] + df["stock_total"]) / 2
    df["rotacion"] = (df["salidas"] / df["stock_promedio"].where(df["stock_promedio"] > 0)).round(2)
    df["cobertura_dias"] = (df["stock_vigente"] / (df["salidas"] / dias).where(df["salidas"] > 0)).round(0)
    df["salidas_valor"] = df["salidas"] * df["precio_costo"]

    def clasificar(salidas: float, stock_total: float, cobertura) -> str:
        if salidas <= 0:
            return "🧊 Sin salidas" if stock_total > 0 else "— Sin stock ni salidas"
        if cobertura <= 30:
            return "🟢 Alta rotación"
        if cobertura <= 90:
            return "🟡 Rotación media"
        return "🟠 Baja rotación"

    df["clasificacion"] = [clasificar(s, t, c) for s, t, c in zip(df["salidas"], df["stock_total"], df["cobertura_dias"])]
    return df.drop(columns=["producto_id"])


def tendencia_movimientos(dias: int) -> pd.DataFrame:
    """Unidades entradas / salidas / ajustes por día (rellena con 0 los días sin movimiento)."""
    hoy = date.today()
    desde = hoy - timedelta(days=dias)
    df = db.fetch_df(
        """SELECT date(fecha) AS dia,
                  SUM(CASE WHEN tipo = 'ENTRADA' THEN  cantidad ELSE 0 END) AS entradas,
                  SUM(CASE WHEN tipo = 'SALIDA'  THEN -cantidad ELSE 0 END) AS salidas,
                  SUM(CASE WHEN tipo = 'AJUSTE'  THEN  cantidad ELSE 0 END) AS ajustes
           FROM movimientos_inventario WHERE date(fecha) >= :desde GROUP BY date(fecha) ORDER BY dia""",
        {"desde": desde.isoformat()})
    df["dia"] = pd.to_datetime(df["dia"])
    rango = pd.date_range(desde, hoy, freq="D")
    return (df.set_index("dia").reindex(rango, fill_value=0).rename_axis("dia").reset_index())


# ===========================================================================
# COMPRAS: ÓRDENES Y FACTURAS
# ===========================================================================
def ordenes_por_estado() -> pd.DataFrame:
    df = cdb.listar_ocs()
    if df.empty:
        return pd.DataFrame({"estado": [], "ordenes": [], "monto": []})
    return (df.groupby("estado_visual").agg(ordenes=("id", "count"), monto=("monto_total", "sum"))
              .rename_axis("estado").reset_index())


def _tramo_factura(dias) -> str:
    if pd.isna(dias):
        return "Sin fecha de pago"
    if dias < 0:
        return "Vencida"
    if dias <= 7:
        return "0-7 días"
    if dias <= 30:
        return "8-30 días"
    return "Más de 30 días"


def cuentas_por_pagar() -> pd.DataFrame:
    """Facturas abiertas agrupadas por tramo de vencimiento de pago y moneda."""
    df = cdb.listar_facturas()
    df = df[df["estado"] == "Abierto"].copy()
    df["tramo"] = [_tramo_factura(d) for d in df["dias_para_pago"]]
    if df.empty:
        return pd.DataFrame({"tramo": [], "moneda": [], "facturas": [], "monto": []})
    return (df.groupby(["tramo", "moneda"]).agg(facturas=("id", "count"), monto=("monto_total", "sum"))
              .reset_index())


def oc_detalle_lineas() -> pd.DataFrame:
    """Reporte formal: una fila por línea de OC con lo pedido, recibido y pendiente."""
    df = db.fetch_df(
        """SELECT oc.numero_oc, oc.proveedor, oc.fecha_emision, oc.estado, oc.moneda,
                  p.codigo, p.nombre AS producto, p.unidad_medida AS unidad,
                  d.cantidad_pedida, d.cantidad_recibida, d.precio_unitario
           FROM oc_detalle d
           JOIN ordenes_compra oc ON oc.id = d.orden_compra_id
           JOIN productos p ON p.id = d.producto_id
           ORDER BY oc.fecha_emision DESC, oc.id DESC, d.id""")
    df["pendiente"] = (df["cantidad_pedida"] - df["cantidad_recibida"]).round(inv.DECIMALES)
    df["subtotal"] = (df["cantidad_pedida"] * df["precio_unitario"]).round(2)
    return df


# ===========================================================================
# KARDEX
# ===========================================================================
def usuarios_para_filtro() -> pd.DataFrame:
    return db.fetch_df("SELECT id, nombre_completo, username FROM usuarios ORDER BY nombre_completo")


def _saldo_por_movimiento(producto_id: int) -> pd.Series:
    """
    Saldo del producto tras cada movimiento (índice = id del movimiento).
    Se calcula hacia atrás desde el stock actual, de modo que SIEMPRE cuadra con el inventario
    aunque existiera stock previo a la activación del kardex ("saldo inicial" implícito).
    """
    mov = db.fetch_df("SELECT id, cantidad FROM movimientos_inventario WHERE producto_id = ? ORDER BY id",
                      (producto_id,))
    fila = db.fetch_one("SELECT COALESCE(SUM(cantidad_actual), 0) FROM lotes_inventario "
                        "WHERE producto_id = ? AND activo = 1", (producto_id,))
    stock_total = float(fila[0]) if fila else 0.0
    saldo_inicial = stock_total - float(mov["cantidad"].sum())
    saldo = mov["cantidad"].cumsum() + saldo_inicial
    return pd.Series(saldo.round(inv.DECIMALES).values, index=mov["id"].values)


def saldo_inicial_producto(producto_id: int) -> float:
    """Stock del producto ANTES del primer movimiento registrado en el kardex."""
    s = _saldo_por_movimiento(producto_id)
    mov = db.fetch_df("SELECT id, cantidad FROM movimientos_inventario WHERE producto_id = ? ORDER BY id LIMIT 1",
                      (producto_id,))
    if s.empty or mov.empty:
        fila = db.fetch_one("SELECT COALESCE(SUM(cantidad_actual), 0) FROM lotes_inventario "
                            "WHERE producto_id = ? AND activo = 1", (producto_id,))
        return float(fila[0]) if fila else 0.0
    return round(float(s.iloc[0]) - float(mov["cantidad"].iloc[0]), inv.DECIMALES)


def kardex(desde: date, hasta: date, *, producto_id: Optional[int] = None, usuario_id: Optional[int] = None,
           tipos: Optional[list[str]] = None, texto: str = "") -> pd.DataFrame:
    """
    Historial de movimientos con trazabilidad completa (usuario, fecha y hora, tipo, lote, referencia).
    Si se filtra por un producto, incluye el saldo acumulado tras cada movimiento.
    """
    df = db.fetch_df(
        """SELECT m.id, m.fecha, m.tipo, p.codigo, p.nombre AS producto, p.unidad_medida AS unidad,
                  m.numero_lote AS lote, l.fecha_vencimiento AS vence, oc.numero_oc AS oc,
                  f.numero_factura AS factura, m.cantidad, m.motivo, m.referencia,
                  m.guia_remision AS guia, m.cliente_nombre AS cliente, m.cliente_documento AS cliente_doc,
                  m.cliente_direccion AS cliente_direccion,
                  COALESCE(u.nombre_completo, '—') AS usuario, COALESCE(u.username, '—') AS username
           FROM movimientos_inventario m
           JOIN productos p ON p.id = m.producto_id
           LEFT JOIN lotes_inventario l ON l.id = m.lote_id
           LEFT JOIN ordenes_compra oc ON oc.id = l.orden_compra_id
           LEFT JOIN facturas f ON f.id = l.factura_id
           LEFT JOIN usuarios u ON u.id = m.usuario_id
           WHERE date(m.fecha) BETWEEN :desde AND :hasta
             AND (:pid IS NULL OR m.producto_id = :pid)
             AND (:uid IS NULL OR m.usuario_id = :uid)
           ORDER BY m.id DESC""",
        {"desde": desde.isoformat(), "hasta": hasta.isoformat(), "pid": producto_id, "uid": usuario_id})

    df["entrada"] = df["cantidad"].clip(lower=0)
    df["salida"] = (-df["cantidad"]).clip(lower=0)
    if producto_id is not None:
        df["saldo"] = df["id"].map(_saldo_por_movimiento(producto_id))
    else:
        df["saldo"] = float("nan")
    if tipos:
        df = df[df["tipo"].isin(tipos)]
    if texto.strip():
        df = inv._aplicar_busqueda(df, texto, ["codigo", "producto", "lote", "motivo", "referencia", "oc", "factura", "guia", "cliente",
                                             "cliente_doc"])
    return df.reset_index(drop=True)
