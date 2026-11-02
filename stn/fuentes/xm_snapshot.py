# fuentes/xm_snapshot.py
# Punto de operacion de cualquier fecha y periodo, desde un snapshot de XM.
#
# -------------------------------------------------------------------------
# DE DONDE SALE EL DATO
# -------------------------------------------------------------------------
# De los parquet que deja `sinsim scrape --fecha YYYY-MM-DD`:
#
#   normalized/dispatch.parquet    despacho programado (dDEC), 24 periodos
#   normalized/demand.parquet      pronostico por barra (PRON_BARRA), 24 per.
#   normalized/generators.parquet  unidades de PARATEC, con su punto de conexion
#   normalized/buses.parquet       barras, con nivel y subestacion
#   normalized/lines.parquet       lineas, para subir de STR a STN
#
# y de las equivalencias curadas de sinsim (`data/equivalencias/`), que
# traducen los nombres de los recursos del despacho y de los nodos del
# pronostico a plantas y subestaciones de PARATEC.
#
# NO se importa sinsim. No se puede: exige Python >=3.12 y el modelo corre en
# 3.11. Se leen sus ficheros, que es una frontera mas estable que su API: son
# parquet con checksum y un manifiesto.
#
# -------------------------------------------------------------------------
# LO QUE ESTE MODULO NO TRAE, Y ES DELIBERADO
# -------------------------------------------------------------------------
# GENERACION DETRAS DEL CONTADOR. Ni una. Se verifico el 31-10-2026: las 536
# plantas que PARATEC marca "No Despachada Centralmente" (3.495 MW) aparecen
# TODAS en el dDEC. PARATEC y XM ven todo lo REGISTRADO; el BTM que estudia la
# tesis ---lo no registrado, embebido en PRON_BARRA--- es invisible aqui por
# construccion. `deslastre_btm.EscenarioBTM` sigue siendo exogeno, alimentado
# por PARATEC_AGPE y PARATEC_Pmenores. Esta fuente no lo toca ni lo redefine.
#
# CONSIGNA DE TENSION. PRON_BARRA y dDEC dan P y Q, no tension. Se fija una
# CONSIGNA UNIFORME POR NIVEL (ver V_SET_POR_NIVEL), no se hereda de ningun
# caso. Las tres alternativas se midieron sobre el 2026-10-20 p19 antes de
# elegir, y la que parecia obvia resulto ser la peor:
#
#   heredar el perfil del caso de PowerFactory  ->  v_max 1,162 pu, 4 barras
#       fuera de banda. Ese perfil no es una consigna: es la SOLUCION de otro
#       despacho, y reimponerla sobre el de XM fuerza reactivo que no toca.
#       Ademas solo cubre 52 de las 61 barras PV y ataria toda fecha de XM al
#       caso de planeamiento de 2042.
#   plano 1,0 (lo que hace sinsim)  ->  v_min 0,867, y los 16 autotrafos en
#       toma 1,0, que es la condicion que `power_flow._ajustar_taps` ya midio
#       como patologica: 864 Mvar circulando por el banco de San Carlos.
#   consigna por nivel  ->  v [0,929; 1,132], y los 16 taps con una relacion
#       uniforme y justificable.
#
# Lo que NINGUNA consigna arregla: la dispersion de tension del punto es de
# 0,217 pu ---mas ancha que la banda de 0,2--- la fije donde la fije. Son dos
# colas radiales, y las dos son propiedades del dia, no del modelo:
#   Arauca      CANO LIMON 0,914 pu: 97 MW de demanda contra 14,8 MW de
#               generacion local (Termosaravena 5 + Alma II 9,8, en 34,5 kV).
#   Ecuador     PIMAMPIRO 1,120 pu: los dos enlaces (ECUADOR138_1 y
#               ECUADOR230_1) estan a 0 MW las 24 horas de esa fecha, asi que
#               Jamondino-Pimampiro es un radial VACIO y sube por Ferranti.
#
# -------------------------------------------------------------------------
# COMO SE SUBE DE STR A STN
# -------------------------------------------------------------------------
# El modelo es un equivalente de 220/500 kV con 148 buses. El pronostico de
# demanda esta casi todo por debajo: en el periodo 19 del 2026-10-20, de
# 12.150 MW solo 603 estan en barras de 220/230 kV. Hay que subirlo.
#
# La regla es la misma que usa `desagregacion._atribuir_por_str()`, con el
# objetivo corregido: se busca la subestacion mas cercana que tenga un bus EN
# EL MODELO ---no en PARATEC---, recorriendo el grafo de subestaciones unidas
# por lineas. Cero saltos significa que la subestacion ya es del modelo.
#
# El grafo usa TODAS las lineas, no solo las de menos de 220 kV. Con solo STR,
# las subestaciones que PARATEC modela a 230 kV y el equivalente no
# (TEBSA II, ALFEREZ II, NUEVA MONTERIA...) quedan aisladas y se llevan su
# carga y su generacion con ellas.
#
# Lo que sigue sin alcanzar un bus del modelo NO se reparte: se reporta. Ver
# `fuentes/calidad.py` para por que eso importa tanto.

import collections
import hashlib
import os
from stn.paths import RED, XM, dato

import pandas as pd

from stn.fuentes import nombres as N
from stn.fuentes.calidad import ReporteCalidad


# Raiz de los datos de sinsim. Se puede apuntar a otra con la variable de
# entorno SINSIM_DATA; por omision, el repo hermano.
DIR_XM = str(XM)

TABLAS = ('buses', 'generators', 'dispatch', 'demand', 'lines')

# Tope de saltos del BFS. Medido sobre el 2026-10-20: el 99,0 % de la demanda
# se ubica en 5 saltos o menos y la cola es plana a partir de 3, asi que 8 es
# holgura, no un parametro que ajustar.
SALTOS_MAX = 8

# Consigna de tension por nivel. Ver la cabecera para por que es una consigna
# fija y no un perfil heredado.
#
# El 230 por encima del 500 es lo normal en el STN y es lo que hace el caso de
# referencia: su mediana es 1,0500 en 230 y 1,0065 en 500. Con estos dos
# valores los 16 autotrafos quedan todos en la misma relacion de toma (1,04),
# frente a las 16 relaciones distintas que salian de un perfil heredado.
#
# Subir v230 desplaza el perfil entero hacia arriba sin estrecharlo, asi que
# el valor sale de un barrido, no de un criterio. Con el enlace de Ecuador
# abierto (ver `abrir_enlaces`), sobre las 24 horas del 2026-10-20:
#
#   v230    tension global      horas con barras fuera de [0,9; 1,1]
#   1,02   [0,8989; 1,0798]     1 por abajo
#   1,03   [0,9144; 1,0935]     ninguna          <-- elegido
#   1,04   [0,9290; 1,1071]     5 por arriba
#
# Antes de abrir el enlace el optimo era 1,04, porque habia que sesgar el
# perfil hacia arriba para sostener la cola cargada de Arauca mientras la de
# Ecuador ya estaba fuera de banda hiciera lo que hiciera. Quitada esa, 1,03
# deja las 24 horas dentro de la banda por los dos lados.
V_SET_POR_NIVEL = {500: 1.00, 230: 1.03}
V_SET_DEFECTO = 1.00

# Enlaces internacionales del modelo, curados a mano. Ver `abrir_enlaces()`.
#
# Solo esta el de 230 kV: el otro recurso de enlace del dDEC, ECUADOR138_1,
# conecta en PANAMERICANA|138, que esta por debajo del corte de 200 kV del
# equivalente y por tanto no existe en la red.
FILE_ENLACES = dato(RED, 'enlaces_internacionales.csv')

# Recursos cuyo punto de conexion en PARATEC no lleva a ninguna barra del
# modelo, y a los que se les asigna subestacion a mano CON EVIDENCIA.
#
# No es una via para tapar huecos: es para los casos en que PARATEC declara
# una subestacion que su propia tabla de lineas deja aislada. Si no se
# corrigen, esos MW no desaparecen ---los pone el slack---, que es peor que
# perderlos, porque entonces no salen en el reporte de calidad.
FILE_CORRECCIONES = dato(RED, 'recursos_correccion_xm.csv')


def consigna(bus):
    """Consigna de tension de una barra del modelo, por su nivel."""
    _, nivel, _ = N.subestacion_de_bus(bus, {})
    if nivel is None:
        return V_SET_DEFECTO
    return V_SET_POR_NIVEL.get(N.nivel_modelo(nivel), V_SET_DEFECTO)


# ---------------------------------------------------------------------------
# Lectura del snapshot
# ---------------------------------------------------------------------------

def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def fechas_disponibles(raiz=None):
    """Fechas con snapshot, ordenadas."""
    d = os.path.join(raiz or DIR_XM, 'paratec')
    if not os.path.isdir(d):
        return []
    return sorted(x for x in os.listdir(d)
                  if os.path.isdir(os.path.join(d, x, 'normalized')))


def cargar_snapshot(fecha, raiz=None):
    """Las cinco tablas que hacen falta, mas el hash de las equivalencias."""
    raiz = raiz or DIR_XM
    base = os.path.join(raiz, 'paratec', str(fecha), 'normalized')
    if not os.path.isdir(base):
        disp = fechas_disponibles(raiz)
        raise FileNotFoundError(
            f'no hay snapshot de {fecha} en {raiz}. '
            f'Disponibles: {disp if disp else "ninguna"}. '
            f'Descargalo con:  sinsim scrape --fecha {fecha}')
    t = {}
    for nombre in TABLAS:
        p = os.path.join(base, f'{nombre}.parquet')
        if not os.path.exists(p):
            raise FileNotFoundError(f'falta {p} en el snapshot de {fecha}')
        t[nombre] = pd.read_parquet(p)

    eq_dir = os.path.join(raiz, 'equivalencias')
    t['_equivalencias'] = {
        'recursos': pd.read_csv(os.path.join(eq_dir, 'recursos_despacho.csv')),
        'nodos': pd.read_csv(os.path.join(eq_dir, 'nodos_demanda.csv')),
    }
    t['_procedencia'] = {
        'raiz': os.path.abspath(raiz),
        'fecha': str(fecha),
        'sha256_equivalencias': _sha256(
            os.path.join(eq_dir, 'recursos_despacho.csv'))[:16],
        'sha256_nodos': _sha256(
            os.path.join(eq_dir, 'nodos_demanda.csv'))[:16],
    }
    manifiesto = os.path.join(raiz, 'paratec', str(fecha), 'manifest.json')
    if os.path.exists(manifiesto):
        import json
        with open(manifiesto, encoding='utf-8') as f:
            m = json.load(f)
        t['_procedencia'].update(
            extraido_en=m.get('extracted_at'),
            version_scraper=m.get('scraper_version'),
            version_despacho=(m.get('dispatch_version') or {}).get('used'))
    return t


# ---------------------------------------------------------------------------
# El grafo que sube de STR a STN
# ---------------------------------------------------------------------------

class _Elevador:
    """Encuentra, para cada subestacion, el bus del modelo mas cercano."""

    def __init__(self, tablas, buses_modelo, alias=None):
        self.destino = N.buses_por_subestacion(buses_modelo, alias)
        bus = tablas['buses']
        self.sub_de_bus = {b: N.norm(s)
                           for b, s in zip(bus.bus_id, bus.substation)}
        # SE PROBO un ascenso en DOS ETAPAS ---primero por las lineas de
        # menos de 220 kV, luego por todas--- con la hipotesis de que plantas
        # como TERMOVALLE o TERMOEMCALI acababan en SAN MARCOS (VALLE) por
        # cruzar el backbone de 500. NO CAMBIO NADA: San Marcos ya era la
        # subestacion del modelo mas cercana por la propia red de 115, asi
        # que la hipotesis era falsa y la complejidad no se paga. Ver
        # medido y declarado: el desplazamiento es mayoritariamente real.
        g = collections.defaultdict(set)
        for a, b in zip(tablas['lines'].from_bus, tablas['lines'].to_bus):
            sa, sb = self.sub_de_bus.get(a), self.sub_de_bus.get(b)
            if sa and sb and sa != sb:
                g[sa].add(sb)
                g[sb].add(sa)
        self.grafo = g
        self._memo = {}

    def subir(self, sub):
        """(bus del modelo, saltos). (None, -1) si no hay camino."""
        if sub in self._memo:
            return self._memo[sub]
        r = self._bfs(sub)
        self._memo[sub] = r
        return r

    def _bfs(self, sub):
        if sub in self.destino:
            return self.destino[sub], 0
        return self._bfs_en(sub, self.grafo)

    def _bfs_en(self, sub, grafo):
        vistos, frente = {sub}, [sub]
        for salto in range(1, SALTOS_MAX + 1):
            siguiente = []
            # ordenado para que el resultado no dependa del orden de un set
            for s in sorted(frente):
                for v in sorted(grafo.get(s, ())):
                    if v in vistos:
                        continue
                    if v in self.destino:
                        return self.destino[v], salto
                    vistos.add(v)
                    siguiente.append(v)
            frente = siguiente
            if not frente:
                break
        return None, -1

    def desde_bus(self, bus_paratec):
        sub = self.sub_de_bus.get(bus_paratec)
        return self.subir(sub) if sub else (None, -1)


# ---------------------------------------------------------------------------
# La fuente
# ---------------------------------------------------------------------------

class FuenteXM:
    """Punto de operacion de (fecha, periodo) sobre los buses del modelo.

    Entrega lo mismo que `data_integration.integrar()` mas
    `desagregacion.desglose_de()`, y ademas un `ReporteCalidad`.

        f = FuenteXM('2026-10-20', 19)
        iny = f.inyecciones()          # DataFrame[P, Q, v_set] por bus
        gen, carga = f.desglose()      # dos Series en MW
        print(f.calidad().resumen())
    """

    nombre = 'XM (PARATEC + dDEC + PRON_BARRA)'

    def __init__(self, fecha, periodo, raiz=None, buses_modelo=None,
                 verbose=True):
        if not 1 <= int(periodo) <= 24:
            raise ValueError(f'periodo fuera de 1..24: {periodo}')
        self.fecha, self.periodo, self.verbose = str(fecha), int(periodo), verbose
        self.tablas = cargar_snapshot(fecha, raiz)
        if buses_modelo is None:
            from stn.data_loader import load_lines
            ld = load_lines(verbose=False)
            buses_modelo = sorted(set(ld.bus0) | set(ld.bus1))
        self.buses_modelo = list(buses_modelo)
        self.alias = N.cargar_alias()
        self.correcciones = cargar_correcciones()
        self._elev = _Elevador(self.tablas, self.buses_modelo, self.alias)
        self._calc = None

    # -- construccion ------------------------------------------------------

    def _generacion(self):
        """MW por bus del modelo, y los recursos que no se pudieron ubicar."""
        t = self.tablas
        eq = t['_equivalencias']['recursos']
        planta_de_recurso = collections.defaultdict(list)
        for _, r in eq[eq.paratec_ids.notna()].iterrows():
            for pid in str(r.paratec_ids).split(';'):
                if pid.strip():
                    planta_de_recurso[N.norm(r.resource)].append(N.norm(pid))

        # Los enlaces internacionales no tienen unidades en PARATEC: su
        # potencia entra por la barra de conexion. Sin este caso aparte, un
        # dia con la interconexion activa perderia esos MW como "recurso sin
        # unidades en PARATEC". El 2026-10-20 no se nota porque esta a 0 las
        # 24 horas, que es justo lo que lo hacia facil de pasar por alto.
        enlace_de_recurso = {
            N.norm(r.resource): r.connection_bus
            for _, r in eq.iterrows()
            if str(r.kind).strip() == 'intl_link' and pd.notna(r.connection_bus)}

        unidades = collections.defaultdict(list)
        for _, r in t['generators'].iterrows():
            if pd.notna(r.bus):
                unidades[N.norm(r.plant)].append(
                    (r.bus, float(r.p_max_mw or 0.0)))

        d = t['dispatch']
        d = d[(d.period == self.periodo) & (d.p_mw > 0)]
        por_bus, sin_ubicar, traslados = {}, [], []
        for _, r in d.sort_values('resource').iterrows():
            res, p = N.norm(r.resource), float(r.p_mw)

            if res in enlace_de_recurso:
                bus_m, saltos = self._elev.desde_bus(enlace_de_recurso[res])
                if bus_m is None:
                    sin_ubicar.append((r.resource, p,
                                       'enlace internacional sin barra en el '
                                       'modelo'))
                    continue
                por_bus[bus_m] = por_bus.get(bus_m, 0.0) + p
                if saltos > 0:
                    traslados.append((r.resource, 'enlace',
                                      enlace_de_recurso[res], bus_m, p,
                                      saltos))
                continue

            plantas = planta_de_recurso.get(res, [res])
            uds = [u for pl in plantas for u in unidades.get(pl, [])]
            if not uds:
                sin_ubicar.append((r.resource, p,
                                   'recurso sin unidades en PARATEC'))
                continue
            destinos = []
            for bus_p, pmax in uds:
                bus_m, saltos = self._elev.desde_bus(bus_p)
                if bus_m:
                    destinos.append((bus_m, pmax, bus_p, saltos))
            if not destinos and res in self.correcciones:
                # Punto de conexion aislado en la tabla de lineas de PARATEC.
                # Se lleva a la subestacion curada; ver FILE_CORRECCIONES.
                bus_m, saltos = self._elev.subir(self.correcciones[res])
                if bus_m:
                    destinos = [(bus_m, 1.0, self.correcciones[res], saltos)]
                    traslados.append((r.resource, 'generacion (curado)',
                                      self.correcciones[res], bus_m, p,
                                      saltos))
            if not destinos:
                sin_ubicar.append((r.resource, p,
                                   'sin bus del modelo alcanzable'))
                continue
            # Reparto proporcional a la placa, igual que hace sinsim cuando un
            # recurso cae en varias barras.
            total_pmax = sum(x[1] for x in destinos)
            for bus_m, pmax, bus_p, saltos in destinos:
                cuota = (p * pmax / total_pmax if total_pmax
                         else p / len(destinos))
                por_bus[bus_m] = por_bus.get(bus_m, 0.0) + cuota
                if saltos > 0:
                    traslados.append((r.resource, 'generacion', bus_p,
                                      bus_m, cuota, saltos))
        return (pd.Series(por_bus, dtype=float),
                pd.DataFrame(sin_ubicar,
                             columns=['recurso', 'p_mw', 'razon']),
                traslados, float(d.p_mw.sum()))

    def _demanda(self):
        """P y Q por bus del modelo, y los nodos que no se pudieron ubicar."""
        t = self.tablas
        eq = t['_equivalencias']['nodos']
        sub_de_nodo = {(N.norm(r.node), float(r.vn_kv)): N.norm(r.substation)
                       for _, r in eq.iterrows()}

        d = t['demand']
        d = d[d.period == self.periodo]
        p_bus, q_bus, sin_ubicar, traslados = {}, {}, [], []
        for _, r in d.sort_values(['node', 'vn_kv']).iterrows():
            # La demanda activa es Ener.Act. + Perd.PSTR, igual que sinsim:
            # las perdidas de referir la carga al punto de conexion son parte
            # de lo que el STN tiene que entregar.
            p = float(r.p_act_mw) + float(r.p_pstr_mw)
            q = float(r.q_mvar)
            sub = sub_de_nodo.get((N.norm(r.node), float(r.vn_kv)))
            if sub is None:
                sin_ubicar.append((r.node, float(r.vn_kv), p, q,
                                   'nodo sin equivalencia en sinsim'))
                continue
            bus_m, saltos = self._elev.subir(sub)
            if bus_m is None:
                sin_ubicar.append((r.node, float(r.vn_kv), p, q,
                                   'sin bus del modelo alcanzable'))
                continue
            p_bus[bus_m] = p_bus.get(bus_m, 0.0) + p
            q_bus[bus_m] = q_bus.get(bus_m, 0.0) + q
            if saltos > 0:
                traslados.append((r.node, 'demanda', sub, bus_m, p, saltos))
        return (pd.Series(p_bus, dtype=float), pd.Series(q_bus, dtype=float),
                pd.DataFrame(sin_ubicar,
                             columns=['nodo', 'vn_kv', 'p_mw', 'q_mvar',
                                      'razon']),
                traslados,
                float((d.p_act_mw + d.p_pstr_mw).sum()), float(d.q_mvar.sum()))

    def _construir(self):
        if self._calc is not None:
            return self._calc

        gen, gen_fuera, tras_g, p_desp_total = self._generacion()
        carga, q_carga, dem_fuera, tras_d, p_dem_total, q_dem_total = \
            self._demanda()

        con_dato = sorted(set(gen.index) | set(carga.index))

        # El indice cubre TODA la red, no solo las barras con dato. Hace falta
        # para los taps: `power_flow._ajustar_taps` solo ajusta un
        # autotransformador si sus DOS barras traen consigna, y
        # `asignar_inyecciones` solo recorre las filas de `inyecciones`. Con el
        # indice reducido, las barras de 500 kV de transito ---que con XM no
        # reciben ni carga ni generacion--- se quedaban sin consigna y los 16
        # autotrafos se quedaban en toma 1,0. Medido: 0 de 16 ajustados.
        #
        # Las barras que se añaden llevan P=Q=0, asi que no crean ni generador
        # ni carga; solo aportan su consigna. Cuales no tenian dato de XM
        # queda en `calidad().buses_sin_datos`, que es donde hay que mirarlo.
        idx = sorted(set(con_dato) | set(self.buses_modelo))
        gen = gen.reindex(idx).fillna(0.0)
        carga = carga.reindex(idx).fillna(0.0)
        q_carga = q_carga.reindex(idx).fillna(0.0)

        iny = pd.DataFrame({
            'P': gen - carga,
            # Convenio de inyeccion: la demanda consume, asi que su Q entra
            # con signo negativo. `asignar_inyecciones` lo vuelve a invertir
            # al crear el Load (`q_set = -Q`).
            'Q': -q_carga,
            'v_set': [consigna(b) for b in idx],
        }, index=idx)
        iny.index.name = 'bus'

        cols_t = ['elemento', 'tipo', 'origen', 'bus_destino', 'p_mw', 'saltos']
        cal = ReporteCalidad(
            fuente=self.nombre, fecha=self.fecha, periodo=self.periodo,
            p_despacho_total_mw=p_desp_total,
            p_despacho_ubicado_mw=float(gen.sum()),
            p_demanda_total_mw=p_dem_total,
            p_demanda_ubicada_mw=float(carga.sum()),
            q_demanda_total_mvar=q_dem_total,
            q_demanda_ubicada_mvar=float(q_carga.sum()),
            recursos_no_ubicados=gen_fuera,
            nodos_no_ubicados=dem_fuera,
            traslados=pd.DataFrame(tras_g + tras_d, columns=cols_t),
            buses_sin_datos=tuple(sorted(set(self.buses_modelo) - set(con_dato))),
            procedencia=self.tablas['_procedencia'],
        )
        self._calc = (iny, gen, carga, cal)
        if self.verbose:
            print(cal.resumen())
        return self._calc

    # -- contrato publico --------------------------------------------------

    def inyecciones(self):
        return self._construir()[0].copy()

    def desglose(self):
        """(gen, carga) en MW por bus. Cumple gen - carga == inyecciones.P.

        Aqui no hace falta el aparato de atribucion de `desagregacion.py`: el
        despacho y el pronostico llegan SEPARADOS de origen, asi que la bruta
        es el dato, no una inferencia. Las ~300 lineas de grafos de ese modulo
        existen solo porque PowerFactory entrega inyecciones NETAS.
        """
        _, gen, carga, _ = self._construir()
        return gen.copy(), carga.copy()

    def calidad(self):
        return self._construir()[3]

    def produccion_solar_pu(self):
        """Cuanto rinde la fotovoltaica en este periodo, en pu de su placa.

        Devuelve (producido_MW, placa_MW, pu) sobre los recursos del despacho
        cuyas plantas son TODAS solares en PARATEC.

        PARA QUE SIRVE. `deslastre_btm` necesita saber que fraccion del BTM
        conectado por inversor esta produciendo de verdad cuando ocurre el
        evento. Suponer la placa entera ---que es el 1,0 por omision--- infla
        el "BTM sano" y con el la suficiencia del esquema. De noche el factor
        es 0 y el evento sencillamente no existe.

        Hasta ahora ese factor salia de la tabla cableada de
        `escenario_diurno.PERFIL`, con dos horas y una sola fecha. Aqui se
        mide, para cualquier fecha y cualquier hora: el 2026-10-20 da 0,000 de
        noche, 0,697 a las 10-11 h y un maximo de 0,732 al mediodia.

        ES UN PROXY, y conviene decirlo: se mide sobre la solar de gran escala
        que XM despacha, y se aplica al BTM, que por definicion no se despacha.
        Es el mismo proxy que usa `escenario_diurno`; lo que cambia es que el
        dato es de la fecha que se esta simulando.
        """
        t = self.tablas
        eq = t['_equivalencias']['recursos']
        plantas_de = collections.defaultdict(list)
        for _, r in eq[eq.paratec_ids.notna()].iterrows():
            for pid in str(r.paratec_ids).split(';'):
                if pid.strip():
                    plantas_de[N.norm(r.resource)].append(N.norm(pid))

        tech, placa = collections.defaultdict(set), collections.Counter()
        for _, r in t['generators'].iterrows():
            p = N.norm(r.plant)
            tech[p].add(r.tech)
            placa[p] += float(r.p_max_mw or 0.0)

        d = t['dispatch']
        d = d[d.period == self.periodo]
        prod = pl = 0.0
        for _, r in d.iterrows():
            res = N.norm(r.resource)
            ps = plantas_de.get(res, [res])
            tecnologias = set().union(*(tech.get(p, set()) for p in ps)) \
                if ps else set()
            # Solo los recursos INTEGRAMENTE solares. Uno mixto no permite
            # separar que parte de su programa es fotovoltaica.
            if tecnologias == {'solar'}:
                prod += float(r.p_mw)
                pl += sum(placa[p] for p in ps)
        return prod, pl, (prod / pl if pl else 0.0)

    def preparar_red(self, network):
        """Abre los enlaces internacionales que no despachan ese periodo.

        Parte del contrato de `fuentes.base.FuentePuntoOperacion`: corre
        despues de `build_network` y antes de `asignar_inyecciones`. Ver
        `abrir_enlaces()` para por que hace falta.
        """
        return abrir_enlaces(network, self.enlaces_inactivos(),
                             verbose=self.verbose)

    def enlaces_inactivos(self):
        """Enlaces internacionales con despacho nulo en este periodo.

        Devuelve una lista de dicts con `recurso`, `bus_colombia`,
        `bus_exterior` y `p_mw`. Un enlace que no aparece en el dDEC cuenta
        como inactivo: no hay programa, no hay intercambio.
        """
        enlaces = cargar_enlaces()
        if not enlaces:
            return []
        d = self.tablas['dispatch']
        d = d[d.period == self.periodo]
        prog = {N.norm(r.resource): float(r.p_mw) for _, r in d.iterrows()}
        fuera = []
        for e in enlaces:
            p = prog.get(N.norm(e['recurso']), 0.0)
            if abs(p) < 1e-9:
                fuera.append(dict(e, p_mw=p))
        return fuera


def cargar_correcciones(path=None):
    """{recurso -> subestacion} curadas. La columna `nota` es obligatoria."""
    path = path or FILE_CORRECCIONES
    if not os.path.exists(path):
        return {}
    df = pd.read_csv(path, encoding='utf-8-sig')
    faltan = [c for c in ('recurso', 'subestacion', 'nota')
              if c not in df.columns]
    if faltan:
        raise ValueError(f'{path}: faltan las columnas {faltan}')
    sin_nota = df[df.nota.isna() | (df.nota.astype(str).str.strip() == '')]
    if len(sin_nota):
        raise ValueError(
            f'{path}: {len(sin_nota)} correcciones sin `nota`: '
            f'{sin_nota.recurso.tolist()}. Mover cientos de MW de sitio sin '
            f'dejar la evidencia no entra.')
    return {N.norm(r.recurso): N.norm(r.subestacion) for _, r in df.iterrows()}


def cargar_enlaces(path=None):
    """Enlaces internacionales curados. Lista de dicts; vacia si no hay tabla."""
    path = path or FILE_ENLACES
    if not os.path.exists(path):
        return []
    df = pd.read_csv(path, encoding='utf-8-sig')
    faltan = [c for c in ('recurso', 'bus_colombia', 'bus_exterior', 'nota')
              if c not in df.columns]
    if faltan:
        raise ValueError(f'{path}: faltan las columnas {faltan}')
    sin_nota = df[df.nota.isna() | (df.nota.astype(str).str.strip() == '')]
    if len(sin_nota):
        raise ValueError(
            f'{path}: {len(sin_nota)} enlaces sin `nota`. Abrir una linea del '
            f'modelo sin dejar la evidencia no entra.')
    return df[['recurso', 'bus_colombia', 'bus_exterior']].to_dict('records')


def abrir_enlaces(network, enlaces, verbose=True):
    """Abre los circuitos de los enlaces dados y aisla lo que quede colgando.

    POR QUE. Una interconexion con programa de intercambio nulo esta ABIERTA,
    no energizada en vacio. Dejarla cerrada convierte el tramo exterior en un
    radial sin carga que sube por efecto Ferranti: medido sobre el 2026-10-20,
    PIMAMPIRO es la barra de mayor tension del modelo en las 24 horas ---hasta
    1,249 pu en la madrugada--- y arrastra consigo de 2 a 9 barras por encima
    de 1,10 pu. No es un defecto del equivalente ni de la consigna de tension:
    es que el enlace esta a 0 MW todo el dia y aun asi conectado.

    Despues de quitar los circuitos se reutiliza `contingencias._separar_islas`,
    que deja solo la isla con mas generacion. Asi lo que quede desconectado se
    retira de verdad, en vez de quedarse como una sub-red que el flujo
    alimentaria con un slack ficticio.

    Devuelve (lineas_abiertas, buses_retirados).
    """
    if not enlaces:
        return [], []

    from stn import contingencias as C

    quitar = []
    for e in enlaces:
        par = {e['bus_colombia'], e['bus_exterior']}
        for l in network.lines.index:
            if {network.lines.at[l, 'bus0'],
                    network.lines.at[l, 'bus1']} == par:
                quitar.append(l)
    if not quitar:
        if verbose:
            print('  ningun circuito que abrir: el enlace no esta en la red')
        return [], []

    antes = set(network.buses.index)
    network.remove('Line', quitar)
    C._separar_islas(network)
    retirados = sorted(antes - set(network.buses.index))

    if verbose:
        nombres = ', '.join(e['recurso'] for e in enlaces)
        print(f'  enlaces a 0 MW abiertos ({nombres}): '
              f'{len(quitar)} circuitos, {len(retirados)} barras retiradas'
              + (f' ({", ".join(retirados)})' if retirados else ''))
    return quitar, retirados


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='Punto de operacion desde un snapshot de XM.')
    ap.add_argument('--fecha', help='YYYY-MM-DD')
    ap.add_argument('--periodo', type=int, default=19, help='1..24')
    ap.add_argument('--listar', action='store_true',
                    help='solo lista las fechas disponibles')
    ap.add_argument('--barrido', action='store_true',
                    help='cobertura de los 24 periodos')
    args = ap.parse_args()

    if args.listar or not args.fecha:
        print(f'Snapshots en {os.path.abspath(DIR_XM)}:')
        for f in fechas_disponibles():
            print('  ', f)
        return

    if args.barrido:
        filas = []
        for h in range(1, 25):
            c = FuenteXM(args.fecha, h, verbose=False).calidad()
            filas.append({
                'periodo': h,
                'gen_MW': round(c.p_despacho_ubicado_mw, 1),
                'dem_MW': round(c.p_demanda_ubicada_mw, 1),
                'cob_gen_%': round(c.cobertura_despacho_pct, 2),
                'cob_dem_%': round(c.cobertura_demanda_pct, 2),
                'balance_MW': round(c.balance_mw, 1),
                'sesgo_MW': round(c.sesgo_de_perdidas_mw, 1),
                'apto': c.apto_para_frecuencia,
            })
        print(pd.DataFrame(filas).to_string(index=False))
        return

    f = FuenteXM(args.fecha, args.periodo)
    iny = f.inyecciones()
    gen, carga = f.desglose()
    print(f'\ninyecciones: {len(iny)} buses | '
          f'P {iny.P.sum():+.1f} MW | Q {iny.Q.sum():+.1f} Mvar')
    print(f'desglose   : gen {gen.sum():.1f} MW, carga {carga.sum():.1f} MW, '
          f'invariante max |gen-carga-P| = '
          f'{(gen - carga - iny.P).abs().max():.2e}')


if __name__ == '__main__':
    main()
