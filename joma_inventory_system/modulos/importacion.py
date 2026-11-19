"""
modulos/importacion.py - Carga masiva desde Excel (.xlsx) o CSV (FASE 3, sin código de interfaz)

Tres tipos de carga (todas con validación COMPLETA antes de tocar la base de datos):
  * entradas : ingreso de stock por lote (código, lote, cantidad, costo, vencimiento, ubicación...).
  * salidas  : despachos masivos con FEFO (código, cantidad, RUC/DNI del cliente, guía de remisión...).
  * oc       : órdenes de compra con su detalle (las filas con la misma 'ref_oc' forman UNA orden;
               el número OC-NNNNN se asigna automáticamente).

Flujo: leer_archivo() -> validar_xxx() -> (el usuario revisa el reporte) -> ejecutar_importacion().
  * entradas y salidas se importan en UNA transacción: o entra todo o no entra nada.
  * oc: cada orden es atómica; si una falla, las demás se crean y se informa cuál falló.
Solo el rol Admin importa (validado contra la BD). Cada importación queda en la auditoría.

Formatos admitidos: números con punto o coma decimal; fechas dd/mm/aaaa o aaaa-mm-dd; CSV con
separador ; , tab o | y codificación UTF-8 o Windows-1252.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Optional

import pandas as pd

import database as db
from modulos import admin_db, clientes_db, identidad
from modulos import compras_db as cdb
from modulos import exportacion as exp
from modulos import inventario as inv

MAX_BYTES = 5 * 1024 * 1024
MAX_FILAS = 5000
DECIMALES = inv.DECIMALES


# ===========================================================================
# ESPECIFICACIÓN DE PLANTILLAS
# ===========================================================================
@dataclass(frozen=True)
class Columna:
    nombre: str
    requerida: bool
    descripcion: str
    ejemplo: str


ESPECIFICACIONES: dict[str, dict[str, Any]] = {
    "entradas": {
        "titulo": "Entradas de stock (ingreso por lote)",
        "archivo": "plantilla_entradas",
        "columnas": [
            Columna("codigo_producto", True, "Código (SKU) de un producto ya registrado.", "PRD-001"),
            Columna("numero_lote", True, "Número de lote (único por producto).", "L2612-A"),
            Columna("cantidad", True, "Unidades que ingresan (mayor que 0).", "100"),
            Columna("costo_unitario", False, "Costo por unidad. Actualiza el costo del producto si marca esa opción.", "6.50"),
            Columna("margen_pct", False, "Margen de ganancia en %. Precio de venta = costo × (1 + margen/100).", "30"),
            Columna("fecha_vencimiento", False, "dd/mm/aaaa. Vacío = producto no perecible.", "31/12/2027"),
            Columna("fecha_ingreso", False, "dd/mm/aaaa. Vacío = hoy.", "15/11/2026"),
            Columna("ubicacion", False, "Almacén / estante.", "A-01"),
            Columna("numero_oc", False, "OC asociada (debe existir).", "OC-00046"),
        ],
    },
    "salidas": {
        "titulo": "Salidas de stock (despacho masivo FEFO)",
        "archivo": "plantilla_salidas",
        "columnas": [
            Columna("codigo_producto", True, "Código (SKU) del producto.", "PRD-001"),
            Columna("cantidad", True, "Unidades que salen (mayor que 0).", "20"),
            Columna("documento_cliente", False, "DNI (8) o RUC (11) de un cliente registrado. Obligatorio en 'Venta / Despacho'.", "20100070970"),
            Columna("guia_remision", False, "Formato SERIE-NÚMERO.", "T001-00001234"),
            Columna("motivo", False, "Vacío = 'Venta / Despacho'. Otros: " + ", ".join(inv.MOTIVOS_SALIDA[1:]) + ".", "Venta / Despacho"),
            Columna("referencia", False, "Pedido, orden u observación.", "Pedido 77"),
        ],
    },
    "oc": {
        "titulo": "Órdenes de compra (con detalle de productos)",
        "archivo": "plantilla_ordenes_compra",
        "columnas": [
            Columna("ref_oc", True, "Referencia para agrupar: todas las filas con la misma referencia forman UNA OC.", "A"),
            Columna("proveedor", True, "Nombre del proveedor.", "Alicorp S.A.A."),
            Columna("codigo_producto", True, "Código (SKU) de un producto ya registrado.", "PRD-001"),
            Columna("cantidad", True, "Unidades pedidas (mayor que 0).", "200"),
            Columna("precio_unitario", False, "Precio por unidad. Vacío = costo actual del producto.", "6.50"),
            Columna("moneda", False, "PEN o USD. Vacío = PEN.", "PEN"),
            Columna("fecha_emision", False, "dd/mm/aaaa. Vacío = hoy.", "15/11/2026"),
            Columna("fecha_entrega", False, "dd/mm/aaaa (entrega estimada).", "30/11/2026"),
            Columna("observaciones", False, "Texto libre.", "Entrega en almacén central"),
        ],
    },
}

_ALIAS = {
    "codigo": "codigo_producto", "sku": "codigo_producto", "producto": "codigo_producto",
    "codigo_de_producto": "codigo_producto", "cod_producto": "codigo_producto", "codigo_sku": "codigo_producto",
    "lote": "numero_lote", "n_lote": "numero_lote", "nro_lote": "numero_lote",
    "costo": "costo_unitario", "costo_unit": "costo_unitario", "margen": "margen_pct", "margen_porcentaje": "margen_pct",
    "vencimiento": "fecha_vencimiento", "fecha_de_vencimiento": "fecha_vencimiento", "vence": "fecha_vencimiento",
    "fecha_de_ingreso": "fecha_ingreso", "ubicación": "ubicacion", "oc": "numero_oc", "n_oc": "numero_oc",
    "ruc": "documento_cliente", "dni": "documento_cliente", "ruc_dni": "documento_cliente", "cliente": "documento_cliente",
    "documento": "documento_cliente", "guia": "guia_remision", "guia_de_remision": "guia_remision",
    "precio": "precio_unitario", "precio_unit": "precio_unitario", "referencia_oc": "ref_oc", "grupo": "ref_oc",
    "entrega_estimada": "fecha_entrega", "fecha_entrega_estimada": "fecha_entrega", "fecha_de_emision": "fecha_emision",
}


# ===========================================================================
# LECTURA DEL ARCHIVO
# ===========================================================================
def _normalizar_encabezado(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"[*()]", "", t)
    t = re.sub(r"[\s\-./]+", "_", t.strip()).strip("_")
    return _ALIAS.get(t, t)


def leer_archivo(nombre: str, contenido: bytes, tipo: str) -> pd.DataFrame:
    """Lee un .xlsx o .csv y devuelve un DataFrame de TEXTO con las columnas canónicas y '_fila' (fila de Excel)."""
    if tipo not in ESPECIFICACIONES:
        raise ValueError("Tipo de carga inválido.")
    if not contenido:
        raise ValueError("El archivo está vacío.")
    if len(contenido) > MAX_BYTES:
        raise ValueError(f"El archivo supera el máximo de {MAX_BYTES // (1024 * 1024)} MB.")

    ext = Path(nombre or "").suffix.lower()
    try:
        if ext in (".xlsx", ".xlsm"):
            df = pd.read_excel(BytesIO(contenido), dtype=str, keep_default_na=False, engine="openpyxl")
        elif ext in (".csv", ".txt"):
            try:
                texto = contenido.decode("utf-8-sig")
            except UnicodeDecodeError:
                texto = contenido.decode("cp1252")
            primera = texto.splitlines()[0] if texto.strip() else ""
            sep = max([";", ",", "\t", "|"], key=primera.count)
            df = pd.read_csv(StringIO(texto), sep=sep, dtype=str, keep_default_na=False)
        else:
            raise ValueError("Formato no soportado. Suba un archivo .xlsx o .csv.")
    except ValueError:
        raise
    except Exception:
        raise ValueError("No se pudo leer el archivo. Verifique que sea un Excel (.xlsx) o CSV válido y no esté "
                         "protegido con contraseña.") from None

    df.columns = [_normalizar_encabezado(c) for c in df.columns]
    df["_fila"] = range(2, len(df) + 2)                      # fila real en Excel (1 = encabezado)
    requeridas = [c.nombre for c in ESPECIFICACIONES[tipo]["columnas"] if c.requerida]
    faltantes = [c for c in requeridas if c not in df.columns]
    if faltantes:
        raise ValueError("Faltan columnas obligatorias: " + ", ".join(faltantes) +
                         ". Descargue la plantilla para ver el formato exacto.")
    conocidas = [c.nombre for c in ESPECIFICACIONES[tipo]["columnas"]]
    for c in conocidas:
        if c not in df.columns:
            df[c] = ""
    df = df[conocidas + ["_fila"]].copy()
    for c in conocidas:
        df[c] = df[c].astype(str).str.strip()
    df = df[df[conocidas].apply(lambda fila: any(v != "" for v in fila), axis=1)].reset_index(drop=True)
    if df.empty:
        raise ValueError("El archivo no tiene filas de datos (solo el encabezado).")
    if len(df) > MAX_FILAS:
        raise ValueError(f"El archivo tiene {len(df):,} filas; el máximo por carga es {MAX_FILAS:,}.")
    return df


# ===========================================================================
# CONVERSORES
# ===========================================================================
def _num(valor: str, campo: str, *, minimo: Optional[float] = None, maximo: Optional[float] = None,
         obligatorio: bool = False, estricto_positivo: bool = False) -> Optional[float]:
    s = (valor or "").replace(" ", "")
    if not s:
        if obligatorio:
            raise ValueError(f"'{campo}' es obligatorio.")
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        x = float(s)
    except ValueError:
        raise ValueError(f"'{campo}' no es un número válido ({valor!r}).") from None
    if not math.isfinite(x):
        raise ValueError(f"'{campo}' no es un número válido ({valor!r}).")
    if estricto_positivo and not x > 0:
        raise ValueError(f"'{campo}' debe ser mayor que cero.")
    if minimo is not None and x < minimo:
        raise ValueError(f"'{campo}' no puede ser menor que {minimo:g}.")
    if maximo is not None and x > maximo:
        raise ValueError(f"'{campo}' no puede ser mayor que {maximo:g}.")
    return round(x, DECIMALES)


def _fecha(valor: str, campo: str) -> Optional[date]:
    s = (valor or "").strip()
    if not s:
        return None
    try:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$", s)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if s.isdigit() and 20000 < int(s) < 80000:                 # número de serie de Excel
            return date(1899, 12, 30) + timedelta(days=int(s))
    except ValueError:
        pass
    raise ValueError(f"'{campo}' no es una fecha válida ({valor!r}). Use dd/mm/aaaa.")


def _texto(valor: str, campo: str, maximo: int, obligatorio: bool = False) -> str:
    t = " ".join((valor or "").split())
    if obligatorio and not t:
        raise ValueError(f"'{campo}' es obligatorio.")
    if len(t) > maximo:
        raise ValueError(f"'{campo}' supera los {maximo} caracteres.")
    return t


# ===========================================================================
# RESULTADO DE LA VALIDACIÓN
# ===========================================================================
@dataclass
class Fila:
    nro: int                       # fila en Excel
    estado: str                    # "ok" | "aviso" | "error"
    mensajes: list[str]
    datos: dict[str, Any]
    resumen: str = ""


@dataclass
class Validacion:
    tipo: str
    filas: list[Fila]
    grupos: list[dict[str, Any]] = field(default_factory=list)   # solo para 'oc'

    @property
    def n_ok(self) -> int:
        return sum(f.estado == "ok" for f in self.filas)

    @property
    def n_aviso(self) -> int:
        return sum(f.estado == "aviso" for f in self.filas)

    @property
    def n_error(self) -> int:
        return sum(f.estado == "error" for f in self.filas)

    @property
    def n_importables(self) -> int:
        if self.tipo == "oc":
            return sum(1 for g in self.grupos if g["valido"])
        return sum(1 for f in self.filas if f.estado != "error")

    def reporte(self) -> pd.DataFrame:
        iconos = {"ok": "✅ OK", "aviso": "🟡 Aviso", "error": "❌ Error"}
        return pd.DataFrame({"Fila": [f.nro for f in self.filas], "Estado": [iconos[f.estado] for f in self.filas],
                             "Registro": [f.resumen for f in self.filas],
                             "Detalle": ["; ".join(f.mensajes) for f in self.filas]})


def _estado(errores: list[str], avisos: list[str]) -> tuple[str, list[str]]:
    if errores:
        return "error", errores + avisos
    return ("aviso" if avisos else "ok"), avisos


def _mapa_productos() -> dict[str, dict[str, Any]]:
    df = db.fetch_df("SELECT id, codigo, nombre, activo, precio_costo, precio_venta FROM productos")
    return {r.codigo.upper(): {"id": int(r.id), "nombre": r.nombre, "activo": bool(r.activo),
                               "costo": float(r.precio_costo), "venta": float(r.precio_venta)}
            for r in df.itertuples()}


# ===========================================================================
# VALIDACIÓN: ENTRADAS
# ===========================================================================
def validar_entradas(df: pd.DataFrame, *, permitir_vencidos: bool = False) -> Validacion:
    productos = _mapa_productos()
    ocs = {r.numero_oc.upper(): int(r.id) for r in db.fetch_df("SELECT id, numero_oc FROM ordenes_compra").itertuples()}
    existentes = {(int(r.producto_id), r.numero_lote) for r in
                  db.fetch_df("SELECT producto_id, numero_lote FROM lotes_inventario").itertuples()}
    hoy = date.today()
    vistos: set[tuple[int, str]] = set()
    filas: list[Fila] = []

    for r in df.to_dict("records"):
        errores: list[str] = []
        avisos: list[str] = []
        d: dict[str, Any] = {}
        codigo = r["codigo_producto"].upper()
        prod = productos.get(codigo)
        if not codigo:
            errores.append("'codigo_producto' es obligatorio.")
        elif prod is None:
            errores.append(f"El producto '{codigo}' no existe.")
        elif not prod["activo"]:
            errores.append(f"El producto '{codigo}' está inactivo.")
        lote = ""
        try:
            lote = _texto(r["numero_lote"], "numero_lote", 40, obligatorio=True).upper()
        except ValueError as e:
            errores.append(str(e))
        if prod and lote:
            clave = (prod["id"], lote)
            if clave in existentes:
                errores.append(f"El lote {lote} ya existe para {codigo}.")
            elif clave in vistos:
                errores.append(f"El lote {lote} está repetido en el archivo para {codigo}.")
            vistos.add(clave)

        def intentar(campo: str, f, *a, **k):
            try:
                return f(r[campo], campo, *a, **k)
            except ValueError as e:
                errores.append(str(e))
                return None

        cantidad = intentar("cantidad", _num, obligatorio=True, estricto_positivo=True)
        costo = intentar("costo_unitario", _num, minimo=0)
        margen = intentar("margen_pct", _num, minimo=0, maximo=1000)
        f_ing = intentar("fecha_ingreso", _fecha)
        f_ven = intentar("fecha_vencimiento", _fecha)
        ubic = intentar("ubicacion", _texto, 60) or ""
        ingreso_explicito = f_ing is not None
        if f_ing is None:
            f_ing = hoy
        elif f_ing > hoy:
            errores.append("'fecha_ingreso' no puede ser futura.")
        if f_ven is not None:
            if f_ven < hoy:
                (avisos if permitir_vencidos else errores).append(
                    "El lote ya está vencido" + ("" if permitir_vencidos else " (marque 'Permitir lotes vencidos' para cargarlo)") + ".")
                if not ingreso_explicito and f_ing > f_ven:
                    f_ing = f_ven          # sin fecha de ingreso informada: se asume anterior al vencimiento
            if f_ven < f_ing:
                errores.append("'fecha_vencimiento' no puede ser anterior a la fecha de ingreso.")
        oc_id = None
        if r["numero_oc"]:
            oc_id = ocs.get(r["numero_oc"].upper())
            if oc_id is None:
                errores.append(f"La OC '{r['numero_oc']}' no existe.")
        if prod and costo is not None and abs(costo - prod["costo"]) > 0.0001:
            avisos.append(f"Costo del producto: {prod['costo']:.2f} → {costo:.2f}")
        if prod and margen is not None:
            base = costo if costo is not None else prod["costo"]
            avisos.append(f"Precio de venta: {prod['venta']:.2f} → {base * (1 + margen / 100):.2f}")

        estado, mensajes = _estado(errores, avisos)
        d.update(producto_id=prod["id"] if prod else None, codigo=codigo, numero_lote=lote, cantidad=cantidad,
                 costo=costo, margen=margen, fecha_ingreso=f_ing, fecha_vencimiento=f_ven, ubicacion=ubic, oc_id=oc_id)
        filas.append(Fila(r["_fila"], estado, mensajes, d, f"{codigo} · lote {lote} · {r['cantidad']}"))
    return Validacion("entradas", filas)


# ===========================================================================
# VALIDACIÓN: SALIDAS
# ===========================================================================
def validar_salidas(df: pd.DataFrame) -> Validacion:
    productos = _mapa_productos()
    stock = {int(r.producto_id): float(r.stock) for r in db.fetch_df(
        """SELECT producto_id, SUM(cantidad_actual) AS stock FROM lotes_inventario
           WHERE activo = 1 AND cantidad_actual > 0 AND (fecha_vencimiento IS NULL OR fecha_vencimiento >= ?)
           GROUP BY producto_id""", (date.today().isoformat(),)).itertuples()}
    clientes = {r.numero_documento: r for r in db.fetch_df(
        "SELECT id, numero_documento, razon_social, activo FROM clientes").itertuples()}
    motivos = {m.lower(): m for m in inv.MOTIVOS_SALIDA}
    usado: dict[int, float] = {}
    filas: list[Fila] = []

    for r in df.to_dict("records"):
        errores: list[str] = []
        avisos: list[str] = []
        codigo = r["codigo_producto"].upper()
        prod = productos.get(codigo)
        if not codigo:
            errores.append("'codigo_producto' es obligatorio.")
        elif prod is None:
            errores.append(f"El producto '{codigo}' no existe.")
        elif not prod["activo"]:
            errores.append(f"El producto '{codigo}' está inactivo.")
        try:
            cantidad = _num(r["cantidad"], "cantidad", obligatorio=True, estricto_positivo=True)
        except ValueError as e:
            cantidad = None
            errores.append(str(e))
        motivo = motivos.get(r["motivo"].lower()) if r["motivo"] else inv.MOTIVOS_SALIDA[0]
        if motivo is None:
            errores.append(f"Motivo inválido '{r['motivo']}'. Use: {', '.join(inv.MOTIVOS_SALIDA)}.")
        cliente_id = None
        if r["documento_cliente"]:
            try:
                doc = identidad.validar_documento(r["documento_cliente"])
                c = clientes.get(doc.numero)
                if c is None:
                    errores.append(f"No existe un cliente con {doc.etiqueta}. Regístrelo en 'Clientes'.")
                elif not c.activo:
                    errores.append(f"El cliente {doc.etiqueta} está inactivo.")
                else:
                    cliente_id = int(c.id)
            except ValueError as e:
                errores.append(f"documento_cliente: {e}")
        if motivo in inv.MOTIVOS_REQUIEREN_CLIENTE and cliente_id is None and not r["documento_cliente"]:
            errores.append(f"El motivo '{motivo}' exige 'documento_cliente'.")
        try:
            guia = identidad.normalizar_guia_remision(r["guia_remision"])
        except ValueError as e:
            guia = ""
            errores.append(f"guia_remision: {e}")
        try:
            referencia = _texto(r["referencia"], "referencia", 120)
        except ValueError as e:
            referencia = ""
            errores.append(str(e))

        if prod and cantidad and not errores:        # control de stock acumulado (varias filas del mismo producto)
            disponible = stock.get(prod["id"], 0.0) - usado.get(prod["id"], 0.0)
            if cantidad > disponible + 1e-9:
                errores.append(f"Stock vigente insuficiente: se piden {cantidad:g} y quedan {max(disponible, 0):g} "
                               "(descontando las filas anteriores del archivo).")
            else:
                usado[prod["id"]] = usado.get(prod["id"], 0.0) + cantidad

        estado, mensajes = _estado(errores, avisos)
        datos = dict(producto_id=prod["id"] if prod else None, codigo=codigo, cantidad=cantidad, motivo=motivo,
                     referencia=referencia, guia=guia, cliente_id=cliente_id)
        filas.append(Fila(r["_fila"], estado, mensajes, datos, f"{codigo} · {r['cantidad']} u."))
    return Validacion("salidas", filas)


# ===========================================================================
# VALIDACIÓN: ÓRDENES DE COMPRA
# ===========================================================================
def validar_oc(df: pd.DataFrame) -> Validacion:
    productos = _mapa_productos()
    hoy = date.today()
    filas: list[Fila] = []
    grupos: dict[str, dict[str, Any]] = {}
    orden: list[str] = []

    for r in df.to_dict("records"):
        errores: list[str] = []
        avisos: list[str] = []
        ref = r["ref_oc"].strip().upper()
        codigo = r["codigo_producto"].upper()
        prod = productos.get(codigo)
        if not ref:
            errores.append("'ref_oc' es obligatorio (agrupa las líneas de una misma OC).")
        if not codigo:
            errores.append("'codigo_producto' es obligatorio.")
        elif prod is None:
            errores.append(f"El producto '{codigo}' no existe.")
        elif not prod["activo"]:
            errores.append(f"El producto '{codigo}' está inactivo.")
        try:
            cantidad = _num(r["cantidad"], "cantidad", obligatorio=True, estricto_positivo=True)
        except ValueError as e:
            cantidad = None
            errores.append(str(e))
        try:
            precio = _num(r["precio_unitario"], "precio_unitario", minimo=0)
        except ValueError as e:
            precio = None
            errores.append(str(e))
        if precio is None and prod:
            precio = prod["costo"]
            avisos.append(f"Sin precio: se usa el costo actual del producto ({precio:.2f}).")

        cab: dict[str, Any] = {}          # solo valores EXPLÍCITOS de esta fila
        try:
            cab["proveedor"] = _texto(r["proveedor"], "proveedor", 150, obligatorio=True)
            if r["moneda"]:
                if r["moneda"].upper() not in ("PEN", "USD"):
                    raise ValueError(f"'moneda' debe ser PEN o USD ({r['moneda']!r}).")
                cab["moneda"] = r["moneda"].upper()
            emision = _fecha(r["fecha_emision"], "fecha_emision")
            entrega = _fecha(r["fecha_entrega"], "fecha_entrega")
            if emision:
                cab["fecha_emision"] = emision
            if entrega:
                cab["fecha_entrega"] = entrega
                if entrega < (emision or hoy):
                    raise ValueError("'fecha_entrega' no puede ser anterior a la fecha de emisión.")
            if r["observaciones"]:
                cab["observaciones"] = _texto(r["observaciones"], "observaciones", 300)
        except ValueError as e:
            errores.append(str(e))

        if ref:
            g = grupos.get(ref)
            if g is None:
                g = grupos[ref] = {"ref": ref, "cabecera": {}, "lineas": [], "filas": [], "productos": set(), "valido": True}
                orden.append(ref)
            for k, v in cab.items():
                if k not in g["cabecera"]:
                    g["cabecera"][k] = v
                elif g["cabecera"][k] != v:
                    errores.append(f"'{k}' distinto dentro de la misma referencia '{ref}'.")
            if prod:
                if prod["id"] in g["productos"]:
                    errores.append(f"El producto '{codigo}' está repetido dentro de la OC '{ref}'.")
                g["productos"].add(prod["id"])
            g["filas"].append(r["_fila"])
            if not errores and prod and cantidad is not None and precio is not None:
                g["lineas"].append({"producto_id": prod["id"], "cantidad": cantidad, "precio": precio})
            if errores:
                g["valido"] = False

        estado, mensajes = _estado(errores, avisos)
        filas.append(Fila(r["_fila"], estado, mensajes, {"ref": ref}, f"OC '{ref}' · {codigo} · {r['cantidad']} u."))

    lista = []
    for ref in orden:
        g = grupos[ref]
        g.pop("productos", None)
        g["cabecera"].setdefault("moneda", "PEN")
        g["cabecera"].setdefault("fecha_emision", hoy)
        lista.append(g)
    # una fila "ok" de una OC con otras filas inválidas no es importable: se marca como aviso informativo
    invalidas = {g["ref"] for g in lista if not g["valido"]}
    for f in filas:
        if f.estado != "error" and f.datos.get("ref") in invalidas:
            f.estado = "aviso"
            f.mensajes.append("Esta OC no se importará porque otra fila de la misma referencia tiene errores.")
    return Validacion("oc", filas, lista)


# ===========================================================================
# EJECUCIÓN
# ===========================================================================
def ejecutar_importacion(usuario: dict, validacion: Validacion, *, solo_validos: bool, nombre_archivo: str,
                         actualizar_precios: bool = False) -> dict[str, Any]:
    """Importa lo validado. Devuelve un resumen. Lanza ValueError con un mensaje claro si no puede."""
    admin_db.exigir_admin(usuario)
    hay_errores = validacion.n_error > 0
    if hay_errores and not solo_validos:
        raise ValueError("El archivo tiene filas con errores. Corríjalas o marque 'Importar solo los registros válidos'.")
    archivo = (nombre_archivo or "archivo")[:60]
    if validacion.tipo == "entradas":
        return _importar_entradas(usuario, validacion, archivo, actualizar_precios)
    if validacion.tipo == "salidas":
        return _importar_salidas(usuario, validacion, archivo)
    if validacion.tipo == "oc":
        return _importar_oc(usuario, validacion, archivo)
    raise ValueError("Tipo de carga inválido.")


def _importar_entradas(usuario: dict, val: Validacion, archivo: str, actualizar_precios: bool) -> dict[str, Any]:
    validas = [f for f in val.filas if f.estado != "error"]
    if not validas:
        raise ValueError("No hay registros válidos para importar.")
    inv._asegurar_tabla_movimientos()
    unidades = 0.0
    precios = 0
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for f in validas:
            d = f.datos
            if conn.execute("SELECT 1 FROM lotes_inventario WHERE producto_id = ? AND numero_lote = ?",
                            (d["producto_id"], d["numero_lote"])).fetchone():
                raise ValueError(f"Fila {f.nro}: el lote {d['numero_lote']} ya existe (alguien lo creó mientras "
                                 "revisaba). No se importó nada; vuelva a validar el archivo.")
            cur = conn.execute(
                """INSERT INTO lotes_inventario (producto_id, numero_lote, fecha_ingreso, fecha_vencimiento,
                       cantidad_inicial, cantidad_actual, orden_compra_id, ubicacion)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (d["producto_id"], d["numero_lote"], d["fecha_ingreso"].isoformat(),
                 d["fecha_vencimiento"].isoformat() if d["fecha_vencimiento"] else None,
                 d["cantidad"], d["cantidad"], d["oc_id"], d["ubicacion"] or None))
            inv._registrar_movimiento(
                conn, lote_id=cur.lastrowid, producto_id=d["producto_id"], numero_lote=d["numero_lote"],
                tipo="ENTRADA", cantidad=d["cantidad"], motivo="Carga masiva de entradas", referencia=archivo,
                usuario_id=usuario["id"])
            unidades += d["cantidad"]
            if actualizar_precios and (d["costo"] is not None or d["margen"] is not None):
                p = conn.execute("SELECT precio_costo, precio_venta FROM productos WHERE id = ?",
                                 (d["producto_id"],)).fetchone()
                costo = d["costo"] if d["costo"] is not None else p["precio_costo"]
                venta = round(costo * (1 + d["margen"] / 100), 2) if d["margen"] is not None else p["precio_venta"]
                conn.execute("UPDATE productos SET precio_costo = ?, precio_venta = ? WHERE id = ?",
                             (costo, venta, d["producto_id"]))
                precios += 1
        admin_db.registrar_auditoria(conn, usuario["id"], "CARGA_MASIVA_ENTRADAS", archivo,
                                     f"{len(validas)} lotes, {unidades:g} unidades, {precios} precios actualizados")
    return {"importados": len(validas), "omitidos": val.n_error, "unidades": unidades, "precios": precios}


def _importar_salidas(usuario: dict, val: Validacion, archivo: str) -> dict[str, Any]:
    validas = [f for f in val.filas if f.estado != "error"]
    if not validas:
        raise ValueError("No hay registros válidos para importar.")
    inv._asegurar_tabla_movimientos()
    unidades = 0.0
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for f in validas:
            d = f.datos
            try:
                cliente = clientes_db.instantanea_cliente(conn, d["cliente_id"])
                inv.salida_fefo_en_transaccion(
                    conn, usuario_id=usuario["id"], producto_id=d["producto_id"], cantidad=d["cantidad"],
                    motivo=d["motivo"], referencia=d["referencia"] or archivo, guia=d["guia"], cliente=cliente)
            except ValueError as e:
                raise ValueError(f"Fila {f.nro}: {e} No se importó nada.") from None
            unidades += d["cantidad"]
        admin_db.registrar_auditoria(conn, usuario["id"], "CARGA_MASIVA_SALIDAS", archivo,
                                     f"{len(validas)} salidas, {unidades:g} unidades")
    return {"importados": len(validas), "omitidos": val.n_error, "unidades": unidades}


def _importar_oc(usuario: dict, val: Validacion, archivo: str) -> dict[str, Any]:
    grupos = [g for g in val.grupos if g["valido"]]
    if not grupos:
        raise ValueError("No hay órdenes de compra válidas para importar.")
    creadas: list[tuple[str, str]] = []
    fallidas: list[tuple[str, str]] = []
    for g in grupos:
        c = g["cabecera"]
        try:
            oc_id = cdb.crear_oc(usuario, proveedor=c["proveedor"], fecha_emision=c["fecha_emision"],
                                 fecha_entrega=c.get("fecha_entrega"), moneda=c["moneda"],
                                 observaciones=c.get("observaciones", ""), lineas=g["lineas"])
            creadas.append((g["ref"], cdb.obtener_oc(oc_id)["numero_oc"]))
        except (ValueError, PermissionError) as e:
            fallidas.append((g["ref"], str(e)))
    with db.db_session() as conn:
        admin_db.registrar_auditoria(conn, usuario["id"], "CARGA_MASIVA_OC", archivo,
                                     f"{len(creadas)} OC creadas, {len(fallidas)} fallidas")
    return {"importados": len(creadas), "creadas": creadas, "fallidas": fallidas,
            "omitidos": sum(1 for g in val.grupos if not g["valido"])}


# ===========================================================================
# PLANTILLAS DESCARGABLES
# ===========================================================================
def plantilla_xlsx(tipo: str) -> bytes:
    """Excel con la hoja 'Plantilla' (solo encabezados, lista para llenar) y la hoja 'Instrucciones'."""
    esp = ESPECIFICACIONES[tipo]
    columnas = [c.nombre for c in esp["columnas"]]
    instrucciones = pd.DataFrame([{"Columna": c.nombre, "Obligatoria": "Sí" if c.requerida else "No",
                                   "Descripción": c.descripcion, "Ejemplo": c.ejemplo} for c in esp["columnas"]])
    return exp.excel_para_descarga({"Plantilla": pd.DataFrame(columns=columnas), "Instrucciones": instrucciones})


def plantilla_csv(tipo: str) -> bytes:
    """CSV (';' + UTF-8 con BOM) con solo los encabezados."""
    return exp.csv_para_excel(pd.DataFrame(columns=[c.nombre for c in ESPECIFICACIONES[tipo]["columnas"]]))
