# validacion.py
"""Compara el flujo de carga resuelto en PyPSA con el que reporta
PowerFactory en el mismo escenario.

Es la comprobacion que cierra el circulo: si el modelo reconstruido en PyPSA
reproduce los flujos de linea del estudio original, entonces la topologia,
los parametros de linea y las inyecciones equivalentes son correctos.

Uso:
    python validacion.py
"""

import re

import numpy as np
import pandas as pd

from stn import config
from stn.config import FILE_PQ, STN_KV_MIN
from stn.data_loader import load_lines, clean_bus_name
from stn.network_builder import build_network
from stn.data_integration import integrar
from stn.power_flow import asignar_inyecciones, designar_slack, run_power_flow
from stn.paths import PUNTO, dato

_SOLO_DIGITOS = re.compile(r'^\d+$')


def clave_linea(nombre):
    """Clave comparable a partir del nombre de una linea.

    El CSV de lineas escribe "ALTO ANCHICAYA - PANCE 1 230 kV" y el reporte
    de PowerFactory "Alto Anchicaya - Pance 1 230". La limpieza de nombres
    quita el sufijo "1 230 kV" del primero pero no del segundo, asi que se
    descartan todos los tokens puramente numericos (numero de circuito y
    tension) y se ordenan los extremos para que el orden no importe.

    Los circuitos paralelos de un mismo corredor comparten clave; por eso
    ambos lados se agregan tomando el flujo maximo del corredor. Los numeros
    romanos de los nombres ("PORCE III", "MIEL I") se conservan porque no son
    digitos.
    """
    s = clean_bus_name(nombre)
    toks = [t for t in re.split(r'[^A-Z0-9]+', s) if t]
    toks = [t for t in toks if t != 'KV' and not _SOLO_DIGITOS.match(t)]
    return ' '.join(sorted(toks))


def flujos_powerfactory(path=FILE_PQ, solo_stn=True):
    """Magnitud del flujo de cada linea segun el reporte.

    Cada linea aparece dos veces (una por extremo) con signo opuesto; se toma
    el valor absoluto maximo, que es el flujo en el extremo de envio.

    Se filtra a las lineas que cuelgan de barras del STN. Sin ese filtro, el
    emparejamiento por prefijo cruzaba niveles de tension y producia falsos
    positivos: la linea de 500 kV CERROMATOSO-PRIMAVERA del modelo se casaba
    con "Cerromatoso - P. Rica 1 110", que es otra linea, y aparecia como una
    desviacion del 371% que no existia.
    """
    df = pd.read_csv(path, encoding='utf-8-sig')
    df['P'] = pd.to_numeric(df['Active Power MW'], errors='coerce')
    df['kv'] = pd.to_numeric(df['Nominal Line-Line Voltage kV'], errors='coerce')
    lineas = df[df['Element type'] == 'Line'].dropna(subset=['P']).copy()
    if solo_stn:
        lineas = lineas[lineas.kv >= STN_KV_MIN]
    lineas['k'] = lineas['Name'].map(clave_linea)
    return lineas.groupby('k')['P'].apply(lambda s: s.abs().max())


def resolver_modelo():
    """Resuelve el mismo caso que main.py, desglose bruto incluido.

    Hasta el 19-9-2026 esta funcion NO aplicaba el desglose, y de ahi salia la
    afirmacion de que activarlo dejaba la validacion "identica al digito"
    (0,8093 y 22,60 MW): se estaba midiendo siempre la red sin desglosar. Con
    el flag cableado aqui, el contraste da 0,8107 y 22,48 MW.

    Que se mueva algo NO rompe el invariante. `Gen(b) - Carga(b) = P_iny(b)`
    se sigue cumpliendo bus a bus con 1e-6 de tolerancia, pero conservar la
    inyeccion neta no basta para congelar el flujo AC: poner un generador en
    una barra la convierte de PQ en PV (28 -> 43 barras), y una barra PV
    sostiene su tension con el reactivo que haga falta. Eso cambia el perfil
    de tension ---0,0041 pu de media, 0,031 el peor caso--- y con el las
    perdidas (128,85 -> 128,23 MW) y el reparto de activa: 0,29 MW de media
    por linea, 5,92 MW en la peor, 14 lineas de 280 por encima de 1 MW.

    El contraste con PARATEC del final tambien cambia, y ahi es por
    construccion: el desglose atribuye generacion a mas barras, asi que hay
    mas maquinas que contrastar (17 -> 18 buses).
    """
    from stn import desagregacion

    lines_df = load_lines(verbose=False)
    network = build_network(lines_df, verbose=False)
    inyecciones, resumen = integrar(verbose=False)
    desglose = desagregacion.desglose_de(inyecciones, verbose=False)
    asignar_inyecciones(network, inyecciones, verbose=False, desglose=desglose)
    designar_slack(network, verbose=False)
    ok, _ = run_power_flow(network, verbose=False)
    if not ok:
        raise RuntimeError("El flujo de carga no convergio; no se puede validar.")

    # Curva de capacidad de reactivo. Sin esto la validacion contrastaba una
    # red DISTINTA de la que analizan los demas modulos en cuanto el flag
    # estaba activo: reportaba 0,8058 / 23,26 mientras el modelo de verdad
    # daba 0,8050 / 23,36. Mismo error que el del desglose descrito arriba.
    from stn import limites_reactivos
    network = limites_reactivos.aplicar_si_procede(network)

    return network, resumen


def _emparejar(claves_modelo, claves_pf):
    """Empareja claves de linea. El reporte abrevia los nombres ("Alto Anchi"
    por "ALTO ANCHICAYA") y omite los calificativos entre parentesis
    ("Ancon Sur" por "ANCON SUR (EPM)"), asi que tras la coincidencia exacta
    se admite una coincidencia por prefijo de palabra."""
    pares = {}
    restantes_pf = set(claves_pf)
    for k in claves_modelo:
        if k in restantes_pf:
            pares[k] = k
            restantes_pf.discard(k)

    def compatible(a, b):
        ta, tb = a.split(), b.split()
        if len(tb) > len(ta):
            ta, tb = tb, ta
        # cada palabra de la mas corta debe ser prefijo de alguna de la otra
        libres = list(ta)
        for t in tb:
            hit = next((u for u in libres if u.startswith(t) or t.startswith(u)), None)
            if hit is None:
                return False
            libres.remove(hit)
        return True

    for k in claves_modelo:
        if k in pares:
            continue
        cands = [c for c in restantes_pf if compatible(k, c)]
        if len(cands) == 1:
            pares[k] = cands[0]
            restantes_pf.discard(cands[0])
    return pares


def comparar(network):
    pf = flujos_powerfactory()
    mio = network.lines_t.p0.iloc[0].abs()
    mio.index = [clave_linea(i) for i in network.lines.index]
    mio = mio.groupby(level=0).max()

    pares = _emparejar(sorted(mio.index), sorted(pf.index))
    filas = []
    for k_mod, k_pf in sorted(pares.items()):
        filas.append({
            'linea': k_mod,
            'PyPSA_MW': round(float(mio[k_mod]), 1),
            'PowerFactory_MW': round(float(pf[k_pf]), 1),
        })
    cmp = pd.DataFrame(filas).set_index('linea')
    if len(cmp):
        cmp['dif_MW'] = (cmp.PyPSA_MW - cmp.PowerFactory_MW).round(1)
        cmp['dif_%'] = (100 * cmp.dif_MW /
                        cmp.PowerFactory_MW.replace(0, np.nan)).round(1)
    return cmp, len(mio), len(pf)


def main():
    print("Resolviendo el modelo en PyPSA...")
    network, resumen = resolver_modelo()

    cmp, n_mio, n_pf = comparar(network)
    print(f"\nLineas del modelo: {n_mio} | lineas en el reporte: {n_pf} | "
          f"comparables: {len(cmp)}")
    if not len(cmp):
        print("No hubo coincidencias de nombre; revisa clave_linea().")
        return

    d = cmp.dif_MW.abs()
    print("\nComparacion de flujos de linea (|P| en el extremo de envio)")
    print(f"  error medio absoluto : {d.mean():7.2f} MW")
    print(f"  mediana del error    : {d.median():7.2f} MW")
    print(f"  correlacion          : {np.corrcoef(cmp.PyPSA_MW, cmp.PowerFactory_MW)[0,1]:7.4f}")
    for u in (10, 25, 50, 100):
        print(f"  |error| < {u:3d} MW      : {100*(d < u).mean():5.1f}%")

    print("\n  Mayores desviaciones:")
    print(cmp.reindex(d.sort_values(ascending=False).index).head(12).to_string())

    try:
        from stn.paratec_equipos import contraste_generacion
        # Contraste ESTRICTO por par de subestaciones, numero de circuito y
        # nivel de tension. Estaba escrito y no se llamaba desde aqui, asi
        # que nunca se ejecutaba: la unica cifra de validacion que se
        # publicaba era la del emparejador difuso, que es el que llega a
        # casar "JUANCHITO - SAN MARCOS (VALLE)" con "San Juan - Valledupar",
        # a 700 km. Cubre menos lineas ---24 frente a 39--- pero no arrastra
        # falsos emparejamientos, asi que es el contraste que vale.
        contraste_edge(network)
    except Exception as e:                                   # noqa: BLE001
        print(f"\n  (no se pudo hacer el contraste estricto: {e})")

    try:
        cg = contraste_generacion(network)
        if len(cg):
            print("\nContraste con la capacidad instalada (PARATEC)")
            print(f"  buses contrastados      : {len(cg)}")
            print(f"  ratio maximo            : {cg.ratio.max():.2f}")
            excede = cg[cg.ratio > 1.05]
            if len(excede):
                print(f"  ATENCION: {len(excede)} buses con mas generacion "
                      f"asignada que capacidad instalada:")
                print(excede.to_string(index=False))
            else:
                print("  ningun bus supera su capacidad instalada (>5%)")
    except Exception as e:
        print(f"\n  (no se pudo contrastar con PARATEC: {e})")

    if resumen['nodos_sin_asignar']:
        print(f"\nNota: quedan {resumen['nodos_sin_asignar']} nodos sin asignar "
              f"({resumen['P_sin_asignar']:.0f} MW). Las desviaciones se")
        print("concentraran alrededor de esos buses hasta que se complete el mapeo.")


# NOTA: este bloque tiene que ser LO ULTIMO del fichero. Estuvo en mitad del
# modulo, delante de `contraste_edge()`, de modo que al correr el script
# `main()` se ejecutaba antes de que esa funcion existiera y el contraste
# estricto era inalcanzable por construccion: se definia, nadie podia
# llamarlo, y la unica validacion que se publicaba era la del emparejador
# difuso.


# ---------------------------------------------------------------------------
# Contraste estricto contra el reporte Edge Elements
# ---------------------------------------------------------------------------

ARCHIVO_EDGE = dato(PUNTO, 'edge_2034.csv')


def _clave_corredor(nombre):
    """'Ancon - Carrieles 1 230' -> (par de subestaciones, circuito, kV).

    Empareja por PAR DE BARRAS, no por bolsa de palabras. El emparejador de
    comparar() acepta prefijos en cualquier direccion, y por eso llega a casar
    'JUANCHITO - SAN MARCOS (VALLE)' con 'San Juan - Valledupar', que estan a
    700 km. Aqui hay que coincidir en las dos subestaciones, en el numero de
    circuito y en el nivel: casa mucho menos, pero lo que casa es cierto.
    """
    s = str(nombre).upper()
    for a, b in [('Ñ', 'N'), ('Á', 'A'), ('É', 'E'), ('Í', 'I'),
                 ('Ó', 'O'), ('Ú', 'U')]:
        s = s.replace(a, b)
    s = re.sub(r'\s+', ' ', re.sub(r'[^A-Z0-9 \-]', ' ', s)).strip()
    m = re.match(r'^(.+?)\s+-\s+(.+)$', s)
    if not m:
        return None
    izq, resto = m.group(1).strip(), m.group(2).strip()
    mk = re.search(r'\b(500|230|220)\b(\s*KV)?\s*$', resto)
    if not mk:
        return None
    kv = 500.0 if mk.group(1) == '500' else 230.0
    r2 = resto[:mk.start()].strip()
    mc = re.search(r'\s(\d+)$', r2)
    circ = int(mc.group(1)) if mc else None
    der = (r2[:mc.start()] if mc else r2).strip()
    tok = lambda x: frozenset(x.replace('-', ' ').split()) - {'KV'}
    if not tok(izq) or not tok(der):
        return None
    return (frozenset([tok(izq), tok(der)]), circ, kv)


def contraste_edge(network, archivo=ARCHIVO_EDGE, verbose=True):
    """Compara los flujos del modelo con el reporte Edge Elements.

    Es un contraste COMPLEMENTARIO al de comparar(), no un sustituto. Cubre
    menos lineas -24 frente a 39- porque exige coincidencia exacta del par de
    subestaciones, pero a cambio no arrastra falsos emparejamientos, y compara
    el flujo del extremo mas cargado en vez del de bus0.

    Limitacion del dato: Edge Elements tambien esta filtrado al 30% de carga
    minima (su cargabilidad minima es 30,1%) y ademas solo lista elementos
    frontera de cada subarea, de modo que de sus 272 lineas unicamente 55 son
    del STN. La validacion de flujos del STN sigue sin poder cerrarse con los
    reportes disponibles.
    """
    import numpy as np
    try:
        d = pd.read_csv(archivo)
    except FileNotFoundError:
        if verbose:
            print(f"  no esta {archivo}; generalo con "
                  f"Datos/extraer_edge_elements.py")
        return pd.DataFrame()

    lin = d[d.tipo.fillna('').str.strip() == 'Line']
    ref = {}
    for (_, nom), sub in lin.groupby(['grid', 'nombre']):
        k = _clave_corredor(nom)
        if k:
            ref.setdefault(k, []).append(
                float(np.nanmax(np.abs(sub.P_MW.values))))

    pm = np.maximum(network.lines_t.p0.iloc[0].abs(),
                    network.lines_t.p1.iloc[0].abs())
    mod = {}
    for l in network.lines.index:
        k = _clave_corredor(l)
        if k:
            mod.setdefault(k, []).append(float(pm[l]))

    comunes = sorted(set(ref) & set(mod), key=lambda k: -max(ref[k]))
    filas = []
    for k in comunes:
        a, b = max(ref[k]), max(mod[k])
        nom = ' - '.join(sorted(' '.join(sorted(t)) for t in k[0]))
        filas.append({'corredor': nom, 'circuito': k[1], 'kV': k[2],
                      'PowerFactory_MW': round(a, 1), 'PyPSA_MW': round(b, 1),
                      'dif_MW': round(b - a, 1)})
    out = pd.DataFrame(filas)

    if verbose and len(out):
        e = out.dif_MW.abs()
        print(f"\nContraste estricto contra Edge Elements")
        print(f"  lineas STN en el reporte : {len(ref)}")
        print(f"  lineas STN en el modelo  : {len(mod)}")
        print(f"  emparejadas por par de barras, circuito y nivel: {len(out)}")
        print(f"  correlacion              : "
              f"{out.PowerFactory_MW.corr(out.PyPSA_MW):.3f}")
        print(f"  error medio absoluto     : {e.mean():.1f} MW")
        print(f"  dentro de 10 MW: {(e < 10).sum()}/{len(out)}   "
              f"dentro de 25 MW: {(e < 25).sum()}/{len(out)}")
        print("\n  Mayores desviaciones:")
        print(out.reindex(e.sort_values(ascending=False).index)
              .head(6).to_string(index=False))
    return out


if __name__ == '__main__':
    main()
