"""
modulos/permisos.py - Roles múltiples y permisos por módulo (FASE 4)

Un usuario puede tener VARIOS roles a la vez (tabla intermedia `usuario_roles`). Sus permisos son la
SUMA de los permisos de todos sus roles: para cada módulo vale el nivel más alto que concede alguno
de ellos ("w" escritura > "r" lectura > sin acceso).

    Admin      -> todo (usuarios, respaldos, modo pruebas y todos los módulos).
    Comercial  -> Clientes, Órdenes de Venta, Despachos y Cuentas por Cobrar.
    Compras    -> Órdenes de Compra, Recepción de mercadería y Cuentas por Pagar.
    Almacén    -> Gestión de Inventario, Recepción de mercadería y Despachos.
    Lector     -> consulta de los módulos operativos (sin escritura).

Seguridad en dos capas:
  * Interfaz: el menú y los formularios se arman con los roles de la sesión.
  * Servidor: cada función de escritura llama a exigir_escritura(usuario, módulo), que vuelve a leer
    los roles DESDE LA BASE DE DATOS (un usuario no puede escalar privilegios manipulando la sesión) y
    solo cuenta usuarios activos.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

import pandas as pd

import database as db

LECTURA, ESCRITURA = "r", "w"

ROLES: dict[str, str] = {
    "Admin": "Acceso total: usuarios, respaldos, modo pruebas y todos los módulos.",
    "Comercial": "Clientes, Órdenes de Venta, Despachos y Cuentas por Cobrar.",
    "Compras": "Órdenes de Compra, recepción de mercadería y Cuentas por Pagar.",
    "Almacén": "Gestión de Inventario, recepción de mercadería y Despachos.",
    "Lector": "Solo consulta de los módulos operativos.",
}

MODULOS: dict[str, str] = {
    "inicio": "Inicio", "clientes": "Clientes", "oc": "Órdenes de Compra", "recepcion": "Recepción de mercadería",
    "ov": "Órdenes de Venta", "despachos": "Despachos", "cxp": "Cuentas por Pagar", "cxc": "Cuentas por Cobrar",
    "inventario": "Gestión de Inventario", "dashboards": "Dashboards", "kardex": "Kardex y Reportes",
    "manual": "Manual de Usuario", "admin": "Administración", "cuenta": "Mi cuenta",
}

_R, _W = LECTURA, ESCRITURA

PERMISOS: dict[str, dict[str, str]] = {
    "Admin": {m: _W for m in MODULOS},
    "Comercial": {"inicio": _R, "clientes": _W, "oc": _R, "ov": _W, "despachos": _W, "cxp": _R, "cxc": _W,
                  "inventario": _R, "dashboards": _R, "kardex": _R, "manual": _R, "cuenta": _W},
    "Compras": {"inicio": _R, "clientes": _R, "oc": _W, "recepcion": _W, "ov": _R, "despachos": _R, "cxp": _W,
                "cxc": _R, "inventario": _R, "dashboards": _R, "kardex": _R, "manual": _R, "cuenta": _W},
    "Almacén": {"inicio": _R, "clientes": _R, "oc": _R, "recepcion": _W, "ov": _R, "despachos": _W,
                "inventario": _W, "dashboards": _R, "kardex": _R, "manual": _R, "cuenta": _W},
    "Lector": {"inicio": _R, "clientes": _R, "oc": _R, "ov": _R, "despachos": _R, "cxp": _R, "cxc": _R,
               "inventario": _R, "dashboards": _R, "kardex": _R, "manual": _R, "cuenta": _W},
}


# ---------------------------------------------------------------------------
# CÁLCULO DE PERMISOS (puro, sin base de datos)
# ---------------------------------------------------------------------------
def normalizar_roles(roles: Optional[Iterable[str]]) -> list[str]:
    """Roles válidos, sin repetir y en el orden del catálogo."""
    pedidos = {str(r).strip() for r in (roles or [])}
    return [r for r in ROLES if r in pedidos]


def nivel(roles: Optional[Iterable[str]], modulo: str) -> Optional[str]:
    """Mejor nivel que concede el conjunto de roles para un módulo: 'w', 'r' o None."""
    niveles = {PERMISOS[r].get(modulo) for r in normalizar_roles(roles)}
    if _W in niveles:
        return _W
    if _R in niveles:
        return _R
    return None


def puede_ver(roles: Optional[Iterable[str]], modulo: str) -> bool:
    return nivel(roles, modulo) is not None


def puede_escribir(roles: Optional[Iterable[str]], modulo: str) -> bool:
    return nivel(roles, modulo) == _W


def es_admin(roles: Optional[Iterable[str]]) -> bool:
    return "Admin" in normalizar_roles(roles)


def texto_roles(roles: Optional[Iterable[str]]) -> str:
    return " + ".join(normalizar_roles(roles)) or "Sin rol"


def matriz_permisos() -> pd.DataFrame:
    """Tabla módulo x rol (✏️ escritura · 👁️ lectura · — sin acceso) para mostrar en Administración."""
    iconos = {_W: "✏️ Escribe", _R: "👁️ Lee", None: "—"}
    filas = [{"Módulo": etiqueta, **{rol: iconos[PERMISOS[rol].get(m)] for rol in ROLES}}
             for m, etiqueta in MODULOS.items()]
    return pd.DataFrame(filas)


# ---------------------------------------------------------------------------
# ROLES DESDE LA BASE DE DATOS (fuente de verdad en el servidor)
# ---------------------------------------------------------------------------
def roles_de(user_id: Any) -> list[str]:
    """Roles vigentes de un usuario ACTIVO (lista vacía si no existe o está inactivo)."""
    if user_id is None:
        return []
    fila_activa = db.fetch_one("SELECT 1 FROM usuarios WHERE id = ? AND activo = 1", (user_id,))
    if fila_activa is None:
        return []
    return normalizar_roles(db.roles_de_usuario(int(user_id)))


def roles_de_usuario_dict(usuario: dict) -> list[str]:
    """Roles de un dict de sesión; si no trae 'roles' (sesiones antiguas) se leen de la BD."""
    return normalizar_roles(usuario.get("roles")) or roles_de(usuario.get("id"))


def exigir_escritura(usuario: dict, modulo: str) -> None:
    """Lanza PermissionError si el usuario (según la BD) no puede MODIFICAR el módulo."""
    roles = roles_de(usuario.get("id"))
    if not puede_escribir(roles, modulo):
        etiqueta = MODULOS.get(modulo, modulo)
        raise PermissionError(f"Sus roles ({texto_roles(roles)}) no permiten modificar «{etiqueta}».")


def exigir_escritura_alguno(usuario: dict, modulos: Iterable[str]) -> None:
    """Basta con poder escribir en UNO de los módulos indicados."""
    roles = roles_de(usuario.get("id"))
    modulos = list(modulos)
    if not any(puede_escribir(roles, m) for m in modulos):
        etiquetas = " / ".join(MODULOS.get(m, m) for m in modulos)
        raise PermissionError(f"Sus roles ({texto_roles(roles)}) no permiten modificar «{etiquetas}».")


def exigir_admin(usuario: dict) -> None:
    roles = roles_de(usuario.get("id"))
    if not es_admin(roles):
        raise PermissionError("Acción permitida solo para usuarios con rol Admin.")
