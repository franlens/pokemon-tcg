# Pokémon TCG — precios Cardmarket

Histórico público de precios en euros de las cartas de la expansión más reciente de Pokémon TCG.

## Datos

Las capturas actuales se guardan en `data/snapshot/` con este formato:

```text
<expansion>-YYYY-MM-DD.csv
```

Ejemplo: `data/snapshot/pitch-black-2026-08-17.csv`.

La carpeta `data/history/` queda reservada para históricos consolidados.

## Análisis de ETB

`analyze_etb_history.py` elige la expansión más reciente que tenga al menos seis
meses y un Elite Trainer Box, selecciona sus 7 cartas con mayor precio y
descarga el histórico diario completo de Cardmarket y TCG Player. El resultado
se guarda como `data/history/<expansion>-YYYY-MM-DD-YYYY-MM-DD.csv`.

```powershell
python analyze_etb_history.py
python analyze_etb_history.py --publish
```

`--publish` confirma el CSV en Git y lo sube a GitHub; es la opción que usará
el futuro cron.

El plan Basic de la API devuelve 30 días por página y limita las peticiones por
minuto. El script se autorregula para respetarlo; una ventana de seis meses para
7 cartas se mantiene dentro del límite de 100 peticiones diarias.

Cada ejecución identifica la expansión publicada más recientemente. Si su CSV ya existe, no descarga de nuevo las cartas. Las extracciones usan la API de Pokémon TCG, con datos de Cardmarket en EUR.

## Automatización

`fetch_latest_expansion.py` identifica la expansión más reciente con una petición y descarga después todas sus cartas, en páginas de hasta 100. Genera una captura nueva en cada ejecución; si ya existe un CSV con la misma fecha, añade `-HHMMSS` al nombre.

Para configurar la clave localmente, copia `.env.example` a `.env` y rellena `RAPIDAPI_KEY`. La clave no se sube al repositorio. Cada CSV incluye el precio general `lowest_near_mint` y el de España `lowest_near_mint_ES`, ambos en EUR.
