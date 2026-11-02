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
