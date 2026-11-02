# paratec_equipos.py
"""Extrae de los reportes de PARATEC los equipos de subestacion que el CSV de
lineas no trae: autotransformadores y compensacion reactiva.

La columna "Elementos que conecta" del reporte de Bahias nombra cada equipo con
su capacidad, con formatos como:

    SOGAMOSO 1 450 MVA 500/230/34.5 kV
    ALTAMIRA REACTOR DE BARRA 1 25 MVAR 230 kV
    CUESTECITAS CONDENSADOR PARALELO BANCO 01 40.7 MVAR 230 kV

De ahi se saca la potencia nominal real de los autotransformadores 500/230,
que hasta ahora eran un valor generico de 1.000 MVA inventado por mi.

La compensacion reactiva se extrae tambien, pero NO se aplica al modelo: ver
la nota en compensacion_stn().
"""

import re
import functools

import pandas as pd

from stn.data_loader import parse_float, clean_bus_name
from stn.paths import PARATEC

# Raiz de la descarga de PARATEC. En minuscula, como la carpeta: macOS no
# distingue mayusculas pero Linux si, y con 'Paratec' el modelo no arrancaba
# fuera de este portatil.
DIR_PARATEC = str(PARATEC)

ARCHIVO_BAHIAS = f'{DIR_PARATEC}/Transmision/Subestaciones/PARATEC_Bahias_06-09-2026.xlsx'
ARCHIVOS_GENERACION = [
    (f'{DIR_PARATEC}/Generacion/PARATEC_Phidraulica_06-09-2026.xlsx', 'Phidráulica',
     'Capacidad efectiva neta [MW]', 'hidraulica'),
    (f'{DIR_PARATEC}/Generacion/PARATEC_Ptermica_06-09-2026.xlsx', 'InfoCombustibles',
     'Capacidad efectiva neta [MW]', 'termica'),
    (f'{DIR_PARATEC}/Generacion/PARATEC_Usolar_06-09-2026.xlsx', 'Usolar',
     'Potencia de la unidad equivalente [MW]', 'solar'),
    (f'{DIR_PARATEC}/Generacion/PARATEC_Peolica_06-09-2026.xlsx', 'Peólica',
     'Capacidad efectiva neta [MW]', 'eolica'),
]

# "450 MVA 500/230/34.5 kV" o "360 MVA 500/230 kV"
_TRAFO_RE = re.compile(
    r'(?P<mva>\d+(?:[.,]\d+)?)\s*MVA\s+(?P<v1>\d+(?:[.,]\d+)?)\s*/\s*'
    r'\(?(?P<v2>\d+(?:[.,]\d+)?)\)?', re.IGNORECASE)
_MVAR_RE = re.compile(r'(?P<mvar>\d+(?:[.,]\d+)?)\s*MVAR', re.IGNORECASE)
# parentesis que solo contienen numeros y separadores: "(230)", "(500 - 110)"
_PAREN_NUM = re.compile(r'\s*\(\s*[\d\s.\-/]+\)')

# PARATEC y el CSV de lineas no siempre usan el mismo nombre para la misma
# subestacion. Solo se declaran los casos comprobados uno a uno.
ALIAS = {
    'ALFEREZ (EEB)': 'ALFEREZ',   # el patio de 500 kV figura como ALFEREZ en las lineas
}


# ---------------------------------------------------------------------------
# Reportes "Detalle de unidad X" (descarga del 20-09-2026)
# ---------------------------------------------------------------------------
#
# Traen, unidad a unidad, lo que el modelo venia suponiendo por tecnologia:
# la constante de inercia H, el estatismo, las capacidades de absorcion y
# generacion de reactivos, y ---lo mas util--- el PUNTO DE CONEXION al STR/STN.
#
# ESTRUCTURA. Las cuatro hojas tienen dos filas de encabezado superpuestas:
# la fila 4 agrupa bloques (INFORMACION BASICA / INFORMACION TECNICA /
# UBICACION; en la termica ademas hay hojas AGC, InfoCombustible y
# VelCarga-Descarga) y la fila 5 son los nombres de columna reales. De ahi
# header=5. Las filas 0-3 son titulo y fecha de generacion.
#
# OJO: el reporte SOLAR no trae punto de conexion, ni H, ni estatismo ---es
# un inventario de inversores--- asi que no participa de esta via.
UNIDADES = [
    (f'{DIR_PARATEC}/Generacion/PARATEC_Utermica_20-09-2026.xlsx',
     'Utérmica', 'termica'),
    (f'{DIR_PARATEC}/Generacion/PARATEC_Uhidraulica_20-09-2026.xlsx',
     'Uhidráulica', 'hidraulica'),
    (f'{DIR_PARATEC}/Generacion/PARATEC_Ueolica_20-09-2026.xlsx',
     'Ueólica', 'eolica'),
]
ARCHIVO_TRAFOS_PARATEC = (f'{DIR_PARATEC}/Transmision/Transformacion/'
                          f'PARATEC_Transformadores_20-09-2026.xlsx')

# El punto de conexion nombra la SUBESTACION, que no siempre coincide con el
# nombre que el modelo da a la barra de la central. Solo los casos verificados
# uno a uno contra el CSV de lineas.
ALIAS_PUNTO = {
    # El modelo llama HIDROSOGAMOSO a la barra de la central y SOGAMOSO a la
    # subestacion; PARATEC declara las tres unidades conectadas en "SOGAMOSO
    # 230". Verificado: las lineas del modelo son HIDROSOGAMOSO - SOGAMOSO
    # 1/2/3 230 kV, es decir la central entrega EN Sogamoso.
    'SOGAMOSO': 'HIDROSOGAMOSO',

    # Mismo patron: PARATEC declara "EL QUIMBO 1" y "EL QUIMBO 2" conectadas
    # en "TESALIA 230", porque la subestacion Tesalia 230 kV se construyo
    # justamente para evacuar El Quimbo ---en la convocatoria UPME 05-2009
    # figura como "Tesalia (Quimbo) 230 kV"---. El modelo tiene la barra
    # EL QUIMBO__230 y no tiene generador en Tesalia.
    #
    # La asignacion es inequivoca por el NOMBRE de la unidad, que dice EL
    # QUIMBO. Hacia falta comprobarlo porque Tesalia tambien es vecina de
    # JAMONDINO__230 en el modelo, de modo que la topologia sola no bastaba
    # para decidir.
    'TESALIA': 'EL QUIMBO',
}

# "ALTO ANCHICAYA 220 kV", "TEBSA II 110", "CERROMATOSO (500 - 110) 110"
_PUNTO_RE = re.compile(
    r'^(?P<sub>.*?)[\s(]*(?P<kv>\d{2,3}(?:[.,]\d+)?)[\s)]*$')


def _punto_conexion(texto):
    """'ALTO ANCHICAYA 220 kV' -> ('ALTO ANCHICAYA', 230.0).

    Devuelve (subestacion_normalizada, kV) o (nombre, None) si no se puede
    separar el nivel. PARATEC usa 220 y 230 para el mismo nivel; el modelo
    usa 230 siempre, asi que se normaliza.
    """
    s = re.sub(r'\s*kv\s*$', '', str(texto).strip(), flags=re.I)
    m = _PUNTO_RE.match(s)
    if not m:
        return _sub_norm(s), None
    kv = float(str(m.group('kv')).replace(',', '.'))
    if kv in (220.0, 230.0):
        kv = 230.0
    sub = _sub_norm(m.group('sub'))
    return ALIAS_PUNTO.get(sub, sub), kv


def _col(df, *patrones):
    """Primera columna cuyo nombre contiene todos los patrones."""
    for c in df.columns:
        s = str(c).lower()
        if all(p.lower() in s for p in patrones):
            return c
    return None


@functools.lru_cache(maxsize=1)
def unidades_generacion():
    """Una fila por unidad generadora, con sus parametros reales.

    Columnas: nombre, tipo, sub, kv, bus (sub__kv), H, estatismo_pct,
    q_abs_mvar, q_gen_mvar, s_mva, p_mw, fp_nom.

    Los valores que PARATEC deja en '-' salen como NaN: el consumidor debe
    caer a su valor por defecto, no inventarlos.
    """
    filas = []
    for archivo, hoja, tipo in UNIDADES:
        try:
            d = pd.read_excel(archivo, sheet_name=hoja, header=5, dtype=str)
        except FileNotFoundError:
            continue
        d = d[d['Nombre'].notna()]
        c_pc = _col(d, 'punto de conexi')
        c_h = _col(d, 'h [s]')
        c_r = _col(d, 'estatismo')
        c_qa = _col(d, 'absorci', 'reactiv')
        c_qg = _col(d, 'generaci', 'reactiv')
        c_s = _col(d, 'potencia aparente')
        c_p = (_col(d, 'capacidad efectiva neta')
               or _col(d, 'capacidad nominal') or _col(d, 'capacidad bruta'))
        c_fp = _col(d, 'factor de potencia')
        for _, r in d.iterrows():
            sub, kv = _punto_conexion(r[c_pc]) if c_pc else (None, None)
            filas.append({
                'nombre': str(r['Nombre']).strip(), 'tipo': tipo,
                'sub': sub, 'kv': kv,
                'bus': f"{sub}__{int(kv)}" if (sub and kv) else None,
                'H': _num_opt(r[c_h]) if c_h else None,
                'estatismo_pct': _num_opt(r[c_r]) if c_r else None,
                'q_abs_mvar': _num_opt(r[c_qa]) if c_qa else None,
                'q_gen_mvar': _num_opt(r[c_qg]) if c_qg else None,
                's_mva': _num_opt(r[c_s]) if c_s else None,
                'p_mw': _num_opt(r[c_p]) if c_p else None,
                'fp_nom': _num_opt(r[c_fp]) if c_fp else None,
            })
    return pd.DataFrame(filas)


@functools.lru_cache(maxsize=1)
def transformadores_paratec(archivo=ARCHIVO_TRAFOS_PARATEC):
    """Transformadores de potencia de PARATEC, un registro por maquina.

    ESTRUCTURA DEL REPORTE, que no es trivial. El encabezado real esta en la
    fila 8 y la 7 agrupa bloques (INFORMACION BASICA / INFORMACION TECNICA /
    DEVANADOS / CAMBIADOR DE TAPS / UBICACION). Y sobre todo: **hay una fila
    por DEVANADO**. La primera fila de cada maquina trae los datos generales
    mas su devanado de ALTA; las siguientes solo rellenan el bloque DEVANADOS
    (columnas 25-36) con el de media y el de baja. Un bidevanado ocupa dos
    filas y un tridevanado tres. Se detecta por la columna `Nombre`: no vacia
    = empieza maquina nueva.

    LA BASE DE LAS IMPEDANCIAS, y como se malinterpreta. El fichero declara:
    "El valor de las impedancias se presenta utilizando como base el menor
    valor de potencia nominal entre los devanados". Leido literalmente como
    "el menor de los TRES devanados" sale mal: en un 500/230/34,5 de
    450/450/150 MVA, rebasar la impedancia Alta-Media desde 150 hasta 450 da
    un 33 %, absurdo para un autotransformador.

    La lectura correcta es **el menor de los DOS devanados de cada par**:

        Z_alta-media  ->  base min(MVA_alta, MVA_media)   = 450
        Z_alta-baja   ->  base min(MVA_alta, MVA_baja)    = 150
        Z_media-baja  ->  base min(MVA_media, MVA_baja)   = 150

    Se comprueba con los datos: `z_am_pct` vale 11,0-11,7 % en las dieciseis
    subestaciones de 500/230, que es justo el valor fisico de un autotrafo de
    ese tamano, mientras que `z_ab_pct` y `z_mb_pct` rondan 14-16 % y 9-12 %,
    coherentes con acoplamientos al terciario en base 150 MVA.

    Devuelve un DataFrame con una fila por maquina y columnas: nombre,
    subestacion, tipo, kv_alta, kv_media, kv_baja, mva_alta, mva_media,
    mva_baja, mva_base (el menor), z_ab_pct, z_am_pct, z_mb_pct,
    p_cu_ab_kw, p_cu_am_kw, p_fe_kw, emerg_pct, tap_pct, estado.
    """
    d = pd.read_excel(archivo, sheet_name='Transformadores', header=8,
                      dtype=str)
    cols = list(d.columns)

    def idx(*pats):
        for i, c in enumerate(cols):
            s = str(c).lower()
            if all(p.lower() in s for p in pats):
                return i
        return None

    i_nom, i_sub, i_tipo = idx('nombre'), idx('subestaci'), idx('tipo de transformador')
    i_est = idx('estado')
    i_zab, i_zam, i_zmb = idx('alta-baja nom'), idx('alta-media nom'), idx('media-baja nom')
    i_cuab, i_cuam = idx('cobre alta-baja'), idx('cobre alta-media')
    i_fe = idx('hierro')
    i_kv, i_mva = idx('tensión nominal'), idx('capacidad nominal')
    i_emg = idx('capacidad de emergencia [')
    i_tap = idx('paso por tap')

    filas, actual = [], None
    for _, r in d.iterrows():
        nom = r.iloc[i_nom] if i_nom is not None else None
        if pd.notna(nom) and str(nom).strip():
            if actual:
                filas.append(actual)
            actual = {
                'nombre': str(nom).strip(),
                'subestacion': _sub_norm(r.iloc[i_sub]) if i_sub is not None else '',
                'tipo': str(r.iloc[i_tipo]).strip() if i_tipo is not None else '',
                'estado': str(r.iloc[i_est]).strip() if i_est is not None else '',
                'z_ab_pct': _num_opt(r.iloc[i_zab]) if i_zab is not None else float('nan'),
                'z_am_pct': _num_opt(r.iloc[i_zam]) if i_zam is not None else float('nan'),
                'z_mb_pct': _num_opt(r.iloc[i_zmb]) if i_zmb is not None else float('nan'),
                'p_cu_ab_kw': _num_opt(r.iloc[i_cuab]) if i_cuab is not None else float('nan'),
                'p_cu_am_kw': _num_opt(r.iloc[i_cuam]) if i_cuam is not None else float('nan'),
                'p_fe_kw': _num_opt(r.iloc[i_fe]) if i_fe is not None else float('nan'),
                'tap_pct': _num_opt(r.iloc[i_tap]) if i_tap is not None else float('nan'),
                'kv': [], 'mva': [], 'emerg': [],
            }
        if actual is not None and i_kv is not None:
            kv = _num_opt(r.iloc[i_kv])
            if kv == kv:                      # no NaN: esta fila trae devanado
                actual['kv'].append(kv)
                actual['mva'].append(_num_opt(r.iloc[i_mva]) if i_mva is not None else float('nan'))
                actual['emerg'].append(_num_opt(r.iloc[i_emg]) if i_emg is not None else float('nan'))
    if actual:
        filas.append(actual)

    out = []
    for f in filas:
        kv, mva, emg = f.pop('kv'), f.pop('mva'), f.pop('emerg')
        f['kv_alta'] = kv[0] if len(kv) > 0 else float('nan')
        f['kv_media'] = kv[1] if len(kv) > 2 else float('nan')
        f['kv_baja'] = kv[-1] if len(kv) > 1 else float('nan')
        f['mva_alta'] = mva[0] if len(mva) > 0 else float('nan')
        f['mva_media'] = mva[1] if len(mva) > 2 else float('nan')
        f['mva_baja'] = mva[-1] if len(mva) > 1 else float('nan')
        val = [m for m in mva if m == m and m > 0]
        f['mva_base'] = min(val) if val else float('nan')
        f['mva_max'] = max(val) if val else float('nan')
        f['n_devanados'] = len(kv)
        f['emerg_pct'] = emg[0] if len(emg) > 0 else float('nan')
        out.append(f)
    return pd.DataFrame(out)


def autotrafos_stn(kv_alta=500.0, kv_baja=230.0, tol=12.0):
    """Los autotransformadores 500/230 del STN, agregados por subestacion.

    Un patio puede tener varios bancos en paralelo: se agregan sumando la
    potencia y combinando las impedancias en paralelo, que es lo que el
    modelo representa con un unico transformador equivalente por subestacion.

    Para cada banco se toma la impedancia del par alta<->230 kV con SU base
    correcta (ver `transformadores_paratec`), se rebasa a la potencia total
    del conjunto y se combinan en paralelo.

    Devuelve un DataFrame indexado por subestacion con s_nom [MVA], x y r en
    p.u. sobre ese s_nom, n_bancos y z_pct_media (la impedancia individual
    tipica, para poder contrastarla a ojo con el 12 % generico).
    """
    t = transformadores_paratec()
    if not len(t):
        return pd.DataFrame()
    op = t[t.estado.str.lower().str.startswith('opera', na=False)]
    m = op[(op.kv_alta.sub(kv_alta).abs() <= tol)
           & (op[['kv_media', 'kv_baja']].sub(kv_baja).abs().min(axis=1) <= tol)]

    def z_y_base(r):
        """Impedancia del par alta<->230 kV y la base que le corresponde."""
        if r.kv_media == r.kv_media and abs(r.kv_media - kv_baja) <= tol:
            # el 230 es el devanado MEDIO: par alta-media
            bases = [v for v in (r.mva_alta, r.mva_media) if v == v and v > 0]
            return r.z_am_pct, (min(bases) if bases else float('nan'))
        # el 230 es el devanado BAJO (bidevanado, o terciario no listado)
        if r.z_am_pct == r.z_am_pct and not (r.kv_baja == r.kv_baja
                                             and abs(r.kv_baja - kv_baja) > tol):
            # tridevanado con terciario entre parentesis: PARATEC deja el
            # 230 en la columna de baja pero la impedancia util sigue siendo
            # la alta-media, en base al mayor de los dos devanados listados
            bases = [v for v in (r.mva_alta, r.mva_baja) if v == v and v > 0]
            return r.z_am_pct, (max(bases) if bases else float('nan'))
        bases = [v for v in (r.mva_alta, r.mva_baja) if v == v and v > 0]
        return r.z_ab_pct, (min(bases) if bases else float('nan'))

    filas = {}
    for sub, g in m.groupby('subestacion'):
        s_tot = float(pd.to_numeric(g.mva_max, errors='coerce').sum())
        if not s_tot > 0:
            continue
        inv, p_cu, zs = 0.0, 0.0, []
        for _, r in g.iterrows():
            z, base = z_y_base(r)
            if not (z == z and base == base and z > 0 and base > 0):
                continue
            zs.append(z)
            z_comun = (z / 100.0) * (s_tot / base)      # rebase a s_tot
            inv += 1.0 / z_comun
            cu = r.p_cu_am_kw if r.p_cu_am_kw == r.p_cu_am_kw else r.p_cu_ab_kw
            if cu == cu:
                p_cu += cu
        if inv <= 0:
            continue
        # R a partir de las perdidas en el cobre: P_cu[pu] = I^2 R ~ R a I=1
        r_pu = (p_cu / 1000.0) / s_tot if p_cu > 0 else float('nan')
        filas[sub] = {'s_nom': s_tot, 'x': 1.0 / inv, 'r': r_pu,
                      'n_bancos': int(len(g)),
                      'z_pct_media': float(sum(zs) / len(zs)) if zs else float('nan')}
    return pd.DataFrame(filas).T


def parametros_por_bus(buses_modelo):
    """Agrega las unidades de PARATEC a los buses del modelo que existan.

    `buses_modelo` es el iterable de nombres de bus generador del modelo.

    La agregacion NO es una media simple. La inercia equivalente de un
    conjunto de maquinas es la ponderada por su potencia aparente ---es lo que
    conserva H*S, que es la energia cinetica almacenada--- y el estatismo
    equivalente tambien, porque la respuesta primaria de cada maquina escala
    con su tamano. Los reactivos se suman, que es lo que hace un patio.

    Se resuelve en dos pasos, el segundo mas laxo que el primero:

      1. coincidencia EXACTA de bus (subestacion y nivel de tension);
      2. misma SUBESTACION a cualquier nivel. Cubre la generacion embebida
         que conecta en 110/115 kV y sube por transformador: el modelo la ve
         en la barra de 230 kV, y su tecnologia e inercia son las mismas.

    Devuelve un DataFrame indexado por bus con las columnas H, estatismo_pct,
    q_abs_mvar, q_gen_mvar, s_mva, p_mw, tipo, n_unidades y `via` (exacto o
    subestacion), mas `tipos` con la mezcla cuando hay varias.
    """
    u = unidades_generacion()
    if not len(u):
        return pd.DataFrame()
    buses = list(buses_modelo)

    def agrega(g, via):
        s = pd.to_numeric(g.s_mva, errors='coerce')
        peso = s.where(s > 0)
        if not peso.notna().any():
            peso = pd.to_numeric(g.p_mw, errors='coerce').where(lambda x: x > 0)
        def pond(col):
            v = pd.to_numeric(g[col], errors='coerce')
            m = v.notna() & peso.notna()
            if not m.any():
                return float('nan')
            return float((v[m] * peso[m]).sum() / peso[m].sum())

        def estatismo_de_barra():
            """Estatismo equivalente de la barra. NO es una media de R.

            DOS ERRORES EN UNO, y los dos en la direccion optimista.
            Antes se hacia `pond('estatismo_pct')`, media aritmetica de R
            ponderada por MVA, incluyendo las unidades que declaran 0,00.

            1. LOS CEROS SON UN DATO, no un hueco. Las ocho unidades que
               declaran 0,00 son TURBINAS DE VAPOR DE CICLO COMBINADO ---
               FLORES 1 VAPOR, FLORES 4, TEBSA 14 y 24, TERMOCANDELARIA CC 3,
               TERMOCENTRO 3, TERMOSIERRA 3, TERMOVALLE 1 VAPOR---. Una de
               vapor de CC sigue el escape de las de gas: no tiene gobernador
               propio sobre frecuencia y NO hace regulacion primaria. Meter
               ese 0 en una media de R la hunde, y un R menor significa MAS
               respuesta por Hz: el modelo le daba a TEBSA 2,857 % cuando sus
               unidades que si regulan declaran 5,46-5,54.
            2. LA MEDIA DE R NO ES LA COMBINACION CORRECTA. Las respuestas se
               SUMAN: dP = sum(S_i/R_i) * df/f. El equivalente de la barra es
               el mismo que `agc_deslastre` ya usa entre barras:
                   R_barra = S_barra / sum(S_i / R_i)
               sobre las unidades que REGULAN. La MVA de las que no regulan
               sigue contando en S_barra ---esta acoplada y aporta inercia---
               pero no aporta nada al sumatorio, asi que R_barra sube y la
               barra responde menos, que es lo cierto.

            Devuelve NaN si en la barra no regula NADIE; quien lo consuma
            decide. Ojo: `agc_deslastre` cae entonces al ESTATISMO por
            omision, lo que le daria respuesta que no tiene. Hoy no ocurre:
            ninguna barra del modelo tiene todas sus unidades a 0.
            """
            r = pd.to_numeric(g['estatismo_pct'], errors='coerce')
            regula = r.notna() & (r > 1e-6) & peso.notna()
            if not regula.any():
                return float('nan')
            s_total = peso[peso.notna()].sum()
            suma = (peso[regula] / (r[regula] / 100.0)).sum()
            return float(100.0 * s_total / suma) if suma > 0 else float('nan')

        return {
            'H': pond('H'), 'estatismo_pct': estatismo_de_barra(),
            'q_abs_mvar': float(pd.to_numeric(g.q_abs_mvar, errors='coerce').sum()),
            'q_gen_mvar': float(pd.to_numeric(g.q_gen_mvar, errors='coerce').sum()),
            's_mva': float(s.sum(skipna=True)),
            'p_mw': float(pd.to_numeric(g.p_mw, errors='coerce').sum()),
            'tipo': g.tipo.mode().iloc[0], 'tipos': ','.join(sorted(set(g.tipo))),
            'n_unidades': int(len(g)), 'via': via,
        }

    por_bus = {b: g for b, g in u[u.bus.notna()].groupby('bus')}
    por_sub = {s: g for s, g in u[u['sub'].notna()].groupby('sub')}
    out = {}
    for b in buses:
        # las centrales desdobladas por unidad (_U1, _U2...) comparten patio
        raiz = b.rsplit('__', 1)[0].rsplit('_U', 1)[0]
        nivel = b.rsplit('__', 1)[-1].split('_U')[0]
        clave = f"{raiz}__{nivel}"
        if clave in por_bus:
            out[b] = agrega(por_bus[clave], 'exacto')
        elif raiz in por_sub:
            out[b] = agrega(por_sub[raiz], 'subestacion')
    return pd.DataFrame(out).T if out else pd.DataFrame()


def _sub_norm(nombre):
    s = clean_bus_name(_PAREN_NUM.sub('', str(nombre)))
    return ALIAS.get(s, s)


def _num(s):
    return float(str(s).replace(',', '.'))


def _num_opt(s):
    """Como _num pero devuelve NaN en vez de fallar.

    Los reportes de unidad marcan con '-' el dato que no declaran. Hay que
    distinguirlo de un cero: un estatismo de '-' significa que no se conoce y
    obliga a caer al valor por defecto, mientras que un 0 declarado significa
    que la maquina no hace regulacion primaria.

    EL SEPARADOR DE MILLAR LO RESUELVE `data_loader.parse_float`, y hubo que
    delegar en el. La heuristica que habia aqui exigia `una coma Y mas de un
    punto`, asi que leia mal los dos casos mas comunes:

        '1.034'    ->  1,034   cuando son 1034
        '1.000,5'  ->  NaN     cuando son 1000,5

    No era teorico. `SABANALARGA 1 450 MVA 500/220/34.5 kV` declara unas
    perdidas en el cobre alta-media de '1.034' kW, que se leian como **1,034
    kW en un autotransformador de 450 MVA** ---su propia perdida alta-baja es
    de 479,5 kW--- y de ahi salia una resistencia un 42 % baja para el mayor
    grupo de 500/230 del pais. `capacidad_plantas()` ya usaba `parse_float`
    por este mismo motivo, documentado en su docstring; esta funcion no se
    entero.

    Barridas todas las hojas que el modelo consume, el cambio afecta a ese
    unico valor: las demas diferencias caen en columnas que no se leen, como
    `Re [ohm]`.
    """
    import math
    if s is None:
        return math.nan
    t = str(s).strip()
    if t in ('', '-', 'nan', 'N/A', 'NA', 'None'):
        return math.nan
    v = parse_float(t)
    return math.nan if v != v else float(v)


@functools.lru_cache(maxsize=1)
def rampas_por_tecnologia():
    """Velocidad de toma de carga declarada, en % de la placa por minuto.

    Sale de la hoja `VelCarga-Descarga` de los reportes de unidad, que trae
    `Velocidad de carga [MW/min]` para las 119 unidades hidraulicas y 70 de
    las 78 termicas.

    Tres trampas de lectura, todas comprobadas:

    1. La fila 4 fusiona dos bloques, CARGA y DESCARGA, sobre los mismos
       nombres de columna. Al leer con header=5 pandas desambigua sufijando
       el segundo con `.1`, asi que **`Velocidad de carga [MW/min].1` es en
       realidad la velocidad de DESCARGA**. Aqui solo interesa la de carga,
       que es la que limita la subida tras perder generacion.
    2. Cinco unidades termicas declaran la rampa POR TRAMOS y el fichero mete
       los tramos en una sola celda separados por saltos de linea. Se toma el
       maximo del tramo, que es la hipotesis optimista; el efecto sobre el
       agregado es despreciable porque son cinco maquinas.
    3. Los numeros vienen como texto con coma decimal y los huecos son '-'
       literal, no celda vacia.

    OJO CON LA INTERPRETACION. Esto es la rampa que la planta declara para el
    REDESPACHO ECONOMICO, en minutos. No es la velocidad del regulador de
    velocidad, que es bastante mas rapida: la respuesta primaria completa se
    exige en decenas de segundos, no en minutos. Usar este valor como tope de
    la primaria es CONSERVADOR. Se expone para poder medir la sensibilidad,
    no porque sea el parametro fisicamente correcto para el nadir.

    Devuelve {'hidraulica': %/min, 'termica': %/min}.
    """
    import numpy as np
    import pandas as pd

    def _maxtramo(v):
        t = str(v)
        if '\n' in t:
            vals = [_num_opt(x) for x in t.split('\n')]
            vals = [x for x in vals if x == x]
            return max(vals) if vals else float('nan')
        return _num_opt(v)

    fuentes = [('hidraulica',
                f'{DIR_PARATEC}/Generacion/PARATEC_Uhidraulica_20-09-2026.xlsx',
                'Uhidráulica'),
               ('termica',
                f'{DIR_PARATEC}/Generacion/PARATEC_Utermica_20-09-2026.xlsx',
                'InfoCombustible')]
    out = {}
    for tec, archivo, hoja_cap in fuentes:
        try:
            vel = pd.read_excel(archivo, sheet_name='VelCarga-Descarga',
                                header=5)
            cap = pd.read_excel(archivo, sheet_name=hoja_cap, header=5)
        except (FileNotFoundError, ValueError):
            continue
        c_vel = _col(vel, 'Velocidad de carga')
        c_cap = _col(cap, 'Capacidad efectiva neta')
        if c_vel is None or c_cap is None:
            continue
        mw_min = vel[c_vel].apply(_maxtramo).sum()
        placa = cap[c_cap].apply(_num_opt).sum()
        if placa > 0:
            out[tec] = 100.0 * float(mw_min) / float(placa)
    return out


@functools.lru_cache(maxsize=1)
def _bahias(archivo=ARCHIVO_BAHIAS):
    df = pd.read_excel(archivo, sheet_name='Bahías', header=5).dropna(how='all')
    df = df[df['Nombre'].notna()].copy()
    df['v'] = df['Voltaje nominal de operación [kV]'].apply(parse_float)
    filas = []
    for _, r in df.iterrows():
        for el in str(r['Elementos que conecta'] or '').split('\n'):
            el = el.strip()
            if el and el != '-':
                filas.append({'sub': r['Subestación'], 'v': r['v'], 'el': el})
    return pd.DataFrame(filas).drop_duplicates()


def autotransformadores(archivo=ARCHIVO_BAHIAS):
    """Potencia nominal de los bancos 500/230 kV por subestacion.

    Devuelve {(subestacion_normalizada, 500.0, 230.0): (s_nom_MVA, n_bancos)}
    sumando todos los bancos declarados en esa subestacion.
    """
    d = _bahias(archivo)
    acc = {}
    for _, r in d.iterrows():
        m = _TRAFO_RE.search(r['el'])
        if not m:
            continue
        v1, v2 = _num(m.group('v1')), _num(m.group('v2'))
        # solo los que unen 500 kV con el nivel de 220/230
        if not (v1 >= 400 and 200 <= v2 < 400):
            continue
        sub = _sub_norm(r['sub'])
        clave = (sub, 500.0, 230.0)
        mva = _num(m.group('mva'))
        # el mismo banco aparece en varias bahias; se cuenta una vez por nombre
        acc.setdefault(clave, {})[r['el']] = mva
    return {k: (sum(v.values()), len(v)) for k, v in acc.items()}


def compensacion_stn(archivo=ARCHIVO_BAHIAS, kv_min=200.0):
    """Reactores de barra y condensadores declarados en barras del STN.

    OJO: esto es capacidad DE PLACA, no el estado de operacion. El escenario
    del reporte de PowerFactory tiene parte de este equipo fuera de servicio
    (por ejemplo "Rea Banadia 1/2 12.5 MVAR" aparece con Q = 0,0 Mvar), asi
    que meterlo como shunt fijo cambiaria el punto de operacion que se quiere
    reproducir. Se extrae para documentar y comparar, no para alimentar el
    modelo.
    """
    d = _bahias(archivo)
    out = []
    for _, r in d.iterrows():
        u = r['el'].upper()
        if 'REACTOR DE BARRA' in u:
            tipo = 'reactor'
        elif 'CONDENSADOR' in u or 'CAPACITOR' in u:
            tipo = 'condensador'
        elif 'SVC' in u or 'STATCOM' in u or 'COMPENSADOR' in u:
            tipo = 'compensador'
        else:
            continue
        if pd.isna(r['v']) or r['v'] < kv_min or r['v'] > 550:
            continue
        m = _MVAR_RE.search(r['el'])
        out.append({'sub': _sub_norm(r['sub']), 'v': r['v'],
                    'tipo': tipo, 'mvar': _num(m.group('mvar')) if m else None,
                    'nombre': r['el']})
    return pd.DataFrame(out)


def capacidad_plantas():
    """Capacidad efectiva neta por planta, de los reportes de generacion.

    Los ficheros guardan las capacidades como TEXTO con punto de separador de
    miles ('1.000' para Chivor), asi que hay que leerlos con dtype=str y pasar
    parse_float. Si se dejan a pandas, '1.000' se convierte en 1,0 y se pierden
    las centrales grandes: Chivor, Guavio, Ituango y San Carlos suman 4.690 MW
    que desaparecerian (12.256 MW de hidraulica se quedarian en 7.571).
    """
    out = []
    for archivo, hoja, col, tipo in ARCHIVOS_GENERACION:
        try:
            d = pd.read_excel(archivo, sheet_name=hoja, header=5, dtype=str)
        except FileNotFoundError:
            continue
        d = d.dropna(how='all')
        d = d[d['Nombre'].notna()]
        if col not in d.columns:
            continue
        d = d[['Nombre', col]].copy()
        d.columns = ['planta', 'mw']
        d['mw'] = d['mw'].apply(parse_float)
        d['tipo'] = tipo
        # una planta puede repetirse (varios combustibles); se toma el maximo
        d = d.groupby(['planta', 'tipo'], as_index=False)['mw'].max()
        out.append(d)
    if not out:
        return pd.DataFrame(columns=['planta', 'tipo', 'mw', 'sub'])
    cap = pd.concat(out, ignore_index=True)
    cap['sub'] = cap.planta.map(_sub_norm)
    return cap


def contraste_generacion(network):
    """Compara la generacion asignada a cada bus con la capacidad instalada
    de la central del mismo nombre.

    Es una comprobacion de sanidad del equivalente: ningun bus deberia tener
    asignada mas potencia de la que la central puede dar.
    """
    cap = capacidad_plantas().groupby('sub')['mw'].sum()
    gen = network.generators.groupby('bus').p_set.sum()
    filas = []
    for bus, p in gen.items():
        sub = bus.rsplit('__', 1)[0]
        if sub in cap.index and cap[sub] > 0:
            filas.append({'bus': bus, 'asignado_MW': round(float(p), 1),
                          'instalado_MW': round(float(cap[sub]), 1),
                          'ratio': round(float(p) / float(cap[sub]), 2)})
    return pd.DataFrame(filas).sort_values('ratio', ascending=False)


def informe():
    at = autotransformadores()
    print(f"### Autotransformadores 500/230 en PARATEC: "
          f"{len(at)} subestaciones, {sum(n for _, n in at.values())} bancos")
    for (sub, a, b), (mva, n) in sorted(at.items()):
        print(f"   {sub:24s} {n} banco(s)  {mva:7.0f} MVA")
    comp = compensacion_stn()
    print(f"\n### Compensacion reactiva declarada en barras >=200 kV: {len(comp)}")
    if len(comp):
        print(comp.groupby('tipo')['mvar'].agg(['count', 'sum']).to_string())
    cap = capacidad_plantas()
    print("\n### Capacidad instalada por tipo:")
    print(cap.groupby('tipo')['mw'].agg(['count', 'sum']).round(1).to_string())
    print(f"   TOTAL: {cap.mw.sum():.1f} MW")


if __name__ == '__main__':
    informe()
