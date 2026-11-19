"""
modulos/empresa.py - Datos fiscales y logotipo corporativo (FASE 4)

Se usan en el membrete de los documentos oficiales (Orden de Venta, comprobantes). Edite las
constantes de abajo con los datos reales de la empresa.

LOGOTIPO OFICIAL: copie el archivo del logo en  assets/logo_joma.png  (también se acepta .jpg).
Si el archivo no existe, los PDF usan un logotipo tipográfico genérico "JOMA" para no quedar sin
identidad; en cuanto agregue el archivo, todos los documentos nuevos lo usan automáticamente.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import database as db

RAZON_SOCIAL = "JOMA S.A.C."
NOMBRE_COMERCIAL = "DISTRIBUIDORA JOMA S.A.C."
RUC = "20615590496"
DIRECCION = ""          # ← complete el domicilio fiscal (se imprime solo si no está vacío)
TELEFONO = ""           # ← opcional
EMAIL = ""              # ← opcional

DIR_ASSETS: Path = db.BASE_DIR / "assets"
LOGOS_ACEPTADOS = ("logo_joma.png", "logo_joma.jpg", "logo_joma.jpeg")


def ruta_logo() -> Optional[Path]:
    """Ruta del logotipo oficial si existe; None si aún no se ha colocado el archivo."""
    for nombre in LOGOS_ACEPTADOS:
        ruta = DIR_ASSETS / nombre
        if ruta.is_file():
            return ruta
    return None


def lineas_membrete() -> list[str]:
    """Líneas de texto del membrete (las vacías se omiten)."""
    contacto = " · ".join(x for x in (f"Tel. {TELEFONO}" if TELEFONO else "", EMAIL) if x)
    return [l for l in (NOMBRE_COMERCIAL, f"RUC: {RUC}", DIRECCION, contacto) if l]
