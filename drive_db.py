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


def subir_comprobante(file_bytes: bytes, filename: str, mimetype: str | None = None,
                      prefijo: str = "") -> str:
    """Sube un archivo a la Unidad Compartida y devuelve su link (webViewLink).

    Queda compartido como "cualquiera con el link puede ver".
    """
    svc = _get_drive_service()
    folder_id = _resolver_folder_id()
    nombre = f"{_slug(prefijo)}__{filename}" if prefijo else filename
    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes),
        mimetype=mimetype or "application/octet-stream",
        resumable=False,
    )
    archivo = svc.files().create(
        body={"name": nombre, "parents": [folder_id]},
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    fid = archivo["id"]
    # Compartir: cualquiera con el link puede ver.
    svc.permissions().create(
        fileId=fid,
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True,
    ).execute()
    return archivo.get("webViewLink") or f"https://drive.google.com/file/d/{fid}/view"
