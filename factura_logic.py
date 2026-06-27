"""factura_logic.py — Lógica pura del Control de Facturas (autocontenida).

Copia recortada de tools/factura_lib.py SIN rutas locales ni lectura de JSON,
para que la app pueda desplegarse en Streamlit Cloud (donde no existe C:\\Users\\...).

Solo stdlib. Recibe filas (dicts) que vienen del Google Sheet y calcula estado
de vencimiento y patrón de emisión. Mantener en sync con factura_lib.py.
"""

from __future__ import annotations

import re
import statistics
from datetime import date, datetime, timedelta

# Meses en español (índice 0 = Enero), para los desplegables de período.
MESES_ES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

# Umbral de días para considerar una factura "por vencer" (amarillo)
UMBRAL_POR_VENCER = 10
# Estimación de emisión cuando el portal no la informa: emisión = vto - OFFSET
OFFSET_EMISION_EST = 12
# Ventana "recién emitida / disponible para descargar"
VENTANA_ANTES = 3
VENTANA_DESPUES = 5


def parse_fecha(s):
    """'dd/mm/aaaa' -> date, o None."""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def fmt_fecha(d):
    """date -> 'dd/mm/aaaa', o '' si None."""
    return d.strftime("%d/%m/%Y") if d else ""


def add_months(d, n):
    """Suma n meses a una fecha, recortando el día al último día válido del mes destino."""
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    if month == 12:
        last_day = 31
    else:
        last_day = (date(year, month + 1, 1) - date(year, month, 1)).days
    return date(year, month, min(d.day, last_day))


# ----- PERÍODO (mes-año del consumo) -----------------------------------------
# Formato canónico: "MM-YY" (ej. "05-26"). Se eligió año de 2 dígitos porque
# Google Sheets convierte "MM-YYYY" en una fecha (lo guarda como nº de serie);
# "MM-YY" lo respeta como texto. Ver migrar_periodos.py.

def parse_periodo(s):
    """Parsea un período en cualquier formato a (anio, mes), o None.

    Acepta 'MM-YY', 'MM/YY', 'MM-YYYY', 'MM/YYYY' y 'YYYY-MM'. Año de 2 dígitos -> 20xx.
    """
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2})\s*[-/.]\s*(\d{2,4})$", s)  # MM-YY / MM/YYYY ...
    if m:
        mes, y = int(m.group(1)), int(m.group(2))
        if y < 100:
            y += 2000
        if 1 <= mes <= 12:
            return (y, mes)
    m = re.match(r"^(\d{4})\s*[-/.]\s*(\d{1,2})$", s)  # YYYY-MM
    if m:
        y, mes = int(m.group(1)), int(m.group(2))
        if 1 <= mes <= 12:
            return (y, mes)
    return None


def fmt_periodo(anio, mes):
    """(anio, mes) -> 'MM-YY' canónico (ej. (2026, 5) -> '05-26')."""
    return f"{int(mes):02d}-{int(anio) % 100:02d}"


def periodo_orden(anio, mes):
    """(anio, mes) -> 'YYYY-MM' (clave para ordenar cronológicamente)."""
    return f"{int(anio):04d}-{int(mes):02d}"


def periodo_desde_vto(primer_vto):
    """Período = mes ANTERIOR al 1er vencimiento -> (anio, mes)."""
    prev = primer_vto.replace(day=1) - timedelta(days=1)
    return (prev.year, prev.month)


def periodo_canonico(f):
    """Período canónico 'MM-YY' de una factura: usa el campo `periodo` si está;
    si no, lo deriva del 1er vencimiento (mes anterior). '' si no se puede."""
    pr = parse_periodo(f.get("periodo"))
    if not pr:
        vto = parse_fecha(f.get("primer_vto"))
        if vto:
            pr = periodo_desde_vto(vto)
    return fmt_periodo(*pr) if pr else ""


def preparar_factura(f):
    """Agrega claves *_d (fechas parseadas) a un dict de factura del Sheet."""
    f = dict(f)
    f["emision_d"] = parse_fecha(f.get("fecha_emision"))
    f["primer_vto_d"] = parse_fecha(f.get("primer_vto"))
    f["segundo_vto_d"] = parse_fecha(f.get("segundo_vto"))
    f["ref_d"] = f["emision_d"] or f["primer_vto_d"]
    return f


def clave_cuenta(f):
    return (f.get("proveedor", ""), f.get("cuenta", ""), f.get("nro_cliente", ""))


def agrupar_cuentas(facturas):
    """Agrupa facturas (ya pasadas por preparar_factura) por cuenta y proyecta el próximo ciclo."""
    grupos = {}
    for f in facturas:
        grupos.setdefault(clave_cuenta(f), []).append(f)

    cuentas = []
    for (prov, cuenta, nro), items in grupos.items():
        items_ord = sorted(items, key=lambda x: x.get("ref_d") or date.min, reverse=True)
        ultima = items_ord[0]

        gaps = [(x["primer_vto_d"] - x["emision_d"]).days
                for x in items if x.get("emision_d") and x.get("primer_vto_d")]
        gap_tipico = int(statistics.median(gaps)) if gaps else None

        if ultima.get("emision_d"):
            prox_emision = add_months(ultima["emision_d"], 1)
            prox_vto = prox_emision
            if gap_tipico is not None:
                prox_vto = prox_emision + timedelta(days=gap_tipico)
        else:
            prox_emision = None
            prox_vto = add_months(ultima["primer_vto_d"], 1) if ultima.get("primer_vto_d") else None

        cuentas.append({
            "proveedor": prov,
            "cuenta": cuenta,
            "nro_cliente": nro,
            "facturas": items_ord,
            "ultima": ultima,
            "gap_tipico": gap_tipico,
            "prox_emision_est": prox_emision,
            "prox_vto_est": prox_vto,
            "nota": ultima.get("nota", ""),
        })

    cuentas.sort(key=lambda c: c["ultima"].get("primer_vto_d") or date.max)
    return cuentas


def estado_factura(vto, hoy, umbral=UMBRAL_POR_VENCER):
    """Devuelve (codigo, etiqueta) del estado de un vencimiento respecto de hoy.

    codigo ∈ {'vencida', 'por_vencer', 'al_dia', 'sin_dato'}.
    """
    if vto is None:
        return ("sin_dato", "Sin vencimiento informado")
    delta = (vto - hoy).days
    if delta < 0:
        return ("vencida", f"Venció hace {abs(delta)} día(s)")
    if delta == 0:
        return ("por_vencer", "¡Vence HOY!")
    if delta <= umbral:
        return ("por_vencer", f"Vence en {delta} día(s)")
    return ("al_dia", f"Vence el {fmt_fecha(vto)}")


# Emojis por estado, para usar en la UI
EMOJI_ESTADO = {
    "vencida": "🔴",
    "por_vencer": "🟡",
    "al_dia": "🟢",
    "sin_dato": "⚪",
}


# ----- Ayuda memoria de EMISIÓN (qué facturas ya deberían estar emitidas) -----

def emision_de_factura(f):
    """Devuelve (fecha_emision, es_estimada). Si el portal no informa emisión,
    se estima emisión = primer_vto - OFFSET_EMISION_EST."""
    if f.get("emision_d"):
        return f["emision_d"], False
    if f.get("primer_vto_d"):
        return f["primer_vto_d"] - timedelta(days=OFFSET_EMISION_EST), True
    return None, True


def ayuda_memoria_descarga(cuenta, ref):
    """Para una cuenta (de agrupar_cuentas) y una fecha `ref`, dice si a esa fecha
    ya debería haber una factura emitida MÁS NUEVA que la última que tenemos cargada.

    Proyecta la emisión mes a mes desde la última emisión conocida (real o estimada)
    y compara con la ventana [emisión - VENTANA_ANTES, ...].

    Devuelve dict: estado 'disponible' | 'proxima' | 'sin_dato'.
    """
    ultima = cuenta["ultima"]
    em_ult, estimada = emision_de_factura(ultima)
    if not em_ult:
        return {"estado": "sin_dato", "estimada": True, "ult_emision": None}

    expected = em_ult
    # avanzar mientras la ventana del próximo ciclo ya haya empezado a la fecha ref
    while add_months(expected, 1) - timedelta(days=VENTANA_ANTES) <= ref:
        expected = add_months(expected, 1)

    if expected > em_ult:
        return {
            "estado": "disponible",
            "emision_esperada": expected,
            "dias_desde": (ref - expected).days,
            "estimada": estimada,
            "ult_emision": em_ult,
        }
    return {
        "estado": "proxima",
        "emision_esperada": add_months(em_ult, 1),
        "estimada": estimada,
        "ult_emision": em_ult,
    }
