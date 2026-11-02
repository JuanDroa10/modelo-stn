# test_regresion_pf.py
"""Regresion numerica del caso de PowerFactory contra el golden congelado.

    python test_regresion_pf.py            # todo
    python test_regresion_pf.py -k tension # solo lo que case con 'tension'

POR QUE EXISTE. `test_invariantes.py` dice en su cabecera, y con razon, que NO
es una prueba de regresion numerica: fija lo que no puede cambiar sin que algo
este mal, no los numeros. Y `validacion.py` imprime la correlacion con
PowerFactory pero no la afirma: no tiene un solo `assert`. Entre las dos, hoy
nadie impide que un cambio mueva la correlacion de 0,80 a 0,60 en silencio.

Esto importa ahora porque el punto de operacion va a dejar de tener una sola
fuente. Mientras la fuente sea el reporte de PowerFactory, el resultado tiene
que ser el de siempre, al digito. Esta prueba es la que lo exige.

QUE HACER CUANDO FALLE. El orden de las pruebas es el orden del diagnostico:
entradas, red, dinamica, metricas. La PRIMERA que falla es la que explica a
las demas; arreglar la ultima sin mirar la primera es perder la tarde.

  * si falla `el_golden_sigue_siendo_del_mismo_caso` -> cambio un fichero de
    entrada o un flag de config. El golden esta obsoleto, no roto.
  * si falla una de entradas -> cambio `integrar` o `desagregacion`.
  * si solo fallan las de red -> cambio la construccion o el flujo.
  * si solo fallan las de dinamica -> cambio `agc_deslastre` o PARATEC.

Si el cambio es DELIBERADO, se recaptura:

    python golden_pf.py

y el commit explica que se movio y por que. Recapturar para que deje de fallar
algo que no se entiende convierte esta prueba en decoracion.
"""

import sys

import numpy as np
import pandas as pd

from tools import golden_pf


# Tolerancias por nivel. No son un numero global a ojo: cada una sale de como
# se calcula la magnitud que cubre.
#
#   ENTRADAS   aritmetica de pandas, sin solver de por medio. Si esto se mueve
#              un apice es que cambio el dato o la formula, no el redondeo.
#   TENSION    el Newton-Raphson converge con config.PF_TOLERANCE = 1e-6.
#              Pedir mas que eso seria pedirle al solver lo que no promete.
#   FLUJO      1e-3 MW sobre corredores de 100-1.000 MW es 1e-6 relativo, el
#              mismo orden que la tolerancia del flujo.
#   ESCALAR    relativo, porque S_base (1e4) y R_eq (1e-2) no admiten la misma
#              tolerancia absoluta.
TOL_ENTRADA = 1e-9
TOL_INVARIANTE = 1e-6      # el mismo que usa test_invariantes
TOL_TENSION = 1e-6
TOL_ANGULO = 1e-5          # grados
TOL_FLUJO = 1e-3           # MW / Mvar
TOL_CARGA_PCT = 1e-3
TOL_ESCALAR_REL = 1e-6
TOL_METRICA = 1e-6

_CACHE = {}


def golden():
    """El congelado. Se lee una vez."""
    if 'g' not in _CACHE:
        try:
            _CACHE['g'] = golden_pf.cargar()
        except FileNotFoundError as e:
            raise AssertionError(
                f"no hay golden en {golden_pf.DIR_GOLDEN}/ ({e.filename}). "
                "Capturalo con:  python golden_pf.py") from e
    return _CACHE['g']


def actual():
    """El caso de hoy, por la puerta canonica. Cuesta ~10 s."""
    if 'n' not in _CACHE:
        import logging
        logging.disable(logging.INFO)
        from stn import contingencias as C
        _CACHE['iny'], _CACHE['des'], _ = golden_pf._entradas()
        _CACHE['n'], _ = C.caso_base(verbose=False)
    return _CACHE['n']


# ---------------------------------------------------------------------------
# Utilidades de comparacion
# ---------------------------------------------------------------------------

def _mismo_indice(viejo, nuevo, que):
    """Falla si aparecieron o desaparecieron filas.

    Va antes que cualquier comparacion de valores: si el indice cambio, un
    diff de valores senala filas al azar y oculta la causa.
    """
    v, n = set(viejo.index), set(nuevo.index)
    faltan, sobran = sorted(v - n), sorted(n - v)
    assert not faltan and not sobran, (
        f"{que}: el indice cambio respecto al golden "
        f"({len(v)} -> {len(n)}). "
        + (f"desaparecen {faltan[:6]}{'...' if len(faltan) > 6 else ''}. "
           if faltan else "")
        + (f"aparecen {sobran[:6]}{'...' if len(sobran) > 6 else ''}."
           if sobran else ""))


def _comparar(viejo, nuevo, cols, tol, que):
    """Compara columnas numericas con tolerancia absoluta."""
    _mismo_indice(viejo, nuevo, que)
    n = nuevo.reindex(viejo.index)
    peor = []
    for c in cols:
        if c not in viejo.columns:
            continue
        a = pd.to_numeric(viejo[c], errors='coerce')
        b = pd.to_numeric(n[c], errors='coerce')
        d = (b - a).abs()
        # NaN en ambos lados es igualdad; NaN en uno solo es una diferencia.
        d = d.where(~(a.isna() & b.isna()), 0.0)
        d = d.where(~(a.isna() ^ b.isna()), np.inf)
        if d.max() > tol:
            k = d.idxmax()
            peor.append(f"{c}: {d.max():.3e} en {k!r} "
                        f"({a[k]:.6f} -> {b[k]:.6f}); "
                        f"{int((d > tol).sum())} filas fuera")
    assert not peor, f"{que} se movio (tol {tol:g}):\n    " + \
                     "\n    ".join(peor)


def _rel(a, b):
    """Diferencia relativa, con guarda para magnitudes casi nulas."""
    escala = max(abs(a), abs(b), 1e-12)
    return abs(b - a) / escala


# ---------------------------------------------------------------------------
# 0. Que el golden siga hablando del mismo caso
# ---------------------------------------------------------------------------

def test_el_golden_sigue_siendo_del_mismo_caso():
    """Los ficheros de entrada y los flags no han cambiado bajo los pies.

    Sin esto, cambiar `Datos/pq_completo.csv` por otro export haria fallar
    TODAS las demas pruebas, y el mensaje apuntaria al ultimo commit de
    codigo en vez de al cambio de dato.
    """
    meta, _ = golden()

    cambiados = golden_pf.entrada_cambiada(meta)
    assert not cambiados, (
        "el golden es de otro dato: han cambiado " + ", ".join(cambiados) +
        ". No es una regresion del modelo. Si el cambio de dato es el que "
        "quieres, recaptura con:  python golden_pf.py")

    from stn import config
    difs = [f"{k}: {v!r} -> {getattr(config, k)!r}"
            for k, v in meta['flags_config'].items()
            if getattr(config, k, None) != v]
    assert not difs, (
        "el golden se capturo con otros flags de config:\n    " +
        "\n    ".join(difs))


def test_las_versiones_de_libreria_son_las_del_golden():
    """Aviso, no veredicto: PyPSA y numpy pueden mover el ultimo digito.

    Se separa de la prueba anterior a proposito. Un cambio de version NO
    invalida el golden ---las tolerancias estan puestas para absorberlo---
    pero si varias pruebas de red fallan por poco, esto es lo primero que hay
    que mirar.
    """
    meta, _ = golden()
    difs = [f"{k}: {v} -> {n}"
            for k, v in meta['versiones'].items()
            for n in [golden_pf._versiones().get(k)] if n != v]
    # AVISA, no falla, y el docstring de arriba siempre dijo que asi debia
    # ser; era la implementacion la que no lo cumplia. La diferencia se nota
    # fuera del entorno de desarrollo: quien clone el repositorio instalara
    # las versiones del dia, que casi nunca seran las del congelado, y veria
    # "1 de 18 fallan" en una instalacion perfectamente sana.
    #
    # Quien decide si el modelo se movio son las otras 17 pruebas, que
    # comparan NUMEROS. Esta solo explica por que, si lo hicieron. Hacerla
    # fallar por su cuenta es confundir el diagnostico con el sintoma.
    if difs:
        print("  aviso el entorno no es el del golden:")
        for d in difs:
            print(f"            {d}")
        print("            Si las pruebas numericas pasan, el modelo da lo "
              "mismo con estas versiones.")


# ---------------------------------------------------------------------------
# 1. Entradas: lo que la fuente nueva tendra que reproducir
# ---------------------------------------------------------------------------

def test_las_inyecciones_son_las_del_golden():
    """P, Q y v_set por bus. Es el objeto que cambia de origen en la fase 2."""
    _, t = golden()
    actual()
    _comparar(t['inyecciones'], _CACHE['iny'], ['P', 'Q', 'v_set'],
              TOL_ENTRADA, "inyecciones")


def test_el_desglose_bruto_es_el_del_golden():
    """Generacion y carga brutas por bus."""
    _, t = golden()
    actual()
    _comparar(t['desglose'], _CACHE['des'], ['gen', 'carga'],
              TOL_ENTRADA, "desglose bruto")


def test_el_desglose_sigue_conservando_la_inyeccion_neta():
    """Gen(b) - Carga(b) = P_iny(b), ahora tambien sobre el golden.

    `test_invariantes` ya lo comprueba sobre el caso de hoy. Aqui se
    comprueba que el GOLDEN lo cumple, porque un golden que no lo cumpliera
    seria una referencia invalida y haria fallar todo lo demas sin motivo.
    """
    _, t = golden()
    iny, des = t['inyecciones'], t['desglose']
    if des['gen'].isna().all():
        return                      # se capturo con DESGLOSE_BRUTO en False
    d = (des['gen'] - des['carga'] - iny['P']).abs()
    assert d.max() < TOL_INVARIANTE, (
        f"el golden no conserva la inyeccion neta: {d.max():.3e} MW en "
        f"{d.idxmax()!r}. El congelado es invalido; recapturalo.")


# ---------------------------------------------------------------------------
# 2. Red resuelta
# ---------------------------------------------------------------------------

def test_la_red_tiene_la_misma_forma():
    """Conteos y reparto de control. Lo primero que se rompe al tocar la red."""
    meta, _ = golden()
    n = actual()
    c = meta['conteos']
    reales = {'buses': len(n.buses), 'generadores': len(n.generators),
              'cargas': len(n.loads)}
    difs = [f"{k}: {c[k]} -> {v}" for k, v in reales.items() if c[k] != v]

    control = {str(k): int(v)
               for k, v in n.generators.control.value_counts().to_dict().items()}
    if control != c['control']:
        difs.append(f"control: {c['control']} -> {control}")
    assert not difs, "la red cambio de forma:\n    " + "\n    ".join(difs)


def test_las_tensiones_son_las_del_golden():
    _, t = golden()
    n = actual()
    v = pd.DataFrame({'v_mag_pu': n.buses_t.v_mag_pu.iloc[0]})
    _comparar(t['buses'], v, ['v_mag_pu'], TOL_TENSION, "tensiones")


def test_los_angulos_son_los_del_golden():
    _, t = golden()
    n = actual()
    a = pd.DataFrame({'v_ang': n.buses_t.v_ang.iloc[0]})
    _comparar(t['buses'], a, ['v_ang'], TOL_ANGULO, "angulos")


def test_los_flujos_por_rama_son_los_del_golden():
    """P y Q en ambos extremos de lineas y trafos, y la cargabilidad."""
    _, t = golden()
    n = actual()
    from stn import contingencias as C
    carga_pct = C._cargabilidad(n)
    filas = []
    for tabla, serie_t in ((n.lines, n.lines_t),
                           (n.transformers, n.transformers_t)):
        if not len(tabla) or not len(serie_t.p0):
            continue
        filas.append(pd.DataFrame({
            'p0': serie_t.p0.iloc[0], 'q0': serie_t.q0.iloc[0],
            'p1': serie_t.p1.iloc[0],
            'carga_pct': carga_pct.reindex(tabla.index)}))
    ramas = pd.concat(filas)
    _comparar(t['ramas'], ramas, ['p0', 'q0', 'p1'], TOL_FLUJO, "flujos")
    _comparar(t['ramas'], ramas, ['carga_pct'], TOL_CARGA_PCT, "cargabilidad")


def test_el_despacho_resuelto_es_el_del_golden():
    """p_set programado y P/Q resueltos por generador."""
    _, t = golden()
    n = actual()
    g = pd.DataFrame({'p_set': n.generators.p_set,
                      'p': n.generators_t.p.iloc[0],
                      'q': n.generators_t.q.iloc[0]})
    _comparar(t['generadores'], g, ['p_set'], TOL_ENTRADA, "p_set de los generadores")
    _comparar(t['generadores'], g, ['p', 'q'], TOL_FLUJO, "despacho resuelto")


def test_las_cargas_son_las_del_golden():
    _, t = golden()
    n = actual()
    c = pd.DataFrame({'p_set': n.loads.p_set, 'q_set': n.loads.q_set})
    _comparar(t['cargas'], c, ['p_set', 'q_set'], TOL_ENTRADA, "cargas")


def test_las_perdidas_y_el_diagnostico_son_los_del_golden():
    """Las cifras que se publican del caso: demanda, tension, cargabilidad."""
    meta, _ = golden()
    n = actual()
    from stn import contingencias as C
    d = C._estado(n)
    esperado = meta['diagnostico']
    difs = []
    for k, tol in (('v_min', TOL_TENSION), ('v_max', TOL_TENSION),
                   ('carga_max_pct', TOL_CARGA_PCT), ('carga_MW', TOL_FLUJO)):
        if abs(d[k] - esperado[k]) > tol:
            difs.append(f"{k}: {esperado[k]:.6f} -> {d[k]:.6f}")
    for k, real in (('n_v_bajas', len(d['v_bajas'])),
                    ('n_v_altas', len(d['v_altas'])),
                    ('n_sobrecargados', len(d['sobrecargados']))):
        if esperado[k] != real:
            difs.append(f"{k}: {esperado[k]} -> {real}")
    assert not difs, "el diagnostico del caso cambio:\n    " + \
                     "\n    ".join(difs)


# ---------------------------------------------------------------------------
# 3. Dinamica: de aqui cuelgan el EDAC y el RoCoF
# ---------------------------------------------------------------------------

def test_los_escalares_de_la_dinamica_son_los_del_golden():
    """S_base, H, reserva, R_eq... Es lo que mas se movera al cambiar fuente.

    Se comparan todos los escalares que publica `Sistema`, no una seleccion:
    el que se olvide de la lista es justo el que se movera sin que nadie lo
    vea.
    """
    meta, _ = golden()
    n = actual()
    from stn import agc_deslastre as AGC
    sis = AGC.Sistema(n)
    real = {k: float(v) for k, v in vars(sis).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}
    real['H_por_S_base'] = float(sis.H * sis.S_base)

    esperado = meta['dinamica']
    faltan = sorted(set(esperado) - set(real))
    assert not faltan, f"Sistema ya no publica {faltan}"

    difs = [f"{k}: {v:.6g} -> {real[k]:.6g} ({_rel(v, real[k]):.2e} rel)"
            for k, v in sorted(esperado.items())
            if _rel(v, real[k]) > TOL_ESCALAR_REL]
    assert not difs, ("los escalares de la dinamica se movieron "
                      f"(tol {TOL_ESCALAR_REL:g} relativo):\n    " +
                      "\n    ".join(difs))


def test_el_reparto_por_tecnologia_es_el_del_golden():
    """MW por tecnologia. La 'desconocida' es la que degrada el dato de H."""
    meta, _ = golden()
    n = actual()
    from stn import agc_deslastre as AGC
    sis = AGC.Sistema(n)
    real = {str(k): float(v)
            for k, v in sis.gen.groupby('tipo').p_set.sum().to_dict().items()}
    esperado = meta['mw_por_tipo']

    difs = [f"{k}: {esperado.get(k, 0.0):.1f} -> {real.get(k, 0.0):.1f} MW"
            for k in sorted(set(esperado) | set(real))
            if abs(esperado.get(k, 0.0) - real.get(k, 0.0)) > TOL_FLUJO]
    assert not difs, "el reparto por tecnologia cambio:\n    " + \
                     "\n    ".join(difs)


def test_los_parametros_por_generador_son_los_del_golden():
    """H_real y estatismo por maquina, de donde salen H y R_eq agregados."""
    _, t = golden()
    n = actual()
    from stn import agc_deslastre as AGC
    sis = AGC.Sistema(n)
    _comparar(t['dinamica_por_generador'], sis.gen,
              ['p_set', 'p_nom', 'placa_declarada', 'n_maquinas', 'H_real',
               'estatismo_real_pct', 's_sinc'],
              TOL_FLUJO, "parametros por generador")


# ---------------------------------------------------------------------------
# 4. Metricas contra PowerFactory
# ---------------------------------------------------------------------------

def test_el_contraste_difuso_es_el_del_golden():
    """Correlacion y MAE sobre las lineas emparejadas por nombre.

    Es la cifra que se cita en la tesis y en los comentarios de config.py
    (0,8048 y 23,38 MW tras el arreglo de MIEL/PORCE). Hasta ahora no la
    defendia nada.
    """
    meta, _ = golden()
    n = actual()
    from stn import validacion as V
    cmp, n_mio, n_pf = V.comparar(n)
    d = cmp.dif_MW.abs()
    real = {
        'lineas_modelo': int(n_mio), 'lineas_reporte': int(n_pf),
        'comparables': int(len(cmp)),
        'mae_mw': float(d.mean()), 'mediana_mw': float(d.median()),
        'correlacion': float(np.corrcoef(cmp.PyPSA_MW,
                                         cmp.PowerFactory_MW)[0, 1]),
        'dentro_10_pct': float(100 * (d < 10).mean()),
        'dentro_25_pct': float(100 * (d < 25).mean()),
        'dentro_50_pct': float(100 * (d < 50).mean()),
        'dentro_100_pct': float(100 * (d < 100).mean()),
    }
    esperado = meta['metricas']['difuso']
    difs = [f"{k}: {v} -> {real[k]}" for k, v in sorted(esperado.items())
            if (v != real[k] if isinstance(v, int)
                else abs(v - real[k]) > TOL_METRICA)]
    assert not difs, "el contraste difuso con PowerFactory cambio:\n    " + \
                     "\n    ".join(difs)


def test_el_contraste_estricto_es_el_del_golden():
    """Par de subestaciones + circuito + nivel, sobre Edge Elements."""
    meta, _ = golden()
    esperado = meta['metricas']['estricto']
    if esperado is None:
        return                      # se capturo sin Datos/edge_2034.csv
    n = actual()
    from stn import validacion as V
    edge = V.contraste_edge(n, verbose=False)
    e = edge.dif_MW.abs()
    real = {'emparejadas': int(len(edge)), 'mae_mw': float(e.mean()),
            'correlacion': float(edge.PowerFactory_MW.corr(edge.PyPSA_MW)),
            'dentro_10': int((e < 10).sum()), 'dentro_25': int((e < 25).sum())}
    difs = [f"{k}: {v} -> {real[k]}" for k, v in sorted(esperado.items())
            if (v != real[k] if isinstance(v, int)
                else abs(v - real[k]) > TOL_METRICA)]
    assert not difs, "el contraste estricto cambio:\n    " + \
                     "\n    ".join(difs)


def test_las_lineas_comparadas_una_a_una_son_las_del_golden():
    """El detalle, no solo el agregado.

    Una media se conserva mientras dos lineas se compensan. Esto lo impide.
    """
    _, t = golden()
    n = actual()
    from stn import validacion as V
    cmp, _, _ = V.comparar(n)
    _comparar(t['contraste_difuso'], cmp, ['PyPSA_MW', 'PowerFactory_MW'],
              0.05, "lineas del contraste")     # el golden guarda 1 decimal


# ---------------------------------------------------------------------------

def main():
    filtro = None
    if '-k' in sys.argv:
        filtro = sys.argv[sys.argv.index('-k') + 1].lower()

    g = globals()
    pruebas = [(k, v) for k, v in g.items()
               if k.startswith('test_') and callable(v)
               and (filtro is None or filtro in k.lower())]
    if not pruebas:
        print("ninguna prueba coincide con el filtro")
        return 1

    print(f"{len(pruebas)} pruebas de regresion contra "
          f"{golden_pf.DIR_GOLDEN}/\n")
    fallos = []
    for nombre, fn in pruebas:
        etq = nombre[5:].replace('_', ' ')
        try:
            fn()
            print(f"  OK    {etq}")
        except AssertionError as e:
            print(f"  FALLA {etq}\n          {e}")
            fallos.append((nombre, str(e)))
        except Exception as e:                               # noqa: BLE001
            print(f"  ERROR {etq}\n          {type(e).__name__}: {e}")
            fallos.append((nombre, f"{type(e).__name__}: {e}"))

    print()
    if fallos:
        print(f"{len(fallos)} de {len(pruebas)} fallan")
        return 1
    print(f"las {len(pruebas)} pasan: el caso de PowerFactory no se ha movido")
    return 0


if __name__ == '__main__':
    sys.exit(main())
