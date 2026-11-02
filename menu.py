# menu.py
"""Consola interactiva del modelo del STN.

Reune en un solo sitio todo lo que el modelo sabe hacer, para no tener que
recordar que modulo y que argumentos corresponden a cada cosa.

    python menu.py

POR QUE MERECE LA PENA, mas alla de agrupar ordenes: el caso base tarda unos
20 segundos en construirse y resolverse, y hasta ahora cada consulta lo
reconstruia desde cero. Aqui se construye UNA VEZ por sesion y se reutiliza,
de modo que preguntar por una barra, por la cobertura de datos o por el BTM es
instantaneo a partir de la segunda vez.

Las simulaciones largas (barridos N-1, escenarios de cascada) se lanzan en un
proceso aparte, porque modifican la red y no conviene que contaminen el caso
base cacheado.

Sin dependencias fuera de la libreria estandar y de lo que el modelo ya usa.
"""

import os
import subprocess
import sys
import textwrap
import time

# El modelo escribe avisos de pypsa y de pandas que no aportan nada en una
# consola interactiva.
os.environ.setdefault('PYTHONWARNINGS', 'ignore')

ANCHO = 78
_BASE = {}          # cache del caso base de esta sesion

# Punto de operacion ACTIVO. Se elige al entrar y vale para toda la sesion:
# tanto para las simulaciones, que se lanzan en otro proceso con `args`, como
# para las consultas, que leen el caso cacheado que construye `base()` con
# `kw`. Las dos cosas TIENEN que salir del mismo sitio, o el menu enseña
# consultas de un sistema y simulaciones de otro.
_PUNTO = {'tipo': 'punta', 'kw': {}, 'args': [],
          'etiqueta': 'punta nocturna 18-22 h'}


# ---------------------------------------------------------------------------
# Presentacion
# ---------------------------------------------------------------------------

def _c(txt, code):
    """Color ANSI, solo si la salida es un terminal de verdad."""
    return f"\033[{code}m{txt}\033[0m" if sys.stdout.isatty() else txt


def titulo(txt):
    print()
    print(_c("=" * ANCHO, '90'))
    print(_c(f" {txt}", '1'))
    print(_c("=" * ANCHO, '90'))


def parrafo(txt, sangria='   '):
    for linea in textwrap.wrap(' '.join(txt.split()), ANCHO - len(sangria)):
        print(sangria + linea)


def pausa():
    try:
        input(_c("\n   [Enter] para volver al menu ", '90'))
    except EOFError:
        pass


def preguntar(texto, defecto=None, tipo=str):
    """Pide un valor por teclado, con valor por defecto."""
    suf = f" [{defecto}]" if defecto is not None else ""
    while True:
        try:
            v = input(_c(f"   {texto}{suf}: ", '96')).strip()
        except EOFError:
            return defecto
        if not v:
            return defecto
        try:
            return tipo(v)
        except ValueError:
            print(_c("   valor no valido", '91'))


def elegir(texto, opciones, defecto=0):
    """Menu corto de opciones; devuelve el elemento elegido."""
    print(f"   {texto}")
    for i, o in enumerate(opciones):
        marca = '*' if i == defecto else ' '
        print(f"     {marca} {i+1}) {o}")
    v = preguntar("numero", defecto + 1, int)
    return opciones[v - 1] if 1 <= v <= len(opciones) else opciones[defecto]


# ---------------------------------------------------------------------------
# Caso base cacheado
# ---------------------------------------------------------------------------

def base(verbose=True):
    """Caso base resuelto, construido una sola vez por sesion.

    Devuelve (network, diagnostico). La red se entrega SIN copiar: quien la
    vaya a modificar debe llamar a .copy() primero. Las consultas de solo
    lectura la usan tal cual, que es el caso habitual aqui.
    """
    if 'n' not in _BASE:
        if verbose:
            print(_c(f"\n   construyendo {_PUNTO['etiqueta']} "
                     f"(solo la primera vez)...", '90'))
        t0 = time.time()
        # Por `punto_operacion` y no por `contingencias.caso_base()` directo:
        # es la unica puerta que sabe construir los TRES puntos, y asi las
        # consultas ven exactamente el mismo sistema que las simulaciones.
        from stn import punto_operacion as PO
        red, diag, ctx = PO.construir(verbose=False, **_PUNTO['kw'])
        _BASE['n'], _BASE['diag'], _BASE['ctx'] = red, diag, ctx
        if verbose:
            print(_c(f"   listo en {time.time()-t0:.0f} s", '90'))
    return _BASE['n'], _BASE['diag']


def sistema():
    """Objeto Sistema (H, S_base, reserva...), tambien cacheado."""
    if 'sis' not in _BASE:
        from stn import agc_deslastre as AGC
        _BASE['sis'] = AGC.Sistema(base()[0])
    return _BASE['sis']


def num_de(fragmento):
    """Numero de menu de la opcion cuyo nombre contiene `fragmento`.

    Las referencias cruzadas ("ver la opcion 16") estaban escritas a mano y se
    desincronizaron en cuanto se inserto una opcion nueva: todas las de debajo
    pasaron a apuntar a la pantalla equivocada, incluida una que remitia a si
    misma. Resolverlo en tiempo de ejecucion lo hace imposible.
    """
    i = 0
    for nombre, accion, _ in MENU:
        if accion is None:
            continue
        i += 1
        if fragmento.lower() in nombre.lower():
            return i
    return 0


def correr(args):
    """Lanza un modulo del modelo en un proceso aparte.

    SIEMPRE enseña la orden que construyo. Antes estaba detras de un parametro
    `descripcion` que ningun sitio pasaba, de modo que el menu era una caja
    negra: el usuario no veia las banderas que se emitian, que es justo como
    sobreviven los desajustes entre lo que el menu cree pedir y lo que el
    modulo entiende.
    """
    print(_c(f"\n   $ python {' '.join(args)}", '90'))
    print()
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable] + args, check=False)
    except KeyboardInterrupt:
        print(_c("\n   interrumpido", '93'))
        return
    if r.returncode != 0:
        print(_c(f"\n   el modulo termino con codigo {r.returncode}: "
                 f"la salida de arriba no es un resultado valido", '91'))
    print(_c(f"\n   ({time.time()-t0:.0f} s)", '90'))


# ---------------------------------------------------------------------------
# 1. Simulaciones
# ---------------------------------------------------------------------------

def _punto():
    """Argumentos de linea de ordenes del punto de operacion ACTIVO.

    Ya no pregunta nada: el punto se elige UNA VEZ al entrar, en
    `elegir_punto()`, y vale para toda la sesion. Antes cada opcion lo
    preguntaba por su cuenta, con el resultado de que las de "consultar el
    modelo" ---que leen el caso cacheado--- enseñaban siempre la punta
    mientras las simulaciones podian estar corriendo sobre el diurno. Dos
    pantallas del mismo menu hablando de sistemas distintos.

    Devuelve una copia: quien la reciba le añade sus propias banderas y no
    puede permitirse mutar el estado de la sesion al hacerlo.
    """
    return list(_PUNTO['args'])


def _fechas():
    """Las fechas descargadas, sin mas.

    NO se marca cuales sirven para un estudio de frecuencia, aunque seria
    util: hoy 10 de las 17 no pasan el filtro de cobertura. Calcularlo exige
    construir el punto de cada una ---unos 25 s en total--- y el menu tiene
    que responder al instante.

    La comprobacion de verdad se hace al elegir, en `_avisar_calidad()`, que
    construye esa fecha y enseña su cobertura antes de dejar seguir. Asi que
    el usuario se entera, pero despues de elegir, no antes.
    """
    from stn.fuentes.xm_snapshot import fechas_disponibles
    return fechas_disponibles()


def elegir_punto(primera_vez=False):
    """Elige el punto de operacion de la sesion. Devuelve True si cambio.

    Es la PRIMERA pantalla del menu, y a proposito: lo primero que hay que
    decidir no es que analisis correr sino sobre que sistema. Antes se
    preguntaba al final, dentro de cada opcion, y era facil correr media
    sesion sin tener claro sobre que.

    El defecto es SIEMPRE la punta, que es el caso medido: asi
    `verificar_sistema --menu`, que contesta con EOF y por tanto toma el
    defecto, sigue comprobando exactamente lo que comprobaba.
    """
    if primera_vez:
        titulo("SOBRE QUE SISTEMA QUIERES TRABAJAR")
        print()
        parrafo("Los tres puntos de operacion NO tienen el mismo respaldo, y "
                "conviene tenerlo delante antes de elegir:")
        print()
        parrafo("La PUNTA esta medida y contrastada contra PowerFactory. El "
                "DIURNO se deriva de ella con factores horarios de XM. Una "
                "FECHA se construye desde cero con el despacho y el pronostico "
                "que publica XM: hereda la validacion de la RED, no la del "
                "punto de operacion.")
        print()

    op = elegir("Punto de operacion:",
                ["punta nocturna 18-22 h  (el caso MEDIDO y validado)",
                 "diurno 10-11 h  (con sol; DERIVADO de la punta, no validado)",
                 "una fecha concreta  (despacho y pronostico reales de XM)"], 0)

    if op.startswith('punta'):
        nuevo = {'tipo': 'punta', 'kw': {}, 'args': [],
                 'etiqueta': 'punta nocturna 18-22 h'}
    elif op.startswith('diurno'):
        h = elegir("Que hora:", ["10:00-11:00 (maxima penetracion solar)",
                                 "13:00-14:00"], 0)
        hora = 11 if h.startswith('10') else 13
        nuevo = {'tipo': 'diurno', 'kw': {'hora': hora},
                 'args': ['--diurno', str(hora)],
                 'etiqueta': f'diurno (HourNN={hora})'}
    else:
        fechas = _fechas()
        if not fechas:
            print(_c("\n   no hay ninguna fecha descargada. Descargalas con "
                     "sinsim; mira el README, seccion 1.", '93'))
            pausa()
            return False
        print()
        parrafo("Fechas descargadas:")
        for i, f in enumerate(fechas):
            print(f"     {i+1:2d}) {f}")
        print()
        i = preguntar(f"cual (1-{len(fechas)})", len(fechas), int)
        fecha = fechas[i - 1] if 1 <= i <= len(fechas) else fechas[-1]
        per = preguntar("periodo horario (1-24; 19 es la punta)", 19, int)
        per = min(max(int(per), 1), 24)
        nuevo = {'tipo': 'fecha', 'kw': {'fecha': fecha, 'periodo': per},
                 'args': ['--fecha', fecha, '--periodo', str(per)],
                 'etiqueta': f'XM {fecha} periodo {per}'}

    cambio = nuevo != _PUNTO
    _PUNTO.update(nuevo)
    if cambio:
        # El caso cacheado es del punto ANTERIOR. Dejarlo seria enseñar
        # consultas de un sistema y simulaciones de otro.
        _BASE.clear()

    if nuevo['tipo'] == 'fecha':
        _avisar_calidad(nuevo)
    return cambio


def _avisar_calidad(punto):
    """Construye el punto y enseña su cobertura antes de dejar seguir.

    Una fecha a la que le falta demanda por ubicar da un nadir OPTIMISTA, y
    eso no se ve en los resultados: hay que decirlo aqui.
    """
    try:
        from stn.fuentes.xm_snapshot import FuenteXM
        cal = FuenteXM(punto['kw']['fecha'], punto['kw']['periodo'],
                       verbose=False).calidad()
    except Exception as e:                                   # noqa: BLE001
        print(_c(f"\n   no se pudo leer esa fecha: {type(e).__name__}: {e}",
                 '91'))
        return
    print()
    print(f"   generacion ubicada: {cal.cobertura_despacho_pct:6.2f} %   "
          f"demanda ubicada: {cal.cobertura_demanda_pct:6.2f} %")
    if cal.apto_para_frecuencia:
        print(_c("   cobertura suficiente para estudios de frecuencia", '92'))
    else:
        print(_c(f"   AVISO: faltan {cal.p_demanda_no_ubicada_mw:.0f} MW de "
                 f"demanda sin ubicar.", '91'))
        parrafo("El EDAC dimensiona sobre la demanda que ve, asi que cortaria "
                "de menos y el nadir saldria OPTIMISTA. Las fechas anteriores "
                "a septiembre de 2026 usan codigos de nodo antiguos que la "
                "tabla de sinsim aun no cubre; mira el README.", '   ')


def sim_caso_base():
    correr(['-m', 'stn.main'] + _punto())


def sim_comparar():
    parrafo("Corre la MISMA bateria sobre los dos puntos de operacion y los "
            "imprime en columnas: balance de potencia, tensiones, inercia, "
            "reserva, respuesta a la misma perturbacion y balance por area. "
            "Evita tener que cruzar a mano dos salidas largas.")
    args = ['-m', 'stn.comparar_casos']
    mw = preguntar("perturbacion de referencia en MW", 3000.0, float)
    args += ['--evento-mw', str(mw)]
    if elegir("El barrido N-1:",
              ["leer los CSV ya generados (rapido)",
               "correrlo ahora (varios minutos por caso)"], 0
              ).startswith('correrlo'):
        args.append('--n1')
    correr(args)


def sim_validacion():
    # La validacion contrasta contra el reporte de PowerFactory, que es de la
    # punta. No acepta --diurno ni --fecha a proposito: no existe un caso de
    # referencia con el que contrastarlos, y un modulo que dijera "validado"
    # sobre ellos estaria mintiendo. Se avisa en vez de callar, porque si no
    # el usuario cree que valido el punto que tiene activo.
    if _PUNTO['tipo'] != 'punta':
        print()
        print(_c(f"   el punto activo es {_PUNTO['etiqueta']}, pero la "
                 f"validacion", '93'))
        print(_c("   SIEMPRE contrasta la punta: es el unico caso con "
                 "referencia.", '93'))
        print(_c("   Lo que sigue NO valida el punto que tienes elegido.",
                 '93'))
        print()
    correr(['-m', 'stn.validacion'])


def sim_invariantes():
    correr(['-m', 'tests.test_invariantes'])


def sim_n1():
    tipo = elegir("Que quieres barrer:",
                  ["todo (lineas y trafos, 296 casos, ~2 min)",
                   "solo 500 kV (55 casos, rapido)",
                   "un area concreta",
                   "dobles contingencias N-2 en 500 kV"], 0)
    args = ['-m', 'stn.contingencias']
    if tipo.startswith('solo 500'):
        args += ['--nivel', '500']
    elif tipo.startswith('un area'):
        a = preguntar("area (Caribe, Antioquia, Oriental, Nordeste, Suroccidental)",
                      "Caribe")
        args += ['--area', a]
    elif tipo.startswith('dobles'):
        args += ['--n2', '--nivel', '500']
    if elegir("Imponer la curva de capacidad de los generadores (PV->PQ)?",
              ["no (mas rapido)", "si (mas lento, detecta colapsos de tension)"], 0
              ).startswith('si'):
        args.append('--con-limites')
        print(_c("   aviso: con limites el barrido completo tarda ~15 min", '93'))
    args += _punto()
    correr(args)


def sim_frecuencia():
    modo = elegir("Que evento simular:",
                  ["perdida de N MW de generacion",
                   "perdida de un area completa",
                   "perdida de generadores concretos"], 0)
    args = ['-m', 'stn.agc_deslastre']
    if modo.startswith('perdida de N'):
        args += ['--perder-mw', str(preguntar("MW que se pierden", 3000, float))]
    elif modo.startswith('perdida de un area'):
        args += ['--perder-area', preguntar("area", "Antioquia")]
    else:
        g = preguntar("nombres de generador separados por espacio",
                      "gen_ANTIOQUIA__500")
        args += ['--perder'] + g.split()
    rep = elegir("Como repartir el deslastre:",
                 ["edac (proporcional, como el esquema regulado)",
                  "optimo (OPF que elige donde cortar)",
                  "comparar los dos"], 0)
    args += ['--modo', rep.split()[0]]
    args += _punto()
    correr(args)


def sim_diurno():
    parrafo("El caso base es la punta nocturna (18-22 h) y a esa hora la "
            "fotovoltaica entrega CERO: el evento que estudia la tesis no "
            "puede ocurrir. Esta opcion construye el punto diurno, derivado "
            "del de punta con factores horarios medidos de la API de XM.")
    h = elegir("Que hora:",
               ["10:00-11:00  (maxima penetracion solar, 23,5 %)",
                "12:00-13:00  (el mediodia: mas demanda, menos penetracion)"], 0)
    args = ['-m', 'stn.escenario_diurno', '--hora', '11' if h.startswith('10') else '13']
    if elegir("Reparto de la solar por:", ["subarea (16 grupos)",
                                           "area (5 grupos)"], 0).startswith('area'):
        args += ['--por', 'area']
    correr(args)


def sim_btm():
    print()
    parrafo("Este es el algoritmo central del trabajo. Reparte un BTM por las "
            "barras, simula que una parte se desconecta de golpe, y decide que "
            "barras botar con un MILP que tiene en cuenta la generacion "
            "embebida que se llevaria por delante.")
    print()
    # El punto ya esta elegido para toda la sesion. Lo unico que hay que
    # decir aqui es si en ESE punto el evento puede ocurrir siquiera: de
    # noche la fotovoltaica da cero y no hay nada que desconectar.
    if _PUNTO['tipo'] == 'punta':
        print(_c("   aviso: en la punta nocturna la solar da CERO, asi que "
                 "el evento que estudia", '93'))
        print(_c("   este modulo no puede ocurrir. El BTM que cae sera solo "
                 "el no solar.", '93'))
        print(_c("   Para verlo de verdad, cambia el punto (opcion p) a "
                 "diurno o a una fecha", '93'))
        print(_c("   con sol.", '93'))
        print()
    fuente = elegir("De donde sale el BTM:",
                    ["agpe (CASO BASE: el BTM real de hoy, 740,8 MW segun UPME)",
                     "agpe_gd (sensibilidad: + generacion distribuida, 1.346,8 MW)",
                     "sintetico (penetracion supuesta, para escenarios futuros)",
                     "ambas (el pool viejo AGPE+menores; NO es BTM, solo para "
                     "reproducir cifras previas al 31-10-2026)"], 0)
    args = ['-m', 'stn.deslastre_btm', '--fuente', fuente.split()[0]] + _punto()
    if fuente.startswith('sintetico'):
        args += ['--btm', str(preguntar("penetracion supuesta en % de la demanda",
                                        20.0, float))]
    args += ['--caida', str(preguntar("% del BTM con inversor que se desconecta",
                                      80.0, float))]
    pg = preguntar("perdida convencional adicional en MW (0 = ninguna)", 0.0, float)
    if pg:
        args += ['--perder-gen', str(pg)]
    rob = elegir("Con que vara exigir la suficiencia del deslastre:",
                 ["peor_escenario (robustez de peor caso, recomendada)",
                  "estimado (se fia de la estimacion)",
                  "piso (garantia literal de Das, sobrecorta)"], 0)
    args += ['--robustez', rob.split()[0]]
    lam = preguntar("peso lambda de la penalizacion por destruir BTM sano", 5.0, float)
    args += ['--lambda-btm', str(lam)]
    if elegir("Comparar contra el reparto proporcional del EDAC actual?",
              ["si", "no"], 0) == 'si':
        args.append('--comparar')
    correr(args)


def sim_cascada():
    esc = elegir("Que escenario:",
                 ["btm (cae el BTM con inversor)",
                  "iberia (secuencia tipo apagon iberico)",
                  "cascada (4 perdidas de 600 MW encadenadas)",
                  "simple (una perdida en t=0)",
                  "abrir las N lineas mas cargadas"], 0)
    args = ['-m', 'stn.cascada'] + _punto()
    if esc.startswith('abrir'):
        args += ['--n-mayores', str(preguntar("cuantas lineas", 6, int))]
    else:
        args += ['--escenario', esc.split()[0]]
        if esc.startswith('btm'):
            args += ['--caida-btm', str(preguntar("fraccion del BTM que cae", 0.9, float))]
        if esc.startswith('iberia'):
            # La solar solo se pregunta en la PUNTA. En el diurno y en una
            # fecha el punto de operacion ya sabe cuanta hay ---medida del
            # despacho, en el caso de la fecha--- y preguntarla seria dar a
            # elegir entre un dato y una suposicion.
            if _PUNTO['tipo'] == 'punta':
                s = preguntar("solar EN PRODUCCION en MW (0 = punta nocturna)",
                              0.0, float)
                if s:
                    args += ['--solar-salida', str(s)]
                    print(_c("   ojo: el caso base es nocturno; esa solar es "
                             "una hipotesis, no un dato", '93'))
            else:
                print(_c(f"   la solar la fija {_PUNTO['etiqueta']}; no hace "
                         f"falta suponerla", '90'))
    correr(args)


def sim_barrido():
    """Las 24 horas de una fecha, con la misma bateria en cada una.

    NO sigue al punto de operacion, y es la quinta opcion que no lo hace: un
    barrido horario solo tiene sentido sobre una fecha de XM ---la punta y el
    diurno son UNA hora cada uno, no un dia--- asi que si el punto activo es
    una fecha la propone, y si no, pide una.
    """
    fechas = _fechas()
    if not fechas:
        print(_c("\n   no hay ninguna fecha descargada. Descargalas con "
                 "sinsim; mira el README, seccion 1.", '93'))
        pausa()
        return

    parrafo("Somete las 24 horas de una fecha a la misma bateria y al mismo "
            "evento en MW absolutos. Es lo que convierte 'cual es la hora "
            "critica' de una eleccion en una medida. Tarda unos 3 minutos "
            "por fecha.")
    print()

    # De la mas reciente a la mas antigua, y no al reves: el catalogo de
    # nodos de sinsim se curo a finales de octubre, asi que las fechas de
    # enero a agosto usan codigos viejos y 10 de las 17 descargadas no pasan
    # el filtro de cobertura. Ordenadas asi, el defecto cae en una que sirve.
    fechas = sorted(fechas, reverse=True)
    # Y si el punto activo ya es una fecha, esa es la candidata obvia.
    actual = _PUNTO['kw'].get('fecha') if _PUNTO['tipo'] == 'fecha' else None
    if actual and actual in fechas:
        fechas = [actual] + [f for f in fechas if f != actual]
    print()
    parrafo("Fechas descargadas, de la mas reciente a la mas antigua. Las "
            "anteriores a septiembre de 2026 rara vez pasan el filtro de "
            "cobertura, y el barrido excluira sus horas del resumen:")
    for i, f in enumerate(fechas):
        marca = _c('  <- el punto activo', '90') if f == actual else ''
        print(f"     {i+1:2d}) {f}{marca}")
    i = preguntar("cual (numero)", 1, int)
    if not 1 <= i <= len(fechas):
        print(_c("   fuera de rango", '91'))
        pausa()
        return
    elegidas = [fechas[i - 1]]

    if len(fechas) > 1 and elegir(
            "Barrer mas fechas para comparar:",
            ["no, solo esa", "todas las descargadas (varios minutos)"],
            0).startswith('todas'):
        elegidas = fechas

    args = ['-m', 'stn.barrido', '--fecha'] + elegidas
    mw = preguntar("evento de generacion perdida en MW", 3000.0, float)
    if mw and mw != 3000.0:
        args += ['--evento-mw', str(mw)]
    if elegir("Grafico de las 24 horas:", ["si", "no"], 0).startswith('si'):
        args += ['--grafico']
    correr(args)


def sim_reactivos():
    correr(['-m', 'stn.limites_reactivos'] + _punto())


def sim_mapa():
    # El trazado es el MISMO en todas las fechas: lo que cambia son los
    # flujos y los colores. Lo que el punto deje fuera de servicio sale a
    # trazos, no desaparece, para que dos fechas se puedan comparar de un
    # vistazo sin preguntarse si una linea falta o nunca estuvo.
    correr(['-m', 'stn.mapa_interactivo'] + _punto())


# ---------------------------------------------------------------------------
# 2. Consultar el modelo
# ---------------------------------------------------------------------------

def con_resumen():
    n, _ = base()
    s = sistema()
    v = n.buses_t.v_mag_pu.iloc[0].astype(float)
    sl = n.generators[n.generators.control == 'Slack'].index[0]
    perd = float(n.generators_t.p.iloc[0].sum() - n.loads_t.p.iloc[0].sum())
    g, c = n.generators.p_set.sum(), n.loads.p_set.sum()
    print()
    print(f"   RED        {len(n.buses)} buses | {len(n.lines)} lineas | "
          f"{len(n.transformers)} trafos | {len(n.generators)} gen | {len(n.loads)} cargas")
    ap_sl = float(n.generators_t.p.iloc[0][sl]) - float(n.generators.at[sl, 'p_set'])
    print(f"   POTENCIA   consignas  {g:9.1f} MW | demanda {c:9.1f} MW | "
          f"perdidas {perd:6.1f} MW")
    print(f"              faltan {c+perd-g:.1f} MW para cerrar y los pone el slack "
          f"-> despacho resuelto {g+ap_sl:.1f} MW")
    print(f"   TENSION    [{v.min():.4f}, {v.max():.4f}] pu | "
          f"{int((v<0.9).sum())} bajas, {int((v>1.1).sum())} altas")
    print(f"   FRECUENCIA H = {s.H:.3f} s | S_base = {s.S_base:.1f} MVA | "
          f"H*S = {s.H*s.S_base:.0f} MVA*s")
    print(f"              estatismo equivalente {s.R_eq:.4f} | "
          f"reserva {s.reserva:.1f} MW ({100*s.reserva/s.P_gen:.1f}%)")
    print()
    print("   MEZCLA POR TECNOLOGIA (MW de placa)")
    for t, mw in s.gen.groupby('tipo').p_nom.sum().sort_values(ascending=False).items():
        sub = s.gen[s.gen.tipo == t]
        nr = int(sub.H_real.notna().sum()) if 'H_real' in sub.columns else 0
        print(f"      {t:14s} {mw:9.1f}   ({nr}/{len(sub)} con H declarado)")


def con_buscar():
    n, _ = base()
    q = preguntar("texto a buscar (nombre de barra, generador o linea)", "")
    if not q:
        return
    Q = q.upper()
    print()
    buses = [b for b in n.buses.index if Q in b.upper()]
    if buses:
        print(_c("   BARRAS", '1'))
        for b in buses:
            gp = n.generators[n.generators.bus == b].p_set.sum()
            cp = n.loads[n.loads.bus == b].p_set.sum()
            v = float(n.buses_t.v_mag_pu.iloc[0][b])
            print(f"      {b:30s} v={v:.4f} pu | gen {gp:8.1f} MW | carga {cp:8.1f} MW")
            ls = [l for l in n.lines.index
                  if n.lines.at[l, 'bus0'] == b or n.lines.at[l, 'bus1'] == b]
            for l in ls:
                p = float(n.lines_t.p0.iloc[0][l])
                # el flujo visto DESDE b: en el extremo lejano es p1, que no es
                # exactamente -p0 porque la linea se come sus perdidas.
                sg = (p if n.lines.at[l, 'bus0'] == b
                      else float(n.lines_t.p1.iloc[0][l]))
                pct = 100*abs(p)/max(float(n.lines.at[l, 's_nom']), 1e-9)
                print(f"         -> {l[:46]:48s} {sg:8.1f} MW  ({pct:.0f}% de s_nom)")
    gens = [g for g in n.generators.index if Q in g.upper()]
    if gens:
        s = sistema()
        print(_c("   GENERADORES", '1'))
        for g in gens:
            r = s.gen.loc[g] if g in s.gen.index else None
            extra = ''
            if r is not None:
                h = r.get('H_real')
                extra = (f" | tipo {r.tipo} | placa {r.p_nom:.1f} MVA"
                         f" | H {'%.2f s (real)' % h if h == h else 'por tecnologia'}")
            print(f"      {g:32s} P={float(n.generators.at[g,'p_set']):8.1f} MW{extra}")
    lineas = [l for l in n.lines.index if Q in l.upper()]
    if lineas and not buses:
        print(_c("   LINEAS", '1'))
        for l in lineas[:25]:
            p = float(n.lines_t.p0.iloc[0][l])
            print(f"      {l[:52]:54s} {p:8.1f} MW | s_nom {float(n.lines.at[l,'s_nom']):.0f} MVA")
    if not (buses or gens or lineas):
        print(_c(f"   nada coincide con '{q}'", '93'))


def con_cobertura():
    n, _ = base()
    s = sistema()
    print()
    print("   CUANTO DEL MODELO ES DATO Y CUANTO ES SUPUESTO")
    print()
    tot = len(s.gen)
    hr = int(s.n_H_real)
    print(f"      inercia H declarada        {hr:3d} de {tot}   "
          f"({100*s.mw_H_real/s.S_base:.0f}% de la placa)")
    print(f"      estatismo declarado        {int(s.n_R_real):3d} de {tot}")
    try:
        from stn import limites_reactivos as LR
        cap = LR.capacidad(n)
        print(f"      Q_min/Q_max declarados     "
              f"{int((cap.fuente=='PARATEC').sum()):3d} de {len(cap)}")
    except Exception:
        pass
    print(f"      tecnologia conocida        "
          f"{int((s.gen.tipo!='desconocido').sum()):3d} de {tot}")
    print(f"      lineas contrastables        39 de {len(n.lines)}   (14%)")
    print()
    print("   LO QUE SIGUE SIENDO LITERATURA O SUPUESTO PROPIO")
    for a, b in [("amortiguamiento de carga D", "1,5 %/%Hz"),
                 ("constante del gobernador", "8,0 s"),
                 ("reserva primaria", "10 % de lo GIRANDO, solo sincronas"),
                 ("factor de potencia nominal", "0,90"),
                 ("limite de subexcitacion k", "0,35"),
                 ("caida_sincrona_pct", "0 %  (supuesto propio)"),
                 ("correlacion espacial del BTM", "0,90  (extrapolacion propia)"),
                 ("X, R de los 16 autotrafos", "0,12 / 0,004 genericos")]:
        print(f"      {a:30s} {b}")


def con_faltante():
    print()
    parrafo("Resumen de la seccion 6 del README, con las cifras recalculadas "
            "sobre el modelo tal como esta ahora.")
    print()
    try:
        from stn import desagregacion as D
        _, det = D.generacion_por_nodo_stn(verbose=False)
        tot, sin_atr = det.MW.sum(), det[det.stn.isna()].MW.sum()
        n, _ = base()
        print("   ENERGIA QUE EL MODELO NO VE")
        print(f"      {tot:9.1f} MW   generacion total del reporte")
        print(f"      {-sin_atr:9.1f} MW   {int(det.stn.isna().sum())} nodos sin camino trazable al STN")
        print(f"      {n.generators.p_set.sum():9.1f} MW   lo que el modelo informa")
        print(f"      {479.2:9.1f} MW   ocultos por el filtro de 30% (no estan en el reporte)")
        print()
        print("   LOS MAYORES NODOS SIN ATRIBUIR")
        for _, r in det[det.stn.isna()].nlargest(8, 'MW').iterrows():
            print(f"      {r.nodo:34s} {r.MW:7.1f} MW")
    except Exception as e:                                   # noqa: BLE001
        print(_c(f"   no se pudo calcular: {e}", '91'))
    print()
    print("   QUE PEDIR, POR ORDEN DE RETORNO")
    for i, t in enumerate([
            "export Lines/Branches de PowerFactory (5 min; validacion 39 -> ~280 lineas)",
            "caso base DIURNO (sin el, el escenario principal es imposible)",
            "re-export con Minimum Loading = 0 (recupera 479,2 MW)",
            "que es 'Pto Solo': 100 MW sincronos en San Marcos sin identificar",
            "topologia del STR de 110/115 kV (los 587,6 MW sin atribuir)",
            "ajustes de proteccion de las plantas menores"], 1):
        print(f"      {i}. {t}")


def con_btm():
    n, _ = base()
    print()
    try:
        from stn import deslastre_btm as DB
        from stn.zonas import area_por_bus
        det = DB.cargar_registrada_detalle('agpe')
        mix = det.groupby('tec').MW.sum().sort_values(ascending=False)
        tot = float(mix.sum())
        print("   EL BTM REAL DE COLOMBIA  (AGPE, {:.2f} MW segun UPME al {})"
              .format(DB.UPME_AGPE_MW, DB.UPME_FECHA))
        print("   forma por subarea y tecnologia de PARATEC, total de la UPME")
        for t, mw in mix.items():
            marca = ' <- lo unico que se cae por hueco de tension' if t == 'solar' else ''
            print(f"      {t:14s} {mw:8.1f} MW   {100*mw/tot:5.1f}%{marca}")
        print(f"      {'TOTAL':14s} {tot:8.1f} MW")
        print()
        p = det.pivot_table(index='area', columns='tec', values='MW',
                            aggfunc='sum').fillna(0.0)
        p['TOTAL'] = p.sum(axis=1)
        # se cae lo que entra por inversor; hoy el registro solo trae solar en
        # esa categoria, pero si aparece eolica embebida debe contar igual.
        p['inv%'] = 100*(p.get('solar', 0) + p.get('eolica', 0))/p['TOTAL']
        print("   POR AREA OPERATIVA")
        print(f"      {'area':16s} {'total':>9} {'% inversor':>11}")
        for a, r in p.sort_values('inv%', ascending=False).iterrows():
            print(f"      {a:16s} {r.TOTAL:9.1f} {r['inv%']:10.1f}%")
        mapa = area_por_bus(n)
        esc = DB.EscenarioBTM(fuente='agpe')
        btm = DB.repartir_btm(n, esc, mapa)
        fi = DB.fraccion_inversor(esc, mapa, btm.index)
        pen = 100*float((btm*fi).sum())/float(n.loads.p_set.sum())
        print()
        print(f"   penetracion con inversor sobre la demanda del modelo: {pen:.2f}%")
        escgd = DB.EscenarioBTM(fuente='agpe_gd')
        bgd = DB.repartir_btm(n, escgd, mapa)
        fgd = DB.fraccion_inversor(escgd, mapa, bgd.index)
        pgd = 100*float((bgd*fgd).sum())/float(n.loads.p_set.sum())
        print(f"   idem anadiendo generacion distribuida (sensibilidad):  {pgd:.2f}%")
        print( "   umbral medido a partir del cual el EDAC actua:         "
               "5,15% (3,8-6,5 segun hipotesis)")
        print()
        print( "   OJO: hasta el 31-10-2026 este panel sumaba las PLANTAS MENORES")
        print( "   (1.496,2 MW). No son BTM: la CREG 086 de 1996 las declara")
        print( "   excluyentes con la figura de autogenerador, y son PCH de ISAGEN,")
        print( "   Celsia y Proelectrica que venden en bolsa.")
        print()
        parrafo("El rango sale de variar la hipotesis sobre la capacidad "
                f"aparente no declarada (opcion {num_de('Sensibilidad')}). Y es una COTA INFERIOR: los "
                "MW que el modelo no ve no entran en S_base y todos lo "
                "aumentarian.")
    except Exception as e:                                   # noqa: BLE001
        print(_c(f"   no se pudo calcular: {e}", '91'))


def con_areas():
    n, _ = base()
    from stn import zonas
    print()
    r = zonas.resumen(n)
    print(r.to_string())
    print(f"\n   TOTAL  Gen={r.GenP.sum():9.1f}  Load={r.LoadP.sum():9.1f}  "
          f"Loss={r.LossP.sum():7.1f} MW")
    parrafo("GenP es el despacho resuelto, o sea que incluye lo que el slack "
            "anade para cerrar el balance; por eso supera en unos pocos MW a la "
            f"suma de consignas que muestra la opcion {num_de('Resumen del sistema')}. InterP positivo = el "
            "area exporta.")

    # Contraste con los limites de importacion por area que publica XM en sus
    # informes de planeamiento operativo (IPOEMP IV 2024, red a dic-2024). No
    # estan en PARATEC. Varios los fija la ESTABILIDAD y no el limite termico,
    # asi que el N-1 de este modelo ---que solo mira termico y tension--- no
    # los detecta por su cuenta.
    print()
    print(_c("   IMPORTACION FRENTE AL LIMITE OPERATIVO QUE PUBLICA XM", '1'))
    lim = {'Caribe': 2000.0, 'Oriental': 900.0, 'Suroccidental': 560.0}
    porque = {'Caribe': 'contingencia sencilla en 500 kV',
              'Oriental': 'contingencia Primavera-Bacata 500 kV',
              'Suroccidental': 'sobrecarga de ATR San Marcos o Virginia'}
    print(f"      {'area':16s} {'importa':>10} {'limite':>9} {'uso':>7}  que lo fija")
    for a, x in r.iterrows():
        imp = -float(x.InterP)
        L = lim.get(a)
        if L is None:
            print(f"      {a:16s} {imp:10.1f} {'-':>9} {'-':>7}  sin limite publicado")
            continue
        marca = '' if imp <= L else _c('  <- EXCEDE', '91')
        print(f"      {a:16s} {imp:10.1f} {L:9.0f} {100*imp/L:6.0f}%  {porque[a]}{marca}")
    print()
    parrafo("Ojo al interpretarlo: el caso de PowerFactory del que parte el "
            "modelo es de RED FUTURA ---declara Study Time 31/12/2042 y "
            "Expansion Stage P20--- mientras que estos limites son para la red "
            "de 2024. Una red reforzada admite mas transferencia, asi que "
            "exceder uno no prueba que el caso sea irrealizable; lo que no "
            "sabemos es cual seria el limite nuevo. La demanda del caso si es "
            "actual: 12.472,5 MW en el reporte completo, un 3 % sobre el "
            "record del SIN de 2025.")


# ---------------------------------------------------------------------------
# 3. Analisis
# ---------------------------------------------------------------------------

def ana_umbral():
    n, _ = base()
    s = sistema()
    from stn import agc_deslastre as AGC, deslastre_btm as DB
    from stn.zonas import area_por_bus
    mapa = area_por_bus(n)
    caida = preguntar("% del BTM con inversor que se desconecta", 90.0, float)
    print()
    print(f"      {'penetracion':>12} {'se cae':>10} {'nadir':>9} {'escalones':>10}")
    for pen in (5.0, 5.37, 6.0, 7.0, 8.0, 8.5, 10.0, 15.0, 20.0, 40.0):
        esc = DB.EscenarioBTM(fuente='sintetico', penetracion_pct=pen,
                              caida_pct=caida)
        b = DB.repartir_btm(n, esc, mapa)
        ca, _ = DB.aplicar_caida_btm(b, esc, DB.fraccion_inversor(esc, mapa, b.index))
        _, _, e, tr = AGC.simular_frecuencia(
            s, con_edac=True,
            eventos=[{'t': 0.0, 'dP_gen': 0.0, 'dP_carga': float(ca.sum())}])
        nd = sum(1 for x in e if x['disparado'])
        m = '  <- dispara' if nd else ''
        etq = f"{pen:.2f}%" + (" (hoy)" if abs(pen-5.37) < 0.01 else "")
        print(f"      {etq:>12} {float(ca.sum()):9.1f} MW {tr['nadir']:8.3f} "
              f"{nd:9d}{m}")
    print()
    parrafo("Sobre ESTE punto ---el caso de referencia, cuyo parque es el de "
            "2042--- el umbral esta entre el 3,8 % y el 6,5 % de penetracion "
            "con inversor, con 5,15 % como valor central, y Colombia (6,25 %) "
            f"queda POR ENCIMA. La horquilla sale de la opcion {num_de('Sensibilidad')}: lo que "
            "la mueve es cuantas unidades estan acopladas y cuanta reserva "
            "hay, y ninguna de las dos es un dato.")
    print()
    parrafo("OJO AL CITARLO. Medido sobre el punto construido desde el "
            "despacho REAL de XM, donde la generacion por convertidor es el "
            "0,3 % y no el 12,9 %, el umbral sube al 8-9 % y Colombia queda "
            "por DEBAJO, con unos 3 puntos de margen. Las dos medidas son "
            "correctas y describen sistemas distintos; lo que sostienen a la "
            "vez es una trayectoria: hoy hay unos 3 puntos de margen, y se "
            "estrechan a 0,85 cuando entre Colectora y la penetracion pase "
            "del 0,3 al 13 %. Para verlo, elige una fecha con la opcion `p` y "
            "vuelve a correr esto.")
    print()
    parrafo("Los 587,6 MW sin atribuir y los 479,2 ocultos por el filtro no "
            "entran en S_base y ambos lo aumentarian, asi que por ese lado el "
            "valor es una cota inferior.")


def ana_sensibilidad():
    import copy
    import numpy as np
    from stn import agc_deslastre as AGC, deslastre_btm as DB
    from stn.zonas import area_por_bus
    n, _ = base()
    s0 = sistema()
    mapa = area_por_bus(n)

    def umbral(s):
        lo, hi = 3.0, 30.0
        for _ in range(18):
            mid = (lo + hi) / 2
            esc = DB.EscenarioBTM(fuente='sintetico', penetracion_pct=mid,
                                  caida_pct=90.0)
            b = DB.repartir_btm(n, esc, mapa)
            ca, _ = DB.aplicar_caida_btm(b, esc,
                                         DB.fraccion_inversor(esc, mapa, b.index))
            _, _, e, _ = AGC.simular_frecuencia(
                s, con_edac=True,
                eventos=[{'t': 0.0, 'dP_gen': 0.0, 'dP_carga': float(ca.sum())}])
            if sum(1 for x in e if x['disparado']) > 0:
                hi = mid
            else:
                lo = mid
        return hi

    g0 = s0.gen
    print()
    parrafo("Las dos hipotesis que de verdad mueven el umbral son CUANTAS "
            "UNIDADES estan acopladas y CUANTA RESERVA hay. Ninguna de las dos "
            "es un dato: el despacho por unidad no lo tenemos, y la reserva "
            "admite dos lecturas legitimas (el minimo que exige la norma, o el "
            "margen fisico de las maquinas que giran).")
    print()

    def variante(etq, s_sinc=None, reserva=None):
        s = copy.copy(s0)
        ss = s0.s_sinc if s_sinc is None else s_sinc
        s.s_sinc = ss
        s.S_base = float(ss.sum())
        s.H = float((s0.h_por_gen * ss).sum() / max(s.S_base, 1e-9))
        if reserva is None:
            r = np.minimum((ss - g0.p_set).clip(lower=0.0),
                           ss * AGC.RESERVA_PRIMARIA).where(~s0.sin_primaria, 0.0)
            s.reserva = float(r.sum())
        else:
            s.reserva = float(reserva)
        s.reserva_baja = float(np.minimum(
            ss * AGC.RESERVA_PRIMARIA,
            g0.p_set.clip(lower=0.0)).where(~s0.sin_primaria, 0.0).sum())
        print(f"      {etq:>32} {s.S_base:9.0f} {s.H:7.3f} "
              f"{s.reserva:9.0f} {umbral(s):7.2f}%")

    nm = g0.get('n_maquinas')
    nm = nm.astype(float) if nm is not None else None
    print(f"      {'hipotesis':>32} {'S sinc':>9} {'H':>7} {'reserva':>9} {'umbral':>8}")
    if nm is not None:
        tam = g0.p_nom / nm.where(nm > 0)
        # todo el parque acoplado: el techo, y lo que hacia el modelo antes
        variante('parque entero acoplado', g0.p_nom)
        # una unidad de mas por planta, que es lo que suele hacer el despacho
        on = np.minimum(np.ceil((g0.p_set / tam).astype(float)).clip(lower=1.0)
                        + 1.0, nm)
        ss = (on * tam)
        ss = ss.where(ss.notna() & (ss > 0), g0.p_nom).clip(upper=g0.p_nom)
        variante('+1 unidad por planta', ss)
    variante('minimo que cubre el despacho *')
    variante('* pero reserva del 3 % (CREG)', None, 0.03 * s0.P_gen)
    print()
    print("      (*) la hipotesis del caso base")
    print()
    parrafo("El umbral vive entre el 3,8 % y el 6,5 % segun lo que se suponga, "
            "con 5,15 % como valor central, y Colombia esta en 6,25 %: por "
            "encima del centro y en el tercio alto del rango. La conclusion "
            "defendible no es una cifra sino esa horquilla ---y, desde que se "
            "puede medir sobre una fecha real, tampoco la horquilla sino la "
            "TRAYECTORIA: sobre el parque de hoy el umbral esta en 8-9 %.")
    print()
    parrafo("Lo que movio el umbral fue sustituir supuestos por datos: 6,0 % "
            "con H por tecnologia; 8,07 % con la capacidad aparente real de "
            "PARATEC; 7,67 % al quitar de la reserva los parques eolicos, que "
            "no la pueden prestar; y 6,22 % al contar como acopladas solo las "
            "unidades que hacen falta, en vez de la planta entera.")


def ana_lambda():
    print()
    parrafo("Barre el peso lambda de la penalizacion por destruir BTM sano en "
            "los tres regimenes que el algoritmo tiene que cubrir: el evento de "
            "BTM, la operacion normal de hoy y la operacion normal con "
            "penetracion alta. Tarda unos 8 minutos: son 24 MILP completos.")
    if elegir("Continuar?", ["si", "no"], 0) != 'si':
        return
    import numpy as np
    from stn import deslastre_btm as DB
    from stn import contingencias as C
    orig = C.caso_base
    red, dg = base()
    C.caso_base = lambda verbose=False: (red.copy(), dg)   # reutiliza el cache
    try:
        casos = {'1. EVENTO de BTM (sint 20%, cae el 90%, +600 MW)':
                 dict(fuente='sintetico', penetracion_pct=20.0, caida_pct=90.0,
                      perdida_gen_mw=600.0),
                 '2. NORMAL HOY (BTM real, NO se cae, -1500 MW gen)':
                 dict(fuente='agpe', caida_pct=0.0, perdida_gen_mw=1500.0),
                 '3. NORMAL A FUTURO (sint 40%, NO se cae, -1500 MW gen)':
                 dict(fuente='sintetico', penetracion_pct=40.0, caida_pct=0.0,
                      perdida_gen_mw=1500.0)}
        for nombre, kw in casos.items():
            print()
            print(_c(f"   {nombre}", '1'))
            print(f"      {'lambda':>7} {'% objetivo':>12} {'BTM sano':>11} {'barras':>8}")
            for lam in (0.0, 0.5, 5.0, 25.0):
                P, S, N = [], [], []
                for sem in (0, 1):
                    r = DB.ejecutar(DB.EscenarioBTM(semilla=sem, **kw),
                                    verbose=False, usar_milp=True, lam=lam)
                    d = r['deslastre']
                    obj = float(r['traza']['deslastre_total'])
                    a = float(d.sum()) if len(d) else 0.0
                    P.append(100*a/max(obj, 1e-9))
                    # ponderado por la fraccion que se bota de cada barra;
                    # ver deslastre_btm.btm_sano_perdido()
                    S.append(float(r['btm_sano_mw']))
                    N.append(len(d))
                aviso = '  <- el rele no corta' if np.mean(P) < 50 else ''
                print(f"      {lam:7.1f} {np.mean(P):11.1f}% {np.mean(S):10.1f} "
                      f"{np.mean(N):8.1f}{aviso}")
    finally:
        C.caso_base = orig
    print()
    parrafo("Leido por columnas: en el caso 1 subir lambda solo mejora (protege "
            "mas BTM sano sin dejar de cumplir el objetivo), asi que calibrar "
            "sobre el evento elegiria 25. El caso 2 no lo desmiente, porque con "
            "el BTM de hoy no hay bastante que penalizar. Es el caso 3 el que "
            "pone el limite: con penetracion alta y BTM que NO se cae, a lambda "
            "25 al optimizador le sale mas barato no cortar y el esquema deja de "
            "actuar. Por eso lambda se fija en el regimen normal y no en el "
            "evento, y por eso el modulo lleva el aviso que acabas de ver.")


def ana_milp():
    print()
    parrafo("Corre el mismo evento con el MILP consciente del BTM y con el "
            "reparto proporcional del esquema regulado, y compara cuanta "
            "generacion embebida SANA destruye cada uno.")
    correr(['-m', 'stn.deslastre_btm', '--fuente', 'sintetico', '--btm', '20',
            '--caida', '90', '--perder-gen', '600', '--comparar'] + _punto())


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

MENU = [
    ('SIMULAR', None, None),
    ('Caso base y flujo de carga', sim_caso_base,
     "Reconstruye la red desde los datos, resuelve el flujo AC de Newton-Raphson "
     "y muestra el balance por area, la reconciliacion de la demanda y los "
     "totales. Es el punto de partida de todo lo demas. Genera ademas el mapa y "
     "los ficheros de exportacion. Tarda ~1 min."),
    ('Validacion contra PowerFactory', sim_validacion,
     "Compara los flujos de linea del modelo con los que reporta el caso de "
     "PowerFactory. Solo 39 de 280 lineas son contrastables, asi que es un "
     "control de sanidad y no una verificacion. Incluye el contraste estricto "
     "contra Edge Elements y el cruce con la capacidad instalada de PARATEC."),
    ('Comprobar los invariantes del modelo', sim_invariantes,
     "Treinta y cuatro comprobaciones de lo que NO puede cambiar sin que algo "
     "este mal: que los cuatro modulos que construyen la red resuelvan la "
     "misma, "
     "que el balance cierre, que nadie despache sobre su placa, que el EDAC no "
     "pase del 40 %, que el MILP no bote mas carga de la que hay, que la "
     "cascada conserve la energia y que el caso diurno tenga menos inercia que "
     "el de punta. Correr despues de tocar config.py o el nucleo."),
    ('Contingencias N-1', sim_n1,
     "Saca de servicio cada linea y transformador, uno a uno, y comprueba tres "
     "cosas: que el flujo siga convergiendo, que nadie quede sobrecargado sobre "
     "su limite de emergencia, y que ninguna tension se salga de 0,9-1,1 pu. "
     "Las violaciones se cuentan respecto al caso base."),
    ('Dinamica de frecuencia (EDAC y AGC)', sim_frecuencia,
     "Simula que pasa cuando se pierde generacion de golpe: integra la ecuacion "
     "de oscilacion, dispara los escalones del EDAC segun el Acuerdo 2105, "
     "reparte el deslastre y deja que el AGC absorba el resto. Termina con un "
     "flujo AC del estado post-evento."),
    ('Comparar punta y diurno lado a lado', sim_comparar,
     "Somete los DOS puntos de operacion a la misma bateria y los imprime en "
     "columnas con la variacion ya calculada: balance de potencia, tensiones y "
     "cargabilidad, inercia y reserva, respuesta a la misma perturbacion, la "
     "contingencia mas grande de cada uno, balance por area, N-1 y el pool de "
     "BTM. Es la via rapida para ver en que se diferencian sin cruzar dos "
     "salidas largas a mano."),
    ('Deslastre con BTM  [la contribucion]', sim_btm,
     "El algoritmo central del trabajo. Reparte generacion detras del contador "
     "por las barras, desconecta de golpe la parte conectada por inversor, y "
     "resuelve un MILP que asigna barras a los escalones del EDAC teniendo en "
     "cuenta cuanta generacion embebida SANA se llevaria por delante cada corte."),
    ('Cascada multironda', sim_cascada,
     "A diferencia del N-1, no supone que el sistema aguanta el primer golpe: "
     "encadena aperturas, deslastre y disparos de proteccion en una sola linea "
     "de tiempo, ronda tras ronda, hasta que el sistema se estabiliza o colapsa."),
    ('Limites de reactivo (curva de capacidad)', sim_reactivos,
     "Impone la curva de capacidad de cada maquina conmutando a PQ las que la "
     "excedan. Desde que LIMITES_REACTIVOS esta activado el caso base YA los "
     "respeta; aqui se ve el antes y el despues: quien se salia, cuanto, y que "
     "cuesta en el contraste contra PowerFactory."),
    ('Barrido de las 24 horas de una fecha', sim_barrido,
     "Somete las 24 horas de una fecha a la misma bateria ---cobertura, red, "
     "inercia y el mismo evento en MW absolutos--- y deja la tabla en un CSV. "
     "Es lo que permite que 'cual es la hora critica' deje de contestarse por "
     "eleccion y pase a contestarse por medida: sale la 4 o la 5 de la "
     "madrugada, y NUNCA la de maxima demanda. Con varias fechas las compara "
     "al final, que es la unica forma de distinguir una propiedad del sistema "
     "de una casualidad del dia. Unos 3 minutos por fecha."),
    ('Mapa interactivo', sim_mapa,
     "Genera un HTML con la red georreferenciada: lineas coloreadas por "
     "cargabilidad, barras por tension, y los autotransformadores marcados."),

    ('CONSULTAR EL MODELO', None, None),
    ('Resumen del sistema', con_resumen,
     "Las cifras de una ojeada: tamano de la red, balance de potencia, rango de "
     "tension, inercia equivalente, reserva y mezcla por tecnologia, indicando "
     "cuantas maquinas tienen H declarado y cuantas usan el valor por defecto."),
    ('Buscar una barra, generador o linea', con_buscar,
     "Busca por texto libre y muestra lo que el modelo sabe de lo que encuentre: "
     "tension, generacion, carga, lineas conectadas con su flujo y cargabilidad, "
     "y para los generadores su tecnologia, placa e inercia."),
    ('Cobertura de datos: que es dato y que es supuesto', con_cobertura,
     "Cuenta cuantas maquinas tienen inercia, estatismo, reactivos y tecnologia "
     "declarados por PARATEC frente a cuantas usan un valor por defecto, y lista "
     "los parametros que siguen siendo literatura o supuesto propio."),
    ('Que informacion falta', con_faltante,
     "La contabilidad de la energia que el modelo no ve, los nodos de generacion "
     "que no se pueden atribuir a ninguna barra del STN, y la lista priorizada "
     "de que datos habria que conseguir."),
    ('Como se construye el escenario diurno', sim_diurno,
     "El punto diurno se elige al entrar, con la opcion `p`; esto ENSEÑA de "
     "donde sale. Deriva del caso de punta aplicando factores horarios MEDIDOS "
     "de la API de XM (638 dias de demanda, 62 laborables de despacho por "
     "tecnologia) y se queda en 10:00-11:00, con 2.454 MW de solar "
     "produciendo y la demanda al 90 % de la punta. Sale con un 25 % menos de "
     "energia cinetica: menos demanda y mas fragil, que es todo el punto."),
    ('El BTM colombiano', con_btm,
     "Composicion del BTM registrado en PARATEC por tecnologia y por area. Lo "
     "importante: el 96,9 % del BTM real ---los AGPE, 740,8 MW--- se conecta "
     "por inversor, que es lo unico que se desconecta en bloque ante un hueco "
     "de tension. Las plantas menores NO son BTM: CREG 086 de 1996 las declara "
     "excluyentes con la figura de autogenerador."),
    ('Balance por area operativa', con_areas,
     "Generacion, demanda, perdidas e intercambio de cada una de las cinco areas "
     "del STN."),

    ('ANALISIS', None, None),
    ('Umbral de penetracion con inversor', ana_umbral,
     "Barre la penetracion de BTM con inversor y busca a partir de que punto una "
     "desconexion masiva llega a disparar el EDAC. Es el resultado fechable del "
     "trabajo. Colombia queda por debajo con las hipotesis centrales, pero por "
     "encima si se toma la reserva minima que exige la norma: mira la "
     "sensibilidad del umbral antes de citar una cifra sola."),
    ('Sensibilidad del umbral', ana_sensibilidad,
     "Cuanto se mueve ese umbral si cambian las hipotesis sobre la capacidad no "
     "declarada. Sirve para saber si el resultado es firme o depende de lo que "
     "no sabemos."),
    ('Barrido del peso lambda', ana_lambda,
     "Recorre el peso de la penalizacion por destruir BTM sano en tres "
     "regimenes: el evento de BTM, la operacion normal de hoy y la operacion "
     "normal con penetracion alta. Muestra que el limite de lambda lo fija el "
     "regimen normal y no el evento. Tarda unos 8 minutos."),
    ('MILP frente al EDAC proporcional', ana_milp,
     "Corre el mismo evento con los dos metodos y compara cuanta generacion "
     "embebida sana destruye cada uno para conseguir el mismo alivio. Mira "
     "tambien la ultima columna: el MILP concentra el corte en muchas menos "
     "barras, y eso deja la red mas cargada que el reparto proporcional."),
]


_AVAL = {'punta': ('MEDIDO y validado contra PowerFactory', '92'),
         'diurno': ('DERIVADO de la punta; no validado', '93'),
         'fecha': ('CONSTRUIDO desde el despacho de XM; red validada, '
                   'punto de operacion no', '93')}


def pintar_menu():
    titulo("MODELO DEL STN COLOMBIANO")
    # La banda del punto activo va ARRIBA y en todas las pantallas. Una salida
    # sin saber de que caso es, es una salida que acaba comparada con otra que
    # no le corresponde.
    aval, color = _AVAL[_PUNTO['tipo']]
    print()
    print(f"  {_c('PUNTO DE OPERACION:', '1;96')} "
          f"{_c(_PUNTO['etiqueta'], '1;97')}")
    print(f"  {_c(aval, color)}")
    print()
    i = 0
    for nombre, accion, _ in MENU:
        if accion is None:
            print(_c(f"  {nombre}", '1;93'))
        else:
            i += 1
            print(f"    {_c(f'{i:2d}', '96')}) {nombre}")
    print()
    print(f"    {_c(' p', '96')}) cambiar el punto de operacion")
    print(f"    {_c(' q', '96')}) salir")
    if 'n' in _BASE:
        print(_c("\n   (caso en memoria: las consultas son inmediatas)", '90'))


def main():
    # el modelo lee rutas relativas, asi que hay que estar en su carpeta
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    import logging
    logging.disable(logging.INFO)

    acciones = [(n, a, d) for n, a, d in MENU if a is not None]

    # Lo primero es sobre QUE sistema se trabaja, no que analisis se corre.
    elegir_punto(primera_vez=True)

    while True:
        pintar_menu()
        try:
            sel = input(_c("\n   opcion: ", '96')).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if sel in ('q', 'salir', 'exit'):
            print(_c("\n   hasta luego\n", '90'))
            return 0
        if sel in ('p', 'punto'):
            titulo("CAMBIAR EL PUNTO DE OPERACION")
            print()
            if elegir_punto():
                print(_c("\n   punto cambiado; el caso en memoria se "
                         "descarto", '90'))
            pausa()
            continue
        if not sel:
            # un Enter de mas no debe cerrar la sesion: tirarla cuesta los
            # ~20 s de reconstruir el caso base cacheado.
            continue
        if not sel.isdigit() or not (1 <= int(sel) <= len(acciones)):
            print(_c("   opcion no valida", '91'))
            continue

        nombre, accion, explica = acciones[int(sel) - 1]
        titulo(nombre.upper())
        print()
        parrafo(explica)
        try:
            accion()
        except KeyboardInterrupt:
            print(_c("\n   interrumpido", '93'))
        except Exception as e:                               # noqa: BLE001
            print(_c(f"\n   ERROR: {type(e).__name__}: {e}", '91'))
            import traceback
            traceback.print_exc(limit=3)
        pausa()


if __name__ == '__main__':
    sys.exit(main())
