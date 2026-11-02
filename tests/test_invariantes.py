# test_invariantes.py
"""Comprueba los invariantes del modelo del STN.

    python test_invariantes.py          # todo
    python test_invariantes.py -k milp  # solo lo que case con 'milp'

POR QUE EXISTE. Hasta ahora estas comprobaciones se hacian a mano cada vez que
alguien auditaba el modelo, y se olvidaban entre sesiones. El resultado fue que
el MISMO fallo aparecio tres veces: un flag de config.py cableado en unos
modulos y no en otros, de modo que cada uno analizaba una red distinta.

  * DESGLOSE_BRUTO no se aplicaba en validacion.py. Se estuvo afirmando
    durante dias que activarlo dejaba la validacion "identica al digito",
    porque se media siempre la red sin desglosar.
  * LIMITES_REACTIVOS no se aplicaba en main.py ni en mapa_interactivo.py. El
    pipeline principal informaba 125,0 MW de perdidas y 0,9857 pu de tension
    minima mientras los modulos de analisis trabajaban sobre 123,4 y 0,9912.

La prueba `test_los_cuatro_caminos_coinciden` existe precisamente para que no
haya una cuarta vez. Las demas codifican lo que se verifico a mano el
21-9-2026: balance de potencia, limites fisicos, cobertura del EDAC, y que
ni el MILP ni la cascada inventen o destruyan energia.

NO son pruebas de regresion numerica: no fijan el nadir ni el umbral, que
cambian legitimamente al mejorar los datos. Fijan lo que NO puede cambiar sin
que algo este mal.
"""

import sys

import numpy as np

# Un solo caso base para todas las pruebas: construirlo cuesta ~12 s.
_CACHE = {}


def base():
    if 'n' not in _CACHE:
        import logging
        logging.disable(logging.INFO)
        from stn import contingencias as C
        from stn import agc_deslastre as AGC
        _CACHE['n'], _ = C.caso_base(verbose=False)
        _CACHE['s'] = AGC.Sistema(_CACHE['n'])
    return _CACHE['n'], _CACHE['s']


# ---------------------------------------------------------------------------
# Coherencia entre modulos
# ---------------------------------------------------------------------------

def test_los_cuatro_caminos_coinciden():
    """caso_base y validacion deben resolver la MISMA red.

    Si uno se salta un flag, analiza un sistema que no existe y sus cifras no
    significan nada. Es el fallo que aparecio dos veces; esta prueba lo
    convierte en un fallo ruidoso.

    QUEDAN DOS CAMINOS INDEPENDIENTES, NO CUATRO. `main.py` y
    `mapa_interactivo.py` replicaban la secuencia a mano y hoy ya no: los dos
    entran por una fuente y por `contingencias.caso_base()`, asi que coinciden
    por construccion y compararlos aqui no probaria nada.
    `validacion.resolver_modelo()` sigue teniendo su propia copia ---y tiene
    que tenerla, porque contrasta SIEMPRE la punta de PowerFactory aunque el
    resto del modelo este corriendo sobre otra fecha---, asi que es la que
    sigue mereciendo la comparacion.
    """
    from stn import contingencias as C
    from stn import validacion as V
    n_base, _ = base()
    n_val, _ = V.resolver_modelo()

    for etq, a, b in (
            ('numero de buses', len(n_base.buses), len(n_val.buses)),
            ('numero de lineas', len(n_base.lines), len(n_val.lines)),
            ('generadores en PQ',
             int((n_base.generators.control == 'PQ').sum()),
             int((n_val.generators.control == 'PQ').sum()))):
        assert a == b, f"{etq}: caso_base={a} pero validacion={b}"

    perd_b = float(n_base.generators_t.p.iloc[0].sum()
                   - n_base.loads_t.p.iloc[0].sum())
    perd_v = float(n_val.generators_t.p.iloc[0].sum()
                   - n_val.loads_t.p.iloc[0].sum())
    assert abs(perd_b - perd_v) < 0.05, (
        f"las perdidas no coinciden: caso_base {perd_b:.2f} MW, "
        f"validacion {perd_v:.2f} MW. Algun flag de config.py esta cableado "
        f"en un modulo y no en el otro.")

    v_b = float(n_base.buses_t.v_mag_pu.iloc[0].min())
    v_v = float(n_val.buses_t.v_mag_pu.iloc[0].min())
    assert abs(v_b - v_v) < 1e-4, (
        f"la tension minima no coincide: {v_b:.4f} vs {v_v:.4f}")


# ---------------------------------------------------------------------------
# El caso base
# ---------------------------------------------------------------------------

def test_el_caso_base_es_una_sola_isla():
    n, _ = base()
    import networkx as nx
    from stn import contingencias as C
    comps = list(nx.connected_components(C._grafo(n)))
    assert len(comps) == 1, f"la red esta partida en {len(comps)} islas"
    assert len(comps[0]) == len(n.buses), "hay buses fuera del grafo"


def test_redesignar_el_slack_no_deja_dos_ni_borra_los_pq():
    """Un solo slack por isla, y sin arrasar las conmutaciones a PQ.

    Dos fallos opuestos en las dos implementaciones que existen:

      * `power_flow.designar_slack` no devolvia a PV el slack anterior, asi
        que redesignar sobre una red ya modificada dejaba DOS. Con dos slacks
        en la misma sub-red PyPSA elige uno y el reparto de perdidas deja de
        ser el que uno cree. Alcanzable desde `aplicar_evento` y desde
        `contingencias.simular`.
      * `cascada._redesignar_slack` si reseteaba, pero de mas: ponia la
        columna entera a 'PV' y se llevaba por delante los doce generadores
        que `config.LIMITES_REACTIVOS` deja en PQ con su Q fijada, deshaciendo
        el lazo de limites de reactivo.
    """
    from stn import cascada as K
    from stn.power_flow import designar_slack
    n, _ = base()
    pq0 = int((n.generators.control == 'PQ').sum())

    # redesignar tras cambiar quien es el mayor
    m = n.copy()
    sl = m.generators[m.generators.control == 'Slack'].index[0]
    m.generators.loc[sl, 'p_set'] = 1.0
    designar_slack(m, verbose=False)
    n_sl = int((m.generators.control == 'Slack').sum())
    assert n_sl == 1, (
        f"designar_slack deja {n_sl} slacks: "
        f"{list(m.generators[m.generators.control == 'Slack'].index)}")
    assert int((m.generators.control == 'PQ').sum()) == pq0, (
        "designar_slack se llevo por delante conmutaciones a PQ")

    # y la version de la cascada
    m2 = n.copy()
    K._redesignar_slack(m2)
    assert int((m2.generators.control == 'Slack').sum()) == 1, (
        "_redesignar_slack no deja exactamente un slack")
    assert int((m2.generators.control == 'PQ').sum()) == pq0, (
        f"_redesignar_slack dejo {int((m2.generators.control == 'PQ').sum())} "
        f"PQ de {pq0}: esta deshaciendo los limites de reactivo")


def test_el_balance_de_potencia_cierra():
    n, _ = base()
    gen = float(n.generators_t.p.iloc[0].sum())
    car = float(n.loads_t.p.iloc[0].sum())
    perd = gen - car
    assert 0 < perd < 0.05 * car, (
        f"perdidas de {perd:.1f} MW sobre {car:.1f} de demanda: "
        f"o son negativas o pasan del 5 %")


def test_las_tensiones_estan_en_rango():
    n, _ = base()
    v = n.buses_t.v_mag_pu.iloc[0].astype(float)
    fuera = v[(v < 0.9) | (v > 1.1)]
    assert len(fuera) == 0, f"{len(fuera)} barras fuera de [0,9-1,1]: {dict(fuera)}"


def test_nada_supera_su_capacidad_nominal():
    n, _ = base()
    car = 100 * n.lines_t.p0.iloc[0].abs() / n.lines.s_nom.replace(0, np.nan)
    assert car.max() <= 100.0, (
        f"linea sobre su nominal: {car.idxmax()} al {car.max():.1f} %")
    if len(n.transformers):
        t = (100 * n.transformers_t.p0.iloc[0].abs()
             / n.transformers.s_nom.replace(0, np.nan))
        assert t.max() <= 100.0, f"trafo al {t.max():.1f} % de su nominal"


def test_el_desglose_conserva_la_inyeccion_neta():
    """Gen(b) - Carga(b) = P_iny(b) bus a bus. Es LA invariante del desglose."""
    n, _ = base()
    g = n.generators.groupby('bus').p_set.sum()
    c = n.loads.groupby('bus').p_set.sum()
    neta = g.reindex(n.buses.index).fillna(0) - c.reindex(n.buses.index).fillna(0)
    assert np.isfinite(neta).all(), "hay inyecciones netas no finitas"
    # el slack es el unico que puede desviarse, porque cierra el balance
    assert abs(float(neta.sum()) - (float(g.sum()) - float(c.sum()))) < 1e-6


# ---------------------------------------------------------------------------
# Limites fisicos de las maquinas
# ---------------------------------------------------------------------------

def test_la_placa_repartida_no_inventa_capacidad():
    """Una planta en varias barras suma lo que PARATEC declara, ni un MVA mas.

    COMPROBACION INDEPENDIENTE: el valor esperado sale del registro de
    PARATEC, no del modelo. Es lo que faltaba, y su ausencia dejo pasar dos
    fallos a la vez:

    1. El grupo de barras que comparten planta se identificaba por la FIRMA
       (tipo, n_unidades, s_mva) y no por la subestacion. MIEL I y PORCE II
       son plantas distintas con la misma firma ---hidraulicas, 3 unidades,
       450 MVA--- y el modelo las fusionaba, dando 225 MVA a cada una.
    2. La placa se repartia a partes iguales en vez de por despacho. San
       Carlos entrega el 83 % por su barra de 230 kV y el 17 % por la de 500,
       pero recibia 680/680; el `max(p_nom, p)` rescataba la de 230 hasta su
       despacho y la planta acababa sumando 1.880,2 MVA frente a 1.360,0.

    Lo que este invariante NO puede exigir es igualdad exacta, porque hay una
    contradiccion real entre fuentes: el caso de PowerFactory despacha PAIPA y
    SAN CARLOS por encima de su placa declarada. Eso se tolera y se acota.
    """
    import collections
    from stn import paratec_equipos as PE

    n, s = base()
    par = PE.parametros_por_bus(list(n.buses.index))
    g = s.gen

    grupos = collections.defaultdict(list)
    for gen_id in g.index:
        bus = g.at[gen_id, 'bus']
        if bus in par.index and float(par.at[bus, 's_mva'] or 0) > 0:
            grupos[bus.rsplit('__', 1)[0]].append(gen_id)

    assert grupos, "ninguna barra casa con el registro de PARATEC"
    repartidas = {k: v for k, v in grupos.items() if len(v) > 1}
    assert repartidas, "no hay ninguna planta repartida; el caso cambio"

    # El invariante es una IGUALDAD exacta, y sale de dos lineas de algebra.
    # Con el prorrateo por despacho, cuota_i = declarado * p_i / sum(p):
    #
    #   si declarado >= sum(p):  cuota_i >= p_i para todo i, el max() no toca
    #                            nada y la suma vale `declarado`
    #   si declarado <  sum(p):  cuota_i <  p_i para todo i, el max() sube
    #                            cada barra a su despacho y la suma vale sum(p)
    #
    # En los dos casos: sum(p_nom) == max(declarado, sum(p_set)).
    #
    # Con el reparto a partes iguales la igualdad se rompe, porque unas barras
    # quedan por encima de su cuota y otras por debajo y el max() solo corrige
    # en un sentido. San Carlos daba 1.880,2 cuando el maximo posible es
    # 1.440,2. Por eso una desigualdad con holgura no sirve: hay que exigir la
    # igualdad.
    for sub, gens in repartidas.items():
        declarado = float(par.at[g.at[gens[0], 'bus'], 's_mva'])
        suma = float(g.loc[gens, 'p_nom'].sum())
        despacho = float(g.loc[gens, 'p_set'].sum())
        esperado = max(declarado, despacho)
        assert abs(suma - esperado) < 1.0, (
            f"{sub}: el modelo le da {suma:.1f} MVA y deberia dar "
            f"{esperado:.1f} = max(declarado {declarado:.1f}, despacho "
            f"{despacho:.1f})")


def test_ningun_generador_despacha_sobre_su_placa():
    _, s = base()
    ex = s.gen[s.gen.p_set > s.gen.p_nom + 1e-6]
    assert len(ex) == 0, f"{len(ex)} generadores sobre su placa: {list(ex.index)}"
    assert (s.gen.p_set >= -1e-6).all(), "hay consignas negativas"


def test_la_capacidad_sincronizada_es_coherente():
    """p_set <= s_sinc <= p_nom. No se puede girar menos de lo que se entrega."""
    _, s = base()
    mal = s.gen[(s.s_sinc > s.gen.p_nom + 1e-6)
                | (s.s_sinc < s.gen.p_set - 1e-6)]
    assert len(mal) == 0, f"capacidad sincronizada incoherente en {list(mal.index)}"


def test_la_reserva_cumple_la_norma_y_cabe_en_la_placa_de_paratec():
    """Dos anclas EXTERNAS: la CREG por abajo y PARATEC por arriba.

    La version anterior comparaba `reserva` contra `(s_sinc - p_set)`, que es
    la misma expresion con la que `_reserva_de` la calcula. No podia fallar, y
    por tanto no podia detectar ningun valor de `RESERVA_PRIMARIA`: el 10 %
    cableado podria subir al 27 % y la prueba seguiria verde.

    Las dos cotas de aqui no salen del modulo:

    POR ABAJO, la Resolucion CREG 023 de 2001 art. 4 exige una reserva
    primaria de al menos el 3 % de la generacion horaria programada. El
    modelo usa el 10 % de la capacidad girando, que es un valor de literatura
    y una magnitud distinta; lo que no puede es quedar por debajo del minimo
    obligatorio. Esta contradiccion entre las dos lecturas esta abierta y
    conocida y declarada: mueve el umbral mas de un punto.

    POR ARRIBA, la capacidad que PARATEC declara, releida del registro y sin
    pasar por `s_sinc`. Ninguna maquina puede subir por encima de su placa, y
    eso no depende de ninguna hipotesis de acoplamiento.
    """
    from stn import agc_deslastre as AGC
    from stn import paratec_equipos as PE
    n, s = base()

    # --- ancla de abajo: el minimo de la CREG --------------------------
    minimo_creg = 0.03 * s.P_gen
    assert s.reserva >= minimo_creg, (
        f"reserva de {s.reserva:.1f} MW, por debajo del 3 % de la generacion "
        f"programada que exige CREG 023 art. 4 ({minimo_creg:.1f} MW)")

    # --- ancla de arriba: la placa de PARATEC --------------------------
    par = PE.parametros_por_bus(list(n.buses.index))
    bus_de = s.gen.bus
    comprobadas = 0
    for gen_id, r in s.reserva_por_gen.items():
        bus = bus_de.get(gen_id)
        if bus not in par.index:
            continue
        declarada = float(par.at[bus, 's_mva'] or 0.0)
        if declarada <= 0:
            continue
        comprobadas += 1
        hueco = max(0.0, declarada - float(s.gen.at[gen_id, 'p_set']))
        assert float(r) <= hueco + 1e-6, (
            f"{gen_id}: {float(r):.1f} MW de reserva y solo le quedan "
            f"{hueco:.1f} hasta los {declarada:.1f} MVA que declara PARATEC")
    assert comprobadas >= 10, (
        f"solo {comprobadas} maquinas contrastadas contra PARATEC; la cota "
        f"de arriba no esta cubriendo casi nada")

    # --- coherencia interna que si vale la pena ------------------------
    # la reserva a bajar no puede pasar del propio despacho: una maquina no
    # genera negativo. Esto NO es tautologico: `baja` se acota por
    # `s_sinc * RESERVA_PRIMARIA` y por `p_set`, y el fallo seria olvidar el
    # segundo tope.
    assert (s.reserva_baja_por_gen <= s.gen.p_set + 1e-6).all(), (
        "alguna maquina tiene mas reserva a bajar que despacho")
    assert s.reserva_baja <= s.P_gen + 1e-6

    # y la constante tiene que seguir siendo la documentada; si alguien la
    # cambia, que sea una decision visible y no un efecto lateral
    assert abs(AGC.RESERVA_PRIMARIA - 0.10) < 1e-9, (
        f"RESERVA_PRIMARIA vale {AGC.RESERVA_PRIMARIA}; el README y el "
        f"informe documentan el 10 % y las cifras publicadas dependen de el")


def test_la_generacion_por_convertidor_no_aporta_primaria():
    """Ni inercia ni regulacion: CREG 060 de 2019 art. 12 par. 2."""
    from stn import agc_deslastre as AGC
    _, s = base()
    conv = s.gen.tipo.isin(AGC.TIPOS_CONVERTIDOR)
    assert float(s.reserva_por_tipo.reindex(
        list(AGC.TIPOS_CONVERTIDOR)).fillna(0).sum()) == 0.0, (
        "los convertidores estan aportando reserva primaria")
    assert float(s.h_por_gen[conv].abs().sum()) == 0.0, (
        "los convertidores tienen inercia distinta de cero")


def test_el_estatismo_cumple_la_norma():
    """CREG 023 de 2001 art. 4: entre 4 y 6 % para convencionales."""
    _, s = base()
    assert 0.04 <= s.R_eq <= 0.06, (
        f"estatismo equivalente {100*s.R_eq:.2f} %, fuera del 4-6 % exigido")


def test_la_banda_muerta_cumple_la_norma():
    """CREG 023 de 2001 art. 4: <= 30 mHz."""
    from stn import agc_deslastre as AGC
    assert AGC.BANDA_MUERTA_HZ <= 0.030 + 1e-12, (
        f"banda muerta de {1000*AGC.BANDA_MUERTA_HZ:.0f} mHz, pasa de 30")


# ---------------------------------------------------------------------------
# Dinamica de frecuencia
# ---------------------------------------------------------------------------

def test_el_agc_no_despliega_mas_reserva_de_la_que_existe():
    """Se MIDE sobre la red: cuanto suben de verdad las maquinas.

    POR QUE ESTA PRUEBA ESTA ESCRITA ASI. La version anterior comprobaba
    `reserva_por_gen.sum() == reserva` y `reserva_por_gen <= s_sinc - p_set`.
    Las dos son tautologias: `_reserva_de` hace `destino.reserva =
    float(res.sum())` y `destino.reserva_por_gen = res` ---son el MISMO
    objeto, la diferencia medida es 0,000e+00 exacto--- y el `hueco` del test
    era caracter por caracter el `margen` del codigo.

    Peor: el docstring prometia proteger contra que `aplicar_evento` y
    `reparto_optimo` recalculasen la reserva por su cuenta, y el test NO
    LLAMABA A NINGUNA DE LAS DOS. Si una volviera a recalcularla sobre `p_nom`
    ---que es el fallo historico, 1.534,2 MW desplegados donde se declaraban
    830,0--- la prueba pasaba igual.

    Aqui se provoca un desbalance mucho mayor que la reserva y se mide el
    despliegue en el objeto PyPSA resultante. Si alguna de las dos mitades
    vuelve a desincronizarse, las maquinas suben mas de lo declarado y se ve.
    """
    import pandas as pd
    from stn import agc_deslastre as AGC
    n, s = base()

    # un desbalance imposible de cubrir: el AGC tiene que saturar
    bus = n.loads.groupby('bus').p_set.sum().idxmax()
    extra = pd.Series({bus: 5.0 * max(s.reserva, 1.0)})
    n2, _ = AGC.aplicar_evento(n, [], pd.Series(dtype=float), s,
                               deficit_mw=0.0, carga_extra=extra,
                               verbose=False)

    delta = (n2.generators.p_set - n.generators.p_set).reindex(
        n.generators.index).fillna(0.0)
    subida = float(delta.clip(lower=0.0).sum())
    assert subida <= s.reserva + 1.0, (
        f"ante un desbalance de {float(extra.sum()):.0f} MW el AGC sube "
        f"{subida:.1f} MW y la reserva declarada es {s.reserva:.1f}")
    # y tiene que desplegarla ENTERA, o el tope no es el que se cree
    assert subida >= s.reserva - 1.0, (
        f"el AGC solo despliega {subida:.1f} MW de los {s.reserva:.1f} que "
        f"declara: el tope efectivo no es `reserva`")

    # ninguna maquina por convertidor puede haber subido
    conv = s.gen.index[s.sin_primaria]
    subio_conv = float(delta.reindex(conv).fillna(0.0).clip(lower=0.0).sum())
    assert subio_conv < 1e-6, (
        f"{subio_conv:.1f} MW de la subida van a generacion por convertidor")

    # ninguna maquina puede acabar sobre su placa
    sobre = n2.generators.p_set - s.gen.p_nom.reindex(n2.generators.index)
    assert float(sobre.max()) < 1.0, (
        f"tras el AGC alguna maquina queda {float(sobre.max()):.1f} MW sobre "
        f"su placa: {sobre.idxmax()}")


def test_el_edac_nunca_pasa_del_40_por_ciento():
    """Es su cobertura nominal: 8 etapas del 5 % (Acuerdo CNO 2105 de 2026).

    LA TOLERANCIA ES ESTRECHA A PROPOSITO. Con el 40,5 % que tenia antes, este
    invariante dejo pasar un error real: al meter las perdidas de red dentro de
    `P_carga` el 3-10-2026, cada escalon paso a dimensionarse sobre el consumo
    (11.690,0 MW) en vez de sobre la demanda (11.566,6), y el esquema completo
    cubria el 40,43 %. Pasaba por siete centesimas de punto.

    El acuerdo define el escalon sobre la DEMANDA. Las perdidas de red no se
    pueden deslastrar, asi que no entran en la base.
    """
    from stn import agc_deslastre as AGC
    _, s = base()

    # cada escalon tiene que ser exactamente el 5 % de la demanda
    _, _, e, _ = AGC.simular_frecuencia(s, deficit_mw=3000., con_edac=True)
    paso = next(x['mw'] for x in e if x['disparado'])
    esperado = AGC.EDAC_FRACCION * s.P_carga
    assert abs(paso - esperado) < 0.5, (
        f"el escalon es {paso:.2f} MW y el 5 % de la demanda son "
        f"{esperado:.2f}: se esta usando otra base")

    for dP in (500., 1500., 3000., 4500.):
        _, _, e, tr = AGC.simular_frecuencia(s, deficit_mw=dP, con_edac=True)
        n_esc = sum(1 for x in e if x['disparado'])
        frac = 100 * tr['deslastre_total'] / s.P_carga
        assert n_esc <= 8, f"{n_esc} escalones disparados, el esquema tiene 8"
        assert frac <= 40.05, (
            f"perdiendo {dP:.0f} MW el EDAC bota el {frac:.2f} % de la "
            f"demanda; el esquema cubre el 40,00 %")


def test_la_frecuencia_no_hace_cosas_imposibles():
    """Dos regimenes, y el nadir solo es un dato en uno de ellos.

    POR QUE ESTA PRUEBA ESTA ESCRITA ASI. La version anterior exigia
    `nadir >= F_COLAPSO - 0,11`, y eso NO PUEDE FALLAR: el integrador corta la
    simulacion en cuanto `f < F_COLAPSO`, de modo que el nadir informado nunca
    baja mas de un paso por debajo de 57,5. Medido:

        perdida de  6.000 MW -> nadir informado 57,4919 (corta en 0,96 s)
        perdida de  9.000 MW -> nadir informado 57,4652 (corta en 0,55 s)
        perdida de 20.000 MW -> nadir informado 57,4573 (corta en 0,23 s)

    Perder 20.000 MW es perder mas que el despacho entero del sistema, y el
    invariante lo daba por bueno. Ademas los tres deficits que probaba
    (300/1000/3000) no colapsan, asi que la rama nunca se ejercitaba.

    Es el mismo artefacto de truncamiento que ya enseño los dientes en el
    modelo español: un nadir informado que no es el minimo real sino el punto
    donde el simulador se rindio. Convertirlo en invariante aceptado es la
    peor version del problema.
    """
    from stn import agc_deslastre as AGC
    _, s = base()

    vistos = {'sano': 0, 'colapso': 0}
    for dP in (300., 1000., 3000., 5000., 9000.):
        t, f, _, tr = AGC.simular_frecuencia(s, deficit_mw=dP, con_edac=True)

        assert f.max() <= 63.0, (
            f"perdiendo {dP:.0f} MW la frecuencia sube a {f.max():.2f} Hz")
        assert tr['nadir_corte'] <= 60.0 + 1e-9, (
            f"perdiendo {dP:.0f} MW el minimo sale por encima de 60 Hz")

        if tr['colapso']:
            vistos['colapso'] += 1
            # El nadir NO es un dato: es donde el integrador corto. Tiene que
            # salir NaN, para que nadie lo promedie ni lo cite como margen.
            assert np.isnan(tr['nadir']), (
                f"perdiendo {dP:.0f} MW hay colapso y el nadir sale "
                f"{tr['nadir']:.4f} en vez de NaN: ese numero se citara")
            assert not tr['nadir_valido']
            assert tr['deslastre_truncado'], (
                "el deslastre de un colapso esta truncado y no se declara")
            # El punto de corte si es comprobable, y sigue donde dice.
            assert tr['nadir_corte'] < AGC.F_COLAPSO, (
                f"se declara colapso pero el corte {tr['nadir_corte']:.4f} no "
                f"baja de {AGC.F_COLAPSO}")
            assert tr['nadir_corte'] >= AGC.F_COLAPSO - 2 * abs(tr['rocof']) * AGC.DT - 1e-6, (
                f"el corte {tr['nadir_corte']:.4f} esta mas de un paso por "
                f"debajo del umbral: la traza no se detuvo donde dice")
            assert t[-1] < AGC.T_FIN - AGC.DT, (
                "se declara colapso y sin embargo la traza llega al final: "
                "el corte no se aplico")
        else:
            vistos['sano'] += 1
            assert tr['nadir'] >= AGC.F_COLAPSO, (
                f"nadir {tr['nadir']:.4f} por debajo de {AGC.F_COLAPSO} y no "
                f"se declara colapso")
            assert t[-1] >= AGC.T_FIN - 2 * AGC.DT, (
                "sin colapso la traza tiene que llegar hasta el final")

            # COTA FISICA, independiente del integrador. Si nada respondiera,
            # la frecuencia caeria en linea recta con la pendiente inicial.
            # La primaria, el amortiguamiento y el EDAC solo FRENAN, nunca
            # aceleran, asi que el nadir real tiene que quedar POR ENCIMA de
            # esa recta evaluada en el instante del nadir. Margen medido:
            # +0,26 Hz a 300 MW y +1,92 Hz a 5.000 MW.
            caida_libre = 60.0 - abs(tr['rocof']) * tr['t_nadir']
            assert tr['nadir'] >= caida_libre - 1e-6, (
                f"perdiendo {dP:.0f} MW el nadir es {tr['nadir']:.3f} Hz y la "
                f"caida libre daria {caida_libre:.3f}: la frecuencia cae mas "
                f"rapido de lo que permite la ecuacion de oscilacion")

    # y la prueba tiene que haber pasado por los DOS regimenes
    assert vistos['sano'] >= 2 and vistos['colapso'] >= 1, (
        f"solo se ejercito {vistos}; hacen falta casos de las dos clases")


def test_la_ecuacion_de_oscilacion_cierra_en_el_estado_final():
    """gen + primaria = (carga + perdidas) * (1 + D*df/f0), ya asentado.

    LAS PERDIDAS CUENTAN. Hasta el 3-10-2026 este invariante las omitia, y al
    omitirlas documentaba un fallo en vez de detectarlo: el integrador
    comparaba generacion despachada contra carga a secas y arrancaba con un
    excedente espurio del tamaño de las perdidas (+120,5 MW). La prueba pasaba
    porque usaba exactamente la misma formula equivocada que el codigo.

    Va con `con_agc=False` A PROPOSITO: lo que comprueba es el equilibrio de
    ESTATISMO, que es una solucion analitica cerrada. Con la secundaria
    conectada el punto final es siempre 60 Hz por construccion del integrador,
    la ecuacion se cumple trivialmente y la prueba dejaria de poder fallar.
    Que el AGC llegue a 60 Hz lo comprueba el invariante siguiente.
    """
    from stn import agc_deslastre as AGC
    _, s = base()
    perd = float(getattr(s, 'P_perdidas', 0.0))
    assert perd > 0, "el sistema no trae perdidas; el caso no esta resuelto"
    for dP in (200., 500., 800.):
        _, f, _, tr = AGC.simular_frecuencia(s, deficit_mw=dP, con_edac=False,
                                             con_agc=False)
        dfpu = (tr['f_final'] - 60.) / 60.
        gen = s.P_gen - dP + tr['aporte_primaria']
        car = (s.P_carga + perd) * (1 + AGC.AMORTIGUAMIENTO * dfpu)
        assert abs(gen - car) < 1.0, (
            f"perdiendo {dP:.0f} MW queda un residuo de {gen-car:.2f} MW")
        riz = float(f[-500:].max() - f[-500:].min())
        assert riz < 0.02, (
            f"la frecuencia no se asienta: {riz:.4f} Hz de rizado al final")


def test_el_agc_devuelve_la_frecuencia_a_60_hz():
    """Con secundaria, el error de estado estacionario se cierra.

    Es la propiedad que distingue primaria de secundaria, y hasta el 1-11-2026
    el modelo no la tenia: la integracion llevaba solo primaria, asi que la
    frecuencia se quedaba en el error de estatismo ---60,14 Hz en la punta tras
    un evento de 3.000 MW--- y no volvia nunca a 60. En la realidad el AGC lo
    cierra.

    Se comprueba en los dos sentidos: perdiendo generacion la frecuencia sube
    hasta 60 y, cuando el EDAC bota de mas, baja hasta 60. Un integrador con el
    signo cambiado pasaria la primera y fallaria la segunda.
    """
    from stn import agc_deslastre as AGC
    _, s = base()
    # Horizonte largo a proposito: la secundaria arranca a los 30 s y tiene
    # una constante de 30 s, asi que en la ventana normal de 60 s aun va por
    # la mitad del camino. Aqui se comprueba el destino, no la velocidad.
    casos = [
        ('perdida de generacion', dict(deficit_mw=800., con_edac=False)),
        ('EDAC que bota de mas', dict(deficit_mw=3000., con_edac=True)),
    ]
    for nombre, kw in casos:
        _, f, _, tr = AGC.simular_frecuencia(s, t_fin=600., con_agc=True, **kw)
        assert abs(tr['f_final'] - 60.0) < 0.01, (
            f"{nombre}: la frecuencia se queda en {tr['f_final']:.4f} Hz; "
            f"el AGC no cierra el error de estado estacionario")
        # y el aporte de la secundaria tiene que tener el signo del desbalance
        sin_agc = AGC.simular_frecuencia(con_agc=False, sis=s, **kw)[3]
        if sin_agc['f_final'] < 60.0:
            assert tr['aporte_agc'] > 0, (
                f"{nombre}: la frecuencia se quedaba baja y el AGC aporta "
                f"{tr['aporte_agc']:+.1f} MW")
        else:
            assert tr['aporte_agc'] < 0, (
                f"{nombre}: la frecuencia se quedaba alta y el AGC aporta "
                f"{tr['aporte_agc']:+.1f} MW")


def test_el_sistema_arranca_en_equilibrio():
    """Sin evento, la frecuencia no se mueve de 60,000 Hz.

    Es el invariante mas basico de todos y faltaba. Su ausencia dejo pasar
    durante meses un desbalance inicial espurio igual a las perdidas de la
    red: sin ningun evento la frecuencia subia sola a 60,056 Hz, toda
    perturbacion se subestimaba en 120,5 MW y una perdida de 100 MW daba
    RoCoF POSITIVO. El sesgo era siempre optimista y se diluia al crecer el
    evento ---4 % de error a 3.000 MW, 120 % a 100 MW--- que es justo lo que
    lo hacia dificil de ver.
    """
    from stn import agc_deslastre as AGC
    from stn import contingencias
    from stn import escenario_diurno as ED

    for nombre, red in (('punta', contingencias.caso_base(verbose=False)[0]),
                        ('diurno', ED.construir(hora=11, verbose=False)[0])):
        s = AGC.Sistema(red)
        desb = s.P_gen - s.P_carga - float(getattr(s, 'P_perdidas', 0.0))
        assert abs(desb) < 0.5, (
            f"{nombre}: desbalance inicial de {desb:+.2f} MW; el sistema no "
            f"arranca en equilibrio")

        _, f, _, _ = AGC.simular_frecuencia(s, deficit_mw=0.0, con_edac=False)
        deriva = float(abs(f - 60.0).max())
        assert deriva < 0.005, (
            f"{nombre}: sin evento la frecuencia se va {deriva:.4f} Hz")


def test_el_rocof_obedece_la_ecuacion_de_oscilacion():
    """RoCoF(t=0) = -dP*f0 / (2 H S), que es la definicion.

    Comprobado a varias magnitudes: el fallo que esto detecta ---un desbalance
    inicial constante--- se nota mucho en eventos pequeños y poco en grandes,
    asi que una sola magnitud no basta. Con el defecto presente, 100 MW daban
    un error del 120 % y 2.000 MW solo del 6 %.
    """
    from stn import agc_deslastre as AGC
    _, s = base()
    for dP in (100., 300., 1000., 2000.):
        _, _, _, tr = AGC.simular_frecuencia(s, deficit_mw=dP, con_edac=False)
        teorico = -dP * 60.0 / (2 * s.H * s.S_base)
        err = abs(tr['rocof'] / teorico - 1)
        assert err < 0.02, (
            f"perdiendo {dP:.0f} MW el RoCoF es {tr['rocof']:.4f} Hz/s y "
            f"deberia ser {teorico:.4f} ({100*err:.1f}% de error)")


# ---------------------------------------------------------------------------
# El algoritmo de deslastre, que es la contribucion
# ---------------------------------------------------------------------------

def test_el_deslastre_anunciado_es_el_que_la_red_realiza():
    """Lo que el plan dice botar y lo que la red bota tienen que coincidir.

    POR QUE ESTA PRUEBA ESTA ESCRITA ASI. La version anterior comprobaba
    `deslastre[b] <= carga[b] + caido[b]`, y eso NO PUEDE FALLAR: para las
    barras que el MILP abre, `deslastre_desde_asignacion` devuelve
    literalmente `alivio_real`, que es `carga + caido`. Medido, la diferencia
    entre los dos lados era 4,6e-4 MW contra una tolerancia de 1e-3: el test
    comparaba un numero consigo mismo y estaba a un redondeo de fallar por
    ruido numerico.

    El observable independiente es la RED POST-EVENTO. `aplicar_evento`
    recorta con `max(0, 1 - mw/total)`, de modo que un plan que pida mas de lo
    que hay no deja carga negativa: deja la barra a cero y pierde el exceso en
    silencio. Y la dinamica de frecuencia ya se habia creido el alivio
    completo. Ese desacuerdo ---alivio anunciado frente a alivio entregado---
    es justo lo que hay que vigilar, y no se ve desde las Series del modulo.
    """
    from stn import deslastre_btm as DB
    for sem in (0, 1, 2):
        r = DB.ejecutar(DB.EscenarioBTM(
            fuente='sintetico', penetracion_pct=20., caida_pct=90.,
            perdida_gen_mw=600., semilla=sem), verbose=False, usar_milp=True)
        d = r['deslastre']
        assert (d >= -1e-9).all(), "el MILP propone deslastres negativos"
        assert len(d), "el plan esta vacio; la prueba no verifica nada"

        pre = r['carga_pre'].add(r['carga_extra'], fill_value=0.0)
        post = r['red_post'].loads.groupby('bus').p_set.sum()
        realizado = pre.sub(post, fill_value=0.0)

        # ninguna barra puede acabar con carga negativa
        assert (post >= -1e-6).all(), (
            f"semilla {sem}: barras con carga negativa tras el deslastre: "
            f"{list(post[post < -1e-6].index)}")

        # y lo entregado tiene que ser lo prometido, barra a barra
        for b, v in d.items():
            hecho = float(realizado.get(b, 0.0))
            assert abs(hecho - float(v)) < 0.5, (
                f"semilla {sem}, barra {b}: el plan anuncia {float(v):.2f} MW "
                f"y la red entrega {hecho:.2f}. Si entrega MENOS, el plan "
                f"pidio mas de lo que hay y el recorte se lo trago")

        # en total: la frecuencia conto con este alivio y la red debe darlo
        total_d = float(d.sum())
        total_r = float(realizado.reindex(d.index).fillna(0.0).sum())
        assert abs(total_r - total_d) < 1.0, (
            f"semilla {sem}: alivio anunciado {total_d:.1f} MW, entregado "
            f"{total_r:.1f}")


def test_el_btm_sano_destruido_esta_ponderado_por_la_fraccion_botada():
    """El BTM sano destruido se recalcula desde la RED, no desde el modulo.

    Es la metrica central del trabajo: la que separa una ventaja real de
    4,6x de la publicada por error de 121x. Merece una comprobacion que no
    use las Series del modulo.

    POR QUE LA VERSION ANTERIOR NO VALIA. Recalculaba `frac = deslastre /
    alivio_real`, que es exactamente el cociente que usa `btm_sano_perdido`.
    Medido: con el MILP `frac` sale 1,000000 y el test cae en la rama trivial
    "barra entera"; con el reparto proporcional sale 0,200000 uniforme y
    `btm_sano / sin_ponderar` da 0,200000 EXACTO, porque `_proporcional`
    reparte en proporcion a `alivio` y el cociente es constante por algebra.
    Las dos ramas eran ciertas por construccion. Si el denominador volviera a
    ser la carga sola ---el fallo historico que inflaba la cifra 6,7 veces---
    el test calcularia la misma `frac` equivocada y pasaria igual.

    LA COMPROBACION INDEPENDIENTE mide la fraccion realmente botada de cada
    barra sobre la red post-evento ---cargas antes y despues, leidas del
    objeto PyPSA--- y reconstruye la metrica con ella. Si el modulo pondera
    con otro denominador, las dos cifras se separan.

    CUAL DE LAS DOS RAMAS HACE EL TRABAJO, porque no son simetricas y conviene
    no enganarse. Verificado reintroduciendo el fallo historico:

        MILP           modulo 2,44    red 2,44     dif 0,00   NO lo detecta
        proporcional   modulo 254,10  red 50,82    dif 203,28 SI lo detecta

    El MILP abre barras ENTERAS ---la asignacion es binaria--- asi que su
    fraccion vale 1 y ponderar no cambia nada: ahi las dos cifras coinciden
    por matematicas, no por casualidad. La deteccion viene de la rama
    proporcional, que es justo donde el fallo importaba: era la cifra del
    reparto proporcional la que salia inflada y convertia la ventaja en 121x.

    Aun asi la rama del MILP comprueba algo real: que cuando el plan dice
    "abre la barra b", la red la abre DEL TODO. Si el recorte de
    `aplicar_evento` se activara, la fraccion bajaria de 1 y las dos cifras
    se separarian.
    """
    from stn import deslastre_btm as DB
    kw = dict(fuente='sintetico', penetracion_pct=20., caida_pct=90.,
              perdida_gen_mw=600.)
    vistos = []
    for usar_milp in (True, False):
        r = DB.ejecutar(DB.EscenarioBTM(**kw), verbose=False,
                        usar_milp=usar_milp)
        d, resto = r['deslastre'], r['resto']
        assert len(d), "plan vacio"

        pre = r['carga_pre'].add(r['carga_extra'], fill_value=0.0)
        post = r['red_post'].loads.groupby('bus').p_set.sum()

        # fraccion de cada barra que la RED perdio de verdad
        total = 0.0
        for b in d.index:
            p0 = float(pre.get(b, 0.0))
            if p0 <= 1e-9:
                continue
            frac = (p0 - float(post.get(b, 0.0))) / p0
            assert -1e-6 <= frac <= 1.0 + 1e-6, (
                f"barra {b}: fraccion botada {frac:.4f}, fuera de [0, 1]")
            total += float(resto.get(b, 0.0)) * frac

        assert abs(total - r['btm_sano_mw']) < 0.5, (
            f"{'MILP' if usar_milp else 'proporcional'}: el modulo informa "
            f"{r['btm_sano_mw']:.2f} MW de BTM sano destruido y la red dice "
            f"{total:.2f}. Se esta ponderando con otro denominador")

        sin_ponderar = float(resto.reindex(d.index).fillna(0).sum())
        assert r['btm_sano_mw'] <= sin_ponderar + 1e-6, (
            "el BTM sano ponderado no puede superar al sin ponderar")
        vistos.append((r['btm_sano_mw'], sin_ponderar))

    # Y la prueba de que ponderar SIRVE: el reparto proporcional abre muchas
    # barras un poco, asi que su cifra ponderada tiene que quedar muy por
    # debajo de la que saldria imputando el BTM entero de cada barra tocada.
    # Sin ponderar, ese caso es el que daba 6,7 veces de mas.
    pond_prop, bruto_prop = vistos[1]
    assert pond_prop < 0.5 * bruto_prop, (
        f"el reparto proporcional pondera {pond_prop:.1f} frente a "
        f"{bruto_prop:.1f} sin ponderar: la ponderacion no esta actuando")


def test_con_escenario_el_estado_ac_recibe_los_cambios_del_escenario():
    """La red post-evento tiene que ser la que simulo la frecuencia.

    `EV.construir()` devuelve que generadores salen, cuanto deficit hay y que
    carga aparece. Durante un tiempo se calculaban y se descartaban: con
    `iberia` la frecuencia veia 4.412,7 MW de perturbacion y el flujo AC se
    resolvia sobre una red a la que solo se le habian quitado 528,9 MW de
    carga, con los cinco generadores del escenario todavia dentro. El slack
    absorbia la diferencia ---salia a -2.548,7 MW, un generador consumiendo
    2,5 GW--- y el informe de sobrecargas describia una red inexistente.

    La firma de ese fallo es un slack fuera de toda proporcion, asi que es lo
    que se comprueba.
    """
    from stn import agc_deslastre as AGC
    from stn import deslastre_btm as DB
    from stn import eventos as EV
    from stn.zonas import area_por_bus
    n, s = base()
    mapa = area_por_bus(n)
    esc = DB.EscenarioBTM(fuente='agpe', caida_pct=80.)
    btm = DB.repartir_btm(n, esc, mapa)
    caido, _ = DB.aplicar_caida_btm(btm, esc,
                                    DB.fraccion_inversor(esc, mapa, btm.index))

    for nombre in ('iberia', 'cascada'):
        spec = EV.ESCENARIOS[nombre]()
        _pert, cambios = EV.construir(n, spec, mapa, s, btm, caido)
        r = DB.ejecutar(esc, verbose=False, usar_milp=True, spec=spec)
        assert r['estado']['convergio'], f"{nombre}: el flujo no converge"

        # Se reconstruye la red post-evento con los MISMOS argumentos que debe
        # usar ejecutar(), y se comprueba que el slack no esta tapando nada.
        red, ok = AGC.aplicar_evento(
            n, cambios['generadores_fuera'], r['deslastre'], s,
            deficit_mw=float(cambios['deficit_mw']),
            carga_extra=cambios['carga_extra'], verbose=False)
        assert ok, f"{nombre}: la red post-evento no converge"
        sl = red.generators[red.generators.control == 'Slack'].index[0]
        p_sl = float(red.generators_t.p.iloc[0][sl])

        # Con el cableado roto el slack salia a -2.548,7 MW: un generador
        # absorbiendo 2,5 GW. El criterio es que no supere en magnitud a la
        # mayor maquina del sistema, que es el orden de lo fisicamente posible.
        mayor = float(s.gen.p_nom.max())
        assert abs(p_sl) <= mayor, (
            f"{nombre}: el slack sale a {p_sl:.1f} MW, por encima de la mayor "
            f"maquina del sistema ({mayor:.1f} MVA). Senal de que el estado AC "
            f"se monto sin los cambios del escenario.")
        assert p_sl > -0.25 * mayor, (
            f"{nombre}: el slack ABSORBE {-p_sl:.1f} MW. Un generador no "
            f"consume; esta tapando un desbalance que no se aplico.")


def test_el_estimador_de_btm_es_insesgado():
    """`E[estimado] = real` y `CV = sigma`, exactamente.

    El estimador truncaba una normal por los dos lados: a cero por abajo y a
    1,25 veces el valor real por arriba, un tope sin justificacion fisica
    ---`btm_real` es la produccion del instante, no la capacidad del
    inversor---. La asimetria metia un sesgo negativo creciente: -3,8 % al
    20 % de error nominal y -25,1 % al 100 %, y ademas comprimia la
    dispersion. Como `--error-estimacion` es la perilla del estudio de
    sensibilidad, subirla añadia ruido Y sesgo a la vez, y los dos efectos
    quedaban confundidos.
    """
    import numpy as np
    from stn import deslastre_btm as DB

    # Comprobacion analitica del nucleo, sin la red: 200.000 muestras bastan
    # para distinguir un sesgo del 1 % del ruido de muestreo.
    for sigma in (0.245, 0.60, 1.00):
        s = np.sqrt(np.log1p(sigma ** 2))
        z = np.random.default_rng(0).normal(0.0, 1.0, 200_000)
        r = np.exp(s * z - 0.5 * s ** 2)
        assert abs(r.mean() - 1.0) < 0.02, (
            f"sigma={sigma}: E[est/real] = {r.mean():.4f}, deberia ser 1")
        assert abs(r.std() - sigma) < 0.02 * max(sigma, 0.1), (
            f"sigma={sigma}: CV = {r.std():.4f}, deberia ser {sigma}")

    # Y sobre el modelo: ningun estimado puede ser negativo, y con error cero
    # el estimador tiene que devolver el valor real intacto.
    from stn.zonas import area_por_bus
    n, _ = base()
    mapa = area_por_bus(n)
    for err in (0.0, 24.5, 100.0):
        e = DB.EscenarioBTM(fuente='sintetico', penetracion_pct=20.,
                            error_estimacion_pct=err, semilla=0)
        b = DB.repartir_btm(n, e, mapa)
        est = DB.estimar_btm(b, e, mapa_areas=mapa)
        assert (est >= -1e-12).all(), f"error {err}%: hay estimados negativos"
        if err == 0.0:
            assert (est - b).abs().max() < 1e-9, (
                "con error cero el estimador deberia devolver el valor real")


def test_la_caida_de_btm_no_excede_el_btm_con_inversor():
    from stn import deslastre_btm as DB
    from stn.zonas import area_por_bus
    n, _ = base()
    mapa = area_por_bus(n)
    for c in (50., 80., 100.):
        e = DB.EscenarioBTM(fuente='agpe', caida_pct=c)
        b = DB.repartir_btm(n, e, mapa)
        fi = DB.fraccion_inversor(e, mapa, b.index)
        ca, _ = DB.aplicar_caida_btm(b, e, fi)
        inv = float((b * fi).sum())
        assert (ca >= -1e-9).all(), "caidas de BTM negativas"
        assert float(ca.sum()) <= inv + 1e-6, (
            f"con caida del {c:.0f} % se cae mas BTM del que hay por inversor")


def test_el_autoconsumo_no_supera_la_carga_de_su_barra():
    """La cota vale para la AGPE, y NO para las plantas menores.

    LA VERSION ANTERIOR LAS METIA EN EL MISMO SACO con el argumento de que
    "generacion embebida mayor que la demanda local no tiene sentido". Para la
    AGPE es cierto por definicion: nadie puede AUTOconsumir mas de lo que
    consume. Para las plantas menores es falso: son centrales de menos de
    20 MW que ALIMENTAN LA RED, no autoconsumo, y una zona con poca carga y
    varias centrales pequenas exporta. El propio modulo lo dice en
    `cargar_registrada`: "no son BTM en sentido estricto".

    La prueba pasaba solo porque el reparto por AREA difuminaba el pool sobre
    medio pais. Al ubicarlo por SUBAREA ---que es el dato que el registro trae
    de verdad--- salio a la luz BOYACA-CASANARE: 120,1 MW registrados sobre
    101,0 MW de carga STN, de los cuales 113,7 son plantas menores y solo 6,4
    AGPE. Paipa es un nudo con poca demanda y varias centrales alrededor.

    Medido con las dos granularidades: la AGPE no excede en NINGUNA barra; las
    menores, en una y solo con subarea.
    """
    from stn import deslastre_btm as DB
    from stn.zonas import area_por_bus
    n, _ = base()
    mapa = area_por_bus(n)
    carga = n.loads.groupby('bus').p_set.sum()

    for por in ('area', 'subarea'):
        # AGPE: cota dura, es autoconsumo
        b = DB.repartir_btm(n, DB.EscenarioBTM(fuente='agpe'), mapa, por=por)
        exc = [(k, round(float(v), 1), round(float(carga.get(k, 0.)), 1))
               for k, v in b.items() if v > float(carga.get(k, 0.)) + 1e-6]
        assert not exc, (
            f"reparto por {por}: hay AUTOCONSUMO por encima de la carga de su "
            f"barra, que es imposible por definicion: {exc[:3]}")

        # sintetico: es un porcentaje de la demanda, no puede excederla
        b = DB.repartir_btm(n, DB.EscenarioBTM(fuente='sintetico',
                                               penetracion_pct=20.),
                            mapa, por=por)
        exc = [k for k, v in b.items() if v > float(carga.get(k, 0.)) + 1e-6]
        assert not exc, f"reparto por {por}: penetracion sintetica > carga"

    # Las menores SI pueden exceder: se comprueba que el modelo lo permita
    # y que siga siendo un caso contado, no la norma.
    b = DB.repartir_btm(n, DB.EscenarioBTM(fuente='menores'), mapa,
                        por='subarea')
    exporta = [k for k, v in b.items() if v > float(carga.get(k, 0.)) + 1e-6]
    assert len(exporta) <= 3, (
        f"{len(exporta)} barras con mas plantas menores que carga local. Una "
        f"o dos es real ---zonas generadoras con poca demanda STN--- pero "
        f"tantas apunta a un reparto mal hecho: {exporta[:5]}")


# ---------------------------------------------------------------------------
# Cascada
# ---------------------------------------------------------------------------

def test_la_isla_de_la_cascada_usa_los_mismos_parametros_que_el_sistema():
    """Sobre una red de UNA isla, `_SistemaIsla` y `Sistema` deben coincidir.

    `cascada._SistemaIsla` replicaba las formulas de `agc_deslastre.Sistema` y
    la copia se quedo atras: no recibio el compromiso de unidades, ni el H de
    PARATEC, ni el estatismo ponderado, ni la exclusion de los convertidores,
    ni la reserva a bajar. Sobre el caso base ---que es una sola isla, o sea
    que no hacia falta partir la red para que divergieran--- daban 13.947,7
    frente a 17.698,0 MVA, y para una perdida de 3.000 MW predecian 5 y 4
    escalones del EDAC. Dos respuestas al mismo evento segun a quien se
    preguntara.
    """
    from stn import agc_deslastre as AGC
    from stn import cascada as K
    n, s = base()
    isla = K._SistemaIsla(n, set(n.buses.index), s.gen)

    for etq, a, b in (('S_base', s.S_base, isla.S_base),
                      ('H', s.H, isla.H),
                      ('R_eq', s.R_eq, isla.R_eq),
                      ('reserva a subir', s.reserva, isla.reserva),
                      ('reserva a bajar', s.reserva_baja, isla.reserva_baja),
                      ('P_gen', s.P_gen, isla.P_gen),
                      ('P_carga', s.P_carga, isla.P_carga)):
        assert abs(a - b) < 1e-6, (
            f"{etq}: Sistema da {a:.4f} y _SistemaIsla {b:.4f} sobre la misma "
            f"red de una sola isla")

    # y la consecuencia, que es lo que de verdad importa
    for dP in (1500., 3000.):
        _, _, e1, t1 = AGC.simular_frecuencia(s, deficit_mw=dP, con_edac=True)
        _, _, e2, t2 = AGC.simular_frecuencia(isla, deficit_mw=dP, con_edac=True)
        n1 = sum(1 for x in e1 if x['disparado'])
        n2 = sum(1 for x in e2 if x['disparado'])
        assert n1 == n2 and abs(t1['nadir'] - t2['nadir']) < 1e-9, (
            f"perdiendo {dP:.0f} MW: agc_deslastre predice {n1} escalones "
            f"(nadir {t1['nadir']:.4f}) y cascada {n2} ({t2['nadir']:.4f})")


def test_la_cascada_conserva_la_carga():
    """servida + deslastrada + aislada = demanda a servir, ronda a ronda.

    `carga_servida_mw` es ACUMULADA y `edac_mw` es POR RONDA: compararlas mal
    hace creer que faltan miles de MW.

    OJO CON LO QUE ESTA PRUEBA ES Y NO ES. No ejecuta la cascada: lee los CSV
    que dejo la ultima corrida. Eso la hace barata pero fragil, y el 3-10-2026
    enseño los dientes: tras correr `cascada.py --diurno`, el CSV paso a ser
    de un punto de operacion distinto del caso base y la prueba comparo
    12.125,1 MW de entrada contra 8.889,3 de salida. No era un fallo del
    modelo sino de la prueba, que daba por supuesto que el fichero venia del
    caso de punta.

    Dos arreglos: la solar que se desconecta por tension ENTRA como demanda y
    faltaba en el balance; y ahora se comprueba que el CSV corresponda de
    verdad al caso base antes de validarlo.
    """
    import pandas as pd
    import os
    n, _ = base()
    D0 = float(n.loads.p_set.sum())
    validados = 0
    for esc in ('btm', 'cascada'):
        ruta = os.path.join('resultados', f'cascada_{esc}.csv')
        if not os.path.exists(ruta):
            continue        # no se ha corrido ese escenario; no es un fallo
        d = pd.read_csv(ruta)
        if not len(d):
            continue

        # El CSV tiene que venir del MISMO punto de operacion. Si no, sus
        # cifras no son comparables con `D0` y validarlas es comparar peras
        # con manzanas. Se reconstruye la carga inicial implicita.
        r0 = d.iloc[0]
        anadida0 = float(r0.carga_anadida_mw) + float(r0.get('solar_mw', 0.0))
        inicial = (float(r0.carga_servida_mw) + float(r0.edac_mw)
                   + float(r0.aislada_mw) - anadida0)
        if abs(inicial - D0) > 0.05 * D0:
            continue        # de otro punto de operacion; no se puede validar

        prev = D0
        for _, r in d.iterrows():
            # la solar que cae por tension tambien aparece como demanda
            anadida = float(r.carga_anadida_mw) + float(r.get('solar_mw', 0.0))
            esperado = prev + anadida
            real = r.carga_servida_mw + r.edac_mw + r.aislada_mw
            assert abs(real - esperado) < 0.5, (
                f"{esc}, ronda {int(r.ronda)}: entran {esperado:.1f} MW y "
                f"salen {real:.1f}. Si el CSV trae solar desconectada por "
                f"tension, desvio conocido, por el estado de maniobra de los reactores: "
                f"~173 MW sin resolver")
            prev = r.carga_servida_mw
        validados += 1

    if not validados:
        print("       (sin CSV del caso base; nada que validar)", end='')


# ---------------------------------------------------------------------------
# El escenario diurno
# ---------------------------------------------------------------------------

def test_el_caso_diurno_cierra_su_balance():
    """Generacion = carga neta + perdidas, sin que el slack tape el hueco.

    Este invariante nacio de un fallo real: `construir()` estimaba las
    perdidas con `base_diag.get('perdidas_MW', 0.0)` y esa clave NO EXISTE en
    el diccionario de estado, asi que el `.get` devolvia cero en silencio. El
    balance seguia cerrando ---siempre cierra, el flujo lo impone--- pero a
    costa de cargar las perdidas enteras sobre una sola maquina: el slack
    tomaba +87,5 MW en vez de los +2,9 MW del caso base.
    """
    from stn import escenario_diurno as ED
    red, _ = ED.construir(hora=11, verbose=False)

    gen = float(red.generators_t.p.iloc[0].sum())
    carga = float(red.loads.p_set.sum())
    cons = float(red.generators.p_set.sum())

    assert abs(gen - carga - (gen - carga)) < 1e-6
    perdidas = gen - carga
    assert 0 < perdidas < 0.03 * carga, (
        f"perdidas de {perdidas:.1f} MW sobre {carga:.1f} de carga")

    slack = red.generators.index[red.generators.control == 'Slack']
    assert len(slack) == 1, f"{len(slack)} slacks en el caso diurno"
    aporte = float(red.generators_t.p.iloc[0][slack[0]]
                   - red.generators.at[slack[0], 'p_set'])
    assert abs(aporte) < 15.0, (
        f"el slack aporta {aporte:+.1f} MW sobre su consigna: las perdidas "
        f"no se estan repartiendo, se las come el slack")
    assert abs(cons - carga - perdidas) < 15.0


def test_el_caso_diurno_tiene_menos_inercia_que_la_punta():
    """La razon de ser del escenario: menos maquinas acopladas.

    No es un detalle de presentacion. Si el caso diurno saliera con la misma
    energia cinetica que el de punta, la construccion estaria mal: la solar
    desplaza generacion sincrona, y eso TIENE que verse en H*S.
    """
    from stn import contingencias
    from stn import escenario_diurno as ED
    from stn import agc_deslastre as AGC

    punta, _ = contingencias.caso_base(verbose=False)
    diurno, info = ED.construir(hora=11, verbose=False)
    sp, sd = AGC.Sistema(punta), AGC.Sistema(diurno)

    assert sd.S_base < sp.S_base, (
        f"el caso diurno sincroniza {sd.S_base:.1f} MVA y el de punta "
        f"{sp.S_base:.1f}: deberia ser al reves")
    assert sd.H * sd.S_base < sp.H * sp.S_base

    # y la solar tiene que estar produciendo, que es la otra mitad del punto
    assert info['solar_produccion_MW'] > 1000.0, (
        f"solo {info['solar_produccion_MW']:.1f} MW de solar en un escenario "
        f"cuyo proposito es tener sol")


def test_la_solar_diurna_se_netea_y_no_se_duplica():
    """La solar baja la carga en exactamente lo que produce.

    La solar de gran escala no es un generador del modelo: entra por debajo
    de 200 kV y desde el STN se ve como MENOS CARGA. El riesgo de la
    construccion es contarla dos veces ---netearla de la carga Y ademas
    dejarla en la generacion--- lo que regalaria MW que no existen.
    """
    from stn import escenario_diurno as ED

    red, info = ED.construir(hora=11, verbose=False)
    bruta = info['demanda_diurna_MW']
    neta = info['carga_neta_MW']
    solar = info['solar_produccion_MW']

    assert abs((bruta - solar) - neta) < 1.0, (
        f"bruta {bruta:.1f} - solar {solar:.1f} = {bruta-solar:.1f}, pero la "
        f"neta es {neta:.1f}")

    # y el reparto por barra tiene que sumar el mismo total
    assert abs(float(info['solar_bus'].sum()) - solar) < 1.0


def test_el_perfil_diurno_no_tiene_factores_inventados():
    """Toda hora de PERFIL trae sus factores medidos, o no esta.

    Protege la regla de no inventar datos: si alguien añade una hora al
    perfil tiene que traer los cuatro campos, y los factores por tecnologia
    tienen que ser fracciones que reduzcan la generacion, no amplificarla.
    """
    from stn import escenario_diurno as ED

    assert ED.PERFIL, "PERFIL esta vacio"
    for hora, p in ED.PERFIL.items():
        for campo in ('f_demanda', 'solar_medida_mw', 'factores_gen',
                      'etiqueta'):
            assert campo in p, f"a la hora {hora} le falta '{campo}'"
        assert 0.5 < p['f_demanda'] < 1.0, (
            f"hora {hora}: f_demanda {p['f_demanda']} fuera de rango; el caso "
            f"diurno no puede tener mas demanda que la punta")
        for tec, f in p['factores_gen'].items():
            assert 0.0 < f <= 1.0, (
                f"hora {hora}, {tec}: factor {f} no es una reduccion")

    # y pedir una hora que no existe tiene que fallar, no inventar
    try:
        ED.factores(3)
    except KeyError:
        pass
    else:
        raise AssertionError("factores(3) deberia fallar: no hay perfil "
                             "nocturno y devolver algo seria inventarlo")


def test_el_btm_que_se_cae_es_el_que_producia():
    """La placa no se desconecta: se desconecta lo que estaba generando.

    El motor repartia la CAPACIDAD registrada del BTM y le aplicaba
    `caida_pct` directamente, es decir, suponia el pool produciendo al 100 %
    de su placa a cualquier hora. En la punta nocturna eso es falso por un
    factor infinito ---la fotovoltaica da cero--- y a las 10-11 h por un 23 %.

    Dos cosas tienen que cumplirse, y la segunda es la sutil:
      1. menos produccion => evento menor.
      2. el BTM que queda SANO tambien se reduce. Botar una barra cuya
         fotovoltaica esta apagada no destruye generacion embebida, y contarla
         inflaria la metrica central del trabajo.
    """
    import pandas as pd
    from stn import deslastre_btm as DB

    btm = pd.Series({'A': 100.0, 'B': 100.0})
    f_inv = pd.Series({'A': 1.0, 'B': 0.0})      # A toda solar, B toda sincrona

    base = DB.EscenarioBTM(fuente='sintetico', caida_pct=90.0, semilla=0)
    media = DB.EscenarioBTM(fuente='sintetico', caida_pct=90.0, semilla=0,
                            produccion_inversor_pu=0.5)

    b1, fi1 = DB.btm_produciendo(btm, base, f_inv)
    b2, fi2 = DB.btm_produciendo(btm, media, f_inv)

    # el pool efectivo baja solo por la parte con inversor
    assert abs(float(b1.sum()) - 200.0) < 1e-9
    assert abs(float(b2.sum()) - 150.0) < 1e-9, float(b2.sum())
    assert abs(b2['A'] - 50.0) < 1e-9 and abs(b2['B'] - 100.0) < 1e-9

    # y la mezcla se recalcula: A sigue siendo 100% inversor, B sigue 0%
    assert abs(fi2['A'] - 1.0) < 1e-9 and abs(fi2['B'] - 0.0) < 1e-9

    c1, r1 = DB.aplicar_caida_btm(b1, base, fi1)
    c2, r2 = DB.aplicar_caida_btm(b2, media, fi2)
    assert float(c2.sum()) < float(c1.sum()), "el evento no se redujo"
    assert float(r2.sum()) < float(r1.sum()), (
        "el BTM sano no se redujo: se esta contando como embebida sana una "
        "fotovoltaica que no estaba produciendo")

    # y el defecto tiene que seguir siendo 1,0, que es lo ya publicado
    assert DB.EscenarioBTM().produccion_inversor_pu == 1.0


# ---------------------------------------------------------------------------

def main():
    filtro = None
    if len(sys.argv) > 2 and sys.argv[1] == '-k':
        filtro = sys.argv[2].lower()

    pruebas = [(k, v) for k, v in sorted(globals().items())
               if k.startswith('test_') and callable(v)
               and (filtro is None or filtro in k.lower())]
    if not pruebas:
        print("ninguna prueba coincide con el filtro")
        return 1

    print(f"{len(pruebas)} pruebas\n")
    fallos = []
    for nombre, fn in pruebas:
        etq = nombre[5:].replace('_', ' ')
        try:
            fn()
            print(f"  OK    {etq}")
        except AssertionError as e:
            print(f"  FALLA {etq}\n          {e}")
            fallos.append((nombre, str(e)))
        except Exception as e:                               # noqa: BLE001
            print(f"  ERROR {etq}\n          {type(e).__name__}: {e}")
            fallos.append((nombre, f"{type(e).__name__}: {e}"))

    print()
    if fallos:
        print(f"{len(fallos)} de {len(pruebas)} fallan")
        return 1
    print(f"las {len(pruebas)} pasan")
    return 0


if __name__ == '__main__':
    sys.exit(main())
