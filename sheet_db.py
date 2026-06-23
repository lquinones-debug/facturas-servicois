"""sheet_db.py — Capa de datos sobre Google Sheets para el Control de Facturas.

Base de datos COMPARTIDA: todas las personas que abren la app leen/escriben el
mismo Google Sheet (a diferencia del viejo localStorage por navegador).

Credenciales (Service Account), en este orden:
  1. st.secrets["gcp_service_account"]  (deploy en Streamlit Cloud)
  2. variables de entorno / .env:  GOOGLE_SA_PATH  +  FACTURAS_SHEETS_ID

Patrón adaptado de tools/sheets_client.py, pero AUTOCONTENIDO (sin importar el
paquete tools/), para poder desplegarlo solo en Streamlit Cloud.

El Sheet tiene 2 pestañas: "Facturas" y "Proveedores" (ver *_HEADERS).
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    # Drive: subir y compartir SOLO los archivos que crea la app (adjuntos).
    "https://www.googleapis.com/auth/drive.file",
]

HOJA_FACTURAS = "Facturas"
HOJA_PROVEEDORES = "Proveedores"
HOJA_DESCARGAS = "Descargas"

FACTURAS_HEADERS = [
    "id", "proveedor", "cuenta", "nro_cliente", "fecha_emision", "primer_vto",
    "segundo_vto", "monto", "periodo", "comprobante", "estado_pago",
    "fecha_pago", "pagado_por", "origen", "nota", "creado_ts",
    # Adjuntos (links en Google Drive). Q = factura, R = comprobante de pago.
    "factura_url", "comprobante_pago_url",
    # Categoría del gasto (carga manual desde menú). S.
    "rubro",
]
# Última columna de Facturas (16→P; 18→R al sumar adjuntos; 19→S al sumar rubro).
RANGO_FACTURAS = "A:S"
COL_FACTURA_URL = "Q"
COL_COMPROBANTE_PAGO_URL = "R"
COL_RUBRO = "S"
PROVEEDORES_HEADERS = ["proveedor", "cuenta", "nro_cliente", "trae_emision", "nota"]
# Marcadores de "ya descargué esta factura" (ayuda memoria, estado compartido).
DESCARGAS_HEADERS = ["clave", "proveedor", "cuenta", "nro_cliente",
                     "emision_esperada", "marcado_por", "marcado_ts"]

ESTADO_PENDIENTE = "Pendiente"
ESTADO_PAGADA = "Pagada"

# Cache corto en memoria (la app tiene botón "Actualizar" que lo invalida)
CACHE_TTL = 30


# ----- Resolución de credenciales --------------------------------------------

_service = None
_service_lock = threading.Lock()
_sheet_id_cached = None


def _read_dotenv(path: str) -> dict:
    """Parser mínimo de .env (sin dependencias)."""
    out = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _resolver_credenciales():
    """Devuelve (Credentials, spreadsheet_id)."""
    # 1) Streamlit secrets
    try:
        import streamlit as st  # import perezoso: los scripts CLI no dependen de streamlit
        if "gcp_service_account" in st.secrets:
            info = dict(st.secrets["gcp_service_account"])
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
            sid = st.secrets.get("sheets_id") or st.secrets.get("FACTURAS_SHEETS_ID")
            if not sid:
                raise RuntimeError("Falta 'sheets_id' en los secrets de Streamlit.")
            return creds, sid
    except RuntimeError:
        raise
    except Exception:
        pass  # no hay runtime de streamlit o no hay secret: probamos env

    # 2) Entorno / .env
    env = dict(os.environ)
    for candidato in (r"C:\Users\lquinones\.env", str(Path.home() / ".env")):
        for k, v in _read_dotenv(candidato).items():
            env.setdefault(k, v)

    sa_path = env.get("GOOGLE_SA_PATH")
    if not sa_path or not Path(sa_path).exists():
        raise RuntimeError(
            f"GOOGLE_SA_PATH no apunta a un archivo válido: {sa_path!r}. "
            "Configurar en .env o usar st.secrets."
        )
    sid = env.get("FACTURAS_SHEETS_ID")
    if not sid:
        raise RuntimeError(
            "Falta FACTURAS_SHEETS_ID (id del Google Sheet de facturas) en .env."
        )
    creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    return creds, sid


def _get_service():
    global _service, _sheet_id_cached
    if _service is not None:
        return _service
    with _service_lock:
        if _service is not None:
            return _service
        creds, sid = _resolver_credenciales()
        _sheet_id_cached = sid
        _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return _service


def _sid() -> str:
    if _sheet_id_cached is None:
        _get_service()
    return _sheet_id_cached


# ----- Utilidades -------------------------------------------------------------

def _col_letter(col_num: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""
    letters = ""
    while col_num > 0:
        col_num, rem = divmod(col_num - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def generar_id(proveedor, nro_cliente, primer_vto, comprobante="") -> str:
    """ID estable y deduplicable de una factura."""
    base = f"{proveedor}|{nro_cliente}|{primer_vto}|{comprobante}".lower()
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    return f"{(proveedor or 'x')[:6].lower()}-{h}"


def _rows_to_dicts(values, headers):
    out = []
    for idx, raw in enumerate(values[1:], start=2):  # fila 1 = headers; A2 es la primera
        padded = list(raw) + [""] * (len(headers) - len(raw))
        d = dict(zip(headers, padded))
        d["_row"] = idx
        out.append(d)
    return out


# ----- Asegurar estructura ----------------------------------------------------

def asegurar_estructura():
    """Crea pestañas y headers si faltan. Idempotente."""
    svc = _get_service()
    sid = _sid()
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    existentes = {s["properties"]["title"] for s in meta.get("sheets", [])}

    requests = []
    for hoja in (HOJA_FACTURAS, HOJA_PROVEEDORES):
        if hoja not in existentes:
            requests.append({"addSheet": {"properties": {"title": hoja}}})
    if requests:
        svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": requests}).execute()

    for hoja, headers in ((HOJA_FACTURAS, FACTURAS_HEADERS), (HOJA_PROVEEDORES, PROVEEDORES_HEADERS)):
        got = svc.spreadsheets().values().get(spreadsheetId=sid, range=f"{hoja}!1:1").execute()
        if not got.get("values"):
            svc.spreadsheets().values().update(
                spreadsheetId=sid, range=f"{hoja}!A1",
                valueInputOption="RAW", body={"values": [headers]},
            ).execute()


# ----- Facturas ---------------------------------------------------------------

_cache = {"facturas": {"ts": 0.0, "rows": []}, "proveedores": {"ts": 0.0, "rows": []},
          "descargas": {"ts": 0.0, "rows": []}}
_cache_lock = threading.Lock()


def invalidar_cache():
    with _cache_lock:
        for k in _cache:
            _cache[k]["ts"] = 0.0


def listar_facturas(force_refresh: bool = False) -> list[dict]:
    now = time.time()
    with _cache_lock:
        c = _cache["facturas"]
        if not force_refresh and (now - c["ts"] < CACHE_TTL):
            return c["rows"]
        svc = _get_service()
        res = svc.spreadsheets().values().get(
            spreadsheetId=_sid(), range=f"{HOJA_FACTURAS}!{RANGO_FACTURAS}",
        ).execute()
        values = res.get("values", [])
        rows = _rows_to_dicts(values, FACTURAS_HEADERS) if values else []
        # cast de monto
        for r in rows:
            r["monto_num"] = _parse_monto(r.get("monto"))
        c.update({"ts": now, "rows": rows})
        return rows


def _parse_monto(v):
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip().replace(".", "").replace(",", ".") if ("," in str(v)) else str(v).strip()
    try:
        return float(s)
    except ValueError:
        try:
            return float(str(v).replace(",", "."))
        except ValueError:
            return None


def append_factura(factura: dict) -> str:
    """Agrega una factura. Devuelve el id. No deduplica (lo hacen los scripts)."""
    svc = _get_service()
    fid = factura.get("id") or generar_id(
        factura.get("proveedor", ""), factura.get("nro_cliente", ""),
        factura.get("primer_vto", ""), factura.get("comprobante", ""),
    )
    row = [
        fid,
        factura.get("proveedor", ""),
        factura.get("cuenta", ""),
        factura.get("nro_cliente", ""),
        factura.get("fecha_emision", "") or "",
        factura.get("primer_vto", ""),
        factura.get("segundo_vto", "") or "",
        factura.get("monto", "") if factura.get("monto") not in (None, "") else "",
        factura.get("periodo", "") or "",
        factura.get("comprobante", "") or "",
        factura.get("estado_pago", ESTADO_PENDIENTE),
        factura.get("fecha_pago", "") or "",
        factura.get("pagado_por", "") or "",
        factura.get("origen", "manual"),
        factura.get("nota", "") or "",
        factura.get("creado_ts") or datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        factura.get("factura_url", "") or "",
        factura.get("comprobante_pago_url", "") or "",
        factura.get("rubro", "") or "",
    ]
    svc.spreadsheets().values().append(
        spreadsheetId=_sid(), range=f"{HOJA_FACTURAS}!{RANGO_FACTURAS}",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    invalidar_cache()
    return fid


def append_facturas_bulk(facturas: list[dict]) -> int:
    """Agrega muchas facturas en una sola llamada. Devuelve cuántas agregó."""
    if not facturas:
        return 0
    svc = _get_service()
    rows = []
    for f in facturas:
        fid = f.get("id") or generar_id(
            f.get("proveedor", ""), f.get("nro_cliente", ""),
            f.get("primer_vto", ""), f.get("comprobante", ""),
        )
        rows.append([
            fid, f.get("proveedor", ""), f.get("cuenta", ""), f.get("nro_cliente", ""),
            f.get("fecha_emision", "") or "", f.get("primer_vto", ""),
            f.get("segundo_vto", "") or "",
            f.get("monto", "") if f.get("monto") not in (None, "") else "",
            f.get("periodo", "") or "", f.get("comprobante", "") or "",
            f.get("estado_pago", ESTADO_PENDIENTE), f.get("fecha_pago", "") or "",
            f.get("pagado_por", "") or "", f.get("origen", "pdf"),
            f.get("nota", "") or "",
            f.get("creado_ts") or datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            f.get("factura_url", "") or "", f.get("comprobante_pago_url", "") or "",
            f.get("rubro", "") or "",
        ])
    svc.spreadsheets().values().append(
        spreadsheetId=_sid(), range=f"{HOJA_FACTURAS}!{RANGO_FACTURAS}",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    invalidar_cache()
    return len(rows)


def ids_existentes() -> set:
    return {r["id"] for r in listar_facturas(force_refresh=True) if r.get("id")}


def _buscar_fila_por_id(fid: str):
    for r in listar_facturas(force_refresh=True):
        if str(r.get("id")) == str(fid):
            return r
    return None


def marcar_pagada(fid: str, pagado_por: str = "", fecha_pago: str = "",
                  comprobante: str | None = None) -> bool:
    """Marca una factura como pagada (estado_pago, fecha_pago, pagado_por, comprobante)."""
    r = _buscar_fila_por_id(fid)
    if not r:
        return False
    svc = _get_service()
    fila = r["_row"]
    fecha = fecha_pago or datetime.now().strftime("%d/%m/%Y")
    comp = comprobante if comprobante is not None else r.get("comprobante", "")
    # Columnas: J=comprobante(10), K=estado_pago(11), L=fecha_pago(12), M=pagado_por(13)
    svc.spreadsheets().values().update(
        spreadsheetId=_sid(), range=f"{HOJA_FACTURAS}!J{fila}:M{fila}",
        valueInputOption="USER_ENTERED",
        body={"values": [[comp, ESTADO_PAGADA, fecha, pagado_por]]},
    ).execute()
    invalidar_cache()
    return True


def marcar_pendiente(fid: str) -> bool:
    """Revierte a Pendiente (limpia fecha_pago y pagado_por)."""
    r = _buscar_fila_por_id(fid)
    if not r:
        return False
    svc = _get_service()
    fila = r["_row"]
    svc.spreadsheets().values().update(
        spreadsheetId=_sid(), range=f"{HOJA_FACTURAS}!K{fila}:M{fila}",
        valueInputOption="USER_ENTERED",
        body={"values": [[ESTADO_PENDIENTE, "", ""]]},
    ).execute()
    invalidar_cache()
    return True


def set_url_adjunto(fid: str, columna: str, url: str) -> bool:
    """Escribe el link de un adjunto en UNA sola celda de la factura.

    columna = COL_FACTURA_URL ('Q') o COL_COMPROBANTE_PAGO_URL ('R').
    No toca el resto de la fila (los rangos de marcar_pagada/pendiente son J:M / K:M).
    """
    if columna not in (COL_FACTURA_URL, COL_COMPROBANTE_PAGO_URL):
        raise ValueError(f"columna inválida para adjunto: {columna!r}")
    r = _buscar_fila_por_id(fid)
    if not r:
        return False
    svc = _get_service()
    fila = r["_row"]
    svc.spreadsheets().values().update(
        spreadsheetId=_sid(), range=f"{HOJA_FACTURAS}!{columna}{fila}",
        valueInputOption="USER_ENTERED",
        body={"values": [[url]]},
    ).execute()
    invalidar_cache()
    return True


def actualizar_factura(fid: str, cambios: dict) -> bool:
    """Reescribe la fila completa (A:S) de una factura aplicando `cambios`.

    Sólo pisa las columnas que vengan en `cambios`; el resto (id, creado_ts,
    estado_pago, etc.) se preserva de lo que ya había. Edición en el lugar para
    corregir datos / asignar rubro sin borrar y recargar.
    """
    r = _buscar_fila_por_id(fid)
    if not r:
        return False
    svc = _get_service()
    fila = r["_row"]
    # Fila completa en el orden de FACTURAS_HEADERS: cambio si vino, si no lo de antes.
    row = []
    for h in FACTURAS_HEADERS:
        if h in cambios:
            v = cambios[h]
        else:
            v = r.get(h, "")
        row.append("" if v is None else v)
    svc.spreadsheets().values().update(
        spreadsheetId=_sid(), range=f"{HOJA_FACTURAS}!A{fila}:{COL_RUBRO}{fila}",
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()
    invalidar_cache()
    return True


# ----- Borrar filas -----------------------------------------------------------

_sheet_gids: dict = {}


def _sheet_id_por_titulo(titulo: str) -> int:
    """gid numérico de una pestaña (lo pide la API para borrar filas). Cacheado."""
    if titulo in _sheet_gids:
        return _sheet_gids[titulo]
    svc = _get_service()
    meta = svc.spreadsheets().get(spreadsheetId=_sid()).execute()
    for s in meta.get("sheets", []):
        props = s["properties"]
        _sheet_gids[props["title"]] = props["sheetId"]
    if titulo not in _sheet_gids:
        raise RuntimeError(f"No existe la pestaña {titulo!r}")
    return _sheet_gids[titulo]


def _borrar_fila(hoja_titulo: str, row_number: int) -> None:
    """Elimina físicamente la fila row_number (1-indexada, como en A1) de la pestaña."""
    svc = _get_service()
    gid = _sheet_id_por_titulo(hoja_titulo)
    svc.spreadsheets().batchUpdate(
        spreadsheetId=_sid(),
        body={"requests": [{"deleteDimension": {"range": {
            "sheetId": gid, "dimension": "ROWS",
            "startIndex": row_number - 1, "endIndex": row_number,
        }}}]},
    ).execute()
    invalidar_cache()


def borrar_factura(fid: str) -> bool:
    """Elimina una factura por id. Devuelve False si no la encuentra."""
    r = _buscar_fila_por_id(fid)
    if not r:
        return False
    _borrar_fila(HOJA_FACTURAS, r["_row"])
    return True


def borrar_proveedor(row_number: int) -> bool:
    """Elimina la fila de un proveedor (row sale de listar_proveedores -> '_row')."""
    _borrar_fila(HOJA_PROVEEDORES, row_number)
    return True


# ----- Proveedores ------------------------------------------------------------

def listar_proveedores(force_refresh: bool = False) -> list[dict]:
    now = time.time()
    with _cache_lock:
        c = _cache["proveedores"]
        if not force_refresh and (now - c["ts"] < CACHE_TTL):
            return c["rows"]
        svc = _get_service()
        res = svc.spreadsheets().values().get(
            spreadsheetId=_sid(), range=f"{HOJA_PROVEEDORES}!A:E",
        ).execute()
        values = res.get("values", [])
        rows = _rows_to_dicts(values, PROVEEDORES_HEADERS) if values else []
        c.update({"ts": now, "rows": rows})
        return rows


def append_proveedor(proveedor, cuenta, nro_cliente="", trae_emision="No", nota="") -> None:
    svc = _get_service()
    svc.spreadsheets().values().append(
        spreadsheetId=_sid(), range=f"{HOJA_PROVEEDORES}!A:E",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": [[proveedor, cuenta, nro_cliente, trae_emision, nota]]},
    ).execute()
    invalidar_cache()


def append_proveedores_bulk(filas: list[list]) -> int:
    if not filas:
        return 0
    svc = _get_service()
    svc.spreadsheets().values().append(
        spreadsheetId=_sid(), range=f"{HOJA_PROVEEDORES}!A:E",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": filas},
    ).execute()
    invalidar_cache()
    return len(filas)


# ----- Descargas (ayuda memoria: "ya la descargué") ---------------------------

def _asegurar_hoja(titulo: str, headers: list) -> None:
    """Crea la pestaña y sus encabezados si faltan. Idempotente."""
    svc = _get_service()
    sid = _sid()
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    existentes = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if titulo not in existentes:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {"title": titulo}}}]},
        ).execute()
        _sheet_gids.clear()
    got = svc.spreadsheets().values().get(spreadsheetId=sid, range=f"{titulo}!1:1").execute()
    if not got.get("values"):
        svc.spreadsheets().values().update(
            spreadsheetId=sid, range=f"{titulo}!A1",
            valueInputOption="RAW", body={"values": [headers]},
        ).execute()


def listar_descargas(force_refresh: bool = False) -> list[dict]:
    now = time.time()
    with _cache_lock:
        c = _cache["descargas"]
        if not force_refresh and (now - c["ts"] < CACHE_TTL):
            return c["rows"]
        svc = _get_service()
        try:
            res = svc.spreadsheets().values().get(
                spreadsheetId=_sid(), range=f"{HOJA_DESCARGAS}!A:G",
            ).execute()
            values = res.get("values", [])
        except Exception:  # noqa: BLE001 — la pestaña puede no existir todavía
            values = []
        rows = _rows_to_dicts(values, DESCARGAS_HEADERS) if values else []
        c.update({"ts": now, "rows": rows})
        return rows


def claves_descargadas(force_refresh: bool = False) -> set:
    """Conjunto de claves ya marcadas como descargadas."""
    return {r.get("clave") for r in listar_descargas(force_refresh) if r.get("clave")}


def marcar_descarga(clave, proveedor, cuenta, nro_cliente, emision_esperada,
                    marcado_por="") -> None:
    """Marca una cuenta/período como ya descargado (estado compartido)."""
    _asegurar_hoja(HOJA_DESCARGAS, DESCARGAS_HEADERS)
    svc = _get_service()
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    svc.spreadsheets().values().append(
        spreadsheetId=_sid(), range=f"{HOJA_DESCARGAS}!A:G",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": [[clave, proveedor, cuenta, nro_cliente,
                          emision_esperada, marcado_por, ts]]},
    ).execute()
    invalidar_cache()


def desmarcar_descarga(clave: str) -> bool:
    """Borra el/los marcadores de descarga con esa clave (deshacer)."""
    filas = [r["_row"] for r in listar_descargas(force_refresh=True)
             if r.get("clave") == clave]
    if not filas:
        return False
    for row in sorted(filas, reverse=True):  # de abajo hacia arriba
        _borrar_fila(HOJA_DESCARGAS, row)
    invalidar_cache()
    return True
