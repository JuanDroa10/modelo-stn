"""Rutas del proyecto, resueltas desde la raiz del repositorio.

POR QUE EXISTE ESTE FICHERO
---------------------------
Todas las rutas del modelo eran relativas al directorio desde el que se
lanzara el proceso, no al proyecto: `'Datos/mapeo_STN.csv'`, `'paratec'`.
Eso funciona mientras uno trabaja siempre dentro de la carpeta del modelo,
y deja de funcionar en cuanto alguien clona el repositorio y lo ejecuta
desde otro sitio ---que es justo lo que hace quien lo descarga---.

Aqui la raiz se deduce de la posicion de ESTE fichero, de modo que da igual
desde donde se lance. `menu.py`, `python -m stn.contingencias` y una sesion
de Python abierta en cualquier parte ven las mismas rutas.

COMO SE USA
-----------
    from stn.paths import RED, PARATEC, dato

    RED / 'mapeo_STN.csv'          # objeto Path
    dato(RED, 'mapeo_STN.csv')     # str, para las APIs que esperan texto

Las constantes son `pathlib.Path`; `dato()` devuelve `str` porque pandas y
openpyxl lo aceptan igual pero algunas comparaciones del codigo esperan
cadena.
"""

import os
from pathlib import Path

#: Raiz del repositorio: la carpeta que contiene `stn/`.
RAIZ = Path(__file__).resolve().parent.parent

#: Datos de entrada, agrupados por lo que describen.
DATOS = RAIZ / 'data'
RED = DATOS / 'red'                     # lineas, mapeo, contorno, equivalencias
PUNTO = DATOS / 'punto_operacion'       # el caso de PowerFactory extraido
PARATEC = DATOS / 'paratec'             # los reportes del portal de XM
GOLDEN = DATOS / 'golden'               # el congelado de regresion

#: Snapshots de XM. Se puede apuntar fuera con la variable de entorno
#: SINSIM_DATA, que es lo que hay que hacer para usar fechas descargadas con
#: `sinsim scrape` en vez de las de ejemplo que trae el repositorio.
XM = Path(os.environ.get('SINSIM_DATA', DATOS / 'xm'))

#: Salidas. Se crea sola la primera vez que algo escribe.
RESULTADOS = RAIZ / 'resultados'


def dato(carpeta, *partes):
    """Ruta de un fichero de datos, como cadena."""
    return str(Path(carpeta).joinpath(*partes))


def salida(*partes):
    """Ruta de un fichero de salida, creando la carpeta si hace falta."""
    p = RESULTADOS.joinpath(*partes)
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)
