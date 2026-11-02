# escenario_diurno.py
# Deriva un punto de operacion DIURNO a partir del caso de punta nocturna.
#
# -------------------------------------------------------------------------
# POR QUE HACE FALTA
# -------------------------------------------------------------------------
# El unico caso medido que tiene el modelo es la punta nocturna: 12.472,5 MW
# de demanda (un 3 % sobre el record del SIN) y fotovoltaica EXACTAMENTE cero
# pese a los 4.200 MW instalados. Las dos cosas solo coinciden despues del
# ocaso, que en Colombia cae cerca de las 18:00 todo el año.
#
# Esa hora es la peor posible para lo que estudia este trabajo. El fenomeno
# central ---la desconexion en bloque de la generacion solar detras del
# contador--- NO PUEDE OCURRIR a las 19:00, porque no hay nada produciendo que
# desconectar. Un escenario que "pierde 4.200 MW de solar" en la punta nocturna
# es fisicamente imposible, y el motor lo refleja: la salida solar por defecto
# vale cero.
#
# Este modulo construye el punto de operacion que falta.
#
# -------------------------------------------------------------------------
# QUE ES Y QUE NO ES
# -------------------------------------------------------------------------
# NO es un caso medido. Es un caso DERIVADO: se parte del de punta ---que si
# esta medido y validado contra PowerFactory--- y se le aplican tres factores
# que vienen de datos publicados de XM. Eso significa:
#
#   * La TOPOLOGIA, las impedancias y el reparto espacial de la demanda son
#     los del caso validado. No se inventa red.
#   * El NIVEL de demanda, la produccion solar y la eolica son factores
#     aplicados, cada uno con su fuente declarada en PERFIL.
#   * NO se puede validar contra PowerFactory. No existe un caso de referencia
#     diurno con el que contrastarlo. Lo que se hereda es la validacion de la
#     red, no la del punto de operacion.
#
# El supuesto mas fuerte, y conviene no perderlo de vista: se escala la demanda
# de cada barra por el MISMO factor. En la realidad el perfil horario no es
# igual en todas partes ---el aire acondicionado del Caribe tira de dia y la
# demanda residencial de Bogota tira de noche--- asi que el reparto espacial
# del mediodia real no es el de la punta reescalado. Corregirlo exigiria el
# perfil horario por area, que XM publica pero que el modelo no carga todavia.
# Es una limitacion conocida y declarada.
#
# -------------------------------------------------------------------------
# LA TRANSFORMACION
# -------------------------------------------------------------------------
#   1. carga_diurna(b) = carga_punta(b) * f_demanda
#   2. solar(b)        = placa_subarea * fc_solar, repartida por demanda
#   3. carga_neta(b)   = carga_diurna(b) - solar(b)
#   4. la generacion del STN baja hasta cubrir sum(carga_neta) + perdidas
#
# El paso 3 es la clave, y es lo que hace interesante al caso: la solar de gran
# escala NO es un generador del modelo (ninguna de las 370 plantas conecta a
# 200 kV o mas), asi que desde el STN no se ve como generacion sino como MENOS
# CARGA. El STN ve la demanda neta, igual que la ve el operador.
#
# De ahi sale el efecto que importa: la generacion sincrona tiene que bajar NO
# SOLO por la caida de demanda, sino tambien por toda la solar. Menos maquinas
# acopladas es menos inercia, y menos inercia es un RoCoF mayor ante la misma
# perdida. El caso diurno es de menor demanda y aun asi mas fragil.

import argparse

import pandas as pd

from stn import config


# ---------------------------------------------------------------------------
# El perfil horario, medido
# ---------------------------------------------------------------------------
#
# TODO LO DE AQUI ESTA MEDIDO, no supuesto. Sale de la API publica de XM,
# descargada el 3-10-2026:
#
#   POST https://servapibi.xm.com.co/hourly
#        {"MetricId":"DemaReal","Entity":"Sistema", ...}      demanda
#        {"MetricId":"Gene","Entity":"Recurso", ...}          generacion
#   POST https://servapibi.xm.com.co/lists
#        {"MetricId":"ListadoRecursos","Entity":"Sistema"}    tecnologia
#
# Muestra: 638 dias (1-1-2025 a 30-9-2026) para la demanda; para el reparto
# por tecnologia, laborables de julio a septiembre de 2026 (n = 62), excluidos
# sabados, domingos y festivos.
#
# CONVENCION HORARIA. XM devuelve Hour01..Hour24 y `HourNN` es el intervalo
# (NN-1):00 -> NN:00. Comprobado por dos vias: el perfil solar es simetrico
# respecto a las 12,0 h exactas (mediodia solar colombiano) y el maximo de
# demanda cae en Hour20 en 544 de 638 dias, que es la punta nocturna.
#
# CONTRASTE DEL METODO. No existe metrica solar agregada en la API, asi que la
# serie solar se construyo cruzando `Gene` por recurso con el listado de
# tecnologias. El agregado resultante da 8,07 % de solar sobre generacion total
# en abril de 2026 y 8,12 % en mayo, que son exactamente las cifras que XM
# publica en sus informes mensuales de esos meses. La agregacion es correcta.
#
# POR QUE LAS 10-11 Y NO EL MEDIODIA. Es la hora de maxima penetracion solar
# (23,50 % de la demanda) y de maximo cociente solar/sincrona (0,304), con la
# generacion sincrona a un 2 % de su minimo diario. La razon es asimetrica: la
# solar ya esta al 98,7 % de su pico a las 10:00, mientras la demanda sigue
# subiendo toda la mañana. De 10-11 a 11-12 la solar gana 34 MW y la demanda
# sube 321. Sobre 273 dias, el maximo diario de penetracion cae en Hour11 en
# 100 dias, mas que en ninguna otra.

# Demanda media del SIN en la punta nocturna (Hour20), laborables jul-sep 2026.
# Sirve de ancla para pasar de la escala real a la del modelo.
DEMANDA_PUNTA_REAL_MW = 12082.8

PERFIL = {
    # 10:00-11:00. La hora de maxima penetracion solar.
    11: {
        'etiqueta': '10:00-11:00, martes a jueves',
        # demanda Hour11 / demanda Hour20 = 10.910,4 / 12.082,8
        'f_demanda': 0.9030,
        # Solar MEDIDA a esa hora, en escala real del SIN. El modulo la pasa a
        # la escala del modelo con el cociente de demandas de punta.
        'solar_medida_mw': 2563.9,
        'demanda_medida_mw': 10910.4,
        # Cociente Hour11/Hour20 de la generacion de cada tecnologia, medido.
        #   hidraulica  5.493,3 / 8.749,7
        #   termica     2.842,6 / 3.383,4
        #   eolica         22,3 /    28,8
        # La hidraulica absorbe el 86 % del ajuste y la termica solo el 14 %:
        # la termica esta despachada por restricciones y minimos tecnicos y
        # apenas se mueve. Repartir la bajada proporcionalmente entre
        # tecnologias SOBREESTIMARIA la inercia, porque es justo la hidraulica
        # ---la de mayor H del parque colombiano--- la que se desacopla.
        'factores_gen': {
            'hidraulica': 0.6278,
            'termica': 0.8402,
            'eolica': 0.7743,
            # El modelo tiene 368,4 MW de tecnologia desconocida. Se le aplica
            # el cociente de la sincrona agregada, 8.431,4 / 12.234,1.
            'desconocido': 0.6892,
        },
        'penetracion_solar_pct': 23.50,
        # Fraccion de su placa que entrega la fotovoltaica a esta hora. Sale
        # de las 14 plantas despachadas centralmente con CEN >= 10 MW: 1.572,1
        # sobre 2.017,0 MW en su hora pico (77,9 %), por el cociente
        # Hour11/Hour12 de 0,987. Es un TECHO para la AGPE de tejado, que no
        # tiene seguidor. Lo consume `deslastre_btm` para no confundir la
        # placa del BTM con lo que de verdad hay produciendo.
        'produccion_solar_pu': 0.769,
    },
    # 12:00-13:00. El mediodia "intuitivo": mas demanda y algo menos solar que
    # las 10-11, luego MENOS penetracion. Se incluye para poder contrastar.
    13: {
        'etiqueta': '12:00-13:00, martes a jueves',
        'f_demanda': 0.9298,           # 11.235,3 / 12.082,8
        'solar_medida_mw': 2556.3,
        'demanda_medida_mw': 11235.3,
        'factores_gen': {
            'hidraulica': 0.6530,      # estimado del agregado de la hora
            'termica': 0.8402,
            'eolica': 0.7743,
            'desconocido': 0.7169,     # 8.770,4 / 12.234,1
        },
        'penetracion_solar_pct': 22.75,
        'produccion_solar_pu': 0.779,  # 0,779 x 1,000 (Hour12 es el pico)
    },
}


def factores(hora, perfil=None):
    """Devuelve el diccionario de la hora, o falla.

    Falla a proposito si la hora no esta en PERFIL. Es preferible a devolver
    un valor por defecto: un escenario construido sobre un factor inventado
    parece un resultado y no lo es.
    """
    perfil = PERFIL if perfil is None else perfil
    if hora not in perfil:
        raise KeyError(
            f"No hay perfil para la hora {hora}. Horas disponibles: "
            f"{sorted(perfil) if perfil else 'ninguna, PERFIL esta vacio'}. "
            "Las claves son la convencion HourNN de XM: 11 = 10:00-11:00.")
    return perfil[hora]


# ---------------------------------------------------------------------------
# Construccion
# ---------------------------------------------------------------------------

def _redespachar(gen_p, tipos_por_gen, factores_gen, objetivo_mw):
    """Lleva la generacion a `objetivo_mw` con la FORMA del despacho medido.

    Dos pasos, y la distincion importa:

    1. A cada barra se le aplica el cociente medido de SU tecnologia. Eso
       reproduce el reparto real: la hidraulica baja mucho, la termica casi
       nada. Esta es la informacion que aporta el dato de XM.

    2. Lo que quede de descuadre se reparte proporcionalmente sobre todo lo
       que sigue despachando, para cerrar el balance. Es un ajuste de cierre,
       no un criterio: si el paso 1 estuviera bien calibrado para este modelo,
       valdria cero.

    El tamano del paso 2 es, de hecho, un CONTRASTE. Los cocientes salen del
    SIN real y el modelo tiene otra mezcla (es una red futura, con mucha mas
    eolica y menos termica que el sistema de hoy). Si aun asi el paso 1 deja
    el balance casi cerrado, los dos sistemas se comportan parecido y el
    traslado esta justificado. Se informa como `ajuste_cierre_pct`.

    Devuelve (generacion nueva, porcentaje de ajuste del paso 2).
    """
    p = gen_p.copy().astype(float)
    for g in p.index:
        f = factores_gen.get(tipos_por_gen.get(g, 'desconocido'))
        if f is not None:
            p[g] = p[g] * float(f)

    forma = float(p.sum())
    objetivo = float(objetivo_mw)
    if forma <= 1e-9:
        return p, 0.0

    ajuste = objetivo / forma
    return p * ajuste, (ajuste - 1.0) * 100.0


def construir(hora=11, f_demanda=None, solar_mw=None, solar_pu=None,
              por='subarea', perfil=None, limites=None, verbose=True):
    """Construye y resuelve el caso diurno. Devuelve (red, info).

    Por defecto toma todo de PERFIL[hora]. Los parametros sueltos permiten
    barridos de sensibilidad sin tocar el perfil:

      `f_demanda`  demanda diurna / demanda de la punta nocturna
      `solar_mw`   produccion solar OBJETIVO en la escala del modelo
      `solar_pu`   alternativa: fraccion de la capacidad registrada

    DE DONDE SALE LA SOLAR, que es la decision delicada. Se ancla a la
    PRODUCCION MEDIDA por XM, no a un factor de planta sobre la capacidad
    registrada. El motivo es que la capacidad registrada no es fiable para
    esto: el reporte Usolar trae 4.200,1 MW de los que solo 2.922,2 estan en
    operacion comercial y 1.277,9 en pruebas, y las de pruebas producen muy
    poco. Aplicar un factor de planta al registro entero daria una produccion
    inflada. La produccion, en cambio, esta medida hora a hora.

    El traslado de escala es el cociente de demandas de punta: el modelo es
    una red futura con mas demanda que el SIN de hoy, asi que la solar se
    escala en la misma proporcion que la demanda.
    """
    from stn import contingencias
    from stn import power_flow
    from stn import zonas
    from stn import deslastre_btm as D
    from stn import agc_deslastre as AGC

    p = factores(hora, perfil) if hora is not None else {}
    if f_demanda is None:
        f_demanda = p['f_demanda']
    factores_gen = p.get('factores_gen', {})

    # Se parte del caso SIN limitar y se limita al final: la conmutacion
    # PV->PQ depende del punto de operacion, asi que hay que rehacerla sobre
    # el estado diurno y no heredar la del nocturno.
    red, base_diag = contingencias.caso_base(verbose=False, limites=False)
    mapa = zonas.area_por_bus(red)

    carga_punta = red.loads.p_set.copy()
    demanda_punta = float(carga_punta.sum())
    # generacion RESUELTA del caso de punta: incluye el aporte del slack, asi
    # que su diferencia con la demanda son las perdidas reales del caso.
    red_punta_gen = float(red.generators_t.p.iloc[0].sum())

    # 1. la demanda baja al nivel de la hora
    red.loads.p_set = carga_punta * f_demanda
    red.loads.q_set = red.loads.q_set * f_demanda
    demanda_diurna = float(red.loads.p_set.sum())

    # 2. la solar produce. Objetivo en MW, trasladado a la escala del modelo.
    escala = demanda_punta / DEMANDA_PUNTA_REAL_MW
    if solar_mw is None and solar_pu is None:
        solar_mw = p['solar_medida_mw'] * escala
    cap_registrada = float(D.cargar_solar_gran_escala(por=por).sum())
    if solar_pu is None:
        solar_pu = float(solar_mw) / cap_registrada
    forma = D.repartir_solar(red, mapa, salida_pu=float(solar_pu), por=por)
    solar_bus = forma
    solar_prod = float(solar_bus.sum())

    # 3. la solar se netea contra la carga de su barra: desde el STN no se ve
    #    como generacion sino como menos carga.
    por_bus = red.loads.groupby('bus').p_set.sum()
    for bus, mw in solar_bus[solar_bus > 1e-9].items():
        cargas = red.loads.index[red.loads.bus == bus]
        if not len(cargas) or por_bus.get(bus, 0.0) <= 1e-9:
            continue
        peso = red.loads.loc[cargas, 'p_set'] / por_bus[bus]
        red.loads.loc[cargas, 'p_set'] -= peso * float(mw)
    carga_neta = float(red.loads.p_set.sum())
    neta_por_bus = red.loads.groupby('bus').p_set.sum()

    # 4. la generacion del STN baja con la FORMA del despacho medido
    tipos = AGC.Sistema(red).gen.tipo
    gen_p = red.generators.p_set.copy()
    gen_punta = float(gen_p.sum())

    # Perdidas del caso de punta, medidas sobre el flujo ya resuelto. No se
    # leen de `base_diag` porque ese diccionario no las trae: un `.get` con
    # valor por defecto devolvia cero en silencio y dejaba al slack cargando
    # con las perdidas enteras (87,5 MW sobre una sola maquina, frente a los
    # 2,9 MW que toma en el caso base).
    perdidas = float(red_punta_gen - demanda_punta)
    # Primera estimacion: las perdidas escalan aproximadamente con el cuadrado
    # del flujo. Es solo el arranque; el bucle de abajo la corrige.
    perdidas_est = perdidas * (carga_neta / demanda_punta) ** 2

    tipos_por_gen = {g: tipos.get(g, 'desconocido') for g in gen_p.index}
    gen_base = gen_p.copy()

    # Punto fijo sobre las perdidas. Sin esto el slack se come la diferencia
    # entre las perdidas estimadas y las reales: el escalado cuadratico se
    # queda corto en el caso diurno ---hay flujos inversos y el patron de
    # transporte no es el de la punta--- y una sola maquina acababa cargando
    # con +27,8 MW. Dos o tres vueltas lo dejan por debajo del MW.
    ajuste_pct = 0.0
    for _ in range(8):
        gen_p, ajuste_pct = _redespachar(gen_base, tipos_por_gen, factores_gen,
                                         carga_neta + perdidas_est)
        red.generators.p_set = gen_p
        ok, _ = power_flow.run_power_flow(red, verbose=False)
        if not ok:
            raise RuntimeError("El caso diurno no converge.")
        real = float(red.generators_t.p.iloc[0].sum()) - carga_neta
        if abs(real - perdidas_est) < 0.5:
            break
        perdidas_est = real

    if limites is not False:
        from stn import limites_reactivos
        red = (limites_reactivos.aplicar(red, verbose=False)[0]
               if limites is True
               else limites_reactivos.aplicar_si_procede(red))

    diag = contingencias._estado(red)
    sis = AGC.Sistema(red)
    sis_punta = AGC.Sistema(contingencias.caso_base(verbose=False)[0])
    info = {
        'hora': hora, 'etiqueta': p.get('etiqueta', ''),
        'f_demanda': f_demanda, 'solar_pu': solar_pu, 'por': por,
        'escala_modelo': escala,
        'ajuste_cierre_pct': ajuste_pct,
        'demanda_punta_MW': demanda_punta,
        'demanda_diurna_MW': demanda_diurna,
        'solar_capacidad_MW': cap_registrada,
        'solar_produccion_MW': solar_prod,
        'carga_neta_MW': carga_neta,
        'gen_punta_MW': gen_punta,
        'gen_diurna_MW': float(red.generators.p_set.sum()),
        'solar_bus': solar_bus[solar_bus > 1e-9],
        'produccion_solar_pu': p.get('produccion_solar_pu'),
        'barras_exportadoras': neta_por_bus[neta_por_bus < -1e-6],
        'mix': red.generators.groupby(
            pd.Series(tipos_por_gen)).p_set.sum(),
        'diag': diag, 'sis': sis, 'sis_punta': sis_punta,
    }

    if verbose:
        _informe(info)
    return red, info


def _informe(i):
    d, s, sp = i['diag'], i['sis'], i['sis_punta']

    def fila(nombre, diurno, punta, fmt='{:9.1f}', uni=''):
        delta = (diurno - punta) / punta * 100 if punta else 0.0
        print(f"  {nombre:26s} {fmt.format(punta)} {fmt.format(diurno)}"
              f"   {delta:+6.1f} %  {uni}")

    print()
    print("=" * 74)
    print(f"  ESCENARIO DIURNO  ---  {i['etiqueta'] or 'a medida'}")
    print("=" * 74)
    print(f"  demanda x{i['f_demanda']:.4f} sobre la punta  |  "
          f"solar {i['solar_pu']*100:.1f} % del registro  |  "
          f"reparto por {i['por']}")
    print(f"  escala modelo/SIN real {i['escala_modelo']:.4f}  |  "
          f"ajuste de cierre del redespacho {i['ajuste_cierre_pct']:+.2f} %")
    print()
    print(f"  {'':26s} {'PUNTA':>9s} {'DIURNO':>9s}")
    print("  " + "-" * 60)
    fila('demanda bruta', i['demanda_diurna_MW'], i['demanda_punta_MW'], uni='MW')
    print(f"  {'solar embebida':26s} {0.0:9.1f} {-i['solar_produccion_MW']:9.1f}"
          f"              MW")
    fila('demanda NETA del STN', i['carga_neta_MW'], i['demanda_punta_MW'],
         uni='MW')
    print("  " + "-" * 60)
    fila('generacion STN', i['gen_diurna_MW'], i['gen_punta_MW'], uni='MW')
    print()
    for tec, mw in i['mix'].sort_values(ascending=False).items():
        print(f"      {tec:22s} {mw:9.1f} MW")
    print()
    print("  --- lo que decide la frecuencia ---")
    print("  " + "-" * 60)
    fila('S_base sincronizada', s.S_base, sp.S_base, uni='MVA')
    fila('H equivalente', s.H, sp.H, fmt='{:9.3f}', uni='s')
    fila('energia cinetica H*S', s.H * s.S_base, sp.H * sp.S_base,
         fmt='{:9.0f}', uni='MVA.s')
    fila('reserva primaria', s.reserva, sp.reserva, uni='MW')
    print()
    print("  --- estado de la red ---")
    print(f"  tension   [{d['v_min']:.4f}, {d['v_max']:.4f}] pu"
          f"   bajas {len(d['v_bajas'])}  altas {len(d['v_altas'])}")
    print(f"  cargabilidad maxima  {d['carga_max_pct']:.1f} %")
    neg = i.get('barras_exportadoras')
    if neg is not None and len(neg):
        print(f"  barras con carga NETA NEGATIVA: {len(neg)} "
              f"({float(neg.sum()):.1f} MW)  ---  la solar local supera a la "
              f"demanda local y la barra exporta")
        for b, v in neg.sort_values().head(5).items():
            print(f"      {b:28s} {v:8.1f} MW")
    else:
        print("  ninguna barra queda con carga neta negativa")


def main():
    ap = argparse.ArgumentParser(
        description="Construye el punto de operacion diurno.")
    ap.add_argument('--hora', type=int, default=11,
                    help="clave HourNN de XM; 11 = 10:00-11:00 (defecto)")
    ap.add_argument('--f-demanda', type=float, default=None,
                    help="demanda diurna / demanda de punta")
    ap.add_argument('--solar-mw', type=float, default=None,
                    help="produccion solar objetivo en MW")
    ap.add_argument('--solar-pu', type=float, default=None,
                    help="produccion solar como fraccion del registro")
    ap.add_argument('--por', default='subarea', choices=['subarea', 'area'])
    args = ap.parse_args()

    construir(hora=args.hora, f_demanda=args.f_demanda,
              solar_mw=args.solar_mw, solar_pu=args.solar_pu,
              por=args.por, verbose=True)


if __name__ == '__main__':
    main()
