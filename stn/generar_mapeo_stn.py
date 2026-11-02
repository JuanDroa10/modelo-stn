"""
generar_mapeo_stn.py

Construye el mapeo entre las barras del reporte de PowerFactory y las
subestaciones de la red de lineas, y lo escribe en Datos/mapeo_STN.csv para
revisarlo a mano.

El mapeo anterior (mapeo_subestaciones_lineas.csv) relacionaba subestaciones
con LINEAS, de modo que cada subestacion acababa asignada a los dos extremos
de todas sus lineas (4,95 buses de media, hasta 11) y su P y Q se sumaban
completas en cada uno. Este mapeo es uno a uno.

Estrategias de asignacion, en orden de fiabilidad:

  exacto        el nombre normalizado y el nivel coinciden con un bus.
  base_unica    el nombre coincide con una unica subestacion de la red al
                quitarle el calificativo entre parentesis. El reporte los
                omite: "MOCOA" -> "MOCOA (JUNIN)", "SURIA" -> "SURIA (DELSUR)".
  vecino_linea  la barra no existe en el CSV de lineas (patio de central,
                colectora eolica). Se lee el nombre de sus propias lineas en
                el reporte para averiguar a que subestacion del STN entrega:
                "Gen 5 Ituango - Antioquia 500" -> ANTIOQUIA.
  regla         asignacion manual declarada en REGLAS_CENTRALES.
  difuso        parecido de texto alto (>= UMBRAL_DIFUSO). Se propone pero
                se marca para revisar.
  revisar       no hay asignacion fiable. Bus_red se deja VACIO a proposito:
                es preferible perder la inyeccion a colocarla en el bus
                equivocado sin que nadie se entere.

Uso:
    python generar_mapeo_stn.py
"""

import difflib
import functools
import re

import pandas as pd

from stn.config import FILE_PQ, FILE_MAPPING, STN_KV_MIN
from stn.data_loader import load_lines, clean_bus_name, nivel_tension, bus_id

# Tension pegada al final del nombre: "San Carlos 220", "Guajira 13.8".
_TENSION_FINAL_RE = re.compile(r'\s+\d{1,3}(?:[.,]\d+)?\s*$')
# Circuito + tension al final del nombre de una linea: "Miraflores 1 220".
_CIRCUITO_TENSION_RE = re.compile(r'\s+\d+\s+\d{2,3}(?:[.,]\d+)?\s*(?:KV)?\s*$',
                                  re.IGNORECASE)
_PARENTESIS_RE = re.compile(r'\s*\([^)]*\)')

UMBRAL_DIFUSO = 0.85
UMBRAL_IRRELEVANTE = 0.5   # MW y Mvar por debajo de los cuales la barra no aporta

# Barras cuyo enganche al STN no se puede deducir del reporte y se decide
# a mano. Se aplica sobre el nombre ya normalizado.
REGLAS_CENTRALES = {
    'CASA MAQUINAS PORCE 3 1': 'PORCE III',
    'CASA MAQUINAS PORCE 3 2': 'PORCE III',
    'GUAVIO GEN': 'GUAVIO',
}

# Asignaciones resueltas caso por caso. La clave es (nodo normalizado, nivel)
# y el valor (subestacion_de_red, confirmado).
#
# confirmado=True  -> la evidencia es del propio reporte y encaja con las
#                     lineas del CSV de PARATEC; no hace falta revisarla.
# confirmado=False -> la asignacion viene de una fuente externa y conviene
#                     que la valide alguien que conozca el sistema.
MAPEO_MANUAL = {
    # -- confirmadas: los nombres de linea del reporte coinciden uno a uno
    #    con las lineas del CSV de la red ------------------------------------
    # "Hidrosogamoso - Sogamoso 1/2/3 230" <-> 3 lineas HIDROSOGAMOSO-SOGAMOSO
    ('WILCHES', 230.0): ('HIDROSOGAMOSO', True),
    # "Alto Anchi - Yumbo 1 230" y "Alto Anchi - Pance 1 230"
    ('ALTO ANCHYA', 230.0): ('ALTO ANCHICAYA', True),
    # "La Miel - San Felipe 1/2 230" <-> 2 lineas MIEL I-SAN FELIPE
    ('LA MIEL', 230.0): ('MIEL I', True),
    # "Nva Esperanza - San Mateo 1 230" y "San Mateo - Tunal 1 230"
    ('SAN MATEO', 230.0): ('SAN MATEO (BOGOTA)', True),
    # trafos "Bucaramanga 4/5 230/115" y linea "Bmanga - Sogamoso 1 230"
    ('BMANGA', 230.0): ('BUCARAMANGA', True),
    # "CiraInfanta - Comuneros 1 230"
    ('CIRAINFANTA', 230.0): ('LA CIRA INFANTAS', True),
    # trafos "Rio Cordoba 3/4 220/110"
    ('R CORDOBA', 230.0): ('RIO CORDOBA', True),
    # trafo "San Bernardino 230/115"
    ('S BERNARDINO', 230.0): ('SAN BERNARDINO', True),
    # trafo "Nv Barranquilla 2 220/110/13.8"
    ('NV BARRANQUILLA', 230.0): ('NUEVA BARRANQUILLA', True),
    # "Cuestecitas(TRC) - San Juan 1 220" <-> CUESTECITAS-SAN JUAN (GEB)
    ('CUESTECITA', 230.0): ('CUESTECITAS', True),
    # trafos "Gen Salvajina 230/13.8" x2: es el patio de la central Salvajina
    ('PAPEL CAUCA', 230.0): ('SALVAJINA', True),
    # trafo "Cuestecitas 500/230": lado de 230 del autotransformador
    ('CUESTECITA 220 II', 230.0): ('NUEVA CUESTECITAS', True),
    # trafos "Flores 6/10 220/110": patio de Termoflores en Barranquilla
    ('FLORES', 230.0): ('TERMOFLORES', True),
    # El reporte da la linea "Cucuta - Tasajero 1 230" y el terminal
    # "PT Cucuta 220- San Mateo". En el CSV, BELEN (CUCUTA) tiene exactamente
    # esas dos lineas y ninguna mas; SAN MATEO (CUCUTA) tendria ademas una a
    # OCANA, que el reporte no lista. Por eso es Belen y no San Mateo.
    ('CUCUTA', 230.0): ('BELEN (CUCUTA)', True),
    # Los nodos "Ficticia" del modelo de PowerFactory son cargas reales de
    # campo petrolero (Quifa). El reporte de Bahias de PARATEC declara en
    # CHIVOR las bahias "BL1 CHIVOR A COROCORA (QUIFA) 230 kV" y
    # "BL1 CHIVOR A JAGUEY 230 kV", asi que cuelgan radialmente de Chivor 230.
    # Sus lineas no son activos de uso STN y no estan en el detalle de lineas,
    # de modo que la carga se lleva a la barra de Chivor, igual que se hace con
    # todo lo que cuelga por debajo del STN. No hay doble conteo: la barra de
    # Chivor del reporte no lista ningun flujo hacia ellas.
    ('COROCORA FICTICIA', 230.0): ('CHIVOR', True),
    # La red modela el extremo ecuatoriano del enlace de Jamondino como
    # PIMAMPIRO (cuatro circuitos). Pomasqui es ese mismo extremo: en este
    # modelo solo hay un nodo de Ecuador, y no tenia datos asignados.
    ('POMASQUI 220KV', 230.0): ('PIMAMPIRO', False),
    # Central sobre el Samana Norte. Dos fuentes independientes la conectan a
    # San Carlos; el reporte la modela en 500 kV, asi que se lleva al patio de
    # 500 de San Carlos. El nivel es lo unico que discrepa entre fuentes.
    ('PORVENR II', 500.0): ('SAN CARLOS', False),
    # Ambas se conectan seccionando una linea existente; complementos.py crea
    # el bus y parte el corredor en el punto geografico real.
    ('CARRETO', 500.0): ('CARRETO', True),
    ('QUEBRADONA', 230.0): ('QUEBRADONA', True),
    ('JAGUEY FICTICIA', 230.0): ('CHIVOR', True),

    # -- de fuente externa, corroboradas despues contra el CSV de lineas ---
    # El proyecto Colectora (UPME 06-2017) une la colectora eolica de La
    # Guajira con Cuestecitas por dos lineas de 500 kV de 114 km.
    ('COLECTORA 1 EOLICA', 500.0): ('NUEVA CUESTECITAS', True),
    # La nueva subestacion Pacifico 230 kV (UPME 01-2019, Buenaventura)
    # se conecta con San Marcos, en Yumbo, por doble circuito de 74 km.
    ('PACIFICO', 230.0): ('SAN MARCOS (VALLE)', True),
    # ISA nombra indistintamente "Betania - Mirolindo" y "Betania - Ibague"
    # al mismo activo de 230 kV; Mirolindo es la subestacion de Ibague.
    ('MIROLINDO', 230.0): ('IBAGUE', True),
}

# Nodos que a proposito se quedan sin bus, con el motivo. Se informan en el
# CSV para que no parezcan un olvido.
SIN_EQUIVALENTE = {
    # El lado de 500 kV de Norte SI se modela: complementos.py anade la
    # subestacion y sus dos lineas. El lado de 230 kV es el otro extremo del
    # MISMO autotransformador, asi que mapearlo duplicaria los 175,5 MW: la
    # potencia ya entra al modelo como retiro en NORTE__500.
    ('NORTE', 230.0): 'otro lado del autotransformador de Norte; mapearlo duplicaria los 175,5 MW de NORTE 500',
    ('TERM', 500.0): 'parque eolico "Costa Atla"; no aparece en ningun fichero de PARATEC',
    ('TERM', 230.0): 'parque eolico "Costa Atla"; no aparece en ningun fichero de PARATEC',
}

# Barras del reporte que son reactores de linea y que el modelo ya incorpora
# como ShuntImpedance desde complementos.py, con la potencia que da el propio
# reporte. Aqui se marcan como resueltas para que no aparezcan como pendientes.
MODELADO_APARTE = {
    ('R NORTESOG', 500.0): 'reactor de la linea Norte-Sogamoso, en la barra de NORTE 500',
    ('R SOGNORTE', 500.0): 'reactor de la linea Sogamoso-Norte, en la barra de SOGAMOSO 500',
    ('R NORTE - NUEVA ESPE', 500.0): 'reactor de la linea Norte-Nueva Esperanza, en la barra de NORTE 500',
    ('R NUEVA ESPE - NORTE', 500.0): 'reactor de la linea Nueva Esperanza-Norte, en la barra de NUEVA ESPERANZA 500',
    ('REA NVESPVIR', 500.0): 'reactor de la linea Nueva Esperanza-La Virginia, en la barra de NUEVA ESPERANZA 500',
    ('REA VIRNVESP', 500.0): 'reactor de la linea La Virginia-Nueva Esperanza, en la barra de LA VIRGINIA 500',
}

# Nombres que denotan un patio de central o una colectora, no una
# subestacion del STN. Solo para estos tiene sentido engancharse a la
# subestacion vecina que aparece en el nombre de sus lineas.
_PATIO_CENTRAL_RE = re.compile(
    r'(^|\s)(U\d+|GEN|CASA MAQUINAS|COLECTORA|CENTRAL|PLANTA)(\s|$)')
# Designador de unidad al final: "Quimbo U1", "Ituango U5".
_UNIDAD_RE = re.compile(r'\s+U\d+\s*$')


def normaliza_pdf(nombre):
    """Normaliza un nombre del reporte quitandole la tension final."""
    return _TENSION_FINAL_RE.sub('', clean_bus_name(nombre)).strip()


def _normaliza_extremo_linea(txt):
    """Normaliza un extremo tomado del nombre de una linea."""
    s = clean_bus_name(txt)
    s = _CIRCUITO_TENSION_RE.sub('', s)
    s = _TENSION_FINAL_RE.sub('', s)
    return s.strip()


def _sin_parentesis(nombre):
    return re.sub(r'\s+', ' ', _PARENTESIS_RE.sub('', nombre)).strip()


def _tokens(nombre):
    """Palabras del nombre, incluidas las de dentro de los parentesis."""
    return frozenset(t for t in re.split(r'[^A-Z0-9]+', nombre.upper()) if t)


def _es_patio_central(nodo):
    return bool(_PATIO_CENTRAL_RE.search(nodo))


# Relacion de transformacion dentro del nombre del equipo: "500/230",
# "500/110", "220/110/13.8". Exige tres digitos a cada lado para no confundirse
# con los numeros de unidad ("Bucaramanga 4/5 230/115").
_RELACION_RE = re.compile(r'(\d{3})\s*/\s*(\d{3})')


def _categoria(t, nombre=''):
    t = str(t)
    if t == 'Line':
        return 'linea'
    if 'Transformer' in t:
        # Un autotransformador que une dos niveles que AMBOS estan en el modelo
        # (500/230) no se resta: ese transporte ya lo hace el Transformer de
        # PyPSA. Restarlo ademas creaba un par de inyecciones fantasma, carga
        # en el lado de 500 y generacion en el de 230, que suplantaban al
        # transformador fisico y lo dejaban casi descargado. En Bacata el
        # modelo llevaba 55 MW donde el caso real lleva 549. El error se
        # cancelaba en la suma global (+X y -X), por eso el balance no lo
        # delataba, pero dejaba los 9 autotransformadores del STN sin carga y
        # el N-1 de transformador no podia encontrar una sola sobrecarga.
        m = _RELACION_RE.search(str(nombre))
        if m and int(m.group(1)) >= STN_KV_MIN and int(m.group(2)) >= STN_KV_MIN:
            return 'trafo_interno'
        return 'trafo'
    if t == 'General Load':
        return 'carga'
    if 'Machine' in t or t in ('Static Generator', 'AC Voltage Source'):
        return 'gen'
    if 'Static Var' in t:
        return 'svc'
    if 'Shunt' in t:
        return 'shunt'
    return 'otro'


def cargar_reporte(path=FILE_PQ):
    df = pd.read_csv(path, encoding='utf-8-sig')
    for c in ['Active Power MW', 'Reactive Power Mvar',
              'Nominal Line-Line Voltage kV']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=['Active Power MW'])
    df['nivel'] = df['Nominal Line-Line Voltage kV'].apply(nivel_tension)
    df['cat'] = [_categoria(t, n) for t, n in
                 zip(df['Element type'], df.get('Name', ''))]
    df['nodo'] = df['Subestacion'].map(normaliza_pdf)
    df = _desdoblar(df)
    df = _demote_trafo_interno(df)   # necesita la columna 'nodo' ya creada
    return df


# A que barra del STN pertenece cada SVC. Se declara a mano y no por parecido
# de nombre: son tres equipos, sus nodos en el reporte no se parecen a los del
# STN ("C Limon 34_5", "SVC Tunal", "Chinu SVS 11") y una asignacion silenciosa
# equivocada moveria decenas de Mvar a la barra que no es.
SVC_NODO_STN = {
    'C LIMON 34': ('CANO LIMON', 230.0),   # SVC OXY, 34,5 kV
    'SVC TUNAL': ('TUNAL', 230.0),         # 19,8 kV
    'CHINU SVS': ('CHINU', 500.0),         # 11 kV; PARATEC lo da como 500 kV
}


def _correccion_svc(df, indice):
    """Saca del equivalente el reactivo de los SVC.

    Un SVC no es demanda: es un regulador. Lo que absorbe o entrega no es un
    dato del escenario sino la RESPUESTA del equipo a ese escenario, y en otro
    punto de operacion habria dado otra cosa. Metido en la carga equivalente se
    convierte en una demanda fija incapaz de responder, y en un radial largo
    eso se realimenta solo.

    Los tres SVC del sistema (Cano Limon 34,5 kV, Tunal 19,8 kV, Chinu 11 kV)
    estan DEBAJO del nivel del STN, asi que el filtro de tension ya los deja
    fuera del pivote y no se pueden descontar como si fueran un shunt de barra.
    Su reactivo llega al equivalente por otra via: dentro de la Q que los
    transformadores de esa subestacion retiran de la barra del STN. Cano Limon
    230 no tiene ningun elemento propio salvo cuatro trafos con 60,7 Mvar, de
    los cuales 36,7 son el SVC de 34,5 kV. Por eso hay que devolverlos aqui y
    no filtrarlos por categoria.

    Efecto medido: Cano Limon pasa de 0,842 a 0,993 pu (el caso real la tiene
    en 1,014), el error medio de tension del sistema baja de 0,0147 a 0,0106 pu
    y las contingencias con tension baja del barrido N-1 pasan de 4 a 0.

    Queda una deuda explicita: el SVC no se modela. La barra se comporta como
    si el equipo estuviera a la deriva en vez de regulando. Es conservador
    frente a congelarlo, pero en una contingencia el SVC si responderia.
    """
    import pandas as pd
    corr = pd.Series(0.0, index=indice)
    for nodo, g in df[df['nodo'].isin(SVC_NODO_STN)].groupby('nodo'):
        svc = g[g['cat'] == 'svc']
        if svc.empty:
            continue
        destino = SVC_NODO_STN.get(nodo)
        if destino is None or destino not in corr.index:
            print(f"  aviso: el SVC '{nodo}' no tiene barra del STN asignada; "
                  f"sus {svc['Reactive Power Mvar'].sum():.1f} Mvar se quedan "
                  f"dentro de la carga equivalente")
            continue
        # Un SVC no es solo su TCR: lleva ramales de filtro permanentemente
        # conectados que son los que le dan el extremo capacitivo de su rango.
        # El reporte los nombra asi ("FILT 5 SVC Tunal", "FILT 7 SVC Tunal",
        # "Shunt/Filter" en Chinu), y hay que devolverlos con el SVC porque
        # son el mismo equipo: en Tunal el TCR absorbe 58,0 Mvar y sus filtros
        # entregan 79,7, de modo que la instalacion ENTREGA 21,7 netos. Contar
        # solo el TCR le daba el signo contrario.
        #
        # Los bancos de condensadores independientes NO entran aunque compartan
        # barra: los "Cap Cano Limon" son compensacion fija, no parte del SVC,
        # y sacarlos empeoraba esa barra de 0,9906 a 0,9589 pu.
        nombres = g['Name'].astype(str).str.upper()
        filtros = g[(g['cat'] == 'shunt') & nombres.str.contains('FILT', na=False)]
        corr[destino] += float(svc['Reactive Power Mvar'].sum()
                               + filtros['Reactive Power Mvar'].sum())
    return corr


@functools.lru_cache(maxsize=1)
def _subestaciones_con_dos_niveles():
    """Subestaciones que el modelo tiene a 500 Y a 230 kV.

    Es la condicion que hace valida la regla 'trafo_interno': solo si el
    modelo tiene las dos barras, network_builder pone un Transformer entre
    ellas y el transporte queda representado. Si falta una, excluir el
    autotransformador del equivalente no traslada nada, lo BORRA.

    Paso justo eso por alto con NORTE. complementos.py anade Norte solo a
    500 kV, y en el reporte el autotransformador es el UNICO elemento de esa
    barra: al marcarlo 'trafo_interno' sus 175,5 MW de demanda real dejaron de
    existir. El balance del equivalente se fue de -6,3 a +169,2 MW, y como el
    modelo de frecuencia no tiene perdidas, ese superavit actuaba de reserva
    gratis: una perdida de 138 MW SUBIA la frecuencia a 60,14 Hz.
    """
    from stn.data_loader import load_lines
    niveles = {}
    lines = load_lines(verbose=False)
    for _, r in lines.iterrows():
        for c in ('sub0', 'sub1'):
            niveles.setdefault(str(r[c]), set()).add(float(r['nivel']))
    return frozenset(s for s, n in niveles.items() if len(n) > 1)


# Nodos del reporte que agrupan DOS subestaciones distintas y hay que separar.
# La clave es (nodo, nivel) y el valor una lista de (patron, nodo_destino):
# las filas cuyo nombre de elemento case con el patron se reasignan.
#
# El caso de Wilches: el reporte etiqueta bajo 'Wilches 115' tanto los tres
# elevadores de Hidrosogamoso (Gen Sogamoso 1/2/3, -250 MW cada uno) como los
# dos transformadores 230/115 de Palenque (+64,8 y +64,7). Al agregarlos en un
# solo nodo, el equivalente inyectaba 620,5 MW en Hidrosogamoso -750 de las
# maquinas menos 129,5 de Palenque- y dejaba Palenque en cero.
#
# Eso producia cinco desviaciones a la vez frente al caso de referencia, todas
# coherentes entre si: los tres circuitos Hidrosogamoso-Sogamoso llevaban 248,6
# / 248,6 / 123,3 MW en vez de 250 cada uno, y los dos Palenque-Sogamoso 56,4
# en vez de 128,1. Un desvio es el espejo del otro: los 129,5 MW estaban en la
# barra equivocada.
DESDOBLAR = {
    ('WILCHES', 230.0): [(r'^Palenque\b', 'PALENQUE')],
}


# NOTA sobre Guatape, para que nadie repita el intento.
#
# El reporte trae cuatro elementos 'Guatape Gen X-Y - Guatape 220' tipificados
# como Line, con -140 MW cada uno: 560 MW que entran a la barra y que el
# equivalente descarta, porque excluye las lineas a proposito. La barra queda
# con P_iny = 0,0 exacta y la regla 'irrelevante' la da por resuelta.
#
# Se probo reclasificarlos como 'trafo' para recuperarlos. NO FUNCIONA, y el
# motivo es el mismo que con los autotransformadores 500/230: esa potencia sale
# de Guatape por tres lineas que SI estan modeladas -a Miraflores, Oriente y
# Envigado-, y las barras que la reciben ya la contabilizan en sus propias
# inyecciones. Anadirla en Guatape la cuenta dos veces.
#
# Medido: el balance del equivalente pasa de -6,3 a +553,7 MW y el slack tiene
# que bajar de 1.200 a 775 MW para absorber el excedente, falseando el despacho
# de San Carlos en 425 MW. La correlacion de flujos mejoraba de 0,825 a 0,847,
# pero por la razon equivocada: mas generacion empujaba los flujos en la
# direccion correcta mientras rompia el balance.
#
# Recuperar Guatape de verdad exige modelar sus lineas de unidad como ramas,
# no convertirlas en inyeccion. Quedan por tanto 560 MW fuera del modelo, y es
# la razon de que sus tres corredores lleven menos flujo del real.


def _desdoblar(df):
    """Separa los nodos del reporte que agrupan dos subestaciones."""
    for (nodo, nivel), reglas in DESDOBLAR.items():
        for patron, destino in reglas:
            m = ((df['nodo'] == nodo) & (df['nivel'] == nivel)
                 & df['Name'].astype(str).str.match(patron, na=False))
            if m.any():
                df.loc[m, 'nodo'] = destino
    return df


def _demote_trafo_interno(df):
    """Devuelve a 'trafo' los autotransformadores sin Transformer en el modelo.

    Se compara el nombre del nodo con las subestaciones de la red y, si no
    casa directamente, con el destino declarado en MAPEO_MANUAL (el reporte
    llama 'CUESTECITA 220 II' a lo que la red llama 'NUEVA CUESTECITAS').
    """
    dobles = _subestaciones_con_dos_niveles()
    destino = {k[0]: v[0] for k, v in MAPEO_MANUAL.items()}
    mask = df['cat'] == 'trafo_interno'
    for nodo in df.loc[mask, 'nodo'].unique():
        if nodo in dobles or destino.get(nodo) in dobles:
            continue
        # El reporte abrevia: "VIRGINIA" por "LA VIRGINIA", "COPEY" por
        # "EL COPEY", "SAN MARCOS" por "SAN MARCOS (VALLE)". Sin esto se
        # degradaban ocho autotransformadores buenos y el equivalente perdia
        # la mitad de su balance.
        tk = _tokens(nodo)
        if tk and sum(1 for s in dobles if tk <= _tokens(s)) == 1:
            continue
        df.loc[mask & (df['nodo'] == nodo), 'cat'] = 'trafo'
    return df


def nodos_stn(df):
    """Inyeccion neta equivalente por barra del STN.

    Convencion del reporte, verificada contra el balance por barra:
      - Line / Transformer / Load / Shunt: positivo = sale de la barra.
      - Synchronous Machine / Static Generator: positivo = inyecta.

    La inyeccion equivalente de una barra del STN es:
        gen_directa - (trafos + cargas + shunts)
    Los transformadores con P>0 bajan potencia a 110/115 kV (demanda) y los
    que tienen P<0 la suben desde los generadores de 13,8-19 kV.

    Solo entran aqui los transformadores que salen del modelo, categoria
    'trafo'. Los autotransformadores 500/230, categoria 'trafo_interno', se
    dejan fuera a proposito: unen dos barras que el modelo ya tiene y su
    transporte lo representa el Transformer de PyPSA. Tampoco entran los SVC,
    categoria 'svc', porque son reguladores y no demanda. Ver _categoria().
    """
    stn = df[df['nivel'] >= STN_KV_MIN]
    idx = ['nodo', 'nivel']
    p = stn.pivot_table(index=idx, columns='cat', values='Active Power MW',
                        aggfunc='sum').fillna(0.0)
    q = stn.pivot_table(index=idx, columns='cat', values='Reactive Power Mvar',
                        aggfunc='sum').fillna(0.0)
    for c in ['linea', 'trafo', 'trafo_interno', 'carga', 'gen', 'shunt',
              'svc', 'otro']:
        if c not in p:
            p[c] = 0.0
        if c not in q:
            q[c] = 0.0
    out = pd.DataFrame(index=p.index)
    out['P_iny'] = p['gen'] - (p['trafo'] + p['carga'] + p['shunt'] + p['otro'])
    out['Q_iny'] = q['gen'] - (q['trafo'] + q['carga'] + q['shunt'] + q['otro'])
    out['Q_iny'] += _correccion_svc(df, out.index)
    out['n_lineas'] = (stn[stn.cat == 'linea'].groupby(idx).size()
                       .reindex(out.index).fillna(0).astype(int))
    return out


def generacion_por_convertidor(df=None):
    """MW que entran al STN por un transformador de CONVERTIDOR, por nodo.

    POR QUE HACE FALTA. El reporte no tiene una columna de tecnologia, asi que
    la generacion que entra por transformador se clasificaba como
    'desconocido' y el modelo le atribuia H = 4,0 s. Pero una parte de esa
    generacion es eolica conectada por electronica de potencia, que **no
    aporta inercia sincrona ninguna**: su H es cero.

    Se reconoce por el secundario. Un transformador de parque eolico eleva
    desde 0,69 kV ---la tension de salida del convertidor--- y el reporte lo
    nombra asi: "Trf Eol Alpha 500/0.69", "Trf Kuisa 500/0.69kV". Ese 0,69 es
    la firma inequivoca: ninguna maquina sincrona del STN genera a esa
    tension.

    OJO CON EL FILTRO OBVIO. Buscar "Eol" en el nombre NO basta: de los diez
    transformadores del complejo de La Guajira, cuatro se llaman Trf Carrizal,
    Trf Casa Electrica, Trf Kuisa, Trf Irraipa y Trf Urraichi, sin mencionar
    que son eolicos. Solo el 0,69 kV los delata a todos.

    Devuelve una Serie indexada por (nodo, nivel) con los MW que inyecta el
    convertidor, en positivo.
    """
    if df is None:
        df = cargar_reporte()
    nombre = df['Name'].astype(str)
    es_conv = (nombre.str.contains(r'0[.,]69', regex=True, na=False)
               | nombre.str.contains('Eol', case=False, na=False))
    conv = df[es_conv & (df['nivel'] >= STN_KV_MIN) & (df['cat'] == 'trafo')]
    if not len(conv):
        return pd.Series(dtype=float)
    # el reporte da positivo = sale de la barra, asi que lo que inyecta es
    # el valor negado
    s = -conv.groupby(['nodo', 'nivel'])['Active Power MW'].sum()
    return s[s > 0]


def convertidor_por_bus(mapeo=None, df=None):
    """Lo mismo, pero agregado al bus del modelo. Serie bus -> MW."""
    s = generacion_por_convertidor(df)
    if not len(s):
        return pd.Series(dtype=float)
    if mapeo is None:
        from stn.data_integration import cargar_mapeo
        mapeo = cargar_mapeo()
    m = mapeo[mapeo.Bus_red != '']
    clave = {(str(a).upper().strip(), float(b)): c
             for a, b, c in zip(m.Nodo_PDF, m.kV_PDF, m.Bus_red)}
    out = {}
    for (nodo, nivel), mw in s.items():
        bus = clave.get((str(nodo).upper().strip(), float(nivel)))
        if bus:
            out[bus] = out.get(bus, 0.0) + float(mw)
    return pd.Series(out, dtype=float)


def vecinos_por_linea(df):
    """Para cada barra, los nombres de subestacion que aparecen en los
    nombres de sus lineas. Permite enganchar patios de central que no
    figuran en el CSV de lineas."""
    vec = {}
    lineas = df[df.cat == 'linea']
    for (nodo, nivel), g in lineas.groupby(['nodo', 'nivel']):
        nombres = set()
        for nm in g['Name'].dropna().astype(str):
            partes = re.split(r'\s+-\s+', nm)
            if len(partes) < 2:
                continue
            for parte in partes:
                ext = _normaliza_extremo_linea(parte)
                if ext and ext != nodo:
                    nombres.add(ext)
        vec[(nodo, nivel)] = nombres
    return vec


def construir_mapeo(verbose=True):
    lines_df = load_lines(verbose=False)
    red = sorted(set(zip(lines_df.sub0, lines_df.nivel)) |
                 set(zip(lines_df.sub1, lines_df.nivel)))
    por_nivel = {}
    for sub, niv in red:
        por_nivel.setdefault(niv, set()).add(sub)

    # indice por nombre sin el calificativo entre parentesis
    base_idx = {}
    for sub, niv in red:
        base_idx.setdefault((_sin_parentesis(sub), niv), set()).add(sub)

    df = cargar_reporte()
    nodos = nodos_stn(df)
    vecinos = vecinos_por_linea(df)

    filas = []
    for (nodo, nivel), fila in nodos.iterrows():
        disponibles = por_nivel.get(nivel, set())
        P, Q = float(fila['P_iny']), float(fila['Q_iny'])

        metodo, elegido, conf = None, '', 0.0
        candidatos = []
        nota = ''

        if (nodo, nivel) in MAPEO_MANUAL:
            destino, confirmado = MAPEO_MANUAL[(nodo, nivel)]
            if destino in disponibles:
                elegido = destino
                metodo = 'manual' if confirmado else 'manual_externo'
                conf = 1.0 if confirmado else 0.8
            else:
                metodo, conf = 'revisar', 0.0
                nota = f"MAPEO_MANUAL apunta a '{destino}', que no existe en {int(nivel)} kV"

        elif (nodo, nivel) in MODELADO_APARTE:
            metodo, conf = 'modelado_aparte', 1.0
            nota = MODELADO_APARTE[(nodo, nivel)]

        elif (nodo, nivel) in SIN_EQUIVALENTE:
            metodo, conf = 'sin_equivalente', 1.0
            nota = SIN_EQUIVALENTE[(nodo, nivel)]

        elif nodo in disponibles:
            metodo, elegido, conf = 'exacto', nodo, 1.0

        elif len(base_idx.get((nodo, nivel), ())) == 1:
            elegido = next(iter(base_idx[(nodo, nivel)]))
            metodo, conf = 'base_unica', 0.95

        elif nodo in REGLAS_CENTRALES and REGLAS_CENTRALES[nodo] in disponibles:
            metodo, elegido, conf = 'regla', REGLAS_CENTRALES[nodo], 0.9

        else:
            # El reporte suele abreviar: omite articulos y calificativos
            # ("COPEY" por "EL COPEY", "ANCON EPM" por "ANCON SUR (EPM)").
            # Si todas las palabras del nombre del reporte estan contenidas
            # en las de una unica subestacion de la red, es esa.
            base = _UNIDAD_RE.sub('', nodo).strip() or nodo
            tk = _tokens(base)
            contenidos = sorted(s for s in disponibles if tk and tk <= _tokens(s))

            vecinos_ok = sorted(v for v in vecinos.get((nodo, nivel), ())
                                if v in disponibles)

            if len(contenidos) == 1:
                metodo, elegido, conf = 'subconjunto', contenidos[0], 0.9

            elif _es_patio_central(nodo) and len(vecinos_ok) == 1:
                # patio de central o colectora que no existe como
                # subestacion: entrega por la linea que sale de el
                metodo, elegido, conf = 'vecino_linea', vecinos_ok[0], 0.85

            else:
                candidatos = list(contenidos)
                for v in vecinos_ok:
                    if v not in candidatos:
                        candidatos.append(v)
                aprox = difflib.get_close_matches(nodo, sorted(disponibles),
                                                 n=3, cutoff=0.6)
                for a in aprox:
                    if a not in candidatos:
                        candidatos.append(a)
                ratio = (difflib.SequenceMatcher(None, nodo, aprox[0]).ratio()
                         if aprox else 0.0)
                if aprox and ratio >= UMBRAL_DIFUSO and not contenidos:
                    metodo, elegido, conf = 'difuso', aprox[0], round(ratio, 3)
                elif (abs(P) < UMBRAL_IRRELEVANTE
                      and abs(Q) < UMBRAL_IRRELEVANTE):
                    # Ultimo recurso, no primero. Antes esta comprobacion iba
                    # ANTES de intentar emparejar, asi que un nodo con
                    # inyeccion nula nunca llegaba a recibir Bus_red... y con
                    # el perdia su TENSION DE REFERENCIA, que si es un dato
                    # util aunque no inyecte nada. Eso dejo a SAN CARLOS 500
                    # con la consigna por defecto de 1,0 pu mientras su barra
                    # de 230 estaba en 1,077: los 0,077 pu de diferencia a
                    # traves del autotransformador forzaban 859 Mvar
                    # circulando entre los dos generadores de la MISMA
                    # central. Un nodo que casa con una barra se asigna
                    # aunque su inyeccion sea cero; 'irrelevante' queda solo
                    # para los que ademas no casan con nada.
                    metodo, conf = 'irrelevante', 1.0
                else:
                    metodo, conf = 'revisar', round(ratio, 3)

        if len(base_idx.get((nodo, nivel), ())) > 1 and not elegido:
            candidatos = sorted(base_idx[(nodo, nivel)]) + candidatos

        revisar = 'NO' if metodo in ('exacto', 'base_unica', 'regla', 'manual',
                                     'vecino_linea', 'subconjunto', 'irrelevante',
                                     'sin_equivalente', 'modelado_aparte') else 'SI'
        filas.append({
            'Nodo_PDF': nodo,
            'kV_PDF': int(nivel),
            'Inyeccion_MW': round(P, 2),
            'Inyeccion_Mvar': round(Q, 2),
            'Bus_red': bus_id(elegido, nivel) if elegido else '',
            'Metodo': metodo,
            'Confianza': conf,
            'Candidatos': ' | '.join(dict.fromkeys(candidatos)),
            'Nota': nota,
            'REVISAR': revisar,
        })

    mapeo = pd.DataFrame(filas)
    mapeo['_abs'] = mapeo.Inyeccion_MW.abs()
    mapeo = mapeo.sort_values(['REVISAR', '_abs'], ascending=[True, False]).drop(columns='_abs')

    if verbose:
        tot = len(mapeo)
        pend = mapeo[mapeo.REVISAR == 'SI']
        pot_tot = mapeo.Inyeccion_MW.abs().sum()
        pot_pend = pend.Inyeccion_MW.abs().sum()
        print(f"Nodos STN en el reporte      : {tot}")
        print(f"  resueltos automaticamente  : {tot - len(pend)} "
              f"({100*(tot-len(pend))/tot:.0f}%)")
        print(f"  pendientes de revisar      : {len(pend)}")
        print(f"  potencia |P| sin resolver   : {pot_pend:.1f} de {pot_tot:.1f} MW "
              f"({100*pot_pend/max(pot_tot,1e-9):.0f}%)")
        print("\nDesglose por metodo:")
        print(mapeo.Metodo.value_counts().to_string())
        if len(pend):
            print("\nPendientes ordenados por potencia:")
            print(pend[['Nodo_PDF', 'kV_PDF', 'Inyeccion_MW', 'Candidatos']]
                  .head(20).to_string(index=False))
        asignados = set(mapeo.loc[mapeo.Bus_red != '', 'Bus_red'])
        todos = {bus_id(s, n) for s, n in red}
        huerf = sorted(todos - asignados)
        print(f"\nBuses de la red sin datos de P/Q: {len(huerf)} de {len(todos)}")
        if huerf:
            print("  ", huerf[:12])
    return mapeo


def main():
    mapeo = construir_mapeo()
    mapeo.to_csv(FILE_MAPPING, index=False, encoding='utf-8-sig')
    print(f"\nMapeo escrito en {FILE_MAPPING}")
    pend = int((mapeo.REVISAR == 'SI').sum())
    if pend:
        print(f"Revisa las {pend} filas con REVISAR=SI y rellena la columna Bus_red.")
    else:
        print("No queda ninguna fila pendiente de revisar.")


if __name__ == '__main__':
    main()
