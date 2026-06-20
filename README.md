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

## Notas

- Google Sheets no es transaccional; para pocos usuarios alcanza. Hay un botón
  **🔄 Actualizar datos** para refrescar el cache.
- Carga manual (app) y PDFs de Irina (`importar_pdf_a_sheet.py`) conviven en la
  misma base; el import deduplica por `id` y no pisa lo ya marcado como pagado.
