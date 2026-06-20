"""app.py — Control de Facturas de Servicios (app web multiusuario).

Streamlit + Google Sheets. Base de datos COMPARTIDA: lo que carga una persona y
lo que marca "Pagos" lo ven todos en tiempo real (no es localStorage por navegador).

Secciones:
  • Cargar factura  — formulario de alta
  • Pagos           — buscar comprobante y tildar pagado
  • Consultar       — pendientes / pagadas / histórico por períodos
  • Proveedores     — alta de proveedores nuevos (para que aparezcan en Cargar)

Correr local:   streamlit run tools/facturas_app/app.py
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import sheet_db as db
import factura_logic as fl

st.set_page_config(page_title="Control de Facturas - Servicios", page_icon="🧾", layout="wide")


# ----- Acceso (clave única compartida) ---------------------------------------

def _check_password() -> bool:
    """Portón de contraseña. La clave sale de st.secrets['app_password']
    (en la nube) o de la variable FACTURAS_APP_PASSWORD; por defecto 00000000."""
    import os
    try:
        clave = st.secrets.get("app_password")
    except Exception:
        clave = None
    if not clave:
        clave = os.getenv("FACTURAS_APP_PASSWORD", "00000000")

    if st.session_state.get("auth_ok"):
        return True

    st.title("🔒 Control de Facturas de Servicios")
    st.caption("Ingresá la clave de acceso del equipo.")
    pwd = st.text_input("Contraseña", type="password", key="login_pwd")
    if st.button("Entrar", type="primary"):
        if pwd == str(clave):
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Contraseña incorrecta.")
    return False


if not _check_password():
    st.stop()


# ----- Helpers ----------------------------------------------------------------

def _refrescar():
    db.invalidar_cache()
    st.cache_data.clear()


@st.cache_data(ttl=db.CACHE_TTL, show_spinner=False)
def cargar_facturas():
    return db.listar_facturas(force_refresh=True)


@st.cache_data(ttl=db.CACHE_TTL, show_spinner=False)
def cargar_proveedores():
    return db.listar_proveedores(force_refresh=True)


def fmt_money(v):
    if v in (None, ""):
        return ""
    try:
        return f"$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return str(v)


def parse_monto_ar(s):
    """Parsea un monto en formato argentino: '.' = miles, ',' = decimales.

    '27.000,00' -> 27000.0 ; '27000' -> 27000.0 ; vacío/inválido -> None.
    """
    s = (s or "").strip().replace(" ", "").replace("$", "")
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def formatear_monto_ar(s):
    """Formatea un monto al estilo argentino con separador de miles.

    '56000' -> '56.000' ; '56000,5' -> '56.000,50'. None si no parsea.
    """
    v = parse_monto_ar(s)
    if v is None:
        return None
    if v == int(v):
        return f"{int(v):,}".replace(",", ".")
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _autoformatear_monto():
    """Callback on_change del campo monto: lo reformatea con miles al salir."""
    f = formatear_monto_ar(st.session_state.get("cf_monto", ""))
    if f is not None:
        st.session_state["cf_monto"] = f


def periodo_auto(primer_vto):
    """Período = mes anterior al 1er vencimiento, formato 'MM-AA' (ej. 05-26)."""
    prev = primer_vto.replace(day=1) - timedelta(days=1)
    return prev.strftime("%m-%y")


def formatear_comprobante(s):
    """Normaliza a 'XXXXX - XXXXXXXX' (5 + 8 dígitos, rellenando ceros a la izquierda).

    '67 - 8878' -> '00067 - 00008878'. Devuelve None si no hay dos grupos de dígitos.
    """
    grupos = [g for g in re.split(r"\D+", (s or "").strip()) if g]
    if len(grupos) != 2:
        return None
    izq, der = grupos
    return f"{int(izq):05d} - {int(der):08d}"


def _autoformatear_comp():
    """Callback on_change del campo comprobante: lo reformatea al salir del campo."""
    f = formatear_comprobante(st.session_state.get("cf_comp", ""))
    if f:
        st.session_state["cf_comp"] = f


def estado_venc(f):
    """(codigo, etiqueta, emoji) de estado de vencimiento de una factura dict del sheet."""
    vto = fl.parse_fecha(f.get("primer_vto"))
    cod, et = fl.estado_factura(vto, date.today())
    return cod, et, fl.EMOJI_ESTADO.get(cod, "⚪")


# ----- Sidebar ----------------------------------------------------------------

st.sidebar.title("🧾 Facturas de Servicios")
seccion = st.sidebar.radio(
    "Sección",
    ["📊 Consultar", "➕ Cargar factura", "💳 Pagos", "🏢 Proveedores"],
)
if st.sidebar.button("🔄 Actualizar datos"):
    _refrescar()
    st.rerun()

# Chequeo de conexión temprano, con mensaje claro
try:
    facturas = cargar_facturas()
    proveedores = cargar_proveedores()
except Exception as e:  # noqa: BLE001
    st.error(
        "No me pude conectar al Google Sheet. Revisá las credenciales "
        "(st.secrets en la nube, o GOOGLE_SA_PATH + FACTURAS_SHEETS_ID en .env).\n\n"
        f"Detalle: {e}"
    )
    st.stop()

st.sidebar.caption(f"{len(facturas)} facturas en la base")


# =============================================================================
# CONSULTAR
# =============================================================================
if seccion == "📊 Consultar":
    st.header("📊 Consultar facturas")
    tab_pend, tab_pag, tab_hist = st.tabs(
        ["⏳ Pendientes de pago", "✅ Pagadas", "📅 Histórico por período"]
    )

    pendientes = [f for f in facturas if f.get("estado_pago", db.ESTADO_PENDIENTE) != db.ESTADO_PAGADA]
    pagadas = [f for f in facturas if f.get("estado_pago") == db.ESTADO_PAGADA]

    # ---- Pendientes
    with tab_pend:
        if not pendientes:
            st.success("No hay facturas pendientes. 🎉")
        else:
            filas = []
            total = 0.0
            for f in sorted(pendientes, key=lambda x: fl.parse_fecha(x.get("primer_vto")) or date.max):
                cod, et, emoji = estado_venc(f)
                if f.get("monto_num"):
                    total += f["monto_num"]
                filas.append({
                    "Estado": f"{emoji} {et}",
                    "Proveedor": f.get("proveedor", ""),
                    "Cuenta": f.get("cuenta", ""),
                    "Nº Cliente": f.get("nro_cliente", ""),
                    "Vencimiento": f.get("primer_vto", ""),
                    "Monto": fmt_money(f.get("monto_num")),
                    "Período": f.get("periodo", ""),
                    "Comprobante": f.get("comprobante", ""),
                })
            vencidas = sum(1 for f in pendientes if estado_venc(f)[0] == "vencida")
            por_vencer = sum(1 for f in pendientes if estado_venc(f)[0] == "por_vencer")
            c1, c2, c3 = st.columns(3)
            c1.metric("Pendientes", len(pendientes))
            c2.metric("🔴 Vencidas", vencidas)
            c3.metric("🟡 Por vencer (≤10d)", por_vencer)
            st.caption(f"Total pendiente: **{fmt_money(total)}**")
            st.dataframe(pd.DataFrame(filas), use_container_width=True, hide_index=True)

    # ---- Pagadas
    with tab_pag:
        if not pagadas:
            st.info("Todavía no hay facturas marcadas como pagadas.")
        else:
            filas = []
            for f in sorted(pagadas, key=lambda x: fl.parse_fecha(x.get("fecha_pago")) or date.min, reverse=True):
                filas.append({
                    "Proveedor": f.get("proveedor", ""),
                    "Cuenta": f.get("cuenta", ""),
                    "Vencimiento": f.get("primer_vto", ""),
                    "Monto": fmt_money(f.get("monto_num")),
                    "Pagada el": f.get("fecha_pago", ""),
                    "Pagó": f.get("pagado_por", ""),
                    "Comprobante": f.get("comprobante", ""),
                    "Período": f.get("periodo", ""),
                })
            st.metric("Pagadas", len(pagadas))
            st.dataframe(pd.DataFrame(filas), use_container_width=True, hide_index=True)

    # ---- Histórico por período
    with tab_hist:
        st.caption("Pagos agrupados por mes de vencimiento.")
        if not pagadas:
            st.info("No hay pagos registrados todavía.")
        else:
            rows = []
            for f in pagadas:
                vto = fl.parse_fecha(f.get("primer_vto"))
                periodo = f.get("periodo") or (vto.strftime("%Y-%m") if vto else "sin fecha")
                rows.append({"Período": periodo, "Monto": f.get("monto_num") or 0.0})
            dfp = pd.DataFrame(rows)
            resumen = dfp.groupby("Período").agg(
                Facturas=("Monto", "count"), Total=("Monto", "sum")
            ).reset_index().sort_values("Período", ascending=False)
            resumen["Total"] = resumen["Total"].apply(fmt_money)
            st.dataframe(resumen, use_container_width=True, hide_index=True)

    # Exportar todo
    st.divider()
    df_all = pd.DataFrame([{k: f.get(k, "") for k in db.FACTURAS_HEADERS} for f in facturas])
    st.download_button(
        "⬇️ Descargar base completa (CSV)",
        df_all.to_csv(index=False).encode("utf-8-sig"),
        file_name="facturas_servicios.csv", mime="text/csv",
    )


# =============================================================================
# CARGAR FACTURA
# =============================================================================
elif seccion == "➕ Cargar factura":
    st.header("➕ Cargar factura")

    # Limpiar campos de la carga anterior ANTES de instanciar los widgets
    if st.session_state.pop("cf_clear", False):
        for k in ("cf_emision", "cf_vto", "cf_vto2", "cf_monto", "cf_comp", "cf_nota"):
            st.session_state.pop(k, None)
    if "cf_ok_msg" in st.session_state:
        st.success(st.session_state.pop("cf_ok_msg"))

    nombres_prov = sorted({p["proveedor"] for p in proveedores if p.get("proveedor")})
    opciones = ["— Elegí proveedor —"] + nombres_prov + ["➕ Nuevo proveedor (cargar en 🏢 Proveedores)"]

    sel_prov = st.selectbox("Proveedor", opciones, key="cf_prov")
    proveedor = ""
    cuenta_val = ""
    nrocli_val = ""
    if sel_prov in nombres_prov:
        proveedor = sel_prov
        cuentas_prov = [p for p in proveedores if p.get("proveedor") == sel_prov]

        def _etiqueta_cuenta(p):
            cta = p.get("cuenta", "")
            nro = p.get("nro_cliente", "")
            if nro and nro not in cta:
                return f"{cta} · {nro}"
            return cta

        etiquetas = [_etiqueta_cuenta(p) for p in cuentas_prov]
        idx = st.selectbox("Cuenta", range(len(cuentas_prov)),
                           format_func=lambda i: etiquetas[i],
                           key="cf_cuenta_idx") if cuentas_prov else None
        if idx is not None:
            cuenta_val = cuentas_prov[idx].get("cuenta", "")
            nrocli_val = cuentas_prov[idx].get("nro_cliente", "")
    elif sel_prov.startswith("➕"):
        st.info("Para un proveedor nuevo, primero cargalo en la sección **🏢 Proveedores** y volvé acá.")

    # --- Campos (sin formulario: reaccionan al instante) ---
    c1, c2 = st.columns(2)
    with c1:
        # Bloqueados: se autocompletan según la cuenta elegida
        st.text_input("Cuenta", value=cuenta_val, disabled=True)
        st.text_input("Nº Cliente", value=nrocli_val, disabled=True)
        emision = st.date_input("Fecha de emisión *", value=None, format="DD/MM/YYYY", key="cf_emision")
        primer_vto = st.date_input("1er vencimiento *", value=None, format="DD/MM/YYYY", key="cf_vto")
    with c2:
        segundo_vto = st.date_input("2do vencimiento (opcional)", value=None,
                                    format="DD/MM/YYYY", key="cf_vto2")
        monto_str = st.text_input("Monto *", placeholder="27.000,00", key="cf_monto",
                                  on_change=_autoformatear_monto,
                                  help="Formato argentino: '.' para miles y ',' para decimales.")
        comprobante = st.text_input("N° de comprobante *", placeholder="67 - 8878", key="cf_comp",
                                    on_change=_autoformatear_comp,
                                    help="Se guarda como 00067 - 00008878 (5 + 8 dígitos).")
        # Período: automático y bloqueado
        periodo_val = periodo_auto(primer_vto) if primer_vto else ""
        st.text_input("Período (automático)", value=periodo_val, disabled=True,
                      help="Mes anterior al 1er vencimiento. Se completa solo.")
    nota = st.text_input("Nota", key="cf_nota")

    if st.button("💾 Guardar factura", type="primary"):
        monto_val = parse_monto_ar(monto_str)
        comp_fmt = formatear_comprobante(comprobante) if comprobante.strip() else None
        faltan = []
        if not proveedor:
            faltan.append("Proveedor")
        if emision is None:
            faltan.append("Fecha de emisión")
        if primer_vto is None:
            faltan.append("1er vencimiento")
        if monto_val is None or monto_val <= 0:
            faltan.append("Monto")
        if not comprobante.strip():
            faltan.append("N° de comprobante")

        if faltan:
            st.error("Faltan campos obligatorios: " + ", ".join(faltan))
        elif comp_fmt is None:
            st.error("El N° de comprobante debe tener dos números, ej: **67 - 8878** "
                     "(se guarda como 00057 - 00089898).")
        else:
            periodo = periodo_auto(primer_vto)
            fid = db.append_factura({
                "proveedor": proveedor,
                "cuenta": cuenta_val,
                "nro_cliente": nrocli_val,
                "fecha_emision": fl.fmt_fecha(emision),
                "primer_vto": fl.fmt_fecha(primer_vto),
                "segundo_vto": fl.fmt_fecha(segundo_vto) if segundo_vto else "",
                "monto": monto_val,
                "periodo": periodo,
                "comprobante": comp_fmt,
                "nota": nota,
                "origen": "manual",
                "estado_pago": db.ESTADO_PENDIENTE,
            })
            _refrescar()
            # Bandera para limpiar los campos en el próximo run (mantiene proveedor/cuenta)
            st.session_state["cf_clear"] = True
            st.session_state["cf_ok_msg"] = (
                f"Factura guardada ✅ — comp. **{comp_fmt}**, período **{periodo}**, "
                f"monto **{fmt_money(monto_val)}** (id {fid})"
            )
            st.rerun()


# =============================================================================
# PAGOS
# =============================================================================
elif seccion == "💳 Pagos":
    st.header("💳 Pagos — buscar y marcar pagado")

    c1, c2 = st.columns([3, 1])
    busqueda = c1.text_input("Buscar (proveedor, cuenta, nº cliente, comprobante o período)")
    solo_pend = c2.checkbox("Solo pendientes", value=True)
    pagado_por = st.text_input("Tu nombre (queda registrado en 'Pagó')", key="pagado_por")

    def coincide(f):
        if solo_pend and f.get("estado_pago") == db.ESTADO_PAGADA:
            return False
        if not busqueda:
            return True
        q = busqueda.lower()
        campos = [f.get(k, "") for k in
                  ("proveedor", "cuenta", "nro_cliente", "comprobante", "periodo")]
        return any(q in str(v).lower() for v in campos)

    resultados = [f for f in facturas if coincide(f)]
    resultados.sort(key=lambda x: fl.parse_fecha(x.get("primer_vto")) or date.max)

    st.caption(f"{len(resultados)} factura(s)")
    if not resultados:
        st.info("Sin resultados. Ajustá la búsqueda.")

    for f in resultados[:200]:
        cod, et, emoji = estado_venc(f)
        pagada = f.get("estado_pago") == db.ESTADO_PAGADA
        with st.container(border=True):
            cols = st.columns([4, 2, 2, 2])
            cols[0].markdown(
                f"**{f.get('proveedor','')}** · {f.get('cuenta','')}  \n"
                f"Cliente {f.get('nro_cliente','—')} · Comp. {f.get('comprobante','—')}"
            )
            cols[1].markdown(f"Vto: **{f.get('primer_vto','')}**  \n{emoji} {et}")
            cols[2].markdown(f"Monto:  \n**{fmt_money(f.get('monto_num'))}**")
            with cols[3]:
                if pagada:
                    st.success(f"✅ Pagada {f.get('fecha_pago','')}")
                    if st.button("↩️ Revertir", key=f"rev_{f['id']}"):
                        db.marcar_pendiente(f["id"])
                        _refrescar()
                        st.rerun()
                else:
                    if st.button("✔️ Marcar pagada", key=f"pay_{f['id']}", type="primary"):
                        db.marcar_pagada(f["id"], pagado_por=pagado_por.strip())
                        _refrescar()
                        st.toast(f"Pagada: {f.get('proveedor','')} {f.get('cuenta','')}")
                        st.rerun()


# =============================================================================
# PROVEEDORES
# =============================================================================
elif seccion == "🏢 Proveedores":
    st.header("🏢 Proveedores")
    st.caption("Acá agregás proveedores/cuentas para que aparezcan en el formulario de carga.")

    if proveedores:
        st.dataframe(
            pd.DataFrame([{k: p.get(k, "") for k in db.PROVEEDORES_HEADERS} for p in proveedores]),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("Todavía no hay proveedores cargados.")

    st.subheader("➕ Agregar proveedor / cuenta")
    with st.form("alta_prov", clear_on_submit=True):
        c1, c2 = st.columns(2)
        proveedor = c1.text_input("Proveedor *")
        cuenta = c2.text_input("Cuenta *")
        nro_cliente = c1.text_input("Nº Cliente")
        trae_emision = c2.selectbox("¿El portal informa fecha de emisión?", ["No", "Sí"])
        nota = st.text_input("Nota")
        if st.form_submit_button("💾 Guardar proveedor", type="primary"):
            if not proveedor or not cuenta:
                st.error("Proveedor y Cuenta son obligatorios.")
            else:
                db.append_proveedor(proveedor.strip(), cuenta.strip(),
                                    nro_cliente.strip(), trae_emision, nota.strip())
                _refrescar()
                st.success(f"Proveedor agregado ✅ ({proveedor} · {cuenta})")
                st.rerun()
