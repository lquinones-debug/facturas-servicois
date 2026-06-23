"""drive_db.py — Subida de adjuntos (comprobantes) a Google Drive.

Sube PDF/imágenes a una Unidad Compartida (Shared Drive) y los comparte como
"cualquiera con el link puede ver", devolviendo el link para guardar en el Sheet.

Usa la MISMA cuenta de servicio que sheet_db (credenciales resueltas allí, con el
scope drive.file ya incluido en sheet_db.SCOPES). El destino sale de:
  1. st.secrets["drive_folder_id"]            (deploy en Streamlit Cloud)
  2. .env / entorno: FACTURAS_DRIVE_FOLDER_ID

IMPORTANTE: la cuenta de servicio NO tiene cuota propia en Drive; por eso el
destino DEBE ser una Unidad Compartida donde la SA sea miembro (no una carpeta
normal de "Mi unidad", que rechazaría la subida por falta de cuota).
"""

from __future__ import annotations

import io
import os
import threading
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

import sheet_db

_drive = None
_drive_lock = threading.Lock()


def _resolver_folder_id() -> str:
    """ID de la Unidad Compartida / carpeta destino. Secrets primero, luego .env."""
    # 1) Streamlit secrets
    try:
        import streamlit as st
        fid = st.secrets.get("drive_folder_id") or st.secrets.get("FACTURAS_DRIVE_FOLDER_ID")
        if fid:
            return fid
    except Exception:
        pass
    # 2) Entorno / .env
    env = dict(os.environ)
    for candidato in (r"C:\Users\lquinones\.env", str(Path.home() / ".env")):
        for k, v in sheet_db._read_dotenv(candidato).items():
            env.setdefault(k, v)
    fid = env.get("FACTURAS_DRIVE_FOLDER_ID")
    if not fid:
        raise RuntimeError(
            "Falta el destino de Drive: definí 'drive_folder_id' en los secrets "
            "(Streamlit Cloud) o FACTURAS_DRIVE_FOLDER_ID en el .env. Debe ser el "
            "ID de una Unidad Compartida donde la cuenta de servicio sea miembro."
        )
    return fid


def _get_drive_service():
    global _drive
    if _drive is not None:
        return _drive
    with _drive_lock:
        if _drive is not None:
            return _drive
        creds, _ = sheet_db._resolver_credenciales()
        _drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        return _drive


def _reset_drive_service():
    """Descarta el servicio cacheado (su conexión HTTP quedó muerta)."""
    global _drive
    with _drive_lock:
        _drive = None


def disponible() -> bool:
    """True si hay un destino de Drive configurado (para mensajes en la UI)."""
    try:
        _resolver_folder_id()
        return True
    except Exception:
        return False


def _slug(s: str) -> str:
    keep = [c if (c.isalnum() or c in " -_.") else "_" for c in (s or "").strip()]
    return ("".join(keep).strip().replace(" ", "_")) or "x"


# Errores de red que justifican reintentar reconstruyendo la conexión: la SA
# cachea una conexión HTTP que el servidor cierra tras un rato de inactividad, y
# al reusarla la subida falla con "Broken pipe" / connection reset / etc.
_ERRORES_RED = (
    BrokenPipeError, ConnectionError, ConnectionResetError,
    ConnectionAbortedError, TimeoutError, OSError,
)


def _intentar_subida(file_bytes: bytes, nombre: str, folder_id: str,
                     mimetype: str | None) -> dict:
    """Una pasada de subida + permiso. Reanudable, con reintentos internos."""
    svc = _get_drive_service()
    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes),
        mimetype=mimetype or "application/octet-stream",
        resumable=True,  # sube por bloques y reintenta cada bloque
        chunksize=5 * 1024 * 1024,
    )
    req = svc.files().create(
        body={"name": nombre, "parents": [folder_id]},
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    )
    archivo = None
    while archivo is None:
        _status, archivo = req.next_chunk(num_retries=3)
    fid = archivo["id"]
    # Compartir: cualquiera con el link puede ver.
    svc.permissions().create(
        fileId=fid,
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True,
        fields="id",
    ).execute(num_retries=3)
    return archivo


def subir_comprobante(file_bytes: bytes, filename: str, mimetype: str | None = None,
                      prefijo: str = "") -> str:
    """Sube un archivo a la Unidad Compartida y devuelve su link (webViewLink).

    Queda compartido como "cualquiera con el link puede ver". Si la conexión
    cacheada está muerta (Broken pipe), reconstruye el servicio y reintenta.
    """
    folder_id = _resolver_folder_id()
    nombre = f"{_slug(prefijo)}__{filename}" if prefijo else filename

    ultimo_error: Exception | None = None
    for intento in range(3):
        try:
            archivo = _intentar_subida(file_bytes, nombre, folder_id, mimetype)
            return (archivo.get("webViewLink")
                    or f"https://drive.google.com/file/d/{archivo['id']}/view")
        except _ERRORES_RED as e:  # conexión muerta → tirar el servicio y reintentar
            ultimo_error = e
            _reset_drive_service()
    raise RuntimeError(
        "No pude subir el archivo a Drive tras varios intentos "
        f"(conexión inestable). Probá de nuevo. Detalle: {ultimo_error}"
    )
