# comparar_casos.py
# Somete los DOS puntos de operacion a la misma bateria y los pone en columnas.
#
# -------------------------------------------------------------------------
# PARA QUE
# -------------------------------------------------------------------------
# Cada modulo acepta ya `--diurno`, asi que las dos salidas existen. Pero son
# dos listados largos, y cruzarlos a mano es justo donde se cuelan los errores
# de lectura: una cifra que se compara contra la casilla equivocada parece un
# hallazgo. Esto corre lo mismo sobre los dos y lo imprime lado a lado, con la
# variacion ya calculada.
#
# -------------------------------------------------------------------------
# QUE SE COMPARA Y QUE NO
# -------------------------------------------------------------------------
# Se comparan las magnitudes que dependen del PUNTO DE OPERACION: el balance
# de potencia, las tensiones, la cargabilidad, los parametros de la ecuacion
# de oscilacion y la respuesta a un evento.
#
# NO se compara la validacion contra PowerFactory, y no por descuido: el caso
# de referencia es de punta y no existe uno diurno. El diurno hereda la
# validacion de la RED ---topologia, impedancias, limites térmicos--- pero no
# la del punto de operacion. Decir "validado" de las dos columnas seria falso.
#
# EL EVENTO ES EL MISMO EN LOS DOS CASOS, en MW absolutos. Esa es la
# comparacion que interesa: la MISMA perturbacion sobre dos sistemas de
# inercia distinta. Que en terminos relativos pese mas en el diurno no es un
# sesgo del montaje, es el resultado.

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


# ---------------------------------------------------------------------------
# Utilidades de impresion
# ---------------------------------------------------------------------------

def _titulo(txt):
    print()
    print('=' * 78)
    print(f"  {txt}")
    print('=' * 78)


def _cab(a='PUNTA', b='DIURNO'):
    print(f"  {'':32s} {a:>12s} {b:>12s}      var")
    print("  " + "-" * 64)


def _fila(nombre, punta, diurno, fmt='{:12.1f}', uni='', rel=True):
    """Una fila de la tabla. `rel=False` para magnitudes donde el % no dice nada."""
    if punta is None or diurno is None:
        p = '-' if punta is None else fmt.format(punta)
        d = '-' if diurno is None else fmt.format(diurno)
        print(f"  {nombre:32s} {p:>12s} {d:>12s}            {uni}")
        return
    var = ''
    if rel and abs(punta) > 1e-9:
        var = f"{(diurno - punta) / punta * 100:+7.1f} %"
    elif not rel:
        var = f"{diurno - punta:+7.2f}  "
    print(f"  {nombre:32s} {fmt.format(punta)} {fmt.format(diurno)}   {var}  {uni}")


def _texto(nombre, punta, diurno):
    print(f"  {nombre:32s} {str(punta):>12s} {str(diurno):>12s}")


# ---------------------------------------------------------------------------
# La bateria
# ---------------------------------------------------------------------------

def construir_los_dos(hora=11, verbose=False):
    """Devuelve el dict de cada caso: red, diagnostico, contexto y Sistema."""
    from stn import agc_deslastre as AGC
    casos = {}
    for clave, h in (('punta', None), ('diurno', hora)):
        print(f"  construyendo {clave}...", flush=True)
        red, diag, ctx = PO.construir(hora=h, verbose=verbose)
        casos[clave] = {'red': red, 'diag': diag, 'ctx': ctx,
                        'sis': AGC.Sistema(red)}
    return casos


def bloque_red(c):
    _titulo("1. LA RED")
    print("  Es la MISMA en los dos casos por construccion: el diurno se deriva")
    print("  del de punta y no toca topologia ni impedancias. Se imprime para")
    print("  dejar constancia de que no ha cambiado nada por el camino.")
    print()
    p, d = c['punta']['red'], c['diurno']['red']
    _cab()
    _fila('buses', len(p.buses), len(d.buses), '{:12d}', rel=False)
    _fila('lineas', len(p.lines), len(d.lines), '{:12d}', rel=False)
    _fila('transformadores', len(p.transformers), len(d.transformers),
          '{:12d}', rel=False)
    _fila('generadores', len(p.generators), len(d.generators), '{:12d}',
          rel=False)
    _fila('cargas', len(p.loads), len(d.loads), '{:12d}', rel=False)


def bloque_potencia(c):
    _titulo("2. BALANCE DE POTENCIA")
    p, d = c['punta'], c['diurno']
    i = d['ctx']['info']
    rp, rd = p['red'], d['red']

    gen_p = float(rp.generators_t.p.iloc[0].sum())
    gen_d = float(rd.generators_t.p.iloc[0].sum())
    car_p = float(rp.loads_t.p.iloc[0].sum())
    car_d = float(rd.loads_t.p.iloc[0].sum())

    _cab()
    _fila('demanda BRUTA', i['demanda_punta_MW'], i['demanda_diurna_MW'],
          uni='MW')
    _fila('solar embebida produciendo', 0.0, i['solar_produccion_MW'],
          uni='MW', rel=False)
    _fila('demanda NETA del STN', car_p, car_d, uni='MW')
    _fila('generacion sincrona resuelta', gen_p, gen_d, uni='MW')
    _fila('perdidas de red', gen_p - car_p, gen_d - car_d, uni='MW')
    _fila('perdidas, % de la generacion',
          100 * (gen_p - car_p) / gen_p, 100 * (gen_d - car_d) / gen_d,
          '{:12.2f}', uni='%', rel=False)
    print()
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
    p, d = c['punta'], c['diurno']
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
    if dd['v_max'] > dp['v_max']:
        print()
        print("  Las tensiones SUBEN en el diurno, y es lo esperado: menos flujo")
        print("  es menos caida en las reactancias serie, y las lineas descargadas")
        print("  aportan reactivo capacitivo. Vigilar el margen contra 1,10 pu.")
    neg = d['ctx']['info'].get('barras_exportadoras')
    if neg is not None and len(neg):
        print()
        print(f"  {len(neg)} barras quedan con carga NETA NEGATIVA en el diurno "
              f"({float(neg.sum()):.1f} MW):")
        for b, v in neg.sort_values().items():
            print(f"      {b:30s} {v:8.1f} MW")
        print("  La solar local supera a la demanda local y la barra EXPORTA al")
        print("  STN. Es un flujo inverso real, no un error de signo.")


def bloque_frecuencia(c):
    _titulo("4. LO QUE DECIDE LA FRECUENCIA")
    sp, sd = c['punta']['sis'], c['diurno']['sis']
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
    print("  ESTA ES LA COMPARACION QUE IMPORTA. El diurno tiene MENOS demanda y")
    print("  aun asi es MAS fragil: la generacion sincrona baja por la demanda y")
    print("  ademas por toda la solar, asi que hay menos maquinas acopladas y")
    print("  menos energia cinetica frente a la misma perturbacion.")


def bloque_evento(c, mw=EVENTO_MW):
    from stn import agc_deslastre as AGC
    _titulo(f"5. RESPUESTA A LA MISMA PERTURBACION ({mw:.0f} MW)")
    print("  Mismo evento en MW absolutos sobre los dos sistemas. En el diurno")
    print("  pesa mas en terminos relativos, y eso es precisamente el resultado.")
    print()

    out = {}
    for k in ('punta', 'diurno'):
        s = c[k]['sis']
        t, f, esc, tr = AGC.simular_frecuencia(s, deficit_mw=mw, con_edac=True)
        out[k] = {'t': t, 'f': f, 'esc': esc, 'tr': tr, 'sis': s}

    p, d = out['punta'], out['diurno']
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
    print("  uno. Aqui el diurno puede salir mejor parado, porque su mayor")
    print("  maquina tambien es mas pequena.")
    print()
    from stn import agc_deslastre as AGC
    _cab()
    datos = {}
    for k in ('punta', 'diurno'):
        s = c[k]['sis']
        pm = s.gen.p_set.astype(float)
        g = pm.idxmax()
        mw = float(pm.max())
        _, _, esc, tr = AGC.simular_frecuencia(s, deficit_mw=mw, con_edac=True)
        datos[k] = {'gen': g, 'mw': mw, 'tr': tr, 'esc': esc}
    p, d = datos['punta'], datos['diurno']
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
    rp = zonas.resumen(c['punta']['red'])
    rd = zonas.resumen(c['diurno']['red'])
    print(f"  {'area':16s} {'GEN punta':>10s} {'GEN diurno':>11s}"
          f" {'CARGA punta':>12s} {'CARGA diurno':>13s} {'INTER punta':>12s}"
          f" {'INTER diurno':>13s}")
    print("  " + "-" * 92)
    for a in rp.index:
        if a not in rd.index:
            continue
        print(f"  {a:16s} {rp.at[a,'GenP']:10.1f} {rd.at[a,'GenP']:11.1f}"
              f" {rp.at[a,'LoadP']:12.1f} {rd.at[a,'LoadP']:13.1f}"
              f" {rp.at[a,'InterP']:12.1f} {rd.at[a,'InterP']:13.1f}")
    print("  " + "-" * 92)
    print(f"  {'TOTAL':16s} {rp.GenP.sum():10.1f} {rd.GenP.sum():11.1f}"
          f" {rp.LoadP.sum():12.1f} {rd.LoadP.sum():13.1f}"
          f" {rp.InterP.sum():12.1f} {rd.InterP.sum():13.1f}")
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
    pc = salida('contingencias_n1.csv')
    dc = salida(f"contingencias_n1_diurno{c['diurno']['ctx']['hora']}.csv")

    if correr:
        from stn import contingencias as C
        for k, dest in (('punta', pc), ('diurno', dc)):
            print(f"  barriendo {k}... (esto tarda)", flush=True)
            base, diag = c[k]['red'], c[k]['diag']
            cand = C.elementos_de(base, ('Line', 'Transformer'))
            r = C.barrido(base, diag, cand, verbose=False)
            r.to_csv(dest, index=False, encoding='utf-8-sig')

    if not (os.path.exists(pc) and os.path.exists(dc)):
        print("  Faltan resultados. Corre primero:")
        print("      python contingencias.py")
        print("      python contingencias.py --diurno")
        print("  o vuelve a llamar a este modulo con --n1 (tarda unos minutos).")
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

    # LAS TENSIONES SON EL MODO DE FALLO DEL DIURNO, y por eso se desglosan
    # aqui en vez de resumirse en un "sin problemas". El caso de punta se
    # queda corto por abajo y el diurno se pasa por arriba: son dos riesgos
    # distintos, y un recuento agregado los daria por equivalentes.
    if 'n_v_bajas' in dp.columns and 'n_v_altas' in dp.columns:
        _fila('casos con tension < 0,90 pu', int((dp.n_v_bajas > 0).sum()),
              int((dd.n_v_bajas > 0).sum()), '{:12d}', rel=False)
        _fila('casos con tension > 1,10 pu', int((dp.n_v_altas > 0).sum()),
              int((dd.n_v_altas > 0).sum()), '{:12d}', rel=False)
        _fila('tension maxima vista', float(dp.v_max.max()),
              float(dd.v_max.max()), '{:12.4f}', uni='pu', rel=False)
        _fila('tension minima vista', float(dp.v_min.min()),
              float(dd.v_min.min()), '{:12.4f}', uni='pu', rel=False)
        altas = dd[dd.n_v_altas > 0]
        if len(altas):
            print()
            # v_max del CASO BASE diurno, no el maximo del barrido: son dos
            # cifras distintas y confundirlas haria creer que el punto de
            # partida ya viola el limite.
            v0 = float(c['diurno']['diag']['v_max'])
            print(f"  En el diurno hay {len(altas)} contingencias que suben alguna "
                  f"barra por encima")
            print("  de 1,10 pu. En la punta no hay ninguna. El caso base diurno "
                  f"ya parte de")
            print(f"  {v0:.4f} pu SIN contingencia, asi que el margen por arriba es "
                  f"de {1.10-v0:.4f} pu")
            print("  y cualquier apertura lo consume:")
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
    info_d = c['diurno']['ctx']['info']
    # Factor de produccion de la hora: que fraccion de su placa entrega de
    # verdad la fotovoltaica. En la punta nocturna vale CERO para la solar.
    prod_pu = info_d.get('produccion_solar_pu') or 1.0

    _cab()
    _fila('BTM registrado (placa)', reg, reg, uni='MW', rel=False)
    for k, v in sorted(por_registro.items()):
        _fila(f'  de los cuales {k}', float(v), float(v), uni='MW', rel=False)
    _fila('factor solar de la hora', 0.0, prod_pu, '{:12.3f}', rel=False)
    print()
    print("  En la punta nocturna el BTM solar produce CERO: el evento central")
    print("  del trabajo ---la desconexion en bloque de la generacion detras del")
    print("  contador--- no puede ocurrir a esa hora. Esa es la razon de ser del")
    print("  caso diurno, y es por lo que las dos columnas no son intercambiables:")
    print("  el algoritmo solo tiene algo que decidir en la de la derecha.")
    print()
    print("  Ojo con la placa: el pool registrado NO es solo solar. Ver el analisis")
    print(f"  completo en `python deslastre_btm.py --diurno {c['diurno']['ctx']['hora']}`.")


def main():
    ap = argparse.ArgumentParser(
        description="Somete la punta nocturna y el punto diurno a la misma "
                    "bateria y los imprime lado a lado.")
    ap.add_argument('--hora', type=int, default=11,
                    help="hora del caso diurno, clave HourNN de XM (defecto 11)")
    ap.add_argument('--evento-mw', type=float, default=EVENTO_MW,
                    help="perturbacion de referencia en MW (defecto 3000)")
    ap.add_argument('--n1', action='store_true',
                    help="correr el barrido N-1 aqui en vez de leer los CSV "
                         "ya generados (tarda varios minutos por caso)")
    ap.add_argument('--verbose', action='store_true',
                    help="enseñar el informe completo de cada constructor")
    args = ap.parse_args()

    print()
    print("Construyendo los dos puntos de operacion...")
    c = construir_los_dos(hora=args.hora, verbose=args.verbose)

    print()
    print('#' * 78)
    print("#  PUNTA NOCTURNA (18-22 h)  frente a  "
          f"{c['diurno']['ctx']['etiqueta'].upper()}")
    print('#' * 78)
    print()
    print("  La columna PUNTA es un caso MEDIDO y contrastado contra PowerFactory.")
    print("  La columna DIURNO es DERIVADA de ella con factores horarios de XM.")
    print("  Las dos corren el mismo codigo; no tienen el mismo respaldo empirico.")

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
    sp, sd = c['punta']['sis'], c['diurno']['sis']
    print(f"  El punto diurno tiene un {100*(1-sd.P_carga/sp.P_carga):.1f} % menos de")
    print(f"  demanda neta y un {100*(1-sd.S_base/sp.S_base):.1f} % menos de capacidad "
          f"acoplada.")
    print(f"  Su energia cinetica cae de {sp.H*sp.S_base:.0f} a {sd.H*sd.S_base:.0f} "
          f"MVA.s, asi que la MISMA")
    print(f"  perturbacion le produce un RoCoF "
          f"{(sp.H*sp.S_base)/(sd.H*sd.S_base):.2f} veces mayor.")
    print()
    print("  Menos demanda no es menos riesgo. Es el punto del trabajo.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
