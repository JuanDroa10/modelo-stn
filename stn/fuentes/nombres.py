# fuentes/nombres.py
# Traduccion entre los nombres de PARATEC y los buses del modelo.
#
# -------------------------------------------------------------------------
# EL PROBLEMA
# -------------------------------------------------------------------------
# Las dos partes nombran la misma subestacion de forma distinta:
#
#   sinsim/PARATEC   "CAÑO LIMON|230"      bus_id = SUBESTACION|kV
#   JUANCHO          "CANO LIMON__230"     bus    = SUB__NIVEL
#
# Medido sobre el snapshot del 2026-10-20 contra los 148 buses del modelo:
# de entrada casan 134 de 158 nombres (84,8 %). Las tres causas del resto no
# son errores de dato, son convenciones:
#
#   1. DIACRITICOS. El CSV de lineas de JUANCHO no tiene ni uno
#      ("CANO LIMON", "OCANA"); PARATEC si. Son 10 subestaciones, y solo
#      Caño Limon mueve 97 MW de demanda.
#   2. RESTOS DEL NOMBRE DEL TRAMO. El CSV arrastra el circuito y la tension
#      dentro del nombre: "PALENQUE 1 230 KV", "SOGAMOSO 2 230 KV",
#      "PURNIO (230)", "LA VIRGINIA (500 230)".
#   3. NOMBRES QUE PARECEN DISTINTOS Y NO LO SON. Ninguno, hoy. Ver abajo.
#
# Con (1) y (2) resueltos por regla, el resto de los huecos son buses que el
# equivalente modela y PARATEC no distingue, o al reves.
#
# -------------------------------------------------------------------------
# POR QUE LA TABLA DE ALIAS ESTA VACIA
# -------------------------------------------------------------------------
# Porque los tres alias que parecian evidentes resultaron ser FALSOS, y los
# tres de la misma manera: el modelo ya tiene las dos barras por separado.
#
#   ALFEREZ -> ALFEREZ (EEB)      el modelo tiene ALFEREZ__230 Y
#                                 ALFEREZ (EEB)__230
#   GUAVIO SUBT DUCTO -> GUAVIO   tiene GUAVIO SUBT DUCTO__230 Y GUAVIO__230
#   HIDROSOGAMOSO -> SOGAMOSO     tiene HIDROSOGAMOSO__230_U1/U2/U3 Y
#                                 SOGAMOSO__230
#
# Aplicarlos fusionaba dos barras reales en una. Es exactamente el fallo que
# ya se pago una vez con MIEL I y PORCE II, que compartian firma y acabaron
# repartiendose la placa a partes iguales. De ahi la guarda de
# `buses_por_subestacion()`: una colision no se resuelve por orden de
# iteracion, se levanta.
#
# La tabla se mantiene ---con su columna `nota` obligatoria--- porque el
# problema reaparecera en cuanto PARATEC renombre algo. La regla, que es la de
# sinsim: lo que no se pueda confirmar se deja SIN fila. El bus se queda sin
# datos y sale en el reporte de calidad, que es el resultado honesto.

import os
import re
import unicodedata

import pandas as pd
from stn.paths import RED, dato


FILE_ALIAS = dato(RED, 'alias_subestaciones_xm.csv')

# "PALENQUE 1 230 KV" -> "PALENQUE";  "PURNIO (230)" -> "PURNIO"
_SUFIJO_TRAMO = re.compile(r'\s+\d+\s+\d+\s*KV$')
_PAREN_KV = re.compile(r'\s*\(\s*\d+(\s+\d+)*\s*\)$')
# "230_U1" -> 230, marcando que es barra de unidad
_NIVEL = re.compile(r'^(\d+)')


def norm(s):
    """Mayusculas, espacios colapsados y sin diacriticos.

    Plegar los diacriticos no es cosmetico: es la diferencia entre ubicar
    Caño Limon y perderla. Ver la cabecera.
    """
    d = unicodedata.normalize('NFD', str(s))
    d = ''.join(c for c in d if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', d.strip().upper())


def nivel_modelo(kv):
    """El nivel con el que el modelo agrupa esa tension.

    Replica `data_loader.nivel_tension()`: todo lo que esta entre 200 y 400 kV
    es la misma barra de 230. Sin esto, PARATEC (que publica 220) y el CSV de
    lineas (que publica 230) no se encontrarian nunca.
    """
    kv = float(kv)
    return 230 if 200 <= kv < 400 else int(kv)


def cargar_alias(path=FILE_ALIAS):
    """{nombre en JUANCHO -> nombre en PARATEC}. Vacio si no hay fichero."""
    if not os.path.exists(path):
        return {}
    df = pd.read_csv(path, encoding='utf-8-sig')
    faltan = [c for c in ('juancho', 'paratec', 'nota') if c not in df.columns]
    if faltan:
        raise ValueError(f'{path}: faltan las columnas {faltan}')
    sin_nota = df[df.nota.isna() | (df.nota.astype(str).str.strip() == '')]
    if len(sin_nota):
        raise ValueError(
            f'{path}: {len(sin_nota)} alias sin `nota`: '
            f'{sin_nota.juancho.tolist()}. Un alias sin evidencia no entra.')
    return {norm(r.juancho): norm(r.paratec) for _, r in df.iterrows()}


def subestacion_de_bus(bus, alias=None):
    """De un bus del modelo a (subestacion en clave PARATEC, nivel, es_unidad).

    Devuelve (None, None, False) si el bus no tiene la forma SUB__NIVEL.
    """
    if '__' not in bus:
        return None, None, False
    sub, lvl = bus.rsplit('__', 1)
    m = _NIVEL.match(lvl)
    if not m:
        return None, None, False
    nivel, es_unidad = int(m.group(1)), '_U' in lvl
    sub = _PAREN_KV.sub('', _SUFIJO_TRAMO.sub('', norm(sub))).strip()
    if alias:
        sub = alias.get(sub, sub)
    return sub, nivel, es_unidad


def buses_por_subestacion(buses_modelo, alias=None):
    """{subestacion PARATEC -> bus del modelo al que sube la carga}.

    Cuando una subestacion tiene varios niveles se elige el MAS BAJO de los
    que el modelo representa: la carga de un STR entra por el 230, no por el
    500. Y una barra de unidad (HIDROSOGAMOSO__230_U1) nunca desplaza a la
    barra base del mismo nivel, porque repartir la demanda de una subestacion
    sobre las unidades de una central no tiene sentido fisico.
    """
    alias = cargar_alias() if alias is None else alias
    por_sub, vistos = {}, {}
    for b in sorted(buses_modelo):
        sub, nivel, es_unidad = subestacion_de_bus(b, alias)
        if sub is None:
            continue
        # Guarda de colision. Dos barras de UNIDAD de la misma central
        # (HIDROSOGAMOSO__230_U1/U2/U3) son el caso legitimo; dos barras base
        # en la misma clave no lo son nunca, y resolverlo por orden de
        # iteracion es como se perdio la placa de MIEL I y PORCE II.
        clave = (sub, nivel)
        if not es_unidad:
            if clave in vistos:
                raise ValueError(
                    f'colision de nombres: {vistos[clave]!r} y {b!r} caen '
                    f'los dos en {clave}. Son barras distintas del modelo; '
                    f'un alias que las fusione pierde una. Revisa '
                    f'{FILE_ALIAS}.')
            vistos[clave] = b
        actual = por_sub.setdefault(sub, {})
        if nivel not in actual or not es_unidad:
            actual[nivel] = b
    return {s: v[min(v)] for s, v in por_sub.items() if v}
