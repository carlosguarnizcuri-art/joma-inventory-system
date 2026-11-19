"""
modulos/documentos_pdf.py - Generación de documentos imprimibles (FASE 3)

Un solo generador reutilizable para:
  * Vista previa / documento de la Orden de Compra.
  * Pre-picking (lista de extracción para almacén).
  * Packing List (lista de empaque).
Cada documento se entrega en PDF (reportlab) y en Excel (.xlsx, pandas + BytesIO).

Requiere:  pip install reportlab
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any, Optional, Sequence
from xml.sax.saxutils import escape

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Flowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from modulos import empresa
from modulos import exportacion as exp

EMPRESA = "JOMA S.A.C."
COLOR_CABECERA = colors.HexColor("#1F3A5F")
COLOR_FILA_ALTERNA = colors.HexColor("#F3F6FA")
MIME_PDF = "application/pdf"


def _limpio(valor: Any) -> str:
    """Texto seguro para fuentes estándar del PDF (Latin-1): sin emojis ni caracteres no soportados."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    if isinstance(valor, float):
        valor = f"{valor:,.2f}" if valor != int(valor) else f"{int(valor):,}"
    return str(valor).encode("cp1252", errors="replace").decode("cp1252")


def _p(valor: Any, estilo: ParagraphStyle, negrita: bool = False) -> Paragraph:
    texto = escape(_limpio(valor)).replace("\n", "<br/>")
    return Paragraph(f"<b>{texto}</b>" if negrita else texto, estilo)


class _LogoTexto(Flowable):
    """Logotipo tipográfico de respaldo (se usa mientras no exista assets/logo_joma.png)."""

    def __init__(self, alto: float):
        super().__init__()
        self.alto, self.ancho = alto, alto * 2.7

    def wrap(self, _w, _h):
        return self.ancho, self.alto

    def draw(self):
        c = self.canv
        c.setFillColor(COLOR_CABECERA)
        c.roundRect(0, 0, self.ancho, self.alto, 7, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", self.alto * 0.46)
        c.drawCentredString(self.ancho / 2, self.alto * 0.42, "JOMA")
        c.setFont("Helvetica", self.alto * 0.15)
        c.drawCentredString(self.ancho / 2, self.alto * 0.13, "DISTRIBUIDORA S.A.C.")


def _logo(alto: float = 1.7 * cm):
    """Logotipo oficial (assets/logo_joma.png) escalado a 'alto'; si no existe, el logotipo de respaldo."""
    ruta = empresa.ruta_logo()
    if ruta is not None:
        try:
            ancho_px, alto_px = ImageReader(str(ruta)).getSize()
            return Image(str(ruta), width=alto * ancho_px / alto_px, height=alto)
        except Exception:      # imagen dañada: se usa el logotipo de respaldo
            pass
    return _LogoTexto(alto)


def _membrete(ancho: float, estilo_empresa: ParagraphStyle, estilo_texto: ParagraphStyle):
    """Cabecera corporativa: logotipo a la izquierda y datos fiscales (razón social, RUC, domicilio) a la derecha."""
    lineas = empresa.lineas_membrete()
    textos = [_p(lineas[0], estilo_empresa)] + [_p(l, estilo_texto) for l in lineas[1:]]
    t = Table([[_logo(), textos]], colWidths=[ancho * 0.34, ancho * 0.66])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("LINEBELOW", (0, 0), (-1, 0), 1.2, COLOR_CABECERA), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def pdf_documento(titulo: str, *, encabezado: Sequence[tuple[str, str]], columnas: Sequence[str],
                  filas: Sequence[Sequence[Any]], anchos: Optional[Sequence[float]] = None,
                  alinear_derecha: Sequence[int] = (), totales: Sequence[tuple[str, str]] = (),
                  notas: Sequence[str] = (), firmas: Sequence[str] = (), horizontal: bool = False,
                  generado_por: str = "", membrete: bool = False) -> bytes:
    """
    Construye un PDF A4 con: empresa, título, datos de cabecera (pares etiqueta/valor), tabla principal
    (con encabezado repetido en cada página), totales, notas y casillas de firma. Devuelve los bytes.
      anchos          -> pesos relativos de las columnas (opcional)
      alinear_derecha -> índices de columnas numéricas
      membrete        -> True para documentos oficiales: logotipo + datos fiscales de la empresa (empresa.py)
    """
    buffer = BytesIO()
    tam = landscape(A4) if horizontal else A4
    doc = SimpleDocTemplate(buffer, pagesize=tam, leftMargin=1.5 * cm, rightMargin=1.5 * cm, topMargin=1.4 * cm,
                            bottomMargin=1.8 * cm, title=_limpio(titulo), author=EMPRESA)
    ancho = tam[0] - 3 * cm
    base = getSampleStyleSheet()
    s_emp = ParagraphStyle("emp", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=13, spaceAfter=0)
    s_tit = ParagraphStyle("tit", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=16, spaceAfter=8)
    s_txt = ParagraphStyle("txt", parent=base["BodyText"], fontName="Helvetica", fontSize=9, leading=11)
    s_cel = ParagraphStyle("cel", parent=s_txt, fontSize=8, leading=9.5)
    s_der = ParagraphStyle("der", parent=s_cel, alignment=TA_RIGHT)
    s_cab = ParagraphStyle("cab", parent=s_cel, fontName="Helvetica-Bold", textColor=colors.white)

    if membrete:
        elems: list = [_membrete(ancho, s_emp, s_txt), Spacer(1, 8), _p(titulo, s_tit)]
    else:
        elems = [_p(EMPRESA, s_emp), _p(titulo, s_tit)]

    if encabezado:
        filas_enc = []
        for i in range(0, len(encabezado), 2):
            fila: list = []
            for etiqueta, valor in encabezado[i:i + 2]:
                fila += [_p(f"{etiqueta}:", s_txt, negrita=True), _p(valor, s_txt)]
            while len(fila) < 4:
                fila += ["", ""]
            filas_enc.append(fila)
        t = Table(filas_enc, colWidths=[ancho * 0.19, ancho * 0.31, ancho * 0.19, ancho * 0.31])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        elems += [t, Spacer(1, 8)]

    derecha = set(alinear_derecha)
    datos = [[_p(c, s_cab) for c in columnas]]
    for fila in filas:
        datos.append([_p(v, s_der if i in derecha else s_cel) for i, v in enumerate(fila)])
    if not filas:
        datos.append([_p("Sin registros", s_cel)] + [""] * (len(columnas) - 1))
    pesos = list(anchos) if anchos else [1] * len(columnas)
    total_pesos = sum(pesos)
    tabla = Table(datos, colWidths=[ancho * p / total_pesos for p in pesos], repeatRows=1)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_CABECERA),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9AA5B1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, COLOR_FILA_ALTERNA]),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elems.append(tabla)

    if totales:
        elems.append(Spacer(1, 8))
        t = Table([[_p(f"{k}:", s_txt, negrita=True), _p(v, s_der, negrita=True)] for k, v in totales],
                  colWidths=[ancho * 0.22, ancho * 0.18], hAlign="RIGHT")
        t.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        elems.append(t)

    for nota in notas:
        elems += [Spacer(1, 6), _p(nota, s_cel)]

    if firmas:
        elems.append(Spacer(1, 42))
        # columnas alternadas (firma, espacio, firma...) para que cada casilla tenga su propia línea
        fila_vacia, fila_texto, anchos_f = [], [], []
        for i, f in enumerate(firmas):
            if i:
                fila_vacia.append(""); fila_texto.append(""); anchos_f.append(ancho * 0.06)
            fila_vacia.append(""); fila_texto.append(_p(f, ParagraphStyle("fir", parent=s_cel, alignment=1)))
            anchos_f.append((ancho - ancho * 0.06 * (len(firmas) - 1)) / len(firmas))
        t = Table([fila_vacia, fila_texto], colWidths=anchos_f)
        estilo_f = [("TOPPADDING", (0, 1), (-1, 1), 3)]
        for c in range(0, len(anchos_f), 2):
            estilo_f.append(("LINEABOVE", (c, 1), (c, 1), 0.6, colors.black))
        t.setStyle(TableStyle(estilo_f))
        elems.append(t)

    pie_texto = f"{EMPRESA} · Generado por {_limpio(generado_por) or '—'} el {datetime.now():%d/%m/%Y %H:%M}"

    def pie(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(1.5 * cm, 0.95 * cm, pie_texto)
        canvas.drawRightString(tam[0] - 1.5 * cm, 0.95 * cm, f"Página {doc_.page}")
        canvas.restoreState()

    doc.build(elems, onFirstPage=pie, onLaterPages=pie)
    return buffer.getvalue()


def excel_documento(titulo: str, *, encabezado: Sequence[tuple[str, str]], columnas: Sequence[str],
                    filas: Sequence[Sequence[Any]], totales: Sequence[tuple[str, str]] = (),
                    nombre_hoja: str = "Documento", generado_por: str = "") -> bytes:
    """Mismo documento en Excel: la tabla en la primera hoja y los datos de cabecera en 'Parámetros'."""
    df = pd.DataFrame([list(f) for f in filas], columns=list(columnas))
    parametros = {"Documento": titulo, **dict(encabezado), **dict(totales), "Generado por": generado_por,
                  "Fecha de generación": datetime.now().strftime("%d/%m/%Y %H:%M")}
    return exp.excel_para_descarga({nombre_hoja: df}, parametros=parametros)
