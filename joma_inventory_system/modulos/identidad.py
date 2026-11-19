"""
modulos/identidad.py - Documentos de identidad peruanos (DNI / RUC) y guía de remisión (FASE 2)

1. VALIDACIÓN OFFLINE ESTRICTA (no requiere internet):
     * DNI : exactamente 8 dígitos.
     * RUC : exactamente 11 dígitos, iniciando en 10 (persona natural) o 20 (persona jurídica),
             con verificación del dígito de control (módulo 11 de SUNAT).
     * Guía de remisión: SERIE-NÚMERO, serie de 4 caracteres alfanuméricos y número de 1 a 8 dígitos
             (ej. T001-00001234, EG07-125).

2. AUTOCOMPLETADO MODULAR:
     La consulta de razón social / dirección se delega en un "proveedor" intercambiable.
     Por defecto se usa ProveedorManual (no consulta nada: el usuario registra los datos a mano).
     Para integrar una API en el futuro basta implementar UNA clase y registrarla; ni la interfaz
     ni la base de datos cambian:

         class MiProveedor:
             nombre = "MI_API"
             def consultar(self, tipo: str, numero: str) -> Optional[DatosContribuyente]:
                 # 1) llamar al servicio con la URL y el token de su proveedor
                 # 2) devolver DatosContribuyente(numero, tipo, nombre, direccion) o None si no existe
                 ...

         configurar_proveedor(MiProveedor())      # p. ej. al iniciar la app

     Si el servicio falla (sin internet, token vencido, etc.) la interfaz sigue funcionando con
     registro manual: nunca se bloquea al usuario.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol

# Si algún RUC real fuera rechazado por el dígito de control, ponga False para validar solo el formato.
VALIDAR_DIGITO_CONTROL_RUC = True
_PESOS_RUC = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
_GUIA_RE = re.compile(r"^[A-Z0-9]{4}-\d{1,8}$")


# ===========================================================================
# VALIDACIÓN OFFLINE
# ===========================================================================
@dataclass(frozen=True)
class DocumentoValidado:
    tipo: str          # "DNI" | "RUC"
    numero: str        # solo dígitos
    descripcion: str   # texto legible para la interfaz

    @property
    def etiqueta(self) -> str:
        """'RUC 20100070970' (se usa como instantánea en el kardex)."""
        return f"{self.tipo} {self.numero}"


def limpiar_numero(texto: object) -> str:
    """Quita espacios, guiones y puntos que suelen copiarse junto con el número."""
    return re.sub(r"[\s.\-]", "", str(texto or ""))


def _digito_control_ruc(primeros_10: str) -> int:
    suma = sum(int(d) * p for d, p in zip(primeros_10, _PESOS_RUC))
    resto = 11 - (suma % 11)
    return resto - 10 if resto >= 10 else resto


def validar_documento(texto: object) -> DocumentoValidado:
    """Valida un DNI o RUC. Lanza ValueError con un mensaje claro si no cumple el formato."""
    numero = limpiar_numero(texto)
    if not numero:
        raise ValueError("Ingrese el número de DNI (8 dígitos) o RUC (11 dígitos).")
    if not numero.isdigit() or not numero.isascii():
        raise ValueError("El documento solo puede contener números.")

    if len(numero) == 8:
        if set(numero) == {"0"}:
            raise ValueError("DNI inválido: no puede ser 00000000.")
        return DocumentoValidado("DNI", numero, "DNI")

    if len(numero) == 11:
        if numero[:2] not in ("10", "20"):
            raise ValueError("RUC inválido: debe iniciar en 10 (persona natural) o 20 (persona jurídica).")
        if VALIDAR_DIGITO_CONTROL_RUC and _digito_control_ruc(numero[:10]) != int(numero[10]):
            raise ValueError("RUC inválido: el dígito verificador no coincide. Revise el número.")
        tipo_persona = "persona natural" if numero.startswith("10") else "persona jurídica"
        return DocumentoValidado("RUC", numero, f"RUC de {tipo_persona}")

    raise ValueError(f"Longitud inválida ({len(numero)} dígitos): el DNI tiene 8 y el RUC tiene 11.")


def evaluar_documento(texto: object) -> tuple[Optional[DocumentoValidado], str]:
    """Versión que no lanza excepciones, pensada para mostrar el estado en pantalla: (documento, mensaje)."""
    try:
        doc = validar_documento(texto)
        return doc, f"{doc.descripcion} válido."
    except ValueError as e:
        return None, str(e)


def normalizar_guia_remision(texto: object) -> str:
    """Devuelve la guía en mayúsculas ('T001-00001234') o '' si está vacía. ValueError si el formato es inválido."""
    guia = re.sub(r"\s+", "", str(texto or "")).upper()
    if not guia:
        return ""
    if not _GUIA_RE.match(guia):
        raise ValueError("Guía de remisión inválida. Use el formato SERIE-NÚMERO, ej. T001-00001234 "
                         "(serie de 4 caracteres y número de 1 a 8 dígitos).")
    return guia


# ===========================================================================
# CONSULTA DE DATOS (PUNTO DE INTEGRACIÓN)
# ===========================================================================
@dataclass(frozen=True)
class DatosContribuyente:
    numero: str
    tipo: str
    nombre: str
    direccion: str = ""
    estado: str = ""
    fuente: str = "API"


class ProveedorConsulta(Protocol):
    """Contrato que debe cumplir cualquier servicio de consulta de RUC/DNI."""
    nombre: str

    def consultar(self, tipo: str, numero: str) -> Optional[DatosContribuyente]: ...


class ProveedorManual:
    """Proveedor por defecto: no consulta nada; el usuario registra los datos manualmente."""
    nombre = "MANUAL"

    def consultar(self, tipo: str, numero: str) -> Optional[DatosContribuyente]:
        return None


_proveedor_activo: ProveedorConsulta = ProveedorManual()


def configurar_proveedor(proveedor: ProveedorConsulta) -> None:
    """Registra el proveedor de consulta que usará toda la aplicación."""
    global _proveedor_activo
    _proveedor_activo = proveedor


def proveedor_activo() -> ProveedorConsulta:
    return _proveedor_activo


def autocompletado_disponible() -> bool:
    return not isinstance(_proveedor_activo, ProveedorManual)


@dataclass(frozen=True)
class ResultadoConsulta:
    documento: DocumentoValidado
    datos: Optional[DatosContribuyente]
    mensaje: str


def consultar_documento(texto: object) -> ResultadoConsulta:
    """
    Valida el documento (offline, estricto) y luego intenta autocompletar con el proveedor activo.
    Lanza ValueError solo si el documento es inválido; cualquier fallo del servicio externo se
    convierte en un mensaje y el registro continúa de forma manual.
    """
    doc = validar_documento(texto)
    try:
        datos = _proveedor_activo.consultar(doc.tipo, doc.numero)
    except Exception as e:   # un servicio externo puede fallar de muchas formas; nunca bloquea el registro
        return ResultadoConsulta(doc, None, f"No se pudo consultar el servicio ({type(e).__name__}). "
                                            "Complete los datos manualmente.")
    if datos is None:
        if autocompletado_disponible():
            return ResultadoConsulta(doc, None, "El servicio no devolvió datos para este documento. "
                                                "Complete los datos manualmente.")
        return ResultadoConsulta(doc, None, "Registro manual: no hay un servicio de consulta configurado. "
                                            "Complete la razón social y la dirección.")
    return ResultadoConsulta(doc, datos, f"Datos obtenidos de {_proveedor_activo.nombre}. Verifíquelos antes de guardar.")
