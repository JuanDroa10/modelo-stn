"""Modelo del Sistema de Transmision Nacional colombiano en PyPSA.

LA COMPROBACION DE VERSION NO ES CEREMONIA
------------------------------------------
PyPSA 1.x exige Python 3.11 o superior. En una maquina donde `python3` sea
3.10 ---que a dia de hoy es muy comun--- el `pip install -r requirements.txt`
del arranque rapido falla con un muro de texto sobre versiones ignoradas, y
el mensaje no dice en ninguna linea «actualiza Python». Quien lo lea por
primera vez concluye que el proyecto esta roto.

Mas vale decirlo aqui, una vez y en una frase.
"""

import sys

MINIMO = (3, 11)

if sys.version_info < MINIMO:
    raise RuntimeError(
        'este modelo necesita Python %d.%d o superior y se esta ejecutando '
        'con %d.%d.\n'
        'PyPSA 1.x no publica ruedas para versiones anteriores, de modo que '
        'ni siquiera llega a instalarse.\n'
        'Crea el entorno con un interprete mas nuevo, por ejemplo:\n'
        '    python3.11 -m venv .venv && source .venv/bin/activate\n'
        '    pip install -r requirements.txt'
        % (MINIMO[0], MINIMO[1], sys.version_info[0], sys.version_info[1]))

__version__ = '1.0.0'


# ---------------------------------------------------------------------------
# EL RUIDO DE PYPSA
# ---------------------------------------------------------------------------
# `pypsa.Network()` llama a `logging.basicConfig(level=INFO)` dentro de su
# propio constructor, de modo que basta con construir una red para que el
# logger raiz se ponga a volcar por consola una linea
#
#     INFO:pypsa.network.power_flow:Performing non-linear load-flow on ...
#
# por CADA flujo que se resuelve. En un barrido N-1 de 296 elementos son 296
# lineas de ruido entre quien mira la pantalla y la tabla de resultados, que
# es lo unico que fue a leer.
#
# Se baja el nivel POR NOMBRE, y no con `logging.disable(INFO)`, que es un
# interruptor global: apagaria de paso los INFO de cualquier otra biblioteca
# y los del propio modelo el dia que los tenga. Los WARNING de pypsa ---el
# que avisa de que un flujo NO ha convergido, el primero de todos--- siguen
# saliendo, que es justo lo que no se puede perder por querer una consola
# limpia.
import logging as _logging

for _ruidoso in ('pypsa', 'linopy'):
    _logging.getLogger(_ruidoso).setLevel(_logging.WARNING)

del _logging, _ruidoso
