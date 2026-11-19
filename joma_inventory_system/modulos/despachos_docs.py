"""
modulos/despachos_docs.py - Documentos de despacho (FASE 4, sin interfaz): Pre-picking, Packing List y pedido.
Los usan despachos.py (descargas) y despachos_db.py (Packing List automático en el expediente de la OV).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from modulos import documentos_pdf as pdf


def fmt_fecha(v: Any) -> str:
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return "—"


def cabecera(d) -> list[tuple[str, str]]:
    return [("N° de despacho", d["numero_despacho"]), ("Fecha", fmt_fecha(d["fecha"])),
            ("Orden de Venta", d["numero_ov"] or "—"), ("OC del cliente", d["oc_cliente"] or "—"),
            ("Cliente", d["cliente_nombre"]), (d["tipo_documento"], d["numero_documento"]),
            ("Dirección", d["cliente_direccion"] or "—"), ("Guía de remisión", d["guia_remision"] or "—"),
            ("Estado", d["estado"])]


def doc_prepicking(plan: pd.DataFrame, faltantes: list[dict]):
    cols = ["#", "SKU", "Descripción", "Unidad", "Ubicación", "Lote", "Vence", "Cantidad", "Pick"]
    pdf_rows, xl_rows = [], []
    for i, r in enumerate(plan.itertuples(), start=1):
        pdf_rows.append([i, r.codigo, r.producto, r.unidad, r.ubicacion or "—", r.lote, fmt_fecha(r.vence),
                         f"{r.cantidad:g}", "[    ]"])
        xl_rows.append([i, r.codigo, r.producto, r.unidad, r.ubicacion or "", r.lote, fmt_fecha(r.vence),
                        float(r.cantidad), ""])
    totales = [("Líneas de picking", str(len(plan))), ("Total de unidades", f"{plan['cantidad'].sum():g}")]
    notas = ["PRE-PICKING: extraiga la mercadería en el orden indicado (ubicación) respetando FEFO: "
             "primero lo que vence antes."]
    if faltantes:
        notas.append("ATENCIÓN - STOCK INSUFICIENTE: " + "; ".join(f"{f['codigo']} (faltan {f['faltante']:g})" for f in faltantes))
    return cols, pdf_rows, xl_rows, totales, notas


def doc_packing(plan: pd.DataFrame):
    cols = ["#", "SKU", "Descripción", "Unidad", "Cantidad", "Ubicación", "Lote(s)"]
    pdf_rows, xl_rows = [], []
    if not plan.empty:
        for i, (codigo, g) in enumerate(plan.groupby("codigo", sort=False), start=1):
            ubic = ", ".join(sorted({u for u in g["ubicacion"] if u})) or "—"
            lotes = ", ".join(f"{r.lote} ({r.cantidad:g})" for r in g.itertuples())
            total = float(g["cantidad"].sum())
            pdf_rows.append([i, codigo, g["producto"].iloc[0], g["unidad"].iloc[0], f"{total:g}", ubic, lotes])
            xl_rows.append([i, codigo, g["producto"].iloc[0], g["unidad"].iloc[0], total, ubic, lotes])
    totales = [("Total de ítems", str(len(pdf_rows))), ("Total de unidades", f"{plan['cantidad'].sum():g}" if not plan.empty else "0")]
    return cols, pdf_rows, xl_rows, totales


def doc_pedido(lineas: pd.DataFrame):
    cols = ["#", "SKU", "Descripción", "Unidad", "Cantidad", "Precio ref.", "Importe ref."]
    pdf_rows, xl_rows = [], []
    for i, r in enumerate(lineas.itertuples(), start=1):
        pdf_rows.append([i, r.codigo, r.producto, r.unidad, f"{r.cantidad:g}", f"{r.precio_venta:,.2f}", f"{r.importe_ref:,.2f}"])
        xl_rows.append([i, r.codigo, r.producto, r.unidad, float(r.cantidad), float(r.precio_venta), float(r.importe_ref)])
    return cols, pdf_rows, xl_rows, [("TOTAL REFERENCIAL", f"S/ {lineas['importe_ref'].sum():,.2f}")]


def pdf_prepicking(d, plan: pd.DataFrame, faltantes: list[dict], generado_por: str = "") -> bytes:
    cols, rows, _, tot, notas = doc_prepicking(plan, faltantes)
    return pdf.pdf_documento(f"Pre-picking {d['numero_despacho']}", encabezado=cabecera(d), columnas=cols, filas=rows,
                             anchos=[0.5, 1.4, 4, 1, 1.4, 1.6, 1.3, 1.1, 0.9], alinear_derecha=[7], totales=tot, notas=notas,
                             firmas=["Preparado por (almacén)", "Verificado por"], horizontal=True, generado_por=generado_por)


def pdf_packing(d, plan: pd.DataFrame, generado_por: str = "") -> bytes:
    cols, rows, _, tot = doc_packing(plan)
    return pdf.pdf_documento(f"Packing List {d['numero_despacho']}", encabezado=cabecera(d), columnas=cols, filas=rows,
                             anchos=[0.5, 1.4, 4, 1, 1.2, 1.6, 3], alinear_derecha=[4], totales=tot,
                             notas=["Verifique cantidades y estado de la mercadería antes de despachar."],
                             firmas=["Armado por", "Verificado por", "Recibido conforme"], generado_por=generado_por, membrete=True)


def pdf_pedido(d, lineas: pd.DataFrame, generado_por: str = "") -> bytes:
    cols, rows, _, tot = doc_pedido(lineas)
    return pdf.pdf_documento(f"Pedido {d['numero_despacho']}", encabezado=cabecera(d), columnas=cols, filas=rows,
                             anchos=[0.5, 1.4, 4, 1, 1.2, 1.4, 1.5], alinear_derecha=[4, 5, 6], totales=tot,
                             notas=["Precios de lista de referencia; no incluyen impuestos."], generado_por=generado_por, membrete=True)
