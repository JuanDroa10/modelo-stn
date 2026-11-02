# barrido.py
# Las 24 horas de una fecha, con la misma bateria en cada una.
#
# -------------------------------------------------------------------------
# PARA QUE
# -------------------------------------------------------------------------
# Hasta ahora el modelo tenia DOS puntos de operacion: la punta nocturna, que
# es el caso medido, y un diurno derivado de ella. La hora del diurno ---las
# 10-11 h--- se eligio por ser la de maxima penetracion solar, y esa eleccion
# se hizo con una tabla de dos horas cableada en `escenario_diurno.PERFIL`.
#
# Con `--fecha` se puede construir cualquier hora de cualquier fecha desde el
# despacho y el pronostico de XM. Este modulo recorre las 24 y somete cada una
# a lo mismo, de manera que la pregunta "cual es la hora critica" deje de
# contestarse por eleccion y pase a contestarse por medida.
#
# -------------------------------------------------------------------------
# QUE SE MIDE Y POR QUE ESE ORDEN
# -------------------------------------------------------------------------
#   1. COBERTURA   cuanto del despacho y del pronostico se pudo ubicar. Va
#                  primero porque condiciona todo lo demas: un nadir calculado
#                  sobre una demanda incompleta es un artefacto, no un
#                  resultado. Las horas que no llegan al umbral se marcan.
#   2. RED         convergencia, tensiones, perdidas, cargabilidad.
#   3. INERCIA     S_base, H y la energia cinetica H*S. Esto es lo nuevo: con
#                  dos escenarios fijos la inercia era un numero; aqui sigue el
#                  compromiso de unidades real de cada hora.
#   4. EVENTO      la MISMA perturbacion en MW absolutos sobre las 24 horas, y
#                  el nadir y los escalones de EDAC que provoca. Que pese mas
#                  en unas horas que en otras no es un sesgo del montaje: es el
#                  resultado.
#
# -------------------------------------------------------------------------
# LO QUE ESTE BARRIDO NO DICE
# -------------------------------------------------------------------------
# No esta validado contra PowerFactory, y no puede estarlo: el caso de
# referencia es de planeamiento y no corresponde a ninguna fecha de operacion
# (ver `golden_pf.py`). Lo que estas 24 horas heredan es la validacion de la
# RED ---topologia, impedancias, limites---, no la del punto de operacion.
#
# Tampoco dice nada sobre el BTM: lo que esta detras del contador es invisible
# en el despacho de XM. El pool sigue saliendo de PARATEC y hay que pedirlo
# aparte con `deslastre_btm.py --fecha`.
#
# -------------------------------------------------------------------------
# USO
# -------------------------------------------------------------------------
#   python barrido.py --fecha 2026-10-20
#   python barrido.py --fecha 2026-10-20 --evento-mw 1500 --grafico

import argparse
import os

import numpy as np
import pandas as pd

from stn.config import salida

# La misma perturbacion que usa `comparar_casos.py`, para que las cifras de
# los dos modulos se puedan leer juntas.
EVENTO_MW = 3000.0


def _una_hora(fecha, periodo, evento_mw, verbose=False):
    """Construye, resuelve y mide una hora. Devuelve un dict de una fila."""
    from stn import agc_deslastre as AGC
    from stn import contingencias as C
    from stn import punto_operacion as PO
    from stn.fuentes.base import sesgo_de_slack

    fila = {'periodo': periodo}
    try:
        red, diag, ctx = PO.construir(fecha=fecha, periodo=periodo,
                                      verbose=False)
    except Exception as e:                                   # noqa: BLE001
        fila['convergio'] = False
        fila['nota'] = f'{type(e).__name__}: {e}'
        return fila

    cal = ctx['calidad']
    sis = AGC.Sistema(red)
    ses = sesgo_de_slack(red)
    v = red.buses_t.v_mag_pu.iloc[0]
    carga_pct = C._cargabilidad(red)

    # El evento: la misma perdida de generacion en las 24 horas.
    #
    # El nadir se toma de la TRAZA, no de `f.min()`. Si la hora colapsa, la
    # traza devuelve NaN a proposito: `f.min()` seria el punto donde el
    # integrador se rindio, y esta tabla acaba en un CSV que se promedia y se
    # compara entre horas. Ver `agc_deslastre.simular_frecuencia`.
    t, f, escalones, tr = AGC.simular_frecuencia(sis, deficit_mw=evento_mw,
                                                 con_edac=True)
    n_esc = sum(1 for e in escalones if e.get('disparado'))
    mw_edac = sum(float(e.get('mw', 0.0)) for e in escalones
                  if e.get('disparado'))

    # Cuanta solar hay a esa hora. Sin esto no se puede separar el efecto de
    # la fotovoltaica del de la demanda sobre la inercia.
    from stn.fuentes.xm_snapshot import FuenteXM
    _, _, solar_pu = FuenteXM(fecha, periodo,
                              verbose=False).produccion_solar_pu()

    fila.update({
        'convergio': True,
        'solar_pu': solar_pu,
        # 1. cobertura
        'cob_gen_pct': cal.cobertura_despacho_pct,
        'cob_dem_pct': cal.cobertura_demanda_pct,
        'apto': cal.apto_para_frecuencia,
        'sesgo_slack_MW': ses[0],
        'sesgo_slack_pct': ses[3],
        # 2. red
        'demanda_MW': sis.P_carga,
        'perdidas_MW': sis.P_perdidas,
        'v_min': float(v.min()),
        'v_max': float(v.max()),
        'fuera_banda': int(((v < 0.90) | (v > 1.10)).sum()),
        'carga_max_pct': float(carga_pct.max()) if len(carga_pct) else 0.0,
        # 3. inercia
        'S_base_MVA': sis.S_base,
        'H_s': sis.H,
        'HS_MVAs': sis.H * sis.S_base,
        'reserva_MW': sis.reserva,
        'reserva_pct': 100.0 * sis.reserva / sis.P_gen if sis.P_gen else 0.0,
        # 4. evento
        'RoCoF_Hz_s': 60.0 * evento_mw / (2.0 * sis.H * sis.S_base)
        if sis.H * sis.S_base else float('nan'),
        'nadir_Hz': float(tr['nadir']),      # NaN si la hora colapsa
        'nadir_corte_Hz': float(tr['nadir_corte']),
        'colapso': bool(tr['colapso']),
        'edac_escalones': n_esc,
        'edac_MW': mw_edac,
        'edac_truncado': bool(tr['deslastre_truncado']),
        'nota': '',
    })
    if verbose:
        print(f"  p{periodo:2d}  dem {fila['demanda_MW']:8.1f}  "
              f"H*S {fila['HS_MVAs']:8.0f}  nadir {fila['nadir_Hz']:.3f}  "
              f"EDAC {n_esc}")
    return fila


def barrer(fecha, periodos=range(1, 25), evento_mw=EVENTO_MW, verbose=True):
    """Las 24 horas en un DataFrame indexado por periodo."""
    if verbose:
        print(f"Barriendo {fecha}, evento de {evento_mw:.0f} MW en cada hora")
    filas = [_una_hora(fecha, p, evento_mw, verbose=verbose) for p in periodos]
    df = pd.DataFrame(filas).set_index('periodo')
    # Una hora que falla devuelve una fila SIN las columnas del resto, asi que
    # `convergio` y `apto` quedan con NaN y dtype object. Entonces `~df.apto`
    # no niega: aplica `operator.invert` entero a entero (~True -> -2) y el
    # filtrado de mas abajo intenta seleccionar COLUMNAS llamadas -2. El
    # barrido entero se perdia por una sola hora mala, despues de haber
    # calculado las otras 23.
    for col, defecto in (('convergio', False), ('apto', False)):
        if col not in df.columns:
            df[col] = defecto
        df[col] = df[col].fillna(defecto).astype(bool)
    return df


# Por debajo de esta fraccion de horas utiles, el resumen de una fecha deja de
# tener sentido y se dice en vez de calcularlo.
#
# El caso que lo obligo: con la puerta de cobertura de generacion puesta, el
# 2026-10-14 se queda con 5 horas de 24, y sobre esas 5 el modulo anunciaba
# "hora critica: 22" y "recorrido 1,10x". Las dos cifras son del SUBCONJUNTO
# que sobrevivio, no del dia: la hora critica de verdad estaba entre las horas
# descartadas. Un resumen calculado sobre una muestra truncada que no avisa de
# que lo esta es exactamente como se publican artefactos.
FRACCION_HORAS_MINIMA = 0.70


def _resumen(df, evento_mw):
    """Lo que hay que leer de la tabla, dicho en palabras."""
    ok = df[df.convergio.fillna(False)]
    print()
    print('=' * 74)
    print('  LO QUE DICE EL BARRIDO')
    print('=' * 74)
    if not len(ok):
        print('  ninguna hora resolvio.')
        return

    no_conv = df[~df.convergio.fillna(False)]
    if len(no_conv):
        print(f"  {len(no_conv)} horas NO resolvieron: "
              f"{list(no_conv.index)}")

    sospechosas = ok[~ok.apto]
    if len(sospechosas):
        print(f"  {len(sospechosas)} horas sin cobertura suficiente "
              f"({list(sospechosas.index)}): su nadir no es de fiar.")
        ok = ok[ok.apto]
        if not len(ok):
            print('  no queda ninguna hora fiable que resumir.')
            return

    frac = len(ok) / max(len(df), 1)
    if frac < FRACCION_HORAS_MINIMA:
        print(f"\n  SOLO {len(ok)} DE {len(df)} HORAS SON UTILIZABLES "
              f"({100*frac:.0f} %).")
        print("  No se resume esta fecha: la hora critica y el recorrido de la")
        print("  inercia saldrian del subconjunto que sobrevivio, no del dia.")
        print("  La hora critica de verdad puede estar entre las descartadas.")
        return

    h_min = ok.HS_MVAs.idxmin()
    h_max = ok.HS_MVAs.idxmax()
    print(f"\n  INERCIA     minima en el periodo {h_min} "
          f"({ok.HS_MVAs.min():,.0f} MVA.s) y maxima en el {h_max} "
          f"({ok.HS_MVAs.max():,.0f})")
    print(f"              recorrido {ok.HS_MVAs.max()/ok.HS_MVAs.min():.2f}x "
          f"dentro del mismo dia")

    # Las horas que colapsan traen el nadir a NaN: no se promedian ni compiten
    # por ser la critica, porque su "minimo" es donde corto el integrador.
    colapsadas = ok[ok.get('colapso', False).fillna(False)] \
        if 'colapso' in ok.columns else ok.iloc[0:0]
    if len(colapsadas):
        print(f"\n  {len(colapsadas)} horas COLAPSAN ({list(colapsadas.index)}): "
              f"su nadir no es un dato y queda fuera del resumen.")
        ok = ok[~ok.index.isin(colapsadas.index)]
        if not len(ok):
            print('  no queda ninguna hora con nadir utilizable.')
            return

    h_nadir = ok.nadir_Hz.idxmin()
    print(f"\n  HORA CRITICA ante {evento_mw:.0f} MW: periodo {h_nadir}, "
          f"nadir {ok.nadir_Hz.min():.3f} Hz "
          f"con {int(ok.loc[h_nadir,'edac_escalones'])} escalones de EDAC "
          f"({ok.loc[h_nadir,'edac_MW']:.0f} MW)")
    print(f"              su demanda son {ok.loc[h_nadir,'demanda_MW']:.1f} MW "
          f"y su H*S {ok.loc[h_nadir,'HS_MVAs']:,.0f} MVA.s")

    h_dem = ok.demanda_MW.idxmax()
    if h_dem != h_nadir:
        print(f"\n  OJO: la hora de MAXIMA DEMANDA es la {h_dem} "
              f"({ok.demanda_MW.max():.1f} MW) y NO es la critica. "
              f"Su nadir es {ok.loc[h_dem,'nadir_Hz']:.3f} Hz.")
        print("       La fragilidad la manda la inercia acoplada, no la carga.")

    fuera = ok[ok.fuera_banda > 0]
    if len(fuera):
        print(f"\n  {len(fuera)} horas con barras fuera de [0,9; 1,1]: "
              f"{list(fuera.index)}")
    peor = ok.sesgo_slack_pct.abs().max()
    print(f"\n  el slack se aparta de su programa hasta un {peor:.1f} % "
          f"(ver README seccion 7)")


def _grafico(df, fecha, evento_mw, destino):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    ok = df[df.convergio.fillna(False)]
    fig, ax = plt.subplots(3, 1, figsize=(10, 9), sharex=True)

    ax[0].plot(ok.index, ok.demanda_MW, 'o-', color='#1f77b4')
    ax[0].set_ylabel('demanda [MW]')
    ax[0].grid(alpha=.3)
    ax[0].set_title(f'{fecha}: las 24 horas, evento de {evento_mw:.0f} MW')

    ax[1].plot(ok.index, ok.HS_MVAs / 1000, 'o-', color='#2ca02c')
    ax[1].set_ylabel('energia cinetica H*S [GVA.s]')
    ax[1].grid(alpha=.3)

    ax[2].plot(ok.index, ok.nadir_Hz, 'o-', color='#d62728')
    ax[2].axhline(59.4, ls='--', lw=1, color='gray')
    ax[2].annotate('primer escalon del EDAC (59,4 Hz)', (1, 59.4),
                   fontsize=8, color='gray', va='bottom')
    ax[2].set_ylabel('nadir [Hz]')
    ax[2].set_xlabel('periodo horario')
    ax[2].grid(alpha=.3)
    ax[2].set_xticks(list(ok.index))

    fig.tight_layout()
    fig.savefig(destino, dpi=120)
    plt.close(fig)
    return destino


def _comparar_fechas(tablas, evento_mw):
    """Lo que se repite y lo que no entre varias fechas.

    Una sola fecha no distingue una propiedad del sistema de una casualidad
    del dia. Esto pone las fechas en columnas para que se vea cual es cual.
    """
    print()
    print('=' * 74)
    print(f'  {len(tablas)} FECHAS COMPARADAS')
    print('=' * 74)

    filas, truncadas = [], []
    for fecha, df in tablas.items():
        ok = df[df.convergio.fillna(False) & df.apto.fillna(False)]
        # Una fecha a la que le faltan demasiadas horas no se resume: ver
        # FRACCION_HORAS_MINIMA. Entra en la tabla con las horas que tiene y
        # el resto en blanco, para que se vea que esta ahi y por que no dice
        # nada, en vez de desaparecer o de mentir.
        if len(ok) / max(len(df), 1) < FRACCION_HORAS_MINIMA:
            filas.append({'fecha': fecha, 'horas_utiles': len(ok)})
            truncadas.append(fecha)
            continue
        dia = ok[ok.solar_pu > 0.05] if 'solar_pu' in ok.columns else ok
        filas.append({
            'fecha': fecha,
            'horas_utiles': len(ok),
            'h_critica': int(ok.nadir_Hz.idxmin()),
            'nadir_min': round(float(ok.nadir_Hz.min()), 3),
            'h_HS_min': int(ok.HS_MVAs.idxmin()),
            'HS_min': round(float(ok.HS_MVAs.min())),
            'HS_max': round(float(ok.HS_MVAs.max())),
            'recorrido': round(float(ok.HS_MVAs.max() / ok.HS_MVAs.min()), 2),
            'h_dem_max': int(ok.demanda_MW.idxmax()),
            'corr_solar_HSporMW': (
                round(float(dia.solar_pu.corr(dia.HS_MVAs / dia.demanda_MW)), 3)
                if 'solar_pu' in ok.columns and len(dia) > 2 else float('nan')),
            'peor_sesgo_pct': round(float(ok.sesgo_slack_pct.abs().max()), 1),
        })
    res = pd.DataFrame(filas).set_index('fecha')
    print(res.to_string())

    print()
    if truncadas:
        print(f"  {len(truncadas)} fechas sin resumir por falta de horas "
              f"utilizables: {truncadas}")
        print("  (no se les calcula hora critica ni recorrido: saldrian del "
              "subconjunto que sobrevivio)")
        print()
    if 'h_critica' not in res.columns or res.h_critica.isna().all():
        print('  ninguna fecha tiene horas suficientes que resumir.')
        return res
    v = res[res.h_critica.notna()]
    crit = v.h_critica.tolist()
    print(f"  hora critica por fecha : {crit}")
    if len(set(crit)) == 1:
        print(f"  -> SIEMPRE la misma ({crit[0]}). Es una propiedad del "
              f"sistema, no del dia.")
    elif all(h <= 7 or h >= 22 for h in crit):
        print("  -> siempre en el valle nocturno (<=7 o >=22), aunque no "
              "siempre la misma hora.")
    else:
        print("  -> NO siempre en el valle: el patron depende del dia.")

    coincide = int((v.h_critica == v.h_dem_max).sum())
    print(f"  la hora critica coincide con la de maxima demanda en "
          f"{coincide} de {len(v)} fechas")
    print(f"  recorrido de la inercia: {v.recorrido.min():.2f}x a "
          f"{v.recorrido.max():.2f}x")
    c = v.corr_solar_HSporMW.dropna()
    if len(c):
        print(f"  correlacion solar vs inercia-por-MW: de {c.min():+.3f} a "
              f"{c.max():+.3f}")
        if c.abs().max() < 0.4:
            print("  -> la penetracion solar NO explica la inercia por MW en "
                  "ninguna fecha.")
        elif c.min() < 0 < c.max():
            print("  -> le CAMBIA EL SIGNO entre fechas: con esta muestra es "
                  "ruido, no un efecto.")

    _pool_solar(tablas)
    return res


def _pool_solar(tablas):
    """La pregunta de la solar, sobre todas las horas juntas.

    POR QUE NO BASTA LA CORRELACION POR FECHA. Son 24 puntos, y ademas la
    solar y la demanda suben a la vez por la mañana, asi que una correlacion
    simple entre solar e inercia-por-MW recoge sobre todo el efecto de la
    demanda. Con varias fechas se pueden juntar las horas y, sobre todo,
    DESCONTAR la demanda con una correlacion parcial:
    r(solar, HS/MW | demanda).
    """
    trozos = []
    for fecha, df in tablas.items():
        ok = df[df.convergio.fillna(False) & df.apto.fillna(False)]
        if 'solar_pu' not in ok.columns or not len(ok):
            continue
        t = ok[['demanda_MW', 'HS_MVAs', 'solar_pu']].copy()
        t['fecha'] = fecha
        trozos.append(t)
    if len(trozos) < 2:
        return
    d = pd.concat(trozos)
    d = d[d.solar_pu > 0.05]                      # solo horas con sol
    if len(d) < 30:
        print(f"\n  muestra de horas con sol demasiado corta ({len(d)}) para "
              f"descontar la demanda.")
        return
    d['HS_por_MW'] = d.HS_MVAs / d.demanda_MW

    def r(a, b):
        return float(d[a].corr(d[b]))

    r_sy, r_sd, r_dy = (r('solar_pu', 'HS_por_MW'),
                        r('solar_pu', 'demanda_MW'),
                        r('demanda_MW', 'HS_por_MW'))
    den = np.sqrt((1 - r_sd ** 2) * (1 - r_dy ** 2))
    parcial = (r_sy - r_sd * r_dy) / den if den > 1e-12 else float('nan')

    print(f"\n  TODAS LAS HORAS CON SOL JUNTAS ({len(d)} horas, "
          f"{d.fecha.nunique()} fechas)")
    print(f"    solar vs inercia-por-MW            r = {r_sy:+.3f}")
    print(f"    solar vs demanda                   r = {r_sd:+.3f}")
    print(f"    demanda vs inercia-por-MW          r = {r_dy:+.3f}")
    print(f"    solar vs inercia-por-MW SIN demanda r = {parcial:+.3f}  "
          f"<- la que responde la pregunta")
    if abs(parcial) < 0.2:
        print("    -> descontada la demanda, la solar no explica la inercia "
              "por MW.")
    elif parcial < 0:
        print("    -> descontada la demanda, MAS solar va con MENOS inercia "
              "por MW: es el efecto que se esperaba.")
    else:
        print("    -> descontada la demanda, MAS solar va con MAS inercia por "
              "MW. Conviene mirarlo antes de citarlo.")


def main():
    ap = argparse.ArgumentParser(
        description='Somete las 24 horas de una o varias fechas a la misma '
                    'bateria.')
    ap.add_argument('--fecha', required=True, nargs='+', metavar='AAAA-MM-DD',
                    help='una o varias fechas; con varias se comparan al final')
    ap.add_argument('--periodos', default='1-24',
                    help='rango o lista: "1-24", "10-14", "3,12,19"')
    ap.add_argument('--evento-mw', type=float, default=EVENTO_MW,
                    help=f'perdida de generacion a simular (defecto '
                         f'{EVENTO_MW:.0f} MW, el mismo de comparar_casos)')
    ap.add_argument('--csv', default=None,
                    help='fichero de salida (defecto resultados/barrido_FECHA.csv)')
    ap.add_argument('--grafico', action='store_true',
                    help='ademas del CSV, dibuja demanda, inercia y nadir')
    a = ap.parse_args()

    if '-' in a.periodos and ',' not in a.periodos:
        i, f = a.periodos.split('-')
        periodos = range(int(i), int(f) + 1)
    else:
        periodos = [int(x) for x in a.periodos.split(',')]

    cols = ['demanda_MW', 'perdidas_MW', 'v_min', 'v_max', 'fuera_banda',
            'solar_pu', 'S_base_MVA', 'H_s', 'HS_MVAs', 'reserva_MW',
            'RoCoF_Hz_s', 'nadir_Hz', 'edac_escalones', 'cob_dem_pct', 'apto']
    redondeo = {'demanda_MW': 1, 'perdidas_MW': 1, 'v_min': 4, 'v_max': 4,
                'solar_pu': 3, 'S_base_MVA': 0, 'H_s': 3, 'HS_MVAs': 0,
                'reserva_MW': 1, 'RoCoF_Hz_s': 4, 'nadir_Hz': 3,
                'cob_dem_pct': 2}

    tablas = {}
    for fecha in a.fecha:
        if len(a.fecha) > 1:
            print(f"\n{'=' * 74}\n  {fecha}\n{'=' * 74}")
        df = barrer(fecha, periodos, a.evento_mw)
        tablas[fecha] = df

        print()
        print(df[[c for c in cols if c in df.columns]]
              .round(redondeo).to_string())
        _resumen(df, a.evento_mw)

        destino = (a.csv if (a.csv and len(a.fecha) == 1)
                   else salida(f"barrido_{fecha.replace('-', '')}.csv"))
        os.makedirs(os.path.dirname(destino) or '.', exist_ok=True)
        df.to_csv(destino)
        print(f"\n  tabla completa en {destino}")

        if a.grafico:
            png = destino.replace('.csv', '.png')
            _grafico(df, fecha, a.evento_mw, png)
            print(f"  grafico en {png}")

    if len(tablas) > 1:
        res = _comparar_fechas(tablas, a.evento_mw)
        destino = salida('barrido_comparacion.csv')
        res.to_csv(destino)
        print(f"\n  comparacion en {destino}")


if __name__ == '__main__':
    main()
