# desagregacion.py
"""Separa la inyeccion neta de cada barra en generacion y demanda brutas.

EL PROBLEMA
-----------
El modelo reporta 9.090 MW de generacion y 9.096 MW de demanda, cuando el caso
de referencia tiene 12.275 y 12.383. El hueco, ~3.200 MW, no es un error: es
una consecuencia inevitable del metodo del equivalente por barra.

La inyeccion equivalente de una barra del STN es

    P_iny = gen_directa - (trafos + cargas + shunts)

y un transformador solo transporta el NETO de lo que cuelga debajo. Si bajo una
subestacion hay 500 MW de carga en 115 kV y 200 MW de generacion embebida, el
transformador lleva 300 MW hacia abajo: los 200 MW de generacion y 200 de los
500 de carga se cancelan y desaparecen del modelo. El flujo del STN sigue
siendo correcto ---por la barra circula exactamente lo que debe--- pero los
totales que el modelo puede informar son netos, no brutos.

Medido sobre el reporte, el hueco se descompone exactamente asi:

    (a) neteo en los trafos del STN          1.454,0 MW de gen
    (b) generacion embebida bajo 200 kV
        que nunca sube al STN                1.730,9 MW de gen
        ------------------------------------------------------
        suma                                 3.184,9 MW  <- el hueco exacto

Con este modulo el modelo informa 11.844,1 MW de generacion y 11.715,9 de
demanda, frente a los 9.225,0 / 9.096,1 de antes.

Durante un tiempo aqui se leyo que el flujo no cambiaba "ni un MW", con la
correlacion contra PowerFactory clavada en 0,8093 y 22,60 MW de error medio.
Era un espejismo: validacion.py no aplicaba el flag, asi que medía siempre la
red SIN desglosar. Aplicandolo, el contraste da 0,8107 y 22,48 MW. Lo que se
conserva exacto es la INYECCION NETA por barra; el flujo AC se mueve un poco
---0,29 MW de media por linea, 5,92 la peor, 0,62 MW de perdidas--- porque
atribuir generacion a una barra la pasa de PQ a PV y eso cambia el reactivo y
la tension. Ver el docstring de validacion.resolver_modelo().

LA IDEA
-------
Hay UN grado de libertad por barra. La inyeccion neta P_iny(b) no se puede
tocar, porque es lo que determina el flujo: cambiarla invalidaria la
validacion contra PowerFactory y el analisis de contingencias. Pero se puede
elegir como repartirla:

    Gen(b) - Carga(b) = P_iny(b)        <- invariante, el flujo no cambia

Basta con decidir Gen(b); la carga sale forzada:

    Carga(b) = Gen(b) - P_iny(b)

Asi que el problema se reduce a UNO solo: atribuir a cada barra del STN la
generacion que cuelga de ella. La carga no hay que estimarla, sale sola, y de
paso absorbe las perdidas de la subtransmision, que es donde fisicamente
estan.

DE DONDE SALE LA ATRIBUCION
---------------------------
Del propio reporte, no de supuestos. Cada transformador aparece una vez por
terminal y las filas comparten el campo `Name`, asi que se puede reconstruir
que barra cuelga de cual. Con ese grafo, cada nodo de generacion se asocia a
la barra del STN que alcanza.

    ya en barra STN        1 nodo      35,4 MW
    a 1 salto             90 nodos  10.124,9 MW
    a 2 saltos            11 nodos     344,7 MW
    a 3-4 saltos           6 nodos      87,0 MW
    no alcanzable         66 nodos   1.682,7 MW

Los no alcanzables son consecuencia del filtro del 30% de carga minima del
reporte: se llevo por delante los transformadores que los conectaban. Se
recuperan por dos vias mas, en este orden:

  2. POR NOMBRE. El nodo nombra su central ---las ocho unidades de Guatape---
     y la subestacion existe en el STN. 10 nodos, 661,0 MW.

  3. POR LA TOPOLOGIA DEL STR. PARATEC publica las 1.063 lineas del pais, de
     44 a 500 kV, con el par de subestaciones de cada una. Con ese grafo se
     traza por donde sale al STN una central que inyecta en el STR: Gecelca
     32, Yopal, Dario Valencia Samper. 16 nodos, 434,1 MW.

Ninguna de las dos inventa nada: son la misma central leida de otra fuente que
si la tiene. Lo unico que se asume en la tercera es que la central entrega a la
subestacion del STN mas cercana por la red, con un tope de 4 saltos; mas alla
ya no se puede decir que 'entregue' ahi y se deja sin atribuir.

Reparto final: 12.274,7 MW en el reporte, 11.687,1 atribuidos, 587,6 sin
atribuir (34 nodos, mediana 10 MW). Los que quedan fuera no aparecen ni en la
topologia de PARATEC o estan a mas de 4 saltos.

LO QUE ESTO NO ARREGLA
----------------------
El filtro del 30% del reporte oculta 479 MW de generacion que no estan en
ninguna fila, asi que el techo alcanzable con esta fuente es 12.274,7 MW y no
los 12.753,9 del `Total System Summary`. Esa diferencia no se puede recuperar
sin un export sin filtrar.
"""

from __future__ import annotations

import re

import networkx as nx
import numpy as np
import pandas as pd

from stn import generar_mapeo_stn as G
from stn.paths import RED, dato

SALTOS_MAX = 6          # profundidad de busqueda en el grafo de conectividad


# ---------------------------------------------------------------------------
# Grafo de conectividad del reporte
# ---------------------------------------------------------------------------

def _nodo_id(df):
    """Clave (nodo, nivel) igual a la que usa el mapeo revisado a mano.

    Tiene que ser la columna `nodo`, no `Subestacion`: el mapeo se indexa por
    Nodo_PDF/kV_PDF, que corresponden a `nodo`/`nivel`. Usar `Subestacion`
    ---que es el patio, 'Primavera 500' frente a 'PRIMAVERA'--- hace que la
    atribucion se calcule bien y luego no encuentre ningun bus al que llevarla.
    """
    return df.nodo.astype(str) + '||' + df.nivel.astype(str)


def _grafo_reporte(df):
    """Une los nodos que comparten un transformador o una linea.

    Cada elemento aparece una vez por terminal y las filas comparten `Name`,
    asi que agrupando por nombre se recupera que nodos conecta.
    """
    g = nx.Graph()
    conectores = df[df.cat.isin(['trafo', 'trafo_interno', 'linea'])]
    for _, sub in conectores.groupby('Name'):
        ns = sub.nodo_id.unique()
        for i in range(len(ns)):
            for j in range(i + 1, len(ns)):
                g.add_edge(ns[i], ns[j])
    return g


def _base_nombre(nodo):
    """Nombre de central a partir del nodo: 'Guatape 6 13.8' -> 'GUATAPE'.

    Quita el nivel de tension, el numero de unidad y los sufijos de patio.
    Sirve para el ultimo recurso, cuando el grafo no llega a la barra porque
    el filtro del reporte borro el transformador que la unia.
    """
    s = str(nodo).split('||')[0].upper()
    for a, b in [('Ñ', 'N'), ('Á', 'A'), ('É', 'E'), ('Í', 'I'),
                 ('Ó', 'O'), ('Ú', 'U')]:
        s = s.replace(a, b)
    s = re.sub(r'\bLV\b|\bMV\b|\bKV\b|\bGEN\b|\bU\d+\b|\bG\d+\b', ' ', s)
    s = re.sub(r'\d+[.,]?\d*', ' ', s)          # niveles y numeros de unidad
    return re.sub(r'\s+', ' ', s).strip()


# ---------------------------------------------------------------------------
# Atribucion
# ---------------------------------------------------------------------------

# Atribuciones que los tres recursos automaticos resuelven MAL y se corrigen a
# mano. Se declaran aqui, con su motivo, en vez de tocar los algoritmos: son
# excepciones verificadas una a una, no una regla nueva.
#
# El formato es  nodo_de_generacion -> nodo_STN_de_destino,  con las claves
# 'NOMBRE||kV' que usa el modulo.
CORRECCION_ATRIBUCION = {
    # El camino mas corto en el grafo del reporte llevaba la unidad 2 de El
    # Quimbo a JAMONDINO, a un salto por la linea Tesalia-Jamondino, cuando
    # la unidad 1 iba correctamente a QUIMBO U1||230.0. Estan en Huila y
    # Nario respectivamente: unos 400 km.
    #
    # Ademas era un DOBLE CONTEO. Como El Quimbo no tiene carga, el ajuste
    # por atribucion corta le subia la generacion hasta igualar su inyeccion
    # neta de 300 MW de todos modos, asi que los 150 MW de la unidad 2 se
    # contaban dos veces: una en Jamondino y otra via ese ajuste. Corregirlo
    # baja la generacion bruta informada de 11.709,6 a 11.559,6 MW.
    #
    # La clave de destino ya existia en el mapeo: QUIMBO U2||230.0 ->
    # EL QUIMBO__230. El algoritmo no la uso porque busca por topologia, no
    # por nombre.
    'QUIMBO U2||13.8': 'QUIMBO U2||230.0',

    # GECELCA 32 acababa en ARMENIA por el tercer recurso (topologia del
    # STR). Gecelca esta en Puerto Libertador, Cordoba, y Armenia en Quindio:
    # unos 700 km. Con 200 MW era, ademas, la mayor atribucion de esa via.
    #
    # Se notaba por otro lado: el desglose ponia 202 MW de generacion
    # embebida bajo Armenia cuando PARATEC registra 100,8 MW de plantas
    # menores en TODA la subarea CQR (Caldas-Quindio-Risaralda). No cabian.
    # Corregido, Armenia queda en 2,0 MW y si cabe.
    #
    # CUIDADO, ESTE DESTINO ES UN JUICIO, NO UN DATO. PARATEC declara que
    # GECELCA 32 conecta en "GECELCA 110", que no es una barra del STN, asi
    # que su potencia tiene que reflejarse en algun nudo de 220/500 y hay que
    # elegir cual. Se toma CERROMATOSO porque es el nudo de Cordoba y porque
    # PARATEC ya declara a GECELCA 3 conectada en "CERROMATOSO 110". Medido,
    # es ademas la opcion que mejor se contrasta con PowerFactory (MAE 22,47
    # frente a 22,48 dejandolo en Armenia y 22,50 llevandolo a Monteria),
    # aunque la diferencia esta muy por debajo de lo que esa muestra de 39
    # lineas puede resolver.
    'GECELCA 32||19.0': 'CERROMATOSO||500.0',
}


def generacion_por_nodo_stn(df=None, verbose=True):
    """MW de generacion que cuelgan de cada nodo (subestacion, nivel) del STN.

    Devuelve (serie_por_nodo_stn, detalle) donde `detalle` lleva la traza de
    como se resolvio cada nodo de generacion y cual quedo sin atribuir.
    """
    if df is None:
        df = G.cargar_reporte()
    df = df.copy()
    df['P'] = pd.to_numeric(df['Active Power MW'], errors='coerce').fillna(0.0)
    df['nodo_id'] = _nodo_id(df)

    grafo = _grafo_reporte(df)
    nodos_stn = set(df.loc[df.nivel >= G.STN_KV_MIN, 'nodo_id'])
    # para el recurso por nombre: base -> nodos STN con ese nombre
    por_nombre = {}
    for n in nodos_stn:
        por_nombre.setdefault(_base_nombre(n), []).append(n)

    gen = df[df.cat == 'gen'].groupby('nodo_id').P.sum()
    filas = []
    for nodo, mw in gen.items():
        destino, via, saltos = None, 'sin_atribuir', -1

        if nodo in nodos_stn:
            destino, via, saltos = nodo, 'directo', 0
        elif nodo in grafo:
            d = nx.single_source_shortest_path_length(
                grafo, nodo, cutoff=SALTOS_MAX)
            cand = sorted((v, k) for k, v in d.items() if k in nodos_stn)
            if cand:
                saltos, destino = cand[0]
                via = 'grafo'

        if destino is None:
            # ultimo recurso: el nombre del nodo nombra su central
            cand = por_nombre.get(_base_nombre(nodo), [])
            if len(cand) == 1:
                destino, via = cand[0], 'nombre'
            elif len(cand) > 1:
                # varios niveles de la misma subestacion: el mas bajo, que es
                # donde entra una central por su transformador elevador
                destino = min(cand, key=lambda n: float(n.split('||')[1]))
                via = 'nombre'

        filas.append({'nodo': nodo, 'MW': float(mw), 'stn': destino,
                      'via': via, 'saltos': saltos})

    det = pd.DataFrame(filas)

    # Tercer recurso: los que ni el grafo del reporte ni el nombre resuelven
    # se buscan en la topologia completa de PARATEC, que si tiene el STR.
    huerfanos = det.loc[det.stn.isna(), 'nodo'].tolist()
    if huerfanos:
        try:
            gstr = _grafo_str()
            resuelto = _atribuir_por_str(huerfanos, nodos_stn, gstr)
        except Exception as e:                               # noqa: BLE001
            print(f"  AVISO: sin topologia del STR ({e})")
            resuelto = {}
        if resuelto:
            m = det.nodo.map(resuelto)
            nuevo_stn = det.stn.isna() & m.notna()
            det.loc[nuevo_stn, 'stn'] = m[nuevo_stn]
            det.loc[nuevo_stn, 'via'] = 'str'

    # Correcciones manuales, las ultimas: prevalecen sobre los tres recursos
    # automaticos. Ver CORRECCION_ATRIBUCION.
    n_corr = 0
    for nodo, destino in CORRECCION_ATRIBUCION.items():
        sel = det.nodo == nodo
        if sel.any():
            det.loc[sel, 'stn'] = destino
            det.loc[sel, 'via'] = 'corregido'
            n_corr += int(sel.sum())

    serie = det[det.stn.notna()].groupby('stn').MW.sum()

    if verbose:
        print("Atribucion de generacion a barras del STN")
        if n_corr:
            print(f"  ({n_corr} atribuciones corregidas a mano; ver "
                  f"CORRECCION_ATRIBUCION)")
        for via, sub in det.groupby('via'):
            print(f"  {via:13s}: {len(sub):3d} nodos, {sub.MW.sum():8.1f} MW")
        falta = det[det.stn.isna()]
        print(f"  {'TOTAL':13s}: {len(det):3d} nodos, {det.MW.sum():8.1f} MW")
        if len(falta):
            print(f"  SIN ATRIBUIR: {len(falta)} nodos, {falta.MW.sum():.1f} MW "
                  f"-> no entran al modelo, quedan documentados aqui:")
            for _, r in falta.nlargest(5, 'MW').iterrows():
                print(f"       {r.nodo:34s} {r.MW:7.1f} MW")
    return serie, det


def bruta_por_bus(inyecciones, mapeo, df=None, verbose=True):
    """Generacion y carga brutas por bus del modelo.

    `inyecciones` es el DataFrame de data_integration.integrar() (indexado por
    bus, columna P con la inyeccion NETA). `mapeo` es Datos/mapeo_STN.csv.

    Invariante que se respeta en todos los casos:

        Gen(b) - Carga(b) == P_iny(b)

    Es la inyeccion NETA lo que se conserva, no la solucion del flujo AC: la
    barra que recibe generador pasa de PQ a PV y eso mueve tension y reactivo
    (ver la cabecera del modulo).
    """
    serie_stn, det = generacion_por_nodo_stn(df, verbose=verbose)

    # nodo del reporte -> bus del modelo, con el mapeo ya revisado a mano
    m = mapeo[mapeo.Bus_red != ''].copy()
    clave = (m.Nodo_PDF.astype(str) + '||'
             + m.kV_PDF.astype(float).astype(str))
    a_bus = dict(zip(clave, m.Bus_red))

    gen_bus = {}
    sin_bus = []
    for nodo, mw in serie_stn.items():
        bus = a_bus.get(nodo)
        if bus is None:
            sin_bus.append((nodo, mw))
            continue
        gen_bus[bus] = gen_bus.get(bus, 0.0) + float(mw)

    gen = pd.Series(gen_bus, dtype=float).reindex(inyecciones.index).fillna(0.0)
    p_iny = inyecciones.P.astype(float)

    # La carga sale forzada por el invariante. Si a una barra se le atribuyo
    # menos generacion de la que exporta, la carga saldria negativa: eso es
    # imposible, y significa que la atribucion se quedo corta ahi. Se sube la
    # generacion hasta la inyeccion y la carga queda en cero, dejando
    # constancia de cuanto hubo que subir.
    ajuste = (p_iny - gen).clip(lower=0.0)
    gen_aj = gen + ajuste
    carga = gen_aj - p_iny

    info = {
        'gen_total': float(gen_aj.sum()),
        'carga_total': float(carga.sum()),
        'neto': float((gen_aj - carga).sum()),
        'ajuste_mw': float(ajuste.sum()),
        'buses_ajustados': int((ajuste > 1e-6).sum()),
        'sin_atribuir_mw': float(det[det.stn.isna()].MW.sum()),
        'sin_bus_mw': float(sum(mw for _, mw in sin_bus)),
        'detalle': det,
    }

    if verbose:
        print(f"\nDesagregacion por bus")
        print(f"  generacion bruta      : {info['gen_total']:9.1f} MW")
        print(f"  demanda bruta         : {info['carga_total']:9.1f} MW")
        print(f"  neto (== inyeccion)   : {info['neto']:9.1f} MW")
        if info['buses_ajustados']:
            print(f"  ajuste por atribucion corta: {info['ajuste_mw']:.1f} MW "
                  f"en {info['buses_ajustados']} buses")
        if info['sin_bus_mw'] > 1:
            print(f"  atribuido a un nodo STN que el mapeo no lleva a ningun "
                  f"bus: {info['sin_bus_mw']:.1f} MW")
    return gen_aj, carga, info


def desglose_de(inyecciones, verbose=True):
    """El par (gen_bruta, carga_bruta) si DESGLOSE_BRUTO esta activo, o None.

    Punto UNICO de lectura del flag. Antes lo leia solo main.py, asi que el
    pipeline principal veia 44 generadores y 11.716 MW mientras todo lo que
    cuelga de `contingencias.caso_base()` ---contingencias, agc_deslastre,
    cascada y deslastre_btm--- seguia viendo 29 y 9.096: dos sistemas
    distintos con el mismo fichero de configuracion. Lo que devuelve esta
    funcion va tal cual al parametro `desglose` de `asignar_inyecciones`.

    El invariante se comprueba aqui, antes de tocar la red: si fallara, el
    flujo AC cambiaria y el modelo dejaria de ser comparable con PowerFactory.
    """
    from stn.config import DESGLOSE_BRUTO
    if not DESGLOSE_BRUTO:
        return None
    from stn.data_integration import cargar_mapeo
    gen, carga, _ = bruta_por_bus(inyecciones, cargar_mapeo(), verbose=verbose)
    verificar(gen, carga, inyecciones)
    return gen.copy(), carga.copy()


def verificar(gen, carga, inyecciones, tol=1e-6):
    """Comprueba el invariante bus a bus. Devuelve el peor desvio."""
    d = (gen - carga) - inyecciones.P.astype(float)
    peor = float(d.abs().max())
    if peor > tol:
        malos = d[d.abs() > tol]
        raise AssertionError(
            f"El desglose no conserva la inyeccion neta en {len(malos)} buses "
            f"(peor desvio {peor:.6f} MW). El flujo AC cambiaria.")
    return peor


# ---------------------------------------------------------------------------
# Tercer recurso: la topologia del STR
# ---------------------------------------------------------------------------

ARCHIVO_LINEAS_COMPLETAS = dato(RED, 'lineas_completas.csv')


def _grafo_str(archivo=ARCHIVO_LINEAS_COMPLETAS):
    """Grafo de subestaciones a partir del CSV completo de PARATEC.

    El reporte de PowerFactory pierde la pista de 50 nodos de generacion
    porque el filtro del 30% borro los transformadores que los conectaban.
    Pero PARATEC publica TODAS las lineas del pais ---1.063, de 44 a 500 kV---
    con el par de subestaciones que une cada una. Con eso se reconstruye por
    donde sale al STN una central que inyecta en el STR.

    No es inventar un dato: es la misma central, leida de otra fuente que si
    la tiene. Lo que se asume es unicamente que la central entrega su potencia
    a la subestacion del STN mas cercana por la red, que es lo que hace.
    """
    import re as _re
    d = pd.read_csv(archivo)
    g = nx.Graph()
    col = 'Subestaciones relacionadas'
    kv = pd.to_numeric(d.get('Voltaje nominal [kV]'), errors='coerce')
    for s, v in zip(d[col].fillna(''), kv.fillna(0)):
        partes = [x.strip() for x in _re.split(r'\s*[-\u2013]\s*', str(s))
                  if x.strip()]
        if len(partes) < 2:
            continue
        ns = [_base_nombre(x + '||0') for x in partes]
        for i in range(len(ns) - 1):
            if ns[i] and ns[i+1] and ns[i] != ns[i+1]:
                g.add_edge(ns[i], ns[i+1], kv=float(v))
    return g


def _atribuir_por_str(falta, nodos_stn, grafo_str, saltos_max=4):
    """Para cada huerfano, la barra del STN mas cercana por la red del STR.

    Devuelve {nodo: nodo_stn}. Solo resuelve los que llegan en pocos saltos:
    una central a cinco subestaciones de distancia ya no se puede decir que
    'entregue' a esa barra, y atribuirla seria forzar el dato.
    """
    # base de subestacion -> nodos STN de esa subestacion
    por_base = {}
    for n in nodos_stn:
        por_base.setdefault(_base_nombre(n), []).append(n)

    out = {}
    for nodo in falta:
        b = _base_nombre(nodo)
        if not b or b not in grafo_str:
            continue
        try:
            d = nx.single_source_shortest_path_length(
                grafo_str, b, cutoff=saltos_max)
        except Exception:                                    # noqa: BLE001
            continue
        cand = sorted((v, k) for k, v in d.items() if k in por_base)
        if not cand:
            continue
        _, base_stn = cand[0]
        # dentro de la subestacion, el nivel mas bajo: por ahi entra el STR
        out[nodo] = min(por_base[base_stn],
                        key=lambda n: float(n.split('||')[1]))
    return out


# ---------------------------------------------------------------------------
# Composicion por tecnologia
# ---------------------------------------------------------------------------

def por_tecnologia(det=None, verbose=True):
    """Reparte la generacion atribuida entre hidraulica, termica, solar y eolica.

    El reporte solo distingue 'Synchronous Machine' de 'Static Generator', que
    no basta: una sincrona puede ser hidraulica o termica. La tecnologia sale
    de `paratec_equipos.capacidad_plantas()`, emparejando por nombre de planta.
    Lo que no empareja queda como 'sin_clasificar' y se informa: es preferible
    a repartirlo con un supuesto.
    """
    from stn.paratec_equipos import capacidad_plantas

    if det is None:
        _, det = generacion_por_nodo_stn(verbose=False)
    det = det[det.stn.notna()].copy()
    det['base'] = det.nodo.apply(_base_nombre)

    cap = capacidad_plantas().copy()
    cap['base'] = cap.planta.astype(str).apply(
        lambda s: _base_nombre(str(s) + '||0'))
    tipo = (cap.groupby('base').tipo.first())

    # Emparejar por nombre completo normalizado deja fuera dos tercios: el
    # reporte escribe 'SAN CARLOS 1' donde PARATEC pone 'SAN CARLOS'. Se
    # intenta primero la coincidencia exacta y luego por prefijo de tokens,
    # que es lo que distingue 'GUATAPE' de 'GUATRON' sin confundirlos.
    def clasificar(base):
        if base in tipo.index:
            return tipo[base]
        toks = base.split()
        for k in range(len(toks), 0, -1):
            pref = ' '.join(toks[:k])
            if pref in tipo.index:
                return tipo[pref]
        cand = [t for t in tipo.index if t and base.startswith(t + ' ')]
        if len(cand) == 1:
            return tipo[cand[0]]
        return 'sin_clasificar'

    det['tipo'] = det.base.apply(clasificar)
    mix = det.groupby('tipo').MW.sum().sort_values(ascending=False)

    if verbose:
        tot = mix.sum()
        print("\nComposicion por tecnologia de la generacion atribuida")
        for t, v in mix.items():
            print(f"  {t:16s} {v:9.1f} MW   {100*v/max(tot,1e-9):5.1f}%")
        print(f"  {'TOTAL':16s} {tot:9.1f} MW")
    return mix
