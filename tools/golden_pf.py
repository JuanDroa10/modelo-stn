# golden_pf.py
# Congelado del caso de PowerFactory: captura y lectura del golden.
#
# -------------------------------------------------------------------------
# POR QUE EXISTE
# -------------------------------------------------------------------------
# El modelo va a dejar de tener UNA sola fuente para el punto de operacion.
# Hoy sale del reporte de PowerFactory (`Datos/pq_completo.csv`); la idea es
# que tambien pueda salir de un snapshot de PARATEC + XM para una fecha y un
# periodo cualesquiera. En cuanto haya dos fuentes hace falta poder demostrar
# que la vieja sigue dando exactamente lo que daba.
#
# Hoy NO se puede demostrar. `validacion.py` imprime cifras y no afirma nada:
# no tiene un solo `assert`. Y `test_invariantes.py` lo dice en su cabecera
# con todas las letras ---"NO son pruebas de regresion numerica: no fijan el
# nadir ni el umbral"---. Entre las dos cubren que el modelo sea COHERENTE,
# no que siga dando LO MISMO. Ahora mismo un cambio puede mover la correlacion
# con PowerFactory de 0,80 a 0,60 sin que nada se queje.
#
# Este modulo congela el caso en uso: las entradas del punto de operacion, la
# red resuelta, los escalares de la dinamica y las metricas de validacion.
# `test_regresion_pf.py` contrasta contra el congelado.
#
# -------------------------------------------------------------------------
# POR QUE SE CONGELA Y NO SE RECALCULA
# -------------------------------------------------------------------------
# La tentacion es decir "corremos el pipeline nuevo para la fecha del caso de
# PowerFactory y comprobamos que coincide". No se puede, por dos razones
# independientes:
#
#   1. El caso NO tiene fecha de operacion. La cabecera del export dice
#      `Study Case: Max`, `Study Time: 31/12/2042`, `Expansion Stage: P20`.
#      Es un caso de planeamiento, no una hora de un dia. (Ver
#      `resultados/caso_2025/LEEME.md`: el "2025" del nombre de la carpeta es
#      la version de PowerFactory, no el año del estudio.)
#
#   2. PARATEC no publica historico. Los parametros que se descargan son los
#      vigentes el dia de la descarga, sean cuales sean las fechas de despacho
#      y demanda que se pidan.
#
# Asi que la validacion contra PowerFactory no es reproducible desde XM por
# construccion. Lo unico honesto es congelarla y defenderla como regresion.
#
# -------------------------------------------------------------------------
# QUE SE CONGELA, Y EN QUE ORDEN IMPORTA
# -------------------------------------------------------------------------
# En cuatro niveles, del mas estable al mas derivado. El orden es util cuando
# el test falla: el primer nivel que se mueve senala la causa.
#
#   1. entradas   inyecciones (P, Q, v_set) y desglose bruto (gen, carga).
#                 Es LO QUE la fuente nueva va a reemplazar. Aritmetica de
#                 pandas, sin solver: deberia ser identico al bit.
#   2. red        tensiones, flujos por rama, generadores y cargas resueltos.
#                 Sale del Newton-Raphson, asi que lleva tolerancia.
#   3. dinamica   S_base, H, reserva, R_eq... Es lo que alimenta el EDAC y el
#                 RoCoF, y lo que mas se va a mover cuando cambie la fuente.
#   4. metricas   correlacion y MAE contra PowerFactory, difuso y estricto.
#
# -------------------------------------------------------------------------
# USO
# -------------------------------------------------------------------------
#   ~/venvs/stn/bin/python golden_pf.py            # captura a Datos/golden_pf/
#   ~/venvs/stn/bin/python golden_pf.py --destino otra/ruta
#
# Recapturar es una decision deliberada: si el test falla y el cambio es
# intencionado, se recaptura y se explica en el commit QUE se movio y POR QUE.
# Recapturar para silenciar un fallo que no se entiende vacia de sentido el
# ejercicio entero.

import hashlib
import json
import os
import sys

import pandas as pd

from stn.config import FILE_LINES, FILE_MAPPING, FILE_PQ
from pathlib import Path

from stn.paths import GOLDEN, PUNTO, RAIZ, RED, dato


DIR_GOLDEN = str(GOLDEN)

# Ficheros de entrada cuyo hash entra en meta.json. Si uno cambia, el golden
# esta OBSOLETO, y eso es distinto de que el codigo este roto: el test lo
# distingue y lo dice, en vez de senalar al cambio de codigo mas reciente.
FICHEROS_ENTRADA = (
    FILE_PQ,                      # el reporte de PowerFactory
    FILE_LINES,                   # las lineas de PARATEC
    FILE_MAPPING,                 # el mapeo nodo -> bus
    dato(RED, 'lineas_completas.csv'),  # topologia STR, la usa desagregacion
    dato(PUNTO, 'edge_2034.csv'),  # referencia del contraste estricto
)

# Flags de config que cambian el resultado. Van al meta para que un fallo del
# test con los flags cambiados se lea como lo que es.
FLAGS_CONFIG = ('DESGLOSE_BRUTO', 'LIMITES_REACTIVOS', 'TRAFO_PARATEC',
                'INCLUIR_COMPLEMENTOS', 'PF_METHOD', 'PF_TOLERANCE',
                'VOLTAGE_MIN', 'VOLTAGE_MAX', 'STN_KV_MIN')

# %.17g es el formato mas corto que garantiza round-trip exacto de un double.
# Con menos precision el propio guardado introduciria una diferencia que el
# test confundiria con una regresion.
FLOAT_FMT = '%.17g'


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _rel(path):
    """Ruta relativa a la raiz del repositorio.

    El congelado se guarda con rutas RELATIVAS y no absolutas por dos
    razones, y las dos se vieron al empaquetar el proyecto. Una: una ruta
    absoluta lleva dentro el nombre de usuario y el arbol de carpetas de
    quien la capturo, que no tiene por que acabar en un repositorio publico.
    Dos, y peor: la comprobacion de "sigue siendo el mismo caso" compara esas
    rutas contra las de ahora, de modo que el congelado fallaria en cuanto
    alguien clonara el repositorio en otro sitio ---es decir, siempre---.
    """
    try:
        return str(Path(path).resolve().relative_to(RAIZ))
    except ValueError:
        return str(path)


def _abs(rel):
    """La inversa de `_rel`, para volver a leer el fichero."""
    p = Path(rel)
    return str(p if p.is_absolute() else RAIZ / p)


def _sha256(path):
    """Hash de un fichero de entrada. None si no existe."""
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for bloque in iter(lambda: f.read(1 << 20), b''):
            h.update(bloque)
    return h.hexdigest()


def _versiones():
    """Versiones que pueden mover un resultado numerico."""
    import numpy
    import pypsa
    return {
        'python': sys.version.split()[0],
        'pypsa': pypsa.__version__,
        'pandas': pd.__version__,
        'numpy': numpy.__version__,
    }


def _flags():
    from stn import config
    return {k: getattr(config, k) for k in FLAGS_CONFIG if hasattr(config, k)}


# ---------------------------------------------------------------------------
# Captura
# ---------------------------------------------------------------------------

def _entradas():
    """Nivel 1: lo que la fuente nueva tendra que producir.

    Se recalcula aqui en vez de sacarlo de la red porque es justo el objeto
    que va a cambiar de origen, y porque en la red ya ha pasado por
    `asignar_inyecciones`, que le cambia el signo a la Q y reparte las
    centrales desdobladas.
    """
    from stn import desagregacion
    from stn.data_integration import integrar

    inyecciones, resumen = integrar(verbose=False)
    desglose = desagregacion.desglose_de(inyecciones, verbose=False)

    iny = inyecciones[['P', 'Q', 'v_set']].copy()
    iny.index.name = 'bus'

    if desglose is None:
        # config.DESGLOSE_BRUTO en False. Se congela igual, con la columna
        # vacia, para que el test distinga "sin desglose" de "desglose roto".
        des = pd.DataFrame(index=iny.index, columns=['gen', 'carga'],
                           dtype=float)
    else:
        gen, carga = desglose
        des = pd.DataFrame({'gen': gen, 'carga': carga})
    des.index.name = 'bus'
    return iny, des, resumen


def _red(network):
    """Nivel 2: la red resuelta, en tres tablas."""
    from stn import contingencias as C

    v = network.buses_t.v_mag_pu.iloc[0]
    ang = network.buses_t.v_ang.iloc[0]
    buses = pd.DataFrame({'v_mag_pu': v, 'v_ang': ang})
    buses.index.name = 'bus'

    carga_pct = C._cargabilidad(network)
    filas = []
    for tipo, tabla, serie_t in (
            ('line', network.lines, network.lines_t),
            ('transformer', network.transformers, network.transformers_t)):
        if not len(tabla) or not len(serie_t.p0):
            continue
        filas.append(pd.DataFrame({
            'tipo': tipo,
            'bus0': tabla.bus0,
            'bus1': tabla.bus1,
            's_nom': tabla.s_nom,
            'p0': serie_t.p0.iloc[0],
            'q0': serie_t.q0.iloc[0],
            'p1': serie_t.p1.iloc[0],
            'carga_pct': carga_pct.reindex(tabla.index),
        }))
    ramas = pd.concat(filas) if filas else pd.DataFrame()
    ramas.index.name = 'rama'

    gens = pd.DataFrame({
        'bus': network.generators.bus,
        'control': network.generators.control,
        'p_set': network.generators.p_set,
        'p_nom': network.generators.p_nom,
        'p': network.generators_t.p.iloc[0],
        'q': network.generators_t.q.iloc[0],
    })
    gens.index.name = 'generador'

    cargas = pd.DataFrame({
        'bus': network.loads.bus,
        'p_set': network.loads.p_set,
        'q_set': network.loads.q_set,
    })
    cargas.index.name = 'carga'
    return buses, ramas, gens, cargas


def _dinamica(network):
    """Nivel 3: los escalares de los que cuelgan el EDAC y el RoCoF."""
    from stn import agc_deslastre as AGC

    sis = AGC.Sistema(network)
    escalares = {k: float(v) for k, v in vars(sis).items()
                 if isinstance(v, (int, float)) and not isinstance(v, bool)}

    # La tabla por generador va aparte: es la que explica un salto de H o de
    # S_base cuando lo haya, y sin ella el escalar no se puede diagnosticar.
    cols = [c for c in ('bus', 'p_set', 'tipo', 'p_nom', 'placa_declarada',
                        'n_maquinas', 'H_real', 'estatismo_real_pct',
                        's_sinc', 'via') if c in sis.gen.columns]
    tabla = sis.gen[cols].copy()
    tabla.index.name = 'generador'

    # La energia cinetica es el producto, y es lo que entra en el RoCoF. Se
    # guarda explicito porque H y S_base pueden moverse en sentidos opuestos
    # y dejar el producto quieto, o al reves.
    escalares['H_por_S_base'] = float(sis.H * sis.S_base)
    por_tipo = sis.gen.groupby('tipo').p_set.sum().round(10).to_dict()
    return escalares, tabla, {str(k): float(v) for k, v in por_tipo.items()}


def _metricas(network):
    """Nivel 4: el contraste contra PowerFactory, difuso y estricto.

    Se calcula sobre la MISMA red que se congela, no re-resolviendo con
    `validacion.resolver_modelo()`. Que los dos caminos coincidan ya lo
    comprueba `test_invariantes.test_los_cuatro_caminos_coinciden`; repetirlo
    aqui solo anadiria otra forma de que el golden y el modelo discrepen.
    """
    import numpy as np

    from stn import validacion as V

    out = {}

    cmp, n_mio, n_pf = V.comparar(network)
    d = cmp.dif_MW.abs()
    out['difuso'] = {
        'lineas_modelo': int(n_mio),
        'lineas_reporte': int(n_pf),
        'comparables': int(len(cmp)),
        'mae_mw': float(d.mean()),
        'mediana_mw': float(d.median()),
        'correlacion': float(np.corrcoef(cmp.PyPSA_MW,
                                         cmp.PowerFactory_MW)[0, 1]),
        'dentro_10_pct': float(100 * (d < 10).mean()),
        'dentro_25_pct': float(100 * (d < 25).mean()),
        'dentro_50_pct': float(100 * (d < 50).mean()),
        'dentro_100_pct': float(100 * (d < 100).mean()),
    }

    try:
        edge = V.contraste_edge(network, verbose=False)
    except FileNotFoundError:
        edge = None
    if edge is not None and len(edge):
        e = edge.dif_MW.abs()
        out['estricto'] = {
            'emparejadas': int(len(edge)),
            'mae_mw': float(e.mean()),
            'correlacion': float(edge.PowerFactory_MW.corr(edge.PyPSA_MW)),
            'dentro_10': int((e < 10).sum()),
            'dentro_25': int((e < 25).sum()),
        }
    else:
        out['estricto'] = None
    return out, cmp


def capturar(destino=DIR_GOLDEN, verbose=True):
    """Corre el pipeline canonico y escribe el golden. Devuelve el meta."""
    from stn import contingencias as C

    if verbose:
        print(f"Capturando el golden del caso en uso en {destino}/")

    os.makedirs(destino, exist_ok=True)

    iny, des, resumen = _entradas()
    if verbose:
        print(f"  [1/4] entradas  : {len(iny)} buses con inyeccion")

    # La puerta canonica: es la que usan contingencias, agc_deslastre, cascada
    # y deslastre_btm. Congelar otra cosa seria congelar un caso que nadie usa.
    network, diag = C.caso_base(verbose=False)
    buses, ramas, gens, cargas = _red(network)
    if verbose:
        print(f"  [2/4] red       : {len(buses)} buses, {len(ramas)} ramas, "
              f"{len(gens)} generadores, {len(cargas)} cargas")

    escalares, tabla_gen, mw_tipo = _dinamica(network)
    if verbose:
        print(f"  [3/4] dinamica  : S_base {escalares['S_base']:.1f} MVA, "
              f"H {escalares['H']:.4f} s, "
              f"reserva {escalares['reserva']:.1f} MW")

    metricas, cmp = _metricas(network)
    if verbose:
        df = metricas['difuso']
        print(f"  [4/4] metricas  : corr {df['correlacion']:.4f}, "
              f"MAE {df['mae_mw']:.2f} MW sobre {df['comparables']} lineas")

    def guardar(df, nombre):
        df.to_csv(os.path.join(destino, nombre), float_format=FLOAT_FMT,
                  encoding='utf-8')

    guardar(iny, 'inyecciones.csv')
    guardar(des, 'desglose.csv')
    guardar(buses, 'buses.csv')
    guardar(ramas, 'ramas.csv')
    guardar(gens, 'generadores.csv')
    guardar(cargas, 'cargas.csv')
    guardar(tabla_gen, 'dinamica_por_generador.csv')
    guardar(cmp, 'contraste_difuso.csv')

    meta = {
        'capturado_en': pd.Timestamp.now('UTC').isoformat(),
        'caso': 'punta nocturna (PowerFactory, Datos/pq_completo.csv)',
        'versiones': _versiones(),
        'flags_config': _flags(),
        'hashes_entrada': {_rel(p): _sha256(p) for p in FICHEROS_ENTRADA},
        'resumen_integrar': {k: (float(v) if isinstance(v, (int, float))
                                 else v)
                             for k, v in resumen.items()},
        'diagnostico': {
            'v_min': diag['v_min'],
            'v_max': diag['v_max'],
            'carga_max_pct': diag['carga_max_pct'],
            'carga_MW': diag['carga_MW'],
            'n_v_bajas': len(diag['v_bajas']),
            'n_v_altas': len(diag['v_altas']),
            'n_sobrecargados': len(diag['sobrecargados']),
        },
        'conteos': {
            'buses': len(buses), 'ramas': len(ramas),
            'generadores': len(gens), 'cargas': len(cargas),
            'control': {str(k): int(v) for k, v in
                        gens.control.value_counts().to_dict().items()},
        },
        'dinamica': escalares,
        'mw_por_tipo': mw_tipo,
        'metricas': metricas,
    }
    with open(os.path.join(destino, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write('\n')

    if verbose:
        print(f"  escrito: 8 CSV + meta.json en {destino}/")
    return meta


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------

def cargar(origen=DIR_GOLDEN):
    """Lee el golden. Devuelve (meta, {nombre: DataFrame})."""
    with open(os.path.join(origen, 'meta.json'), encoding='utf-8') as f:
        meta = json.load(f)
    tablas = {}
    for nombre in ('inyecciones', 'desglose', 'buses', 'ramas', 'generadores',
                   'cargas', 'dinamica_por_generador', 'contraste_difuso'):
        path = os.path.join(origen, f'{nombre}.csv')
        tablas[nombre] = pd.read_csv(path, index_col=0, encoding='utf-8')
    return meta, tablas


def entrada_cambiada(meta):
    """Ficheros de entrada cuyo hash ya no coincide con el del golden.

    Sirve para que el test diga "el golden esta obsoleto" en vez de "el
    modelo esta roto" cuando lo que cambio fue el dato de partida.
    """
    return [r for r, h in meta['hashes_entrada'].items()
            if _sha256(_abs(r)) != h]


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='Congela el caso de PowerFactory como golden de regresion.')
    ap.add_argument('--destino', default=DIR_GOLDEN,
                    help=f'carpeta de salida (por omision {DIR_GOLDEN})')
    args = ap.parse_args()
    capturar(destino=args.destino)


if __name__ == '__main__':
    main()
