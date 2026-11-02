# limites_reactivos.py
"""Limites de potencia reactiva y conmutacion PV->PQ sobre el modelo del STN.

Por que hace falta un modulo aparte
-----------------------------------
PyPSA `Network.pf()` NO impone limites de reactivo: el componente Generator
solo expone `q_set` y `q`, sin `q_min` ni `q_max`. En consecuencia una barra PV
sostiene su consigna de tension con los MVAr que hagan falta, aunque sean
fisicamente imposibles.

Medido sobre el caso base: nueve de los 29 generadores exceden su limite de
subexcitacion, y alguno absorbe mas reactivo del que podria entregar su propia
maquina. El efecto no es un error numerico pequeno: mientras las barras PV
puedan inventar reactivo, **el colapso de tension es estructuralmente
imposible**, y ese es justamente el modo de fallo dominante de un N-1 real y el
mecanismo que desencadeno el apagon iberico del 28 de abril de 2025.

Este modulo no modifica el modelo: lo recibe ya construido y resuelto, y le
aplica el lazo externo que PyPSA no trae.

El metodo
---------
Es la conmutacion PV->PQ clasica de los flujos de carga de Newton-Raphson:

  1. Resolver con todas las barras de generacion en PV.
  2. Para cada generador, comprobar si su Q esta dentro de su curva de
     capacidad.
  3. El que la exceda deja de regular tension: pasa a PQ con su Q FIJADA en el
     limite violado. Su tension deja de ser una consigna y pasa a ser un
     resultado.
  4. Volver a resolver y repetir, porque al fijar unos la tension cambia y
     otros pueden entrar o salir de limite.
  5. Un generador ya conmutado puede VOLVER a PV si en la nueva solucion su
     tension se ha desviado en el sentido que indica que ya no necesita el
     limite. Sin esa vuelta atras el lazo se queda en una solucion peor que la
     real.

Curva de capacidad
------------------
Para una maquina sincrona de potencia aparente nominal S:

    Q_max = +sqrt(S^2 - P^2)        limite por calentamiento del rotor
    Q_min = -k * S                  limite de subexcitacion (estabilidad)

El limite superior es el del circulo de capacidad. El inferior lo fija la
estabilidad del angulo y el calentamiento de la cabeza del estator, y en
maquinas reales cae entre 0,2 y 0,4 de la nominal; se usa 0,35 por defecto,
configurable. No se modela el limite por potencia de accionamiento porque el
despacho activo viene fijado del caso de referencia.

Uso:
    python limites_reactivos.py                  # informe sobre el caso base
    python limites_reactivos.py --k 0.25         # subexcitacion mas estricta
    python limites_reactivos.py --sin-vuelta     # sin retorno PV, para comparar

    from stn.limites_reactivos import aplicar
    n, info = aplicar(network)
"""

import argparse
import sys

import numpy as np
import pandas as pd

# Fraccion de la potencia aparente nominal que la maquina puede absorber
# subexcitada. Rango tipico 0,2-0,4; 0,35 es un valor conservador.
K_SUBEXCITACION = 0.35

# Factor de potencia nominal de la maquina, para pasar de MW a MVA.
#
# PARATEC declara la "capacidad efectiva neta" en MW, que es potencia ACTIVA.
# La placa de un generador se da en MVA, y su relacion es S = P / cos(phi).
# Tomar los MW como si fueran MVA deja a toda maquina despachada a plena carga
# con Q_max = sqrt(S^2 - P^2) = 0, es decir incapaz de dar un solo MVAr, lo que
# es falso: una maquina a plena potencia activa sigue teniendo margen reactivo
# porque su placa aparente es mayor. Los turbogeneradores e hidraulicos grandes
# se disenan entre 0,85 y 0,95; 0,90 es el valor central habitual.
FP_NOMINAL = 0.90

# Factor de potencia que la CREG exige en el punto de conexion a la
# generacion por CONVERTIDOR (solar y eolica).
#
# POR QUE NO VALE LA CURVA SINCRONA PARA ELLAS. `K_SUBEXCITACION` es el
# limite de subexcitacion del ROTOR de una maquina sincrona: por debajo de
# cierto punto pierde estabilidad. Un inversor no tiene rotor y no tiene ese
# limite. Aplicarselo es inventar una restriccion fisica que no existe, y
# ademas se le concedia por arriba un cos phi de 0,72 ---sqrt(S^2-P^2) sobre
# una S supuesta--- donde la norma pide 0,95.
#
# Medido en la punta antes de corregirlo, sobre los cuatro parques del
# modelo (1.511 MW): CARACOLI entregaba 122,9 MVAr con 41,9 permitidos
# (2,9 veces), CUESTECITAS 88,1 con 49,3, y EL RIO estaba PEGADO a un
# Q_min de -72,9 MVAr que no deberia existir.
FP_CONVERTIDOR = 0.95

MAX_ITER = 12
TOL_MVAR = 0.5          # holgura antes de declarar violacion
TOL_V = 1e-4            # holgura de tension para la vuelta a PV


def capacidad(network, sis=None, k=None, fp=None):
    """Curva de capacidad de cada generador: (Q_min, Q_max) en MVAr.

    La potencia aparente nominal sale de `agc_deslastre.Sistema`, que la cruza
    con los reportes de generacion de PARATEC. Para los buses cuya inyeccion es
    un equivalente y no se conoce la maquina, se supone que opera al 80% de su
    capacidad, que es el mismo criterio que usa ese modulo.

    DATO REAL FRENTE A CURVA PARAMETRICA. Desde la descarga del 20-09-2026,
    los reportes "Detalle de unidad X" declaran, unidad a unidad, la
    "Capacidad de absorcion de reactivos (a Pmaxima)" y la "Capacidad de
    generacion de reactivos (a Pmaxima)". Donde existen se usan tal cual:
    son el limite que el operador reconoce, medido, y sustituyen a la
    aproximacion S = P/cos(phi) con Q_min = -k*S, que usaba dos valores
    tipicos de la literatura (0,90 y 0,35) no verificados maquina a maquina.

    Donde NO existen se conserva la curva parametrica, y la columna `fuente`
    dice cual se aplico en cada generador para que el diagnostico no mezcle
    las dos cosas en silencio.

    Ojo con la letra pequena: PARATEC declara esas capacidades *a P maxima*,
    mientras que la curva parametrica las da al P del caso. Usar el dato real
    es conservador cuando la maquina esta por debajo de su maximo ---su
    capacidad real de reactivo seria mayor--- que es el lado seguro.
    """
    # `k` y `fp` se reciben como PARAMETRO y no se leen del global. Antes
    # `aplicar()` mutaba `K_SUBEXCITACION` al arrancar, de modo que en
    # `comparar()` la columna "SIN limites" se medía con el k viejo y la
    # columna "CON limites" con el nuevo: la tabla atribuia a imponer los
    # limites un efecto que era, en realidad, el de cambiar k. Con
    # `--k 0.25` ---el ejemplo del docstring--- la comparacion no valia.
    k = K_SUBEXCITACION if k is None else float(k)
    fp = FP_NOMINAL if fp is None else float(fp)
    from stn.agc_deslastre import TIPOS_CONVERTIDOR
    if sis is None:
        from stn.agc_deslastre import Sistema
        sis = Sistema(network)

    filas = []
    for g in network.generators.index:
        p = float(network.generators.at[g, 'p_set'])
        mw = float(sis.gen.at[g, 'p_nom']) if g in sis.gen.index else max(p / 0.8, 1e-3)
        s = mw / fp                             # de MW de placa a MVA de placa
        s = max(s, abs(p) / fp, 1e-3)
        es_conv = (g in sis.gen.index
                   and str(sis.gen.at[g, 'tipo']) in TIPOS_CONVERTIDOR)
        if es_conv:
            # Banda simetrica de la norma, referida a la ACTIVA. Ver
            # FP_CONVERTIDOR: ni curva de rotor por abajo ni sqrt(S^2-P^2)
            # por arriba, porque ninguna de las dos describe un inversor.
            lim = abs(p) * float(np.tan(np.arccos(FP_CONVERTIDOR)))
            q_max, q_min, fuente = lim, -lim, 'CREG (convertidor)'
        else:
            q_max = float(np.sqrt(max(s * s - p * p, 0.0)))
            q_min = -k * s
            fuente = 'parametrica'

        if g in sis.gen.index and 'q_gen_mvar' in sis.gen.columns:
            qg = sis.gen.at[g, 'q_gen_mvar']
            qa = sis.gen.at[g, 'q_abs_mvar']
            if pd.notna(qg) and pd.notna(qa) and (float(qg) > 0 or float(qa) > 0):
                q_max = float(qg)
                q_min = -abs(float(qa))
                fuente = 'PARATEC'
        filas.append({'gen': g, 'P': p, 'S_nom': s,
                      'Q_min': q_min, 'Q_max': q_max, 'fuente': fuente})
    return pd.DataFrame(filas).set_index('gen')


def diagnostico(network, cap=None, k=None, fp=None):
    """Quien excede su curva de capacidad en la solucion actual."""
    if cap is None:
        cap = capacidad(network, k=k, fp=fp)
    q = network.generators_t.q.iloc[0]
    out = []
    for g in cap.index:
        if g not in q.index:
            continue
        qq = float(q[g])
        exceso = max(qq - cap.at[g, 'Q_max'], cap.at[g, 'Q_min'] - qq, 0.0)
        if exceso > TOL_MVAR:
            out.append({'gen': g, 'Q': qq, 'Q_min': cap.at[g, 'Q_min'],
                        'Q_max': cap.at[g, 'Q_max'], 'exceso_MVAr': exceso,
                        'sentido': 'sobreexcitado' if qq > cap.at[g, 'Q_max']
                                   else 'subexcitado'})
    return pd.DataFrame(out)


def apoyo_sobre_curva_supuesta(network, cap=None, k=None, fp=None,
                               tol=TOL_MVAR):
    """Cuanto reactivo descansa en una curva que NO sale de un dato.

    POR QUE HACE FALTA. "0 generadores fuera de curva" es facil de leer como
    "todas las maquinas operan dentro de sus limites fisicos", y para una
    parte del parque no significa eso: la curva de esas maquinas se CONSTRUYE
    a partir de su propio despacho.

    La cadena, para los generadores sin placa en PARATEC:
        p_nom = p_set / 0,8      (se supone la maquina al 80 % de su placa)
        S_nom = p_nom / 0,90     (se supone la placa con cos phi 0,90)
        Q_max = sqrt(S_nom^2 - P^2)
    Compuesto: S_nom = 1,389 P y Q_max = 0,964 P. La aritmetica es correcta
    ---una maquina aliviada de activa tiene mas margen de reactivo--- pero el
    margen entero cuelga de un factor de carga SUPUESTO. Si esas maquinas
    estuvieran al 95 % de su placa y no al 80 %, su Q_max seria una fraccion
    del que el modelo les concede.

    Medido en la punta: 15 de 45 curvas son parametricas y CUATRO generadores
    quedan pegados exactamente a ese limite, sosteniendo 174,3 MVAr.

    Devuelve (df_pegados, mvar_en_juego, n_parametricas).
    """
    if cap is None:
        cap = capacidad(network, k=k, fp=fp)
    q = network.generators_t.q.iloc[0]
    filas = []
    for g in cap.index:
        if g not in q.index or str(cap.at[g, 'fuente']) == 'PARATEC':
            continue
        qq = float(q[g])
        arriba = abs(qq - float(cap.at[g, 'Q_max'])) < tol
        abajo = abs(qq - float(cap.at[g, 'Q_min'])) < tol
        if arriba or abajo:
            filas.append({
                'gen': g, 'P': float(cap.at[g, 'P']),
                'S_nom_supuesto': float(cap.at[g, 'S_nom']),
                'Q': qq,
                # Cual de los dos limites muerde. Importa: el de arriba sale
                # de sqrt(S^2-P^2) y el de abajo de K_SUBEXCITACION, que es
                # un limite de maquina SINCRONA aplicado tambien a parques
                # por convertidor, que no lo tienen.
                'limite': 'Q_max' if arriba else 'Q_min (subexcitacion)',
                'valor': float(cap.at[g, 'Q_max' if arriba else 'Q_min'])})
    df = pd.DataFrame(filas)
    # Solo cuentan como "supuesta" las PARAMETRICAS. Las de convertidor salen
    # de la banda que exige la CREG, que es una norma, no una suposicion.
    n_par = int((cap.fuente.astype(str) == 'parametrica').sum())
    if len(df):
        df = df[df.gen.map(lambda g: str(cap.at[g, 'fuente']) == 'parametrica')]
    return df, (float(df.Q.abs().sum()) if len(df) else 0.0), n_par


def aplicar_si_procede(network, verbose=False):
    """Impone la curva de capacidad si `config.LIMITES_REACTIVOS` lo pide.

    UN SOLO SITIO DONDE SE DECIDE, y hay motivo. Cuatro modulos construyen la
    red por su cuenta ---main, validacion, contingencias.caso_base y
    mapa_interactivo--- y cada vez que se añade un flag que afecta al caso
    base hay que acordarse de cablearlo en los cuatro. Ya se fallo dos veces:

      * Con DESGLOSE_BRUTO, que validacion.py no aplicaba. Se estuvo afirmando
        durante dias que activarlo dejaba la validacion "identica al digito"
        porque se media siempre la red sin desglosar.
      * Con LIMITES_REACTIVOS, que main.py no aplicaba. El pipeline principal
        informaba 125,0 MW de perdidas y 0,9857 pu de tension minima mientras
        todos los modulos de analisis trabajaban sobre 123,4 y 0,9912.

    Los dos fallos son el mismo: una red que nadie usa produce cifras que no
    significan nada, y el sintoma ---dos modulos que no concuerdan--- solo
    aparece si alguien los compara. Con esta funcion, cablear un flag nuevo es
    una linea en un sitio.

    Devuelve la red, conmutada o no. No modifica la que recibe.
    """
    from stn import config
    if not getattr(config, 'LIMITES_REACTIVOS', False):
        return network
    red, _info = aplicar(network, verbose=verbose)
    return red


def aplicar(network, k=None, max_iter=MAX_ITER, vuelta_a_pv=True, verbose=True):
    """Resuelve imponiendo los limites de reactivo. Devuelve (red, info).

    La red original NO se modifica: se trabaja sobre una copia.
    """
    from stn.power_flow import run_power_flow

    k_efec = K_SUBEXCITACION if k is None else float(k)
    n = network.copy()
    cap = capacidad(n, k=k_efec)
    # tension que cada barra PV deberia sostener, para decidir la vuelta atras
    consigna = {g: float(n.buses.at[n.generators.at[g, 'bus'], 'v_mag_pu_set'])
                for g in n.generators.index}
    # Los que LLEGAN ya en PQ con su Q en el limite de su curva tambien
    # cuentan como fijados. Si no se siembran aqui, el paso 2 nunca los mira
    # ---solo recorre `fijados`--- y el paso 1 tampoco, porque su Q esta
    # exactamente sobre el limite y no lo excede. Quedaban congelados en su Q
    # del caso base, incapaces de regular tension pasara lo que pasara en la
    # contingencia. Con `config.LIMITES_REACTIVOS` activo eso son doce
    # maquinas en cada caso de `--con-limites`, justo las que tocan limite.
    fijados = {}
    for g in n.generators.index:
        if n.generators.at[g, 'control'] != 'PQ' or g not in cap.index:
            continue
        qs = float(n.generators.at[g, 'q_set'])
        if (abs(qs - cap.at[g, 'Q_max']) <= TOL_MVAR
                or abs(qs - cap.at[g, 'Q_min']) <= TOL_MVAR):
            fijados[g] = qs
    historia = []

    for it in range(1, max_iter + 1):
        ok, _ = run_power_flow(n, verbose=False)
        if not ok:
            historia.append({'iter': it, 'convergio': False})
            break

        q = n.generators_t.q.iloc[0]
        v = n.buses_t.v_mag_pu.iloc[0]
        cambios = 0

        # 1. los que violan su curva pasan a PQ en el limite
        for g in cap.index:
            if g in fijados or g not in q.index:
                continue
            if n.generators.at[g, 'control'] == 'Slack':
                continue        # el slack no se conmuta: cerraria el balance
            qq = float(q[g])
            if qq > cap.at[g, 'Q_max'] + TOL_MVAR:
                lim = cap.at[g, 'Q_max']
            elif qq < cap.at[g, 'Q_min'] - TOL_MVAR:
                lim = cap.at[g, 'Q_min']
            else:
                continue
            n.generators.loc[g, 'control'] = 'PQ'
            n.generators.loc[g, 'q_set'] = lim
            fijados[g] = lim
            cambios += 1

        # 2. los ya fijados que ya no necesitan estarlo vuelven a PV
        if vuelta_a_pv:
            for g in list(fijados):
                bus = n.generators.at[g, 'bus']
                vv, vref = float(v[bus]), consigna[g]
                # si estaba en Q_max la tension se le queda BAJA; volver a PV
                # solo tiene sentido si ahora la tension se ha ido por encima
                sube = fijados[g] >= cap.at[g, 'Q_max'] - TOL_MVAR
                if (sube and vv > vref + TOL_V) or (not sube and vv < vref - TOL_V):
                    n.generators.loc[g, 'control'] = 'PV'
                    n.generators.loc[g, 'q_set'] = 0.0
                    del fijados[g]
                    cambios += 1

        historia.append({'iter': it, 'convergio': True,
                         'fijados': len(fijados), 'cambios': cambios})
        if cambios == 0:
            break

    ok, _ = run_power_flow(n, verbose=False)
    v = n.buses_t.v_mag_pu.iloc[0].astype(float)
    # El lazo PV/PQ convergio SI Y SOLO SI la ultima pasada no cambio nada.
    # `convergio` a secas es la convergencia numerica del ultimo flujo, que es
    # otra cosa: un lazo que agota `max_iter` oscilando entre PV y PQ deja un
    # flujo perfectamente convergido y una solucion que no es la de equilibrio.
    # `contingencias.simular` leia solo `convergio`, asi que un caso cuyo lazo
    # nunca se asento se reportaba como un N-1 limpio, que es lo contrario de
    # para lo que existe `--con-limites`.
    lazo_ok = bool(historia) and historia[-1].get('cambios', 1) == 0
    info = {
        'convergio': bool(ok),
        'convergio_lazo': lazo_ok,
        'iteraciones': len(historia),
        'conmutados': fijados,
        'historia': pd.DataFrame(historia),
        'v_min': float(v.min()), 'v_max': float(v.max()),
        'v_fuera': int(((v < 0.9) | (v > 1.1)).sum()),
        'restantes': diagnostico(n, cap),
    }

    if verbose:
        print(f"\nLimites de reactivo (k = {K_SUBEXCITACION:.2f})")
        print(f"  iteraciones            : {info['iteraciones']}")
        print(f"  generadores conmutados : {len(fijados)} de {len(n.generators)}")
        print(f"  convergio el flujo     : {info['convergio']}")
        print(f"  convergio el lazo PV/PQ: {info['convergio_lazo']}"
              + ("" if lazo_ok else
                 f"   <- agoto las {max_iter} iteraciones sin asentarse"))
        print(f"  tension                : [{info['v_min']:.4f}, {info['v_max']:.4f}] "
              f"pu   fuera de [0,9-1,1]: {info['v_fuera']}")
        if len(fijados):
            print("\n  Generadores que dejaron de regular tension:")
            for g, lim in sorted(fijados.items(), key=lambda x: x[1]):
                bus = n.generators.at[g, 'bus']
                print(f"     {g[:34]:34s} Q fijada {lim:8.1f} MVAr   "
                      f"v {float(v[bus]):.4f} (consigna {consigna[g]:.4f})")
        if len(info['restantes']):
            print(f"\n  AVISO: {len(info['restantes'])} siguen fuera de curva "
                  f"tras el lazo (revisar convergencia)")
    return n, info


def comparar(network, k=None, fp=None, vuelta_a_pv=True, verbose=True):
    """Contrasta el caso sin limites con el caso con limites.

    LAS DOS COLUMNAS SE MIDEN CON LA MISMA CURVA. `k` y `fp` se pasan tanto al
    diagnostico de partida como al lazo, porque antes `aplicar()` mutaba el
    global `K_SUBEXCITACION` al arrancar: la columna "SIN limites" quedaba
    medida con el k anterior y la "CON limites" con el nuevo, de modo que la
    tabla atribuia a imponer los limites un efecto que era el de haber
    cambiado k. Con `--k 0.25`, que es el ejemplo del docstring del modulo, la
    comparacion no significaba nada.
    """
    q0 = network.generators_t.q.iloc[0]
    v0 = network.buses_t.v_mag_pu.iloc[0].astype(float)
    d0 = diagnostico(network, k=k, fp=fp)

    n, info = aplicar(network, k=k, vuelta_a_pv=vuelta_a_pv, verbose=False)
    v1 = n.buses_t.v_mag_pu.iloc[0].astype(float)
    q1 = n.generators_t.q.iloc[0]

    if verbose:
        print("\nEfecto de imponer los limites de reactivo")
        print(f"  {'':28s} {'SIN limites':>14s} {'CON limites':>14s}")
        print(f"  {'generadores fuera de curva':28s} {len(d0):14d} "
              f"{len(info['restantes']):14d}")
        print(f"  {'Q total de generadores':28s} {q0.sum():13.0f}  {q1.sum():13.0f}")
        print(f"  {'|Q| maxima':28s} {q0.abs().max():13.0f}  {q1.abs().max():13.0f}")
        print(f"  {'tension minima':28s} {v0.min():14.4f} {v1.min():14.4f}")
        print(f"  {'tension maxima':28s} {v0.max():14.4f} {v1.max():14.4f}")
        # Cuanto de ese "dentro de curva" descansa en una curva supuesta.
        try:
            peg, mvar, n_par = apoyo_sobre_curva_supuesta(n)
            if n_par:
                print(f"\n  [!] {n_par} de {len(capacidad(n))} curvas son "
                      f"PARAMETRICAS, no de PARATEC: se construyen a partir "
                      f"del propio")
                print(f"      despacho (p_nom = p_set/0,8; S_nom = p_nom/0,90). "
                      f"Estar 'dentro de curva' frente")
                print(f"      a una curva derivada del despacho no dice lo "
                      f"mismo que estarlo frente a la placa.")
                if len(peg):
                    print(f"      {len(peg)} generadores quedan PEGADOS a ese "
                          f"limite supuesto, sosteniendo {mvar:.1f} MVAr:")
                    for _, r in peg.iterrows():
                        print(f"         {r.gen:26s} P={r.P:7.1f}  "
                              f"Q={r.Q:7.1f} = {r.limite:22s} "
                              f"(S_nom supuesto {r.S_nom_supuesto:.1f} MVA)")
        except Exception as e:                               # noqa: BLE001
            print(f"  (no se pudo medir el apoyo sobre curva supuesta: {e})")
        print(f"  {'barras fuera de rango':28s} "
              f"{int(((v0<0.9)|(v0>1.1)).sum()):14d} {info['v_fuera']:14d}")
        peor = (v1 - v0).abs()
        if len(peor):
            b = peor.idxmax()
            print(f"  mayor cambio de tension: {b} {v0[b]:.4f} -> {v1[b]:.4f}")
        if not info['convergio_lazo']:
            print(f"  AVISO: el lazo PV/PQ agoto sus iteraciones sin asentarse; "
                  f"la solucion de la derecha no es de equilibrio")
    return n, info


def main():
    global FP_NOMINAL
    ap = argparse.ArgumentParser(
        description="Limites de reactivo y conmutacion PV->PQ sobre el STN.")
    ap.add_argument('--k', type=float, default=K_SUBEXCITACION,
                    help="limite de subexcitacion en fraccion de la nominal")
    ap.add_argument('--fp', type=float, default=FP_NOMINAL,
                    help="factor de potencia nominal para pasar de MW a MVA")
    ap.add_argument('--sin-vuelta', action='store_true',
                    help="no devolver generadores a PV, para comparar")
    ap.add_argument('--detalle', action='store_true',
                    help="lista completa de quien excede su curva")
    from stn import punto_operacion as PO
    PO.anadir_flag(ap)
    args = ap.parse_args()

    # `--fp` tiene que surtir efecto ANTES del primer diagnostico. Estaba
    # despues, asi que la cuenta de "sin limites" de aqui arriba usaba 0,90 y
    # la de la tabla de `comparar()` el valor pedido: dos cifras que dicen lo
    # mismo y no coincidian en cuanto se usaba `--fp`.
    FP_NOMINAL = args.fp

    print("Construyendo el punto de operacion...")
    # SIN limitar: este modulo existe para enseñar el antes y el despues.
    # `limites=False` tiene que llegar a los DOS constructores, porque el
    # diurno tambien conmuta a PQ al final de `escenario_diurno.construir()`:
    # si heredara la red ya limitada, compararia el caso limitado consigo
    # mismo y no habria ninguna violacion que enseñar.
    base, _, ctx = PO.de_args(args, limites=False, verbose=False)
    PO.cabecera(ctx)

    d = diagnostico(base, k=args.k, fp=args.fp)
    print(f"\nSin limites: {len(d)} de {len(base.generators)} generadores "
          f"exceden su curva de capacidad")
    if args.detalle and len(d):
        print(d.round(1).to_string(index=False))

    # `--sin-vuelta` se parseaba y no se usaba en ningun sitio.
    comparar(base, k=args.k, fp=args.fp, vuelta_a_pv=not args.sin_vuelta)
    return 0


if __name__ == '__main__':
    sys.exit(main())
