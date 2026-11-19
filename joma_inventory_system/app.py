"""
app.py - Aplicación principal: JOMA S.A.C. | Sistema de Inventarios y Documentos

Ejecutar con:  streamlit run app.py

Contiene:
  * Configuración de página
  * Pantalla de Login (con bloqueo temporal por intentos fallidos)
  * Sidebar dinámico según el rol (Admin / Lector)
  * Router que conecta los módulos (carpeta /modulos)
  * Pantalla de Inicio con indicadores y alertas
"""

import time
from datetime import datetime

import streamlit as st

import database as db
from modulos import (administracion, admin_db, dashboards, despachos, facturacion, inventario, manual,
                     modulo_clientes, ordenes_compra, ordenes_venta, permisos, reportes)

# ---------------------------------------------------------------------------
# CONFIGURACIÓN (debe ser la primera llamada a Streamlit)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="JOMA S.A.C. | Inventarios",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

MAX_INTENTOS = 5          # Intentos de login permitidos
BLOQUEO_SEGUNDOS = 60     # Tiempo de bloqueo tras superar los intentos


@st.cache_resource(show_spinner="Inicializando base de datos...")
def _inicializar_bd() -> bool:
    """Se ejecuta una sola vez por proceso del servidor."""
    db.init_db()
    return True


# ---------------------------------------------------------------------------
# ESTADO DE SESIÓN
# ---------------------------------------------------------------------------
def _init_session() -> None:
    defaults = {
        "autenticado": False,
        "usuario": None,
        "intentos_fallidos": 0,
        "bloqueado_hasta": 0.0,
    }
    for clave, valor in defaults.items():
        st.session_state.setdefault(clave, valor)


def _cerrar_sesion() -> None:
    st.session_state.autenticado = False
    st.session_state.usuario = None
    st.session_state.pop("pagina_actual", None)


# ---------------------------------------------------------------------------
# PANTALLA DE LOGIN
# ---------------------------------------------------------------------------
def pantalla_login() -> None:
    _, centro, _ = st.columns([1, 1.2, 1])
    with centro:
        st.markdown("<h1 style='text-align:center'>📦 JOMA S.A.C.</h1>", unsafe_allow_html=True)
        st.markdown(
            "<p style='text-align:center; color:gray'>Sistema de Gestión de Inventarios y Documentos</p>",
            unsafe_allow_html=True,
        )

        aviso = st.session_state.pop("aviso_login", None)
        if aviso:
            st.warning(aviso)

        # Bloqueo temporal por intentos fallidos
        restante = int(st.session_state.bloqueado_hasta - time.time())
        if restante > 0:
            st.error(f"Demasiados intentos fallidos. Espere {restante} segundos e intente de nuevo.")
            return

        with st.form("form_login", clear_on_submit=False):
            username = st.text_input("Usuario", placeholder="Ingrese su usuario")
            password = st.text_input("Contraseña", type="password", placeholder="Ingrese su contraseña")
            enviar = st.form_submit_button("Ingresar", use_container_width=True, type="primary")

        if enviar:
            usuario = db.autenticar_usuario(username, password)
            if usuario:
                st.session_state.autenticado = True
                st.session_state.usuario = usuario
                st.session_state.intentos_fallidos = 0
                st.rerun()
            else:
                st.session_state.intentos_fallidos += 1
                if st.session_state.intentos_fallidos >= MAX_INTENTOS:
                    st.session_state.bloqueado_hasta = time.time() + BLOQUEO_SEGUNDOS
                    st.session_state.intentos_fallidos = 0
                    st.error("Cuenta bloqueada temporalmente por seguridad.")
                else:
                    restantes = MAX_INTENTOS - st.session_state.intentos_fallidos
                    st.error(f"Usuario o contraseña incorrectos. Intentos restantes: {restantes}")

        st.caption("Roles disponibles: **Admin** (acceso total) · **Lector** (solo consulta).")


# ---------------------------------------------------------------------------
# PANTALLA DE INICIO (bienvenida + indicadores)
# ---------------------------------------------------------------------------
def pagina_inicio(usuario: dict) -> None:
    hora = datetime.now().hour
    saludo = "Buenos días" if hora < 12 else ("Buenas tardes" if hora < 19 else "Buenas noches")

    st.title(f"{saludo}, {usuario['nombre_completo']} 👋")
    st.caption(
        f"Roles: **{permisos.texto_roles(usuario.get('roles'))}** · Hoy es {datetime.now().strftime('%d/%m/%Y')} · "
        "Bienvenido al sistema de inventarios de JOMA S.A.C."
    )

    # Aviso de seguridad si se mantiene la contraseña de fábrica
    if db.usa_password_por_defecto(usuario["username"]):
        st.warning("⚠️ Está usando la contraseña inicial. Cámbiela en el menú '🔑 Mi cuenta'.")

    r = db.get_resumen_general(dias_alerta=30)

    # --- Fila 1: indicadores de inventario
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Productos activos", f"{r['productos_activos']:,}")
    c2.metric("Lotes con stock", f"{r['lotes_activos']:,}")
    c3.metric("Unidades vigentes", f"{r['unidades_vigentes']:,.0f}")
    c4.metric("Productos con stock bajo", r["productos_stock_bajo"],
              delta="Revisar" if r["productos_stock_bajo"] else None, delta_color="inverse")

    # --- Fila 2: vencimientos y documentos
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Lotes por vencer (30 d)", r["lotes_por_vencer"])
    c6.metric("Lotes vencidos", r["lotes_vencidos"],
              delta="Acción requerida" if r["lotes_vencidos"] else None, delta_color="inverse")
    c7.metric("OC abiertas", r["oc_abiertas"])
    c8.metric("Facturas abiertas", r["facturas_abiertas"])

    # --- Alertas visuales
    st.subheader("🚨 Alertas de vencimiento")
    criticos = db.get_lotes_criticos(dias_alerta=30)
    if criticos.empty:
        st.success("No hay lotes vencidos ni próximos a vencer en los siguientes 30 días.")
    else:
        st.dataframe(
            criticos[["estado", "codigo", "producto", "lote", "fecha_vencimiento",
                      "dias_restantes", "cantidad", "ubicacion"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "estado": "Estado",
                "codigo": "Código",
                "producto": "Producto",
                "lote": "Lote",
                "fecha_vencimiento": "Vence",
                "dias_restantes": st.column_config.NumberColumn("Días restantes", format="%d"),
                "cantidad": st.column_config.NumberColumn("Cantidad", format="%.0f"),
                "ubicacion": "Ubicación",
            },
        )

    st.divider()
    st.info(
        "📌 Utilice el menú lateral para navegar. ¿Dudas? Consulte el '📖 Manual de Usuario'. "
        "Cambie su contraseña en '🔑 Mi cuenta'."
    )


# ---------------------------------------------------------------------------
# REGISTRO DE PÁGINAS Y PERMISOS POR ROL
# Para agregar una página nueva: añadir una entrada aquí. El menú se genera solo.
# ---------------------------------------------------------------------------
PAGINAS = {
    "🏠 Inicio": {
        "render": pagina_inicio, "modulos": ("inicio",), "caption": "Resumen y alertas",
    },
    "👥 Clientes": {
        "render": modulo_clientes.render, "modulos": ("clientes",), "caption": "Paso 0 · Registro y datos fiscales",
    },
    "🛒 Órdenes de Compra": {
        "render": ordenes_compra.render, "modulos": ("oc",), "caption": "Paso 1 · Abastecimiento y proveedores",
    },
    "📑 Órdenes de Venta": {
        "render": ordenes_venta.render, "modulos": ("ov",), "caption": "Paso 2 · Gestión comercial y expediente",
    },
    "🚚 Despachos": {
        "render": despachos.render, "modulos": ("despachos",), "caption": "Paso 3 · Pedidos, picking y salida",
    },
    "🧾 Facturación": {
        "render": facturacion.render, "modulos": ("cxp", "cxc"), "caption": "Paso 4 · Por pagar y por cobrar",
    },
    "📦 Gestión de Inventario": {
        "render": inventario.render, "modulos": ("inventario",), "caption": "Paso 5 · Stock, lotes y FEFO",
    },
    "📊 Dashboards": {
        "render": dashboards.render, "modulos": ("dashboards",), "caption": "Indicadores en tiempo real",
    },
    "📜 Kardex y Reportes": {
        "render": reportes.render, "modulos": ("kardex",), "caption": "Trazabilidad y exportaciones",
    },
    "📖 Manual de Usuario": {
        "render": manual.render, "modulos": ("manual",), "caption": "Ayuda por secciones",
    },
    "⚙️ Administración": {
        "render": administracion.render, "modulos": ("admin",), "caption": "Usuarios, roles y respaldo",
    },
    "🔑 Mi cuenta": {
        "render": administracion.render_mi_cuenta, "modulos": ("cuenta",), "caption": "Cambiar mi contraseña",
    },
}


def _pagina_permitida(cfg: dict, roles: list[str]) -> bool:
    """Una página se muestra si ALGUNO de los roles del usuario da acceso a ALGUNO de sus módulos."""
    return any(permisos.puede_ver(roles, m) for m in cfg["modulos"])


def menu_para_roles(roles) -> list[str]:
    """Menú consolidado: suma los permisos de TODOS los roles del usuario, en el orden del flujo de trabajo."""
    roles = permisos.normalizar_roles(roles)
    return [nombre for nombre, cfg in PAGINAS.items() if _pagina_permitida(cfg, roles)]


def menu_para_rol(rol) -> list[str]:
    """Compatibilidad: acepta un rol (str) o una lista de roles."""
    return menu_para_roles([rol] if isinstance(rol, str) else rol)


# ---------------------------------------------------------------------------
# APLICACIÓN AUTENTICADA (sidebar + router)
# ---------------------------------------------------------------------------
def app_principal() -> None:
    usuario = st.session_state.usuario

    # Sincroniza la sesión con la BD: si un administrador desactivó la cuenta o cambió sus roles,
    # el cambio se aplica de inmediato (sin esperar a que el usuario cierre sesión).
    vigente = admin_db.estado_usuario(usuario["id"])
    if vigente is None or not vigente["activo"]:
        _cerrar_sesion()
        st.session_state["aviso_login"] = "Su cuenta fue desactivada o ya no existe. Contacte al administrador."
        st.rerun()
    if (vigente["roles"] != usuario.get("roles") or vigente["rol"] != usuario.get("rol")
            or vigente["nombre_completo"] != usuario["nombre_completo"]):
        usuario = {**usuario, "rol": vigente["rol"], "roles": vigente["roles"], "nombre_completo": vigente["nombre_completo"]}
        st.session_state.usuario = usuario

    roles = usuario["roles"]
    opciones = menu_para_roles(roles)
    if st.session_state.get("pagina_actual") not in opciones:   # p. ej. si cambiaron sus roles
        st.session_state.pop("pagina_actual", None)

    with st.sidebar:
        st.markdown("## 📦 JOMA S.A.C.")
        st.caption("Distribuidora · ERP")
        st.divider()
        st.markdown(f"**{'🛡️' if permisos.es_admin(roles) else '👤'} {usuario['nombre_completo']}**")
        st.caption(f"Usuario: `{usuario['username']}` · Roles: {permisos.texto_roles(roles)}")
        st.divider()
        seleccion = st.radio("Navegación", opciones, key="pagina_actual", label_visibility="collapsed",
                             captions=[PAGINAS[o]["caption"] for o in opciones])
        st.divider()
        if st.button("🚪 Cerrar sesión", use_container_width=True):
            _cerrar_sesion()
            st.rerun()

    # Verificación de permisos en servidor (defensa extra ante manipulación de estado)
    pagina = PAGINAS.get(seleccion)
    if pagina is None or not _pagina_permitida(pagina, roles):
        st.error("⛔ No tiene permisos para acceder a esta sección.")
        return

    pagina["render"](usuario)


# ---------------------------------------------------------------------------
# PUNTO DE ENTRADA
# ---------------------------------------------------------------------------
def main() -> None:
    _inicializar_bd()
    _init_session()

    if st.session_state.autenticado and st.session_state.usuario:
        app_principal()
    else:
        pantalla_login()


if __name__ == "__main__":
    main()
