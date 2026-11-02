# Modelo-stn — dinámica de frecuencia del STN colombiano

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
programado y el pronóstico de demanda por barra que publica XM.

> **Información derivada de fuentes públicas de XM S.A. E.S.P. (PARATEC,
> `dDEC`, `PRON_BARRA`).** Los resultados describen un programa de despacho o
> un escenario de planeamiento, nunca el estado real del SIN.

## Requisitos

- **Python 3.11 o superior.** PyPSA 1.x no publica ruedas para versiones
  anteriores, así que con 3.10 la instalación falla. Compruébalo antes de
  empezar:

  ```bash
  python3 --version
  ```

  Si da 3.10 o menos, usa un intérprete más nuevo al crear el entorno
  (`python3.11 -m venv ...`, `python3.12 -m venv ...`).

- Las dependencias de `requirements.txt` (PyPSA, pandas, NumPy, SciPy,
  NetworkX, matplotlib, openpyxl, pyarrow, HiGHS, folium).

## Primeros pasos

El repositorio trae los datos necesarios, de modo que **funciona sin internet
desde el primer momento**:

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
python -m tests.test_fuente_xm       # 41 de la fuente de datos de XM
python -m tests.verificar_sistema    # 67 comprobaciones externas
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
  xm/            tres fechas de ejemplo, en la capa normalizada de sinsim
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
  El repositorio incluye tres fechas de ejemplo; para descargar otras se usa
  [sinsim](https://github.com/jucajata/sinsim) y se apunta la variable de
  entorno `SINSIM_DATA` a su carpeta de datos.
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
