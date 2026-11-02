# cascada.py
"""Motor de cascada: el bucle que convierte una contingencia en un apagon.

QUE HACE Y POR QUE HACE FALTA
-----------------------------
El barrido N-1 de `contingencias.py` responde a "si se cae este elemento,
aguanta el sistema?". Un apagon real no es eso. En Espana el 28 de abril de
2025 ningun elemento individual habria tumbado el sistema: lo que lo tumbo fue
que la primera perdida sobrecargo a las vecinas, esas dispararon, y la
redistribucion siguiente disparo a las siguientes. La pregunta no es si
aguanta el primer golpe, sino si el sistema se detiene despues del primero.

Este modulo cierra ese bucle:

    abrir el elemento -> resolver el flujo -> quien queda fuera de su
    proteccion -> abrir eso tambien -> resolver otra vez -> ...

hasta que una ronda no produzca disparos nuevos (el sistema se estabilizo) o
hasta que no quede nada que resolver (apagon).

LAS TRES REALIMENTACIONES QUE LO HACEN REALISTA
-----------------------------------------------
Un motor que solo mire sobrecargas se queda corto, porque hay tres lazos
distintos que se retroalimentan y cada uno tiene su escala de tiempo:

  1. TERMICA / SOBRECARGA (segundos a minutos). Al abrir una linea su flujo se
     reparte entre las vecinas. Si alguna supera su ajuste de proteccion,
     dispara. Es el lazo clasico de cascada.

  2. TENSION (segundos). Al perder lineas se pierde tambien su compensacion y
     cambia el reparto de reactivo. Sin limites de reactivo este lazo no
     existe: los generadores sostienen cualquier tension con MVAr infinitos.
     Por eso el motor exige `limites_reactivos` en cada resolucion, y por eso
     hubo que arreglar antes el deficit de reactivo del modelo.

  3. FRECUENCIA (decimas de segundo a segundos). Cuando la red se parte, cada
     isla queda con su propio desbalance y su propia frecuencia. El EDAC actua,
     la solar se desconecta por sus ajustes de proteccion, y el resultado
     cambia la carga que el flujo tiene que resolver en la ronda siguiente.

El tercero es el que conecta este modulo con `agc_deslastre.py`, y es el que
hace que un evento tipo Espana se pueda estudiar de verdad: alli la cascada de
tension y la de frecuencia se realimentaron entre si.

LO QUE ESTE MODELO NO ES
------------------------
No es un simulador de estabilidad transitoria. Cada ronda es un flujo de carga
en regimen permanente, no una integracion del comportamiento electromecanico
maquina a maquina. Los angulos de rotor, la estabilidad de pequena senal y la
perdida de sincronismo entre areas quedan fuera. Un estudio de cascada
completo necesitaria RMS o EMT; esto es el nivel de detalle que permite un
modelo de flujo, y sirve para responder "que secuencia de disparos sale" y
"cuanta carga se pierde", no "a que milisegundo se pierde el sincronismo".

Los tiempos que calcula sirven para ORDENAR los disparos, que es lo que
determina el resultado de una cascada. No pretenden ser exactos al
milisegundo.

TRES SALVEDADES QUE CONVIENE TENER DELANTE AL LEER UN RESULTADO
---------------------------------------------------------------
1. LA NO CONVERGENCIA SE CUENTA COMO APAGON. Cuando el flujo no resuelve, el
   motor lo declara colapso. Es la convencion habitual en estudios de cascada
   y suele ser correcta ---un sistema sin solucion de regimen permanente no se
   sostiene--- pero confunde el colapso fisico con el fallo numerico. Si un
   caso termina asi, merece la pena mirar la ultima ronda a mano antes de
   concluir nada.

2. LOS AJUSTES DE PROTECCION SON GENERICOS salvo los de la solar. La curva IEC
   de sobrecarga y los limites de tension de generador son valores tipicos,
   no los reles reales del STN, que no estan publicados. La secuencia que sale
   es plausible; no esta verificada contra los ajustes de XM.

3. OCHO PLANTAS SOLARES (135 MW de 4.200) declaran un voltaje maximo igual o
   menor que su nominal, y cinco un minimo igual o mayor. Tomados al pie de la
   letra disparan ante cualquier desviacion. Probablemente sea el dato
   repitiendo el nominal en vez de un ajuste real. Son 168 MW, el 4% de la
   solar, asi que se dejan como estan; si alguna conclusion dependiera de ese
   margen, habria que depurarlos.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

import networkx as nx
import numpy as np
import pandas as pd

from stn import agc_deslastre as AGC
from stn import contingencias as CT
from stn import eventos as EV
from stn import limites_reactivos as LR
from stn.config import salida


# ---------------------------------------------------------------------------
# Ajustes de proteccion
# ---------------------------------------------------------------------------

# Curva de tiempo inverso IEC 60255-151, caracteristica "standard inverse":
#
#       t = TMS * 0,14 / ((I/Is)^0,02 - 1)
#
# Se usa como modelo de la proteccion de respaldo (sobrecorriente temporizada
# y zona 3 de distancia), que es la que dispara por carga en una cascada. El
# arranque Is se toma en el limite de EMERGENCIA de cada elemento, no en el
# nominal, porque es el criterio con el que se ajusta el respaldo.
#
# Con TMS = 0,1 la curva da: 110% -> 7,3 s | 150% -> 1,7 s | 200% -> 1,0 s.
# Lo importante no es el valor absoluto sino el orden: cuanto mas cargado, mas
# rapido dispara, que es lo que decide la secuencia de la cascada.
#
# ADVERTENCIA: son ajustes genericos. El estudio riguroso usaria el ajuste real
# de cada rele, que PARATEC no publica. Con ajustes genericos la secuencia es
# plausible, no verificada.
IEC_TMS = 0.10
IEC_K = 0.14
IEC_ALFA = 0.02

CARGA_ARRANQUE_PCT = 100.0    # % de s_eme por debajo del cual no hay disparo
T_DISPARO_MAX_S = 30.0        # por encima de esto se considera que no dispara
T_RONDA_S = 10.0              # ventana de cada ronda (ver _disparos_de_ronda)

# Proteccion de tension de los generadores. Valores tipicos de rele 27/59; el
# fabricante los ajusta maquina a maquina y PARATEC no los publica.
V_GEN_MIN = 0.80              # pu
V_GEN_MAX = 1.20              # pu
T_GEN_SUBTENSION = 2.0        # s
T_GEN_SOBRETENSION = 1.0      # s

# raiz de la descarga de PARATEC, definida en un solo sitio
from stn.paratec_equipos import DIR_PARATEC

ARCHIVO_USOLAR = f'{DIR_PARATEC}/Generacion/PARATEC_Usolar_06-09-2026.xlsx'

MAX_RONDAS = 25


# ---------------------------------------------------------------------------
# Estructuras
# ---------------------------------------------------------------------------

@dataclass
class Disparo:
    """Un elemento que sale de servicio, con su causa y su instante."""
    ronda: int
    elemento: str
    clase: str       # 'Line' | 'Transformer' | 'Generator' | 'solar'
    causa: str       # 'sobrecarga' | 'sobretension' | 'subtension' | ...
    valor: float     # la magnitud que lo disparo
    t: float         # s desde el inicio del evento

    def __str__(self):
        return (f"t={self.t:6.2f}s  {self.causa:13s} {self.elemento:38s} "
                f"({self.valor:.3f})")


@dataclass
class Ronda:
    """Lo que ocurrio en una vuelta del bucle."""
    n: int
    convergio: bool = True
    islas: int = 1
    disparos: list = field(default_factory=list)
    deslastre_edac_mw: float = 0.0
    solar_perdida_mw: float = 0.0
    carga_anadida_mw: float = 0.0     # recursos neteados que caen y reaparecen
    # lo mismo desglosado por tipo de evento ('btm', 'solar', 'demanda'...):
    # sin esto, un escenario diurno sumaba los 2.500 MW de solar bajo la
    # etiqueta "BTM" y el informe atribuia mal 2/3 de la carga que aparece.
    carga_anadida_por_tipo: dict = field(default_factory=dict)
    carga_aislada_mw: float = 0.0
    gen_aislada_mw: float = 0.0
    f_min: float = 60.0
    islas_colapsadas: int = 0
    v_min: float = np.nan
    v_max: float = np.nan
    carga_max_pct: float = np.nan
    carga_servida_mw: float = np.nan


@dataclass
class Resultado:
    rondas: list = field(default_factory=list)
    disparos: list = field(default_factory=list)
    carga_inicial_mw: float = 0.0
    carga_final_mw: float = 0.0
    motivo_fin: str = ''
    apagon: bool = False

    @property
    def carga_anadida_mw(self):
        """Demanda que APARECE durante el evento y que tambien hay que servir.

        Es el BTM y la solar que se desconectan: como estan neteados por
        debajo de 200 kV, no se ven como generacion que se va sino como carga
        que brota en la tabla de loads.
        """
        return float(sum(r.carga_anadida_mw + r.solar_perdida_mw
                         for r in self.rondas))

    @property
    def demanda_total_mw(self):
        """Todo lo que el sistema tuvo que servir: la inicial mas la aparecida."""
        return self.carga_inicial_mw + self.carga_anadida_mw

    @property
    def carga_perdida_mw(self):
        """Demanda efectivamente desconectada: EDAC mas aislamiento.

        Tiene que medirse contra la demanda TOTAL, no contra la inicial. Con
        `inicial - final` la carga que aparece a mitad del evento se restaba
        de lo deslastrado y el desglose no cuadraba: en `--escenario btm`
        salian 1.694,9 MW de "carga perdida" con 3.171,9 MW solo de EDAC, y
        el residuo era exactamente el BTM que se habia anadido.
        """
        return self.demanda_total_mw - self.carga_final_mw

    @property
    def fraccion_perdida(self):
        return self.carga_perdida_mw / max(self.demanda_total_mw, 1e-9)


# ---------------------------------------------------------------------------
# Proteccion de sobrecarga
# ---------------------------------------------------------------------------

def tiempo_disparo(carga_pct):
    """Segundos hasta el disparo segun la curva IEC de tiempo inverso.

    Devuelve inf si la carga no llega al arranque. La formula diverge cuando
    I/Is -> 1, que es justo lo que se quiere: al 100,1% tarda una eternidad.
    """
    r = np.asarray(carga_pct, dtype=float) / CARGA_ARRANQUE_PCT
    with np.errstate(divide='ignore', invalid='ignore'):
        t = IEC_TMS * IEC_K / (np.power(r, IEC_ALFA) - 1.0)
    t = np.where(r <= 1.0, np.inf, t)
    return t


def curva_tension_solar(archivo=ARCHIVO_USOLAR, verbose=False):
    """Umbrales de TENSION a los que se desconecta la solar, desde PARATEC.

    Es el mismo dato que `eventos.curva_disparo_solar()` usa para frecuencia,
    pero la otra mitad: PARATEC declara para cada planta el voltaje maximo y
    minimo de operacion, en kV. Normalizados contra el nominal de la planta dan
    el ajuste en pu.

    Este es el mecanismo que importo en Espana. La solar del modelo colombiano
    no es un generador del STN ---ninguna de las 370 plantas conecta a 200 kV o
    mas, todas entran por debajo y estan neteadas en la inyeccion equivalente---
    asi que su desconexion no se ve como un generador que se va, sino como una
    CARGA QUE APARECE en la barra que la tenia neteada.

    Devuelve una lista de umbrales {'v_min','v_max','mw'} ordenada de mas
    estricto a menos.
    """
    from stn.data_loader import parse_float
    from stn.paratec_equipos import capacidad_plantas

    d = pd.read_excel(archivo, sheet_name='Usolar', header=5,
                      dtype=str).dropna(how='all')
    d = d[d['Nombre'].notna()].copy()
    for col, dest in (('Voltaje máximo [kV]', 'vmax'),
                      ('Voltaje mínimo [kV]', 'vmin'),
                      ('Voltaje nominal [kV]', 'vnom')):
        d[dest] = d[col].apply(parse_float)
    d['n'] = d['Nombre'].astype(str).str.upper().str.strip()

    # el mismo emparejamiento por nombre que usa eventos.curva_disparo_solar
    cap = capacidad_plantas()
    solar = cap[cap.tipo == 'solar'].copy()
    solar['n'] = solar.planta.astype(str).str.upper().str.strip()
    d = d.merge(solar[['n', 'mw']], on='n', how='left')

    ok = d.vnom.gt(0) & d.vmax.notna() & d.vmin.notna() & d.mw.notna()
    t = pd.DataFrame({
        'v_max': (d.vmax / d.vnom)[ok],
        'v_min': (d.vmin / d.vnom)[ok],
        'mw': d.mw[ok].astype(float),
    }).dropna().reset_index(drop=True)

    if verbose and len(t):
        print(f"  curva de tension solar: {len(t)} plantas, {t.mw.sum():.0f} MW"
              f" | v_min mediana {t.v_min.median():.3f} pu, "
              f"v_max mediana {t.v_max.median():.3f} pu")
    return t


# ---------------------------------------------------------------------------
# Islas
# ---------------------------------------------------------------------------

def _islas(network):
    """Componentes conexas, de mayor a menor generacion."""
    comps = list(nx.connected_components(CT._grafo(network)))
    gen = []
    for c in comps:
        gen.append(float(network.generators[
            network.generators.bus.isin(c)].p_set.sum()))
    orden = np.argsort(gen)[::-1]
    return [comps[i] for i in orden]


class _SistemaIsla:
    """Los parametros de la ecuacion de oscilacion, restringidos a una isla.

    `agc_deslastre.Sistema` los calcula para la red entera. Una cascada parte
    la red, y cada trozo tiene su propia inercia, su propia reserva y su propia
    frecuencia: una isla exportadora se acelera mientras la importadora se
    hunde. Reusar el Sistema global daria la frecuencia de un sistema que ya no
    existe.

    Se calculan con las MISMAS formulas y constantes de `agc_deslastre`, solo
    que sumando sobre las barras de la isla. Y ahora de verdad: se delega en
    `AGC.parametros_dinamicos()`, que es el unico sitio donde viven.

    Durante un tiempo esto era una COPIA de esas formulas, y la copia se quedo
    atras segun `Sistema` iba incorporando mejoras: el compromiso de unidades,
    el H declarado por PARATEC, el estatismo ponderado, la exclusion de los
    convertidores y la reserva a bajar. Sobre el caso base, que es una sola
    isla, los dos daban 13.947,7 frente a 17.698,0 MVA y predecian 5 y 4
    escalones del EDAC para la misma perdida de 3.000 MW.
    """

    def __init__(self, network, buses, gen_tabla):
        # OJO: gen_tabla viene indexada por GENERADOR, con la barra en la
        # columna 'bus'. Filtrar por el indice deja la tabla vacia y la isla
        # aparece sin generacion, lo que la declara muerta entera.
        g = gen_tabla[gen_tabla.bus.isin(buses)
                      & gen_tabla.index.isin(network.generators.index)].copy()
        # gen_tabla es una FOTO del arranque: sirve para 'tipo' y 'p_nom', que
        # no cambian, pero su p_set se queda congelado. Cuando un evento hace
        # rechazo parcial de carga ---recortar el despacho de una maquina en
        # vez de quitarla--- la reduccion no aparece por ningun lado y el
        # modelo de frecuencia calcula un deficit de -6 MW donde los hay -606.
        # Sin deficit no actua el EDAC, el slack absorbe los MW que faltan y
        # aparece una cascada termica que en realidad no existe.
        if len(g):
            g['p_set'] = network.generators.p_set.reindex(g.index).astype(float)
        self.network = network
        self.P_carga = float(
            network.loads[network.loads.bus.isin(buses)].p_set.sum())
        # Para que `parametros_dinamicos` pueda contar SOLO las perdidas de
        # las ramas internas a esta isla y no las del sistema entero.
        self.buses_isla = set(buses)
        # Todo lo demas ---s_sinc, S_base, H, R_eq, reserva a subir y a
        # bajar--- sale del mismo sitio que para el sistema entero.
        AGC.parametros_dinamicos(self, g)


def _frecuencia_de_islas(network, ronda, gen_tabla, con_edac=True,
                         verbose=False):
    """Respuesta de frecuencia de cada isla; devuelve el deslastre a aplicar.

    Para cada isla:
      - desbalance DP = generacion - carga
      - si DP < 0, hay deficit: se integra la oscilacion con EDAC
      - si la frecuencia cae de F_COLAPSO, la isla se apaga entera
      - si DP > 0 hay exceso; los reguladores lo absorben bajando generacion,
        y solo se marca como colapso si el exceso supera lo que la primaria
        puede recoger

    Devuelve {bus: MW a deslastrar} y actualiza la ronda.
    """
    deslastre = {}
    islas = _islas(network)
    ronda.islas = len(islas)
    f_min_global = 60.0

    for buses in islas:
        sis = _SistemaIsla(network, buses, gen_tabla)
        cargas = network.loads[network.loads.bus.isin(buses)]
        # las perdidas internas de la isla son consumo suyo
        dp = sis.P_gen - sis.P_carga - float(getattr(sis, 'P_perdidas', 0.0))

        if sis.S_base <= 0 or sis.P_gen <= 0:
            # isla sin generacion: se apaga entera, no hay frecuencia que
            # simular. Es el caso de una cola radial que se separa.
            if sis.P_carga > 0:
                for i, r in cargas.iterrows():
                    deslastre[i] = float(r.p_set)
                ronda.islas_colapsadas += 1
                ronda.carga_aislada_mw += sis.P_carga
            continue

        if dp >= 0:
            # exceso de generacion: sobrefrecuencia. Los reguladores bajan
            # potencia; solo si el exceso desborda la banda de regulacion se
            # considera perdida la isla. No se modela el disparo por
            # sobrefrecuencia maquina a maquina.
            if dp > 0.5 * sis.P_gen:
                ronda.islas_colapsadas += 1
                for i, r in cargas.iterrows():
                    deslastre[i] = float(r.p_set)
                ronda.carga_aislada_mw += sis.P_carga
            continue

        deficit = -dp
        t, f, escalones, traza = AGC.simular_frecuencia(
            sis, deficit_mw=deficit, con_edac=con_edac)
        f_fin = float(np.min(f))
        f_min_global = min(f_min_global, f_fin)

        if f_fin < AGC.F_COLAPSO:
            # la isla no se sostiene: apagon en ese trozo
            ronda.islas_colapsadas += 1
            for i, r in cargas.iterrows():
                deslastre[i] = float(r.p_set)
            ronda.carga_aislada_mw += sis.P_carga
            if verbose:
                print(f"      isla de {len(buses):3d} barras COLAPSA "
                      f"(deficit {deficit:.0f} MW, f_min {f_fin:.2f} Hz)")
            continue

        # 'escalones' es una lista de dicts por escalon del EDAC, no numeros:
        # el total deslastrado lo lleva la traza
        mw = float(traza.get('deslastre_total', 0.0))
        if mw > 0:
            # reparto proporcional a la carga dentro de la isla
            total = float(cargas.p_set.sum())
            for i, r in cargas.iterrows():
                deslastre[i] = mw * float(r.p_set) / max(total, 1e-9)
            ronda.deslastre_edac_mw += mw
            if verbose:
                print(f"      isla de {len(buses):3d} barras: deficit "
                      f"{deficit:.0f} MW, EDAC {mw:.0f} MW, "
                      f"f_min {f_fin:.2f} Hz")

    ronda.f_min = f_min_global
    return deslastre


# ---------------------------------------------------------------------------
# Resolucion de una ronda
# ---------------------------------------------------------------------------

def _redesignar_slack(network):
    """Un slack por isla: el generador mas grande. Islas sin generacion mueren.

    Tras cada cambio de topologia hay que rehacerlo, porque el slack anterior
    puede haber quedado en otra isla y PyPSA devolveria NaN en la que se quedo
    sin referencia.
    """
    # Solo los que eran slack vuelven a PV. Poner la columna entera a 'PV'
    # ---que es lo que hacia antes--- arrasa tambien los PQ, y con
    # `config.LIMITES_REACTIVOS` activo hay doce generadores conmutados a PQ
    # con su Q fijada en el limite de su curva. Devolverlos a PV deshace el
    # lazo de limites entero y les permite inventar reactivo otra vez.
    from stn.power_flow import _liberar_slacks
    _liberar_slacks(network)
    muertas = []
    for buses in _islas(network):
        g = network.generators[network.generators.bus.isin(buses)]
        if len(g) and g.p_set.max() > 0:
            network.generators.loc[g.p_set.idxmax(), 'control'] = 'Slack'
        else:
            muertas.extend(buses)
    return muertas


def _quitar_buses(network, buses):
    """Elimina barras y todo lo que cuelga de ellas."""
    if not buses:
        return 0.0, 0.0
    buses = [b for b in buses if b in network.buses.index]
    if not buses:
        return 0.0, 0.0
    carga = float(network.loads[network.loads.bus.isin(buses)].p_set.sum())
    gen = float(network.generators[network.generators.bus.isin(buses)].p_set.sum())
    for comp in ('Generator', 'Load', 'ShuntImpedance'):
        df = CT._tabla(network, comp)
        if len(df):
            q = df[df.bus.isin(buses)].index.tolist()
            if q:
                network.remove(comp, q)
    for comp in ('Line', 'Transformer'):
        df = CT._tabla(network, comp)
        if len(df):
            q = df[df.bus0.isin(buses) | df.bus1.isin(buses)].index.tolist()
            if q:
                network.remove(comp, q)
    network.remove('Bus', buses)
    return carga, gen


def _resolver(network, con_limites=True):
    """Flujo AC con limites de reactivo. Devuelve (ok, red_resuelta)."""
    try:
        res = network.pf(x_tol=1e-6, use_seed=False)
        ok = bool(res.converged.values.all())
    except Exception:                                        # noqa: BLE001
        return False, network
    if not ok:
        return False, network
    if con_limites:
        try:
            network, _ = LR.aplicar(network, verbose=False)
        except Exception:                                    # noqa: BLE001
            pass
    v = network.buses_t.v_mag_pu.iloc[0]
    return bool(v.notna().all()), network


# ---------------------------------------------------------------------------
# Deteccion de disparos
# ---------------------------------------------------------------------------

def _disparos_de_ronda(network, ronda_n, t0, verbose=False):
    """Quien dispara en esta ronda y en que orden.

    Se calcula el tiempo de disparo de TODOS los elementos que superan su
    arranque y se disparan solo los que caen dentro de una ventana T_RONDA_S
    desde el elemento mas rapido. Esa ventana es lo que reproduce la secuencia:
    si una linea dispara en 1 s y otra en 20 s, la segunda puede no llegar a
    disparar nunca porque la reconfiguracion tras la primera la descarga.
    Disparar todo a la vez ---el error tipico de un motor de cascada ingenuo---
    exagera el tamano del apagon.
    """
    cand = []

    pct = CT._cargabilidad(network, emergencia=True)
    for elem, c in pct.items():
        if c <= CARGA_ARRANQUE_PCT:
            continue
        t = float(tiempo_disparo(c))
        if t > T_DISPARO_MAX_S:
            continue
        clase = 'Line' if elem in network.lines.index else 'Transformer'
        cand.append(Disparo(ronda_n, elem, clase, 'sobrecarga', float(c), t0 + t))

    v = network.buses_t.v_mag_pu.iloc[0].astype(float)
    for g, bus in network.generators.bus.items():
        if bus not in v.index or not np.isfinite(v[bus]):
            continue
        if v[bus] < V_GEN_MIN:
            cand.append(Disparo(ronda_n, g, 'Generator', 'subtension',
                                float(v[bus]), t0 + T_GEN_SUBTENSION))
        elif v[bus] > V_GEN_MAX:
            cand.append(Disparo(ronda_n, g, 'Generator', 'sobretension',
                                float(v[bus]), t0 + T_GEN_SOBRETENSION))

    if not cand:
        return []

    t_min = min(d.t for d in cand)
    dentro = [d for d in cand if d.t <= t_min + T_RONDA_S]
    return sorted(dentro, key=lambda d: d.t)


def _solar_por_tension(network, curva, ida_por_carga, salida_mw=0.0,
                       verbose=False):
    """Solar que se desconecta por tension y reaparece como carga.

    La solar esta neteada en la inyeccion equivalente de cada barra, asi que
    su desconexion no se ve como un generador que se va sino como una CARGA
    QUE APARECE. Es el mecanismo que importo en Espana.

    EL REPARTO. Ninguna de las 370 plantas conecta a 200 kV o mas, asi que el
    modelo no sabe en que barra esta cada una. Comparar el ajuste de cada
    planta contra la tension MINIMA O MAXIMA DE TODO EL PAIS seria un error
    grosero: bastaria una barra remota a 1,09 pu para desconectar todas las
    plantas ajustadas a 1,00, aunque estuvieran en otra punta del sistema.

    Lo que se hace: repartir los 4.200 MW entre las barras con carga, en
    proporcion a su demanda ---el mismo criterio que usa `deslastre_btm`---, y
    evaluar CADA BARRA contra su propia tension. La fraccion de plantas que
    dispara en esa barra se toma de la distribucion de ajustes de PARATEC
    ponderada por MW. No se sabe donde esta cada planta, pero si como se
    distribuyen los ajustes, y eso basta para un reparto estadistico.

    `ida_por_carga` acumula, por barra, la solar ya desconectada, para que no
    se cuente dos veces entre rondas.
    """
    # Lo que se desconecta es la PRODUCCION, no la placa. A la punta nocturna
    # la fotovoltaica entrega cero, asi que no hay nada que desconectar: sin
    # esta guarda aparecian ~190 MW de carga fantasma a las 19:00, generados
    # por plantas que a esa hora no estaban produciendo.
    if salida_mw <= 0:
        return 0.0
    if curva is None or not len(curva) or not len(network.loads):
        return 0.0
    v = network.buses_t.v_mag_pu.iloc[0].astype(float)
    if not v.notna().any():
        return 0.0

    placa = float(curva.mw.sum())
    solar_total = min(float(salida_mw), placa)
    p = network.loads.p_set.astype(float)
    total = float(p.sum())
    if total <= 0:
        return 0.0

    mw_ido = 0.0
    for idx, carga in p.items():
        bus = network.loads.at[idx, 'bus']
        if bus not in v.index or not np.isfinite(v[bus]):
            continue
        vb = float(v[bus])
        # fraccion de la solar (ponderada por MW) que dispara a esta tension
        viola = (curva.v_min > vb) | (curva.v_max < vb)
        frac = float(curva.mw[viola].sum()) / max(placa, 1e-9)
        solar_bus = solar_total * carga / total
        objetivo = solar_bus * frac
        ya = ida_por_carga.get(idx, 0.0)
        nuevo = objetivo - ya
        if nuevo <= 1e-6:
            continue
        ida_por_carga[idx] = objetivo
        network.loads.loc[idx, 'p_set'] = float(carga) + nuevo
        mw_ido += nuevo

    if verbose and mw_ido > 0.1:
        print(f"      solar desconectada por tension: {mw_ido:.1f} MW")
    return mw_ido


# ---------------------------------------------------------------------------
# Eventos programados de eventos.py
# ---------------------------------------------------------------------------

def _subir_carga(network, mw, verbose=False, etiqueta=''):
    """Reparte un aumento de carga proporcionalmente a la demanda de cada barra.

    Es como entra al modelo la perdida de un recurso que esta NETEADO en la
    inyeccion equivalente: no se ve como generacion que desaparece sino como
    demanda que aparece.
    """
    if mw <= 0 or not len(network.loads):
        return 0.0
    p = network.loads.p_set.astype(float)
    total = float(p.sum())
    if total <= 0:
        return 0.0
    network.loads['p_set'] = p * (1.0 + mw / total)
    if verbose:
        print(f"      +{mw:.1f} MW de carga ({etiqueta})")
    return mw


def _aplicar_evento(network, ev, ctx, verbose=False):
    """Aplica UNA perturbacion programada a la red, en su instante.

    La diferencia con `eventos.construir()` es el momento: alli todos los
    eventos se agregan de una vez para alimentar la ecuacion de oscilacion.
    Aqui cada uno entra cuando le toca, intercalado con los disparos de
    proteccion que va produciendo la propia cascada. Esa es la parte que
    importa de un evento tipo Iberia: el orden en que ceden las cosas.

    Devuelve (mw_generacion_perdida, mw_carga_anadida).
    """
    dgen = dcarga = 0.0
    p_set = network.generators.p_set

    if ev.tipo == 'gen':
        if ev.elementos:
            nom = [g for g in ev.elementos if g in network.generators.index]
        else:
            objetivo = (float(p_set.sum()) * float(ev.fraccion)
                        if ev.fraccion else float(ev.mw))
            # Maquinas enteras mientras quepan, y el resto se recorta de la
            # siguiente bajando su despacho. Sin ese recorte, pedir 328 MW
            # cuando la maquina mas grande da 1.200 se lleva los 1.200 y el
            # parametro de escala deja de controlar nada: el evento sale 3,7
            # veces mayor que el pedido.
            nom, acum = [], 0.0
            resto_gen, resto_mw = None, 0.0
            for g, p in p_set.sort_values(ascending=False).items():
                p = float(p)
                if acum + p <= objetivo + 1e-9:
                    nom.append(g)
                    acum += p
                elif acum < objetivo:
                    resto_gen, resto_mw = g, objetivo - acum
                    break
                else:
                    break
            nom = [g for g in nom if g in network.generators.index]
            if nom:
                dgen = float(p_set[nom].sum())
                network.remove('Generator', nom)
            if resto_gen is not None and resto_gen in network.generators.index:
                # rechazo parcial de carga en esa maquina
                actual = float(network.generators.at[resto_gen, 'p_set'])
                network.generators.loc[resto_gen, 'p_set'] = max(
                    0.0, actual - resto_mw)
                dgen += min(resto_mw, actual)
            return dgen, dcarga

        nom = [g for g in nom if g in network.generators.index]
        if nom:
            dgen = float(p_set[nom].sum())
            network.remove('Generator', nom)

    elif ev.tipo in ('solar', 'eolica'):
        # En este modelo NINGUNA planta solar es generador del STN: las 370
        # conectan por debajo de 200 kV y estan neteadas en la inyeccion
        # equivalente. Por eso su perdida no se quita de la generacion sino
        # que se anade a la carga. Si algun dia hubiera solar en el STN, la
        # primera rama la recogeria.
        nom = [g for g in network.generators.index
               if ctx.get('tipo_de', {}).get(g) == ev.tipo]
        if nom:
            dgen = float(p_set[nom].sum()) * float(ev.fraccion or 1.0)
            if (ev.fraccion or 1.0) >= 0.999:
                network.remove('Generator', nom)
        else:
            mw = ctx.get(f'{ev.tipo}_mw', 0.0) * float(ev.fraccion or 1.0)
            dcarga = _subir_carga(network, mw, verbose, ev.descripcion())

    elif ev.tipo == 'btm':
        # `ev.fraccion or 1.0` convertia la fraccion 0,0 en 1,0, porque el
        # cero es falsy en Python: '--caida-btm 0' significaba 100 %, justo
        # lo contrario de lo que pide. Se comprueba contra None.
        frac = 1.0 if ev.fraccion is None else float(ev.fraccion)
        mw = ctx.get('btm_mw', 0.0) * frac
        if ev.mw and ev.fraccion is None:
            mw = float(ev.mw)
        dcarga = _subir_carga(network, mw, verbose, ev.descripcion())

    elif ev.tipo == 'linea':
        nom = list(ev.elementos) or ctx.get('apertura', [])
        for e in nom:
            for comp in ('Line', 'Transformer'):
                if e in CT._tabla(network, comp).index:
                    network.remove(comp, e)
                    break
        ctx['apertura'] = []      # la apertura inicial solo se usa una vez

    elif ev.tipo == 'interconexion':
        nom = [g for g in network.generators.index if 'PIMAMPIRO' in str(g).upper()]
        if nom:
            dgen = float(p_set[nom].sum())
            network.remove('Generator', nom)

    elif ev.tipo == 'demanda':
        dcarga = _subir_carga(network, float(ev.mw), verbose, ev.descripcion())

    else:
        raise ValueError(f"tipo de evento desconocido: '{ev.tipo}'")

    return dgen, dcarga


def _contexto(network, sis=None, btm_mw=None, solar_mw=None, apertura=(),
              produccion_btm_pu=0.0):
    """Datos que los eventos necesitan y que no estan en la red."""
    ctx = {'apertura': list(apertura)}
    if sis is not None and hasattr(sis, 'gen'):
        ctx['tipo_de'] = sis.gen.tipo.to_dict()

    # LO QUE SE DESCONECTA ES LA PRODUCCION, NO LA PLACA. Las 370 plantas
    # suman 4.200 MW instalados, pero el caso base es la punta NOCTURNA
    # (~19:00) y a esa hora la fotovoltaica entrega cero. Un evento que
    # "pierde 4.200 MW de solar" a esa hora es fisicamente imposible: no hay
    # nada generando que se pueda desconectar.
    #
    # Por eso solar_mw es la SALIDA en el punto de operacion, no la capacidad.
    # Por defecto cero, que es lo que corresponde al caso que hay. Para
    # estudiar un evento diurno hay que pasar la salida explicitamente
    # (--solar-salida) o, mejor, un caso base diurno, que es lo que esta
    # pedido al proveedor de datos.
    ctx['solar_placa_mw'] = 0.0
    try:
        ctx['solar_placa_mw'] = float(curva_tension_solar().mw.sum())
    except Exception:                                        # noqa: BLE001
        pass
    ctx['solar_mw'] = 0.0 if solar_mw is None else float(solar_mw)
    ctx['eolica_mw'] = 0.0        # el parque eolico de 2034 no esta en el caso

    if btm_mw is None:
        # `repartir_btm` necesita el escenario y el mapa de areas; si no se
        # pueden construir aqui, es preferible avisar que devolver 0 en
        # silencio, porque un BTM de 0 MW hace que el evento 'btm' del
        # escenario iberia no haga NADA y el resultado parezca benigno.
        try:
            from stn import deslastre_btm as DB
            from stn import zonas
            esc_btm = DB.EscenarioBTM(
                produccion_inversor_pu=float(produccion_btm_pu))
            mapa = zonas.area_por_bus(network)
            btm = DB.repartir_btm(network, esc_btm, mapa)
            # Solo la parte conectada por INVERSOR responde al hueco de
            # tension del escenario; el resto del registro son pequenas
            # centrales hidraulicas y termicas, maquinas sincronas que no se
            # desconectan en bloque. Sin este filtro cascada.py desconectaba
            # los 1.641 MW enteros mientras deslastre_btm.py, ya corregido,
            # desconectaba 621: los dos modulos discrepaban sobre el mismo
            # evento fisico.
            f_inv = DB.fraccion_inversor(esc_btm, mapa, btm.index)
            # LO QUE SE DESCONECTA ES LA PRODUCCION, NO LA PLACA, y esto
            # faltaba: `_contexto` repartia el BTM y filtraba la parte con
            # inversor, pero nunca llamaba a `btm_produciendo`, de modo que
            # `produccion_inversor_pu` se IGNORABA. El escenario desconectaba
            # los 722,8 MW de AGPE como si entregasen el 100 % de su placa
            # ---tambien en la punta NOCTURNA, donde la fotovoltaica entrega
            # cero y ese evento es fisicamente imposible---. Es el mismo
            # defecto que `deslastre_btm` corrigio el 3-10-2026 y que este
            # modulo no heredo.
            btm, f_inv = DB.btm_produciendo(btm, esc_btm, f_inv)
            btm_mw = float((btm * f_inv).sum())
        except Exception as e:                               # noqa: BLE001
            btm_mw = 0.0
            print(f"  AVISO: no se pudo estimar el BTM ({e}). El evento 'btm' "
                  f"de la secuencia no tendra efecto.")
    ctx['btm_mw'] = btm_mw
    return ctx


# ---------------------------------------------------------------------------
# El bucle
# ---------------------------------------------------------------------------

def _invalidar_despacho(network):
    """Borra el despacho RESUELTO tras un evento que cambia la generacion.

    EL EVENTO DE GENERACION NO LLEGABA AL MODELO DE FRECUENCIA, y este es el
    motivo. `_frecuencia_de_islas` calcula el deficit como

        dp = P_gen - P_carga - P_perdidas

    y `P_gen` lo da `agc_deslastre._despacho_resuelto`, que PREFIERE
    `generators_t.p` sobre `p_set`. Eso es correcto en el caso base ---la
    diferencia es el aporte del slack, 2,7 MW--- pero deja de serlo en cuanto
    un evento modifica la consigna sin volver a resolver: `generators_t.p`
    sigue siendo la solucion ANTERIOR, que por construccion esta equilibrada
    porque el slack cerro el balance. El deficit salia cero siempre.

    `_aplicar_evento` quita generadores enteros cuando puede, y esos si
    desaparecen del indice y se notan. Pero el remanente lo consigue BAJANDO
    el `p_set` de una maquina, y ese trozo era invisible. En el escenario
    `cascada` (4 x 600 MW) ninguna perdida llegaba a ser un generador entero
    ---San Carlos despacha 1.200 MW--- asi que se perdian los 2.400 MW
    completos. Medido antes del arreglo:

        ronda   P_gen resuelto   P_gen consigna   dp usado   dp real
          1          11.689,8         11.087,1       +0,0     -602,7
          4          11.731,1          9.287,1      +20,8   -2.423,2

    Con el despacho resuelto borrado, `_despacho_resuelto` cae a `p_set`, que
    es justo el respaldo que su propio docstring describe ("si la red no esta
    resuelta se cae a la consigna"). El flujo se vuelve a resolver al final de
    la ronda, asi que la siguiente arranca de nuevo con el dato bueno.

    No se tocan `lines_t` ni `buses_t`: las perdidas y las tensiones de la
    ronda anterior siguen siendo la mejor estimacion disponible hasta que el
    flujo se resuelva, y su error es de unos pocos MW frente a los cientos
    que estaban desapareciendo.

    SE SINCRONIZA, NO SE BORRA. Vaciar `generators_t['p']` tambien funcionaba
    para el deficit ---`_despacho_resuelto` caia a `p_set`--- pero dejaba a
    PyPSA un marco sin filas y el flujo de la ronda dejaba de converger. Se
    reescribe la fila con la consigna, que es exactamente el valor que debe
    verse: lo que el eje entrega tras el evento y antes de que nadie lo
    corrija. La diferencia con la carga es el deficit que el slack tendria
    que cubrir, y es justo lo que la ecuacion de oscilacion necesita.
    """
    din = getattr(network, 'generators_t', None)
    if din is None or 'p' not in din or not len(din['p']):
        return
    p = din['p']
    consigna = network.generators.p_set.reindex(p.columns)
    p.iloc[0] = consigna.fillna(0.0).values


def simular_cascada(network, apertura=(), eventos=None, con_edac=True,
                    con_solar=True, max_rondas=MAX_RONDAS, ctx=None,
                    verbose=True):
    """Propaga una perturbacion hasta que el sistema se estabiliza.

    Dos formas de arrancar, combinables:

      `apertura`  lista de elementos que se abren en t=0. Lo simple.
      `eventos`   lista de `eventos.Evento` CON SU INSTANTE. Lo interesante.

    LA CLAVE DE LA SEGUNDA: un mismo reloj. Los eventos programados y los
    disparos de proteccion se intercalan en la misma linea de tiempo, asi que
    la red que encuentra el evento de t=3,0 s ya lleva encima lo que la
    proteccion disparo en t=1,2 s. Aplicarlos todos de golpe al principio
    ---que es lo que hace `eventos.construir()` para la ecuacion de
    oscilacion--- perderia justamente lo que define un evento tipo Iberia: el
    orden en que ceden las cosas.

    Cuando una ronda no produce disparos pero quedan eventos pendientes, el
    reloj SALTA al siguiente evento en vez de terminar. El sistema puede
    aguantar el primer golpe y caerse con el cuarto.

    Devuelve un `Resultado` con la traza completa.
    """
    n = network.copy()
    res = Resultado()
    res.carga_inicial_mw = float(n.loads.p_set.sum())

    gen_tabla = AGC._tipo_y_capacidad_por_bus(n)
    curva = None
    if con_solar:
        try:
            curva = curva_tension_solar(verbose=verbose)
        except Exception as e:                               # noqa: BLE001
            if verbose:
                print(f"  AVISO: sin curva de tension solar ({e})")
    solar_ida = {}

    pendientes = sorted(eventos, key=lambda e: e.t) if eventos else []
    if ctx is None:
        ctx = _contexto(n, apertura=apertura)
    else:
        ctx.setdefault('apertura', list(apertura))

    # --- apertura directa (sin secuencia) -----------------------------------
    if apertura and not pendientes:
        for elem in apertura:
            for comp in ('Line', 'Transformer'):
                if elem in CT._tabla(n, comp).index:
                    n.remove(comp, elem)
                    res.disparos.append(
                        Disparo(0, elem, comp, 'evento inicial', 0.0, 0.0))
                    break

    t = 0.0
    for k in range(1, max_rondas + 1):
        r = Ronda(n=k)

        # 0. eventos programados cuyo instante ya llego
        while pendientes and pendientes[0].t <= t + 1e-9:
            ev = pendientes.pop(0)
            try:
                dgen, dcarga = _aplicar_evento(n, ev, ctx, verbose=verbose)
            except Exception as e:                           # noqa: BLE001
                if verbose:
                    print(f"      AVISO: evento '{ev.tipo}' no aplicable ({e})")
                continue
            r.carga_anadida_mw += dcarga
            if dcarga:
                r.carga_anadida_por_tipo[ev.tipo] = (
                    r.carga_anadida_por_tipo.get(ev.tipo, 0.0) + dcarga)
            res.disparos.append(
                Disparo(k, ev.descripcion(), 'evento', ev.tipo,
                        dgen if dgen else dcarga, ev.t))
            if verbose:
                print(f"  t={ev.t:5.2f}s  EVENTO  {ev.descripcion()}"
                      + (f"   (-{dgen:.0f} MW gen)" if dgen else '')
                      + (f"   (+{dcarga:.0f} MW carga)" if dcarga else ''))
            if dgen:
                _invalidar_despacho(n)

        # 1. islas y frecuencia -> deslastre
        muertas = _redesignar_slack(n)
        if muertas:
            c, g = _quitar_buses(n, muertas)
            r.carga_aislada_mw += c
            r.gen_aislada_mw += g
            _redesignar_slack(n)

        if not len(n.buses) or not len(n.generators):
            r.convergio = False
            res.rondas.append(r)
            res.motivo_fin = 'no queda sistema que resolver'
            res.apagon = True
            break

        deslastre = _frecuencia_de_islas(n, r, gen_tabla, con_edac=con_edac,
                                         verbose=verbose)
        for idx, mw in deslastre.items():
            if idx in n.loads.index:
                n.loads.loc[idx, 'p_set'] = max(
                    0.0, float(n.loads.at[idx, 'p_set']) - mw)

        # 2. resolver el flujo con limites de reactivo
        ok, n = _resolver(n, con_limites=True)
        r.convergio = ok
        if not ok:
            res.rondas.append(r)
            res.motivo_fin = f'el flujo no converge en la ronda {k}'
            res.apagon = True
            break

        # 3. solar que se va por tension (aparece como carga). Si cambia algo,
        #    hay que volver a resolver: si no, las cargabilidades del paso 4
        #    serian las de antes de que apareciera esa carga.
        if curva is not None:
            mw = _solar_por_tension(n, curva, solar_ida,
                                    salida_mw=ctx.get('solar_mw', 0.0),
                                    verbose=verbose)
            r.solar_perdida_mw = mw
            if mw > 0.1:
                ok, n = _resolver(n, con_limites=True)
                r.convergio = ok
                if not ok:
                    res.rondas.append(r)
                    res.motivo_fin = (f'el flujo no converge en la ronda {k} '
                                      f'tras desconectarse {mw:.0f} MW de solar')
                    res.apagon = True
                    break

        v = n.buses_t.v_mag_pu.iloc[0].astype(float)
        pct = CT._cargabilidad(n, emergencia=True)
        r.v_min, r.v_max = float(v.min()), float(v.max())
        r.carga_max_pct = float(pct.max()) if len(pct) else 0.0
        r.carga_servida_mw = float(n.loads_t.p.iloc[0].sum())

        # 4. quien dispara
        disparos = _disparos_de_ronda(n, k, t, verbose=verbose)
        r.disparos = disparos
        res.rondas.append(r)

        if verbose:
            print(f"  ronda {k:2d}: islas {r.islas}  v [{r.v_min:.4f},"
                  f"{r.v_max:.4f}]  cmax {r.carga_max_pct:5.1f}%  "
                  f"carga {r.carga_servida_mw:7.1f} MW  "
                  f"EDAC {r.deslastre_edac_mw:6.1f}  disparos {len(disparos)}")
            for d in disparos[:6]:
                print(f"      {d}")
            if len(disparos) > 6:
                print(f"      ... y {len(disparos)-6} mas")

        if not disparos:
            if pendientes:
                # nada disparo, pero la secuencia no ha terminado: el reloj
                # salta al siguiente evento. Aguantar el primer golpe no es
                # aguantar el evento.
                t = max(t, pendientes[0].t)
                continue
            res.motivo_fin = f'estable tras {k} rondas'
            break

        # 5. aplicar los disparos y volver a empezar
        t = max(d.t for d in disparos)
        res.disparos.extend(disparos)
        for d in disparos:
            comp = d.clase
            if comp in ('Line', 'Transformer') and d.elemento in CT._tabla(n, comp).index:
                n.remove(comp, d.elemento)
            elif comp == 'Generator' and d.elemento in n.generators.index:
                n.remove('Generator', d.elemento)
    else:
        res.motivo_fin = f'sigue disparando tras {max_rondas} rondas'
        res.apagon = True

    res.carga_final_mw = float(n.loads.p_set.sum()) if len(n.loads) else 0.0
    if res.fraccion_perdida > 0.5:
        res.apagon = True
    return res


# ---------------------------------------------------------------------------
# Informe
# ---------------------------------------------------------------------------

def informe(res, titulo=''):
    print(f"\n{'='*70}")
    print(f"CASCADA{(': ' + titulo) if titulo else ''}")
    print('='*70)
    print(f"  rondas             : {len(res.rondas)}")
    print(f"  disparos totales   : {len(res.disparos)}")
    edac = sum(r.deslastre_edac_mw for r in res.rondas)
    aisl = sum(r.carga_aislada_mw for r in res.rondas)
    sol = sum(r.solar_perdida_mw for r in res.rondas)
    por_tipo = {}
    for r in res.rondas:
        for k, v in r.carga_anadida_por_tipo.items():
            por_tipo[k] = por_tipo.get(k, 0.0) + v

    # Los recursos neteados que se caen ENTRAN como demanda, no salen como
    # perdida, asi que van en su propio bloque: sumados al desglose de abajo
    # hacian que lo deslastrado pareciera mayor que el total perdido.
    print(f"  carga inicial      : {res.carga_inicial_mw:9.1f} MW")
    for k in sorted(por_tipo, key=lambda k: -por_tipo[k]):
        if por_tipo[k] > 0.05:
            print(f"   + {k:15s} : {por_tipo[k]:9.1f} MW "
                  f"(neteado: aparece como carga)")
    if sol > 0.05:
        print(f"   + solar por tension: {sol:9.1f} MW (neteada: aparece como carga)")
    if res.carga_anadida_mw > 0.05:
        print(f"  demanda a servir   : {res.demanda_total_mw:9.1f} MW")
    print(f"  carga final        : {res.carga_final_mw:9.1f} MW")
    print(f"  carga no servida   : {res.carga_perdida_mw:9.1f} MW "
          f"({100*res.fraccion_perdida:.1f}% de la demanda a servir)")
    print(f"     por EDAC        : {edac:9.1f} MW")
    print(f"     por aislamiento : {aisl:9.1f} MW")
    resid = res.carga_perdida_mw - edac - aisl
    if abs(resid) > 0.5:
        print(f"     sin explicar    : {resid:9.1f} MW  <- el desglose no cuadra")
    print(f"  frecuencia minima  : {min((r.f_min for r in res.rondas), default=60):.2f} Hz")
    print(f"  islas al final     : {res.rondas[-1].islas if res.rondas else 1}")
    print(f"  resultado          : "
          f"{'APAGON' if res.apagon else 'el sistema se detiene'}")
    print(f"  motivo             : {res.motivo_fin}")

    if res.disparos:
        print("\n  secuencia de disparos")
        por_causa = {}
        for d in res.disparos:
            por_causa[d.causa] = por_causa.get(d.causa, 0) + 1
        for c, k in sorted(por_causa.items(), key=lambda x: -x[1]):
            print(f"     {c:15s} {k:3d}")
        print()
        for d in res.disparos[:25]:
            print(f"     ronda {d.ronda}  {d}")
        if len(res.disparos) > 25:
            print(f"     ... y {len(res.disparos)-25} mas")


def tabla(res):
    """Las rondas como DataFrame, para guardar o graficar."""
    return pd.DataFrame([{
        'ronda': r.n, 'convergio': r.convergio, 'islas': r.islas,
        'disparos': len(r.disparos), 'v_min': r.v_min, 'v_max': r.v_max,
        'carga_max_pct': r.carga_max_pct, 'carga_servida_mw': r.carga_servida_mw,
        'edac_mw': r.deslastre_edac_mw, 'aislada_mw': r.carga_aislada_mw,
        'solar_mw': r.solar_perdida_mw, 'carga_anadida_mw': r.carga_anadida_mw,
        'f_min': r.f_min,
        'islas_colapsadas': r.islas_colapsadas,
    } for r in res.rondas])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _apertura_por_nombre(network, patrones, nivel=None):
    """Elementos cuyo nombre contiene alguno de los patrones."""
    out = []
    for comp in ('Line', 'Transformer'):
        df = CT._tabla(network, comp)
        for e in df.index:
            u = str(e).upper()
            if any(p.upper() in u for p in patrones):
                if nivel and str(int(nivel)) not in u:
                    continue
                out.append(e)
    return out


def main():
    ap = argparse.ArgumentParser(description='Motor de cascada del STN')
    ap.add_argument('--abrir', nargs='+', default=None,
                    help='patrones de nombre de los elementos iniciales')
    ap.add_argument('--n-mayores', type=int, default=0,
                    help='abre las N lineas mas cargadas del caso base')
    ap.add_argument('--escenario', choices=sorted(EV.ESCENARIOS),
                    help='secuencia temporal de eventos.py')
    ap.add_argument('--escala', type=float, default=0.45,
                    help='fraccion de generacion perdida en el escenario iberia')
    ap.add_argument('--caida-btm', type=float, default=0.9)
    ap.add_argument('--solar-salida', type=float, default=0.0,
                    help='MW de solar EN PRODUCCION en el punto de operacion. '
                         'El caso base es la punta nocturna, donde vale 0: sin '
                         'esto el evento solar del escenario iberia no hace '
                         'nada, que es lo correcto a esa hora.')
    ap.add_argument('--sin-edac', action='store_true')
    ap.add_argument('--sin-solar', action='store_true')
    ap.add_argument('--mw', type=float, default=None,
                    help='magnitud total del escenario (simple, cascada)')
    ap.add_argument('--max-rondas', type=int, default=MAX_RONDAS)
    ap.add_argument('--csv', default=None)
    ap.add_argument('--diurno', nargs='?', type=int, const=11, default=None,
                    metavar='HORA',
                    help='corre la cascada sobre el punto de operacion DIURNO '
                         'en vez de la punta nocturna. Sin valor usa la hora '
                         '11 (10:00-11:00), la de maxima penetracion solar. '
                         'Fija por si solo la solar en produccion, asi que no '
                         'hace falta --solar-salida')
    ap.add_argument('--fecha', default=None, metavar='AAAA-MM-DD',
                    help='corre la cascada sobre el despacho y el pronostico '
                         'de XM de esa fecha. Incompatible con --diurno. Fija '
                         'por si solo la solar en produccion.')
    ap.add_argument('--periodo', type=int, default=19, metavar='N',
                    help='periodo horario 1..24 de --fecha (defecto 19)')
    a = ap.parse_args()

    if a.fecha and a.diurno is not None:
        ap.error('--fecha y --diurno son dos puntos de operacion distintos; '
                 'elige uno')

    if a.fecha:
        from stn.fuentes.xm_snapshot import FuenteXM
        fuente = FuenteXM(a.fecha, a.periodo, verbose=False)
        base, _ = CT.caso_base(verbose=False, fuente=fuente)
        cal = fuente.calidad()
        prod, placa_solar, pu = fuente.produccion_solar_pu()
        # Igual que con --diurno: el punto de operacion manda sobre
        # --solar-salida. Aqui `solar_mw` alimenta SOLO la curva de disparo
        # por tension (`_solar_por_tension`); el evento 'solar' de la
        # secuencia lee los generadores solares de la red, que en un punto de
        # XM existen de verdad porque la fotovoltaica va en el despacho. Son
        # dos mecanismos distintos y no se solapan.
        if a.solar_salida and abs(a.solar_salida - prod) > 1.0:
            print(f"  [!] --solar-salida {a.solar_salida:.0f} MW ignorado: el "
                  f"despacho de XM fija la solar en {prod:.0f} MW a esa hora")
        a.solar_salida = prod
        # Cuanto del BTM por inversor esta produciendo a esa hora. SIN ESTA
        # LINEA el modulo reventaba con UnboundLocalError en cuanto se le
        # pedia un escenario sobre una fecha: la variable solo se asignaba en
        # las ramas de punta y de diurno. Se usa el mismo factor medido del
        # despacho solar que alimenta `--solar-salida`.
        prod_btm_pu = float(pu)
        from stn import agc_deslastre as _AGC
        s_x = _AGC.Sistema(base)
        print(f"\nPUNTO DE OPERACION: XM {a.fecha} periodo {a.periodo}")
        print(f"  demanda {cal.p_demanda_ubicada_mw:.1f} MW "
              f"({cal.cobertura_demanda_pct:.2f} % del pronostico)  |  "
              f"solar {prod:.1f} MW ({pu:.3f} pu de {placa_solar:.0f})  |  "
              f"H*S {s_x.H*s_x.S_base:.0f} MVA.s")
        if not cal.apto_para_frecuencia:
            print(f"  AVISO: {cal.p_demanda_no_ubicada_mw:.1f} MW de demanda "
                  f"sin ubicar; el EDAC dimensiona por debajo")
    elif a.diurno is not None:
        from stn import escenario_diurno as ED
        base, info_d = ED.construir(hora=a.diurno, verbose=False)
        # El punto diurno ya sabe cuanta solar esta produciendo. Pasarla
        # ademas por --solar-salida la contaria dos veces, asi que manda el
        # escenario y se avisa si el usuario fijo las dos cosas.
        if a.solar_salida and abs(a.solar_salida
                                  - info_d['solar_produccion_MW']) > 1.0:
            print(f"  [!] --solar-salida {a.solar_salida:.0f} MW ignorado: el "
                  f"escenario diurno ya fija la solar en "
                  f"{info_d['solar_produccion_MW']:.0f} MW")
        a.solar_salida = info_d['solar_produccion_MW']
        prod_btm_pu = float(info_d.get('produccion_solar_pu') or 0.0)
        s_d = info_d['sis']
        print(f"\nPUNTO DE OPERACION: diurno, {info_d['etiqueta']}")
        print(f"  demanda neta {info_d['carga_neta_MW']:.1f} MW  |  "
              f"solar {info_d['solar_produccion_MW']:.1f} MW  |  "
              f"H*S {s_d.H*s_d.S_base:.0f} MVA.s "
              f"(punta: {info_d['sis_punta'].H*info_d['sis_punta'].S_base:.0f})")
    else:
        # punta nocturna: la fotovoltaica entrega cero, luego el BTM solar
        # no esta produciendo y no hay nada que desconectar.
        prod_btm_pu = 0.0
        base, _ = CT.caso_base(verbose=True)
    pct = CT._cargabilidad(base, emergencia=False)

    if a.n_mayores:
        apertura = list(pct.nlargest(a.n_mayores).index)
        titulo = f'las {a.n_mayores} lineas mas cargadas'
    elif a.abrir:
        apertura = _apertura_por_nombre(base, a.abrir)
        titulo = ' + '.join(a.abrir)
    else:
        apertura = [pct.idxmax()]
        titulo = 'la linea mas cargada'

    eventos = None
    if a.escenario:
        # Cada constructor de eventos.py nombra sus parametros a su manera y
        # todos aceptan **kw, asi que una clave mal puesta se descarta EN
        # SILENCIO: '--caida-btm 0.5' daba exactamente el mismo resultado que
        # 0.8 porque _btm() espera 'caida', no 'caida_btm'. Se pasan los dos
        # nombres para que cada escenario encuentre el suyo.
        kw = {'escala': a.escala, 'caida_btm': a.caida_btm,
              'caida': a.caida_btm}
        if a.mw is not None:
            kw['mw'] = a.mw
        eventos = EV.ESCENARIOS[a.escenario](**kw)
        titulo = f"escenario '{a.escenario}'"
        print(f"\nSecuencia temporal ({len(eventos)} eventos):")
        for e in eventos:
            print(f"   t={e.t:5.2f}s  {e.descripcion()}")
        print(f"\nDisparo local inicial: {apertura[0]}")
    else:
        if not apertura:
            print('No hay elementos que abrir con ese criterio.')
            return
        print(f"\nEvento inicial: {len(apertura)} elementos")
        for e in apertura:
            print(f"   {e}")

    sis = AGC.Sistema(base)
    # Factor de produccion del BTM a la hora del caso. En la punta nocturna
    # la fotovoltaica entrega CERO, asi que el evento de BTM no existe; en el
    # diurno lo fija el perfil horario de `escenario_diurno`.
    ctx = _contexto(base, sis=sis, apertura=apertura,
                    solar_mw=a.solar_salida,
                    produccion_btm_pu=prod_btm_pu)
    if a.escenario:
        print(f"   recursos neteados: solar EN PRODUCCION "
              f"{ctx['solar_mw']:.0f} MW (de {ctx['solar_placa_mw']:.0f} MW "
              f"instalados), BTM {ctx['btm_mw']:.0f} MW")
        if ctx['solar_mw'] <= 0:
            print("   NOTA: el caso base es la punta nocturna y la solar "
                  "entrega cero, asi que el evento solar no tendra efecto.\n"
                  "         Para un evento diurno: --diurno (recomendado) o\n"
                  "         --solar-salida <MW> si solo quieres mover la solar.")

    res = simular_cascada(base, apertura=apertura, eventos=eventos,
                          con_edac=not a.sin_edac,
                          con_solar=not a.sin_solar, ctx=ctx,
                          max_rondas=a.max_rondas, verbose=True)
    informe(res, titulo)
    t = tabla(res)
    # El sufijo del punto es obligatorio: sin el, correr el mismo escenario
    # sobre la punta, sobre el diurno y sobre una fecha escribia TRES veces
    # el mismo fichero, y acababas comparando un caso consigo mismo sin
    # notarlo. Es justo lo que `punto_operacion.sufijo()` existe para evitar.
    from stn import punto_operacion as _PO
    _suf = _PO.sufijo(a.diurno, getattr(a, 'fecha', None),
                      getattr(a, 'periodo', None))
    destino = a.csv or salida(
        f"cascada_{a.escenario or 'directo'}{_suf}.csv")
    t.to_csv(destino, index=False)
    print(f"\nRondas guardadas en {destino}")


if __name__ == '__main__':
    main()
