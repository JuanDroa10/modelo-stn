# deslastre_btm.py
"""Deslastre optimo por baja frecuencia con generacion detras del contador.

Que problema resuelve
---------------------
La generacion detras del contador (BTM) no la ve el operador. Solo mide el
NETO en cada barra:

    P_medido = P_carga_real - P_BTM

Cuando el BTM se desconecta de golpe -una nube sobre una region, una rampa de
viento, o una desconexion masiva por hueco de tension como en el apagon de
Espana de 2025- el operador no observa una perdida de generacion: observa que
la demanda sube sola. Es el "+dP_DK" de la formulacion.

Eso rompe el AGC por dos motivos:

  1. El ACE de cada area se calcula como dP_intercambio + B*df. Como el BTM
     esta repartido en miles de puntos pequenos, cada area ve una desviacion
     modesta y ninguna dispara por si sola, mientras el balance del conjunto
     ya esta muy desequilibrado y la frecuencia cae.
  2. Cuando el nivel de emergencia decide deslastrar, se encuentra con que el
     alivio real de botar una barra NO es lo que marca el contador. Si en esa
     barra todavia queda BTM conectado, al abrirla tambien se pierde esa
     generacion y el alivio neto es menor. Deslastrar a ciegas puede llegar a
     empeorar el balance.

Que hace este modulo
--------------------
  1. Reparte una penetracion BTM por area y simula su desconexion, total o
     parcial, en VARIAS AREAS A LA VEZ.
  2. Calcula el ACE de cada area y lo compara con el desbalance real, para
     cuantificar cuanto se le escapa al operador.
  3. Estima el BTM perdido a partir de lo observable, con un error de
     estimacion configurable.
  4. Resuelve un MILP que asigna cada barra a un escalon del EDAC teniendo en
     cuenta el BTM que queda en ella, y que es robusto frente a varios
     escenarios de desconexion.
  5. Simula la frecuencia y resuelve el flujo AC del estado final.

Formulacion del MILP
--------------------
Sigue a Arenas et al., "Optimizing Feeder Selection Across Underfrequency
Load Shedding Scheme Steps" (papers/Arenas.pdf):

    min  sum_{s,j} (up[s,j] + dn[s,j]) + lambda * sum_{b,j} btm_resto[b] x[b,j]

    s.a. sum_{b} alivio[s,b] * sum_{j'<=j} x[b,j']  >=  objetivo[j] - up[s,j]
         sum_{b} alivio[s,b] * sum_{j'<=j} x[b,j']  <=  objetivo[j] + dn[s,j]
         sum_{j} x[b,j] <= 1
         x[b,j] binaria

donde `s` recorre escenarios de desconexion BTM y `alivio[s,b]` es el alivio
NETO de abrir la barra b en ese escenario, no su lectura de contador. El
termino con lambda penaliza llevarse BTM sano por delante, que es la idea de
Das et al. (IEEE TPWRD 2016, papers/Improved_Load-Shedding...pdf).

La asignacion barra-escalon se decide UNA vez y tiene que servir para todos
los escenarios, que es como funciona un esquema real: los reles se ajustan por
adelantado, no se reprograman durante el evento.

De donde sale el BTM
--------------------
Por defecto, de los reportes de PARATEC: AGPE (144,9 MW, el 1,4% de la demanda,
99,7% solar) mas plantas menores no despachadas (1.496 MW, el 14,7%). Los
totales por area son dato; el reparto dentro del area es proporcional a la
demanda de cada barra, porque PARATEC registra municipio y no subestacion.

Con --fuente sintetico se usa una penetracion supuesta, para explorar
escenarios futuros con mas autogeneracion de la que hay hoy.

Uso:
    python deslastre_btm.py --fuente ambas --caida 80
    python deslastre_btm.py --fuente agpe --caida 100
    python deslastre_btm.py --fuente ambas --caida 80 --areas Caribe Antioquia
    python deslastre_btm.py --fuente sintetico --btm 25 --caida 90 --comparar
    python deslastre_btm.py --fuente ambas --caida 80 --perder-gen 800
"""

import argparse
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from stn.agc_deslastre import (F0, F_COLAPSO, EDAC_UMBRALES_HZ, EDAC_FRACCION,
                           Sistema, simular_frecuencia, aplicar_evento,
                           estado_red)

# Penetracion BTM por defecto, en % de la demanda del area. Solo se usa con
# --fuente sintetico; con las fuentes reales la penetracion sale de PARATEC.
BTM_POR_DEFECTO = 15.0

# Ajuste de subfrecuencia al que se dispara el BTM que seguia SANO, en Hz.
# `None` lo desactiva, que es lo que hay por omision.
#
# La realimentacion que modela es justo el fenomeno de este trabajo: cae la
# frecuencia, se desconecta mas generacion embebida, aparece como mas demanda,
# y la frecuencia cae mas. Es lo que ocurrio en Iberia el 28-4-2025.
#
# POR QUE NO ESTA ACTIVO. A diferencia de la solar de gran escala, que tiene
# ajuste declarado planta a planta en PARATEC, el BTM domestico son miles de
# inversores sin telemedida y **no hay ningun registro de su ajuste**. Los
# 58,5 Hz de `eventos.curva_disparo_btm()` son un supuesto del autor, no un
# dato, y encenderlo por omision moveria el resultado central del trabajo
# apoyandose en una cifra inventada.
#
# CUANTO IMPORTA, medido. Solo actua si el nadir baja de los 58,5 Hz, lo que
# en este sistema exige penetracion alta:
#
#     penetracion   nadir sin   nadir con   BTM sano que se autodispara
#         20 %        58,984      58,984         0 MW  (no se llega a 58,5)
#         40 %        58,366      58,285       508,2 MW
#
# o sea 0,081 Hz y 50,8 MW mas de deslastre en el caso de 40 %. Desactivarlo
# es por tanto la hipotesis OPTIMISTA, y conviene decirlo al presentar el
# umbral: sin esta realimentacion el sistema aguanta algo mas de lo que
# aguantaria si los inversores domesticos disparan donde se supone.
CASCADA_BTM_HZ = None

# raiz de la descarga de PARATEC, definida en un solo sitio
from stn.paratec_equipos import DIR_PARATEC

# ---------------------------------------------------------------------------
# CUANTO BTM HAY DE VERDAD EN COLOMBIA
# ---------------------------------------------------------------------------
#
# Revisado el 31-10-2026 contra fuentes oficiales, porque el pool que usaba
# este modulo NO era BTM. Dos errores, y no se cancelaban.
#
# ERROR 1: LAS PLANTAS MENORES NO SON BTM. El propio reporte de PARATEC cita
# la Resolucion CREG 086 de 1996 en su cabecera:
#
#     "Generacion con Plantas Menores: [...] operadas por EMPRESAS
#      GENERADORAS, productores marginales o productores independientes [...]
#      y que COMERCIALIZAN ESTA ENERGIA CON TERCEROS [...] La categoria de
#      Generacion con Plantas Menores y la de Autogenerador son EXCLUYENTES."
#
# La norma dice que no son autogeneradores. Los datos lo confirman: de las 343
# plantas, 124 pasan de 1 MW y suman 1.301,4 MW (el 87 %), y las diez mayores
# son centrales de 19,9 MW de ISAGEN, Celsia y Proelectrica. Son PCH que
# venden en bolsa y cuya generacion XM mide. De ahi salia el "55 % hidraulico"
# del pool: no era una rareza del BTM colombiano, eran esas PCH.
#
# ERROR 2: EL AGPE DE PARATEC ESTA SUBCONTADO. Sus 1.832 instalaciones
# (144,9 MW) tienen TODAS agente representante de mercado --1.826
# comercializador, 6 generador--: es el subconjunto que entrega excedentes al
# SIN y por eso figura ante XM. Minenergia contaba 24.447 techos solares en
# mayo de 2026; PARATEC ve el 7 %.
#
# LAS CIFRAS OFICIALES. Tablero Plan 6GW+ de la UPME, consultado el
# 31-10-2026 (ultima actualizacion que declara la pagina: 27-10-2026):
#
#     https://www.upme.gov.co/simec/plan-6gw/
#
#     AGPE  autogeneracion a pequena escala   740,83 MW
#     GD    generacion distribuida            605,95 MW
#     AGGE  autogeneracion a gran escala      593,90 MW
#
# QUE ENTRA EN EL CASO BASE Y QUE EN LA SENSIBILIDAD.
#
#   AGPE es BTM en sentido estricto: autoconsumo detras del contador, bajo
#     1 MW (CREG 174 de 2021), el operador solo ve el neto. Es el CASO BASE.
#
#   GD vende energia y el operador de red SI la mide, asi que estrictamente no
#     es BTM. Pero para lo que estudia este trabajo las tres condiciones que
#     importan se cumplen igual: entra por inversor, esta por debajo del STN
#     --o sea que se ve como menos demanda neta-- y no la despacha el CND.
#     Va como SENSIBILIDAD, no como caso base.
#
#   AGGE queda FUERA de las dos: por encima de 1 MW suele ser cogeneracion
#     sincrona industrial, que no comparte el modo de fallo del inversor.
#
# COMO SE USA EL DATO. UPME publica el total NACIONAL; el reparto por subarea
# que necesita `repartir_btm` solo lo trae PARATEC. Asi que se conserva la
# FORMA de PARATEC --su distribucion por subarea y su mezcla tecnologica-- y
# se reescala al total de UPME. Queda declarado el supuesto: la geografia del
# BTM es la del subconjunto registrado ante XM.
UPME_AGPE_MW = 740.83
UPME_GD_MW = 605.95
UPME_FECHA = '27-10-2026'
UPME_URL = 'https://www.upme.gov.co/simec/plan-6gw/'

# fuente -> (registros de PARATEC que dan la forma, total objetivo en MW)
# None en el total = no se reescala, se usa la placa de PARATEC tal cual.
ESCALA_OFICIAL = {
    'agpe':     (('agpe',), UPME_AGPE_MW),
    'agpe_gd':  (('agpe',), UPME_AGPE_MW + UPME_GD_MW),
}

ARCHIVO_AGPE = f'{DIR_PARATEC}/Generacion/PARATEC_AGPE_06-09-2026.xlsx'
ARCHIVO_MENORES = f'{DIR_PARATEC}/Generacion/PARATEC_Pmenores_06-09-2026.xlsx'
ARCHIVO_USOLAR = f'{DIR_PARATEC}/Generacion/PARATEC_Usolar_06-09-2026.xlsx'

# Constante de respuesta de frecuencia del area (bias), en MW/Hz. Se estima
# como la respuesta natural: 1/estatismo sobre la generacion del area mas el
# amortiguamiento de su carga.
BIAS_ESTATISMO = 0.05
BIAS_AMORTIGUAMIENTO = 1.5

# Peso de la penalizacion por llevarse BTM sano por delante, en la funcion
# objetivo del MILP. Barrido el 19-9-2026 sobre los dos regimenes (3 semillas):
#
#   lambda   BTM sano destruido   % del objetivo conseguido
#     0,5          33,7 MW              103,3 %      <- valor anterior
#     5,0           6,5 MW              104,0 %
#    25,0           0,3 MW              104,2 %
#
# En el evento de BTM subirlo es casi gratis: de 0,5 a 25 se destruye 100 veces
# menos generacion sana a cambio de 0,9 puntos mas de sobrecorte. Pero NO se
# puede calibrar sobre ese evento, y esta es la razon:
#
# LA PENALIZACION ESCALA CON EL BTM QUE SIGUE SANO, y eso es maximo justo
# cuando NO hay evento de BTM. Si lambda es grande, el termino de penalizacion
# domina al de desviacion y al MILP le sale mas barato no deslastrar nada que
# tocar una barra con generacion embebida. Medido en regimen normal (perdida
# convencional de 1.500 MW, el BTM instalado no se cae):
#
#   penetracion   lambda=5   lambda=25   lambda=65
#        14 %      100,9 %     100,9 %      99,8 %
#        20 %      102,5 %     101,9 %       0,0 %   <- no deslastra NADA
#        40 %      105,0 %      13,6 %       0,0 %
#        60 %      106,0 %       0,0 %       0,0 %
#
# Calibrar lambda sobre el escenario para el que se diseño el algoritmo habria
# elegido 25 ---el que mejor protege el BTM--- y habria apagado el esquema en
# operacion normal con penetracion alta, sin ningun aviso. El limite lo fija el
# regimen NORMAL a la maxima penetracion que se quiera cubrir, no el evento.
#
# 5,0 sobrevive los tres niveles de penetracion probados con margen y mejora
# cinco veces sobre el valor anterior. Ajustable con --lambda-btm.
LAMBDA_BTM = 5.0
UMBRAL_ACE = 50.0     # MW, a partir de los cuales un area "ve" el problema


# ---------------------------------------------------------------------------
# 1. Escenario BTM
# ---------------------------------------------------------------------------

@dataclass
class EscenarioBTM:
    """Como esta repartido el BTM y cuanto de el se cae."""
    penetracion_pct: float = BTM_POR_DEFECTO   # % de la demanda de cada area
    caida_pct: float = 60.0                    # % del BTM que se desconecta
    areas: tuple = ()                          # areas afectadas; vacio = todas
    error_estimacion_pct: float = 20.0         # error del estimador de BTM
    # % de la parte SINCRONA del BTM (hidraulica y termica pequenas) que se
    # desconecta en el evento. Cero por defecto: el mecanismo que se estudia
    # es la desconexion en bloque de inversores, y una maquina sincrona no
    # responde asi. Ver aplicar_caida_btm().
    caida_sincrona_pct: float = 0.0
    semilla: int = 0
    perdida_gen_mw: float = 0.0                # perdida convencional adicional
    penetracion_por_area: dict = field(default_factory=dict)
    # 'agpe' = solo autogeneradores (BTM estricto, 145 MW)
    # 'menores' = solo plantas menores no despachadas (1.496 MW)
    # 'ambas' = las dos (16,1% de la demanda)
    # 'sintetico' = penetracion supuesta con penetracion_pct
    # CASO BASE: el AGPE real que publica la UPME (740,83 MW), no el pool
    # mezclado con plantas menores que se usaba hasta el 31-10-2026. Ver la
    # cabecera del modulo y `cargar_registrada`.
    fuente: str = 'agpe'
    # Fraccion de la CAPACIDAD con inversor que esta PRODUCIENDO en el punto
    # de operacion. Lo que se desconecta es lo que estaba generando, no la
    # placa.
    #
    # EL DEFECTO 1,0 ES FALSO Y ESTA PUESTO A PROPOSITO. Todo lo publicado
    # hasta el 3-10-2026 asume capacidad = produccion, asi que cambiar el
    # defecto moveria cifras ya citadas sin avisar. Pero es un supuesto que
    # solo se sostiene a la hora de maxima irradiancia, y ni siquiera ahi:
    #
    #   punta nocturna (18-22 h)   produccion real = 0,00  -> el evento es
    #                              IMPOSIBLE, no pequeno
    #   diurno 10-11 h             produccion real ~ 0,77  -> el evento es un
    #                              23 % menor de lo que dice el defecto
    #
    # El 0,77 sale de las 14 plantas solares despachadas centralmente con
    # CEN >= 10 MW: entregan 1.572,1 MW sobre 2.017,0 MW de capacidad en su
    # hora pico (77,9 %), corregido por el cociente Hour11/Hour12 de 0,987.
    # Es un TECHO para la generacion de tejado: la AGPE no tiene seguidor y su
    # orientacion es subobtima, asi que rinde menos que una granja.
    #
    # `escenario_diurno` lo fija solo; a mano se pasa con --produccion-btm.
    produccion_inversor_pu: float = 1.0
    # Granularidad con la que se ubica el pool: 'subarea' (18 grupos) o
    # 'area' (5). Ver `repartir_btm`. Se guarda en el escenario para que el
    # reparto y la mezcla tecnologica no puedan desincronizarse.
    reparto: str = 'subarea'


def cargar_registrada(fuente='ambas', por='subarea'):
    """Capacidad de generacion pequena registrada en PARATEC, por zona.

    `por` elige la granularidad: 'subarea' (18 grupos, por defecto) o 'area'
    (5). La subarea es el dato que el registro trae de verdad; agregarlo a
    area y volver a repartirlo por demanda es tirar informacion y sustituirla
    por un supuesto. Importa mas aqui que en ningun otro sitio: es el dato
    contra el que decide el MILP.

    LAS FUENTES, Y CUAL ES BTM DE VERDAD (ver la cabecera del modulo):

      'agpe'     CASO BASE. Forma de PARATEC reescalada a los 740,83 MW que
                 publica la UPME. BTM estricto: autoconsumo bajo 1 MW detras
                 del contador, el operador solo ve el neto. ~97 % solar.
      'agpe_gd'  SENSIBILIDAD. Lo mismo reescalado a 1.346,78 MW (AGPE + GD).
                 La GD vende energia y el operador de red la mide, asi que no
                 es BTM estricto, pero comparte el modo de fallo del inversor
                 y tampoco la despacha el CND.
      'sintetico' penetracion supuesta, para escenarios de futuro. No toca
                 ningun registro.

    Y las dos que se conservan solo para poder REPRODUCIR cifras publicadas
    antes del 31-10-2026, no porque describan BTM:

      'agpe_paratec'  los 144,9 MW crudos del registro de PARATEC, sin
                 reescalar. Es el 7 % de los techos que cuenta Minenergia.
      'menores'  las 343 plantas menores (1.496,2 MW). NO SON BTM: la CREG
                 086 de 1996 las define como excluyentes con la figura de
                 autogenerador, son PCH de ISAGEN, Celsia y Proelectrica que
                 venden en bolsa, y 124 de ellas pasan de 1 MW.
      'ambas'    agpe_paratec + menores. Era el defecto hasta el 31-10-2026 y
                 es el pool equivocado: su 55 % hidraulico son esas PCH.
    """
    det = cargar_registrada_detalle(fuente)
    if not len(det):
        return pd.Series(dtype=float), {}
    clave = 'subarea' if por == 'subarea' else 'area'
    total = det.dropna(subset=[clave]).groupby(clave).MW.sum()
    return total, {k: float(v) for k, v in
                   det.groupby('registro').MW.sum().items()}


# Tecnologias que PARATEC declara y como las trata el modelo. Lo que importa
# para este trabajo no es el combustible sino EL MODO DE CONEXION: la
# electronica de potencia se desconecta en bloque ante un hueco de tension o
# una excursion de frecuencia ---es el mecanismo del apagon iberico de 2025---
# mientras que una maquina sincrona pequena tiene inercia propia, protecciones
# con ajustes mas holgados y no dispara simultaneamente con las demas.
INVERSOR = {'solar', 'eolica'}


def _normaliza_tec(t):
    """'Hidráulica' -> 'hidraulica'. Devuelve 'otra' si no se reconoce."""
    s = str(t).strip().lower()
    for a, b in (('á', 'a'), ('é', 'e'), ('í', 'i'), ('ó', 'o'), ('ú', 'u')):
        s = s.replace(a, b)
    if s.startswith('solar') or 'fotovolt' in s:
        return 'solar'
    if s.startswith('eolic'):
        return 'eolica'
    if s.startswith('hidr'):
        return 'hidraulica'
    if s.startswith('term'):
        return 'termica'
    if s.startswith('biomas') or s.startswith('bioga'):
        return 'biomasa'
    return 'otra' if s and s != 'nan' else 'otra'


def cargar_registrada_detalle(fuente='ambas'):
    """Capacidad registrada planta a planta, con area y tecnologia.

    Devuelve un DataFrame con columnas `registro` ('agpe'/'menores'), `area`,
    `tec` y `MW`.

    POR QUE HACE FALTA LA TECNOLOGIA. El escenario principal desconecta en
    bloque un porcentaje del BTM, aplicando el modo de fallo de un inversor.
    Pero el pool registrado no es homogeneo: medido el 19-9-2026 son 624,6 MW
    solares (38,1 %), 903,9 MW hidraulicos (55,1 %) y 112,1 MW termicos
    (6,8 %). Tratarlo como un solo bloque aplica una desconexion por hueco de
    tension a una mayoria de pequenas centrales hidraulicas, que no se
    comportan asi. Con el detalle por tecnologia, `aplicar_caida_btm` puede
    desconectar solo lo que fisicamente se desconecta.

    Los dos registros declaran la tecnologia de forma distinta: AGPE tiene una
    columna `Tipo de generacion`; en Plantas Menores la tecnologia es una fila
    de cabecera (con '-' en el operador) que encabeza su bloque de plantas.
    """
    from stn.data_loader import parse_float
    from stn.zonas import normaliza_subarea, AREA_DE_SUBAREA

    filas = []

    def subarea_de(d):
        sacol = next(c for c in d.columns if 'rea' in c and 'Sub' in c)
        return d[sacol].map(normaliza_subarea)

    def area_de(d):
        return subarea_de(d).map(AREA_DE_SUBAREA)

    if fuente in ('agpe', 'agpe_gd', 'agpe_paratec', 'ambas'):
        try:
            d = pd.read_excel(ARCHIVO_AGPE, sheet_name='AGPE', header=5,
                              dtype=str).dropna(how='all')
            d = d[d['Nombre'].notna() & (d['Nombre'] != '-')]
            d['MW'] = d['Capacidad instalada o nominal [MW]'].apply(parse_float)
            d['subarea'] = subarea_de(d)
            d['area'] = d['subarea'].map(AREA_DE_SUBAREA)
            d['tec'] = d['Tipo de generación'].map(_normaliza_tec)
            d['registro'] = 'agpe'
            filas.append(d.dropna(subset=['MW', 'area'])
                         [['registro', 'subarea', 'area', 'tec', 'MW']])
        except FileNotFoundError:
            pass

    if fuente in ('menores', 'ambas'):
        try:
            d = pd.read_excel(ARCHIVO_MENORES, sheet_name='Pmenores', header=7,
                              dtype=str).dropna(how='all')
            d['MW'] = d['Capacidad Efectiva Neta [MW]'].apply(parse_float)
            d['subarea'] = subarea_de(d)
            d['area'] = d['subarea'].map(AREA_DE_SUBAREA)
            # la tecnologia es la ultima fila de cabecera vista
            nom = d.columns[0]
            tec, col = None, []
            for _, r in d.iterrows():
                op = str(r['Operador']).strip()
                etiqueta = str(r[nom]).strip()
                if op == '-' and not etiqueta.upper().startswith('TOTAL'):
                    tec = _normaliza_tec(etiqueta)
                col.append(tec)
            d['tec'] = col
            d['registro'] = 'menores'
            d = d[(d['Operador'].notna()) & (d['Operador'].astype(str).str.strip() != '-')]
            filas.append(d.dropna(subset=['MW', 'area'])
                         [['registro', 'subarea', 'area', 'tec', 'MW']])
        except FileNotFoundError:
            pass

    if not filas:
        return pd.DataFrame(columns=['registro', 'subarea', 'area', 'tec', 'MW'])
    det = pd.concat(filas, ignore_index=True)

    # Reescalado al total oficial de la UPME. Se multiplica la columna MW por
    # un escalar, asi que la distribucion por subarea y la mezcla tecnologica
    # quedan EXACTAMENTE como las trae PARATEC: lo unico que cambia es el
    # tamano del pool. El supuesto que esto introduce ---que la geografia del
    # BTM real es la del subconjunto registrado ante XM--- esta declarado en
    # la cabecera del modulo y es el unico reparto disponible: la UPME publica
    # el total nacional y no lo desglosa.
    if fuente in ESCALA_OFICIAL:
        _, objetivo = ESCALA_OFICIAL[fuente]
        actual = float(det.MW.sum())
        if actual > 1e-9 and objetivo:
            det = det.assign(MW=det.MW * (objetivo / actual))
    return det


def cargar_solar_gran_escala(por='subarea', estado=None):
    """Capacidad solar de gran escala, del reporte Usolar.

    `estado` filtra por la columna `Estado` del reporte. None (defecto) no
    filtra y da los 4.200,1 MW de siempre; 'Operacion' da solo las plantas ya
    en operacion comercial.

    NO TODO LO REGISTRADO ESTA OPERANDO, y la diferencia no es menor. Medido
    sobre el reporte del 6-9-2026:

        Operacion   233 plantas   2.922,2 MW   (69,6 %)
        Pruebas     137 plantas   1.277,9 MW   (30,4 %)
        ---------------------------------------------
        total       370 plantas   4.200,1 MW

    Las de Pruebas estan fisicamente conectadas y si inyectan, pero su
    produccion es intermitente y no esta en regimen comercial. El defecto sigue
    siendo None para no mover los resultados ya publicados ---la curva de
    disparo por subfrecuencia, por ejemplo, debe incluirlas porque estan
    conectadas y sus protecciones actuan--- pero un escenario que afirme
    "cuanta solar habia produciendo" deberia decir cual de los dos usa.

    `por` elige la granularidad: 'subarea' (por defecto, 16 grupos) o 'area'
    (5 grupos). La subarea es notablemente mejor y no cuesta nada: el reporte
    Usolar trae la columna `Subarea operativa` planta a planta, las 370 plantas
    casan con PARATEC y las 16 subareas con solar tienen barra en el modelo.
    Repartir por area mete los 1.217 MW del ATLANTICO en un saco Caribe de
    2.316 MW y los esparce por toda la costa; por subarea se quedan donde
    estan. Importa porque el Caribe es area importadora y limitada.

    Son 370 plantas y 4.200 MW que NO estan en el modelo como generadores:
    cero de ellas conecta a 200 kV o mas, asi que todas entran por debajo y su
    produccion ya esta neteada dentro de la inyeccion equivalente de su barra.
    Vista desde el STN, la solar de 110 kV es indistinguible del BTM: no se ve
    como generacion, se ve como menos carga. Por eso se trata igual.

    No se solapa con las plantas menores: los dos registros no comparten ni un
    solo nombre de los 370 y 343 respectivos.

    OJO: esto es CAPACIDAD, no salida. Cuanto esta produciendo depende de la
    hora, y el caso de referencia es de punta de demanda -12.383 MW-, que en
    Colombia cae sobre las 19:00, con la solar practicamente en cero. Por eso
    la salida es un parametro explicito (`salida_pu`) y su valor por defecto es
    0: en el escenario que reproduce el modelo, la solar no esta generando.
    Para estudiar un evento diurno hay que subirlo a mano.
    """
    from stn.data_loader import parse_float
    from stn.zonas import normaliza_subarea, AREA_DE_SUBAREA
    from stn.paratec_equipos import capacidad_plantas

    d = pd.read_excel(ARCHIVO_USOLAR, sheet_name='Usolar', header=5,
                      dtype=str).dropna(how='all')
    d = d[d['Nombre'].notna()].copy()
    d['n'] = d['Nombre'].astype(str).str.upper().str.strip()
    sa = next(c for c in d.columns if 'rea' in str(c))
    d['subarea'] = d[sa].map(normaliza_subarea)
    d['area'] = d['subarea'].map(AREA_DE_SUBAREA)

    if estado is not None:
        col = next((c for c in d.columns if str(c).strip() == 'Estado'), None)
        if col is not None:
            def _sin_tilde(s):
                s = str(s).strip().lower()
                for a, b in (('á', 'a'), ('é', 'e'), ('í', 'i'),
                             ('ó', 'o'), ('ú', 'u')):
                    s = s.replace(a, b)
                return s
            d = d[d[col].map(_sin_tilde) == _sin_tilde(estado)]

    cap = capacidad_plantas()
    sol = cap[cap.tipo == 'solar'].copy()
    sol['n'] = sol.planta.astype(str).str.upper().str.strip()
    clave = 'subarea' if por == 'subarea' else 'area'
    m = d.merge(sol[['n', 'mw']], on='n', how='left').dropna(
        subset=['mw', clave])
    return m.groupby(clave).mw.sum()


def autodisparo_por_bus(traza, network, mapa_areas, resto, solar_pu=0.0):
    """Reparte por barra los MW que se autodispararon por subfrecuencia.

    La dinamica de frecuencia simula la cascada endogena ---solar de gran
    escala con su ajuste de PARATEC, y opcionalmente el BTM sano--- pero
    devuelve solo un total escalar, `mw_autodisparo`. Sin repartirlo por
    barra no puede entrar en el estado AC posterior, y el flujo se resolvia
    sobre una red a la que le faltaba esa desconexion: la frecuencia veia
    caerse 508 MW mas y el flujo AC no se enteraba.

    Es el mismo hueco que tenian los cambios del escenario, por otra via.

    Cada fuente se reparte con su propio criterio: la solar por el reparto de
    capacidad instalada entre areas, y el BTM por donde estaba el que seguia
    sano. Devuelve una Serie por barra en MW, lista para sumar a `carga_extra`.
    """
    total = float(traza.get('mw_autodisparo', 0.0))
    if total <= 1e-9:
        return pd.Series(dtype=float)

    por_fuente = {'solar': 0.0, 'btm': 0.0}
    for d in traza.get('disparos', []):
        if not d.get('disparado'):
            continue
        etq = str(d.get('etiqueta', '')).lower()
        por_fuente['btm' if 'btm' in etq or 'distribuida' in etq
                   else 'solar'] += float(d.get('mw', 0.0))

    out = pd.Series(0.0, index=network.loads.groupby('bus').p_set.sum().index)
    if por_fuente['solar'] > 1e-9:
        base = repartir_solar(network, mapa_areas, salida_pu=max(solar_pu, 1e-9))
        if float(base.sum()) > 1e-9:
            out = out.add(base / float(base.sum()) * por_fuente['solar'],
                          fill_value=0.0)
    if por_fuente['btm'] > 1e-9 and float(resto.sum()) > 1e-9:
        out = out.add(resto / float(resto.sum()) * por_fuente['btm'],
                      fill_value=0.0)
    return out[out > 1e-9]


def repartir_solar(network, mapa_areas, salida_pu=0.0, por='subarea'):
    """Salida solar por barra, repartida dentro de cada zona por demanda.

    `por` = 'subarea' (defecto) o 'area'. Dentro de la zona el reparto sigue
    siendo por demanda, que es un supuesto: no se conoce a que barra del STN
    entrega cada planta. Pero con la subarea ese supuesto solo tiene que
    repartir dentro de un grupo pequeno y geograficamente coherente, en vez
    de dentro de un area entera.
    """
    cap = cargar_solar_gran_escala(por=por)
    idx = 0 if por == 'subarea' else 1
    carga = network.loads.groupby('bus').p_set.sum()
    carga = carga[carga > 0]
    out = pd.Series(0.0, index=carga.index)
    if salida_pu <= 0:
        return out
    for zona, mw in cap.items():
        buses = [b for b in carga.index
                 if mapa_areas.get(b, ('', ''))[idx] == zona]
        if not buses:
            continue
        peso = carga[buses] / carga[buses].sum()
        out[buses] = peso * float(mw) * float(salida_pu)
    return out


def repartir_btm(network, esc, mapa_areas, por='subarea'):
    """BTM instalado en cada barra.

    El total de cada ZONA es un dato de PARATEC; el reparto entre las barras
    de esa zona es un supuesto, proporcional a la demanda, porque la
    autogeneracion se instala donde hay consumo y el registro da municipio,
    no subestacion.

    `por` decide el tamano de esa zona: 'subarea' (18 grupos, por defecto) o
    'area' (5). Con subarea el supuesto solo tiene que repartir dentro de un
    grupo pequeno y geograficamente coherente; con area, difumina el pool por
    medio pais.

    POR QUE IMPORTA MAS QUE EN LA SOLAR. Este es el dato contra el que decide
    el MILP: su valor entero esta en elegir que barras botar segun donde quede
    BTM sano. Si la ubicacion es un promedio por demanda, el optimizador
    trabaja sobre un patron espacial en parte ficticio. Medido al cambiar de
    area a subarea, se reubican 313,5 MW ---el 19 % del pool--- y entre ellos
    PAIPA (97,4), IBAGUE (37,5) y LA HERMOSA (35,7), que son barras que el
    MILP abre o protege.
    """
    from stn.zonas import AREA_DE_SUBAREA
    carga = network.loads.groupby('bus').p_set.sum()
    carga = carga[carga > 0]
    idx = 0 if por == 'subarea' else 1
    zona_de = {b: mapa_areas.get(b, ('', 'Sin area'))[idx] for b in carga.index}
    area_de = {b: mapa_areas.get(b, ('', 'Sin area'))[1] for b in carga.index}

    if esc.fuente == 'sintetico':
        # penetracion supuesta, uniforme dentro de cada area
        btm = {}
        for bus, p in carga.items():
            area = area_de[bus]
            if esc.areas and area not in esc.areas:
                continue
            pct = esc.penetracion_por_area.get(area, esc.penetracion_pct)
            btm[bus] = float(p) * pct / 100.0
        return pd.Series(btm, dtype=float)

    # capacidad real registrada por zona, repartida dentro de la zona en
    # proporcion a la demanda de cada barra.
    por_zona, _ = cargar_registrada(esc.fuente, por=por)

    # UNA ZONA SIN BARRA ELEGIBLE NO PUEDE TRAGARSE SU BTM. En el caso diurno
    # hay barras con carga NETA NEGATIVA ---Ibague, La Hermosa, Armenia: la
    # solar local supera a la demanda local--- y subareas enteras (CQR,
    # HUILA-TOLIMA) se quedan sin ninguna barra de carga positiva. Filtrar por
    # `carga > 0` hacia desaparecer sus 182,1 MW de BTM, y con ellos el evento
    # caia de 452,8 a 390,3 MW: lo justo para que el EDAC dejara de disparar.
    # Una conclusion distinta por un MW que se pierde en la contabilidad.
    #
    # El BTM esta ahi aunque la INYECCION neta de la barra sea negativa: lo que
    # es negativo es el balance, no el consumo. Asi que el peso es la carga
    # positiva donde la haya, su valor absoluto si toda la zona esta en
    # negativo, y si la zona no tiene ninguna barra, su BTM sube al AREA.
    bruta = network.loads.groupby('bus').p_set.sum()
    buses_zona = {}
    for b in bruta.index:
        buses_zona.setdefault(
            mapa_areas.get(b, ('', 'Sin area'))[idx], []).append(b)

    btm = {}
    huerfano = {}
    for zona, cap in por_zona.items():
        cap = float(cap)
        if cap <= 0:
            continue
        bs = [b for b in buses_zona.get(zona, [])
              if not esc.areas or area_de.get(b) in esc.areas]
        peso = bruta.reindex(bs).fillna(0.0).clip(lower=0.0) if bs else None
        if peso is None or float(peso.sum()) <= 1e-9:
            # toda la zona en negativo: se reparte por magnitud
            peso = bruta.reindex(bs).fillna(0.0).abs() if bs else None
        if peso is None or float(peso.sum()) <= 1e-9:
            # la zona no existe en el modelo: su BTM se acumula a su area
            ar = AREA_DE_SUBAREA.get(zona, zona) if por == 'subarea' else zona
            huerfano[ar] = huerfano.get(ar, 0.0) + cap
            continue
        peso = peso / float(peso.sum())
        for b, w in peso.items():
            btm[b] = btm.get(b, 0.0) + cap * float(w)

    # lo huerfano, al area que le corresponde
    for ar, cap in huerfano.items():
        bs = [b for b in bruta.index
              if area_de.get(b) == ar and (not esc.areas or ar in esc.areas)]
        peso = bruta.reindex(bs).fillna(0.0).clip(lower=0.0) if bs else None
        if peso is None or float(peso.sum()) <= 1e-9:
            continue
        peso = peso / float(peso.sum())
        for b, w in peso.items():
            btm[b] = btm.get(b, 0.0) + cap * float(w)

    # SIN LAS BARRAS A CERO. No es cosmetica: `aplicar_caida_btm` sortea un
    # vector aleatorio de longitud len(btm), asi que arrastrar barras con cero
    # desplaza el sorteo entero y cambia los resultados de todas las demas sin
    # que nada lo justifique. Medido al introducirlas sin querer: el evento
    # diurno paso de 452,8 a 437,2 MW solo por eso.
    # ORDEN DETERMINISTA.  sortea un vector posicional, de
    # modo que el orden del indice decide que dispersion le toca a cada barra.
    # Sin ordenar, el resultado dependia del orden en que se recorren las
    # zonas, que es un detalle de implementacion.
    s = pd.Series(btm, dtype=float).sort_index()
    return s[s > 1e-9]


def fraccion_inversor(esc, mapa_areas, buses, por='subarea'):
    """Que fraccion del BTM de cada barra se conecta por inversor.

    El BTM se reparte dentro de cada zona en proporcion a la demanda, asi que
    la mezcla tecnologica de una barra es la de su zona. Devuelve una Serie
    indexada por barra con valores en [0, 1].

    `por` TIENE QUE COINCIDIR CON EL DE `repartir_btm`. Si el pool se ubica
    por subarea pero la mezcla se toma del area, a una barra se le asigna la
    capacidad de su subarea con la proporcion solar/hidraulica de media
    provincia, y las dos mitades del escenario dejan de describir el mismo
    sistema. Por eso el defecto es el mismo en las dos.

    Con `--fuente sintetico` no hay registro que consultar: la penetracion es
    un supuesto sobre autogeneracion futura, que se asume fotovoltaica, y la
    fraccion vale 1 en todas las barras.
    """
    if esc.fuente == 'sintetico':
        return pd.Series(1.0, index=list(buses))
    det = cargar_registrada_detalle(esc.fuente)
    if not len(det):
        return pd.Series(1.0, index=list(buses))
    clave = 'subarea' if por == 'subarea' else 'area'
    idx = 0 if por == 'subarea' else 1
    por_zona = det.dropna(subset=[clave]).groupby(
        [clave, 'tec']).MW.sum().unstack(fill_value=0.0)
    inv = por_zona.reindex(columns=sorted(INVERSOR), fill_value=0.0).sum(axis=1)
    frac = (inv / por_zona.sum(axis=1).replace(0, np.nan)).fillna(1.0)
    zona_de = {b: mapa_areas.get(b, ('', 'Sin area'))[idx] for b in buses}
    return pd.Series({b: float(frac.get(zona_de[b], 1.0)) for b in buses})


def btm_produciendo(btm, esc, frac_inversor=None):
    """Pasa de capacidad instalada a potencia que esta generando.

    Devuelve (btm_efectivo, frac_inversor_efectiva).

    POR QUE NO BASTA CON ESCALAR LA CAIDA. Se podria aplicar el factor de
    produccion solo al trozo que se desconecta, pero entonces el `resto`
    ---el BTM que queda sano, sobre el que se mide el dano del deslastre---
    seguiria contando la solar que no estaba produciendo. Botar una barra
    cuya fotovoltaica esta apagada no destruye generacion embebida: no hay
    ninguna. Inflaria la metrica central del trabajo.

    Por eso el factor se aplica ANTES, a la capacidad, y la fraccion de
    inversor se recalcula sobre el pool ya reducido: si la solar produce al
    77 % y la hidraulica pequena al 100 %, la mezcla del pool cambia y la
    parte que responde al modo de fallo de inversor es menor.
    """
    pu = float(esc.produccion_inversor_pu)
    if frac_inversor is None or abs(pu - 1.0) < 1e-9:
        return btm, frac_inversor

    fi = frac_inversor.reindex(btm.index).fillna(1.0)
    inv = btm * fi * pu
    sinc = btm * (1.0 - fi)
    efectivo = inv + sinc
    fi_ef = (inv / efectivo.where(efectivo > 1e-12)).fillna(0.0)
    return efectivo, fi_ef


def aplicar_caida_btm(btm, esc, frac_inversor=None):
    """Devuelve (btm_caido, btm_resto) por barra.

    La caida no es uniforme: una nube o un hueco de tension afecta mas a unas
    zonas que a otras. Se modela con una dispersion alrededor del porcentaje
    nominal para que el escenario no sea artificialmente homogeneo.

    NO TODO EL BTM SE CAE IGUAL. `frac_inversor` es la parte del BTM de cada
    barra conectada por electronica de potencia. El evento que se estudia ---un
    hueco de tension o una excursion de frecuencia que desconecta inversores en
    bloque, el mecanismo del apagon iberico--- es especifico de esa
    electronica. Las pequenas centrales hidraulicas y termicas del registro son
    maquinas sincronas: tienen inercia propia, ajustes de proteccion mas
    holgados y no disparan todas a la vez.

    Sin este reparto el escenario aplicaba `caida_pct` al pool entero, del que
    el 55 % es hidraulico: se estaba atribuyendo a turbinas de agua un modo de
    fallo de inversor. La caida de la parte sincrona se controla aparte con
    `esc.caida_sincrona_pct`, que por defecto es CERO ---no participan en este
    modo de fallo---. Es un supuesto, y es el conservador para la tesis: hace
    el evento MAS PEQUENO, no mas alarmante.

    Con `frac_inversor=None` se mantiene el comportamiento anterior (todo el
    pool cae por igual), para poder comparar.
    """
    rng = np.random.default_rng(esc.semilla)
    frac = np.clip(rng.normal(esc.caida_pct / 100.0, 0.15, len(btm)), 0.0, 1.0)
    if frac_inversor is not None:
        fi = frac_inversor.reindex(btm.index).fillna(1.0).values
        f_sinc = np.clip(
            rng.normal(esc.caida_sincrona_pct / 100.0, 0.05, len(btm)), 0.0, 1.0)
        frac = frac * fi + f_sinc * (1.0 - fi)
    caido = btm * frac
    return caido, btm - caido


# ---------------------------------------------------------------------------
# 2. ACE por area: lo que el operador SI ve
# ---------------------------------------------------------------------------

def bias_por_area(network, mapa_areas, sis):
    """Constante de respuesta de frecuencia (MW/Hz) de cada area."""
    gen = network.generators.groupby('bus').p_set.sum()
    carga = network.loads.groupby('bus').p_set.sum()
    out = {}
    for bus in network.buses.index:
        a = mapa_areas.get(bus, ('', 'Sin area'))[1]
        g = float(gen.get(bus, 0.0))
        c = float(carga.get(bus, 0.0))
        b = out.setdefault(a, {'gen': 0.0, 'carga': 0.0})
        b['gen'] += g
        b['carga'] += c
    return pd.Series({
        a: (v['gen'] / BIAS_ESTATISMO + v['carga'] * BIAS_AMORTIGUAMIENTO) / F0
        for a, v in out.items()})


def desviacion_intercambio(deficit_area, bias):
    """Desviacion de intercambio de cada area respecto de lo programado.

    Cuando falta generacion, los reguladores de TODO el sistema responden y el
    reparto es proporcional al bias de cada area. El area i acaba importando la
    diferencia entre su propio deficit y lo que aportan sus maquinas:

        dP_tie[i] = -( deficit[i] - bias[i]/sum(bias) * deficit_total )

    Negativo significa que el area importa mas de lo programado. Aqui esta la
    clave del problema del BTM: si el deficit se reparte entre las areas mas o
    menos en la misma proporcion que su bias, este termino se acerca a cero en
    todas. Ninguna ve nada raro en sus intercambios, aunque el sistema entero
    este muy desequilibrado.
    """
    total = float(deficit_area.sum())
    b = bias.reindex(bias.index).fillna(0.0)
    aporte = b / max(float(b.sum()), 1e-9) * total
    d = deficit_area.reindex(bias.index).fillna(0.0)
    return -(d - aporte)


def ace_por_area(deficit_area, delta_f, bias):
    """ACE = desviacion de intercambio + B * df, el convenio habitual."""
    return desviacion_intercambio(deficit_area, bias) + bias * delta_f


def diagnostico_ace(caido_por_area, bias, delta_f):
    """Separa lo que cada area ve por sus intercambios (senal LOCAL) de lo que
    ve por la frecuencia (senal GLOBAL).

    La distincion importa: la frecuencia avisa de que algo pasa, pero es igual
    en todo el sistema y no dice donde. Lo unico que localiza el problema es la
    desviacion de intercambio, y es justo lo que un evento distribuido no
    produce.
    """
    total = float(caido_por_area.sum())
    tie = desviacion_intercambio(caido_por_area, bias)
    d = pd.DataFrame({
        'deficit_real_MW': caido_por_area.reindex(bias.index).fillna(0.0).round(1),
        'dP_tie_MW': tie.round(1),
        'B_df_MW': (bias * delta_f).round(1),
        'ACE_MW': ace_por_area(caido_por_area, delta_f, bias).round(1),
    })
    d['localiza'] = d.dP_tie_MW.abs() > UMBRAL_ACE
    return d, total


# ---------------------------------------------------------------------------
# 3. Estimacion del BTM perdido
# ---------------------------------------------------------------------------

def estimar_btm_perdido(caido_real, esc):
    """Estimacion del BTM que se ha caido, con error.

    Los metodos de los papers (Kabir et al., y la desagregacion no supervisada
    de Applied Energy) necesitan SERIES TEMPORALES de la lectura neta para
    separar carga y solar. Aqui solo hay una foto del sistema, asi que no se
    puede reproducir esa desagregacion. Lo que se hace es lo unico honesto con
    los datos disponibles: tomar el valor real y degradarlo con un error de
    estimacion configurable, para poder medir CUANTO IMPORTA ese error en la
    decision de deslastre. Ese es justamente el argumento de Das et al.: el
    error del estimador solo degrada la priorizacion, no la invalida.
    """
    rng = np.random.default_rng(esc.semilla + 1)
    err = rng.normal(0.0, esc.error_estimacion_pct / 100.0, len(caido_real))
    return (caido_real * (1.0 + err)).clip(lower=0.0)


# Calibracion del error de estimacion, de la literatura y no inventada.
#
# Qu, Ge, Lu y Wang, "Unsupervised disaggregation of aggregated net load
# considering behind-the-meter PV", Applied Energy 381 (2025) 125007. Es el
# unico de los tres papers que trabaja a nivel AGREGADO, que es el nivel de
# este modelo, y el unico que caracteriza la distribucion del error.
#
#   - nMAE del BTM a nivel agregado: 12,3% con penetracion del 20% (Tabla 3).
#   - El error es INSESGADO y normal: "el valor medio de la distribucion del
#     error es 0, sin sesgo significativo" (Sec. 4.5, Fig. 9). Esto es lo que
#     justifica usar una normal centrada en cero y no un sesgo inventado.
#   - Suelo irreducible: incluso conociendo la FV real, el PV-MAPE no baja de
#     11,7% (Tabla 5). El grueso del error no viene de la falta de datos.
#   - Escala con la penetracion (Tabla 8): nMAE 12-20% entre el 20% y el 120%,
#     subiendo a ~25% en los extremos.
#
# A eso hay que componerle el error del REGISTRO de capacidad instalada, que
# es una fuente distinta: Kabir et al., IEEE Trans. Sustainable Energy 12(1)
# 2021, Sec. IV-C-4, reportan un MAPE del 18-20% al estimar la capacidad
# declarada. Compuestas como independientes:
#     sigma_total = sqrt(0.155^2 + 0.19^2) = 0.245
ERROR_DESAGREGACION = 0.155   # nRMSE diurno derivado de Applied Energy 2025
ERROR_CAPACIDAD = 0.19        # MAPE del registro de capacidad instalada

# Cuanto del error es COMUN a toda una region y cuanto es propio de cada barra.
# ATENCION: esto NO sale de los papers. Ninguno de los tres analiza correlacion
# espacial del error entre zonas; el de Applied Energy trabaja sobre un unico
# agregado y los de Kabir asumen independencia condicional entre clientes. Se
# extrapola del efecto de shrinkage que sí cuantifican (Paper 2, Fig. 3: la
# desviacion de los interceptos cae de 0,49 sin agrupar a 0,14 agrupando, o sea
# que ~8% de la varianza es idiosincratica). El argumento fisico es que el
# error del BTM lo domina el error de irradiancia, y la irradiancia esta
# correlacionada a escala regional. Queda declarado como supuesto propio.
FRACCION_ERROR_COMUN = 0.90


def estimar_btm(btm_real, esc, mapa_areas=None, capacidad=None):
    """El BTM instalado por barra TAL COMO LO VE EL OPERADOR.

    Esta es la entrada del optimizador. Antes el MILP recibia el BTM real, de
    modo que decidia con informacion perfecta y `--error-estimacion` no podia
    cambiar ninguna decision: con error del 0%, 20% y 60% el resultado era
    identico hasta el ultimo decimal. Se reportaba un error de estimacion que
    no afectaba a nada, y el modulo no podia responder a la pregunta para la
    que existe, que es si el deslastre BTM-aware sigue siendo mejor cuando el
    operador se equivoca al estimar.

    La separacion es la de cualquier decision bajo incertidumbre: se DECIDE
    sobre la estimacion y se EVALUA sobre la realidad. El alivio real que se
    consigue (`alivio_real` en ejecutar()) sigue calculandose con el BTM
    verdadero, porque es lo que de verdad ocurre al abrir el interruptor.

    El error tiene tres propiedades que NO son un ruido gaussiano cualquiera:

    1. Es INSESGADO. No se supone: Applied Energy 2025, Sec. 4.5, verifica que
       la media de la distribucion del error es cero para las tres variantes
       de su metodo. Por eso la normal va centrada en 0.

    2. Esta CORRELACIONADO ESPACIALMENTE. El error del BTM lo domina el error
       de irradiancia, y dos barras de la misma region comparten nubes. Un
       ruido independiente por barra se cancela al agregar -con 94 barras, el
       error de area se dividiria por diez- y eso hace el problema
       artificialmente facil. Aqui el error se descompone en una componente
       comun a cada area y otra propia de cada barra.

    3. Esta ACOTADO POR LA FISICA: un BTM estimado no puede ser negativo. Se
       consigue con un error MULTIPLICATIVO LOGNORMAL en vez de truncar una
       normal, y la diferencia importa.

       Antes se muestreaba una normal y se truncaba por los dos lados: a cero
       por abajo y a `btm_real/0,8` por arriba. Las dos cotas eran un
       problema. La de arriba no tiene justificacion fisica ---`btm_real` es
       la PRODUCCION del instante, no la capacidad del inversor, asi que una
       estimacion legitima puede superarla holgadamente cuando la produccion
       real es baja--- y la asimetria entre ambas destruia la insesgadura que
       la propiedad 1 declara. Medido sobre 20 semillas:

           error nominal   sesgo    tocan el tope   desv realizada
                   10 %   -1,4 %           0,0 %       0,096
                   20 %   -3,8 %          11,5 %       0,181
                   40 %  -11,7 %          29,0 %       0,303
                  100 %  -25,1 %          39,8 %       0,443

       o sea que subir `--error-estimacion` no añadia solo ruido: añadia
       sesgo negativo creciente Y comprimia la dispersion, de modo que el
       estudio de sensibilidad confundia tres efectos en una sola perilla.

       Con la lognormal, si `s = sqrt(ln(1+sigma^2))` y `z ~ N(0,1)`,

           est = btm * exp(s*z - s^2/2)

       cumple `E[est] = btm` exactamente y `CV[est] = sigma` exactamente, sin
       truncar nada y sin poder ser negativo. La correlacion espacial de la
       propiedad 2 se conserva porque se aplica sobre `z`.

       Si alguna vez se dispone de la capacidad instalada del inversor por
       barra, se puede pasar en `capacidad` y se aplica como cota superior:
       esa si es una cota fisica de verdad.

    Y hay una cuarta propiedad que no se implementa aqui sino en la estructura
    del modulo: el error del BTM y el de la carga bruta estan PERFECTAMENTE
    ANTICORRELADOS. La lectura del contador es `P_medido = P_carga - P_BTM` y
    es un dato exacto, asi que equivocarse en X MW de BTM implica equivocarse
    en los mismos X MW de carga, con signo contrario. Es un unico grado de
    libertad por barra, no dos. `escenarios_alivio` lo respeta al calcular la
    carga bruta como `carga_medida + btm_estimado`: si se muestrearan por
    separado se violaria la identidad del contador.
    """
    rng = np.random.default_rng(esc.semilla + 7)
    sigma = esc.error_estimacion_pct / 100.0
    if sigma <= 0:
        return btm_real.copy()

    # Normal estandar con la estructura espacial de la propiedad 2: una
    # componente comun a cada area y otra propia de cada barra. Las varianzas
    # suman 1, de modo que `z` es N(0,1) y la calibracion de `sigma` se
    # conserva intacta.
    fc = FRACCION_ERROR_COMUN
    areas = {}
    if mapa_areas is not None:
        areas = {b: mapa_areas.get(b, ('', 'Sin area'))[1]
                 for b in btm_real.index}
    unicas = sorted(set(areas.values())) or ['unica']
    comun = {a: rng.normal(0.0, 1.0) for a in unicas}

    z = np.array([comun.get(areas.get(b, 'unica'), 0.0)
                  for b in btm_real.index]) * np.sqrt(fc)
    z = z + rng.normal(0.0, 1.0, len(btm_real)) * np.sqrt(1.0 - fc)

    # Lognormal con media exactamente `btm_real` y coeficiente de variacion
    # exactamente `sigma`. El -s^2/2 es lo que cancela el sesgo que la
    # exponencial introduciria de otro modo.
    s = np.sqrt(np.log1p(sigma ** 2))
    est = btm_real * np.exp(s * z - 0.5 * s ** 2)

    # Unica cota fisica de verdad, y solo si se conoce: la capacidad instalada
    # del inversor. Por omision no se aplica ninguna; la lognormal ya garantiza
    # que el resultado es positivo.
    if capacidad is not None:
        tope = (capacidad.reindex(est.index).fillna(np.inf)
                if hasattr(capacidad, 'reindex') else capacidad)
        est = est.clip(upper=tope)
    return est


# ---------------------------------------------------------------------------
# 4. MILP de asignacion barra -> escalon del EDAC
# ---------------------------------------------------------------------------

def escenarios_alivio(network, btm, esc, n_escenarios=5, frac_inversor=None):
    """Alivio de abrir cada barra, en varios escenarios, y el piso garantizado.

    Devuelve `(alivio, piso)`:

      - `alivio`: DataFrame barra x escenario con el alivio ESPERADO,

            alivio = carga_real(b) - btm_que_sigue_conectado(b)
                   = carga_medida(b) + btm_que_se_cae(b)

        Si en la barra queda BTM sano, al abrirla tambien se pierde y el
        alivio es menor. En el limite, abrir una barra con mas BTM que carga
        EMPEORA el balance y el alivio sale negativo.

      - `piso`: Serie con la carga MEDIDA de cada barra. Es un alivio que se
        consigue con certeza, porque el BTM que se caiga solo puede sumar:
        al desconectarse, la barra pasa a demandar mas de la red, nunca menos.
        Ningun error de estimacion puede hacerlo bajar.

    DOS FUENTES DE INCERTIDUMBRE, NO UNA. Antes los escenarios solo variaban
    la FRACCION del BTM que se cae, partiendo siempre de la misma estimacion:
    si el estimador estaba sesgado en un area, los cinco escenarios heredaban
    el mismo sesgo y el conjunto no cubria el error de estimacion, solo el
    reparto de la caida. Como ademas el alivio se construye con esa
    estimacion, el MILP decidia como si conociera el BTM y se quedaba corto al
    ejecutarse. Ahora cada escenario perturba tambien la propia estimacion con
    la desviacion que el operador le atribuye (`esc.error_estimacion_pct`),
    que es la incertidumbre que de verdad tiene sobre ella.

    `btm` debe ser el BTM ESTIMADO, no el real: estos escenarios son los que
    el operador se plantea, no la verdad.

    Las semillas arrancan en +1000 a proposito, para que ningun escenario
    coincida con el realizado. Antes el desplazamiento era 100*s, asi que el
    escenario 0 usaba `esc.semilla`, la misma que la caida que de verdad
    ocurre: el conjunto contenia el caso real y no habia robustez que medir.
    Tampoco puede chocar con +1 ni +7, que usan los estimadores.
    """
    carga = network.loads.groupby('bus').p_set.sum()
    carga = carga[carga > 0]
    piso = carga.round(3)

    sigma = esc.error_estimacion_pct / 100.0
    filas = {}
    for s in range(n_escenarios):
        semilla = esc.semilla + 1000 + 100 * s
        e = EscenarioBTM(**{**esc.__dict__, 'semilla': semilla})

        # el operador no sabe si su propia estimacion se queda corta o larga:
        # cada escenario la perturba con la desviacion que le atribuye
        rng = np.random.default_rng(semilla + 13)
        btm_s = (btm * (1.0 + rng.normal(0.0, sigma, len(btm)))).clip(lower=0.0)

        _, resto = aplicar_caida_btm(btm_s, e, frac_inversor)
        # carga real = lo medido + el BTM instalado (el contador ya lo resta)
        real = carga + btm_s.reindex(carga.index).fillna(0.0)
        filas[f's{s}'] = (real - resto.reindex(carga.index).fillna(0.0)).round(3)
    return pd.DataFrame(filas), piso


def milp_asignacion(alivio, btm_resto, objetivos, lam=LAMBDA_BTM,
                    secuencial=True, limite_s=30, verbose=False,
                    alivio_piso=None):
    """Asigna cada barra a un escalon del EDAC.

    Por defecto usa el MODELO SECUENCIAL que propone Arenas et al.: en vez de
    decidir las 8 asignaciones a la vez, resuelve un subproblema por escalon y
    va fijando lo ya asignado. El modelo completo con 69 barras y 8 escalones
    son 552 binarias y no cerraba el gap en 120 s; cada subproblema secuencial
    se resuelve en menos de un segundo y el propio paper muestra que la calidad
    de la solucion es comparable.

    Con secuencial=False se resuelve el modelo completo, con limite de tiempo.

    EL DESACOPLE DE DAS, Y LO QUE CUESTA FUERA DE TIEMPO REAL
    ----------------------------------------------------------
    `alivio_piso` es el alivio que se acredita en la restriccion de
    SUFICIENCIA (el lado ">="). `alivio` es el alivio esperado en cada
    escenario y gobierna la de NO PASARSE (el lado "<=").

    En Das et al. el alivio que entra en la restriccion es el flujo NETO
    MEDIDO en cabecera, un dato exacto, y la estimacion de generacion
    distribuida entra unicamente en el score de la funcion objetivo. Por eso
    puede afirmar que un error de estimacion "solo introduce error en la
    priorizacion de alimentadores": nunca deja el deslastre corto.

    Aqui se planifica por adelantado y el alivio NO se puede medir, asi que
    esa garantia no sale gratis. Hay tres varas posibles y cada una compra una
    cosa distinta:

      - `None`  -> se exige sobre el alivio estimado. Es lo que habia. Sin
        sesgo sistematico, pero un estimador optimista deja el corte corto,
        que es el fallo peligroso.
      - alivio MINIMO sobre los escenarios -> robustez de peor caso al estilo
        clasico. No protege contra un error mayor que el rango de escenarios,
        pero tampoco tira toda la contribucion del BTM.
      - carga MEDIDA (el piso absoluto) -> la garantia literal de Das: el BTM
        que se caiga solo puede sumar, porque al desconectarse la barra pasa a
        demandar mas de la red. Ningun error del estimador puede dejar el
        corte corto. A cambio paga un SOBRECORTE sistematico del orden de la
        penetracion BTM, porque el objetivo se dimensiona sobre carga bruta y
        el alivio se acredita sobre la neta.

    El sobrecorte es el lado seguro del error ---cuesta energia no suministrada,
    no un colapso--- y queda acotado por el termino `dn` de la funcion
    objetivo, pero no es despreciable y hay que declararlo.

    `objetivos` es la lista de objetivos ACUMULADOS por escalon. Puede darse
    tambien como DataFrame escalon x escenario, si el objetivo depende del
    escenario ---que es el caso cuando se dimensiona sobre carga bruta, porque
    cada escenario supone una caida de BTM distinta---.

    Devuelve un DataFrame barra x escalon con la asignacion binaria.
    """
    import contextlib
    import io

    import linopy

    def _resolver(modelo):
        # linopy escribe su resumen por logging y por stderr; con 8
        # subproblemas secuenciales son 8 bloques que tapan la salida util.
        import logging as _lg
        lg = _lg.getLogger('linopy')
        antes = lg.level
        lg.setLevel(_lg.ERROR)
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                modelo.solve(solver_name='highs', output_flag=False,
                             time_limit=limite_s)
        finally:
            lg.setLevel(antes)

    buses = list(alivio.index)
    escen = list(alivio.columns)
    resto = btm_resto.reindex(buses).fillna(0.0)

    # objetivo por (escalon, escenario). Si viene una lista, vale para todos.
    if isinstance(objetivos, pd.DataFrame):
        obj = objetivos.reindex(columns=escen)
    else:
        obj = pd.DataFrame({sc: list(objetivos) for sc in escen})
    pasos = list(range(len(obj)))

    # Alivio garantizado: el que se exige en la restriccion de suficiencia.
    # Sin el, se cae al comportamiento anterior y se exige sobre el estimado.
    piso = (alivio_piso.reindex(buses).fillna(0.0) if alivio_piso is not None
            else None)

    if not secuencial:
        m = linopy.Model()
        x = m.add_variables(binary=True, coords=[buses, pasos], name='x')
        up = m.add_variables(lower=0.0, coords=[escen, pasos], name='up')
        dn = m.add_variables(lower=0.0, coords=[escen, pasos], name='dn')
        m.add_constraints(x.sum(dims='dim_1') <= 1, name='una_vez')
        for j in pasos:
            acum = [jj for jj in pasos if jj <= j]
            sel_acum = x.sel(dim_1=acum).sum(dims='dim_1')
            for sc in escen:
                a = alivio[sc].reindex(buses).fillna(0.0).values
                e = (sel_acum * a).sum()
                # suficiencia contra el piso garantizado (Das); si no se pasa
                # piso, se exige sobre el alivio estimado como antes
                e_min = e if piso is None else (sel_acum * piso.values).sum()
                m.add_constraints(
                    e_min >= obj.at[j, sc] - up.sel(dim_0=sc, dim_1=j),
                    name=f'mn_{sc}_{j}')
                m.add_constraints(
                    e <= obj.at[j, sc] + dn.sel(dim_0=sc, dim_1=j),
                    name=f'mx_{sc}_{j}')
        pen = (x.sum(dims='dim_1') * resto.values).sum()
        m.add_objective(up.sum() + dn.sum() + lam * pen)
        _resolver(m)
        sol = x.solution.to_pandas()
        sol.index.name = 'bus'
        return sol.round(0).astype(int)

    # --- modelo secuencial -------------------------------------------------
    asign = pd.DataFrame(0, index=buses, columns=pasos)
    disponibles = list(buses)
    # lo ya comprometido por escalones anteriores, medido con la misma vara
    # que la restriccion: el piso si se exige sobre el piso, y el alivio
    # mediano si se exige sobre el estimado.
    acumulado = 0.0
    if verbose:
        # Aviso explicito: esto es el PLAN del EDAC ---que barras cuelga de
        # cada escalon, se dispare o no---, no lo que dispara en este evento.
        # Sin la cabecera, las ocho lineas caian justo debajo de "escalones
        # disparados: 2 de 8" y parecian contradecirla: sumaban ~3.640 MW
        # frente a los 909,6 que de verdad se botan.
        print(f"\n--- Plan del EDAC (MILP): reparto de barras entre los "
              f"{len(pasos)} escalones ---")
        print("     (asignacion completa; cuales llegan a disparar lo decide "
              "la frecuencia)")
    # vara con la que se mide lo conseguido: la misma que la restriccion
    vara = piso if piso is not None else alivio.median(axis=1)
    for j in pasos:
        # objetivo incremental por escenario
        incr = {sc: float(obj.at[j, sc]) - acumulado for sc in escen}
        if max(incr.values()) <= 0 or not disponibles:
            continue
        m = linopy.Model()
        y = m.add_variables(binary=True, coords=[disponibles], name='y')
        up = m.add_variables(lower=0.0, coords=[escen], name='up')
        dn = m.add_variables(lower=0.0, coords=[escen], name='dn')
        e_min = (y * vara.reindex(disponibles).fillna(0.0).values).sum()
        for sc in escen:
            a = alivio[sc].reindex(disponibles).fillna(0.0).values
            e = (y * a).sum()
            m.add_constraints(e_min >= incr[sc] - up.sel(dim_0=sc),
                              name=f'mn_{sc}')
            m.add_constraints(e <= incr[sc] + dn.sel(dim_0=sc), name=f'mx_{sc}')
        pen = (y * resto.reindex(disponibles).fillna(0.0).values).sum()
        m.add_objective(up.sum() + dn.sum() + lam * pen)
        _resolver(m)
        sel = y.solution.to_pandas().round(0).astype(int)
        elegidas = [b for b in disponibles if sel.get(b, 0) == 1]
        for b in elegidas:
            asign.loc[b, j] = 1
        logrado = float(vara.reindex(elegidas).fillna(0.0).sum())
        acumulado += logrado
        disponibles = [b for b in disponibles if b not in set(elegidas)]
        if verbose:
            print(f"     escalon {j+1}: {len(elegidas)} barras, objetivo "
                  f"{np.mean(list(incr.values())):.0f} MW, conseguido "
                  f"{logrado:.0f} MW")
    asign.index.name = 'bus'
    return asign


def deslastre_desde_asignacion(asignacion, alivio_real, escalones_disparados):
    """MW a botar en cada barra, segun los escalones que hayan disparado."""
    activos = [j for j, e in enumerate(escalones_disparados) if e['disparado']]
    if not activos:
        return pd.Series(dtype=float)
    sel = asignacion[activos].sum(axis=1) > 0
    buses = asignacion.index[sel]
    return alivio_real.reindex(buses).dropna().clip(lower=0.0).round(3)


# ---------------------------------------------------------------------------
# 5. Escenario completo
# ---------------------------------------------------------------------------

ROBUSTEZ = ('estimado', 'peor_escenario', 'piso')


def ejecutar(esc, verbose=True, usar_milp=True, spec=None,
             solar_pu=0.0, con_cascada=True, robustez='peor_escenario',
             lam=LAMBDA_BTM, base=None):
    """`base` permite inyectar un punto de operacion ya construido.

    Por omision se usa el caso de punta nocturna. Pasar la red que devuelve
    `escenario_diurno.construir()` corre el mismo analisis sobre el punto
    diurno, que es donde el evento que se estudia puede ocurrir de verdad:
    en la punta nocturna la fotovoltaica entrega cero y no hay nada que
    desconectar.
    """
    from stn import contingencias as C
    from stn.zonas import area_por_bus

    if base is None:
        base, _ = C.caso_base(verbose=False)
    sis = Sistema(base)
    mapa = area_por_bus(base)

    btm_placa = repartir_btm(base, esc, mapa, por=esc.reparto)
    # solo la parte conectada por inversor responde al modo de fallo que se
    # estudia; ver aplicar_caida_btm(). La granularidad TIENE que ser la misma
    # que la del reparto, por eso sale del escenario y no de un defecto.
    f_inv_placa = fraccion_inversor(esc, mapa, btm_placa.index, por=esc.reparto)
    # y lo que se desconecta es lo que estaba PRODUCIENDO, no la placa
    btm, f_inv = btm_produciendo(btm_placa, esc, f_inv_placa)
    caido, resto = aplicar_caida_btm(btm, esc, f_inv)
    area_de = {b: mapa.get(b, ('', 'Sin area'))[1] for b in btm.index}
    caido_area = caido.groupby(area_de).sum()
    btm_area = btm.groupby(area_de).sum()

    deficit = float(caido.sum()) + esc.perdida_gen_mw

    if verbose:
        print("\n--- Escenario BTM ---")
        if esc.fuente == 'sintetico':
            print(f"  fuente                 : penetracion supuesta de "
                  f"{esc.penetracion_pct:.1f}%")
        else:
            _, desglose = cargar_registrada(esc.fuente)
            det = ', '.join(f'{k} {v:.1f} MW' for k, v in desglose.items())
            print(f"  fuente                 : PARATEC ({esc.fuente}) -> {det}")
        pen_real = 100 * btm.sum() / max(float(base.loads.p_set.sum()), 1e-9)
        print(f"  penetracion resultante : {pen_real:.2f}% de la demanda"
              + (f"  (solo {', '.join(esc.areas)})" if esc.areas else "  (todas las areas)"))
        print(f"  BTM instalado (placa)  : {btm_placa.sum():9.1f} MW en "
              f"{len(btm_placa)} barras")
        if abs(esc.produccion_inversor_pu - 1.0) > 1e-9:
            print(f"  BTM PRODUCIENDO        : {btm.sum():9.1f} MW  "
                  f"(inversor al {esc.produccion_inversor_pu*100:.1f}% de su "
                  f"placa a esta hora)")
        else:
            print("  [!] se supone el BTM produciendo al 100% de su placa; "
                  "cierto solo a mediodia")
        if esc.fuente != 'sintetico':
            det_tec = cargar_registrada_detalle(esc.fuente)
            if len(det_tec):
                mix = det_tec.groupby('tec').MW.sum().sort_values(ascending=False)
                tot = float(mix.sum())
                txt = ', '.join(f'{t} {100*v/tot:.0f}%' for t, v in mix.items())
                print(f"     mezcla tecnologica  : {txt}")
                inv_mw = float((btm * f_inv.reindex(btm.index).fillna(0.0)).sum())
                print(f"     con inversor        : {inv_mw:9.1f} MW "
                      f"(lo unico que responde al hueco de tension)")
        print(f"  caida                  : {esc.caida_pct:.0f}% del inversor"
              + (f" y {esc.caida_sincrona_pct:.0f}% de lo sincrono"
                 if esc.caida_sincrona_pct else "")
              + f"  ->  {caido.sum():.1f} MW se desconectan de golpe")
        if esc.perdida_gen_mw:
            print(f"  perdida convencional   : {esc.perdida_gen_mw:9.1f} MW")
        print(f"  DEFICIT TOTAL          : {deficit:9.1f} MW "
              f"({100*deficit/max(sis.P_gen,1e-9):.1f}% del despacho)")
        print("\n  reparto por area:")
        for a in sorted(btm_area.index):
            print(f"     {a:16s} BTM {btm_area[a]:8.1f} MW   se cae "
                  f"{caido_area.get(a,0.0):8.1f} MW")

    # --- lo que ve el operador ------------------------------------------
    bias = bias_por_area(base, mapa, sis)

    # Secuencia de perturbaciones. Sin escenario se mantiene el comportamiento
    # de siempre: todo el deficit en t=0. Con escenario, cada evento lleva su
    # instante, que es lo que decide que escalones del EDAC llegan a disparar.
    perturbaciones, disparos, cambios_ev = None, None, None
    if spec is not None:
        from stn import eventos as EV
        perturbaciones, cambios_ev = EV.construir(
            base, spec, mapa, sis, btm, caido)
        if verbose:
            print("\n--- Secuencia del evento ---")
            for l in EV.resumen(perturbaciones):
                print(l)
    if con_cascada:
        # Cascada endogena: la solar se desconecta sola al cruzar el ajuste de
        # proteccion que PARATEC declara para cada planta. Solo actua si hay
        # solar produciendo; en la punta de demanda que reproduce el modelo la
        # salida solar es cero y esta curva no dispara nada.
        from stn import eventos as EV
        disparos = [dict(d, mw=d['mw'] * float(solar_pu))
                    for d in EV.curva_disparo_solar()] if solar_pu > 0 else None

        # Cascada endogena del BTM: la generacion embebida que seguia SANA se
        # desconecta tambien si la frecuencia baja de su ajuste. Ver
        # CASCADA_BTM_HZ para el porque de que este apagado por omision y
        # cuanto cambia el resultado al encenderlo.
        if CASCADA_BTM_HZ is not None:
            sano = float(resto.sum())
            extra = EV.curva_disparo_btm(sano, f_umbral=float(CASCADA_BTM_HZ))
            if extra:
                disparos = (disparos or []) + extra

    if perturbaciones is not None:
        t, f, escalones, traza = simular_frecuencia(
            sis, con_edac=True, eventos=perturbaciones, disparos=disparos)
        deficit = traza['perturbacion_total']
    else:
        # El BTM que se cae entra por `dP_carga`, NO por `dP_gen`: esta
        # neteado por debajo de 200 kV, asi que el operador no lo ve
        # desaparecer de la generacion sino aparecer como mas demanda. Es lo
        # que dice el docstring de `simular_frecuencia`, lo que hace
        # `cascada.py` y lo que hace este mismo modulo unas lineas mas abajo
        # al pasar `carga_extra=caido` a `aplicar_evento`.
        #
        # La distincion no es cosmetica: el escalon del EDAC se dimensiona
        # como P_carga x EDAC_FRACCION, asi que meterlo como generacion
        # perdida dejaba la demanda base sin actualizar y el deslastre salia
        # corto ---101,5 MW, un 11,2%, para un deficit de 1.015 MW---.
        eventos_t0 = []
        if float(caido.sum()) > 1e-9:
            eventos_t0.append({'t': 0.0, 'dP_gen': 0.0,
                               'dP_carga': float(caido.sum()),
                               'etiqueta': 'caida del BTM (aparece como carga)'})
        if esc.perdida_gen_mw:
            eventos_t0.append({'t': 0.0, 'dP_gen': float(esc.perdida_gen_mw),
                               'dP_carga': 0.0,
                               'etiqueta': 'perdida de generacion convencional'})
        t, f, escalones, traza = simular_frecuencia(
            sis, con_edac=True, eventos=eventos_t0, disparos=disparos)
    if verbose and traza.get('mw_autodisparo', 0) > 0.1:
        # Puede ser solar de gran escala (ajuste REAL de PARATEC), BTM sano
        # (ajuste SUPUESTO, ver CASCADA_BTM_HZ), o ambos. Decirlo, porque la
        # calidad del dato detras de cada uno no es la misma.
        quien = []
        if solar_pu > 0:
            quien.append('solar de gran escala')
        if CASCADA_BTM_HZ is not None:
            quien.append('BTM sano (ajuste supuesto)')
        print(f"\n  cascada endogena: se desconectan "
              f"{traza['mw_autodisparo']:.0f} MW de "
              f"{' y '.join(quien) or 'generacion'} por subfrecuencia")

    # El ACE se evalua con la desviacion de frecuencia de REGIMEN PERMANENTE,
    # no con la del nadir. La formula ACE = dP_tie + B*df sale del equilibrio
    # df = -dP/B, y solo ahi es consistente: con ese df la identidad cierra
    # exacta (suma de ACE = -desbalance, error 0,1 MW sobre 1321). Usando el
    # nadir, que es un transitorio 4,75 veces mas profundo, la columna ACE
    # salia sobreestimada en la misma proporcion: -6282 MW para un desbalance
    # real de 1321. El diagnostico cualitativo no cambiaba, porque dP_tie no
    # depende de df, pero las cifras no eran un ACE.
    df_regimen = -float(caido_area.sum()) / max(float(bias.sum()), 1e-9)
    diag, total = diagnostico_ace(caido_area, bias, df_regimen)

    if verbose:
        print("\n--- Lo que ve el operador: ACE por area ---")
        print(diag.to_string())
        n_ciegas = int((~diag['localiza']).sum())
        print(f"\n  desbalance real del sistema      : {total:9.1f} MW")
        print(f"  mayor desviacion de intercambio  : "
              f"{diag.dP_tie_MW.abs().max():9.1f} MW")
        print(f"  areas que NO localizan el problema por sus intercambios "
              f"(|dP_tie| < {UMBRAL_ACE:.0f} MW): {n_ciegas} de {len(diag)}")
        if n_ciegas == len(diag):
            print("  >> ninguna area ve nada raro en sus intercambios. Lo unico "
                  "que avisa es la frecuencia,")
            print("     que es la misma en todo el sistema y no dice DONDE esta "
                  "el problema.")
        # contraste: el mismo deficit concentrado en un area
        concentrada = diag.deficit_real_MW.idxmax()
        conc = pd.Series(0.0, index=diag.index)
        conc[concentrada] = total
        tie_conc = desviacion_intercambio(conc, bias)
        print(f"\n  Contraste: si esos mismos {total:.0f} MW se hubieran perdido "
              f"solo en {concentrada},")
        print(f"  su desviacion de intercambio seria de "
              f"{abs(tie_conc[concentrada]):.0f} MW en vez de "
              f"{abs(diag.dP_tie_MW[concentrada]):.0f} MW: el ACE la senalaria "
              f"de inmediato.")

    # --- estimacion del BTM ----------------------------------------------
    # El error que se reporta tiene que ser el del estimador que DECIDE
    # (`estimar_btm`, el que alimenta al MILP), no el de otro distinto. Habia
    # aqui una llamada a `estimar_btm_perdido`, que usa otra semilla (+1 en vez
    # de +7) y error independiente por barra en vez de correlado espacialmente:
    # se publicaba un "error de estimacion" que no habia movido ninguna
    # decision.
    estimado = estimar_btm(caido, esc, mapa_areas=mapa)
    if verbose:
        err = 100 * (estimado.sum() - caido.sum()) / max(caido.sum(), 1e-9)
        print("\n--- Estimacion del BTM perdido ---")
        print(f"  real {caido.sum():.1f} MW | estimado {estimado.sum():.1f} MW "
              f"| error {err:+.1f}%   (mismo estimador que alimenta al MILP)")

    # --- frecuencia --------------------------------------------------------
    if verbose:
        print("\n--- Dinamica de frecuencia ---")
        print(f"  RoCoF inicial          : {traza['rocof']:9.3f} Hz/s")
        print(f"  frecuencia minima      : {traza['nadir']:9.3f} Hz a "
              f"{traza['t_nadir']:.2f} s")
        print(f"  frecuencia final       : {traza['f_final']:9.3f} Hz")
        if traza['colapso']:
            print(f"  COLAPSO por debajo de {F_COLAPSO} Hz")
        disp = [(j, e) for j, e in enumerate(escalones) if e['disparado']]
        print(f"  escalones disparados   : {len(disp)} de {len(escalones)}  "
              f"->  {traza['deslastre_total']:.1f} MW a botar")
        for j, e in disp:
            print(f"     escalon {j+1}  ajuste {e['umbral']:.2f} Hz  "
                  f"via {e['via']:6s}  t={e['t_disparo']:.2f} s  "
                  f"{e['mw']:7.1f} MW")

    # --- reparto del deslastre --------------------------------------------
    carga = base.loads.groupby('bus').p_set.sum()
    carga = carga[carga > 0]
    alivio_real = (carga + btm.reindex(carga.index).fillna(0.0)
                   - resto.reindex(carga.index).fillna(0.0))

    origen = 'proporcional'
    if usar_milp:
        try:
            # Se DECIDE sobre lo que el operador cree, no sobre la verdad.
            # `alivio_real` (arriba) es la evaluacion y sigue usando el BTM
            # verdadero: es lo que de verdad pasa al abrir el interruptor.
            btm_est = estimar_btm(btm, esc, mapa_areas=mapa)
            _, resto_est = aplicar_caida_btm(btm_est, esc, f_inv)
            alv, piso = escenarios_alivio(base, btm_est, esc,
                                          frac_inversor=f_inv)

            # Objetivo de cada escalon, segun Das et al. ec. (3):
            #
            #     C_shed(k) = A_k * consumo BRUTO
            #
            # no sobre la lectura del contador. Es la asimetria deliberada de
            # Das ---lado derecho bruto, lado izquierdo neto--- y aqui importa
            # el doble: el rele mide la carga del alimentador EN EL INSTANTE
            # DEL DISPARO, cuando el BTM ya se cayo y la barra demanda mas. La
            # suma de `alv[sc]` es exactamente esa carga vigente en el
            # escenario sc, asi que el objetivo se dimensiona con la misma
            # vara con la que el EDAC va a cortar.
            #
            # Antes se usaba `base.loads.p_set.sum()`, la carga NETA previa al
            # evento: el plan se dimensionaba sobre 585,8 MW por escalon
            # mientras el rele botaba 634,4. Ni Das ni Arenas congelan este
            # valor; Arenas lo recalcula para cada una de las 720 horas de su
            # muestra.
            objetivos = pd.DataFrame(
                {sc: [float(alv[sc].sum()) * EDAC_FRACCION * (j + 1)
                      for j in range(len(EDAC_UMBRALES_HZ))]
                 for sc in alv.columns})

            # Con que vara se exige la suficiencia (ver milp_asignacion)
            vara = {'estimado': None,
                    'peor_escenario': alv.min(axis=1),
                    'piso': piso}[robustez]

            asign = milp_asignacion(alv, resto_est, objetivos, lam=lam,
                                    secuencial=True, verbose=verbose,
                                    alivio_piso=vara)

            # GUARDA: un lambda demasiado alto hace que al MILP le salga mas
            # barato no deslastrar que tocar barras con BTM sano, y el plan
            # sale vacio o ridiculo SIN LANZAR NINGUN ERROR. Es un fallo
            # silencioso y peligroso ---el esquema deja de proteger--- asi que
            # se comprueba explicitamente. Ver la nota de LAMBDA_BTM.
            vara_chk = vara if vara is not None else alv.median(axis=1)
            plan = float(vara_chk.reindex(
                asign.index[asign.sum(axis=1) > 0]).fillna(0.0).sum())
            meta = float(objetivos.iloc[-1].mean())
            if meta > 0 and plan < 0.75 * meta:
                print(f"  AVISO: el plan del MILP solo cubre {100*plan/meta:.0f}% "
                      f"del objetivo del ultimo escalon ({plan:.0f} de "
                      f"{meta:.0f} MW).")
                print(f"         Con lambda={lam:g} la penalizacion por BTM sano "
                      f"domina y al optimizador le sale mas barato no cortar. "
                      f"Baja --lambda-btm.")
            deslastre = deslastre_desde_asignacion(asign, alivio_real, escalones)
            origen = (f'MILP BTM-aware, robustez={robustez} '
                      f'({int((asign.sum(axis=1) > 0).sum())} barras asignadas)')
        except Exception as e:                      # noqa: BLE001
            if verbose:
                print(f"  aviso: el MILP no resolvio ({type(e).__name__}: "
                      f"{str(e)[:70]}); se reparte proporcionalmente")
            deslastre = _proporcional(alivio_real, traza['deslastre_total'])
            origen = f'proporcional ({type(e).__name__})'
    else:
        deslastre = _proporcional(alivio_real, traza['deslastre_total'])

    # BTM sano destruido, ponderado por la fraccion que se bota de cada barra.
    # Ver btm_sano_perdido(): sin ponderar, al reparto proporcional se le
    # imputaba el BTM entero de barras de las que solo quita el 15 %.
    btm_sano_mw, btm_sano_barra = btm_sano_perdido(deslastre, resto, alivio_real)

    if verbose and len(deslastre):
        print(f"\n--- Deslastre ({origen}) ---")
        print(f"  {len(deslastre)} barras, {deslastre.sum():.1f} MW de alivio neto")
        top = deslastre.sort_values(ascending=False).head(8)
        for b, v in top.items():
            disp = float(alivio_real.get(b, 0.0))
            pct = 100 * v / disp if disp > 1e-9 else 0.0
            print(f"     {b:30s} {v:8.1f} MW ({pct:3.0f}% de la barra)"
                  f"   BTM sano que se lleva: {btm_sano_barra.get(b, 0.0):6.1f} MW")
        print(f"  BTM sano desconectado en total: {btm_sano_mw:.1f} MW")

    # --- estado final -------------------------------------------------------
    # El BTM que cae entra como MAS CARGA en las barras donde estaba, que es
    # como lo ve el operador; la perdida de generacion convencional entra como
    # deficit a prorrata. Antes no entraba ninguna de las dos y el slack tapaba
    # el hueco absorbiendo GW enteros.
    #
    # CON ESCENARIO HAY QUE PASAR LOS CAMBIOS DEL ESCENARIO, no los del
    # escenario BTM simple. `EV.construir()` devuelve que generadores salen,
    # cuanto deficit total hay y que carga extra aparece; durante un tiempo se
    # calculaban y se tiraban, y el estado AC se montaba con la caida de BTM a
    # secas. Con `iberia` eso significaba simular la frecuencia sobre una
    # perturbacion de 4.412,7 MW y luego resolver el flujo sobre una red a la
    # que solo se le habian quitado 528,9 MW de carga, sin retirar ni uno de
    # los cinco generadores que el escenario desconecta: el slack absorbia la
    # diferencia y el informe de sobrecargas describia una red que no existe.
    #
    # Es exactamente el fallo que el docstring de `aplicar_evento` dice haber
    # corregido, reaparecido por otra via.
    # Lo que se autodisparo por subfrecuencia tambien es carga extra para el
    # flujo AC: la frecuencia lo vio desconectarse y la red tiene que verlo
    # igual. Ver autodisparo_por_bus().
    auto = autodisparo_por_bus(traza, base, mapa, resto, solar_pu=solar_pu)

    if cambios_ev is not None:
        fuera = cambios_ev.get('generadores_fuera', [])
        extra = cambios_ev.get('carga_extra')
        if extra is None or not len(extra):
            extra = caido
        extra = extra.add(auto, fill_value=0.0) if len(auto) else extra
        n, ok = aplicar_evento(base, fuera, deslastre, sis,
                               deficit_mw=float(cambios_ev.get('deficit_mw', 0.0)),
                               carga_extra=extra, verbose=False)
    else:
        extra = caido.add(auto, fill_value=0.0) if len(auto) else caido
        n, ok = aplicar_evento(base, [], deslastre, sis,
                               deficit_mw=esc.perdida_gen_mw, carga_extra=extra,
                               verbose=False)
    est = estado_red(n, ok)
    if verbose:
        print("\n--- Estado post-evento (flujo AC) ---")
        if not est['convergio']:
            print("  el flujo no converge")
        else:
            print(f"  demanda servida        : {est['demanda_MW']:9.1f} MW")
            print(f"  tension                : [{est['v_min']}, {est['v_max']}] p.u."
                  f"  fuera de rango: {est['n_v_fuera']}")
            print(f"  cargabilidad maxima    : {est['carga_max_pct']}%  "
                  f"sobrecargas: {est['n_sobrecargas']}")

    return {'traza': traza, 'diag_ace': diag, 'deficit': deficit,
            'deslastre': deslastre, 'origen': origen, 'estado': est,
            'btm': btm, 'caido': caido, 'resto': resto, 't': t, 'f': f,
            'alivio_real': alivio_real,
            'btm_sano_mw': btm_sano_mw, 'btm_sano_barra': btm_sano_barra,
            # La red POST-EVENTO y la carga que se le añadio. No son adorno:
            # son el unico observable que permite comprobar este modulo sin
            # reusar sus propias Series.
            #
            # `aplicar_evento` recorta con `max(0, 1 - mw/total)`, asi que si
            # el plan pide botar mas de lo que una barra tiene, el exceso se
            # pierde EN SILENCIO: la red no queda con carga negativa, queda a
            # cero, y el deslastre realizado es menor que el anunciado. La
            # dinamica de frecuencia, en cambio, ya se creyo el anunciado.
            # Contrastar `red_post` contra `deslastre` es lo que detecta ese
            # desacuerdo; compararlo contra `alivio_real` no, porque
            # `deslastre` ES `alivio_real` en las barras que el MILP abre.
            'red_post': n, 'carga_extra': extra,
            'carga_pre': base.loads.groupby('bus').p_set.sum()}


def btm_sano_perdido(deslastre, resto, alivio_real):
    """Generacion embebida SANA que se lleva por delante un plan de deslastre.

    Devuelve (total_MW, por_barra).

    HAY QUE PONDERAR POR LA FRACCION QUE SE BOTA DE CADA BARRA, y durante un
    tiempo no se hacia. La version anterior sumaba `resto` entero de toda barra
    que apareciese en el plan:

        resto.reindex(deslastre.index).fillna(0).sum()

    Para el MILP esa cuenta es correcta por casualidad, porque abre la barra
    ENTERA: su fraccion es 1,0 y ponderar no cambia nada. Para el reparto
    proporcional es falsa, porque ese metodo quita un trozo de cada barra ---en
    el caso de referencia, el 15 %--- y aun asi se le imputaba el 100 % del BTM
    sano de las 82 barras que toca.

    El efecto sobre la comparacion que es el resultado del trabajo:

        metodo          barras   alivio    BTM sano (antes)   BTM sano (real)
        MILP BTM-aware      18   2.131,9         2,1 MW             2,1 MW
        proporcional        82   2.043,9       254,1 MW            38,1 MW

    Ojo al citar la mejora: esa fila del MILP es UNA semilla, y resulta ser la
    mas favorable de ocho. Promediando, el MILP destruye 8,7 MW frente a los
    38,1 del proporcional, o sea **4,4x**, con un rango de 1,7x a 20,6x segun
    donde caiga el reparto aleatorio del BTM. La cifra de 121x que se publicaba
    combinaba esta metrica inflada con la semilla mas favorable.

    `alivio_real` es la carga disponible en cada barra DESPUES de que el BTM se
    cayera (carga + caido): es el denominador correcto, porque lo que el EDAC
    abre son alimentadores que llevan a la vez carga bruta y el BTM que sigue
    vivo colgado de ellos.
    """
    import pandas as pd
    if not len(deslastre):
        return 0.0, pd.Series(dtype=float)
    d = deslastre
    disp = alivio_real.reindex(d.index)
    frac = (d / disp.where(disp > 1e-9)).clip(upper=1.0).fillna(0.0)
    por_barra = resto.reindex(d.index).fillna(0.0) * frac
    return float(por_barra.sum()), por_barra


def _proporcional(alivio, mw):
    pos = alivio[alivio > 0]
    if not len(pos) or mw <= 0:
        return pd.Series(dtype=float)
    return (pos / pos.sum() * min(mw, pos.sum())).round(3)


def main():
    ap = argparse.ArgumentParser(
        description="Deslastre optimo por baja frecuencia con BTM, multi-area.")
    ap.add_argument('--fuente',
                    choices=['agpe', 'agpe_gd', 'sintetico',
                             'agpe_paratec', 'menores', 'ambas'],
                    default='agpe',
                    help="de donde sale el BTM. 'agpe' (defecto) es el caso "
                         "base: AGPE real de la UPME, 740,83 MW. 'agpe_gd' "
                         "anade generacion distribuida (1.346,78 MW) como "
                         "sensibilidad. 'sintetico' es penetracion supuesta, "
                         "para escenarios de futuro. Las otras tres solo "
                         "sirven para reproducir cifras anteriores al "
                         "31-10-2026 y NO describen BTM")
    ap.add_argument('--btm', type=float, default=BTM_POR_DEFECTO,
                    help="penetracion supuesta en %% (solo con --fuente sintetico)")
    ap.add_argument('--caida', type=float, default=60.0,
                    help="%% del BTM que se desconecta de golpe")
    ap.add_argument('--areas', nargs='+', default=None,
                    help="areas afectadas; por defecto todas")
    ap.add_argument('--perder-gen', type=float, default=0.0,
                    help="perdida convencional adicional, en MW")
    ap.add_argument('--error-estimacion', type=float,
                    default=100.0*(ERROR_DESAGREGACION**2 + ERROR_CAPACIDAD**2)**0.5,
                    help="error del estimador de BTM en %%; por defecto 24,5, "
                         "compuesto de 15,5%% de desagregacion (Applied Energy "
                         "2025) y 19%% del registro de capacidad")
    ap.add_argument('--comparar', action='store_true',
                    help="MILP con BTM frente a reparto proporcional")
    ap.add_argument('--sin-milp', action='store_true',
                    help="solo reparto proporcional, sin el MILP")
    ap.add_argument('--robustez', choices=ROBUSTEZ, default='peor_escenario',
                    help="con que vara se exige la suficiencia del deslastre: "
                         "'estimado' (alivio estimado, sin proteccion contra "
                         "un estimador optimista), 'peor_escenario' (robustez "
                         "de peor caso sobre los escenarios) o 'piso' (la "
                         "garantia literal de Das: solo se acredita la carga "
                         "medida, nunca corta de menos, pero sobrecorta)")
    ap.add_argument('--lambda-btm', type=float, default=LAMBDA_BTM,
                    help="peso de la penalizacion por llevarse BTM sano. "
                         "Antes era inalcanzable desde la linea de ordenes: "
                         "`lam=LAMBDA_BTM` es un valor por defecto que se fija "
                         "al DEFINIR la funcion, asi que reasignar la global "
                         "no lo cambiaba y ejecutar() nunca lo pasaba")
    ap.add_argument('--escenario', choices=['simple','btm','cascada','iberia'],
                    default=None,
                    help="tipo de evento; sin esto, la caida de BTM en t=0")
    ap.add_argument('--escala', type=float, default=0.45,
                    help="tamano relativo del evento 'iberia' (0-1)")
    ap.add_argument('--solar-salida', type=float, default=0.0,
                    help="salida de la solar de gran escala en pu de su "
                         "capacidad (4.200 MW). 0 = punta nocturna, que es el "
                         "escenario del caso de referencia; sube a 0,5-0,7 "
                         "para un evento diurno")
    ap.add_argument('--cascada-btm', type=float, default=None,
                    metavar='HZ',
                    help="ajuste de subfrecuencia al que se dispara el BTM "
                         "sano, en Hz (tipico 58.5). Por omision no se "
                         "modela: no hay registro del ajuste real y seria un "
                         "supuesto. Ver CASCADA_BTM_HZ.")
    ap.add_argument('--sin-cascada', action='store_true',
                    help="desactiva el autodisparo de la solar por subfrecuencia")
    ap.add_argument('--semilla', type=int, default=0)
    ap.add_argument('--produccion-btm', type=float, default=None,
                    metavar='PU',
                    help="fraccion de la placa con inversor que esta "
                         "PRODUCIENDO. Por omision 1,0, que es falso salvo a "
                         "mediodia y es el supuesto de todo lo publicado hasta "
                         "ahora. Con --diurno se fija solo (0,769 a las "
                         "10-11 h). De noche seria 0: el evento no existe")
    ap.add_argument('--diurno', nargs='?', type=int, const=11, default=None,
                    metavar='HORA',
                    help="corre sobre el punto de operacion DIURNO en vez de "
                         "la punta nocturna. Sin valor usa la hora 11 "
                         "(10:00-11:00), la de maxima penetracion solar. "
                         "Es el unico punto en que el evento que se estudia "
                         "puede ocurrir: de noche la fotovoltaica da cero")
    # OJO con el nombre: `--fuente` (arriba) es la fuente del POOL BTM
    # (agpe/menores/ambas/sintetico). `--fecha` es la del PUNTO DE OPERACION.
    # Son cosas distintas y no se tocan: el BTM sigue saliendo de los ficheros
    # de PARATEC, porque el despacho de XM no lo ve ---las 536 plantas "No
    # Despachada Centralmente" aparecen TODAS en el dDEC, asi que lo que esta
    # detras del contador es invisible ahi por construccion---.
    ap.add_argument('--fecha', default=None, metavar='AAAA-MM-DD',
                    help="corre sobre el despacho y el pronostico de XM de esa "
                         "fecha. Incompatible con --diurno. El factor de "
                         "produccion del inversor se MIDE de esa fecha en vez "
                         "de heredarse de la tabla de escenario_diurno")
    ap.add_argument('--periodo', type=int, default=19, metavar='N',
                    help="periodo horario 1..24 de --fecha (defecto 19)")
    args = ap.parse_args()

    if args.fecha and args.diurno is not None:
        ap.error('--fecha y --diurno son dos puntos de operacion distintos; '
                 'elige uno')

    esc = EscenarioBTM(
        fuente=args.fuente,
        penetracion_pct=args.btm, caida_pct=args.caida,
        areas=tuple(args.areas) if args.areas else (),
        error_estimacion_pct=args.error_estimacion,
        semilla=args.semilla, perdida_gen_mw=args.perder_gen,
        produccion_inversor_pu=(args.produccion_btm
                                if args.produccion_btm is not None else 1.0))

    print("=" * 72)
    print(" DESLASTRE CON GENERACION DETRAS DEL CONTADOR (BTM)")
    print("=" * 72)

    # ejecutar() ya aceptaba usar_milp=False, pero no habia forma de pedirlo
    # desde la linea de ordenes: solo se alcanzaba dentro de --comparar.
    spec = None
    if args.escenario:
        from stn import eventos as EV
        spec = EV.ESCENARIOS[args.escenario](
            escala=args.escala, caida=args.caida/100.0,
            caida_btm=args.caida/100.0)

    if args.cascada_btm is not None:
        global CASCADA_BTM_HZ
        CASCADA_BTM_HZ = float(args.cascada_btm)
        print(f"  cascada endogena del BTM activada a {CASCADA_BTM_HZ:.2f} Hz "
              f"(ajuste SUPUESTO, no hay registro)")

    base = None
    solar_pu = args.solar_salida
    if args.fecha:
        from stn import contingencias as C
        from stn.fuentes.xm_snapshot import FuenteXM
        fuente = FuenteXM(args.fecha, args.periodo, verbose=False)
        base, _ = C.caso_base(verbose=False, fuente=fuente)
        cal = fuente.calidad()
        prod, placa_solar, pu = fuente.produccion_solar_pu()

        # Mismo criterio que con --diurno: el punto de operacion manda sobre
        # --solar-salida, porque contar las dos cosas seria contarla dos veces.
        if solar_pu and abs(solar_pu - pu) > 1e-6:
            print(f"  [!] --solar-salida {solar_pu:.3f} ignorado: el despacho "
                  f"de XM fija la solar en {pu:.3f} pu esa hora")
        solar_pu = pu
        # La produccion del inversor se MIDE de la fecha, en vez de suponer la
        # placa entera produciendo (que es falso salvo a mediodia) o heredar
        # la tabla de dos horas de escenario_diurno.
        if args.produccion_btm is None:
            esc.produccion_inversor_pu = float(pu)

        print(f"\n  PUNTO DE OPERACION: XM {args.fecha} periodo {args.periodo}")
        print(f"    demanda {cal.p_demanda_ubicada_mw:.1f} MW "
              f"({cal.cobertura_demanda_pct:.2f} % del pronostico)  |  "
              f"generacion {cal.p_despacho_ubicado_mw:.1f} MW "
              f"({cal.cobertura_despacho_pct:.2f} %)")
        print(f"    solar despachada {prod:.1f} MW de {placa_solar:.1f} de "
              f"placa = {pu:.4f} pu  ->  produccion del inversor BTM")
        if not cal.apto_para_frecuencia:
            print(f"    AVISO: {cal.p_demanda_no_ubicada_mw:.1f} MW de demanda "
                  f"sin ubicar; el EDAC dimensiona por debajo")
        if pu < 1e-6:
            print("    AVISO: la fotovoltaica da CERO a esa hora. El evento "
                  "que estudia este modulo no puede ocurrir; el BTM que cae "
                  "sera solo el no solar")
    elif args.diurno is not None:
        from stn import escenario_diurno as ED
        base, info_d = ED.construir(hora=args.diurno, verbose=False)
        # El punto diurno ya fija cuanta solar esta produciendo. Usar ademas
        # --solar-salida seria contarla dos veces, asi que manda el escenario.
        if solar_pu and abs(solar_pu - info_d['solar_pu']) > 1e-6:
            print(f"  [!] --solar-salida {solar_pu:.3f} ignorado: el escenario "
                  f"diurno ya fija la solar en {info_d['solar_pu']:.3f} pu")
        solar_pu = info_d['solar_pu']
        # El punto diurno sabe cuanto rinde la fotovoltaica a esa hora; si el
        # usuario no lo forzo a mano, el escenario BTM hereda ese factor en vez
        # de suponer la placa entera produciendo.
        if args.produccion_btm is None and info_d.get('produccion_solar_pu'):
            esc.produccion_inversor_pu = float(info_d['produccion_solar_pu'])
        print(f"\n  PUNTO DE OPERACION: diurno, {info_d['etiqueta']}")
        print(f"    demanda neta {info_d['carga_neta_MW']:.1f} MW  |  "
              f"solar {info_d['solar_produccion_MW']:.1f} MW  |  "
              f"H*S {info_d['sis'].H*info_d['sis'].S_base:.0f} MVA.s "
              f"(punta: {info_d['sis_punta'].H*info_d['sis_punta'].S_base:.0f})")

    r1 = ejecutar(esc, verbose=True, usar_milp=not args.sin_milp, spec=spec,
                  solar_pu=solar_pu, con_cascada=not args.sin_cascada,
                  robustez=args.robustez, lam=args.lambda_btm, base=base)
    if args.comparar:
        print("\n" + "=" * 72)
        print(" Referencia: mismo evento con reparto proporcional")
        print("=" * 72)
        r2 = ejecutar(esc, verbose=True, usar_milp=False, spec=spec,
                      solar_pu=solar_pu,
                      con_cascada=not args.sin_cascada, base=base)
        print("\n" + "=" * 72)
        print(" COMPARACION")
        print("=" * 72)
        filas = []
        for k, r in (('MILP BTM-aware', r1), ('proporcional', r2)):
            # `origen` y no la etiqueta: si el MILP cayo al reparto
            # proporcional, la tabla lo tiene que decir en vez de publicar
            # cifras proporcionales bajo el rotulo del MILP.
            cayo = k.startswith('MILP') and not r['origen'].startswith('MILP')
            filas.append({
                'metodo': (k + '  [!] cayo a proporcional') if cayo else k,
                'barras': len(r['deslastre']),
                'alivio_MW': round(float(r['deslastre'].sum()), 1),
                'frac_barra': round(float(
                    (r['deslastre'] / r['alivio_real'].reindex(r['deslastre'].index)
                     ).clip(upper=1.0).mean()), 3) if len(r['deslastre']) else 0.0,
                'BTM_sano_perdido_MW': round(r['btm_sano_mw'], 1),
                'AC_ok': r['estado']['convergio'],
                'carga_max_%': r['estado'].get('carga_max_pct', '-'),
            })
        print(pd.DataFrame(filas).to_string(index=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
