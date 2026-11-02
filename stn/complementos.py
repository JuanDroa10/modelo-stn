# complementos.py
"""Elementos de red que existen en el sistema real y en el estudio de
PowerFactory pero que NO estan en el CSV de PARATEC.

Cada entrada lleva su fuente. Se activan con INCLUIR_COMPLEMENTOS en
config.py, de forma que siempre se pueda comparar el modelo con y sin ellos.

Los parametros de linea que no vienen dados por una fuente se estiman con la
mediana por kilometro de las lineas de 500 kV del propio CSV
(R = 0,0244 ohm/km, X = 0,3222 ohm/km, B = 5,128 uS/km). Sobre la linea
CERROMATOSO-PRIMAVERA, de 245 km, esa estimacion reproduce los parametros
reales con menos de un 2% de error, asi que es razonable para lineas de la
misma familia. Aun asi son parametros ESTIMADOS, no medidos.
"""

import re
from stn.paths import PUNTO, dato

# Medianas por kilometro de las lineas de 500 kV del CSV
R_KM_500 = 0.02440      # ohm/km
X_KM_500 = 0.32220      # ohm/km
B_KM_500 = 5.1280e-6    # S/km


# Coordenadas de las subestaciones que anade este modulo y que por tanto no
# estan en el CSV de lineas. Sin esto el bus se dibujaba en (0, 0).
#   Norte 500 kV: vereda San Jose, Gachancipa (Cundinamarca), predio Barro
#   Blanco del GEB, segun el expediente ANLA del proyecto UPME 01-2013.
COORDENADAS = {
    'NORTE': (4.9903, -73.8722),
    'CARRETO': (9.9530, -75.0820),      # San Juan Nepomuceno, Bolivar
    'QUEBRADONA': (5.7906, -75.7869),   # Jerico, Antioquia
}

# Subestaciones que se conectan en entrada-salida sobre una linea existente.
# La linea original se sustituye por dos tramos y los parametros se reparten
# segun la fraccion, que se calcula con la posicion geografica real de la
# subestacion sobre el corredor.
DERIVACIONES = [
    {
        # UPME 06-2021. La ANLA y la UPME describen un seccionamiento en doble
        # circuito sobre Chinu-Sabanalarga 500 kV, quedando Chinu-Carreto y
        # Carreto-Sabanalarga. San Juan Nepomuceno cae al 55,7% del corredor.
        'sobre': 'CHINU - SABANALARGA', 'nivel': 500.0,
        'sub': 'CARRETO', 'fraccion': 0.557,
        'fuente': 'UPME 06-2021 / ANLA',
    },
    {
        # Nodo de demanda del proyecto minero de cobre. Se conecta abriendo la
        # linea Ancon ISA - Esmeralda 220 kV en la zona de Jerico, que cae al
        # 34,4% del corredor desde Ancon Sur (ISA).
        'sobre': 'ANCON SUR (ISA) - ESMERALDA', 'nivel': 230.0,
        'sub': 'QUEBRADONA', 'fraccion': 0.344,
        'fuente': 'expediente ANLA / UPME',
    },
]


# Centrales cuyas unidades tienen cada una su propio circuito de salida.
#
# El reporte de PowerFactory lo muestra sin ambiguedad para Hidrosogamoso:
#
#   Sogamoso 1  Synchronous Machine  250,0 -> Gen Sogamoso 1 -> circuito 1
#   Sogamoso 2  Synchronous Machine  250,0 -> Gen Sogamoso 2 -> circuito 2
#   Sogamoso 3  Synchronous Machine  250,0 -> Gen Sogamoso 3 -> circuito 3
#
# No es un nudo con tres lineas en paralelo compartiendo un flujo: son tres
# caminos independientes unidad-linea. Modelandolo con una sola barra, el
# reparto lo decide la impedancia, y como el circuito 3 es de conductor simple
# -el doble de reactancia que los 1 y 2, que son de doble conductor- salia
# 300,5 / 300,5 / 149 MW en vez de 250 / 250 / 250. Eso ponia los circuitos 1
# y 2 al 104,4% de su limite de 296 MVA: dos sobrecargas FALSAS en el caso
# base, donde el sistema real no tiene ninguna.
#
# La solucion no es igualar las reactancias -serian datos falsos- sino
# representar la topologia: una barra por unidad, cada una con su circuito.
CENTRALES_POR_UNIDAD = {
    ('HIDROSOGAMOSO', 230.0): {'hacia': 'SOGAMOSO', 'unidades': 3,
                               'fuente': 'reporte PowerFactory: Gen Sogamoso 1/2/3'},
}


def unidades_de(sub, nivel):
    """Nombres de los buses por unidad de una central desdoblada."""
    from stn.data_loader import bus_id
    d = CENTRALES_POR_UNIDAD.get((sub, nivel))
    if not d:
        return []
    return [f"{bus_id(sub, nivel)}_U{i+1}" for i in range(d['unidades'])]


def desdoblar_centrales(lines_df):
    """Reasigna cada circuito de salida a la barra de su propia unidad."""
    import pandas as pd
    from stn.data_loader import bus_id
    if not CENTRALES_POR_UNIDAD or not len(lines_df):
        return lines_df
    filas = lines_df.to_dict('records')
    for (sub, nivel), d in CENTRALES_POR_UNIDAD.items():
        base = bus_id(sub, nivel)
        buses = unidades_de(sub, nivel)
        # circuitos que salen de la central hacia el destino declarado
        idx = [i for i, r in enumerate(filas)
               if r['nivel'] == nivel
               and {str(r['sub0']), str(r['sub1'])} == {sub, d['hacia']}]
        if len(idx) != d['unidades']:
            print(f"  aviso: {sub} declara {d['unidades']} unidades pero se "
                  f"encontraron {len(idx)} circuitos; no se desdobla")
            continue
        for k, i in enumerate(sorted(idx, key=lambda j: str(filas[j]['name']))):
            if filas[i]['bus0'] == base:
                filas[i]['bus0'] = buses[k]
            else:
                filas[i]['bus1'] = buses[k]
    return pd.DataFrame(filas)


def aplicar_derivaciones(lines_df):
    """Sustituye cada linea derivada por sus dos tramos.

    Los parametros serie (R, X) y la susceptancia se reparten en proporcion a
    la longitud de cada tramo, que es como se comporta una linea homogenea.
    """
    import pandas as pd
    if not DERIVACIONES or not len(lines_df):
        return lines_df
    from stn.data_loader import bus_id

    filas = lines_df.to_dict('records')
    for d in DERIVACIONES:
        sub, f = d['sub'], float(d['fraccion'])
        lat, lon = COORDENADAS[sub]
        nuevas, quitar = [], []
        for i, r in enumerate(filas):
            if not str(r['name']).startswith(d['sobre']) or r['nivel'] != d['nivel']:
                continue
            quitar.append(i)
            for frac, otro, es_primero in ((f, r['sub0'], True), (1 - f, r['sub1'], False)):
                t = dict(r)
                t['name'] = (f"{r['name']} (tramo {'A' if es_primero else 'B'} por {sub})")
                if es_primero:
                    t['sub1'], t['lat1'], t['lon1'] = sub, lat, lon
                else:
                    t['sub0'], t['lat0'], t['lon0'] = sub, lat, lon
                for c in ('r', 'x', 'b', 'length'):
                    t[c] = r[c] * frac
                t['bus0'] = bus_id(t['sub0'], t['nivel'])
                t['bus1'] = bus_id(t['sub1'], t['nivel'])
                nuevas.append(t)
        if quitar:
            filas = [r for i, r in enumerate(filas) if i not in set(quitar)] + nuevas
    return pd.DataFrame(filas)


def _linea_500(nombre, sub0, sub1, km, fuente):
    lat0, lon0 = COORDENADAS.get(sub0, (float('nan'), float('nan')))
    lat1, lon1 = COORDENADAS.get(sub1, (float('nan'), float('nan')))
    return {
        'name': nombre,
        'sub0': sub0,
        'sub1': sub1,
        'v_nom': 500.0,
        'nivel': 500.0,
        'r': km * R_KM_500,
        'x': km * X_KM_500,
        'b': km * B_KM_500,
        'length': km,
        's_nom': 2078.0,    # mediana de las lineas de 500 kV del CSV
        's_eme': 2400.0,
        'lat0': lat0, 'lon0': lon0,
        'lat1': lat1, 'lon1': lon1,
        'fuente': fuente,
    }


# ---------------------------------------------------------------------------
# Subestacion Norte 500/230 kV  (convocatoria UPME 01-2013)
#
# "Primer refuerzo de red del area oriental": subestacion Norte 500 kV con dos
# bancos de 500/230 kV de 450 MVA, linea Sogamoso-Norte 500 kV de 245 km en
# circuito sencillo y linea Norte-Nueva Esperanza 500 kV de 74 km.
#   https://www.upme.gov.co/convocatorias/convocatoria-publica-upme-01-2013-sogamoso-norte-nueva-esperanza-500-kv/
#
# El corredor NO esta duplicado: el CSV de PARATEC no tiene ninguna linea
# directa SOGAMOSO - NUEVA ESPERANZA a 500 kV, de modo que hoy falta ese
# camino por completo.
#
# El propio reporte de PowerFactory confirma la topologia con los reactores de
# ambas lineas: "Rea Sogamoso 120Mvar" y "Rea Norte 120Mvar" en Sogamoso-Norte,
# "Rea Nva Espe-Norte 40Mvar" y "Rea Norte-Nva Esp 40Mvar" en Norte-Nva Esp.
# ---------------------------------------------------------------------------
LINEAS = [
    _linea_500('SOGAMOSO - NORTE 1 500 kV', 'SOGAMOSO', 'NORTE', 245.0,
               'UPME 01-2013'),
    _linea_500('NORTE - NUEVA ESPERANZA 1 500 kV', 'NORTE', 'NUEVA ESPERANZA',
               74.0, 'UPME 01-2013'),
]

# Reactores de linea, con los valores que da el propio reporte.
# En PyPSA la potencia reactiva inyectada por un ShuntImpedance es Q = |V|^2 * b,
# asi que un reactor (que absorbe) necesita b negativa: b = -Q / v_nom^2.
REACTORES = [
    {'name': 'Rea Sogamoso 120Mvar', 'sub': 'SOGAMOSO', 'nivel': 500.0,
     'q_mvar': 120.0, 'fuente': 'reporte PowerFactory (R SogNorte)'},
    {'name': 'Rea Norte 120Mvar', 'sub': 'NORTE', 'nivel': 500.0,
     'q_mvar': 120.0, 'fuente': 'reporte PowerFactory (R NorteSog)'},
    {'name': 'Rea Norte-Nva Esp 40Mvar', 'sub': 'NORTE', 'nivel': 500.0,
     'q_mvar': 40.0, 'fuente': 'reporte PowerFactory (R Norte - Nva Espe)'},
    {'name': 'Rea Nva Espe-Norte 40Mvar', 'sub': 'NUEVA ESPERANZA',
     'nivel': 500.0, 'q_mvar': 40.0,
     'fuente': 'reporte PowerFactory (R Nva Espe - Norte)'},
    # Reactores de la linea LA VIRGINIA - NUEVA ESPERANZA 500 kV. Esta linea
    # SI esta en el CSV de PARATEC, pero sus reactores figuran en el reporte
    # como nodos propios ("Rea VirNvEsp" / "Rea NvEspVir") y se perdian. El
    # reporte los da EN SERVICIO, con 84,3 y 83,4 Mvar absorbidos.
    {'name': 'Rea Virginia-Nueva Esperanza 84Mvar', 'sub': 'LA VIRGINIA',
     'nivel': 500.0, 'q_mvar': 84.0,
     'fuente': 'reporte PowerFactory (Rea VirNvEsp, 84,3 Mvar en servicio)'},
    {'name': 'Rea Nueva Esperanza-Virginia 84Mvar', 'sub': 'NUEVA ESPERANZA',
     'nivel': 500.0, 'q_mvar': 84.0,
     'fuente': 'reporte PowerFactory (Rea NvEspVir, 83,4 Mvar en servicio)'},
]


# ---------------------------------------------------------------------------
# NO se anaden, y por que
#
# Carreto 500 kV (68 MW). Existe (UPME 06-2021, Celsia, San Juan Nepomuceno)
#   pero se conecta "en entrada-salida" sobre la linea Chinu-Sabanalarga
#   500 kV existente, y no se conoce el punto de derivacion a lo largo del
#   corredor. Anadirla obligaria a partir esa linea por un sitio inventado.
#
# Porvenir II (240 MW). El reporte la sitúa en 500 kV y las fuentes publicas
#   en la linea San Carlos-Purnio de 230 kV. La evidencia se contradice y la
#   central esta en licenciamiento (operacion prevista 2032).
#
# Pomasqui 220 kV (35 MW). Es el equivalente de Ecuador. La red ya modela la
#   interconexion con PIMAMPIRO (cuatro circuitos desde JAMONDINO); anadirlo
#   DUPLICARIA el enlace con Ecuador.
#
# Term / "Costa Atla" (127 MW). Parque eolico sin subestacion identificable.
#
# Corocora y Jaguey "Ficticia" (74 MW cada una). El propio modelo de
#   PowerFactory las declara ficticias; no son subestaciones reales.
#
# Quebradona 220 kV (58 MW). Carga de un proyecto minero, sin punto de
#   conexion documentado.
# ---------------------------------------------------------------------------


def lineas_df():
    """Devuelve las lineas complementarias como DataFrame con el mismo
    esquema que data_loader.load_lines()."""
    import pandas as pd
    from stn.data_loader import bus_id

    if not LINEAS:
        return pd.DataFrame()
    df = pd.DataFrame(LINEAS)
    df['bus0'] = [bus_id(s, n) for s, n in zip(df.sub0, df.nivel)]
    df['bus1'] = [bus_id(s, n) for s, n in zip(df.sub1, df.nivel)]
    return df


# Reporte 'Detalle de reactores' de PARATEC. Se descarga con:
#   curl -s https://paratecbackend.xm.com.co/reportetransmision/api/Reactor/getAll
# raiz de la descarga de PARATEC, definida en un solo sitio
from stn.paratec_equipos import DIR_PARATEC

ARCHIVO_REACTORES = f'{DIR_PARATEC}/Transmision/Compensacion/PARATEC_reactores.csv'


def _norm_sub(s):
    """Nombre de subestacion comparable entre PARATEC y el reporte."""
    s = str(s).upper().split('(')[0].strip()
    for a, b in [('Ñ', 'N'), ('Á', 'A'), ('É', 'E'), ('Í', 'I'),
                 ('Ó', 'O'), ('Ú', 'U')]:
        s = s.replace(a, b)
    return re.sub(r'\s+\d{2,3}$', '', s).strip()   # quita el sufijo de tension


def _subestaciones_con_shunts_fuera():
    """Subestaciones cuyos shunts estan TODOS fuera en el caso de referencia.

    PARATEC dice que un reactor existe; el reporte de PowerFactory dice si
    estaba conectado. No es lo mismo: un reactor se maniobra a diario. Cuando
    el reporte trae el equipo con Q = 0 en una subestacion, es que ese dia no
    estaba en servicio y meterlo falsea el escenario que se quiere reproducir.
    """
    import pandas as pd
    try:
        d = pd.read_csv(dato(PUNTO, 'pq_completo.csv'))
    except Exception as e:                                  # noqa: BLE001
        # Devolver un set vacio en silencio dejaba entrar 4 reactores que el
        # caso de referencia tiene FUERA de servicio, sin que nada lo dijera.
        print(f"  AVISO: no se pudo leer Datos/pq_completo.csv ({e}); no se "
              f"filtran los reactores que la referencia tiene fuera de "
              f"servicio, asi que entraran 4 de mas")
        return set()
    d['kv'] = pd.to_numeric(d['Nominal Line-Line Voltage kV'], errors='coerce')
    d['Q'] = pd.to_numeric(d['Reactive Power Mvar'], errors='coerce')
    sh = d[(d['Element type'].astype(str).str.contains('Shunt', na=False))
           & (d.kv >= 200)].copy()
    if sh.empty:
        return set()
    sh['sub_n'] = sh['Subestacion'].apply(_norm_sub)
    return {s for s, g in sh.groupby('sub_n') if (g.Q.abs() <= 0.05).all()}


def reactores_paratec():
    """Reactores del STN que declara PARATEC: los fijos y los maniobrables.

    De los 61 del STN entran 46: los 24 fijos que el caso de referencia no
    contradice (1728 MVAr) y 22 maniobrables (1515 MVAr).

    POR QUE SE INCLUYEN AHORA LOS MANIOBRABLES
    ------------------------------------------
    Antes solo entraban los fijos. El argumento era operativo y suena bien: el
    escenario esta cerca de la punta (12 383 MW) y en punta los reactores
    maniobrables se sacan, porque con la red cargada no sobra reactivo que
    absorber. Se respaldaba con una medida: meterlos empeoraba el error medio
    de tension de 0,0147 a 0,0216 pu.

    Esa medida se tomo ANTES de corregir las tomas de los autotransformadores
    y de pasar las consignas de tension a las barras (v_mag_pu_set). Las dos
    correcciones bajaron el error base a 0,0088 pu y cambiaron el punto de
    operacion por debajo de aquella conclusion, que nadie volvio a revisar.

    Repetida sobre el modelo actual, la medida se invierte: el error baja de
    0,0088 a 0,0075 pu. Es evidencia contra la respuesta del propio caso de
    PowerFactory, asi que el caso real se parece mas al modelo CON reactores.

    El efecto grande aparece al activar los limites de reactivo. Sin ellos los
    generadores absorbian Q sin limite y tapaban el deficit; con ellos, 244 de
    296 contingencias N-1 se iban por encima de 1,1 pu. Con los maniobrables
    puestos quedan en 1: San Marcos (Valle) - Yumbo 230 kV, en 1,1000 pu justo.
    Los generadores fuera de su curva de capacidad bajan de 5 a 3.

    El coste es menos margen por abajo: la tension minima del barrido pasa de
    0,9608 a 0,9174 pu. Sigue sin violaciones y para un caso de punta es hasta
    mas realista, pero el colchon es de 0,017 pu, no de 0,061.

    DOS SALVEDADES
    --------------
    1. Los maniobrables se filtran ademas por subestacion contra REACTORES.
       Sogamoso, Nueva Esperanza y La Virginia ya entran por la lista fija del
       reporte de PowerFactory, y PARATEC los vuelve a listar como
       maniobrables: son 522 MVAr que se contarian dos veces. Quitarlos MEJORA
       el ajuste (sesgo -0,0055 -> -0,0033 pu), que es lo que cabe esperar si
       de verdad sobraban. A los fijos no se les aplica este filtro porque ya
       se comprobo que el unico que coincide de subestacion (La Virginia hacia
       San Carlos) es equipo distinto, de otra linea.

    2. El sesgo sigue en -0,0033 pu, o sea que el modelo queda un pelo bajo.
       Antes quedaba un pelo alto (+0,0052). Que cruce el cero sugiere que lo
       exacto seria un subconjunto de los 22, no los 22; queda como afinado
       pendiente.

    Los reactores de LINEA se modelan aqui como shunt fijo en la barra, que es
    correcto para el caso base pero no para el N-1: al abrir la linea, su
    reactor sale con ella. Mientras no se modele esa dependencia, una
    contingencia sobre una linea de 500 kV con reactor deja el reactor puesto
    y subestima la subida de tension.
    """
    import pandas as pd
    df = pd.read_csv(ARCHIVO_REACTORES)
    df['mvar'] = pd.to_numeric(df.nominalCapacity, errors='coerce')
    df['kv'] = pd.to_numeric(df.nomU, errors='coerce')
    df = df[(df.transmissionSystemType == 'Uso STN') & (df.kv >= 200)
            & df.mvar.notna()].copy()
    df['sub_n'] = df.subStation.apply(_norm_sub)
    df = df[~df.sub_n.isin(_subestaciones_con_shunts_fuera())]

    # el filtro anti-duplicado solo va sobre los maniobrables (salvedad 1)
    ya_puestos = {_norm_sub(r['sub']) for r in REACTORES}
    return df[(df.isManeuverable == 0) | ~df.sub_n.isin(ya_puestos)]


def reactores():
    """Reactores del modelo con su susceptancia en Siemens.

    Son dos conjuntos que se complementan y no se solapan:

      1. REACTORES, los seis que el reporte de PowerFactory muestra EN
         SERVICIO. Son imprescindibles porque PARATEC no tiene ninguno en
         NORTE, y los de Sogamoso, Nueva Esperanza y La Virginia que si estan
         conectados figuran alli como maniobrables, asi que el filtro de
         reactores_paratec() los dejaria fuera.
      2. reactores_paratec(), los fijos del STN que el reporte no contradice.

    Se comprobo que no hay duplicados: en las subestaciones del grupo 1,
    PARATEC solo aporta un reactor fijo mas (La Virginia a San Carlos), que es
    de otra linea distinta.
    """
    from stn.data_loader import bus_id
    out = []
    for r in REACTORES:
        v = r['nivel'] * 1e3               # V
        b = -r['q_mvar'] * 1e6 / (v ** 2)  # S, negativa porque absorbe
        out.append({'name': r['name'],
                    'bus': bus_id(r['sub'], r['nivel']),
                    'b': b,
                    'fuente': r['fuente']})
    try:
        for _, r in reactores_paratec().iterrows():
            v = float(r['kv']) * 1e3
            out.append({'name': str(r['elementName'])[:60],
                        'bus': bus_id(_norm_sub(r['subStation']), float(r['kv'])),
                        'b': -float(r['mvar']) * 1e6 / (v ** 2),
                        'fuente': 'PARATEC Detalle de reactores (fijo)'})
    except Exception as e:                              # noqa: BLE001
        # Sin este fichero el modelo se queda con 6 reactores y 488 MVAr en
        # vez de 30 y 2.216: el 78% de la compensacion del STN desaparece y
        # con ella el perfil de tension deja de ser creible. Antes era un
        # aviso de una linea que se perdia entre el resto de la salida, asi
        # que se para: es preferible no arrancar a arrancar mal en silencio.
        raise RuntimeError(
            f"No se pudieron cargar los reactores de PARATEC desde "
            f"'{ARCHIVO_REACTORES}' ({type(e).__name__}: {e}).\n"
            f"    Sin ellos el modelo pierde el 78% de la compensacion "
            f"reactiva del STN.\n"
            f"    Se descarga con:\n"
            f"      curl -s https://paratecbackend.xm.com.co/"
            f"reportetransmision/api/Reactor/getAll -o reactores.json\n"
            f"    y se convierte a CSV con las columnas del reporte."
        ) from e
    return out
