"""
modulos/documentos.py - Gestión documental local (MÓDULO 3)

Responsabilidades:
  * Estructura de carpetas bajo data/documentos/:
        data/documentos/ordenes_compra/<N_OC>/
        data/documentos/facturas/<N_FACTURA>_<PROVEEDOR>/
  * Adjuntar archivos (PDF, imágenes, Excel, XML...) a una OC o factura.
  * Vincular rutas EXTERNAS (archivos o carpetas locales / de red) sin copiarlas.
  * Sincronizar una carpeta: registrar los archivos que se copiaron a mano.
  * Lectura segura de documentos para descarga.

Seguridad:
  * Solo Admin escribe (validado contra la BD en cada función de escritura).
  * Los nombres de archivo se sanean (sin rutas, sin '..', solo ASCII seguro).
  * Los archivos subidos SIEMPRE se guardan dentro de data/documentos.
  * Para descargar se lee únicamente lo registrado en la BD (por id), nunca una
    ruta escrita por el usuario que consulta.
"""

from __future__ import annotations

import mimetypes
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

import database as db

# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------
DOCS_DIR: Path = db.DOCS_DIR
EXTENSIONES_PERMITIDAS = (".pdf", ".png", ".jpg", ".jpeg", ".xlsx", ".xls", ".docx", ".doc",
                          ".xml", ".zip", ".csv", ".txt")
MAX_MB = 25
MAX_BYTES = MAX_MB * 1024 * 1024

CARPETAS_BASE = {"OC": "ordenes_compra", "FACTURA": "facturas"}

# Metadatos por tipo de entidad (SOLO constantes: se interpolan en SQL de forma segura)
ENTIDADES = {
    "OC":      {"tabla": "ordenes_compra", "col_ruta": "ruta_carpeta",   "col_numero": "numero_oc"},
    "FACTURA": {"tabla": "facturas",       "col_ruta": "ruta_documento", "col_numero": "numero_factura"},
}


# ---------------------------------------------------------------------------
# SEGURIDAD Y UTILIDADES DE RUTAS
# ---------------------------------------------------------------------------
def exigir_admin(usuario: dict[str, Any]) -> None:
    """Verifica en la BD (no solo en la sesión) que el usuario sea Admin activo."""
    fila = db.fetch_one("SELECT rol, activo FROM usuarios WHERE id = ?", (usuario.get("id"),))
    if fila is None or not fila["activo"] or fila["rol"] != "Admin":
        raise PermissionError("Acción permitida solo para usuarios con rol Admin.")


def _validar_tipo(tipo: str) -> None:
    if tipo not in ENTIDADES:
        raise ValueError(f"Tipo de documento inválido: {tipo}")


def slug(texto: str, maximo: int = 80) -> str:
    """Convierte texto en un nombre seguro para archivos/carpetas (ASCII, sin '..' ni separadores)."""
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[^A-Za-z0-9._-]+", "_", t).strip("._-")
    return t[:maximo] or "sin_nombre"


def dentro_de_docs(ruta: Path) -> bool:
    """True si 'ruta' (existente o no) queda dentro de data/documentos (evita path traversal)."""
    try:
        ruta.resolve().relative_to(DOCS_DIR.resolve())
        return True
    except (ValueError, OSError):
        return False


def a_relativa(ruta: Path) -> str:
    """Ruta relativa a data/documentos en formato POSIX (portable si se mueve el proyecto)."""
    return ruta.resolve().relative_to(DOCS_DIR.resolve()).as_posix()


def crear_carpeta(ruta: Path) -> None:
    try:
        ruta.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"No se pudo crear la carpeta '{ruta}': {e}") from None


def inicializar_estructura() -> None:
    """Crea el árbol base de carpetas (idempotente)."""
    for sub in CARPETAS_BASE.values():
        (DOCS_DIR / sub).mkdir(parents=True, exist_ok=True)


def carpeta_por_defecto(tipo: str, numero: str, proveedor: str = "") -> Path:
    """Carpeta estándar de una OC o factura."""
    _validar_tipo(tipo)
    base = DOCS_DIR / CARPETAS_BASE[tipo]
    nombre = slug(numero) if tipo == "OC" else slug(f"{numero}_{proveedor}")
    return base / nombre


def _resolver_guardada(valor: Optional[str]) -> Optional[Path]:
    """Convierte la ruta guardada en BD (relativa o absoluta heredada) en Path."""
    if not valor:
        return None
    p = Path(valor)
    return p if p.is_absolute() else DOCS_DIR / p


def carpeta_entidad(tipo: str, entidad_id: int) -> Path:
    """Carpeta efectiva (en disco) de una OC/factura. No la crea. Si la ruta guardada
    quedó fuera de data/documentos (p. ej. se movió el proyecto), usa la estándar."""
    _validar_tipo(tipo)
    meta = ENTIDADES[tipo]
    fila = db.fetch_one(
        f"SELECT {meta['col_numero']} AS numero, proveedor, {meta['col_ruta']} AS ruta "
        f"FROM {meta['tabla']} WHERE id = ?", (entidad_id,))
    if fila is None:
        raise ValueError("El registro no existe.")
    p = _resolver_guardada(fila["ruta"])
    if p is None or not dentro_de_docs(p):
        p = carpeta_por_defecto(tipo, fila["numero"], fila["proveedor"])
    return p


def _guardar_carpeta(conn, tipo: str, entidad_id: int, carpeta: Path) -> None:
    meta = ENTIDADES[tipo]
    conn.execute(f"UPDATE {meta['tabla']} SET {meta['col_ruta']} = ? WHERE id = ?",
                 (a_relativa(carpeta), entidad_id))


def asegurar_carpeta_entidad(usuario: dict, tipo: str, entidad_id: int) -> Path:
    """Crea (o repara) la carpeta de la OC/factura y deja su ruta registrada en la BD."""
    exigir_admin(usuario)
    carpeta = carpeta_entidad(tipo, entidad_id)
    crear_carpeta(carpeta)
    with db.db_session() as conn:
        _guardar_carpeta(conn, tipo, entidad_id, carpeta)
    return carpeta


def formatear_tamano(n: Optional[float]) -> str:
    if n is None or pd.isna(n):
        return "—"
    n = float(n)
    for unidad in ("B", "KB", "MB", "GB"):
        if n < 1024 or unidad == "GB":
            return f"{n:.0f} {unidad}" if unidad == "B" else f"{n:.1f} {unidad}"
        n /= 1024
    return "—"


# ---------------------------------------------------------------------------
# REGISTRO DE DOCUMENTOS
# ---------------------------------------------------------------------------
def _nombre_unico(carpeta: Path, nombre: str) -> Path:
    """Evita sobrescribir: si el nombre existe, agrega fecha/hora."""
    destino = carpeta / nombre
    if not destino.exists():
        return destino
    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    destino = carpeta / f"{Path(nombre).stem}_{marca}{Path(nombre).suffix}"
    n = 1
    while destino.exists():
        destino = carpeta / f"{Path(nombre).stem}_{marca}_{n}{Path(nombre).suffix}"
        n += 1
    return destino


def guardar_archivo(usuario: dict, tipo: str, entidad_id: int, nombre_original: str,
                    contenido: bytes, descripcion: str = "") -> int:
    """Guarda un archivo subido en la carpeta de la OC/factura y lo registra en la BD."""
    exigir_admin(usuario)
    _validar_tipo(tipo)

    nombre_original = Path((nombre_original or "").replace("\\", "/")).name   # descarta rutas del navegador
    ext = Path(nombre_original).suffix.lower()
    if ext not in EXTENSIONES_PERMITIDAS:
        raise ValueError(f"Tipo de archivo no permitido ({ext or 'sin extensión'}). "
                         f"Permitidos: {', '.join(EXTENSIONES_PERMITIDAS)}.")
    if not contenido:
        raise ValueError(f"El archivo '{nombre_original}' está vacío.")
    if len(contenido) > MAX_BYTES:
        raise ValueError(f"'{nombre_original}' supera el máximo de {MAX_MB} MB.")

    carpeta = carpeta_entidad(tipo, entidad_id)
    crear_carpeta(carpeta)
    destino = _nombre_unico(carpeta, f"{slug(Path(nombre_original).stem)}{ext}")
    try:
        destino.write_bytes(contenido)
    except OSError as e:
        raise ValueError(f"No se pudo guardar el archivo: {e}") from None

    try:
        with db.db_session() as conn:
            cur = conn.execute(
                """INSERT INTO documentos_adjuntos
                   (tipo_entidad, entidad_id, tipo_vinculo, nombre, ruta, descripcion, tamano_bytes, subido_por)
                   VALUES (?, ?, 'ARCHIVO', ?, ?, ?, ?, ?)""",
                (tipo, entidad_id, destino.name, a_relativa(destino),
                 (descripcion or "").strip() or None, len(contenido), usuario["id"]),
            )
            _guardar_carpeta(conn, tipo, entidad_id, carpeta)
            return cur.lastrowid
    except Exception:
        destino.unlink(missing_ok=True)   # no dejar archivos huérfanos si falla la BD
        raise


def vincular_ruta_externa(usuario: dict, tipo: str, entidad_id: int, ruta_texto: str,
                          descripcion: str = "") -> int:
    """Registra la ruta de un archivo o carpeta existente (local o de red) sin copiarlo."""
    exigir_admin(usuario)
    _validar_tipo(tipo)

    txt = (ruta_texto or "").strip().strip('"').strip("'")   # admite 'Copiar como ruta' de Windows
    if not txt:
        raise ValueError("Ingrese la ruta del archivo o carpeta.")
    p = Path(txt).expanduser()
    if not p.is_absolute():
        raise ValueError("Ingrese una ruta completa (ej. C:\\Documentos\\Facturas\\F001.pdf "
                         "o \\\\servidor\\carpeta).")
    try:
        existe = p.exists()
    except OSError:
        existe = False
    if not existe:
        raise ValueError("La ruta no existe o no es accesible desde este equipo.")
    if p.is_file() and p.suffix.lower() not in EXTENSIONES_PERMITIDAS:
        raise ValueError(f"Tipo de archivo no permitido ({p.suffix or 'sin extensión'}).")

    # Si ya está dentro de data/documentos, se registra como archivo propio (ruta portable)
    if p.is_file() and dentro_de_docs(p):
        vinculo, ruta_guardada = "ARCHIVO", a_relativa(p)
    else:
        vinculo, ruta_guardada = "RUTA_EXTERNA", str(p)

    with db.db_session() as conn:
        if conn.execute("SELECT 1 FROM documentos_adjuntos WHERE tipo_entidad = ? AND entidad_id = ? AND ruta = ?",
                        (tipo, entidad_id, ruta_guardada)).fetchone():
            raise ValueError("Esa ruta ya está vinculada.")
        cur = conn.execute(
            """INSERT INTO documentos_adjuntos
               (tipo_entidad, entidad_id, tipo_vinculo, nombre, ruta, descripcion, tamano_bytes, subido_por)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (tipo, entidad_id, vinculo, p.name or str(p), ruta_guardada,
             (descripcion or "").strip() or None, p.stat().st_size if p.is_file() else None, usuario["id"]),
        )
        return cur.lastrowid


def escanear_carpeta(usuario: dict, tipo: str, entidad_id: int) -> int:
    """Registra los archivos que existan en la carpeta de la OC/factura y aún no estén en la BD."""
    exigir_admin(usuario)
    carpeta = carpeta_entidad(tipo, entidad_id)
    if not carpeta.is_dir():
        raise ValueError("La carpeta aún no existe. Use 'Crear / reparar carpeta' primero.")

    with db.db_session() as conn:
        registrados = {r["ruta"] for r in conn.execute(
            "SELECT ruta FROM documentos_adjuntos WHERE tipo_entidad = ? AND entidad_id = ?", (tipo, entidad_id))}
        nuevos = []
        for f in sorted(carpeta.iterdir()):
            if (f.is_file() and not f.name.startswith(".") and f.suffix.lower() in EXTENSIONES_PERMITIDAS
                    and a_relativa(f) not in registrados):
                nuevos.append((tipo, entidad_id, f.name, a_relativa(f), f.stat().st_size, usuario["id"]))
        conn.executemany(
            """INSERT INTO documentos_adjuntos
               (tipo_entidad, entidad_id, tipo_vinculo, nombre, ruta, descripcion, tamano_bytes, subido_por)
               VALUES (?, ?, 'ARCHIVO', ?, ?, 'Detectado al sincronizar la carpeta', ?, ?)""",
            nuevos,
        )
        _guardar_carpeta(conn, tipo, entidad_id, carpeta)
    return len(nuevos)


# ---------------------------------------------------------------------------
# LECTURA
# ---------------------------------------------------------------------------
def _ruta_real(vinculo: str, ruta: str) -> Optional[Path]:
    """Resuelve la ruta física de un documento registrado (None si no es segura)."""
    if vinculo == "ARCHIVO":
        p = DOCS_DIR / ruta
        return p if dentro_de_docs(p) else None
    return Path(ruta)


def _disponible(vinculo: str, ruta: str) -> bool:
    try:
        p = _ruta_real(vinculo, ruta)
        return bool(p and p.exists())
    except OSError:
        return False


def listar_documentos(tipo: str, entidad_id: int) -> pd.DataFrame:
    _validar_tipo(tipo)
    df = db.fetch_df(
        """SELECT d.id, d.nombre, d.tipo_vinculo, d.ruta, d.descripcion, d.tamano_bytes, d.subido_en,
                  COALESCE(u.nombre_completo, '—') AS subido_por
           FROM documentos_adjuntos d LEFT JOIN usuarios u ON u.id = d.subido_por
           WHERE d.tipo_entidad = ? AND d.entidad_id = ?
           ORDER BY d.id DESC""",
        (tipo, entidad_id),
    )
    df["disponible"] = [_disponible(v, r) for v, r in zip(df["tipo_vinculo"], df["ruta"])]
    return df


def leer_documento(doc_id: int) -> tuple[bytes, str, str]:
    """Devuelve (contenido, nombre, mime) de un documento REGISTRADO. Solo archivos permitidos."""
    fila = db.fetch_one("SELECT nombre, tipo_vinculo, ruta FROM documentos_adjuntos WHERE id = ?", (doc_id,))
    if fila is None:
        raise ValueError("El documento no está registrado.")
    p = _ruta_real(fila["tipo_vinculo"], fila["ruta"])
    try:
        ok = p is not None and p.is_file()
    except OSError:
        ok = False
    if not ok:
        raise ValueError("El archivo no está disponible (se movió, se eliminó o no es un archivo).")
    if p.suffix.lower() not in EXTENSIONES_PERMITIDAS:
        raise ValueError("Tipo de archivo no permitido para descarga.")
    if p.stat().st_size > MAX_BYTES:
        raise ValueError(f"El archivo supera {MAX_MB} MB; ábralo directamente desde su ubicación.")
    return p.read_bytes(), p.name, mimetypes.guess_type(p.name)[0] or "application/octet-stream"


# ---------------------------------------------------------------------------
# ELIMINACIÓN Y UTILIDADES DE SISTEMA
# ---------------------------------------------------------------------------
def eliminar_documento(usuario: dict, doc_id: int, borrar_archivo: bool = False) -> None:
    """Quita el registro. El archivo físico solo se borra si se pide y está dentro de data/documentos."""
    exigir_admin(usuario)
    with db.db_session() as conn:
        fila = conn.execute("SELECT tipo_vinculo, ruta FROM documentos_adjuntos WHERE id = ?", (doc_id,)).fetchone()
        if fila is None:
            raise ValueError("El documento no existe.")
        conn.execute("DELETE FROM documentos_adjuntos WHERE id = ?", (doc_id,))
    if borrar_archivo and fila["tipo_vinculo"] == "ARCHIVO":
        p = _ruta_real("ARCHIVO", fila["ruta"])
        try:
            if p is not None:
                p.unlink(missing_ok=True)
        except OSError as e:
            raise ValueError(f"Se quitó el registro, pero no se pudo borrar el archivo: {e}") from None


def abrir_en_explorador(usuario: dict, carpeta: Path) -> None:
    """Abre la carpeta en el explorador del equipo donde corre el sistema (uso local)."""
    exigir_admin(usuario)
    if not dentro_de_docs(carpeta) or not carpeta.is_dir():
        raise ValueError("La carpeta no existe todavía. Créela primero.")
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(carpeta))                       # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(carpeta)])
        else:
            subprocess.Popen(["xdg-open", str(carpeta)])
    except OSError as e:
        raise ValueError(f"No se pudo abrir el explorador: {e}") from None
