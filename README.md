# Modelo del STN colombiano: dinámica de frecuencia y análisis de red

Modelo del **Sistema de Transmisión Nacional (STN)** colombiano de 220 y 500 kV, desarrollado sobre [PyPSA](https://pypsa.org/). El proyecto integra cuatro áreas principales de análisis:

- **Análisis de contingencias N-1**, con consideración de los límites de potencia reactiva de los generadores.
- **Dinámica de frecuencia**, que incorpora el esquema EDAC establecido en el Acuerdo 2105 de 2026 del CNO y la respuesta del control secundario (AGC).
- **Simulación de cascadas con múltiples rondas**, mediante la interacción entre flujos de potencia, protecciones y dinámica de frecuencia.
- **Deslastre de carga considerando la generación detrás del medidor (BTM)**, mediante un problema de optimización lineal entera mixta (MILP) que busca cumplir el objetivo de deslastre del EDAC y minimizar la desconexión de generación embebida que permanece en servicio.

El punto de operación puede obtenerse de un caso de flujo de carga de PowerFactory o construirse para una fecha y un periodo determinados a partir del despacho programado (`dDEC`) y del pronóstico de demanda por barra (`PRON_BARRA`) publicados por XM. El modelo permite descargar estos dos archivos para fechas adicionales.

> **Alcance de los datos:** la información utilizada para construir los escenarios de fecha específica procede de fuentes públicas de XM S.A. E.S.P. (`PARATEC`, `dDEC` y `PRON_BARRA`). Por tanto, los resultados representan un despacho programado o un escenario de planeamiento construido a partir de dichos datos; no corresponden a una medición del estado operativo real del SIN.

## Requisitos

- **Python 3.11 o superior.** El proyecto utiliza PyPSA 1.x, cuya instalación requiere una versión compatible de Python. Con Python 3.10 o anterior, la instalación de las dependencias puede fallar. Comprueba la versión disponible antes de crear el entorno:

  ```powershell
  py --list          # Windows: intérpretes instalados
  ```

  ```bash
  python3 --version  # macOS o Linux
  ```

  Si la versión disponible es 3.10 o anterior, instala una versión más reciente y utilízala para crear el entorno virtual.

- Las dependencias incluidas en `requirements.txt`: PyPSA, pandas, NumPy, SciPy, NetworkX, matplotlib, openpyxl, pyarrow, HiGHS y folium.

## Instalación y primeros pasos

El repositorio incluye los datos necesarios para ejecutar el modelo sin conexión a internet. Se requiere conexión únicamente para descargar información correspondiente a fechas que todavía no estén disponibles localmente.

### Windows (PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python menu.py
```

Si la activación del entorno virtual falla debido a la política de ejecución de PowerShell (`execution policy`), habilita la ejecución de scripts únicamente para la ventana actual y vuelve a ejecutar el comando de activación:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### macOS o Linux (Bash)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python menu.py
```

Al iniciar, el menú solicita seleccionar el **punto de operación** sobre el que se ejecutarán las consultas y simulaciones. La selección se mantiene durante toda la sesión:

```text
SOBRE QUE SISTEMA QUIERES TRABAJAR
   1) punta nocturna 18-22 h   (el caso MEDIDO y validado)
   2) diurno 10-11 h           (con sol; DERIVADO de la punta)
   3) una fecha concreta       (despacho y pronostico reales de XM)
```

La tercera opción muestra las fechas disponibles localmente e incluye, al final de la lista, la alternativa para descargar una fecha adicional desde XM. Al seleccionarla, se solicita la fecha y se descargan el despacho programado y el pronóstico de demanda correspondientes. Esta operación requiere conexión a internet.

Después de seleccionar el punto de operación, el menú presenta 18 opciones, organizadas en tres grupos: **Simular**, **Consultar el modelo** y **Análisis**. Antes de ejecutar cada opción, se muestra una descripción de su función y, cuando corresponde, de los parámetros y del tiempo de ejecución estimado. El punto de operación se puede cambiar durante la sesión mediante la tecla `p`.

La [guía de uso](GUIA_DE_USO.md) describe las 18 opciones del menú, sus subopciones, los valores predeterminados y el significado de cada parámetro. Se recomienda consultarla cuando se requiera información detallada sobre el funcionamiento de una opción.

El menú es la interfaz principal para utilizar el modelo. Algunas opciones de consulta y análisis están implementadas directamente en `menu.py` y no cuentan con un comando equivalente en la línea de comandos. Por otra parte, las rutinas de verificación —como la validación contra PowerFactory, las pruebas de invariantes y la evaluación de la curva de capacidad reactiva— se ejecutan mediante comandos independientes, descritos en las secciones siguientes.

## Ejecución desde la línea de comandos

Las opciones del menú que ejecutan módulos independientes también pueden invocarse directamente desde la terminal. El repositorio incluye, además, utilidades para construir escenarios, generar mapas y ejecutar verificaciones.

### Módulos de simulación y análisis

| Comando | Función |
|---|---|
| `python -m stn.main` | Ejecuta el flujo de carga y presenta el balance por área; también genera mapas y archivos de salida. |
| `python -m stn.contingencias` | Ejecuta el barrido de contingencias N-1 sobre 296 elementos. |
| `python -m stn.contingencias --caso "<elemento>"` | Simula la contingencia N-1 de un elemento específico. |
| `python -m stn.agc_deslastre --perder-mw 3000` | Simula la dinámica de frecuencia, la actuación del EDAC y la respuesta del AGC ante una pérdida de generación. |
| `python -m stn.deslastre_btm` | Ejecuta el algoritmo de deslastre de carga que considera la generación BTM. |
| `python -m stn.cascada --escenario iberia` | Simula una cascada con múltiples rondas para el escenario indicado. |
| `python -m stn.barrido --fecha 2026-10-20 --grafico` | Ejecuta el análisis de las 24 horas de una fecha y genera el gráfico correspondiente. |
| `python -m stn.comparar_casos --caso-a punta --caso-b diurno:11` | Compara dos puntos de operación y presenta los resultados lado a lado. |

### Utilidades y verificaciones

| Comando | Función |
|---|---|
| `python -m stn.limites_reactivos` | Evalúa la curva de capacidad de potencia reactiva de los generadores. |
| `python -m stn.escenario_diurno` | Construye y describe el escenario diurno a partir del caso de punta. |
| `python -m stn.validacion` | Contrasta el modelo con el caso de referencia de PowerFactory. |
| `python -m stn.mapa_interactivo` | Genera un mapa HTML georreferenciado de la red. |
| `python -m stn.fuentes.descarga_xm --fecha 2026-11-01` | Descarga desde XM la información de una fecha específica. |

Los módulos de simulación que admiten selección del punto de operación permiten utilizar las banderas `--diurno [HORA]` y `--fecha AAAA-MM-DD --periodo N`. Por ejemplo:

```bash
python -m stn.contingencias --fecha 2026-10-20 --periodo 19
python -m stn.deslastre_btm --diurno --comparar
python -m stn.barrido --fecha 2026-10-20 2026-10-21 2026-10-24 --grafico
```

El módulo `stn.comparar_casos` constituye una excepción, ya que recibe dos puntos de operación. Cada caso se especifica con uno de los siguientes formatos: `punta`, `diurno:HORA` o `AAAA-MM-DD:PERIODO`. Por ejemplo, para comparar los periodos 5 y 19 de una misma fecha:

```bash
python -m stn.comparar_casos --caso-a 2026-10-20:5 --caso-b 2026-10-20:19
```

Las salidas se guardan en el directorio `resultados/`. Los archivos incorporan un sufijo que identifica el punto de operación —por ejemplo, `_diurno11` o `_20261020p19`— para evitar que una ejecución sobrescriba los resultados de otra. El caso de punta no utiliza sufijo.

## Comprobaciones

El repositorio incluye cuatro conjuntos de pruebas para verificar propiedades del modelo, la consistencia con el caso de referencia y el procesamiento de las fuentes de datos:

```bash
python -m tests.test_invariantes      # 34 invariantes del modelo
python -m tests.test_regresion_pf     # 18 regresiones contra el caso congelado
python -m tests.test_fuente_xm        # 52 pruebas de la fuente de datos de XM
python -m tests.verificar_sistema     # 44 comprobaciones externas
```

## Estructura del repositorio

```text
.
├── README.md                 documentación general del proyecto
├── GUIA_DE_USO.md            manual de uso del menú, opción por opción
├── menu.py                   punto de entrada interactivo
├── requirements.txt          dependencias de Python
├── stn/                      implementación del modelo
│   ├── paths.py               rutas relativas a la raíz del repositorio
│   ├── config.py              parámetros y banderas de configuración
│   ├── fuentes/               construcción del punto de operación (PowerFactory o XM)
│   └── ...                    red, flujo de carga, contingencias, frecuencia, EDAC, BTM y cascada
├── data/                     datos de entrada y referencia
│   ├── red/                   líneas, mapeo nodo-barra y contorno geográfico
│   ├── punto_operacion/       caso de flujo de carga de PowerFactory
│   ├── paratec/               parámetros de PARATEC: capacidad, inercia, estatismo y reactivo
│   ├── xm/                    fechas de ejemplo y datos descargados desde XM
│   └── golden/                estado congelado del caso de referencia para las regresiones
├── tests/                    cuatro conjuntos de pruebas
└── tools/                    utilidades, incluida la actualización del caso congelado
```

## Fuentes de datos

El modelo utiliza las siguientes fuentes:

- **PARATEC (XM):** contiene parámetros técnicos de líneas, transformadores, reactores y unidades de generación. Incluye información de inercia y estatismo para 30 de las 45 barras generadoras consideradas.
- **Despacho programado (`dDEC`) y pronóstico de demanda por barra (`PRON_BARRA`) (XM):** se utilizan para construir el punto de operación de una fecha específica. El repositorio incluye cuatro fechas de ejemplo. El módulo `stn.fuentes.descarga_xm` permite descargar información de otras fechas desde el portal público de XM, sin instalar dependencias adicionales.
- **Caso de flujo de carga de PowerFactory:** corresponde al punto de operación de referencia utilizado para contrastar los resultados del modelo.

### Descarga de información para otra fecha

Para descargar los datos de una fecha específica, ejecuta:

```bash
python -m stn.fuentes.descarga_xm --fecha 2026-11-01
```

Para cada fecha, se descargan únicamente el **despacho programado** y el **pronóstico de demanda**. Las demás tablas utilizadas para describir la red proceden de PARATEC. Como PARATEC no dispone de un histórico consultable mediante la API utilizada por el proyecto, esta devuelve el registro vigente, independientemente de la fecha solicitada. Por este motivo, el modelo hereda esas tablas del snapshot más reciente disponible localmente. El archivo `manifest.json` registra el snapshot de origen.

Estas tablas se utilizan como directorio de referencia para identificar la ubicación de las barras, la conectividad de las líneas y los puntos de conexión de las unidades de generación. Cuando un recurso incluido en el despacho no tiene una correspondencia en ese directorio, no se incorpora de forma silenciosa: la cobertura del punto de operación disminuye y el menú presenta esta condición antes de permitir continuar.

XM publica el despacho de un día durante la tarde del día anterior. Por esta razón, la fecha más reciente que se puede solicitar es el día siguiente al actual.

Si Python no puede verificar el certificado del portal —por ejemplo, en redes corporativas con un certificado raíz propio que no está incluido en el almacén de certificados utilizado por Python—, define la variable de entorno `SSL_CERT_FILE` para que apunte a un archivo de certificados que incluya esa autoridad:

**Windows (PowerShell):**

```powershell
$env:SSL_CERT_FILE = "C:\ruta\a\cacert.pem"
```

**macOS o Linux (Bash):**

```bash
export SSL_CERT_FILE=/opt/homebrew/etc/ca-certificates/cert.pem
```

En Windows, normalmente no es necesario configurar esta variable, ya que Python puede utilizar el almacén de certificados del sistema, donde suele estar instalada la autoridad certificadora corporativa.

Los datos descargados mediante [sinsim](https://github.com/jucajata/sinsim) también pueden utilizarse. Para ello, define la variable de entorno `SINSIM_DATA` con la ruta de su directorio de datos. El modelo leerá la información desde esa ubicación en lugar de utilizar `data/xm/`.

## Cómo colaborar

Una contribución especialmente útil para mejorar la cobertura del modelo consiste en revisar y ampliar las **tablas de equivalencias** que relacionan los recursos del despacho y los nodos del pronóstico con las subestaciones representadas en el modelo:

- `data/red/alias_subestaciones_xm.csv`: alias de subestaciones utilizados entre las distintas fuentes.
- `data/red/recursos_correccion_xm.csv`: recursos cuyo punto de conexión no queda identificado directamente en PARATEC.
- `data/red/enlaces_internacionales.csv`: circuitos de interconexión internacional.

Cada correspondencia debe estar respaldada por evidencia. Los registros que no puedan justificarse permanecen **sin ubicar** en el reporte de calidad; el modelo no les asigna una ubicación por inferencia. Los puntos de operación con una cobertura inferior al 98 % se clasifican como no aptos para estudios de frecuencia y se excluyen de los barridos.

Antes de enviar un *pull request*, ejecuta los cuatro conjuntos de pruebas ubicados en `tests/` y comprueba que todos finalicen correctamente.

## Licencia

Este proyecto se distribuye bajo la licencia MIT. Consulta el archivo [LICENSE](LICENSE) para conocer sus condiciones.
