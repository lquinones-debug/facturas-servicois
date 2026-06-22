# Control de Facturas de Servicios — App web

App multiusuario (Streamlit + Google Sheets) para cargar facturas de servicios,
marcarlas como pagadas y consultar pendientes / pagadas / histórico.
La base de datos es un **Google Sheet compartido**: lo que carga o paga una
persona lo ven todos (no es `localStorage` por navegador).

## Estructura

```
facturas_app/
├── app.py              # la app Streamlit (4 secciones)
├── sheet_db.py         # lee/escribe el Google Sheet (Service Account)
├── factura_logic.py    # lógica de estados de vencimiento (autocontenida)
├── requirements.txt
├── .streamlit/
│   └── secrets.toml.example   # plantilla de credenciales (copiar a secrets.toml)
└── .gitignore
```

Scripts de soporte (en `tools/`, se corren una vez desde la PC):
- `crear_sheet.py` — crea el Google Sheet con sus pestañas y lo comparte.
- `migrar_facturas_a_sheet.py` — vuelca las facturas históricas de `facturas.json`.
- `importar_pdf_a_sheet.py` — agrega al Sheet las facturas nuevas de los PDFs de Irina.

## 1) Crear y poblar la base (una sola vez, desde la PC)

```powershell
python tools\crear_sheet.py            # crea el Sheet y muestra su ID + URL
# pegar el ID en .env como  FACTURAS_SHEETS_ID=...
python tools\migrar_facturas_a_sheet.py
```

`crear_sheet.py` necesita en `.env`: `GOOGLE_SA_PATH` (ya está) y comparte el
Sheet con `lquinones@indian.ar` como editor.

## 2) Probar local

Crear `tools/facturas_app/.streamlit/secrets.toml` (copia del `.example`) **o**
simplemente tener en `.env`: `GOOGLE_SA_PATH` y `FACTURAS_SHEETS_ID`.

```powershell
cd tools\facturas_app
streamlit run app.py
```

Se abre en http://localhost:8501.

## 3) Deploy en la nube (Streamlit Community Cloud — gratis)

1. Crear un repo en GitHub con **solo** el contenido de `facturas_app/`
   (el `.gitignore` ya evita subir `secrets.toml` y los `.json`).
2. Entrar a https://share.streamlit.io → **New app** → elegir el repo,
   rama y `app.py`.
3. En **Settings → Secrets**, pegar el contenido de `secrets.toml`
   (el bloque `[gcp_service_account]` completo con el JSON del Service Account,
   y `sheets_id = "..."`).
4. Deploy. Queda un link público para abrir desde cualquier PC o celular.

> El Google Sheet debe estar compartido como **Editor** con el email del
> Service Account: `tiendabot-sheets@tiendabot-494700.iam.gserviceaccount.com`
> (`crear_sheet.py` ya lo deja listo).

## 4) Adjuntos (comprobantes en Cargar y Pagos)

Los archivos (PDF/imagen de facturas y comprobantes de pago) se guardan en
**Google Drive** y en el Sheet queda el link (columnas `factura_url` y
`comprobante_pago_url`).

Setup (una sola vez):
1. En Drive → **Unidades compartidas** → crear una (ej. "Comprobantes Facturas").
2. Agregar como miembro el email del Service Account
   `tiendabot-sheets@tiendabot-494700.iam.gserviceaccount.com` con rol
   **Administrador de contenido**.
   > La cuenta de servicio **no tiene almacenamiento propio**: por eso el destino
   > DEBE ser una Unidad Compartida (no una carpeta de "Mi unidad").
3. Copiar el **ID** de la Unidad desde la URL y guardarlo en:
   - Streamlit Cloud → Settings → Secrets: `drive_folder_id = "<ID>"`
   - Local `.env`: `FACTURAS_DRIVE_FOLDER_ID=<ID>`
4. Para que los encabezados nuevos existan en el Sheet, correr una vez
   `python tools\migrar_headers_adjuntos.py` (o agregar a mano `factura_url` en
   Q1 y `comprobante_pago_url` en R1 de la pestaña Facturas).

Cada archivo subido queda como **"cualquiera con el link puede ver"**, así el
equipo lo abre sin login de Google. Adjuntar es **opcional**: si no hay Unidad
configurada, la app igual guarda/paga (sólo avisa que no pudo subir el adjunto).

## Notas

- Google Sheets no es transaccional; para pocos usuarios alcanza. Hay un botón
  **🔄 Actualizar datos** para refrescar el cache.
- Carga manual (app) y PDFs de Irina (`importar_pdf_a_sheet.py`) conviven en la
  misma base; el import deduplica por `id` y no pisa lo ya marcado como pagado.
