# agc_deslastre.py
"""Respuesta del STN a una perdida subita de generacion.

Encadena cuatro cosas sobre el modelo ya construido:

  1. Dinamica de frecuencia. Modelo de una sola masa: respuesta inercial,
     regulacion primaria por estatismo y amortiguamiento de la carga.
  2. EDAC. Deslastre automatico por baja frecuencia con el esquema regulado
     colombiano: ocho escalones del 5% con sus retardos.
  3. Eleccion de que carga botar. Ademas del reparto proporcional que hace el
     EDAC real, se puede resolver un OPF que elige EN QUE BARRAS deslastrar
     para no dejar corredores sobrecargados.
  4. AGC. Control secundario que reparte el desbalance residual entre los
     generadores con reserva y devuelve la frecuencia a 60 Hz.

Al final se resuelve un flujo AC del estado post-evento para comprobar que lo
que queda en pie es operable: sin sobrecargas ni tensiones fuera de rango.

Sobre el esquema EDAC
---------------------
El esquema vigente es el del Acuerdo 2105 del CNO, de 5 de febrero de 2026.
Mantiene la meta de cubrir el 40% de la demanda del SIN en ocho etapas, cada
una con una desconexion equivalente al 5%, con retardos de 200, 200, 400, 400,
600, 1000, 2000 y 4000 ms y funcion df/dt en las dos ultimas. El propio acuerdo
fija la tolerancia de las pruebas en +-0,03 Hz sobre el umbral de cada etapa y
+-50 ms sobre su retardo.

La tabla de ajustes esta transcrita completa del acuerdo, ya no hay supuestos:

    etapa  umbral   retardo   carga    df/dt: f      df/dt     retardo
      1    59,4 Hz    200 ms    5%
      2    59,2 Hz    200 ms    5%
      3    59,0 Hz    400 ms    5%
      4    58,8 Hz    400 ms    5%
      5    58,6 Hz    600 ms    5%
      6    58,6 Hz   1000 ms    5%
      7    58,4 Hz   2000 ms    5%       58,0 Hz   -0,3 Hz/s    200 ms
      8    58,4 Hz   4000 ms    5%       58,0 Hz   -0,2 Hz/s    400 ms

Dos detalles que no son erratas. Las etapas 5-6 comparten umbral (58,6 Hz) y
las 7-8 tambien (58,4 Hz): lo que las separa es el retardo, no la frecuencia.
Y la funcion df/dt de las dos ultimas es una via de disparo ADICIONAL, no un
requisito extra: la etapa 8 actua a los 4 s por umbral, o a los 400 ms si la
frecuencia ya bajo de 58 Hz cayendo a mas de 0,2 Hz/s. Dispara la que llegue
primero, que en un evento severo es siempre la de df/dt.

Uso:
    python agc_deslastre.py --perder-mw 1500
    python agc_deslastre.py --perder gen_ANTIOQUIA__500
    python agc_deslastre.py --perder-area Antioquia
    python agc_deslastre.py --perder-mw 2500 --modo comparar
    python agc_deslastre.py --perder-mw 2000 --sin-edac
"""

import argparse
import sys

import numpy as np
import pandas as pd

from stn.config import salida

F0 = 60.0

# Constante de inercia por tecnologia, en segundos sobre la base de la maquina.
# Valores tipicos: las hidraulicas rondan 3-4 s, las termicas de vapor 4-6 s, y
# la solar y la eolica no aportan inercia porque van tras un convertidor.
H_POR_TIPO = {
    'hidraulica': 3.5,
    'termica': 5.0,
    'solar': 0.0,
    'eolica': 0.0,
    'desconocido': 4.0,
}

# Las que van tras un convertidor: ni inercia (H = 0 arriba) ni regulacion
# primaria (ver el porque en Sistema.__init__). 'desconocido' NO entra: no
# sabemos lo que es y quitarle la reserva seria suponer lo peor sin dato.
TIPOS_CONVERTIDOR = ('solar', 'eolica')

# Contar como acopladas solo las unidades que hacen falta para cubrir el
# despacho de cada barra, en vez de la planta entera. Ver la funcion
# _capacidad_sincronizada() para el criterio, su procedencia y su efecto.
# Se deja conmutable porque la diferencia es grande y conviene poder medirla.
COMPROMISO_UNIDADES = True

ESTATISMO = 0.05        # 5%, el valor tipico de los reguladores de velocidad
AMORTIGUAMIENTO = 1.5   # % de variacion de la carga por cada % de frecuencia
T_GOBERNADOR = 8.0      # s, constante de tiempo equivalente de la primaria
RESERVA_PRIMARIA = 0.10  # fraccion de la capacidad disponible para primaria

# Banda muerta del regulador de velocidad. La primaria no responde a
# desviaciones menores que esta. CREG 023 de 2001 art. 4 (texto vigente por el
# art. 99 de la Res. CREG 101 113 de 2026) exige <= 30 mHz a las convencionales
# y permite 0-120 mHz configurables a eolicas y solares, inicialmente <= 30.
BANDA_MUERTA_HZ = 0.030

# DOS COSAS QUE SE PROBARON Y SE DESCARTARON. Se dejan escritas porque el
# motivo no es obvio y volver a intentarlas cuesta tiempo.
#
# 1. TOPE DE RAMPA EN MW/s con la velocidad de toma de carga de PARATEC
#    (`paratec_equipos.rampas_por_tecnologia()`, hidraulica 39,1 %/min y
#    termica 3,96 %/min, 73,3 MW/s agregados). Es la rampa del REDESPACHO
#    ECONOMICO, en minutos: CREG 009 de 2003 y el Acuerdo CNO 1816 la modelan
#    en MWh entre periodos horarios y solo para termicas. La respuesta
#    primaria vive en otra escala, 0-10 s segun CREG 023 art. 4. Aplicar una a
#    la otra es un error de escala temporal; daba un umbral de 6,60 % que hay
#    que descartar.
#
# 2. GOLPE DE ARIETE de la turbina hidraulica, P(s)/g(s) = (1-Tw s)/(1+0,5 Tw s).
#    La fisica es real ---al abrir el distribuidor la potencia primero baja---
#    pero el modelo asi queda INCOMPLETO y por tanto es falso: un regulador
#    hidraulico con solo estatismo permanente y Tw > 0 es inestable, y eso es
#    resultado de libro, no un fallo numerico. Medido aqui con Tw = 2 s: la
#    frecuencia oscilaba con amplitud CRECIENTE de 1,65 Hz y el umbral se
#    derrumbaba a un absurdo 2 %. Los reguladores reales lo evitan con
#    compensacion de estatismo transitorio (dashpot, Rt y Tr sintonizados a
#    partir de Tw y H).
#
#    No se implemento el dashpot por dos razones. Una, Tw no esta en PARATEC
#    ni en ningun dato que tengamos, y Rt y Tr saldrian de formulas de
#    sintonia: serian tres parametros de literatura encadenados para mover el
#    resultado central del trabajo. Dos, y mas de fondo, `T_GOBERNADOR` = 8 s
#    YA ES un modelo agregado de orden reducido que engloba regulador y
#    turbina; injertarle un modelo detallado de turbina encima duplica la
#    lentitud y mezcla ordenes de modelo de forma incoherente.
#
#    Si algun dia se quiere hacer bien, hay que sustituir el retardo de primer
#    orden por un gobernador con dashpot completo, no añadir Tw a lo que hay.

# Esquema EDAC del Acuerdo 2105 de 2026 del CNO. Tabla oficial completa.
# Ojo con las etapas 5-6 (ambas a 58,6 Hz) y 7-8 (ambas a 58,4 Hz): comparten
# umbral y solo se distinguen por el retardo. No es una errata.
EDAC_UMBRALES_HZ = [59.4, 59.2, 59.0, 58.8, 58.6, 58.6, 58.4, 58.4]
EDAC_FRACCION = 0.05
EDAC_RETARDOS_S = [0.2, 0.2, 0.4, 0.4, 0.6, 1.0, 2.0, 4.0]

# Funcion df/dt, solo en las etapas 7 y 8. Es una via de disparo ADICIONAL e
# independiente de la de umbral: si la frecuencia cae por debajo de `umbral`
# Y ademas lo hace mas rapido que `pendiente`, el escalon actua con un retardo
# muy corto en vez de esperar los 2 o 4 s de la via lenta. Dispara la que
# llegue primero. Sirve para adelantar los ultimos escalones cuando la caida
# es abrupta, que es justo cuando los 4 s de la etapa 8 llegarian tarde.
# Indice del escalon (base 0) -> (umbral Hz, pendiente Hz/s, retardo s)
EDAC_DFDT = {
    6: {'umbral': 58.0, 'pendiente': -0.3, 'retardo': 0.2},
    7: {'umbral': 58.0, 'pendiente': -0.2, 'retardo': 0.4},
}

# Tolerancias de las pruebas de conformidad que fija el mismo acuerdo.
EDAC_TOL_HZ = 0.03
EDAC_TOL_MS = 50.0

F_COLAPSO = 57.5        # por debajo de esto se considera apagon
DT = 0.01               # paso de integracion, s
T_FIN = 60.0            # horizonte de simulacion, s

# Control secundario (AGC). Hasta el 1-11-2026 la integracion llevaba SOLO
# primaria, de modo que la frecuencia se quedaba en el error de estatismo y no
# volvia nunca a 60 Hz. No era un fallo del integrador ---con primaria sola,
# ese error es el resultado correcto--- sino que faltaba el lazo que en la
# realidad lo cierra. El AGC del modulo existia, pero en `aplicar_evento`,
# como redespacho de regimen permanente para el flujo: nunca entraba en la
# traza de frecuencia.
#
# Se modela como integrador puro sobre el error de frecuencia,
#
#     d(dP_agc)/dt = -(1/T_AGC) * B * (f - f0),
#
# con `B` la caracteristica natural del sistema (primaria mas amortiguamiento,
# en MW/Hz). Elegido asi, el lazo despliega el desbalance residual en un
# tiempo del orden de T_AGC y la frecuencia vuelve a 60 Hz exactamente: en
# equilibrio el integrador solo se para con error nulo.
#
# T_AGC = 30 s es el orden de magnitud de la regulacion secundaria: CREG 023
# de 2001 art. 5 pide que la secundaria sustituya a la primaria en pocos
# minutos, y el ciclo del AGC de XM es de segundos. No es un dato medido, y
# por eso esta aqui con nombre y no empotrado en una formula.
T_AGC = 30.0            # s, constante de tiempo del lazo secundario
#
# EL RETARDO NO ES UN DETALLE: SIN EL, EL AGC MEJORA EL RESULTADO CENTRAL.
#
# Con el integrador arrancando en t=0, la secundaria ya ha desplegado unos
# 280 MW cuando se deciden los escalones del EDAC, y eso basta para cambiar
# la respuesta: en la punta el nadir subia de 58,495 a 58,578 Hz, el esquema
# pasaba de 6 escalones a 5 y el deslastre de 3.470,0 a 2.891,7 MW. Es decir,
# el modelo haria parecer al sistema mas robusto de lo que es, y justo en la
# magnitud que mide este trabajo.
#
# El reparto convencional ---y el que fija CREG 023 art. 4 y 5--- es que la
# primaria cubre los primeros 0-30 s y la secundaria la sustituye despues. El
# nadir ocurre a los ~2 s y el ultimo escalon del EDAC a los ~4 s, de modo que
# con 30 s de retardo la secundaria no toca ninguno de los dos: solo hace lo
# suyo, que es devolver la frecuencia a 60 Hz una vez pasado el transitorio.
T_AGC_RETARDO = 30.0    # s desde la perturbacion hasta que actua la secundaria


# ---------------------------------------------------------------------------
# Parametros agregados del sistema
# ---------------------------------------------------------------------------

def _tipo_y_capacidad_por_bus(network):
    """Tecnologia y capacidad instalada de cada bus generador.

    Se cruza el nombre de la subestacion con los reportes de generacion de
    PARATEC. Para los buses cuya inyeccion es un equivalente (potencia que
    sube por un transformador desde 110 kV) no se conoce la tecnologia y se
    marcan como 'desconocido'.
    """
    try:
        from stn.paratec_equipos import capacidad_plantas
        cap = capacidad_plantas()
        porsub = cap.groupby('sub').agg(
            mw=('mw', 'sum'), tipo=('tipo', lambda s: s.mode().iloc[0]))
    except Exception:
        porsub = pd.DataFrame(columns=['mw', 'tipo'])

    # Cuantos generadores del modelo cuelgan de cada subestacion. Hace falta
    # porque la capacidad de PARATEC viene por PLANTA, no por barra: San
    # Carlos tiene generador en __230 y en __500, y asignarle a cada uno los
    # 1240 MW de la central contaba 2480. Eso inflaba S_base, la reserva y
    # sobre todo la inercia, y un H alto de mas suaviza el nadir justo en los
    # escenarios severos que interesan. La capacidad se reparte entre las
    # barras en proporcion a lo que cada una despacha.
    despacho_sub = {}
    for g in network.generators.index:
        sub = network.generators.at[g, 'bus'].rsplit('__', 1)[0]
        despacho_sub[sub] = despacho_sub.get(sub, 0.0) + max(
            float(network.generators.at[g, 'p_set']), 0.0)

    # Parametros REALES por unidad (reportes "Detalle de unidad X" de PARATEC,
    # descarga del 20-09-2026): H, estatismo y reactivos declarados maquina a
    # maquina, mas el punto de conexion al STR/STN. Sustituyen a los valores
    # por tecnologia donde existan; donde no, se cae al valor por defecto.
    try:
        from stn.paratec_equipos import parametros_por_bus
        reales = parametros_por_bus(
            sorted({network.generators.at[g, 'bus']
                    for g in network.generators.index}))
    except Exception:                                    # noqa: BLE001
        reales = pd.DataFrame()

    # Generacion que entra por CONVERTIDOR (parques eolicos, secundario de
    # 0,69 kV). No aporta inercia sincrona: su H es cero, no 4,0. Se lee del
    # propio reporte de PowerFactory, no se supone. Ver
    # generar_mapeo_stn.generacion_por_convertidor().
    try:
        from stn.generar_mapeo_stn import convertidor_por_bus
        conv = convertidor_por_bus()
    except Exception:                                    # noqa: BLE001
        conv = pd.Series(dtype=float)

    # Cuantas BARRAS del modelo comparten el mismo grupo de unidades de
    # PARATEC. San Carlos tiene barra en 230 y en 500 y las tres barras de
    # Hidrosogamoso salen del mismo patio: sin prorratear, la capacidad y los
    # reactivos de esas unidades se contarian dos y tres veces. H y el
    # estatismo NO se prorratean, porque son intensivos: el H de una maquina
    # no depende de cuantas barras la representen.
    #
    # LA CLAVE LLEVA LA SUBESTACION, Y NO ES OPCIONAL. Hasta el 3-10-2026 la
    # clave era solo (tipo, n_unidades, s_mva), que es una FIRMA, no una
    # identidad. Y dos plantas distintas pueden tener la misma firma: MIEL I
    # (Caldas) y PORCE II (Antioquia) son las dos hidraulicas de 3 unidades y
    # 450 MVA, asi que el codigo las tomaba por una sola planta repartida en
    # dos barras y le daba 225 MVA a cada una. Despues el `max(p_nom, p)` las
    # rescataba hasta su propio despacho, lo que disimulaba el sintoma:
    #
    #     MIEL I     450 MVA declarados -> 261,3 (su despacho exacto)
    #     PORCE II   450 MVA declarados -> 318,0 (su despacho exacto)
    #
    # El efecto llega al compromiso de unidades por partida doble, porque
    # `n_maquinas` tambien se divide por `n_comp`: las dos plantas quedaban
    # con 2 unidades en vez de 3 y con s_sinc igual a su despacho, o sea con
    # reserva primaria CERO.
    #
    # Agrupar por subestacion es lo que el comentario de arriba describia
    # desde el principio; la firma era un sustituto que colisiona.
    comparten = {}
    if len(reales):
        for b in reales.index:
            clave = (b.rsplit('__', 1)[0],
                     reales.at[b, 'tipo'], reales.at[b, 'n_unidades'],
                     round(float(reales.at[b, 's_mva'] or 0.0), 3))
            comparten.setdefault(clave, []).append(b)

    # Despacho por barra, para prorratear la placa de una planta repartida en
    # PROPORCION A LO QUE ENTREGA CADA BARRA y no a partes iguales.
    #
    # El reparto igual solo acierta cuando las barras entregan lo mismo. En
    # Hidrosogamoso acierta ---tres barras de 250 MW cada una--- pero en San
    # Carlos no: la de 230 kV entrega el 83,3 % y la de 500 kV el 16,7 %. Con
    # el reparto igual, a la de 230 le tocaban 680 MVA, su despacho de 1.200,2
    # los superaba, el `max(p_nom, p)` la subia hasta 1.200,2 y la planta
    # acababa sumando 1.880,2 MVA frente a los 1.360,0 que declara PARATEC:
    # un 38 % de placa inventada.
    #
    # Prorrateando por despacho la suma vuelve a dar exactamente lo declarado.
    despacho_bus = network.generators.groupby('bus').p_set.sum()

    filas = []
    for g in network.generators.index:
        bus = network.generators.at[g, 'bus']
        p = float(network.generators.at[g, 'p_set'])
        sub = bus.rsplit('__', 1)[0]
        h_real = r_real = q_abs = q_gen = np.nan
        via = ''
        if len(reales) and bus in reales.index:
            clave = (bus.rsplit('__', 1)[0],
                     reales.at[bus, 'tipo'], reales.at[bus, 'n_unidades'],
                     round(float(reales.at[bus, 's_mva'] or 0.0), 3))
            hermanas = comparten.get(clave, [bus])
            n_comp = max(len(hermanas), 1)
            # cuota de la planta que le toca a esta barra
            tot = float(sum(float(despacho_bus.get(b2, 0.0)) for b2 in hermanas))
            cuota = (p / tot) if tot > 1e-9 else (1.0 / n_comp)
            tipo = reales.at[bus, 'tipo']
            via = str(reales.at[bus, 'via'])
            h_real = float(reales.at[bus, 'H'])
            r_real = float(reales.at[bus, 'estatismo_pct'])
            q_abs = float(reales.at[bus, 'q_abs_mvar']) * cuota
            q_gen = float(reales.at[bus, 'q_gen_mvar']) * cuota
        elif sub in porsub.index:
            tipo = porsub.at[sub, 'tipo']
        else:
            tipo = 'desconocido'

        # Si la mayor parte de lo que inyecta esa barra entra por convertidor,
        # la barra es eolica aunque PARATEC no tenga unidad registrada. Se
        # exige mayoria (>50 %) para no reclasificar una barra mixta donde el
        # convertidor sea marginal.
        mw_conv = float(conv.get(bus, 0.0))
        if mw_conv > 0 and p > 0 and mw_conv >= 0.5 * abs(p):
            tipo = 'eolica'
            h_real = r_real = np.nan      # el dato de PARATEC no aplica aqui
            via = 'convertidor'

        # Capacidad de la barra, por orden de calidad del dato:
        #
        #   1. POTENCIA APARENTE de los reportes de unidad de PARATEC. Es la
        #      mejor: es un dato real y ademas es la magnitud CORRECTA, porque
        #      S_base debe ser la suma de las potencias nominales aparentes de
        #      las maquinas acopladas, que es lo que entra en la ecuacion de
        #      oscilacion 2*H*S_base.
        #   2. Capacidad efectiva neta del reporte de PLANTA, prorrateada.
        #      Ojo: viene en MW y aqui se usa como si fuera MVA, de modo que
        #      subestima la placa en un factor cos(phi). Se conserva donde no
        #      hay dato de unidad para no cambiar mas de lo necesario.
        #   3. p_set/0,8, que no es un dato sino una suposicion.
        #
        # El paso 1 se añadio el 20-9-2026. Antes, 27 de las 45 maquinas
        # ---el 42 % de S_base--- usaban el paso 3, y para doce de ellas el
        # MVA real ya estaba disponible y difiere mucho: TEBSA figuraba con
        # 187,5 MVA frente a 810 reales, TERMOFLORES 125 frente a 444. El
        # efecto sobre la energia cinetica almacenada, que es lo que fija el
        # RoCoF, era del 12 %.
        s_real = np.nan
        if len(reales) and bus in reales.index:
            clave = (bus.rsplit('__', 1)[0],
                     reales.at[bus, 'tipo'], reales.at[bus, 'n_unidades'],
                     round(float(reales.at[bus, 's_mva'] or 0.0), 3))
            hermanas = comparten.get(clave, [bus])
            n_comp = max(len(hermanas), 1)
            tot = float(sum(float(despacho_bus.get(b2, 0.0)) for b2 in hermanas))
            s = float(reales.at[bus, 's_mva'])
            if s > 0:
                # por despacho, no a partes iguales: ver `despacho_bus`
                s_real = s * (p / tot if tot > 1e-9 else 1.0 / n_comp)

        if s_real == s_real:                     # hay MVA de unidad
            p_nom = s_real
        elif sub in porsub.index:
            total_sub = despacho_sub.get(sub, 0.0)
            cuota = (max(p, 0.0) / total_sub) if total_sub > 1e-9 else 1.0
            p_nom = float(porsub.at[sub, 'mw']) * cuota
        else:
            # sin dato de placa se supone que esta al 80% de su capacidad
            p_nom = p / 0.8 if p > 0 else 0.0
        # Cuantas MAQUINAS representa esta barra. PARATEC da las unidades por
        # planta; si varias barras comparten la planta, se reparten. Hace
        # falta para saber cuantas estan girando (ver COMPROMISO_UNIDADES).
        n_maq = np.nan
        if len(reales) and bus in reales.index:
            clave = (bus.rsplit('__', 1)[0],
                     reales.at[bus, 'tipo'], reales.at[bus, 'n_unidades'],
                     round(float(reales.at[bus, 's_mva'] or 0.0), 3))
            n_comp = max(len(comparten.get(clave, [bus])), 1)
            nu = reales.at[bus, 'n_unidades']
            if nu == nu and float(nu) > 0:
                n_maq = max(round(float(nu) / n_comp), 1)

        # `max(p_nom, p)` es una red de seguridad: ninguna maquina puede
        # figurar despachando por encima de su placa. Pero cuando se activa
        # esta TAPANDO una contradiccion entre el caso de PowerFactory y el
        # registro de PARATEC, asi que se deja constancia en vez de
        # silenciarla. Hoy la activan dos maquinas, y las dos son reales:
        #
        #     PAIPA        255,0 MW sobre 210,0 MVA declarados  (121,4 %)
        #     SAN CARLOS 1.440,2 MW sobre 1.360,0 MVA en total  (105,9 %)
        #
        # No se puede entregar mas MW que MVA de placa. O el caso despacha de
        # mas, o el registro esta desactualizado. Ver `placa_rescatada`.
        filas.append({'gen': g, 'bus': bus, 'p_set': p,
                      'tipo': tipo, 'p_nom': max(p_nom, p),
                      'placa_declarada': p_nom,
                      'n_maquinas': n_maq,
                      'H_real': h_real, 'estatismo_real_pct': r_real,
                      'q_abs_mvar': q_abs, 'q_gen_mvar': q_gen, 'via': via})
    return pd.DataFrame(filas).set_index('gen')


def _capacidad_sincronizada(gen):
    """Cuanta placa esta REALMENTE GIRANDO, no cuanta hay instalada.

    La inercia y la reserva primaria las dan las maquinas acopladas, no el
    parque. El modelo daba a cada barra su placa entera en cuanto despachaba
    un solo MW, y eso infla las dos cosas. Los casos mas claros del caso base:
    Guavio sincronizaba sus 5 grupos de 270 MVA para entregar 300 MW, cuando
    bastan 2; TEBSA los 5 de 162 MVA para entregar 150, cuando basta 1. Entre
    las dos aportaban 11.070 MVA*s, el 16 % de la energia cinetica del modelo,
    con la mayoria de sus maquinas paradas en la realidad. El sintoma agregado
    era tener el 97 % de toda la hidraulica de Colombia girando a la vez.

    Criterio: el MINIMO numero de unidades que cubre el despacho de la barra,
    suponiendolas iguales. Es exactamente el que usa XM para calcular la
    inercia del SIN ---"despacho del numero minimo de unidades de generacion
    para cubrir la demanda total", Castrillon Gutierrez et al., Direccion
    Planeacion de la Operacion, seccion 2.3--- asi que las dos cifras quedan
    homologables.

    Es una COTA INFERIOR de lo acoplado: el despacho real compromete alguna
    unidad de mas por reserva y por seguridad. Con ella el caso base pasa de
    69.519 a 52.786 MVA*s, que es lo que cabe esperar de una hora de punta
    (estamos al 96 % de la punta del SIN) contra el maximo anual que reporta
    el operador.

    Donde no se sabe cuantas unidades tiene la planta se devuelve la placa
    entera, que es el comportamiento anterior: no se inventa un reparto.
    """
    n = gen.get('n_maquinas')
    if n is None:
        return gen.p_nom.copy()
    n = n.astype(float)
    tam = gen.p_nom / n.where(n > 0)                 # MVA por maquina
    on = np.ceil((gen.p_set / tam).astype(float))    # las que hacen falta
    on = on.clip(lower=1.0)
    on = np.minimum(on, n)
    s = on * tam
    # sin dato de unidades, o si el reparto sale absurdo, se deja la placa
    return s.where(s.notna() & (s > 0), gen.p_nom).clip(upper=gen.p_nom)


def _despacho_resuelto(network, gen):
    """Generacion resuelta de las maquinas de `gen`, con respaldo en p_set.

    El flujo AC deja en `generators_t.p` lo que cada maquina entrega de
    verdad; `p_set` es solo la consigna, y en el slack las dos difieren. Si la
    red no esta resuelta se cae a la consigna, que es el comportamiento
    anterior.
    """
    if network is None:
        return float(gen.p_set.sum())
    din = getattr(network, 'generators_t', None)
    p = None if din is None else din.get('p')
    if p is None or not len(p):
        return float(gen.p_set.sum())
    res = p.iloc[0].reindex(gen.index)
    return float(res.fillna(gen.p_set).sum())


def _perdidas_de(network, buses=None):
    """Perdidas activas de la red, o de las ramas internas a `buses`.

    Suma p0+p1 de cada linea y transformador, que es la potencia que la rama
    disipa. Con `buses` solo cuenta las ramas con AMBOS extremos dentro, que
    son las de la isla; una rama con un extremo fuera ya no esta transportando
    nada si la isla esta separada.

    Devuelve 0,0 si la red no esta resuelta, que deja el comportamiento
    anterior en vez de reventar.
    """
    if network is None:
        return 0.0
    total = 0.0
    for comp in ('lines', 'transformers'):
        est = getattr(network, comp, None)
        din = getattr(network, comp + '_t', None)
        if est is None or din is None or not len(est):
            continue
        p0, p1 = din.get('p0'), din.get('p1')
        if p0 is None or p1 is None or not len(p0):
            continue
        perd = p0.iloc[0] + p1.iloc[0]
        if buses is not None:
            dentro = est.bus0.isin(buses) & est.bus1.isin(buses)
            perd = perd[dentro.reindex(perd.index).fillna(False)]
        total += float(perd.sum())
    return total


def parametros_dinamicos(destino, gen):
    """Calcula sobre `destino` los parametros de la ecuacion de oscilacion.

    `gen` es la tabla de generadores ---la del sistema entero o la de una
    isla--- y `destino` el objeto donde dejar los atributos: `s_sinc`,
    `S_base`, `H`, `R_eq`, `reserva`, `reserva_baja` y los informativos.

    UN SOLO SITIO DONDE SE CALCULA, y hay motivo. `cascada._SistemaIsla`
    replicaba estas formulas para restringirlas a una isla, y su docstring
    afirmaba usar "las MISMAS formulas y constantes". Dejo de ser cierto en
    cuanto `Sistema` fue incorporando mejoras que la copia no recibio:

      * `s_sinc` en vez de `p_nom`, o sea el compromiso de unidades
      * el H declarado por PARATEC en vez del valor por tecnologia
      * `R_eq` ponderado en vez del estatismo plano
      * la exclusion de los convertidores de la reserva primaria
      * la reserva a bajar, que ni existia

    Medido sobre el caso base, que es UNA sola isla ---o sea que no hacia
    falta que la red se partiera para que los dos modulos divergieran---:

                        Sistema     la copia
        S_base         13.947,7     17.698,0 MVA
        H                 3,785        3,356 s
        reserva           830,0      1.534,2 MW
        R_eq             0,0485       0,05 plano

    y para una perdida de 3.000 MW uno predecia 5 escalones y 2.891,7 MW y el
    otro 4 escalones y 2.313,3 MW. Dos respuestas distintas al mismo evento
    sobre la misma red, segun que modulo preguntara.
    """
    destino.gen = gen
    # Generacion RESUELTA donde la haya, no la consignada. La diferencia es el
    # aporte del slack ---2,9 MW en el caso base--- y es lo que de verdad esta
    # entregando el eje en el instante del que parte la simulacion. Con la
    # consigna quedaba un desbalance inicial residual de ese tamaño.
    destino.P_gen = _despacho_resuelto(getattr(destino, 'network', None), gen)

    # --- Perdidas de la red ---------------------------------------------
    #
    # LAS PERDIDAS SON CONSUMO Y TIENEN QUE ENTRAR EN LA ECUACION DE
    # OSCILACION. Sin esto el modelo arrancaba con un excedente espurio igual
    # a las perdidas del caso ---+120,5 MW en el caso base--- porque comparaba
    # la generacion despachada contra la carga a secas. Consecuencias medidas
    # antes del arreglo:
    #
    #   * sin ningun evento, la frecuencia SUBIA sola de 60,000 a 60,056 Hz
    #   * todo evento se subestimaba en 120,5 MW
    #   * una perdida de 100 MW daba RoCoF POSITIVO (+0,0117 Hz/s)
    #   * la contingencia de diseño de 300 MW daba -0,102 Hz/s en vez de
    #     -0,171: un 40 % de error
    #
    # El sesgo era siempre optimista y se diluia al crecer el evento, que es
    # justo el patron que lo hacia dificil de ver: a 3.000 MW el error es del
    # 4 % y a 100 MW del 120 %.
    #
    # Se calculan sumando p0+p1 de cada rama, que es su perdida, y NO como
    # generacion-menos-carga. El motivo es la cascada: una isla necesita las
    # perdidas de SUS ramas, no las del sistema entero, y la resta global no
    # se puede repartir. Filtrando por las barras de la isla, la misma formula
    # sirve para los dos casos.
    destino.P_perdidas = _perdidas_de(getattr(destino, 'network', None),
                                      getattr(destino, 'buses_isla', None))

    # Capacidad GIRANDO. Todo lo que sigue ---inercia, estatismo equivalente y
    # reserva--- se apoya en ella y no en la placa instalada, porque una
    # maquina parada no aporta ninguna de las tres. Ver
    # _capacidad_sincronizada() para el criterio y su procedencia.
    if not len(gen):
        destino.s_sinc = gen.get('p_nom', pd.Series(dtype=float))
        destino.S_instalada = destino.S_base = 0.0
        destino.frac_acoplada = 0.0
        destino.H = destino.R_eq = 0.0
        destino.h_por_gen = pd.Series(dtype=float)
        destino.n_H_real = destino.n_R_real = 0
        destino.mw_H_real = destino.reserva = destino.reserva_baja = 0.0
        destino.reserva_excluida = 0.0
        destino.sin_primaria = pd.Series(dtype=bool)
        destino.reserva_por_tipo = pd.Series(dtype=float)
        destino.frac_reserva_hidro = 0.0
        return destino

    if COMPROMISO_UNIDADES:
        gen['s_sinc'] = _capacidad_sincronizada(gen)
    else:
        gen['s_sinc'] = gen.p_nom
    destino.s_sinc = gen['s_sinc']
    destino.S_instalada = float(gen.p_nom.sum())
    destino.S_base = float(destino.s_sinc.sum())
    destino.frac_acoplada = destino.S_base / max(destino.S_instalada, 1e-9)

    # H real declarado por PARATEC donde exista; valor por tecnologia donde
    # no. Es la diferencia entre un parametro medido y uno supuesto, y el
    # analisis de frecuencia entero cuelga de el.
    h_tipo = gen.tipo.map(H_POR_TIPO).fillna(H_POR_TIPO['desconocido'])
    h = (gen['H_real'] if 'H_real' in gen.columns
         else pd.Series(np.nan, index=gen.index)).astype(float)
    destino.n_H_real = int(h.notna().sum())
    destino.mw_H_real = float(destino.s_sinc[h.notna()].sum())
    h = h.fillna(h_tipo)
    # H equivalente, ponderado por la capacidad GIRANDO de cada maquina
    destino.H = float((h * destino.s_sinc).sum() / max(destino.S_base, 1e-9))
    destino.h_por_gen = h

    # Estatismo equivalente, misma logica. Se pondera por capacidad porque la
    # aportacion de cada maquina a la primaria escala con su tamano:
    # R_eq = sum(S_i) / sum(S_i/R_i).
    r_real = (gen['estatismo_real_pct'] / 100.0
              if 'estatismo_real_pct' in gen.columns
              else pd.Series(np.nan, index=gen.index)).astype(float)
    # un estatismo de 0 significa que la maquina no regula: no es un dato
    # utilizable para R_eq, se cae al valor por defecto
    r_real = r_real.where(r_real > 1e-6)
    destino.n_R_real = int(r_real.notna().sum())
    r = r_real.fillna(ESTATISMO)
    denom = float((destino.s_sinc / r).sum())
    destino.R_eq = (float(destino.S_base / denom) if denom > 1e-9
                    else ESTATISMO)
    _reserva_de(destino, gen)
    return destino


def _reserva_de(destino, gen):
    """Reserva primaria a subir y a bajar. Separada solo por longitud."""
    # Reserva primaria disponible: margen hasta la placa, acotado.
    #
        # LA GENERACION POR CONVERTIDOR NO APORTA. Antes si lo hacia, y eran
        # 188,9 MW ---el 12,3 % de la reserva--- que no existian. Tres razones,
        # cada una suficiente por si sola:
        #
        #  1. Ese "margen" era un ARTEFACTO. Los cuatro parques eolicos salian
        #     exactamente al 80 % de su placa porque ninguno declara potencia
        #     aparente en PARATEC y se les aplica el respaldo p_nom = P/0,80.
        #     El 20 % restante no es holgura medida: es el factor de potencia
        #     supuesto, reciclado como si fuera reserva.
        #  2. Aunque el margen fuese real, no seria utilizable. En un parque la
        #     diferencia entre lo despachado y la placa es viento que no sopla
        #     o sol que no da; no hay valvula que abrir. La holgura girando
        #     exige estar recortado a proposito, y nada dice que lo esten.
        #  3. La norma los exime. CREG 060 de 2019 art. 12 paragrafo 2: las
        #     solares y eolicas "se excluyen transitoriamente de la
        #     obligatoriedad de la prestacion del servicio de respuesta
        #     primaria para eventos de SUBFRECUENCIA".
        #
    # Ademas era incoherente con el propio modelo, que ya les asigna H = 0
    # por ir tras un convertidor: no se puede negar la inercia y conceder la
    # primaria a la vez.
    #
    # Se mide sobre la capacidad GIRANDO, no sobre la instalada: una maquina
    # parada no regula. Es la otra cara del compromiso de unidades, y pesa:
    # Guavio pasa de 135 MW de reserva a 54.
    margen = (destino.s_sinc - gen.p_set).clip(lower=0.0)
    res = np.minimum(margen, destino.s_sinc * RESERVA_PRIMARIA)
    destino.sin_primaria = gen.tipo.isin(TIPOS_CONVERTIDOR)
    destino.reserva_excluida = float(res[destino.sin_primaria].sum())
    res = res.where(~destino.sin_primaria, 0.0)
    destino.reserva = float(res.sum())

    # POR GENERADOR, y no solo el total. El AGC de `aplicar_evento` y la banda
    # de `reparto_optimo` necesitan saber cuanto puede subir CADA maquina, y
    # las dos lo recalculaban por su cuenta sobre `p_nom` y sin excluir los
    # convertidores: desplegaban 1.534,2 MW donde este mismo objeto declaraba
    # 830,0, y un 12,3 % del despliegue iba a parques eolicos que no pueden
    # subir. Es el fallo que el comentario de `aplicar_evento` dice haber
    # corregido ---"las dos mitades de la herramienta se contradecian"---
    # reaparecido al introducir el compromiso de unidades, porque la copia no
    # se entero. Ahora hay una sola fuente.
    destino.reserva_por_gen = res

    # Reserva A BAJAR. La primaria regula en los dos sentidos: CREG 023 de
    # 2001 art. 4 obliga a poder "incrementar o decrementar" la generacion, y
    # solo exceptua el decremento a las maquinas que ya estan en su minimo
    # tecnico. Sin esto la frecuencia se disparaba a 63 Hz en cuanto el EDAC
    # botaba de mas, porque nada la devolvia. Se acota tambien por el propio
    # despacho, que ninguna maquina baje de cero.
    baja = np.minimum(destino.s_sinc * RESERVA_PRIMARIA,
                      gen.p_set.clip(lower=0.0))
    baja = baja.where(~destino.sin_primaria, 0.0)
    destino.reserva_baja = float(baja.sum())
    destino.reserva_baja_por_gen = baja

    # Reparto de la reserva por tecnologia. Solo informativo, pero dice de un
    # vistazo de que cuelga la primaria: aqui, casi toda del agua.
    destino.reserva_por_tipo = res.groupby(gen.tipo).sum()
    destino.frac_reserva_hidro = float(
        destino.reserva_por_tipo.get('hidraulica', 0.0)
        / max(destino.reserva, 1e-9))


class Sistema:
    """Parametros agregados que gobiernan la respuesta de frecuencia."""

    def __init__(self, network):
        self.network = network
        self.P_carga = float(network.loads.p_set.sum())
        parametros_dinamicos(self, _tipo_y_capacidad_por_bus(network))

    def resumen(self):
        print(f"  generacion despachada : {self.P_gen:9.1f} MW")
        print(f"  demanda               : {self.P_carga:9.1f} MW")
        print(f"  capacidad acoplada    : {self.S_base:9.1f} MVA")
        print(f"  inercia equivalente H : {self.H:9.2f} s"
              + (f"   ({self.n_H_real} de {len(self.gen)} maquinas con H "
                 f"declarado por PARATEC, {100*self.mw_H_real/max(self.S_base,1e-9):.0f}% "
                 f"de la placa)" if getattr(self, 'n_H_real', 0) else ""))
        print(f"  estatismo equivalente : {self.R_eq:9.4f}"
              + (f"   ({self.n_R_real} maquinas con dato real)"
                 if getattr(self, 'n_R_real', 0) else ""))
        print(f"  reserva primaria      : {self.reserva:9.1f} MW "
              f"({100*self.reserva/max(self.P_gen,1e-9):.1f}% del despacho)")
        mix = self.gen.groupby('tipo').p_nom.sum().sort_values(ascending=False)
        print("  mezcla por tecnologia (MW de placa):")
        for t, v in mix.items():
            g = self.gen[self.gen.tipo == t]
            hr = g['H_real'] if 'H_real' in g.columns else None
            if hr is not None and hr.notna().any():
                print(f"     {t:14s} {v:8.1f}   H={float((hr.fillna(H_POR_TIPO.get(t,4.0))*g.p_nom).sum()/max(g.p_nom.sum(),1e-9)):.2f} s "
                      f"({int(hr.notna().sum())}/{len(g)} reales)")
            else:
                print(f"     {t:14s} {v:8.1f}   H={H_POR_TIPO.get(t, 4.0):.1f} s (por tecnologia)")


# ---------------------------------------------------------------------------
# Dinamica de frecuencia
# ---------------------------------------------------------------------------

def simular_frecuencia(sis, deficit_mw=0.0, con_edac=True, umbrales=None,
                       retardos=None, fraccion=EDAC_FRACCION, eventos=None,
                       disparos=None, banda_muerta=None, con_agc=True,
                       t_fin=None):
    """Integra la ecuacion de oscilacion tras una perturbacion.

    Devuelve (t, f, deslastre_por_escalon, traza).

    `deficit_mw` es la forma simple: toda la perdida de generacion en t=0.

    `eventos` es la forma general: una lista de perturbaciones con INSTANTE
    propio, cada una un dict

        {'t': segundos desde el inicio,
         'dP_gen': MW de generacion que se pierde (positivo = se pierde),
         'dP_carga': MW de demanda que aparece (positivo = sube la demanda),
         'etiqueta': texto para el informe}

    Sin esto todo ocurria en t=0, y eso no es como se cae un sistema. En el
    apagon iberico del 28 de abril de 2025 el disparo fue local -un
    transformador en Granada- y a partir de ahi se desconectaron en CASCADA
    parques fotovoltaicos y eolicos en Badajoz, Segovia, Huelva, Sevilla y
    Caceres: 15.000 MW en cinco segundos, no de golpe. La diferencia importa
    porque el EDAC actua con retardos de 0,2 a 4 s, asi que el reparto en el
    tiempo decide que escalones llegan a disparar.

    La generacion detras del contador entra por `dP_carga`, no por `dP_gen`:
    el operador no la ve desaparecer, la ve aparecer como mas demanda.
    """
    umbrales = EDAC_UMBRALES_HZ if umbrales is None else umbrales
    retardos = EDAC_RETARDOS_S if retardos is None else retardos

    # Disparos por SUBFRECUENCIA: generacion que se desconecta sola cuando la
    # frecuencia cruza su ajuste de proteccion. Es la cascada endogena, la que
    # hizo caer a Iberia: no hace falta programarla, emerge del propio
    # transitorio. Cada entrada es {'f': umbral Hz, 'mw': MW, 'retardo': s,
    # 'etiqueta': texto}; la potencia entra como MAS DEMANDA porque en este
    # modelo toda la solar esta empotrada por debajo de 200 kV.
    disp = [dict(d) for d in (disparos or [])]
    for d in disp:
        d.setdefault('retardo', 0.1)
        d['disparado'] = False
        d['t_cruce'] = None
        d['t_disparo'] = None

    ev = sorted(eventos or [], key=lambda e: float(e.get('t', 0.0)))
    if deficit_mw:
        ev.insert(0, {'t': 0.0, 'dP_gen': float(deficit_mw), 'dP_carga': 0.0,
                      'etiqueta': 'perdida de generacion'})
    for e in ev:
        e.setdefault('dP_gen', 0.0)
        e.setdefault('dP_carga', 0.0)
        e['aplicado'] = False

    # El horizonte es parametrizable porque la secundaria vive en otra escala
    # que la primaria: en 60 s el nadir y el EDAC ya han ocurrido enteros,
    # pero el AGC apenas ha empezado a devolver la frecuencia. Para MIRAR la
    # restitucion hace falta mas ventana; para medir nadir y escalones, no, y
    # alargarla a todo el mundo solo costaria tiempo de calculo.
    n = int((T_FIN if t_fin is None else float(t_fin)) / DT)
    t = np.arange(n) * DT
    f = np.zeros(n)
    f[0] = F0

    P_gen = sis.P_gen                  # se va reduciendo segun ocurren eventos
    # DOS MAGNITUDES DISTINTAS, Y CONFUNDIRLAS CUESTA.
    #
    # `P_carga` es lo que el sistema CONSUME: demanda mas perdidas de red.
    # Es lo que entra en la ecuacion de oscilacion, y sumar las perdidas es lo
    # que hace que el sistema arranque en equilibrio (ver `_perdidas_de`).
    #
    # `P_demanda` es lo que se puede DESLASTRAR. El EDAC bota carga de
    # clientes; las perdidas de la red no se pueden botar. El Acuerdo 2105
    # define cada escalon como el 5 % de la demanda, no del consumo.
    #
    # Al meter las perdidas en `P_carga` el 3-10-2026, los escalones pasaron a
    # dimensionarse sobre 11.690,0 en vez de 11.566,6 MW: 584,50 MW por
    # escalon en vez de 578,33, un 1,07 % de mas, y el esquema completo cubria
    # el 40,43 % de la demanda en vez del 40,00 % que define el acuerdo. El
    # invariante que lo vigila toleraba hasta el 40,5 % y dejo pasar el error
    # por 7 centesimas de punto.
    P_perdidas = float(getattr(sis, 'P_perdidas', 0.0))
    P_demanda = sis.P_carga
    P_carga = P_demanda + P_perdidas
    reserva = sis.reserva
    reserva_baja = float(getattr(sis, 'reserva_baja', 0.0))

    # Regulacion primaria. `g_*` son las consignas de los reguladores (lo que
    # pide el gobernador) y `dP_prim` la potencia mecanica que de verdad
    # llega al eje. Sin golpe de ariete las dos coinciden.
    banda = BANDA_MUERTA_HZ if banda_muerta is None else banda_muerta
    # factor exacto del retardo de primer orden en un paso, en vez de Euler
    a_gob = float(np.exp(-DT / T_GOBERNADOR))
    dP_prim = 0.0      # aporte acumulado de la primaria
    dP_agc = 0.0       # aporte acumulado del control secundario
    # La secundaria se cuenta desde la ULTIMA perturbacion, no desde t=0: en
    # una cascada cada evento reinicia el reparto entre primaria y secundaria.
    t_agc_desde = float(max([0.0] + [float(e['t']) for e in ev])) + T_AGC_RETARDO

    # los de t <= 0 entran antes de arrancar la integracion
    for e in ev:
        if float(e['t']) <= 0.0:
            P_gen -= float(e['dP_gen'])
            # el BTM que se cae es demanda nueva, y es deslastrable: sube las dos
            P_demanda += float(e['dP_carga'])
            P_carga += float(e['dP_carga'])
            e['aplicado'] = True

    escalones = [{'umbral': u, 'retardo': r, 'fraccion': fraccion,
                  'dfdt': dict(EDAC_DFDT[i]) if i in EDAC_DFDT else None,
                  'disparado': False, 't_cruce': None, 't_cruce_dfdt': None,
                  't_disparo': None, 'via': None, 'mw': 0.0}
                 for i, (u, r) in enumerate(zip(umbrales, retardos))]
    carga_deslastrada = 0.0

    for k in range(1, n):
        # eventos que ocurren en este paso
        for e in ev:
            if not e['aplicado'] and float(e['t']) <= t[k]:
                P_gen -= float(e['dP_gen'])
                P_demanda += float(e['dP_carga'])
                P_carga += float(e['dP_carga'])
                e['aplicado'] = True
                e['t_real'] = float(t[k])

        df_pu = (f[k-1] - F0) / F0

        # Regulacion primaria: objetivo por estatismo, acotado por la reserva.
        # `sis.R_eq` es el estatismo equivalente ponderado por capacidad, con
        # los valores que PARATEC declara maquina a maquina donde existen; si
        # el Sistema no lo trae (objeto antiguo), se cae al valor plano.
        r_eq = getattr(sis, 'R_eq', ESTATISMO) or ESTATISMO
        # Banda muerta: el regulador ignora la desviacion hasta `banda`, y a
        # partir de ahi responde al EXCESO sobre ella, no a la desviacion
        # entera. Restarla asi (y no simplemente activar/desactivar) es lo que
        # mantiene la respuesta continua en el borde. Simetrica en los dos
        # sentidos.
        bm = banda / F0
        df_util = (df_pu + bm) if df_pu < -bm else (
                  (df_pu - bm) if df_pu > bm else 0.0)
        # Objetivo de la primaria, acotado por la reserva disponible en cada
        # sentido: hacia arriba `reserva`, hacia abajo `reserva_baja`.
        objetivo = -df_util / r_eq * sis.P_gen
        objetivo = max(-reserva_baja, min(objetivo, reserva))

        # La primaria persigue el objetivo con el retardo del gobernador,
        # usando el factor exponencial exacto del paso en vez de Euler.
        dP_prim = objetivo + (dP_prim - objetivo) * a_gob

        # Control secundario: integrador puro sobre el error de frecuencia.
        # Es lo que lleva la frecuencia de vuelta a 60 Hz; la primaria sola la
        # deja en el error de estatismo, que es su comportamiento correcto.
        # `B` es la caracteristica natural en MW/Hz: lo que aportan primaria y
        # amortiguamiento juntos por cada hercio de desviacion.
        if con_agc and t[k] >= t_agc_desde:
            B = (sis.P_gen / r_eq + AMORTIGUAMIENTO * P_carga) / F0
            dP_agc -= (DT / T_AGC) * B * (f[k-1] - F0)

        # La reserva es UNA, y la comparten los dos lazos: lo que ya despliega
        # la secundaria no esta disponible para la primaria. Se acota la suma,
        # no cada una por su lado, que es lo que permitiria desplegar el doble
        # de lo que el parque tiene.
        aporte = dP_prim + dP_agc
        if aporte > reserva:
            dP_agc = reserva - dP_prim
        elif aporte < -reserva_baja:
            dP_agc = -reserva_baja - dP_prim
        aporte = dP_prim + dP_agc

        # La carga baja sola cuando cae la frecuencia
        carga_efectiva = (P_carga - carga_deslastrada) * (
            1.0 + AMORTIGUAMIENTO * df_pu)

        desbalance = (P_gen + aporte) - carga_efectiva
        # 2H df/dt = dP, todo en por unidad sobre S_base
        df_dt = desbalance / (2.0 * sis.H * max(sis.S_base, 1e-9)) * F0
        f[k] = f[k-1] + df_dt * DT

        # generacion que se desconecta sola al cruzar su ajuste
        for d in disp:
            if d['disparado']:
                continue
            if f[k] <= float(d['f']):
                if d['t_cruce'] is None:
                    d['t_cruce'] = t[k]
                elif t[k] - d['t_cruce'] >= float(d['retardo']):
                    # generacion embebida que se autodispara: desde el STN es
                    # demanda nueva, y es deslastrable igual que el resto
                    P_demanda += float(d['mw'])
                    P_carga += float(d['mw'])
                    d['disparado'] = True
                    d['t_disparo'] = float(t[k])
            else:
                d['t_cruce'] = None

        if con_edac:
            for e in escalones:
                if e['disparado']:
                    continue
                via = None

                # Via de umbral: f bajo el ajuste durante el retardo completo
                if f[k] <= e['umbral']:
                    if e['t_cruce'] is None:
                        e['t_cruce'] = t[k]
                    elif t[k] - e['t_cruce'] >= e['retardo']:
                        via = 'umbral'
                else:
                    e['t_cruce'] = None

                # Via df/dt: exige umbral Y pendiente, con su propio retardo
                d = e['dfdt']
                if via is None and d is not None:
                    if f[k] <= d['umbral'] and df_dt <= d['pendiente']:
                        if e['t_cruce_dfdt'] is None:
                            e['t_cruce_dfdt'] = t[k]
                        elif t[k] - e['t_cruce_dfdt'] >= d['retardo']:
                            via = 'df/dt'
                    else:
                        e['t_cruce_dfdt'] = None

                if via is not None:
                    e['disparado'] = True
                    e['t_disparo'] = t[k]
                    e['via'] = via
                    e['mw'] = P_demanda * e['fraccion']
                    carga_deslastrada += e['mw']

        if f[k] < F_COLAPSO:
            f[k:] = f[k]
            t = t[:k+1]
            f = f[:k+1]
            break

    colapso = bool(f.min() < F_COLAPSO)

    # EL NADIR DE UN COLAPSO NO ES UN DATO, y ahora el objeto lo dice.
    #
    # El lazo se detiene en cuanto f baja de F_COLAPSO, asi que `f.min()` es
    # el punto donde el integrador se rindio, no un minimo. Hasta ahora se
    # entregaba como `nadir` y el aviso solo se imprimia en la salida de ESTE
    # modulo: los otros seis consumidores ---comparar_casos, deslastre_btm,
    # barrido, el menu...--- recibian el numero pelado y lo imprimian o lo
    # metian en un CSV junto a nadires de verdad.
    #
    # Lo que se consigue con eso es lo contrario de informar. Medido sobre la
    # punta, el deslastre informado NO ES MONOTONO porque los escalones que
    # habrian disparado despues del corte no llegan a disparar:
    #
    #   dP = 4.800 MW   no colapsa   8 escalones   4.626,6 MW   nadir 57,673
    #   dP = 5.200 MW   COLAPSA      7 escalones   4.048,3 MW   corte 57,498
    #   dP = 7.000 MW   COLAPSA      4 escalones   2.313,3 MW   corte 57,481
    #
    # Un evento mayor deslastra MENOS y parece terminar mas arriba. Cualquier
    # comparacion o promedio que cruce el umbral queda corrompido.
    #
    # Por eso `nadir` sale NaN cuando hay colapso: un NaN se propaga y rompe
    # la media a gritos, mientras que 57,498 se cuela y sesga en silencio. El
    # valor del corte sigue disponible como `nadir_corte`, que es un nombre
    # que no invita a citarlo como margen.
    traza = {
        'nadir': float('nan') if colapso else float(f.min()),
        'nadir_corte': float(f.min()),
        'nadir_valido': not colapso,
        't_nadir': float(t[int(np.argmin(f))]),
        'f_final': float(f[-1]),
        'rocof': float((f[1] - f[0]) / DT),
        'colapso': colapso,
        'deslastre_total': float(carga_deslastrada),
        # El deslastre de un colapso es real ---esos escalones dispararon---
        # pero esta TRUNCADO: faltan los que habrian disparado despues. No
        # se puede comparar con el de un caso que no colapsa.
        'deslastre_truncado': colapso,
        'aporte_primaria': float(dP_prim),
        'aporte_agc': float(dP_agc),
        'eventos': ev,
        'disparos': disp,
        'mw_autodisparo': float(sum(d['mw'] for d in disp if d['disparado'])),
        'perturbacion_total': float(sum(e['dP_gen'] + e['dP_carga'] for e in ev)),
        'rocof_max': float(np.min(np.diff(f) / DT)) if len(f) > 1 else 0.0,
    }
    return t, f, escalones, traza


# ---------------------------------------------------------------------------
# Reparto del deslastre
# ---------------------------------------------------------------------------

def reparto_proporcional(network, mw_total):
    """Como lo hace el EDAC real: el mismo porcentaje en toda la demanda."""
    carga = network.loads.groupby('bus').p_set.sum()
    carga = carga[carga > 0]
    total = carga.sum()
    if total <= 0 or mw_total <= 0:
        return pd.Series(dtype=float)
    return (carga / total * min(mw_total, total)).round(3)


def reparto_optimo(network, mw_total, generadores_fuera=(), deficit_mw=0.0,
                   sis=None, verbose=False):
    """Elige en que barras deslastrar resolviendo un OPF.

    El planteamiento tiene que reflejar el evento completo, no solo el corte:
    primero se retira del modelo la generacion perdida, y solo entonces el
    deslastre tiene sentido como lo que es, la potencia que sustituye a la que
    falta. Al no hacerlo, los cortes se sumaban a una generacion intacta,
    sobraba potencia y el problema salia infactible.

    El deslastre se modela como un generador ficticio en cada barra con carga,
    con coste, y se minimiza el coste sujeto a los limites de las lineas. Lo
    que pide la dinamica de frecuencia entra como COTA INFERIOR, no como
    igualdad: el OPF decide donde recortar y puede recortar mas si la red no
    alcanza a llevar la potencia hasta donde esta la demanda.

    Si el solver no encuentra solucion se cae al reparto proporcional, que es
    el comportamiento del esquema regulado.
    """
    if mw_total <= 0:
        return pd.Series(dtype=float), 'nada que deslastrar'
    try:
        m = network.copy()

        # 1. retirar del modelo la generacion que se perdio
        if generadores_fuera:
            for g in generadores_fuera:
                if g in m.generators.index:
                    m.remove('Generator', g)
        elif deficit_mw > 0:
            # perdida generica: se reduce a todos en proporcion
            total = m.generators.p_set.sum()
            if total > 0:
                factor = max(0.0, 1.0 - deficit_mw / total)
                m.generators['p_set'] = m.generators.p_set * factor

        # 2. margen de maniobra de los generadores que quedan (reserva)
        #
        # La banda tiene que ser simetrica. Al principio el piso era la salida
        # actual, de modo que solo podian SUBIR, y eso rompia el problema en
        # cuanto el EDAC se pasaba: el esquema corta en escalones fijos del 5%,
        # asi que casi nunca corta exactamente lo que se perdio. Perdiendo 3000
        # MW el EDAC bota 3064; con los generadores pinchados en su salida esos
        # 64 MW de sobra no los podia absorber nadie y el OPF salia infactible,
        # cayendose al reparto proporcional sin motivo real. En el sistema de
        # verdad ese excedente se lo comen los reguladores bajando carga, que es
        # justo lo que se ve en la simulacion cuando la frecuencia se pasa de
        # 60 Hz. La reserva se usa en los dos sentidos.
        # La banda sale de `sis.reserva_por_gen` y `sis.reserva_baja_por_gen`,
        # que son las mismas series con las que `Sistema` calcula sus totales.
        # Antes se recalculaba aqui como `p_nom * RESERVA_PRIMARIA`, sin
        # excluir los convertidores y sobre la placa instalada en vez de la
        # girando: el OPF daba margen de maniobra a maquinas que no lo tienen.
        info = sis.gen if sis is not None else None
        sube = getattr(sis, 'reserva_por_gen', None) if sis is not None else None
        baja = getattr(sis, 'reserva_baja_por_gen', None) if sis is not None else None
        for g in m.generators.index:
            p = float(m.generators.at[g, 'p_set'])
            techo, piso = p, p
            if info is not None and g in info.index:
                b_sube = (float(sube.get(g, 0.0)) if sube is not None
                          else float(info.at[g, 'p_nom']) * RESERVA_PRIMARIA)
                b_baja = (float(baja.get(g, 0.0)) if baja is not None
                          else float(info.at[g, 'p_nom']) * RESERVA_PRIMARIA)
                tope = float(info.at[g, 's_sinc']) if 's_sinc' in info.columns \
                    else float(info.at[g, 'p_nom'])
                techo = max(p, min(tope, p + b_sube))
                piso = max(0.0, p - b_baja)
            m.generators.loc[g, 'p_nom'] = max(techo, 1e-3)
            m.generators.loc[g, 'p_min_pu'] = min(piso / max(techo, 1e-3), 1.0)
            m.generators.loc[g, 'p_max_pu'] = 1.0
            m.generators.loc[g, 'marginal_cost'] = 1.0
            m.generators.loc[g, 'control'] = 'PQ'

        # Y hay que BORRAR p_set, o nada de lo anterior sirve. PyPSA le impone
        # p == p_set a todo generador que lo tenga definido (la restriccion
        # 'Generator-p_set'), y el constructor lo fija en todos para poder
        # correr el flujo AC. Con eso el despacho quedaba congelado y el OPF no
        # optimizaba nada: solo repartia entre barras un corte cuyo total ya
        # venia impuesto por el balance. Se notaba en que el resultado siempre
        # daba exactamente demanda - generacion, y en que el aviso de "la red
        # obliga a X MW mas" culpaba a la red de lo que en realidad era el
        # despacho congelado. Con p_set en NaN mandan p_min_pu y p_max_pu.
        m.generators['p_set'] = np.nan

        # 3. tras una contingencia las lineas pueden llegar a su limite de
        #    emergencia, no al nominal. Sin esto el OPF sale infactible: el
        #    corredor de Hidrosogamoso ya va al 90% de su nominal en el caso
        #    base y cualquier redespacho lo pasaria.
        if 's_eme' in m.lines.columns:
            ratio = (m.lines.s_eme / m.lines.s_nom.replace(0, np.nan))
            m.lines['s_max_pu'] = ratio.fillna(1.0).clip(lower=1.0)

        # 4. el deslastre, como generador ficticio en cada barra con carga
        carga = m.loads.groupby('bus').p_set.sum()
        carga = carga[carga > 0]
        for bus, p in carga.items():
            m.add("Generator", f"corte_{bus}", bus=bus, p_nom=float(p),
                  marginal_cost=100.0, control='PQ')

        # No se anade generador de holgura. Se probo y era contraproducente:
        # el solver lo usaba para cubrir los 485 MW que faltaban dejando a los
        # generadores en su minimo, pese a costar 1e4 frente a 1,0. En vez de
        # asegurar la factibilidad, falseaba el reparto. Si el problema resulta
        # infactible se cae al reparto proporcional, que es mas honesto.
        cortes = [g for g in m.generators.index if g.startswith('corte_')]
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            m.optimize.create_model()
            # en linopy la dimension de la variable se llama 'name'
            var = m.model.variables['Generator-p'].sel(name=cortes)
            # El deslastre que pide la frecuencia es un MINIMO, no una
            # igualdad. Forzarlo como igualdad hacia el problema infactible, y
            # con razon: el modelo de frecuencia es de una sola barra y no ve
            # la red, mientras que el OPF si. Si la red no puede transportar lo
            # que falta hasta donde esta la demanda, hay que deslastrar mas.
            m.model.add_constraints(var.sum() >= float(mw_total),
                                    name='deslastre_minimo')
            m.optimize.solve_model(solver_name='highs', log_to_console=False)

        p = m.generators_t.p.iloc[0]
        if not all(g in p.index for g in cortes):
            raise RuntimeError("el solver no devolvio solucion")
        res = pd.Series({g.replace('corte_', ''): float(p[g])
                         for g in cortes if float(p[g]) > 1e-6})
        if res.empty:
            raise RuntimeError("el OPF no asigno deslastre")
        reales = [g for g in m.generators.index if not g.startswith('corte_')]
        extra = res.sum() - mw_total
        nota = f'OPF, AGC {p[reales].sum():.0f} MW'
        if extra > 1.0:
            nota += (f'; la red obliga a {extra:.0f} MW mas de los '
                     f'{mw_total:.0f} que pide la frecuencia')
        return res.round(3), nota
    except Exception as e:                          # noqa: BLE001
        if verbose:
            print(f"  aviso: el OPF no resolvio ({type(e).__name__}: "
                  f"{str(e)[:70]}); se reparte proporcionalmente")
        return (reparto_proporcional(network, mw_total),
                f'proporcional ({type(e).__name__})')


# ---------------------------------------------------------------------------
# Estado post-evento: AGC + flujo AC
# ---------------------------------------------------------------------------

def aplicar_evento(network, generadores_fuera, deslastre, sis, deficit_mw=0.0,
                   carga_extra=None, verbose=False):
    """Construye la red post-evento: quita generacion, recorta carga y reparte
    el desbalance restante entre los generadores con reserva (AGC).

    `deficit_mw` es la perdida TOTAL de generacion del evento. Lo que no quede
    cubierto por los generadores de `generadores_fuera` se retira bajando a
    prorrata la generacion que queda. Sin esto, un evento definido solo por su
    magnitud (--perder-mw, o la caida de BTM) recortaba la carga pero dejaba la
    generacion intacta, y el slack absorbia en silencio la diferencia: para una
    perdida de 3000 MW el slack, una hidraulica de 1250 MW de placa, salia con
    p = -1957 MW. El flujo convergia y el informe de sobrecargas y tensiones se
    publicaba como valido, cuando describia una red que no existe.

    `carga_extra` es una Serie de MW por barra que se SUMA a la demanda. Es la
    forma correcta de meter una caida de generacion detras del contador: el
    operador no la ve desaparecer, la ve aparecer como mas carga, y aparece
    justo en las barras donde estaba el BTM.
    """
    from stn.power_flow import designar_slack, run_power_flow

    n = network.copy()
    retirado = 0.0
    for g in generadores_fuera:
        if g in n.generators.index:
            retirado += float(n.generators.at[g, 'p_set'])
            n.remove('Generator', g)

    # el resto del deficit, a prorrata sobre lo que sigue acoplado
    residual = float(deficit_mw) - retirado
    total_gen = n.generators.p_set.sum()
    if residual > 1e-6 and total_gen > 0:
        n.generators['p_set'] = n.generators.p_set * max(
            0.0, 1.0 - residual / total_gen)

    # generacion detras del contador que desaparece: se ve como mas demanda
    if carga_extra is not None:
        for bus, mw in carga_extra.items():
            if mw > 1e-6 and bus in n.buses.index:
                n.add('Load', f'btm_perdido_{bus}', bus=bus,
                      p_set=float(mw), q_set=0.0)

    # deslastre por barra
    for bus, mw in deslastre.items():
        cargas = n.loads[n.loads.bus == bus]
        total = cargas.p_set.sum()
        if total <= 0:
            continue
        factor = max(0.0, 1.0 - mw / total)
        for l in cargas.index:
            n.loads.loc[l, 'p_set'] *= factor
            n.loads.loc[l, 'q_set'] *= factor

    # AGC: el desbalance que queda se reparte entre los generadores con margen
    P_gen = n.generators.p_set.sum()
    P_carga = n.loads.p_set.sum()
    desbalance = P_carga - P_gen
    info = sis.gen.reindex(n.generators.index).dropna(subset=['p_nom'])
    # El margen es el hueco hasta la placa PERO acotado por la reserva que la
    # maquina tiene asignada, igual que hacen Sistema.reserva y la banda de
    # reparto_optimo. Sin el tope, este AGC desplegaba 3.849 MW donde el mismo
    # objeto declaraba 1.065: la dinamica de frecuencia imprimia "COLAPSO" y
    # acto seguido el estado AC daba la demanda servida al 100%, porque el
    # reparto habia repuesto una reserva que no existe. Las dos mitades de la
    # herramienta se contradecian.
    # El hueco se mide contra el despacho PREVIO al evento (info.p_set), no
    # contra el de ahora. Con --perder-mw la perdida se reparte a prorrata
    # entre todos los generadores, asi que todos quedan replegados y aparece
    # un hueco que en la realidad no existe: la reserva de una maquina no
    # crece porque OTRA se haya disparado. Esa medida es justo la que hace
    # `parametros_dinamicos`, asi que basta con reutilizarla.
    #
    # DESDE EL 21-9-2026 SE CONSUME `sis.reserva_por_gen`, que es la misma
    # serie con la que `Sistema` calcula su `reserva`. Recalcularla aqui habia
    # vuelto a desincronizarlas: esta copia seguia usando `p_nom` y no excluia
    # los convertidores, de modo que el tope salia 1.534,2 MW frente a los
    # 830,0 declarados, y el 12,3 % del despliegue iba a parques eolicos que
    # no pueden subir.
    margen = sis.reserva_por_gen.reindex(info.index).fillna(0.0)
    if desbalance > 0 and margen.sum() > 0:
        reparto = margen / margen.sum() * min(desbalance, margen.sum())
        for g, dp in reparto.items():
            n.generators.loc[g, 'p_set'] += float(dp)
        if verbose:
            print(f"  AGC reparte {reparto.sum():.1f} MW entre "
                  f"{int((reparto > 0.01).sum())} generadores")

    designar_slack(n, verbose=False)
    ok, _ = run_power_flow(n, verbose=False)
    return n, ok


def estado_red(n, ok):
    if not ok:
        return {'convergio': False}
    from stn import contingencias as C
    est = C._estado(n, emergencia=True)
    return {
        'convergio': True,
        'v_min': round(est['v_min'], 4),
        'v_max': round(est['v_max'], 4),
        'n_v_fuera': len(est['v_bajas']) + len(est['v_altas']),
        'carga_max_pct': round(est['carga_max_pct'], 1),
        'n_sobrecargas': len(est['sobrecargados']),
        'critico': (est['carga_pct'].idxmax() if len(est['carga_pct']) else ''),
        'demanda_MW': round(float(n.loads_t.p.iloc[0].sum()), 1),
    }


# ---------------------------------------------------------------------------
# Escenario completo
# ---------------------------------------------------------------------------

def escenario(base, sis, generadores_fuera, deficit_mw, modo='edac',
              con_edac=True, verbose=True):
    """Simula el evento completo y devuelve el informe."""
    t, f, escalones, traza = simular_frecuencia(sis, deficit_mw, con_edac)

    if verbose:
        print(f"\n--- Dinamica de frecuencia "
              f"({'con EDAC' if con_edac else 'SIN deslastre'}) ---")
        print(f"  deficit               : {deficit_mw:9.1f} MW "
              f"({100*deficit_mw/max(sis.P_gen,1e-9):.1f}% del despacho)")
        print(f"  RoCoF inicial         : {traza['rocof']:9.3f} Hz/s")
        if traza['colapso']:
            # EL NADIR DE UN COLAPSO NO ES UN DATO. La integracion se detiene
            # en cuanto f baja de F_COLAPSO, asi que el minimo informado es el
            # punto donde el simulador se rindio, no donde la frecuencia se
            # habria estabilizado. Perder 20.000 MW ---mas que todo el
            # despacho--- informa 57,4573 Hz, solo 43 milesimas bajo el piso.
            # Imprimirlo como "frecuencia minima" invita a citarlo como si
            # fuera un margen, que es exactamente el error que hay que evitar.
            print(f"  COLAPSO: la frecuencia baja de {F_COLAPSO} Hz a los "
                  f"{traza['t_nadir']:.2f} s y la simulacion se detiene ahi")
            print(f"  [!] el minimo real NO se conoce: {traza['nadir_corte']:.3f} Hz "
                  f"es donde corta el integrador, no un nadir. No citarlo")
            print(f"  [!] el deslastre de {traza['deslastre_total']:.1f} MW esta "
                  f"TRUNCADO: faltan los escalones posteriores al corte")
        else:
            print(f"  frecuencia minima     : {traza['nadir']:9.3f} Hz "
                  f"a los {traza['t_nadir']:.2f} s")
            print(f"  frecuencia final      : {traza['f_final']:9.3f} Hz")
        print(f"  aporte de la primaria : {traza['aporte_primaria']:9.1f} MW")
        disparados = [e for e in escalones if e['disparado']]
        if disparados:
            print(f"  escalones EDAC disparados: {len(disparados)} de "
                  f"{len(escalones)}  ->  {traza['deslastre_total']:.1f} MW")
            # se numera por la etapa real del esquema, no por orden de disparo:
            # las etapas 5-6 y 7-8 comparten umbral y se confundirian
            for i, e in enumerate(escalones, 1):
                if not e['disparado']:
                    continue
                via = '' if e['via'] == 'umbral' else f"  [via {e['via']}]"
                print(f"     etapa {i}: {e['umbral']:.1f} Hz  "
                      f"t={e['t_disparo']:.2f} s  {e['mw']:.1f} MW{via}")
        elif con_edac:
            print("  el EDAC no llego a actuar")

    mw = traza['deslastre_total']
    if modo == 'optimo':
        deslastre, origen = reparto_optimo(
            base, mw, generadores_fuera, deficit_mw, sis, verbose)
    else:
        deslastre, origen = reparto_proporcional(base, mw), 'proporcional'

    if verbose and len(deslastre):
        print(f"\n--- Reparto del deslastre ({origen}) ---")
        print(f"  {len(deslastre)} barras, {deslastre.sum():.1f} MW")
        top = deslastre.sort_values(ascending=False).head(8)
        for b, v in top.items():
            print(f"     {b:30s} {v:8.1f} MW")

    n, ok = aplicar_evento(base, generadores_fuera, deslastre, sis,
                           deficit_mw=deficit_mw, verbose=verbose)
    est = estado_red(n, ok)
    if verbose:
        print("\n--- Estado post-evento (flujo AC) ---")
        if traza['colapso']:
            # El flujo AC es una foto de regimen permanente y no sabe nada de
            # la frecuencia: si esta se hundio, ese regimen nunca llega a
            # existir. Sin este aviso, el informe decia "COLAPSO" y dos lineas
            # despues daba la demanda servida al 100%, como si el sistema
            # hubiera aguantado.
            print("  AVISO: la frecuencia colapsa, asi que este estado NO se "
                  "alcanza en la realidad.")
            print("         Lo que sigue describe el reparto de potencia, no "
                  "un punto de operacion viable.")
        if not est['convergio']:
            print("  el flujo no converge: el punto de operacion no es viable")
        else:
            print(f"  demanda servida       : {est['demanda_MW']:9.1f} MW")
            print(f"  tension               : [{est['v_min']}, {est['v_max']}] p.u."
                  f"   fuera de rango: {est['n_v_fuera']}")
            print(f"  cargabilidad maxima   : {est['carga_max_pct']}%   "
                  f"sobrecargas: {est['n_sobrecargas']}")
            if est['n_sobrecargas']:
                print(f"  elemento mas exigido  : {est['critico']}")

    return {'traza': traza, 'escalones': escalones, 'deslastre': deslastre,
            'origen': origen, 'estado': est, 't': t, 'f': f, 'red': n}


def grafico(resultados, archivo=None):
    archivo = archivo or salida('frecuencia_evento.png')
    """Dibuja las trayectorias de frecuencia comparadas."""
    import matplotlib
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(10, 5.5))
    for etiqueta, r in resultados.items():
        ax.plot(r['t'], r['f'], lw=1.8, label=etiqueta)
    ax.axhline(F0, color='#555', lw=0.8, ls=':')
    for u in EDAC_UMBRALES_HZ:
        ax.axhline(u, color='#bbb', lw=0.5, ls='--')
    ax.axhline(F_COLAPSO, color='#c0392b', lw=1.0, ls='-.',
               label=f'colapso ({F_COLAPSO} Hz)')
    ax.set_xlabel('tiempo [s]')
    ax.set_ylabel('frecuencia [Hz]')
    ax.set_title('Respuesta de frecuencia del STN ante perdida de generacion')
    ax.legend(loc='lower right', fontsize=9)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(archivo, dpi=150)
    if matplotlib.get_backend().lower() not in ('agg', 'pdf', 'ps', 'svg'):
        plt.show()
    plt.close(fig)
    print(f"\n  grafico guardado en '{archivo}'")


def main():
    ap = argparse.ArgumentParser(
        description="Respuesta del STN a una perdida subita de generacion: "
                    "dinamica de frecuencia, EDAC, OPF de deslastre y AGC.")
    ap.add_argument('--perder', nargs='+', default=None,
                    help="generadores que salen de servicio, por nombre")
    ap.add_argument('--perder-mw', type=float, default=None,
                    help="perdida generica en MW, sin senalar generadores")
    ap.add_argument('--perder-area', default=None,
                    help="pierde toda la generacion de un area operativa")
    ap.add_argument('--modo', choices=['edac', 'optimo', 'comparar'],
                    default='edac', help="como se elige que carga botar")
    ap.add_argument('--sin-edac', action='store_true',
                    help="simular tambien sin deslastre, para comparar")
    ap.add_argument('--grafico', default=None)
    from stn import punto_operacion as PO
    PO.anadir_flag(ap)
    args = ap.parse_args()

    print("Construyendo el punto de operacion...")
    base, _, ctx = PO.de_args(args, verbose=False)
    PO.cabecera(ctx)
    sis = Sistema(base)
    print("\nParametros del sistema")
    sis.resumen()

    # El grafico lleva el sufijo del caso salvo que se pida uno concreto: con
    # un nombre fijo, la curva del diurno sobrescribia la de la punta y las
    # dos dinamicas ---que es justo lo que se quiere contrastar--- quedaban
    # reducidas a la ultima que se hubiera corrido.
    grafico_dest = args.grafico or salida(f'frecuencia_evento{ctx["sufijo"]}.png')

    # Limites de velocidad de la primaria, si se piden. Se fijan como globales
    # del modulo para que los alcancen tambien los modulos que llaman a
    # simular_frecuencia sin pasar los argumentos.

    fuera, deficit = [], 0.0
    if args.perder:
        for g in args.perder:
            if g in base.generators.index:
                fuera.append(g)
                deficit += float(base.generators.at[g, 'p_set'])
            else:
                print(f"AVISO: el generador '{g}' no existe")
    elif args.perder_area:
        from stn.zonas import area_por_bus
        mapa = area_por_bus(base)
        for g in base.generators.index:
            bus = base.generators.at[g, 'bus']
            if mapa.get(bus, ('', ''))[1].lower() == args.perder_area.lower():
                fuera.append(g)
                deficit += float(base.generators.at[g, 'p_set'])
    elif args.perder_mw:
        deficit = args.perder_mw
    else:
        ap.error("indica --perder, --perder-mw o --perder-area")

    if deficit <= 0:
        print("No hay generacion que perder con esos criterios.")
        return 1

    print(f"\n{'='*70}")
    print(f" EVENTO: se pierden {deficit:.1f} MW"
          + (f" en {len(fuera)} generadores" if fuera else " (perdida generica)"))
    print('='*70)

    resultados = {}
    modos = ['edac', 'optimo'] if args.modo == 'comparar' else [args.modo]
    for modo in modos:
        print(f"\n### Modo de deslastre: {modo}")
        resultados[f'EDAC ({modo})'] = escenario(
            base, sis, fuera, deficit, modo=modo, con_edac=True)

    if args.sin_edac:
        print("\n### Sin deslastre, para comparar")
        resultados['sin deslastre'] = escenario(
            base, sis, fuera, deficit, modo='edac', con_edac=False)

    try:
        grafico(resultados, grafico_dest)
    except Exception as e:                          # noqa: BLE001
        print(f"  no se pudo graficar: {e}")

    if len(resultados) > 1:
        print(f"\n{'='*70}")
        print(" COMPARACION")
        print('='*70)
        filas = []
        for k, r in resultados.items():
            e = r['estado']
            filas.append({
                'caso': k,
                'nadir_Hz': round(r['traza']['nadir'], 3),
                'f_final_Hz': round(r['traza']['f_final'], 3),
                'pide_frec_MW': round(r['traza']['deslastre_total'], 1),
                'deslastrado_MW': round(float(r['deslastre'].sum()), 1),
                'colapso': r['traza']['colapso'],
                'AC_ok': e['convergio'],
                'sobrecargas': e.get('n_sobrecargas', '-'),
                'carga_max_%': e.get('carga_max_pct', '-'),
            })
        print(pd.DataFrame(filas).to_string(index=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
