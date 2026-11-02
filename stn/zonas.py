# zonas.py
"""Agrupacion del STN en las cinco areas operativas del sistema colombiano.

Cada bus se asigna a una subarea a partir de la columna "Subarea" del CSV de
lineas de PARATEC, y las subareas se agrupan en las cinco areas que usa XM:
Caribe, Antioquia, Nordeste, Oriental y Suroccidental.

Con eso se calcula por area la generacion, la demanda, las perdidas y el
intercambio neto con el resto del sistema, que es el formato en el que vienen
los resultados de referencia de PowerFactory.
"""

import re
import unicodedata

import pandas as pd

from stn.config import FILE_LINES, VOLTAGE_MIN, VOLTAGE_MAX
from stn.data_loader import parse_float, clean_bus_name, nivel_tension, bus_id
from stn.paths import RED, dato

ARCHIVO_REFERENCIA = dato(RED, 'resumen_por_zonas.csv')

# subarea normalizada -> area operativa
AREA_DE_SUBAREA = {
    'ATLANTICO': 'Caribe',
    'GCM': 'Caribe',
    'BOLIVAR': 'Caribe',
    'CORDOBA-SUCRE': 'Caribe',
    'CERROMATOSO': 'Caribe',
    'ANTIOQUIA': 'Antioquia',
    'ARAUCA': 'Nordeste',
    'SANTANDER': 'Nordeste',
    'BOYACA-CASANARE': 'Nordeste',
    'NORTE DE SANTANDER': 'Nordeste',
    'BOGOTA': 'Oriental',
    'META': 'Oriental',
    'CQR': 'Suroccidental',
    'PUTUMAYO': 'Suroccidental',
    'VALLE': 'Suroccidental',
    'HUILA-TOLIMA': 'Suroccidental',
    'CAUCA-NARINO': 'Suroccidental',
    'CAQUETA': 'Suroccidental',
}

AREAS = ['Caribe', 'Antioquia', 'Nordeste', 'Oriental', 'Suroccidental']


def normaliza_subarea(txt):
    """'SubArea Cordoba_Sucre' / 'Córdoba-Sucre' -> 'CORDOBA-SUCRE'."""
    if not isinstance(txt, str):
        return ''
    s = ''.join(c for c in unicodedata.normalize('NFD', txt.upper())
                if unicodedata.category(c) != 'Mn')
    s = re.sub(r'^\s*SUBAREA\s+', '', s)
    s = s.replace('_', '-').replace('NARINO', 'NARINO')
    return re.sub(r'\s+', ' ', s).strip()


def subarea_por_bus(verbose=False):
    """Asigna una subarea a cada bus segun las lineas que llegan a el.

    Un bus puede recibir lineas catalogadas en subareas distintas (los
    corredores de frontera); se toma la mas frecuente.
    """
    df = pd.read_csv(FILE_LINES, encoding='utf-8-sig')
    df.columns = df.columns.str.strip()
    df['v'] = df['Voltaje nominal [kV]'].apply(parse_float)
    df = df[df.v.between(VOLTAGE_MIN, VOLTAGE_MAX)]

    votos = {}
    for _, r in df.iterrows():
        sa = normaliza_subarea(r.get('Subárea'))
        if not sa:
            continue
        nivel = nivel_tension(r.v)
        for campo in ('subestacion_inicio', 'subestacion_fin'):
            sub = clean_bus_name(r.get(campo))
            if sub:
                votos.setdefault(bus_id(sub, nivel), []).append(sa)

    asignacion = {}
    for bus, vs in votos.items():
        asignacion[bus] = pd.Series(vs).mode().iloc[0]
    if verbose:
        print(f"buses con subarea: {len(asignacion)}")
    return asignacion


def area_por_bus(network=None):
    """{bus: (subarea, area)} para los buses de la red."""
    sub = subarea_por_bus()
    out = {}
    for bus, sa in sub.items():
        out[bus] = (sa, AREA_DE_SUBAREA.get(sa, 'Sin area'))
    if network is not None:
        # los buses que no estan en el CSV (p.ej. los de complementos.py)
        # heredan el area de la subestacion vecina mas conectada
        faltan = [b for b in network.buses.index if b not in out]
        for b in faltan:
            vecinos = list(network.lines[network.lines.bus0 == b].bus1) + \
                      list(network.lines[network.lines.bus1 == b].bus0)
            areas = [out[v] for v in vecinos if v in out]
            out[b] = pd.Series(areas).mode().iloc[0] if areas else ('', 'Sin area')
    return out


def resumen(network):
    """Balance por area: generacion, demanda, perdidas e intercambio.

    Las perdidas de una linea se reparten a partes iguales entre las dos areas
    que une; las de una linea interna van entera a su area. El intercambio es
    el flujo neto que sale del area por las lineas de frontera.
    """
    mapa = area_por_bus(network)
    area = {b: mapa.get(b, ('', 'Sin area'))[1] for b in network.buses.index}

    filas = {a: {'GenP': 0.0, 'LoadP': 0.0, 'LoadQ': 0.0,
                 'LossP': 0.0, 'InterP': 0.0, 'buses': 0} for a in AREAS}
    filas['Sin area'] = dict(filas['Caribe'])

    for a in set(area.values()):
        filas.setdefault(a, {'GenP': 0.0, 'LoadP': 0.0, 'LoadQ': 0.0,
                             'LossP': 0.0, 'InterP': 0.0, 'buses': 0})
    for b in network.buses.index:
        filas[area[b]]['buses'] += 1

    gen = network.generators_t.p.iloc[0]
    for g, p in gen.items():
        filas[area[network.generators.at[g, 'bus']]]['GenP'] += float(p)
    loadp = network.loads_t.p.iloc[0]
    loadq = network.loads_t.q.iloc[0]
    for l, p in loadp.items():
        a = area[network.loads.at[l, 'bus']]
        filas[a]['LoadP'] += float(p)
        filas[a]['LoadQ'] += float(loadq[l])

    # perdidas e intercambio por las lineas
    p0 = network.lines_t.p0.iloc[0]
    p1 = network.lines_t.p1.iloc[0]
    for ln in network.lines.index:
        a0 = area[network.lines.at[ln, 'bus0']]
        a1 = area[network.lines.at[ln, 'bus1']]
        perdida = float(p0[ln] + p1[ln])
        if a0 == a1:
            filas[a0]['LossP'] += perdida
        else:
            filas[a0]['LossP'] += perdida / 2
            filas[a1]['LossP'] += perdida / 2
            filas[a0]['InterP'] += float(p0[ln])
            filas[a1]['InterP'] += float(p1[ln])
    # y por los transformadores (siempre internos a una subestacion)
    if len(network.transformers):
        t0 = network.transformers_t.p0.iloc[0]
        t1 = network.transformers_t.p1.iloc[0]
        for tr in network.transformers.index:
            a0 = area[network.transformers.at[tr, 'bus0']]
            filas[a0]['LossP'] += float(t0[tr] + t1[tr])

    d = pd.DataFrame(filas).T
    d = d[d.buses > 0]
    orden = [a for a in AREAS if a in d.index] + \
            [a for a in d.index if a not in AREAS]
    return d.loc[orden].round(2)


def resumen_subareas(network):
    """Mismo balance pero desglosado por subarea."""
    mapa = area_por_bus(network)
    sub = {b: mapa.get(b, ('', ''))[0] for b in network.buses.index}
    filas = {}
    for b in network.buses.index:
        filas.setdefault(sub[b], {'Area': AREA_DE_SUBAREA.get(sub[b], 'Sin area'),
                                  'GenP': 0.0, 'LoadP': 0.0, 'LossP': 0.0,
                                  'buses': 0})['buses'] += 1
    for g, p in network.generators_t.p.iloc[0].items():
        filas[sub[network.generators.at[g, 'bus']]]['GenP'] += float(p)
    for l, p in network.loads_t.p.iloc[0].items():
        filas[sub[network.loads.at[l, 'bus']]]['LoadP'] += float(p)
    # Lineas Y transformadores. Antes solo se contaban las lineas, asi que
    # esta funcion daba 120,60 MW de perdidas donde resumen() daba 126,32:
    # dos funciones del mismo modulo con dos totales distintos para la misma
    # red. Los 5,7 MW que faltaban eran los de los 16 autotransformadores.
    for comp, tabla in (('lines', network.lines),
                        ('transformers', network.transformers)):
        if not len(tabla):
            continue
        p0 = getattr(network, f'{comp}_t').p0.iloc[0]
        p1 = getattr(network, f'{comp}_t').p1.iloc[0]
        for el in tabla.index:
            s0 = sub[tabla.at[el, 'bus0']]
            s1 = sub[tabla.at[el, 'bus1']]
            perd = float(p0[el] + p1[el])
            if s0 == s1:
                filas[s0]['LossP'] += perd
            else:
                filas[s0]['LossP'] += perd / 2
                filas[s1]['LossP'] += perd / 2
    d = pd.DataFrame(filas).T
    return d.sort_values(['Area', 'LoadP'], ascending=[True, False]).round(2)


def comparar_con_referencia(res, archivo=ARCHIVO_REFERENCIA):
    """Compara el resumen calculado con los resultados de PowerFactory."""
    try:
        ref = pd.read_csv(archivo)
    except FileNotFoundError:
        return None
    ref = ref[ref.Subarea == 'Total'].set_index('Area')[['GenP', 'LoadP', 'LossP']]
    comp = res[['GenP', 'LoadP', 'LossP']].join(ref, how='inner', rsuffix='_ref')
    # El balance neto (gen - demanda - perdidas) es lo unico directamente
    # comparable: es la exportacion neta del area, y no depende de a que nivel
    # de tension este conectada cada planta o cada carga.
    comp['NetoExp'] = (comp.GenP - comp.LoadP - comp.LossP).round(1)
    comp['NetoExp_ref'] = (comp.GenP_ref - comp.LoadP_ref - comp.LossP_ref).round(1)
    comp['dNeto'] = (comp.NetoExp - comp.NetoExp_ref).round(1)
    return comp[['GenP', 'GenP_ref', 'LoadP', 'LoadP_ref', 'LossP', 'LossP_ref',
                 'NetoExp', 'NetoExp_ref', 'dNeto']].round(1)


AVISO_REFERENCIA = """
  Esta comparacion NO es una validacion. Hay dos razones:

  1. El escenario no es el mismo. La referencia tiene ~88% de la demanda del
     reporte de PowerFactory que alimenta el modelo, y esa proporcion se repite
     en las 18 subareas (Antioquia 0,84 / Bogota 0,91 / Valle 0,87): es otro
     punto de operacion, mas liviano.

  2. GenP y LoadP no miden lo mismo en cada lado. La referencia da la
     generacion y la demanda BRUTAS del area, a todos los niveles de tension.
     Aqui son las inyecciones y los retiros NETOS en la frontera del STN, con
     todo lo que cuelga por debajo de 220 kV ya compensado entre si. Por eso
     Atlantico figura con 0 MW de generacion aunque alli esten Tebsa y
     Termoflores: esas plantas se conectan en 110 kV y su produccion se cancela
     contra la demanda local antes de llegar al STN.

  La columna comparable es NetoExp (generacion - demanda - perdidas), que es la
  exportacion neta del area y no depende del nivel de tension de cada elemento.
  Aun asi, al ser escenarios distintos, tampoco tiene por que coincidir."""
