# punto_operacion.py
# Selector unico del punto de operacion sobre el que corre cada analisis.
#
# -------------------------------------------------------------------------
# POR QUE EXISTE
# -------------------------------------------------------------------------
# El modelo tiene DOS puntos de operacion y hasta ahora no estaban al mismo
# nivel. La punta nocturna era la unica que sabian construir `main`,
# `contingencias`, `agc_deslastre` y `limites_reactivos`; el diurno solo lo
# alcanzaban `cascada` y `deslastre_btm`, cada uno con su propio cableado
# copiado a mano. El resultado practico era que el caso diurno ---que es
# justo donde el fenomeno que estudia la tesis PUEDE ocurrir--- no tenia
# flujo de carga publicado, ni barrido N-1, ni diagnostico de reactivo, ni
# evento de frecuencia. No habia con que compararlo.
#
# Este modulo pone los dos puntos detras de la misma puerta. Cada modulo
# llama a `anadir_flag(ap)` y a `de_args(args)`, y a partir de ahi trabaja
# con una red resuelta sin saber de cual de los dos se trata.
#
# -------------------------------------------------------------------------
# LO QUE NO HACE, Y CONVIENE TENERLO DELANTE
# -------------------------------------------------------------------------
# Que los dos casos pasen por las mismas pruebas NO los pone al mismo nivel
# de evidencia. La punta esta MEDIDA y contrastada contra PowerFactory; el
# diurno es DERIVADO de ella con factores horarios de XM. Lo que el diurno
# hereda es la validacion de la RED (topologia, impedancias, limites), no la
# del punto de operacion. `validacion.py` sigue sin aceptar `--diurno` a
# proposito: no existe un caso de referencia diurno con el que contrastarlo,
# y un modulo que dijera "validado" sobre el diurno estaria mintiendo.
#
# Ver la cabecera de `escenario_diurno.py` para la transformacion completa.

import argparse


# Sufijos de fichero por caso. Sin esto, correr el diurno PISA los resultados
# de la punta ---el mismo contingencias_n1.csv, la misma frecuencia_evento.png---
# y acabas comparando un caso consigo mismo sin notarlo.
def sufijo(hora, fecha=None, periodo=None):
    """Sufijo para los ficheros de salida del caso. Vacio en la punta."""
    if fecha:
        return f"_{str(fecha).replace('-', '')}p{periodo}"
    return '' if hora is None else f'_diurno{hora}'


def etiqueta(hora, fecha=None, periodo=None):
    """Nombre legible del punto de operacion."""
    if fecha:
        return f'XM {fecha} periodo {periodo}'
    if hora is None:
        return 'punta nocturna (18-22 h)'
    from stn import escenario_diurno as ED
    try:
        return f"diurno {ED.factores(hora)['etiqueta']}"
    except KeyError:
        return f'diurno (HourNN={hora})'


def anadir_flag(ap, ayuda_extra=''):
    """Anade `--diurno [HORA]` y `--fecha/--periodo` al parser que se le pase.

    Sin valor, `--diurno` toma la hora 11 (10:00-11:00), que es la de maxima
    penetracion solar y por tanto el peor caso para la inercia. Ver
    `escenario_diurno`.

    `--fecha` es el tercer punto de operacion, y no se parece a los otros dos:
    la punta esta MEDIDA y el diurno se DERIVA de ella, mientras que este se
    construye desde cero con el despacho y el pronostico de XM de esa fecha.
    Ver `fuentes/xm_snapshot.py`.
    """
    ap.add_argument('--diurno', nargs='?', type=int, const=11, default=None,
                    metavar='HORA',
                    help="correr sobre el punto DIURNO en vez de la punta "
                         "nocturna; HORA es la clave HourNN de XM, 11 = "
                         "10:00-11:00 (defecto). " + ayuda_extra)
    ap.add_argument('--fecha', default=None, metavar='AAAA-MM-DD',
                    help="correr sobre el despacho y el pronostico de XM de "
                         "esa fecha, en vez de sobre el caso de PowerFactory. "
                         "Necesita un snapshot descargado con `sinsim scrape`. "
                         + ayuda_extra)
    ap.add_argument('--periodo', type=int, default=19, metavar='N',
                    help="periodo horario 1..24 de --fecha (defecto 19, la "
                         "punta nocturna).")


def construir(hora=None, limites=None, verbose=True, fecha=None, periodo=19):
    """Devuelve (red resuelta, diagnostico, contexto).

    Tres puntos de operacion, y conviene no confundirlos:

      `fecha=None, hora=None`   punta nocturna. MEDIDA, y contrastada contra
                                PowerFactory.
      `fecha=None, hora=11`     diurno. DERIVADO de la punta con factores
                                horarios de XM.
      `fecha='AAAA-MM-DD'`      cualquier fecha y periodo. CONSTRUIDO desde el
                                despacho y el pronostico publicados por XM; no
                                hereda la validacion del punto de operacion,
                                solo la de la RED.

    `limites` se pasa tal cual: None respeta `config.LIMITES_REACTIVOS`, False
    entrega la red SIN conmutar a PQ (que es lo que necesita
    `limites_reactivos` para poder enseñar el antes y el despues).

    El contexto trae `hora`, `fecha`, `periodo`, `etiqueta`, `sufijo`, `info`
    y `calidad`. `info` es el diccionario completo de
    `escenario_diurno.construir()` en el caso diurno y None en los otros.
    `calidad` es el `ReporteCalidad` de la fuente cuando hay fecha: ahi esta
    cuanta demanda y cuanta generacion se quedaron sin ubicar, que es lo que
    hay que mirar antes de creerse un nadir.
    """
    from stn import contingencias as C

    ctx = {'hora': hora, 'fecha': fecha, 'periodo': periodo,
           'etiqueta': etiqueta(hora, fecha, periodo),
           'sufijo': sufijo(hora, fecha, periodo),
           'info': None, 'calidad': None}

    if fecha:
        from stn.fuentes.base import sesgo_de_slack
        from stn.fuentes.xm_snapshot import FuenteXM
        fuente = FuenteXM(fecha, periodo, verbose=verbose)
        red, diag = C.caso_base(verbose=verbose, limites=limites,
                                fuente=fuente)
        ctx['calidad'] = fuente.calidad()
        ctx['sesgo_slack'] = sesgo_de_slack(red)
        return red, diag, ctx

    if hora is None:
        red, diag = C.caso_base(verbose=verbose, limites=limites)
        return red, diag, ctx

    from stn import escenario_diurno as ED
    red, info = ED.construir(hora=hora, limites=limites, verbose=verbose)
    ctx['info'] = info
    # `escenario_diurno` ya calcula el diagnostico con la misma funcion que
    # usa `caso_base`, asi que los dos caminos devuelven el mismo diccionario
    # y nada aguas abajo tiene que distinguirlos.
    return red, info['diag'], ctx


def de_args(args, limites=None, verbose=True):
    """`construir()` leyendo el punto del namespace de argparse."""
    return construir(hora=getattr(args, 'diurno', None), limites=limites,
                     verbose=verbose, fecha=getattr(args, 'fecha', None),
                     periodo=getattr(args, 'periodo', 19))


def cabecera(ctx):
    """Banda que identifica el caso. Se imprime SIEMPRE, tambien en la punta.

    Antes la punta no se anunciaba, porque era el unico caso posible. Ahora
    que hay dos, una salida sin cabecera es una salida que no se sabe de que
    caso es: justo el error que hace que dos ejecuciones se confundan.
    """
    print()
    print('=' * 74)
    print(f"  PUNTO DE OPERACION: {ctx['etiqueta']}")
    i = ctx.get('info')
    c = ctx.get('calidad')
    if c is not None:
        print(f"  generacion {c.p_despacho_ubicado_mw:.1f} MW "
              f"({c.cobertura_despacho_pct:.2f} % del despacho)  |  "
              f"demanda {c.p_demanda_ubicada_mw:.1f} MW "
              f"({c.cobertura_demanda_pct:.2f} % del pronostico)")
        print("  caso CONSTRUIDO desde el despacho y el pronostico de XM: "
              "hereda la validacion de la RED,")
        print("  no la del punto de operacion. No esta contrastado contra "
              "PowerFactory.")
        if not c.apto_para_frecuencia:
            print(f"  AVISO: faltan {c.p_demanda_no_ubicada_mw:.1f} MW de "
                  f"demanda sin ubicar; el EDAC dimensionaria por debajo")
        s = ctx.get('sesgo_slack')
        if s and s[1] and abs(s[3]) > 1.0:
            # El hueco entre el programa de XM y lo que este equivalente
            # necesita lo absorbe una SOLA maquina. Ver fuentes/base.py.
            print(f"  el slack {s[1]} cierra el balance apartandose "
                  f"{s[2]:+.1f} MW de su programa ({s[3]:+.1f} %)")
    elif i:
        print(f"  demanda x{i['f_demanda']:.4f} sobre la punta  |  "
              f"solar produciendo {i['solar_produccion_MW']:.1f} MW  |  "
              f"demanda NETA {i['carga_neta_MW']:.1f} MW")
        print("  caso DERIVADO de la punta con factores horarios de XM: "
              "no esta validado contra PowerFactory")
    else:
        print("  caso MEDIDO y contrastado contra PowerFactory")
    print('=' * 74)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description="Construye un punto de operacion y resume su estado.")
    anadir_flag(ap)
    a = ap.parse_args()
    red, diag, ctx = de_args(a, verbose=False)
    cabecera(ctx)
    print(f"  {len(red.buses)} buses | {len(red.lines)} lineas | "
          f"{len(red.transformers)} trafos")
    print(f"  demanda {diag['carga_MW']:.1f} MW | tension "
          f"[{diag['v_min']:.4f}, {diag['v_max']:.4f}] pu | "
          f"cargabilidad maxima {diag['carga_max_pct']:.1f} %")
