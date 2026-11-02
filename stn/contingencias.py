# contingencias.py
"""Analisis de contingencias sobre el modelo del STN.

Simula la salida de elementos de la red (lineas, autotransformadores o
generadores) y evalua el estado resultante contra tres criterios:

  1. Convergencia del flujo de carga.
  2. Carga desconectada, si la contingencia separa la red en islas.
  3. Sobrecargas y tensiones fuera de rango en la parte que sigue conectada.

Criterio de sobrecarga. En operacion normal una linea se evalua contra su
capacidad nominal (s_nom); despues de una contingencia se admite el limite de
emergencia (s_eme), que es 1,3 veces el nominal en la mediana de esta red. Los
dos valores salen del CSV de PARATEC.

Las violaciones se reportan siempre RESPECTO AL CASO BASE: si una barra ya
esta por debajo de 0,9 p.u. sin ninguna contingencia -como pasa con el radial
de Arauca- no se cuenta como violacion nueva. De lo contrario todas las
contingencias arrastrarian los mismos cuatro incumplimientos y no se
distinguiria lo que cada una provoca de verdad.

Uso:
    python contingencias.py                      # N-1 sobre lineas y trafos
    python contingencias.py --nivel 500          # solo elementos de 500 kV
    python contingencias.py --tipo generador     # salida de generadores
    python contingencias.py --area Caribe        # solo un area operativa
    python contingencias.py --n2 --nivel 500     # dobles contingencias en 500 kV
    python contingencias.py --caso "LINEA A" "LINEA B"   # un caso concreto
    python contingencias.py --top 30 --csv salida.csv
"""

import argparse
import itertools
import sys
import time

import networkx as nx
import numpy as np
import pandas as pd

from stn import config
from stn.config import salida

# PyPSA 1.x marco Network.df() como obsoleto; se accede al DataFrame estatico
# de cada componente por su atributo.
ATRIBUTO = {'Line': 'lines', 'Transformer': 'transformers',
            'Generator': 'generators', 'Load': 'loads',
            'ShuntImpedance': 'shunt_impedances', 'Bus': 'buses'}


def _tabla(network, tipo):
    return getattr(network, ATRIBUTO[tipo])


V_MIN = 0.90
V_MAX = 1.10
CARGA_MAX_PCT = 100.0      # sobre s_eme tras la contingencia
SALIDA_CSV = salida('contingencias_n1.csv')


# ---------------------------------------------------------------------------
# Caso base
# ---------------------------------------------------------------------------

def caso_base(verbose=True, limites=None, fuente=None):
    """Construye y resuelve el caso base, y devuelve (red, diagnostico).

    Esta es la unica puerta de entrada al modelo para contingencias,
    agc_deslastre, cascada y deslastre_btm, asi que el desglose bruto
    (config.DESGLOSE_BRUTO) tiene que aplicarse AQUI y no solo en main.py: de
    lo contrario los modulos de fallas dimensionan el EDAC, la inercia y la
    reserva sobre una demanda neteada de 9.096 MW mientras el pipeline
    principal trabaja con 11.716.

    `limites` fuerza o impide la curva de capacidad de reactivo. Por omision
    (None) manda `config.LIMITES_REACTIVOS`. Pasar False sirve para obtener la
    red SIN limitar, que es lo que necesita `limites_reactivos.main()` para
    poder enseñar el antes y el despues: si recibiera la red ya conmutada,
    compararia el caso limitado consigo mismo y diria que no hay ninguna
    violacion que corregir.

    `fuente` dice DE DONDE sale el punto de operacion. Por omision, del caso
    de PowerFactory, que es lo que hacia esta funcion cuando no habia mas que
    una fuente posible; pasando `fuentes.xm_snapshot.FuenteXM(fecha, periodo)`
    sale del despacho y el pronostico de XM de esa fecha y ese periodo. Lo que
    NO cambia es nada de lo que viene despues: la red, el flujo y los cuatro
    modulos de fallas no saben de donde vinieron los numeros.
    """
    from stn.data_loader import load_lines
    from stn.network_builder import build_network
    from stn.fuentes.base import verificar_contrato
    from stn.power_flow import asignar_inyecciones, designar_slack, run_power_flow

    if fuente is None:
        from stn.fuentes.powerfactory import FuentePowerFactory
        fuente = FuentePowerFactory(verbose=False)

    lines_df = load_lines(verbose=False)
    network = build_network(lines_df, verbose=False)
    # Topologia que depende del punto (enlaces internacionales sin programa).
    # Va antes de asignar, para que las barras que se retiren no lleguen a
    # tener generador ni carga. Ver fuentes/base.py.
    fuente.preparar_red(network)
    inyecciones = fuente.inyecciones()
    desglose = fuente.desglose()
    verificar_contrato(fuente)
    asignar_inyecciones(network, inyecciones, verbose=False, desglose=desglose)
    designar_slack(network, verbose=False)
    ok, _ = run_power_flow(network, verbose=False)
    if not ok:
        raise RuntimeError("El caso base no converge; no tiene sentido "
                           "analizar contingencias sobre el.")

    # Curva de capacidad de reactivo. La decision esta centralizada; ver
    # limites_reactivos.aplicar_si_procede() para el porque.
    if limites is not False:
        from stn import limites_reactivos
        network = (limites_reactivos.aplicar(network, verbose=False)[0]
                   if limites is True
                   else limites_reactivos.aplicar_si_procede(network))

    diag = _estado(network)
    if verbose:
        print(f"Caso base: {len(network.buses)} buses, {len(network.lines)} lineas, "
              f"{len(network.transformers)} trafos"
              + ("  [desglose bruto]" if desglose is not None else ""))
        print(f"  demanda {diag['carga_MW']:.1f} MW | tension "
              f"[{diag['v_min']:.3f}, {diag['v_max']:.3f}] p.u. | "
              f"cargabilidad max {diag['carga_max_pct']:.1f}%")
        if diag['v_bajas'] or diag['v_altas']:
            print(f"  ya en el caso base hay {len(diag['v_bajas'])} barras bajas "
                  f"y {len(diag['v_altas'])} altas: {sorted(diag['v_bajas'])[:4]}")
    return network, diag


def _cargabilidad(network, emergencia=False):
    """Cargabilidad de lineas y trafos en % del limite que corresponda."""
    out = {}
    if len(network.lines) and len(network.lines_t.p0):
        s = np.hypot(network.lines_t.p0.iloc[0], network.lines_t.q0.iloc[0])
        lim = network.lines['s_eme'] if (emergencia and 's_eme' in network.lines) \
            else network.lines['s_nom']
        lim = lim.where(lim > 0, network.lines['s_nom'])
        pct = (s / lim.replace(0, np.nan) * 100).dropna()
        out.update(pct.to_dict())
    if len(network.transformers) and len(network.transformers_t.p0):
        s = np.hypot(network.transformers_t.p0.iloc[0],
                     network.transformers_t.q0.iloc[0])
        pct = (s / network.transformers['s_nom'].replace(0, np.nan) * 100).dropna()
        out.update(pct.to_dict())
    return pd.Series(out, dtype=float)


def _estado(network, emergencia=False):
    """Resumen del punto de operacion resuelto."""
    v = network.buses_t.v_mag_pu.iloc[0]
    pct = _cargabilidad(network, emergencia)
    return {
        'v': v,
        'v_min': float(v.min()),
        'v_max': float(v.max()),
        'v_bajas': set(v[v < V_MIN].index),
        'v_altas': set(v[v > V_MAX].index),
        'carga_pct': pct,
        'carga_max_pct': float(pct.max()) if len(pct) else 0.0,
        'sobrecargados': set(pct[pct > CARGA_MAX_PCT].index),
        'carga_MW': float(network.loads_t.p.iloc[0].sum()),
    }


# ---------------------------------------------------------------------------
# Simulacion de una contingencia
# ---------------------------------------------------------------------------

def _grafo(network):
    g = nx.Graph()
    g.add_nodes_from(network.buses.index)
    g.add_edges_from(zip(network.lines.bus0, network.lines.bus1))
    if len(network.transformers):
        g.add_edges_from(zip(network.transformers.bus0, network.transformers.bus1))
    return g


def _separar_islas(network):
    """Deja solo la isla principal y devuelve lo que se desconecto.

    La isla principal es la que concentra mas generacion. Las demas se quitan
    de la red para que su carga no acabe alimentada por un slack ficticio: lo
    que interesa de una separacion es precisamente cuanta demanda queda sin
    servicio.
    """
    comps = list(nx.connected_components(_grafo(network)))
    if len(comps) <= 1:
        return 0.0, 0.0, 0

    gen_por_isla = []
    for c in comps:
        g = network.generators[network.generators.bus.isin(c)].p_set.sum()
        gen_por_isla.append(float(g))
    principal = comps[int(np.argmax(gen_por_isla))]

    fuera = [b for c in comps if c is not principal for b in c]
    if not fuera:
        return 0.0, 0.0, len(comps)

    carga_perdida = float(
        network.loads[network.loads.bus.isin(fuera)].p_set.sum())
    gen_perdida = float(
        network.generators[network.generators.bus.isin(fuera)].p_set.sum())

    for comp, col in (('Generator', 'bus'), ('Load', 'bus'),
                      ('ShuntImpedance', 'bus')):
        df = _tabla(network, comp)
        if len(df):
            quitar = df[df[col].isin(fuera)].index.tolist()
            if quitar:
                network.remove(comp, quitar)
    for comp in ('Line', 'Transformer'):
        df = _tabla(network, comp)
        if len(df):
            quitar = df[df.bus0.isin(fuera) | df.bus1.isin(fuera)].index.tolist()
            if quitar:
                network.remove(comp, quitar)
    network.remove('Bus', fuera)
    return carga_perdida, gen_perdida, len(comps)


def simular(base, elementos, base_diag, verbose=False, con_limites=False):
    """Saca de servicio los elementos indicados y evalua el resultado.

    `elementos` es una lista de (tipo, nombre) con tipo en
    {'Line', 'Transformer', 'Generator'}.

    Con `con_limites=True` el caso se resuelve imponiendo la curva de capacidad
    de cada maquina, conmutando a PQ las que la excedan (ver
    limites_reactivos.py). Cambia lo que el barrido PUEDE encontrar: sin
    limites, las barras PV sostienen su tension con el reactivo que haga falta
    y el colapso de tension es imposible por construccion. Es mas lento,
    porque cada caso necesita varios flujos en vez de uno.
    """
    from stn.power_flow import designar_slack, run_power_flow

    n = base.copy()
    for tipo, nombre in elementos:
        if nombre in _tabla(n, tipo).index:
            n.remove(tipo, nombre)

    carga_perdida, gen_perdida, n_islas = _separar_islas(n)

    res = {
        'contingencia': ' + '.join(nm for _, nm in elementos),
        'tipo': '+'.join(sorted({t for t, _ in elementos})),
        'n_elementos': len(elementos),
        'islas': n_islas,
        'MW_desconectados': round(carga_perdida, 1),
        'MW_gen_perdida': round(gen_perdida, 1),
    }

    if not len(n.buses) or not len(n.generators):
        res.update({'convergio': False, 'motivo': 'red sin generacion',
                    'carga_max_pct': np.nan, 'n_sobrecargas': 0,
                    'v_min': np.nan, 'v_max': np.nan,
                    'n_v_bajas': 0, 'n_v_altas': 0, 'elemento_critico': ''})
        return res

    try:
        designar_slack(n, verbose=False)
        if con_limites:
            from stn.limites_reactivos import aplicar
            n, _info = aplicar(n, verbose=False)
            # Hacen falta LAS DOS. `convergio` es la del ultimo flujo; un lazo
            # PV/PQ que agota sus iteraciones oscilando deja un flujo
            # perfectamente convergido y una solucion que no es de equilibrio.
            # Leyendo solo la primera, un caso cuyo lazo nunca se asento se
            # reportaba como un N-1 limpio, que es justo lo contrario de para
            # lo que existe `--con-limites`.
            ok = _info['convergio'] and _info['convergio_lazo']
            res['gen_en_limite'] = len(_info['conmutados'])
            if not _info['convergio_lazo']:
                res['motivo'] = 'el lazo PV/PQ no se asento'
        else:
            ok, _ = run_power_flow(n, verbose=False)
    except Exception as e:                      # noqa: BLE001
        res.update({'convergio': False, 'motivo': f'{type(e).__name__}',
                    'carga_max_pct': np.nan, 'n_sobrecargas': 0,
                    'v_min': np.nan, 'v_max': np.nan,
                    'n_v_bajas': 0, 'n_v_altas': 0, 'elemento_critico': ''})
        return res

    if not ok:
        res.update({'convergio': False, 'motivo': 'no converge',
                    'carga_max_pct': np.nan, 'n_sobrecargas': 0,
                    'v_min': np.nan, 'v_max': np.nan,
                    'n_v_bajas': 0, 'n_v_altas': 0, 'elemento_critico': ''})
        return res

    est = _estado(n, emergencia=True)
    # solo lo que la contingencia anade respecto del caso base
    nuevas_bajas = est['v_bajas'] - base_diag['v_bajas']
    nuevas_altas = est['v_altas'] - base_diag['v_altas']
    critico = (est['carga_pct'].idxmax() if len(est['carga_pct']) else '')

    res.update({
        'convergio': True,
        'motivo': '',
        'carga_max_pct': round(est['carga_max_pct'], 1),
        'n_sobrecargas': len(est['sobrecargados']),
        'elemento_critico': critico,
        'v_min': round(est['v_min'], 4),
        'v_max': round(est['v_max'], 4),
        'n_v_bajas': len(nuevas_bajas),
        'n_v_altas': len(nuevas_altas),
    })
    return res


def _severidad(r):
    """Puntua la gravedad para poder ordenar los casos.

    El orden de importancia es: no converger, dejar carga sin servicio,
    sobrecargar equipos y por ultimo sacar tensiones de rango.
    """
    if not r['convergio']:
        return 1e9
    s = 0.0
    s += r['MW_desconectados'] * 100
    s += max(r['carga_max_pct'] - 100.0, 0.0) * 50
    s += r['n_sobrecargas'] * 500
    s += (r['n_v_bajas'] + r['n_v_altas']) * 100
    return s


# ---------------------------------------------------------------------------
# Barridos
# ---------------------------------------------------------------------------

def elementos_de(network, tipos=('Line', 'Transformer'), nivel=None, area=None):
    """Lista de (tipo, nombre) candidatos, con los filtros pedidos."""
    if area:
        from stn.zonas import area_por_bus
        mapa = area_por_bus(network)
        buses_area = {b for b, (_, a) in mapa.items() if a.lower() == area.lower()}
    out = []
    for tipo in tipos:
        df = _tabla(network, tipo)
        for nombre in df.index:
            if nivel is not None:
                bus = df.at[nombre, 'bus0'] if 'bus0' in df.columns \
                    else df.at[nombre, 'bus']
                if float(network.buses.at[bus, 'v_nom']) != float(nivel):
                    continue
            if area:
                buses = [df.at[nombre, c] for c in ('bus0', 'bus1', 'bus')
                         if c in df.columns]
                if not any(b in buses_area for b in buses):
                    continue
            out.append((tipo, nombre))
    return out


def barrido(base, base_diag, candidatos, orden=1, verbose=True,
            con_limites=False):
    """Ejecuta las contingencias y devuelve un DataFrame ordenado."""
    casos = (list(itertools.combinations(candidatos, orden)) if orden > 1
             else [(c,) for c in candidatos])
    if verbose:
        print(f"\nSimulando {len(casos)} contingencias de orden {orden}...")
    t0 = time.time()
    filas = []
    for i, caso in enumerate(casos, 1):
        filas.append(simular(base, list(caso), base_diag,
                             con_limites=con_limites))
        if verbose and (i % 25 == 0 or i == len(casos)):
            print(f"  {i}/{len(casos)}  ({time.time()-t0:.0f} s)", end='\r')
    if verbose:
        print(f"  {len(casos)}/{len(casos)}  ({time.time()-t0:.0f} s)")
    d = pd.DataFrame(filas)
    d['severidad'] = d.apply(_severidad, axis=1)
    return d.sort_values('severidad', ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Informe
# ---------------------------------------------------------------------------

def _es_trafo(nombre):
    """Los autotransformadores se nombran `trafo_<SUB>_<alta>_<baja>`."""
    return str(nombre).startswith('trafo_')


def _desglose_sobrecargas(d, con_sobre, base_diag):
    """Separa lineas de trafos, y lo heredado de lo que causa la contingencia.

    POR QUE EXISTE. Sin esto, un caso base que ya arranca con un elemento por
    encima del 100 % hace que el barrido informe "289 de 292 sobrecargan algo"
    ---y lo que ocurre es que las 289 heredan EL MISMO trafo---. El usuario lee
    que casi todo el sistema falla cuando en realidad falla una cosa, siempre
    la misma, y ya antes de la contingencia. Caso real: el 2026-10-24 p13
    arranca con BACATA 500/230 al 109,2 % y NUEVA ESPERANZA al 105,8 %.

    El criterio del N-1 es lo que la contingencia EMPEORA. Lo que ya estaba
    por encima no es un hallazgo del barrido: es un problema del punto de
    operacion, y hay que decirlo por separado y una sola vez.
    """
    previos = set(base_diag.get('sobrecargados', ()) or ())
    pct0 = base_diag.get('carga_pct')

    if previos:
        print()
        print(f"  [!] EL CASO BASE YA ARRANCA CON {len(previos)} ELEMENTO(S) "
              f"SOBRE EL 100 %:")
        for e in sorted(previos):
            v = f"{float(pct0[e]):.1f} %" if pct0 is not None and e in pct0 \
                else 'n/d'
            print(f"        {e:42s} {v}")
        print(f"      Toda contingencia los hereda, asi que el recuento de "
              f"arriba los suma una vez por")
        print(f"      caso. NO son {len(con_sobre)} problemas distintos: son "
              f"{len(previos)}, y vienen del PUNTO DE")
        print(f"      OPERACION, no del barrido. Revisa el caso base antes de "
              f"leer el N-1.")

    if not len(con_sobre):
        print("\n  lineas sobrecargadas : NO")
        print("  trafos sobrecargados : NO")
        return

    crit = con_sobre['elemento_critico'].dropna().astype(str)
    lineas = crit[~crit.map(_es_trafo)]
    trafos = crit[crit.map(_es_trafo)]

    for etq, serie in (('lineas sobrecargadas', lineas),
                       ('trafos sobrecargados', trafos)):
        if not len(serie):
            print(f"\n  {etq} : NO")
            continue
        cuenta = serie.value_counts()
        print(f"\n  {etq} : SI, en {len(serie)} contingencias   "
              f"({cuenta.nunique() if False else len(cuenta)} elementos "
              f"distintos)")
        for nombre, n in cuenta.head(6).items():
            marca = '  [ya sobrecargado en el caso base]' if nombre in previos \
                else ''
            print(f"        {nombre:42s} en {n:4d} casos{marca}")
        if len(cuenta) > 6:
            print(f"        ... y {len(cuenta)-6} elementos mas")
        # Si uno solo explica casi todo, decirlo con todas las letras.
        if len(cuenta) and cuenta.iloc[0] / len(serie) > 0.8:
            print(f"      -> {100*cuenta.iloc[0]/len(serie):.0f} % de esos "
                  f"casos son SIEMPRE el mismo elemento: {cuenta.index[0]}")


def informe(d, base_diag, top=20, con_limites=False, red=None):
    """Imprime el resultado del barrido.

    `con_limites` NO es decorativo: decide si el recuento de tensiones fuera
    de rango significa algo. Sin limites de reactivo las barras PV sostienen
    su tension con el reactivo que haga falta, asi que el colapso de tension
    es IMPOSIBLE POR CONSTRUCCION y el "0 tensiones fuera de rango" no es un
    hallazgo, es el montaje. `simular()` ya lo decia en su docstring, pero
    quien lee la salida no ve docstrings: el aviso tiene que viajar con el
    numero, no con el codigo.
    """
    n = len(d)
    no_conv = d[~d.convergio]
    con_isla = d[d.MW_desconectados > 0.1]
    con_sobre = d[d.n_sobrecargas > 0]
    con_v = d[(d.n_v_bajas + d.n_v_altas) > 0]
    seguras = d[(d.convergio) & (d.MW_desconectados <= 0.1) &
                (d.n_sobrecargas == 0) & ((d.n_v_bajas + d.n_v_altas) == 0)]

    print("\n" + "=" * 72)
    print(f" RESULTADO: {n} contingencias simuladas")
    print("=" * 72)
    print(f"  sin problemas                    : {len(seguras):4d}  "
          f"({100*len(seguras)/max(n,1):.0f}%)")
    print(f"  el flujo no converge             : {len(no_conv):4d}")
    print(f"  dejan carga sin servicio         : {len(con_isla):4d}  "
          f"(hasta {d.MW_desconectados.max():.1f} MW)")
    print(f"  sobrecargan algun elemento       : {len(con_sobre):4d}")
    print(f"  sacan alguna tension de rango    : {len(con_v):4d}"
          + ("" if con_limites else "   <-- ver el aviso de abajo"))

    _desglose_sobrecargas(d, con_sobre, base_diag)

    if len(d[d.convergio]):
        cmax = d.carga_max_pct.max()
        print(f"\n  cargabilidad maxima alcanzada (sobre limite de emergencia): "
              f"{cmax:.1f}%")
        # El limite de emergencia es 1,30 veces el nominal en la mediana de
        # las lineas del modelo. Un 90 % de ese limite suena holgado y son
        # 117 % del nominal: conviene decirlo al lado, no en una nota.
        if red is not None and len(red.lines):
            rel = (red.lines.s_eme / red.lines.s_nom.replace(0, np.nan))
            med = float(rel.replace([np.inf, -np.inf], np.nan).dropna().median())
            print(f"     equivale a ~{cmax*med:.0f}% del limite NOMINAL "
                  f"(el de emergencia es {med:.2f}x el nominal en la mediana)")

    # --- bajo que regimen de reactivo se obtuvo todo lo anterior ----------
    print()
    if con_limites:
        print("  Reactivo: curva de capacidad IMPUESTA (PV->PQ donde se "
              "excede).")
        print("  El recuento de tensiones fuera de rango es un resultado "
              "fisico.")
    else:
        print("  [!] Reactivo ILIMITADO en las barras PV: cada generador "
              "sostiene su tension")
        print("      con el reactivo que haga falta, sin curva de capacidad. "
              "Bajo ese regimen")
        print("      el COLAPSO DE TENSION ES IMPOSIBLE POR CONSTRUCCION, "
              "asi que el recuento")
        print("      de tensiones fuera de rango NO es un hallazgo del "
              "sistema: es el montaje.")
        print("      Para un barrido con significado fisico: "
              "--con-limites (mas lento).")

    peores = d.head(top)
    if len(peores):
        print(f"\n  Las {len(peores)} contingencias mas severas:")
        cols = ['contingencia', 'convergio', 'MW_desconectados',
                'carga_max_pct', 'n_sobrecargas', 'v_min', 'n_v_bajas']
        vista = peores[cols].copy()
        vista['contingencia'] = vista.contingencia.str.slice(0, 46)
        print(vista.to_string(index=False))

    if len(con_sobre):
        print("\n  Elementos que mas veces quedan sobrecargados:")
        print(con_sobre.elemento_critico.value_counts().head(8).to_string())


def main():
    ap = argparse.ArgumentParser(
        description="Analisis de contingencias N-1 sobre el modelo del STN.")
    ap.add_argument('--tipo', choices=['linea', 'trafo', 'generador', 'todos'],
                    default='todos', help="que elementos sacar de servicio")
    ap.add_argument('--nivel', type=float, default=None,
                    help="filtrar por nivel de tension (230 o 500)")
    ap.add_argument('--area', default=None,
                    help="filtrar por area operativa (Caribe, Antioquia...)")
    ap.add_argument('--n2', action='store_true',
                    help="dobles contingencias en vez de N-1")
    ap.add_argument('--caso', nargs='+', default=None,
                    help="simular solo estos elementos, por nombre")
    ap.add_argument('--top', type=int, default=20, help="cuantas mostrar")
    ap.add_argument('--con-limites', action='store_true',
                    help="imponer la curva de capacidad de cada generador "
                         "(conmutacion PV->PQ); mas lento pero permite "
                         "detectar colapsos de tension")
    ap.add_argument('--csv', default=None, help="fichero de salida")
    from stn import punto_operacion as PO
    PO.anadir_flag(ap)
    args = ap.parse_args()

    base, base_diag, ctx = PO.de_args(args, verbose=True)
    PO.cabecera(ctx)
    # El CSV lleva el sufijo del caso salvo que se pida uno concreto. Si los
    # dos barridos escribieran en contingencias_n1.csv, el segundo borraria
    # al primero y la comparacion entre casos seria imposible de reconstruir.
    csv = args.csv or salida(f'contingencias_n1{ctx["sufijo"]}.csv')

    if args.caso:
        elementos = []
        for nombre in args.caso:
            for tipo in ('Line', 'Transformer', 'Generator'):
                if nombre in _tabla(base, tipo).index:
                    elementos.append((tipo, nombre))
                    break
            else:
                print(f"AVISO: '{nombre}' no existe en la red")
        if not elementos:
            return 1
        print(f"\nSimulando la salida simultanea de {len(elementos)} elementos:")
        for _, nm in elementos:
            print(f"  - {nm}")
        r = simular(base, elementos, base_diag)
        print()
        for k, v in r.items():
            print(f"  {k:20s}: {v}")
        return 0

    tipos = {'linea': ('Line',), 'trafo': ('Transformer',),
             'generador': ('Generator',),
             'todos': ('Line', 'Transformer')}[args.tipo]
    candidatos = elementos_de(base, tipos, args.nivel, args.area)
    if not candidatos:
        print("Ningun elemento cumple los filtros.")
        return 1

    if args.n2 and len(candidatos) > 60:
        print(f"AVISO: {len(candidatos)} elementos dan "
              f"{len(candidatos)*(len(candidatos)-1)//2} pares. "
              f"Acota con --nivel o --area.")
        return 1

    d = barrido(base, base_diag, candidatos, orden=2 if args.n2 else 1,
                con_limites=args.con_limites)
    informe(d, base_diag, args.top, con_limites=args.con_limites, red=base)
    d.to_csv(csv, index=False, encoding='utf-8-sig')
    print(f"\nResultado completo en '{csv}'")
    return 0


if __name__ == '__main__':
    sys.exit(main())
