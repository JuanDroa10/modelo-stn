# comparar_casos.py
# Somete DOS puntos de operacion a las mismas pruebas y los pone en columnas.
#
# -------------------------------------------------------------------------
# PARA QUE
# -------------------------------------------------------------------------
# Cada modulo acepta ya `--diurno` y `--fecha`, asi que las dos salidas
# existen. Pero son dos listados largos, y cruzarlos a mano es justo donde se
# cuelan los errores de lectura: una cifra que se compara contra la casilla
# equivocada parece un hallazgo. Esto corre lo mismo sobre los dos y lo
# imprime lado a lado, con la variacion ya calculada.
#
# -------------------------------------------------------------------------
# QUE PAR SE COMPARA
# -------------------------------------------------------------------------
# CUALQUIERA de los que el modelo sabe construir, uno en cada columna:
#
#     punta              la punta nocturna 18-22 h
#     diurno:11          el punto diurno derivado, a la hora HourNN que se diga
#     2026-10-20:5       el despacho y el pronostico de XM de esa fecha y ese
#                        periodo horario
#
# Durante mucho tiempo el par estaba CABLEADO a punta-contra-diurno, que es la
# comparacion con la que nacio el modulo. Pero la pregunta que mas se hace
# sobre una fecha descargada no es esa: es «en que se diferencia la hora
# critica de la punta de ESE dia», y eso son dos periodos del mismo fichero de
# XM. Ahora las dos columnas se eligen.
#
# -------------------------------------------------------------------------
# QUE SE COMPARA Y QUE NO
# -------------------------------------------------------------------------
# Se comparan las magnitudes que dependen del PUNTO DE OPERACION: el balance
# de potencia, las tensiones, la cargabilidad, los parametros de la ecuacion
# de oscilacion y la respuesta a un evento.
#
# NO se compara la validacion contra PowerFactory, y no por descuido: el caso
# de referencia es de punta y no existe otro. El diurno y las fechas heredan
# la validacion de la RED ---topologia, impedancias, limites térmicos--- pero
# no la del punto de operacion. Decir "validado" de las dos columnas seria
# falso.
#
# EL EVENTO ES EL MISMO EN LOS DOS CASOS, en MW absolutos. Esa es la
# comparacion que interesa: la MISMA perturbacion sobre dos sistemas de
# inercia distinta. Que en terminos relativos pese mas en el punto mas flojo
# no es un sesgo del montaje, es el resultado.

import argparse
import os
import sys

import numpy as np
import pandas as pd

from stn import punto_operacion as PO
from stn.config import salida


# Evento de referencia. 3.000 MW es el que usan por defecto el menu y los
# informes, asi que las cifras de aqui son directamente comparables con las
# que ya estan publicadas.
EVENTO_MW = 3000.0


# Rotulo corto de cada columna, para las cabeceras. Es un global de modulo y
# no un parametro que se pase por las treinta llamadas a `_cab()`: lo fija
# `construir_los_dos()` una vez y no vuelve a cambiar en toda la ejecucion.
_ETQ = ['A', 'B']


# ---------------------------------------------------------------------------
# El par de puntos
# ---------------------------------------------------------------------------

def leer_punto(txt):
    """Traduce 'punta' / 'diurno:11' / '2026-10-20:5' a argumentos de PO.

    Un solo token por columna, y no un juego de banderas por cada lado
    (`--a-fecha`, `--a-periodo`, `--a-diurno`...), porque la orden entera
    tiene que caber en una linea legible: es la que el menu enseña antes de
    lanzarla y la que acaba copiada en un cuaderno.
    """
    t = str(txt).strip().lower()
    if t in ('punta', 'p', ''):
        return {'hora': None, 'fecha': None}
    if t.startswith('diurno'):
        _, _, h = t.partition(':')
        try:
            return {'hora': int(h) if h else 11, 'fecha': None}
        except ValueError:
            raise ValueError(f"'{txt}': despues de 'diurno:' va la hora, la "
                             f"clave HourNN de XM (por ejemplo diurno:11)")
    fecha, _, per = txt.strip().partition(':')
    try:
        periodo = int(per) if per else 19
    except ValueError:
        raise ValueError(f"'{txt}': el periodo despues de ':' tiene que ser "
                         f"un numero de 1 a 24")
    if not 1 <= periodo <= 24:
        raise ValueError(f"'{txt}': el periodo horario va de 1 a 24")
    if len(fecha) != 10 or fecha.count('-') != 2:
        raise ValueError(
            f"'{txt}' no es un punto de operacion. Usa 'punta', 'diurno:HORA' "
            f"o 'AAAA-MM-DD:PERIODO'.")
    return {'hora': None, 'fecha': fecha, 'periodo': periodo}


def _rotulo(kw, otro):
    """Nombre corto de la columna, como maximo 12 caracteres.

    Mira TAMBIEN el otro punto: comparando dos periodos de la misma fecha, la
    fecha es lo unico que las dos columnas comparten y repetirla en las dos
    cabeceras gasta el ancho sin distinguir nada. Entonces basta el periodo.
    """
    if kw.get('fecha'):
        if otro.get('fecha') == kw['fecha']:
            return f"periodo {kw.get('periodo', 19)}"
        return f"{kw['fecha'][5:]} p{kw.get('periodo', 19)}"
    if kw.get('hora') is None:
        return 'PUNTA'
    return f"DIURNO {kw['hora']}"


# ---------------------------------------------------------------------------
# Utilidades de impresion
# ---------------------------------------------------------------------------

def _titulo(txt):
    print()
    print('=' * 78)
    print(f"  {txt}")
    print('=' * 78)


def _cab():
    print(f"  {'':32s} {_ETQ[0]:>12s} {_ETQ[1]:>12s}      var")
    print("  " + "-" * 64)


def _fila(nombre, a, b, fmt='{:12.1f}', uni='', rel=True):
    """Una fila de la tabla. `rel=False` para magnitudes donde el % no dice nada."""
    if a is None or b is None:
        p = '-' if a is None else fmt.format(a)
        d = '-' if b is None else fmt.format(b)
        print(f"  {nombre:32s} {p:>12s} {d:>12s}            {uni}")
        return
    var = ''
    if rel and abs(a) > 1e-9:
        var = f"{(b - a) / a * 100:+7.1f} %"
    elif not rel:
        var = f"{b - a:+7.2f}  "
    print(f"  {nombre:32s} {fmt.format(a)} {fmt.format(b)}   {var}  {uni}")


def _texto(nombre, a, b):
    print(f"  {nombre:32s} {str(a):>12s} {str(b):>12s}")


# ---------------------------------------------------------------------------
# Las pruebas
# ---------------------------------------------------------------------------

def construir_los_dos(a, b, verbose=False):
    """Construye las dos columnas. `a` y `b` son kwargs de `PO.construir`.

    Devuelve {'a': {...}, 'b': {...}} con red, diagnostico, contexto y
    Sistema de cada una, y deja fijados de paso los rotulos de cabecera.
    """
    from stn import agc_deslastre as AGC
    _ETQ[0], _ETQ[1] = _rotulo(a, b), _rotulo(b, a)
    casos = {}
    for clave, kw in (('a', a), ('b', b)):
        etq = PO.etiqueta(kw.get('hora'), kw.get('fecha'),
                          kw.get('periodo', 19))
        print(f"  construyendo {etq}...", flush=True)
        red, diag, ctx = PO.construir(verbose=verbose, **kw)
        casos[clave] = {'red': red, 'diag': diag, 'ctx': ctx,
                        'sis': AGC.Sistema(red)}
    return casos


def _solar_embebida(caso):
    """MW de solar DETRAS del contador produciendo en ese punto, o None.

    Solo el punto diurno la conoce con nombre y apellidos: se construye
    restandola de la demanda, asi que la cifra esta en su `info`. La punta
    nocturna tiene cero por definicion. Una fecha de XM NO lo sabe ---el
    PRON_BARRA es demanda NETA y el dDEC no lleva lo embebido---, y ahi la
    respuesta honesta es «no se», no un cero: un cero diria que no hay, que
    es una afirmacion que el dato no sostiene.
    """
    ctx = caso['ctx']
    if ctx.get('fecha'):
        return None
    if ctx.get('hora') is None:
        return 0.0
    return float((ctx.get('info') or {}).get('solar_produccion_MW', 0.0))


def _factor_inversor(caso):
    """Que fraccion de su placa entrega la fotovoltaica en ese punto.

    Es el factor que `deslastre_btm` aplica a la generacion detras del
    contador antes de tirarla: la placa NO es la produccion, y confundirlas
    infla el BTM sano que el MILP cree estar salvando.
    """
    ctx = caso['ctx']
    if ctx.get('fecha'):
        from stn.fuentes.xm_snapshot import FuenteXM
        return float(FuenteXM(ctx['fecha'], ctx['periodo'],
                              verbose=False).produccion_solar_pu()[2])
    if ctx.get('hora') is None:
        return 0.0
    return float((ctx.get('info') or {}).get('produccion_solar_pu') or 0.0)


def bloque_red(c):
    _titulo("1. LA RED")
    p, d = c['a']['red'], c['b']['red']
    _cab()
    _fila('buses', len(p.buses), len(d.buses), '{:12d}', rel=False)
    _fila('lineas', len(p.lines), len(d.lines), '{:12d}', rel=False)
    _fila('transformadores', len(p.transformers), len(d.transformers),
          '{:12d}', rel=False)
    _fila('generadores', len(p.generators), len(d.generators), '{:12d}',
          rel=False)
    _fila('cargas', len(p.loads), len(d.loads), '{:12d}', rel=False)
    print()
    # Los GENERADORES van aparte de la topologia, y no es un detalle de
    # presentacion: una maquina esta en el modelo si el despacho la pone a
    # producir esa hora, asi que dos periodos del mismo dia tienen casi
    # siempre un numero distinto. Metido en el mismo recuento, ese cambio
    # normal disparaba el aviso de "no es la misma red" y acusaba a PARATEC
    # de un censo cambiante que no habia cambiado.
    if all(len(getattr(p, t)) == len(getattr(d, t))
           for t in ('buses', 'lines', 'transformers', 'loads')):
        print("  La TOPOLOGIA es la misma en las dos columnas: mismos buses,")
        print("  mismas lineas, mismos trafos y mismas cargas. Lo que separe a")
        print("  las dos de aqui en adelante es del punto de operacion.")
    else:
        # Dos fechas distintas NO tienen por que traer el mismo directorio: a
        # PARATEC se le dan de alta plantas cada pocos dias. Callarlo haria
        # leer como diferencia de hora lo que es diferencia de censo.
        print("  [!] Las dos columnas NO tienen la misma topologia. Dos fechas")
        print("      de XM pueden traer un directorio de PARATEC distinto ---una")
        print("      planta nueva, un circuito que aparece---, asi que parte de")
        print("      lo que sigue es diferencia de RED y no de hora.")
    if len(p.generators) != len(d.generators):
        print()
        print(f"  El numero de GENERADORES si cambia ({len(p.generators)} frente "
              f"a {len(d.generators)}),")
        print("  y eso no es un defecto: una maquina esta en el modelo si el")
        print("  despacho la pone a producir esa hora. Ese compromiso de unidades")
        print("  es, de hecho, lo que mueve la inercia de una hora a otra.")


def bloque_potencia(c):
    _titulo("2. BALANCE DE POTENCIA")
    p, d = c['a'], c['b']
    rp, rd = p['red'], d['red']

    gen_p = float(rp.generators_t.p.iloc[0].sum())
    gen_d = float(rd.generators_t.p.iloc[0].sum())
    car_p = float(rp.loads_t.p.iloc[0].sum())
    car_d = float(rd.loads_t.p.iloc[0].sum())

    sol_p, sol_d = _solar_embebida(p), _solar_embebida(d)

    _cab()
    _fila('demanda BRUTA',
          None if sol_p is None else car_p + sol_p,
          None if sol_d is None else car_d + sol_d, uni='MW')
    _fila('solar embebida produciendo', sol_p, sol_d, uni='MW', rel=False)
    _fila('demanda NETA del STN', car_p, car_d, uni='MW')
    _fila('generacion sincrona resuelta', gen_p, gen_d, uni='MW')
    _fila('perdidas de red', gen_p - car_p, gen_d - car_d, uni='MW')
    _fila('perdidas, % de la generacion',
          100 * (gen_p - car_p) / gen_p, 100 * (gen_d - car_d) / gen_d,
          '{:12.2f}', uni='%', rel=False)
    print()
    if sol_p is None or sol_d is None:
        # Un guion en la casilla es una pregunta sin responder, y quien la lea
        # merece saber por que no se responde en vez de suponer que es cero.
        print("  La demanda BRUTA sale en blanco en las columnas que vienen de")
        print("  una fecha de XM: el PRON_BARRA publica demanda NETA y el dDEC no")
        print("  lleva lo embebido, asi que el modelo no tiene con que separar la")
        print("  una de la otra. Lo que SI es comparable entre columnas es la")
        print("  demanda neta, que es la que el STN ve.")
    else:
        print("  La solar NO es un generador del modelo: ninguna de las 370 plantas")
        print("  conecta a 200 kV o mas. Desde el STN se ve como MENOS CARGA, que es")
        print("  como la ve el operador. De ahi que la generacion sincrona baje mucho")
        print("  mas que la demanda.")

    print()
    print("  MEZCLA POR TECNOLOGIA (MW despachados)")
    mp = p['sis'].gen.groupby('tipo').p_set.sum()
    md = d['sis'].gen.groupby('tipo').p_set.sum()
    _cab()
    for t in sorted(set(mp.index) | set(md.index),
                    key=lambda x: -float(mp.get(x, 0.0))):
        _fila(f"  {t}", float(mp.get(t, 0.0)), float(md.get(t, 0.0)), uni='MW')


def bloque_estado(c):
    _titulo("3. ESTADO DE LA RED RESUELTA")
    p, d = c['a'], c['b']
    dp, dd = p['diag'], d['diag']
    vp = p['red'].buses_t.v_mag_pu.iloc[0].astype(float)
    vd = d['red'].buses_t.v_mag_pu.iloc[0].astype(float)

    _cab()
    _fila('tension minima', dp['v_min'], dd['v_min'], '{:12.4f}', uni='pu',
          rel=False)
    _fila('tension maxima', dp['v_max'], dd['v_max'], '{:12.4f}', uni='pu',
          rel=False)
    _fila('tension media', float(vp.mean()), float(vd.mean()), '{:12.4f}',
          uni='pu', rel=False)
    _fila('barras bajo 0,90 pu', len(dp['v_bajas']), len(dd['v_bajas']),
          '{:12d}', rel=False)
    _fila('barras sobre 1,10 pu', len(dp['v_altas']), len(dd['v_altas']),
          '{:12d}', rel=False)
    _fila('cargabilidad maxima', dp['carga_max_pct'], dd['carga_max_pct'],
          '{:12.1f}', uni='%', rel=False)
    pp = np.hypot(p['red'].lines_t.p0.iloc[0], p['red'].lines_t.q0.iloc[0])
    pd_ = np.hypot(d['red'].lines_t.p0.iloc[0], d['red'].lines_t.q0.iloc[0])
    _fila('flujo |S| maximo en linea', float(pp.max()), float(pd_.max()),
          uni='MVA')
    _fila('generadores conmutados a PQ',
          int((p['red'].generators.control == 'PQ').sum()),
          int((d['red'].generators.control == 'PQ').sum()), '{:12d}', rel=False)

    # El sentido del cambio de tension no es obvio y conviene dejarlo escrito.
    # Cual de las dos columnas sube se LEE de las cifras: cableado a "el
    # diurno", el comentario seguia afirmandolo con cualquier otro par.
    if abs(dd['v_max'] - dp['v_max']) > 1e-4:
        arriba = _ETQ[1] if dd['v_max'] > dp['v_max'] else _ETQ[0]
        print()
        print(f"  Las tensiones son mas altas en {arriba}, y suele ser lo")
        print("  esperado en el punto mas descargado: menos flujo es menos caida")
        print("  en las reactancias serie, y las lineas descargadas aportan")
        print("  reactivo capacitivo. Vigilar el margen contra 1,10 pu.")
    for caso, etq in ((p, _ETQ[0]), (d, _ETQ[1])):
        neg = (caso['ctx'].get('info') or {}).get('barras_exportadoras')
        if neg is None or not len(neg):
            continue
        print()
        print(f"  {len(neg)} barras quedan con carga NETA NEGATIVA en {etq} "
              f"({float(neg.sum()):.1f} MW):")
        for b, v in neg.sort_values().items():
            print(f"      {b:30s} {v:8.1f} MW")
        print("  La solar local supera a la demanda local y la barra EXPORTA al")
        print("  STN. Es un flujo inverso real, no un error de signo.")


def bloque_frecuencia(c):
    _titulo("4. LO QUE DECIDE LA FRECUENCIA")
    sp, sd = c['a']['sis'], c['b']['sis']
    _cab()
    _fila('capacidad instalada', sp.S_instalada, sd.S_instalada, uni='MVA')
    _fila('capacidad ACOPLADA (S_base)', sp.S_base, sd.S_base, uni='MVA')
    _fila('fraccion acoplada', 100 * sp.frac_acoplada, 100 * sd.frac_acoplada,
          '{:12.1f}', uni='%', rel=False)
    _fila('inercia equivalente H', sp.H, sd.H, '{:12.3f}', uni='s')
    _fila('energia cinetica H*S', sp.H * sp.S_base, sd.H * sd.S_base,
          '{:12.0f}', uni='MVA.s')
    _fila('estatismo equivalente R_eq', sp.R_eq, sd.R_eq, '{:12.4f}',
          rel=False)
    _fila('reserva primaria a subir', sp.reserva, sd.reserva, uni='MW')
    _fila('reserva, % del despacho', 100 * sp.reserva / sp.P_gen,
          100 * sd.reserva / sd.P_gen, '{:12.1f}', uni='%', rel=False)
    _fila('reserva a bajar', sp.reserva_baja, sd.reserva_baja, uni='MW')
    _fila('maquinas con H de PARATEC', sp.n_H_real, sd.n_H_real, '{:12d}',
          rel=False)

    print()
    print("  ESTA ES LA COMPARACION QUE IMPORTA: la energia cinetica H*S es lo")
    print("  que fija el RoCoF y el nadir ante la misma perturbacion.")
    # El veredicto se LEE de las dos columnas en vez de estar escrito de
    # antemano. Con "el diurno tiene menos demanda y aun asi es mas fragil"
    # fijo en el codigo, cualquier otro par de horas seguia leyendo esa frase
    # aunque sus cifras dijeran justo lo contrario.
    ea, eb = sp.H * sp.S_base, sd.H * sd.S_base
    floja = _ETQ[0] if ea < eb else _ETQ[1]
    if (sp.P_carga - sd.P_carga) * (ea - eb) < 0:
        print(f"  Ojo al sentido: {floja} tiene MAS demanda que la otra columna")
        print("  y aun asi MENOS energia cinetica. Cuando la demanda la tapa")
        print("  generacion que no acopla masa, el sistema queda mas flojo pese")
        print("  a ser mayor.")
    else:
        print(f"  La columna mas floja es {floja}, que es tambien la de menos")
        print("  demanda. No se lee al reves: el evento se mide en MW ABSOLUTOS")
        print("  ---la mayor maquina no encoge con la demanda--- y frente a eso")
        print("  lo unico que cuenta es la energia cinetica que haya acoplada.")
        print("  Menos demanda no es menos riesgo.")


def bloque_evento(c, mw=EVENTO_MW):
    from stn import agc_deslastre as AGC
    _titulo(f"5. RESPUESTA A LA MISMA PERTURBACION ({mw:.0f} MW)")
    print("  Mismo evento en MW absolutos sobre los dos sistemas. En el mas")
    print("  pequeno pesa mas en terminos relativos, y eso es el resultado, no")
    print("  un sesgo del montaje.")
    print()

    out = {}
    for k in ('a', 'b'):
        s = c[k]['sis']
        t, f, esc, tr = AGC.simular_frecuencia(s, deficit_mw=mw, con_edac=True)
        out[k] = {'t': t, 'f': f, 'esc': esc, 'tr': tr, 'sis': s}

    p, d = out['a'], out['b']
    tp, td = p['tr'], d['tr']
    _cab()
    _fila('perdida, % del despacho', 100 * mw / p['sis'].P_gen,
          100 * mw / d['sis'].P_gen, '{:12.1f}', uni='%', rel=False)
    _fila('RoCoF inicial', tp['rocof'], td['rocof'], '{:12.4f}', uni='Hz/s')
    _fila('escalones EDAC que disparan',
          sum(1 for e in p['esc'] if e.get('disparado')),
          sum(1 for e in d['esc'] if e.get('disparado')), '{:12d}', rel=False)
    _fila('carga deslastrada', tp['deslastre_total'], td['deslastre_total'],
          uni='MW')
    _fila('deslastre, % de la demanda',
          100 * tp['deslastre_total'] / p['sis'].P_carga,
          100 * td['deslastre_total'] / d['sis'].P_carga, '{:12.1f}', uni='%',
          rel=False)
    _fila('aporte de la primaria', tp['aporte_primaria'], td['aporte_primaria'],
          uni='MW')
    _fila('frecuencia final', tp['f_final'], td['f_final'], '{:12.3f}',
          uni='Hz', rel=False)
    _texto('colapsa (< 57,5 Hz)', 'SI' if tp['colapso'] else 'no',
           'SI' if td['colapso'] else 'no')

    # El nadir SOLO se publica si no hay colapso. Si lo hay, el minimo que
    # devuelve la traza es donde el integrador corta, no un nadir fisico:
    # citarlo seria inventar una cifra.
    if not tp['colapso'] and not td['colapso']:
        _fila('nadir', tp['nadir'], td['nadir'], '{:12.3f}', uni='Hz',
              rel=False)
        _fila('instante del nadir', tp['t_nadir'], td['t_nadir'], '{:12.2f}',
              uni='s', rel=False)
    else:
        print()
        print("  [!] Al menos uno de los dos COLAPSA. El minimo de la traza es")
        print("      donde corta el integrador (57,5 Hz), no un nadir real: no")
        print("      se publica ninguna de las dos cifras, porque dejarian de")
        print("      ser comparables entre si.")
    return out


def bloque_contingencia_critica(c):
    _titulo("6. LA CONTINGENCIA MAS GRANDE DE CADA CASO")
    print("  No es el mismo evento: es el MAYOR generador despachado en cada")
    print("  punto, que es la contingencia de diseño que le corresponde a cada")
    print("  uno. El punto mas pequeno puede salir mejor parado aqui, porque su")
    print("  mayor maquina tambien es mas pequena.")
    print()
    from stn import agc_deslastre as AGC
    _cab()
    datos = {}
    for k in ('a', 'b'):
        s = c[k]['sis']
        pm = s.gen.p_set.astype(float)
        g = pm.idxmax()
        mw = float(pm.max())
        _, _, esc, tr = AGC.simular_frecuencia(s, deficit_mw=mw, con_edac=True)
        datos[k] = {'gen': g, 'mw': mw, 'tr': tr, 'esc': esc}
    p, d = datos['a'], datos['b']
    _texto('mayor maquina', p['gen'][:12], d['gen'][:12])
    _fila('potencia que pierde', p['mw'], d['mw'], uni='MW')
    _fila('RoCoF inicial', p['tr']['rocof'], d['tr']['rocof'], '{:12.4f}',
          uni='Hz/s')
    _fila('escalones EDAC', sum(1 for e in p['esc'] if e.get('disparado')),
          sum(1 for e in d['esc'] if e.get('disparado')), '{:12d}', rel=False)
    _fila('carga deslastrada', p['tr']['deslastre_total'],
          d['tr']['deslastre_total'], uni='MW')
    if not p['tr']['colapso'] and not d['tr']['colapso']:
        _fila('nadir', p['tr']['nadir'], d['tr']['nadir'], '{:12.3f}',
              uni='Hz', rel=False)


def bloque_areas(c):
    _titulo("7. BALANCE POR AREA OPERATIVA")
    from stn import zonas
    rp = zonas.resumen(c['a']['red'])
    rd = zonas.resumen(c['b']['red'])
    ea, eb = _ETQ
    # Cabecera de DOS lineas, con el grupo arriba y el rotulo de cada columna
    # debajo. Con 'GEN punta' / 'CARGA diurno' en una sola linea bastaba un
    # rotulo un poco mas largo ---'periodo 19'--- para que el titulo se saliera
    # de su casilla y dejara de cuadrar con los numeros de debajo.
    w = max(9, len(ea), len(eb))
    print(f"  {'':16s}" + ''.join(f" {g:^{2*w+1}s}"
                                  for g in ('GEN', 'CARGA', 'INTERCAMBIO')))
    print(f"  {'area':16s}" + ''.join(f" {t:>{w}s}"
                                      for t in (ea, eb, ea, eb, ea, eb)))
    raya = "  " + "-" * (16 + 6 * (w + 1))
    print(raya)
    for a in rp.index:
        if a not in rd.index:
            continue
        print(f"  {a:16s}" + ''.join(
            f" {v:>{w}.1f}" for v in (rp.at[a, 'GenP'], rd.at[a, 'GenP'],
                                      rp.at[a, 'LoadP'], rd.at[a, 'LoadP'],
                                      rp.at[a, 'InterP'], rd.at[a, 'InterP'])))
    print(raya)
    print(f"  {'TOTAL':16s}" + ''.join(
        f" {v:>{w}.1f}" for v in (rp.GenP.sum(), rd.GenP.sum(),
                                  rp.LoadP.sum(), rd.LoadP.sum(),
                                  rp.InterP.sum(), rd.InterP.sum())))
    print()
    print("  InterP es el intercambio neto: positivo exporta, negativo importa.")
    print("  Un cambio de SIGNO entre los dos casos significa que el area pasa de")
    print("  importadora a exportadora (o al reves) segun la hora.")
    for a in rp.index:
        if a in rd.index and np.sign(rp.at[a, 'InterP']) != np.sign(rd.at[a, 'InterP']):
            print(f"      {a}: {rp.at[a,'InterP']:+.1f} -> "
                  f"{rd.at[a,'InterP']:+.1f} MW   CAMBIA DE SENTIDO")


def bloque_n1(c, correr=False):
    _titulo("8. BARRIDO N-1")
    # El nombre del CSV sale del SUFIJO del punto, que es quien garantiza que
    # dos casos no se pisen el fichero. Escrito a mano ---'_diurno{hora}'---
    # este bloque solo sabia leer el par punta/diurno y habria buscado el
    # fichero equivocado en cuanto una columna fuese una fecha.
    pc = salida(f"contingencias_n1{c['a']['ctx']['sufijo']}.csv")
    dc = salida(f"contingencias_n1{c['b']['ctx']['sufijo']}.csv")

    if correr:
        from stn import contingencias as C
        for k, dest in (('a', pc), ('b', dc)):
            print(f"  barriendo {c[k]['ctx']['etiqueta']}... (esto tarda)",
                  flush=True)
            base, diag = c[k]['red'], c[k]['diag']
            cand = C.elementos_de(base, ('Line', 'Transformer'))
            r = C.barrido(base, diag, cand, verbose=False)
            r.to_csv(dest, index=False, encoding='utf-8-sig')

    if not (os.path.exists(pc) and os.path.exists(dc)):
        print("  Faltan resultados. Vuelve a llamar a este modulo con --n1")
        print("  (tarda unos minutos por columna), o genera cada barrido antes:")
        for caso in (c['a'], c['b']):
            ctx = caso['ctx']
            if ctx.get('fecha'):
                bandera = f"--fecha {ctx['fecha']} --periodo {ctx['periodo']}"
            elif ctx.get('hora') is not None:
                bandera = f"--diurno {ctx['hora']}"
            else:
                bandera = ''
            print(f"      python -m stn.contingencias {bandera}".rstrip())
        return

    dp, dd = pd.read_csv(pc), pd.read_csv(dc)
    _cab()
    _fila('casos simulados', len(dp), len(dd), '{:12d}', rel=False)
    for col, etq in (('convergio', 'casos que convergen'),
                     ('n_sobrecargas', 'casos con sobrecarga')):
        if col not in dp.columns or col not in dd.columns:
            continue
        if col == 'convergio':
            _fila(etq, int(dp[col].sum()), int(dd[col].sum()), '{:12d}',
                  rel=False)
        else:
            _fila(etq, int((dp[col] > 0).sum()), int((dd[col] > 0).sum()),
                  '{:12d}', rel=False)
    if 'carga_max_pct' in dp.columns:
        _fila('cargabilidad maxima vista', float(dp.carga_max_pct.max()),
              float(dd.carga_max_pct.max()), '{:12.1f}', uni='%', rel=False)
    if 'islas' in dp.columns:
        _fila('casos que parten la red', int((dp.islas > 1).sum()),
              int((dd.islas > 1).sum()), '{:12d}', rel=False)

    # LAS TENSIONES SUELEN SER EL MODO DE FALLO DEL PUNTO DESCARGADO, y por
    # eso se desglosan aqui en vez de resumirse en un "sin problemas". Un
    # punto cargado se queda corto por abajo y uno descargado se pasa por
    # arriba: son dos riesgos distintos, y un recuento agregado los daria por
    # equivalentes.
    if 'n_v_bajas' in dp.columns and 'n_v_altas' in dp.columns:
        _fila('casos con tension < 0,90 pu', int((dp.n_v_bajas > 0).sum()),
              int((dd.n_v_bajas > 0).sum()), '{:12d}', rel=False)
        _fila('casos con tension > 1,10 pu', int((dp.n_v_altas > 0).sum()),
              int((dd.n_v_altas > 0).sum()), '{:12d}', rel=False)
        _fila('tension maxima vista', float(dp.v_max.max()),
              float(dd.v_max.max()), '{:12.4f}', uni='pu', rel=False)
        _fila('tension minima vista', float(dp.v_min.min()),
              float(dd.v_min.min()), '{:12.4f}', uni='pu', rel=False)
        for df, caso, etq, otro in ((dp, c['a'], _ETQ[0], dd),
                                    (dd, c['b'], _ETQ[1], dp)):
            altas = df[df.n_v_altas > 0]
            if not len(altas):
                continue
            print()
            # v_max del CASO BASE de esa columna, no el maximo del barrido:
            # son dos cifras distintas y confundirlas haria creer que el punto
            # de partida ya viola el limite.
            v0 = float(caso['diag']['v_max'])
            n_otro = int((otro.n_v_altas > 0).sum())
            print(f"  En {etq} hay {len(altas)} contingencias que suben alguna "
                  f"barra por encima de")
            print(f"  1,10 pu (en la otra columna, {n_otro}). Ese caso base ya "
                  f"parte de {v0:.4f} pu SIN")
            print(f"  contingencia, asi que el margen por arriba es de "
                  f"{1.10-v0:.4f} pu y cualquier")
            print("  apertura lo consume:")
            for _, r in altas.sort_values('v_max', ascending=False).iterrows():
                print(f"      {str(r.contingencia)[:44]:46s} v_max "
                      f"{r.v_max:.4f} pu  ({int(r.n_v_altas)} barras)")
            print("  Es SOBRETENSION por red descargada, no un colapso. Un sistema")
            print("  real lo resuelve con reactores o bajando taps; el modelo no")
            print("  tiene ni lo uno ni lo otro, asi que lo ensena crudo.")


def bloque_btm(c):
    _titulo("9. EL POOL DE GENERACION DETRAS DEL CONTADOR")
    from stn import deslastre_btm as D
    # `cargar_registrada` devuelve (serie por zona, desglose por registro).
    total, por_registro = D.cargar_registrada('agpe', por='subarea')
    reg = float(total.sum())
    # Factor de produccion de la hora: que fraccion de su placa entrega de
    # verdad la fotovoltaica. De noche vale CERO, y entonces no hay evento.
    pu_a, pu_b = _factor_inversor(c['a']), _factor_inversor(c['b'])

    _cab()
    _fila('BTM registrado (placa)', reg, reg, uni='MW', rel=False)
    for k, v in sorted(por_registro.items()):
        _fila(f'  de los cuales {k}', float(v), float(v), uni='MW', rel=False)
    _fila('factor solar de la hora', pu_a, pu_b, '{:12.3f}', rel=False)
    _fila('BTM solar que PRODUCE', reg * pu_a, reg * pu_b, uni='MW', rel=False)
    print()
    # El umbral es el 1 % de la placa y no un cero exacto: a las siete de la
    # tarde el despacho deja 0,002 pu de residuo solar, que no es produccion
    # sino el rastro de la curva apagandose. Con `pu > 0` como criterio, esa
    # hora pasaba por tener sol.
    muertas = [e for e, pu in ((_ETQ[0], pu_a), (_ETQ[1], pu_b)) if pu < 0.01]
    if len(muertas) == 2:
        print("  La fotovoltaica da CERO en LAS DOS columnas: el evento central")
        print("  del trabajo ---la desconexion en bloque de la generacion detras")
        print("  del contador--- no puede ocurrir en ninguna de las dos, y el")
        print("  algoritmo no tiene nada que decidir aqui.")
    elif muertas:
        print(f"  La fotovoltaica da CERO en {muertas[0]}: el evento central del")
        print("  trabajo ---la desconexion en bloque de la generacion detras del")
        print("  contador--- no puede ocurrir ahi. Por eso las dos columnas no son")
        print("  intercambiables: el algoritmo solo tiene algo que decidir en una.")
    else:
        print("  Las dos columnas tienen sol, asi que el evento central del")
        print("  trabajo puede plantearse en las dos y la comparacion es de")
        print("  cuanto pesa, no de si ocurre.")
    print()
    print("  Ojo con la placa: el pool registrado NO es solo solar. El analisis")
    print("  completo de cada columna sale de:")
    for caso in (c['a'], c['b']):
        ctx = caso['ctx']
        if ctx.get('fecha'):
            bandera = f"--fecha {ctx['fecha']} --periodo {ctx['periodo']}"
        elif ctx.get('hora') is not None:
            bandera = f"--diurno {ctx['hora']}"
        else:
            bandera = ''
        print(f"      python -m stn.deslastre_btm {bandera}".rstrip())


def main():
    ap = argparse.ArgumentParser(
        description="Somete DOS puntos de operacion a las mismas pruebas y "
                    "los imprime lado a lado.",
        epilog="Cada columna se escribe como 'punta', 'diurno:HORA' o "
               "'AAAA-MM-DD:PERIODO'. Ejemplos: "
               "--caso-a 2026-10-20:5 --caso-b 2026-10-20:19   (la hora "
               "critica contra la punta de ese dia); "
               "--caso-a punta --caso-b diurno:11   (el par clasico, y el "
               "que sale si no se dice nada).")
    ap.add_argument('--caso-a', default=None, metavar='PUNTO',
                    help="punto de la columna izquierda (defecto: punta)")
    ap.add_argument('--caso-b', default=None, metavar='PUNTO',
                    help="punto de la columna derecha (defecto: diurno:HORA)")
    ap.add_argument('--hora', type=int, default=11,
                    help="hora del caso diurno cuando no se usan --caso-a y "
                         "--caso-b, clave HourNN de XM (defecto 11)")
    ap.add_argument('--evento-mw', type=float, default=EVENTO_MW,
                    help="perturbacion de referencia en MW (defecto 3000)")
    ap.add_argument('--n1', action='store_true',
                    help="correr el barrido N-1 aqui en vez de leer los CSV "
                         "ya generados (tarda varios minutos por caso)")
    ap.add_argument('--verbose', action='store_true',
                    help="enseñar el informe completo de cada constructor")
    args = ap.parse_args()

    # Sin --caso-a/--caso-b el par es el de siempre, punta contra diurno. La
    # orden documentada en el README y los informes ya publicados tiene que
    # seguir dando exactamente lo que daba.
    try:
        a = leer_punto(args.caso_a if args.caso_a else 'punta')
        b = leer_punto(args.caso_b if args.caso_b else f'diurno:{args.hora}')
    except ValueError as e:
        print(f"ERROR: {e}")
        return 2
    if a == b:
        print("ERROR: las dos columnas son el mismo punto de operacion; no "
              "hay nada que comparar.")
        return 2

    print()
    print("Construyendo los dos puntos de operacion...")
    c = construir_los_dos(a, b, verbose=args.verbose)

    print()
    print('#' * 78)
    print(f"#  {c['a']['ctx']['etiqueta'].upper()}  frente a  "
          f"{c['b']['ctx']['etiqueta'].upper()}")
    print('#' * 78)
    print()
    # El respaldo de cada columna NO es el mismo y depende de cual sea: una
    # cabecera que dijera siempre "PUNTA medida / DIURNO derivada" mentiria en
    # cuanto una columna fuese una fecha de XM.
    for caso, etq in ((c['a'], _ETQ[0]), (c['b'], _ETQ[1])):
        ctx = caso['ctx']
        if ctx.get('fecha'):
            aval = ("CONSTRUIDA desde el despacho y el pronostico de XM; "
                    "hereda la validacion de la RED, no la del punto")
        elif ctx.get('hora') is None:
            aval = "MEDIDA y contrastada contra PowerFactory"
        else:
            aval = "DERIVADA de la punta con factores horarios de XM"
        print(f"  {etq:>12s}: {aval}")
    print()
    print("  Las dos corren el mismo codigo; no tienen por que tener el mismo")
    print("  respaldo empirico.")

    bloque_red(c)
    bloque_potencia(c)
    bloque_estado(c)
    bloque_frecuencia(c)
    bloque_evento(c, args.evento_mw)
    bloque_contingencia_critica(c)
    bloque_areas(c)
    bloque_n1(c, correr=args.n1)
    bloque_btm(c)

    _titulo("RESUMEN")
    sp, sd = c['a']['sis'], c['b']['sis']
    ea, eb = sp.H * sp.S_base, sd.H * sd.S_base
    def _cuanto(x, y):
        v = 100 * (y / x - 1)
        return f"un {abs(v):.1f} % {'MAS' if v >= 0 else 'MENOS'}"

    print(f"  Respecto de {_ETQ[0]}, la columna {_ETQ[1]} tiene "
          f"{_cuanto(sp.P_carga, sd.P_carga)} de")
    print(f"  demanda neta y {_cuanto(sp.S_base, sd.S_base)} de capacidad "
          f"acoplada.")
    print(f"  Su energia cinetica pasa de {ea:.0f} a {eb:.0f} MVA.s, asi que la "
          f"MISMA")
    print(f"  perturbacion le produce un RoCoF {ea/eb:.2f} veces el de "
          f"{_ETQ[0]}.")
    print()
    # La moraleja se escribe segun lo que digan las cifras, no de antemano.
    # Es la misma de siempre mientras la columna floja sea la pequena ---el
    # par punta-diurno, el caso con el que nacio el modulo---, pero deja de
    # serlo en cuanto la floja es la grande, y ahi decirlo igual seria
    # publicar una conclusion que el par comparado no sostiene.
    if (sp.P_carga - sd.P_carga) * (ea - eb) < 0:
        print("  Y es la columna GRANDE la que queda mas floja: aqui la demanda")
        print("  ni siquiera sirve de indicio del riesgo.")
    else:
        print("  Menos demanda no es menos riesgo. Es el punto del trabajo.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
