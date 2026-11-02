# modelo-stn — dinámica de frecuencia del STN colombiano

Modelo del **Sistema de Transmisión Nacional** de 220/500 kV construido sobre
[PyPSA](https://pypsa.org/), con cuatro capas de análisis sobre él:

- **contingencias N-1** con límites de potencia reactiva,
- **dinámica de frecuencia** con el esquema EDAC del Acuerdo 2105 de 2026 del
  CNO y control secundario,
- **cascada multironda**, que realimenta flujo, protecciones y frecuencia, y
- **deslastre de carga consciente de la generación detrás del contador (BTM)**,
  un MILP que cumple el objetivo del EDAC destruyendo la menor cantidad posible
  de generación embebida sana.

El punto de operación puede venir de un caso de flujo de carga de PowerFactory
o construirse para **cualquier fecha y periodo** a partir del despacho
programado y el pronóstico de demanda por barra que publica XM, que el propio
modelo descarga.

> **Información derivada de fuentes públicas de XM S.A. E.S.P. (PARATEC,
> `dDEC`, `PRON_BARRA`).** Los resultados describen un programa de despacho o
> un escenario de planeamiento, nunca el estado real del SIN.

## Requisitos

- **Python 3.11 o superior.** PyPSA 1.x no publica ruedas para versiones
  anteriores, así que con 3.10 la instalación falla. Compruébalo antes de
  empezar:

  ```powershell
  py --list          # Windows: los intérpretes instalados
  ```

  ```bash
  python3 --version  # macOS o Linux
  ```

  Si da 3.10 o menos, usa un intérprete más nuevo al crear el entorno.

- Las dependencias de `requirements.txt` (PyPSA, pandas, NumPy, SciPy,
  NetworkX, matplotlib, openpyxl, pyarrow, HiGHS, folium).

## Primeros pasos

El repositorio trae los datos necesarios, de modo que **funciona sin internet
desde el primer momento** (solo hace falta para descargar fechas nuevas).

### Windows (PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python menu.py
```

Si `Activate.ps1` falla con un error de *execution policy* —lo habitual en una
instalación recién hecha—, se permite solo para esta ventana y se vuelve a
intentar:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### macOS o Linux (Bash)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python menu.py
```

Lo primero que pregunta el menú es **sobre qué sistema trabajar**, y esa
elección vale para toda la sesión:

```
SOBRE QUE SISTEMA QUIERES TRABAJAR
  1) punta nocturna 18-22 h   (el caso MEDIDO y validado)
  2) diurno 10-11 h           (con sol; DERIVADO de la punta)
  3) una fecha concreta       (despacho y pronostico reales de XM)
```

La tercera lista las fechas que ya están en disco y, al final, **`otra fecha
— descargarla de XM ahora`**: se escribe la fecha y el modelo se trae del
portal público el despacho y el pronóstico de ese día. Hace falta internet
solo para eso.

Después ofrece 22 opciones agrupadas en **simular**, **consultar el modelo** y
**análisis**. Cada una explica qué hace y cuánto tarda antes de lanzarla, y se
puede cambiar de punto en cualquier momento con la tecla `p`.

## Comandos

Todo lo que hace el menú está disponible también como módulo:

| Comando | Qué hace |
|---|---|
| `python -m stn.main` | flujo de carga, balance por área y mapas |
| `python -m stn.contingencias` | barrido N-1 de 296 elementos |
| `python -m stn.agc_deslastre --perder-mw 3000` | dinámica de frecuencia, EDAC y AGC |
| `python -m stn.deslastre_btm` | el algoritmo de deslastre consciente del BTM |
| `python -m stn.cascada --escenario iberia` | cascada multironda |
| `python -m stn.limites_reactivos` | curva de capacidad de reactivo |
| `python -m stn.barrido --fecha 2026-10-20 --grafico` | las 24 horas de una fecha |
| `python -m stn.comparar_casos` | punta y diurno, lado a lado |
| `python -m stn.validacion` | contraste contra PowerFactory |
| `python -m stn.mapa_interactivo` | mapa HTML georreferenciado |
| `python -m stn.fuentes.descarga_xm --fecha 2026-11-01` | trae una fecha nueva de XM |

Los módulos de simulación aceptan `--diurno [HORA]` y
`--fecha AAAA-MM-DD --periodo N` para correr sobre otro punto de operación:

```bash
python -m stn.contingencias --fecha 2026-10-20 --periodo 19
python -m stn.deslastre_btm --diurno --comparar
python -m stn.barrido --fecha 2026-10-20 2026-10-21 2026-10-24 --grafico
```

Las salidas se escriben en `resultados/`, con un sufijo por punto de operación
(`_diurno11`, `_20261020p19`) para que una ejecución no pise a la otra.

## Comprobaciones

```bash
python -m tests.test_invariantes     # 34 invariantes del modelo
python -m tests.test_regresion_pf    # 18 regresiones contra el caso congelado
python -m tests.test_fuente_xm       # 52 de la fuente de datos de XM
python -m tests.verificar_sistema    # 44 comprobaciones externas
```

## Estructura

```
menu.py        punto de entrada interactivo
stn/           el modelo
  paths.py       rutas, resueltas desde la raiz del repositorio
  config.py      parametros y banderas de comportamiento
  fuentes/       de donde sale el punto de operacion (PowerFactory o XM)
  ...            red, flujo, contingencias, frecuencia, EDAC, BTM, cascada
data/
  red/           lineas, mapeo nodo-barra, contorno geografico
  punto_operacion/  el caso de PowerFactory extraido
  paratec/       reportes de PARATEC: capacidad, inercia, estatismo, reactivo
  xm/            cuatro fechas de ejemplo; aqui aterrizan las que se bajen
  golden/        estado congelado del caso de referencia, para las regresiones
tests/         las cuatro baterias de comprobacion
tools/         utilidades: recapturar el congelado
```

## Fuentes de datos

- **PARATEC** (XM): parámetros técnicos de líneas, transformadores, reactores y
  unidades de generación —incluida la constante de inercia y el estatismo
  máquina a máquina para 30 de las 45 barras generadoras.
- **Despacho programado (`dDEC`) y pronóstico de demanda por barra
  (`PRON_BARRA`)** (XM): definen el punto de operación de una fecha concreta.
  El repositorio incluye cuatro fechas de ejemplo y
  `stn.fuentes.descarga_xm` trae cualquier otra del portal público, sin
  dependencias adicionales.

### Traer otra fecha

```bash
python -m stn.fuentes.descarga_xm --fecha 2026-11-01
```

De un día solo son suyos **el despacho y el pronóstico de demanda**; eso es lo
que se descarga. Las demás tablas del snapshot vienen de PARATEC, que es el
registro del sistema y **no tiene histórico** —su API devuelve siempre el
estado de hoy, se le pida la fecha que se le pida—, así que se heredan del
snapshot más reciente que haya en disco y el `manifest.json` deja escrito de
cuál. El modelo las usa solo como directorio: en qué subestación está cada
barra, qué línea une qué, dónde se conecta cada unidad. Un recurso que el
despacho nombre y el directorio no conozca no se cuela en silencio: baja la
cobertura, que es lo que el menú enseña antes de dejar seguir.

XM publica el despacho de un día la tarde anterior, de modo que la fecha más
nueva que se puede pedir es mañana.

Si el certificado del portal no se puede verificar —habitual detrás del proxy
de una empresa, que firma con una raíz propia que Python no conoce—, hay que
apuntar `SSL_CERT_FILE` a un almacén que sí la tenga:

```powershell
$env:SSL_CERT_FILE = "C:\ruta\a\cacert.pem"
```

```bash
export SSL_CERT_FILE=/opt/homebrew/etc/ca-certificates/cert.pem
```

En Windows suele no hacer falta: Python lee el almacén de certificados del
sistema, donde la raíz de la empresa ya está.

Las fechas descargadas con [sinsim](https://github.com/jucajata/sinsim)
también sirven: se apunta `SINSIM_DATA` a su carpeta de datos y el modelo lee
de ahí en vez de `data/xm/`.
- **Caso de flujo de carga de PowerFactory**: el punto de operación medido
  contra el que se contrasta el modelo.

## Cómo colaborar

El punto donde más rinde una contribución son las **tablas de equivalencias**
que relacionan los recursos del despacho y los nodos del pronóstico con las
subestaciones del modelo:

- `data/red/alias_subestaciones_xm.csv` — alias de subestación entre fuentes.
- `data/red/recursos_correccion_xm.csv` — recursos cuyo punto de conexión
  PARATEC deja aislado.
- `data/red/enlaces_internacionales.csv` — circuitos de interconexión.

Cada fila necesita evidencia: lo que no se puede justificar queda **sin
ubicar** en el reporte de calidad y el modelo lo declara, en vez de rellenarlo
en silencio. Un punto de operación que no alcance el 98 % de cobertura se marca
como no apto para estudios de frecuencia y los barridos lo excluyen.

Antes de abrir un *pull request*, las cuatro baterías de `tests/` deben pasar.

## Licencia

MIT. Ver [LICENSE](LICENSE).
