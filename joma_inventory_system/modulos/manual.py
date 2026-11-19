"""
modulos/manual.py - Manual de Usuario integrado, por secciones (FASE 3)

Cada sección se muestra a los usuarios cuyos roles (uno o varios) le aplican. Incluye búsqueda y descarga del manual completo en Markdown.
Para actualizar el manual basta editar la lista SECCIONES.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import streamlit as st

TODOS = ("Admin", "Comercial", "Compras", "Almacén", "Lector")
SOLO_ADMIN = ("Admin",)
ALMACEN = ("Admin", "Almacén")
COMPRAS = ("Admin", "Compras")
COMERCIAL = ("Admin", "Comercial")


@dataclass(frozen=True)
class Seccion:
    titulo: str
    roles: tuple[str, ...]
    contenido: str


SECCIONES: list[Seccion] = [
    Seccion("🚀 Primeros pasos", TODOS, """
## Primeros pasos

**Ingreso.** Escriba su usuario y contraseña. Tras 5 intentos fallidos la pantalla se bloquea 60 segundos.

**Roles múltiples.** Un usuario puede tener **varios roles a la vez** (por ejemplo Admin + Comercial) y sus permisos son
la **suma** de todos. El menú lateral muestra solo los módulos a los que usted tiene acceso, en el orden del proceso:

| Paso | Módulo | Qué se hace |
|---|---|---|
| 0 | 👥 Clientes | Registro y datos fiscales (DNI/RUC). |
| 1 | 🛒 Órdenes de Compra | Abastecimiento y proveedores. |
| 2 | 📑 Órdenes de Venta | Gestión comercial y expediente digital. |
| 3 | 🚚 Despachos | Pedido, picking y salida registrada contra la OV. |
| 4 | 🧾 Facturación | Cuentas por Pagar y Cuentas por Cobrar. |
| 5 | 📦 Gestión de Inventario | Stock, lotes, FEFO y salidas/entradas. |

**Mi cuenta.** Cambie su contraseña en **🔑 Mi cuenta** (mínimo 8 caracteres, con letras y números).

Si un administrador cambia sus roles o desactiva su cuenta, el cambio se aplica de inmediato.
"""),
    Seccion("📦 Inventario y FEFO", TODOS, """
## Inventario y FEFO

**FEFO** (*First Expired, First Out*): al sacar mercadería se descuenta primero del lote que **vence antes**.
Los lotes vencidos **no** se despachan y los productos sin vencimiento salen al final.

**Semáforos**
- Lotes: 🟢 vigente · 🟡 por vencer (≤ 30 días) · 🟠 crítico (≤ 15 días) · 🔴 vencido · ⚪ no perecible · ⚫ agotado.
- Stock: 🟢 OK · 🟡 cerca del mínimo · 🔴 stock bajo · ⛔ sin stock. El stock "vigente" excluye lotes vencidos.

**Consulta de stock** (todos los roles): *Stock por producto*, *Lotes (orden FEFO)* con la prioridad de despacho,
*Simulador FEFO* (muestra de qué lotes saldría una cantidad **sin mover nada**) e *Historial de movimientos*.
Todas las tablas se pueden filtrar y exportar a CSV.
"""),
    Seccion("➕➖ Entradas, salidas y ajustes", ALMACEN, """
## Entradas, salidas y ajustes (Admin, Almacén)

En **📦 Gestión de Inventario → Gestión / Entradas / Salidas**:

**Entrada de mercadería.** Producto, N° de lote (único por producto), cantidad, fechas, ubicación y, opcionalmente, OC y
factura asociadas. Un lote ya vencido no se puede ingresar.

**Salida (FEFO).** Para mermas, uso interno, muestras o devoluciones. Pulse *Previsualizar*, revise los lotes y **Confirme**.
🚚 **Las salidas por VENTA no se hacen aquí:** se registran en **Despachos**, contra una Orden de Venta.

**Productos.** Cree y edite productos. No se pueden desactivar mientras tengan stock.

**Ajuste / Baja de lote.** Corrija un conteo físico o dé de baja un lote vencido o dañado. Todo queda en el kardex.
"""),
    Seccion("📥 Carga masiva (Excel / CSV)", ALMACEN, """
## Carga masiva (Admin, Almacén, Compras)

Disponible en **Entradas**, **Salidas** y **Órdenes de Compra → Nueva OC**.

1. Pulse **⬇️ Plantilla Excel (.xlsx)** o **Plantilla CSV**. El Excel trae la hoja *Plantilla* (encabezados) y la hoja
   *Instrucciones* (qué significa cada columna).
2. Llene la plantilla y súbala.
3. Revise el **reporte de validación**: cada fila aparece como ✅ OK, 🟡 aviso o ❌ error, con el motivo. Puede descargarlo.
4. Pulse **Importar**. Nada se guarda antes de este paso.

| Carga | Columnas obligatorias | Notas |
|---|---|---|
| Entradas | `codigo_producto`, `numero_lote`, `cantidad` | Opcionales: costo, margen %, vencimiento, ingreso, ubicación, OC. Con la opción marcada, costo y margen actualizan el precio del producto (venta = costo × (1 + margen/100)). |
| Salidas | `codigo_producto`, `cantidad`, `motivo` | Mermas, uso interno, muestras o devoluciones (las ventas van por Despachos). Se controla el stock acumulado. |
| OC | `ref_oc`, `proveedor`, `codigo_producto`, `cantidad` | Las filas con la misma `ref_oc` forman **una** OC; el número se asigna solo. |

**Reglas.** Entradas y salidas se importan en **una sola operación** (todo o nada). Si hay errores puede importar solo
las filas válidas. Números con punto o coma decimal; fechas `dd/mm/aaaa`. Máximo 5 000 filas y 5 MB.

**Empezar desde cero.** Puede iniciar con stock y OC en 0: en **⚙️ Administración → 🧪 Modo pruebas** se reinician los
datos operativos (con respaldo automático).
"""),
    Seccion("🛒 Órdenes de compra", TODOS, """
## Órdenes de compra

**Numeración automática.** Cada OC recibe el siguiente número correlativo (por ejemplo `OC-00046`); no se escribe a mano.

**Lista y detalle.** Filtre por texto y estado (abiertas, atrasadas, cerradas). Al elegir una OC verá su avance de
recepción, líneas, facturas vinculadas y documentos.

**Estados.** *Abierto* o *Cerrado*. 🔴 Atrasada = abierta y con la fecha de entrega vencida.

**👁️ Vista previa (1 clic).** Muestra la OC como documento y permite descargarla en **PDF** o **Excel**.

**Solo Admin:** crear OC, editar cabecera, agregar o quitar líneas, cerrar o reabrir, eliminar (solo si no tiene
mercadería recibida ni facturas) y **📋 Guardar como plantilla**.
"""),
    Seccion("📋 Plantillas de compra", TODOS, """
## Plantillas de compra

Una plantilla guarda **proveedor, moneda y productos con cantidades y precios**.

- **Usarla:** en *Nueva orden de compra* elija la plantilla en **⚡ Plantilla de compra**: el formulario se autocompleta
  y puede ajustar cantidades y precios antes de guardar.
- **Crearla (Admin):** pestaña *Plantillas de compra → Nueva plantilla*, o desde el detalle de una OC con
  **📋 Guardar como plantilla**.
"""),
    Seccion("📥 Recepción de mercadería", TODOS, """
## Recepción de mercadería (Admin, Compras, Almacén)

En **🛒 Órdenes de Compra → 📥 Recepción de mercadería**: elija la OC abierta y la línea, indique cantidad, lote,
vencimiento (o *no perecible*), ubicación y factura. En una sola operación el sistema crea el **lote**, actualiza lo
recibido de la OC y registra la **entrada en el kardex**. No se puede recibir más de lo pendiente. Si la OC queda
completa puede cerrarse automáticamente.
"""),
    Seccion("🧾 Facturación", TODOS, """
## Facturación (dos pestañas)

**💳 Cuentas por Pagar** — facturas de **proveedores** vinculadas a Órdenes de Compra. Estado Abierto / Cerrado
(cierre = pagada). 🔴 Vencida · 🟠 Por vencer (≤ 7 días). Al vincular una OC, proveedor y moneda deben coincidir.
Escriben Admin y Compras.

**💰 Cuentas por Cobrar** — facturas de **venta** vinculadas a una Orden de Venta y a su cliente. El total no puede
superar lo pendiente por facturar de la OV. Al registrar la factura el sistema genera su comprobante y lo **deposita en la
carpeta de la OV**. Cargue la copia **firmada** aquí para completar el expediente. Estado *Por cobrar* / *Cobrada*.
Escriben Admin y Comercial.
"""),
    Seccion("👥 Clientes", TODOS, """
## Clientes

**Registrar (Admin).** Escriba el DNI o RUC: el sistema lo **valida sin internet**.
- DNI: exactamente **8 dígitos**.
- RUC: **11 dígitos**, empezando en **10** (persona natural) o **20** (persona jurídica), con verificación del dígito
  de control.

Luego complete razón social y dirección (obligatoria para RUC). El botón *Consultar datos* autocompletará cuando se
conecte un servicio de consulta; mientras tanto el registro es manual.

**Editar (Admin).** Mismo flujo: escriba el DNI/RUC del cliente, el sistema lo encuentra y carga sus datos para
actualizarlos. El documento no se modifica.

Los cambios **no alteran el kardex histórico** (cada salida conserva los datos del cliente de ese momento). Los
clientes no se eliminan: se desactivan.
"""),
    Seccion("🚚 Despachos, Pre-picking y Packing List", TODOS, """
## Despachos, Pre-picking y Packing List

Toda salida física por venta se registra **contra una Orden de Venta**.

1. **Buscar.** En *➕ Nuevo despacho* escriba el **nombre del cliente** o el **N° de OV**: aparecen las OV con mercadería
   pendiente. Al elegir una, el pedido se **autocompleta** con lo pendiente.
2. **Picking inmediato.** Descargue al instante el **Pre-picking** (PDF/Excel: SKU, descripción, cantidad y ubicación,
   lote por lote con FEFO) y el Packing List. Reduzca cantidades para un despacho parcial y pulse *Crear despacho*.
3. **Confirmar.** Descuenta **todo** el pedido con FEFO en una sola operación, actualiza el kardex (OV, cliente, guía)
   y lo despachado de la OV. Si algo no alcanza, no se descuenta nada. El Packing List se guarda solo en el expediente.
4. **Guía de Remisión.** Cargue la guía en el detalle del despacho: va a la carpeta de la OV.

Escriben Admin, Comercial y Almacén.
"""),
    Seccion("📜 Kardex y reportes", TODOS, """
## Kardex y reportes

El **Kardex** es el registro de auditoría de todos los movimientos y es **de solo lectura**.

Muestra fecha y hora, tipo (🟢 entrada · 🔴 salida · 🟠 ajuste), producto, lote, vencimiento, **OC y factura de origen**,
entradas, salidas, **saldo acumulado** (al elegir un producto), motivo, referencia, **guía de remisión, cliente,
RUC/DNI y dirección**, y el usuario responsable.

Filtre por fechas, producto, tipo, usuario o texto. Exporte a **CSV** (compatible con Excel en español) o a **Excel
(.xlsx)** con una hoja de resumen y otra con los parámetros del reporte.

**Separador decimal.** Si al abrir un CSV los números se ven mal, cambie el selector *Separador decimal* (coma o punto)
para que coincida con su Excel.
"""),
    Seccion("📊 Dashboards", TODOS, """
## Dashboards

Gráficos interactivos con los datos del momento (pase el cursor para ver el detalle):

- **Inventario:** valor del almacén, valor por categoría, top productos, stock frente al mínimo y mercadería por tramo
  de vencimiento, con tablas de alertas exportables.
- **Rotación y movimientos:** entradas y salidas por día, productos que más rotan y los que **no tienen salidas**.
  Elija el período (30 a 365 días). Se calcula con el kardex, así que mejora a medida que se registran movimientos.
- **Compras:** órdenes por estado, avance de recepción y cuentas por pagar por antigüedad.
"""),
    Seccion("⚙️ Administración", SOLO_ADMIN, """
## Administración (Admin)

- **👥 Usuarios:** cree cuentas y **asigne o retire roles** (uno o varios) con un selector múltiple; active o desactive
  cuentas. Siempre debe quedar al menos un administrador activo y nadie puede quitarse su propio rol de Admin.
  La **matriz de permisos** muestra qué puede hacer cada rol.
- **🔐 Contraseñas:** restablecer con una contraseña temporal segura (se muestra **una sola vez**). Se guardan con bcrypt.
- **📋 Auditoría:** registro de acciones administrativas (incluye cambios de roles), exportable.
- **💾 Sistema y respaldo:** copia de la base de datos e integridad. **Guárdela en un lugar seguro.**
- **🧪 Modo pruebas:** pone en cero stock, OC, OV, facturas, despachos y kardex. Pide escribir `REINICIAR`, genera un
  respaldo antes y no toca usuarios ni contraseñas.
"""),
    Seccion("📑 Órdenes de Venta y expediente digital", TODOS, """
## Órdenes de Venta (OV) y expediente digital

**Crear (Admin, Comercial).** Escriba el RUC/DNI del cliente (debe estar registrado), la OC del cliente, condiciones y
productos. El número es automático por año: `OV001-26`, `OV002-26`… Los totales incluyen **IGV (18 %)**.

**PDF corporativo.** Se genera solo, con el logotipo y los datos fiscales de la empresa, y se guarda en la carpeta de la OV.

**Carpeta nodriza.** Cada OV tiene su carpeta `data/documentos_ov/OV001-26/` con: Cotización, Orden de Venta del sistema,
OC del cliente, Guía de Remisión y Factura. Lo que se genere o se cargue desde Órdenes de Venta, Despachos o Cuentas por
Cobrar se guarda **automáticamente** allí. Los documentos retirados van a una papelera (no se borran).

**Cierre documental automático.** Checklist obligatorio: ✅ OC del cliente · ✅ Guía de Remisión · ✅ Factura **firmada**.
La OV permanece **Abierta** mostrando lo que falta y se **cierra sola** al completarse. Si luego se retira un documento
obligatorio, vuelve a Abierta. Una OV no se elimina: se anula (solo si no tiene despachos ni facturas).
"""),
    Seccion("🎭 Roles y permisos", TODOS, """
## Roles y permisos

| Rol | Escribe en | Solo consulta |
|---|---|---|
| **Admin** | Todo | — |
| **Comercial** | Clientes, Órdenes de Venta, Despachos, Cuentas por Cobrar | Compras, inventario, reportes |
| **Compras** | Órdenes de Compra, Recepción, Cuentas por Pagar | Ventas, despachos, inventario |
| **Almacén** | Inventario, Recepción, Despachos | Órdenes y reportes |
| **Lector** | — | Todos los módulos operativos |

Los permisos se **suman**: un usuario Admin + Comercial o Compras + Almacén tiene el acceso de ambos roles.
Si no ve un menú o un botón, sus roles no incluyen ese permiso: pida a un administrador que se lo asigne.
"""),
    Seccion("❓ Preguntas frecuentes", TODOS, """
## Preguntas frecuentes

**No puedo ver un menú o un botón.** Depende de su rol. Los botones de crear, editar y eliminar son solo del Admin.

**El sistema rechaza mi RUC o DNI.** Revise la longitud (8 y 11 dígitos) y que el RUC empiece en 10 o 20. El sistema
también verifica el dígito de control del RUC.

**¿Por qué no puedo despachar un lote?** Los lotes vencidos no participan en FEFO. Dé de baja el lote en
*Ajuste / Baja de lote*.

**El Excel muestra todo en una sola columna.** Use los botones de exportación del sistema (CSV con `;` o `.xlsx`).

**Me equivoqué en una salida.** El kardex no se borra: registre una entrada o un ajuste con el motivo correspondiente.

**Olvidé mi contraseña.** Pida a un administrador que la restablezca y luego cámbiela en *Mi cuenta*.
"""),
]


def _texto_plano(s: Seccion) -> str:
    return (s.titulo + " " + s.contenido).lower()


def render(usuario: dict) -> None:
    """Manual de usuario por secciones; se muestran las de TODOS los roles del usuario."""
    roles = usuario.get("roles") or [usuario.get("rol", "Lector")]
    rol = " + ".join(roles)
    visibles = [s for s in SECCIONES if set(s.roles) & set(roles)]

    st.title("📖 Manual de Usuario")
    st.caption(f"Ayuda para el rol **{rol}**. Elija una sección o busque una palabra.")

    buscar = st.text_input("🔎 Buscar en el manual", key="manual_q", placeholder="Ej. FEFO, plantilla, guía, respaldo")
    if buscar.strip():
        terminos = buscar.lower().split()
        visibles_filtradas = [s for s in visibles if all(t in _texto_plano(s) for t in terminos)]
    else:
        visibles_filtradas = visibles
    if not visibles_filtradas:
        st.info("No se encontraron secciones con esa búsqueda.")
        return

    c_menu, c_texto = st.columns([1, 3])
    titulos = [s.titulo for s in visibles_filtradas]
    if st.session_state.get("manual_sec") not in titulos:
        st.session_state.pop("manual_sec", None)
    elegido = c_menu.radio("Secciones", titulos, key="manual_sec", label_visibility="collapsed")
    seccion = next(s for s in visibles_filtradas if s.titulo == elegido)
    with c_texto:
        st.markdown(seccion.contenido)

    completo = f"# Manual de Usuario - JOMA S.A.C.\n\n_Rol: {rol} · generado el {datetime.now():%d/%m/%Y}_\n\n" + \
        "\n\n---\n\n".join(s.contenido.strip() for s in visibles)
    st.download_button("⬇️ Descargar manual completo (.md)", completo.encode("utf-8"),
                       file_name="manual_joma.md", mime="text/markdown", key="manual_dl")
