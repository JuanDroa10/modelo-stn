# data_loader.py
"""Carga y normalizacion de los datos de entrada del modelo STN.

Correcciones respecto de la version anterior:

  1. parse_float distingue el separador decimal del de miles. El CSV mezcla
     ambos convenios: "0,0653" usa coma decimal, "3.60813" (latitud) usa punto
     decimal y "1.060" (corriente nominal) usa el punto como separador de
     miles. Antes "1.060" se leia como 1,06 A en vez de 1060 A.

  2. La susceptancia se convierte de microsiemens a SIEMENS. PyPSA espera
     el parametro 'b' en S; al pasarle microsiemens la carga capacitiva
     quedaba 1.000.000 de veces sobredimensionada y el flujo de carga no
     podia converger.

  3. Los nombres de subestacion se limpian del sufijo de circuito y tension
     que se colo desde el nombre de la linea ("HELICONIA 1 230 KV" ->
     "HELICONIA"), que creaba buses fantasma duplicados.

  4. Cada bus queda identificado por subestacion + NIVEL DE TENSION
     ("BACATA__500" y "BACATA__230" son buses distintos, unidos despues por
     un autotransformador). Antes se fusionaban en un unico nodo, lo que
     hacia que 56 lineas conectaran buses de distinto voltaje nominal y
     falseaba la conversion a por-unidad.
"""

import re
import unicodedata

import numpy as np
import pandas as pd

from stn.config import FILE_LINES, VOLTAGE_MIN, VOLTAGE_MAX, INCLUIR_COMPLEMENTOS

# "1.060" o "12.345.678": punto seguido siempre de grupos de 3 digitos.
_MILES_RE = re.compile(r'^-?\d{1,3}(\.\d{3})+$')

# Sufijo de circuito + tension pegado al nombre de la subestacion:
# "HELICONIA 1 230 KV", "PIMAMPIRO 4 230 kV"
_SUFIJO_CIRCUITO_RE = re.compile(r'\s+\d+\s+\d+(\.\d+)?\s*KV\s*$', re.IGNORECASE)
# Parentesis puramente numericos: "PURNIO (230)", "LA VIRGINIA (500 230)"
_PAREN_NUM_RE = re.compile(r'\s*\(\s*[\d\s.]+\)\s*$')


def parse_float(x):
    """Convierte a float respetando el convenio decimal de cada valor.

    - Si hay coma, la coma es el separador decimal y el punto el de miles.
    - Si no hay coma pero el patron es "1.060" / "1.234.567", el punto es
      separador de miles.
    - En cualquier otro caso el punto es el separador decimal.
    """
    if x is None:
        return np.nan
    if isinstance(x, (int, float, np.number)):
        return float(x)
    s = str(x).strip()
    if s == '' or s == '-':
        return np.nan
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    elif _MILES_RE.match(s):
        s = s.replace('.', '')
    try:
        return float(s)
    except ValueError:
        return np.nan


def nivel_tension(v):
    """Agrupa el voltaje nominal en el nivel del STN al que pertenece.

    220 kV y 230 kV son el mismo nivel fisico en el sistema colombiano y se
    interconectan directamente sin transformador.
    """
    if pd.isna(v):
        return np.nan
    v = float(v)
    if 200.0 <= v < 400.0:
        return 230.0
    return v


def clean_bus_name(name):
    """Normaliza el nombre de una subestacion.

    Conserva los calificadores alfabeticos entre parentesis porque
    distinguen subestaciones realmente distintas: "ANCON SUR (EPM)" y
    "ANCON SUR (ISA)" no son la misma barra.
    """
    if not isinstance(name, str):
        return ''
    s = name.upper().strip()
    s = ''.join(c for c in unicodedata.normalize('NFD', s)
                if unicodedata.category(c) != 'Mn')
    s = _SUFIJO_CIRCUITO_RE.sub('', s)
    s = _PAREN_NUM_RE.sub('', s)
    s = re.sub(r'\bNVA\b', 'NUEVA', s)
    s = re.sub(r'\bNVO\b', 'NUEVO', s)
    s = s.replace('_', ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def bus_id(nombre, nivel):
    """Identificador de bus: subestacion + nivel de tension."""
    return f"{nombre}__{int(nivel)}"


def load_lines(verbose=True):
    """Carga las lineas del STN y devuelve un DataFrame con una fila por
    linea, con los parametros ya agregados y en unidades de PyPSA.

    Columnas devueltas:
        name, bus0, bus1, sub0, sub1, v_nom, nivel,
        r [ohm], x [ohm], b [S], length [km], lat0, lon0, lat1, lon1
    """
    df = pd.read_csv(FILE_LINES, encoding='utf-8-sig')
    df.columns = df.columns.str.strip()

    col_r = next(c for c in df.columns if c.startswith('Resistencia (R1)'))
    col_x = next(c for c in df.columns if c.startswith('Reactancia (X1)'))
    col_b = next(c for c in df.columns if c.startswith('Susceptancia (B1)'))
    col_i = 'Corriente nominal [A]'
    col_ie = 'Límite de emergencia [A]'

    num_cols = ['Voltaje nominal [kV]', 'Longitud [km]', col_r, col_x, col_b,
                col_i, col_ie, 'lat_inicio', 'lon_inicio', 'lat_fin', 'lon_fin']
    for col in num_cols:
        if col in df.columns:
            df[col] = df[col].apply(parse_float)

    df_stn = df[df['Voltaje nominal [kV]'].between(VOLTAGE_MIN, VOLTAGE_MAX)].copy()
    if verbose:
        print(f"Lineas en el rango {VOLTAGE_MIN}-{VOLTAGE_MAX} kV: {len(df_stn)}")

    df_stn = df_stn.dropna(subset=['Nombre', 'Longitud [km]'])
    df_stn = df_stn[(df_stn[col_r] > 0) & (df_stn[col_x] > 0)]

    filas = []
    for name, g in df_stn.groupby('Nombre'):
        v_nom = g['Voltaje nominal [kV]'].iloc[0]
        nivel = nivel_tension(v_nom)

        sub0 = clean_bus_name(g['subestacion_inicio'].iloc[0])
        sub1 = clean_bus_name(g['subestacion_fin'].iloc[0])
        if not sub0 or not sub1 or sub0 == sub1:
            # sin extremos utilizables o lazo sobre si misma
            continue

        # Los tramos de una misma linea estan en serie: R y X se suman, y la
        # susceptancia total tambien es la suma de la de cada tramo.
        r_total = float((g[col_r] * g['Longitud [km]']).sum())
        x_total = float((g[col_x] * g['Longitud [km]']).sum())
        b_micro = float((g[col_b] * g['Longitud [km]']).sum())
        largo = float(g['Longitud [km]'].sum())

        # Capacidad nominal a partir de la corriente nominal del conductor:
        # S = sqrt(3) * V * I. El tramo mas limitante manda. Estos datos ya
        # estaban en el CSV pero no se usaban, asi que todas las lineas
        # entraban a PyPSA con s_nom = 0, es decir, sin limite.
        i_nom = g[col_i].min()
        i_eme = g[col_ie].min()
        s_nom = float(np.sqrt(3) * v_nom * i_nom / 1000.0) if pd.notna(i_nom) else 0.0
        s_eme = float(np.sqrt(3) * v_nom * i_eme / 1000.0) if pd.notna(i_eme) else 0.0

        filas.append({
            'name': name,
            'sub0': sub0,
            'sub1': sub1,
            'bus0': bus_id(sub0, nivel),
            'bus1': bus_id(sub1, nivel),
            'v_nom': v_nom,
            'nivel': nivel,
            'r': r_total,                 # ohm
            'x': x_total,                 # ohm
            'b': b_micro * 1e-6,          # microsiemens -> SIEMENS
            'length': largo,
            's_nom': s_nom,               # MVA, limite nominal
            's_eme': s_eme,               # MVA, limite de emergencia (N-1)
            'lat0': g['lat_inicio'].iloc[0],
            'lon0': g['lon_inicio'].iloc[0],
            'lat1': g['lat_fin'].iloc[0],
            'lon1': g['lon_fin'].iloc[0],
        })

    lines_df = pd.DataFrame(filas)

    if INCLUIR_COMPLEMENTOS:
        from stn import complementos
        extra = complementos.lineas_df()
        if len(extra):
            ya = set(lines_df['name'])
            extra = extra[~extra['name'].isin(ya)]
            # evitar duplicar un corredor que ya exista entre los mismos buses
            pares = {frozenset((a, b)) for a, b in zip(lines_df.bus0, lines_df.bus1)}
            dup = extra.apply(lambda r: frozenset((r.bus0, r.bus1)) in pares, axis=1)
            if dup.any() and verbose:
                for nm in extra.loc[dup, 'name']:
                    print(f"  complemento omitido (corredor ya existe): {nm}")
            extra = extra[~dup]
            if len(extra):
                lines_df = pd.concat(
                    [lines_df, extra[lines_df.columns]], ignore_index=True)
                if verbose:
                    print(f"  complementos anadidos: {len(extra)} lineas "
                          f"({', '.join(extra['name'])})")
        n_antes = len(lines_df)
        lines_df = complementos.aplicar_derivaciones(lines_df)

        lines_df = complementos.desdoblar_centrales(lines_df)
        if verbose and len(lines_df) != n_antes:
            print(f"  derivaciones aplicadas: {len(lines_df) - n_antes} tramos "
                  f"nuevos por seccionamiento de linea")

    if verbose:
        n_sub = len(set(lines_df.sub0) | set(lines_df.sub1))
        n_bus = len(set(lines_df.bus0) | set(lines_df.bus1))
        print(f"Lineas unicas (tramos agregados): {len(lines_df)}")
        print(f"Subestaciones: {n_sub}  ->  buses por nivel de tension: {n_bus}")
    return lines_df
