# power_flow.py
"""Asignacion de cargas y generadores y ejecucion del flujo de carga.

Correcciones respecto de la version anterior:

  - run_power_flow comprobaba la convergencia con un try/except, pero
    Network.pf() NO lanza excepcion cuando no converge: devuelve un objeto
    con un DataFrame 'converged'. El modelo anterior imprimia "convergio
    exitosamente" mientras PyPSA avisaba de lo contrario, y todo el analisis
    posterior se hacia sobre resultados invalidos.

  - La version anterior llamaba a network.pf(method='DC'), que no existe:
    la firma de pf() no admite 'method'. El flujo lineal es network.lpf().

  - El balance por isla se hacia ajustando un generador existente hasta
    cubrir el deficit, lo que llego a fijar 83.809 MW en un solo generador
    (cuatro veces la capacidad instalada de Colombia). Ahora el slack se
    designa explicitamente y absorbe unicamente las perdidas, porque las
    inyecciones equivalentes ya vienen balanceadas del reporte.
"""

import networkx as nx
import numpy as np
import pandas as pd

from stn.config import PF_TOLERANCE


def asignar_inyecciones(network, inyecciones, verbose=True, desglose=None):
    """Crea Load y Generator en la red a partir de las inyecciones netas.

    P > 0 -> generador (inyecta)      P < 0 -> carga (consume)

    `desglose` es la tupla (gen_bruta, carga_bruta) de desagregacion.py, dos
    Series por bus con Gen - Carga == P. Si se pasa, cada barra recibe los dos
    elementos en vez de la inyeccion neta, y los totales dejan de estar
    neteados contra la generacion embebida bajo 200 kV.

    La inyeccion neta de cada barra se conserva exacta, pero el flujo AC no
    sale identico: la barra que recibe generador pasa de PQ a PV y sostiene su
    tension con reactivo, lo que mueve la activa 0,29 MW de media por linea
    (5,92 la peor). Las cifras estan medidas en validacion.resolver_modelo().
    """
    n_gen = n_load = 0
    p_gen = p_load = 0.0
    con_consigna = set()   # barras con consigna REAL del reporte, no el 1,0
    # Centrales desdobladas: la inyeccion del mapeo viene agregada en la barra
    # de la central, pero el modelo tiene una barra por unidad. Se reparte a
    # partes iguales, que es lo que hace la central real: cada maquina entrega
    # lo mismo por su propio circuito. Sin esto los 750 MW de Hidrosogamoso
    # caian en una barra que ya no existe y se perdian.
    from stn import complementos as _cmp
    reparto = {}
    for (sub, niv) in getattr(_cmp, 'CENTRALES_POR_UNIDAD', {}):
        us = [b for b in _cmp.unidades_de(sub, niv) if b in network.buses.index]
        if us:
            reparto[f"{sub}__{int(niv)}"] = us
    if reparto:
        filas = []
        for bus, fila in inyecciones.iterrows():
            if bus in reparto:
                us = reparto[bus]
                for u in us:
                    f = fila.copy()
                    f['P'] = float(fila['P']) / len(us)
                    f['Q'] = float(fila['Q']) / len(us)
                    filas.append((u, f))
            else:
                filas.append((bus, fila))
        inyecciones = pd.DataFrame([f for _, f in filas],
                                   index=[b for b, _ in filas])
        # El desglose bruto viene indexado por la barra agregada, asi que hay
        # que repartirlo IGUAL que la inyeccion. Sin esto, las barras
        # desdobladas no encontrarian su entrada y caerian en silencio al
        # comportamiento neto, mezclando los dos criterios en la misma red.
        if desglose is not None:
            g_s, c_s = desglose
            for serie in (g_s, c_s):
                for bus, us in reparto.items():
                    if bus in serie.index:
                        v = float(serie.pop(bus)) / len(us)
                        for u in us:
                            serie[u] = serie.get(u, 0.0) + v
            desglose = (g_s, c_s)

    for bus, fila in inyecciones.iterrows():
        if bus not in network.buses.index:
            continue
        P, Q = float(fila['P']), float(fila['Q'])
        v_set = fila.get('v_set', np.nan)
        v_set = 1.0 if (v_set is None or pd.isna(v_set)) else float(v_set)

        # La consigna va en la BARRA. El flujo AC de PyPSA lee la tension de
        # las barras PV y slack de n.buses.v_mag_pu_set; el v_set del generador
        # es una columna que el solver no mira. Pasandola solo al generador,
        # las 146 barras se quedaban en el 1,0 por defecto y el modelo entero
        # resolvia medio punto porcentual por debajo del caso real, que opera
        # cerca de 1,05: el error medio de tension era de 0,057 pu y solo 11
        # de 104 barras caian dentro de +-0,02 pu. Como la corriente sube
        # cuando baja la tension, la cargabilidad tambien salia inflada.
        network.buses.at[bus, 'v_mag_pu_set'] = v_set
        if not pd.isna(fila.get('v_set', np.nan)):
            con_consigna.add(bus)

        # --- desglose bruto opcional ------------------------------------
        # Con `desglose` la barra recibe a la vez un generador y una carga,
        # en vez de una sola inyeccion neta. Se cumple Gen - Carga == P, asi
        # que el flujo AC resuelve EXACTAMENTE igual: lo unico que cambia es
        # que los totales dejan de estar neteados. Ver desagregacion.py.
        if desglose is not None and bus in desglose[0].index:
            g_b = float(desglose[0].get(bus, 0.0))
            c_b = float(desglose[1].get(bus, 0.0))
            if g_b > 1e-9 or c_b > 1e-9:
                if g_b > 1e-9:
                    network.add("Generator", f"gen_{bus}", bus=bus,
                                p_set=g_b, control="PV", v_set=v_set)
                    n_gen += 1
                    p_gen += g_b
                if c_b > 1e-9:
                    network.add("Load", f"load_{bus}", bus=bus,
                                p_set=c_b, q_set=-Q)
                    n_load += 1
                    p_load += c_b
                elif abs(Q) > 1e-9:
                    network.add("Load", f"q_{bus}", bus=bus,
                                p_set=0.0, q_set=-Q)
                continue

        if P > 0:
            network.add("Generator", f"gen_{bus}", bus=bus,
                        p_set=P, control="PV", v_set=v_set)
            n_gen += 1
            p_gen += P
            if abs(Q) > 1e-9:
                # la Q del equivalente que el generador PV no fija
                network.add("Load", f"q_{bus}", bus=bus, p_set=0.0, q_set=-Q)
        elif P < 0:
            network.add("Load", f"load_{bus}", bus=bus, p_set=-P, q_set=-Q)
            n_load += 1
            p_load += -P
        elif abs(Q) > 1e-9:
            network.add("Load", f"q_{bus}", bus=bus, p_set=0.0, q_set=-Q)

    n_taps = _ajustar_taps(network, con_consigna, verbose=verbose)

    if verbose:
        print(f"Generadores: {n_gen} ({p_gen:.1f} MW)   "
              f"Cargas: {n_load} ({p_load:.1f} MW)   "
              f"Taps ajustados: {n_taps}")
    return n_gen, n_load


def _ajustar_taps(network, buses_con_consigna, verbose=True):
    """Fija la toma de cada autotransformador con la relacion del caso real.

    Un autotransformador 500/230 lleva regulacion bajo carga, y el caso de
    referencia la usa: pide 1,0770 pu en San Carlos 230 con 0,9995 en San
    Carlos 500, una relacion de 1,0775 que un transformador de toma fija no
    puede dar. La mediana de los 16 es 1,0535, asi que el desajuste es del
    orden del 5% en TODOS, no un caso aislado.

    Con tap_ratio = 1 el flujo solo puede sostener esas dos tensiones haciendo
    circular reactivo: los dos generadores de San Carlos, que son la MISMA
    central, se intercambiaban 864 Mvar a traves de su propio banco, y 12 de
    27 generadores acababan fuera de su limite fisico de subexcitacion.
    Fijando la toma a la relacion observada, el reactivo maximo por
    transformador baja de 864 a 375 Mvar y los generadores fuera de limite de
    12 a 9.

    Solo se ajustan los transformadores cuyas DOS barras tienen consigna real
    del reporte. Los que tengan una barra sin dato se quedan con toma 1,0,
    porque una relacion calculada contra el 1,0 por defecto seria inventada.
    """
    n = 0
    for t in network.transformers.index:
        b0 = network.transformers.at[t, 'bus0']
        b1 = network.transformers.at[t, 'bus1']
        if b0 not in buses_con_consigna or b1 not in buses_con_consigna:
            continue
        v0 = float(network.buses.at[b0, 'v_mag_pu_set'])
        v1 = float(network.buses.at[b1, 'v_mag_pu_set'])
        if v0 <= 0 or v1 <= 0:
            continue
        # convencion de PyPSA verificada a mano: la toma va como v0/v1
        network.transformers.loc[t, 'tap_ratio'] = v0 / v1
        n += 1
    if verbose and n < len(network.transformers):
        print(f"  {len(network.transformers) - n} transformadores se quedan "
              f"con toma 1,0 porque les falta la consigna de una barra")
    return n


def _liberar_slacks(network):
    """Devuelve a PV los generadores que eran slack, y SOLO esos.

    Sin este paso, redesignar sobre una red ya modificada deja DOS slacks: el
    nuevo y el de antes, que nadie quito. Pasa en cuanto el que era mayor deja
    de serlo ---rechazo parcial de carga, reparto del AGC, retirada de
    maquinas--- y es alcanzable desde `aplicar_evento` y desde
    `contingencias.simular`. Con dos slacks en la misma sub-red PyPSA elige
    uno y el reparto de perdidas deja de ser el que uno cree.

    OJO CON NO PASARSE. La forma obvia ---`generators['control'] = 'PV'`---
    arrasa tambien los PQ, y con `config.LIMITES_REACTIVOS` activo hay doce
    generadores en PQ con su Q fijada en el limite de su curva de capacidad.
    Devolverlos a PV deshace el lazo entero y les permite inventar reactivo
    otra vez. Es lo que hace `cascada._redesignar_slack`, que se escribio
    cuando no habia ningun PQ en el caso base.
    """
    era_slack = network.generators.control == 'Slack'
    if era_slack.any():
        network.generators.loc[era_slack, 'control'] = 'PV'


def designar_slack(network, verbose=True):
    """Designa un slack por isla: el generador mas grande de cada una.

    Si una isla no tiene ningun generador se crea uno en su bus mas cargado,
    para que PyPSA pueda resolverla en vez de devolver NaN.
    """
    _liberar_slacks(network)
    G = nx.Graph()
    G.add_nodes_from(network.buses.index)
    G.add_edges_from(zip(network.lines.bus0, network.lines.bus1))
    if len(network.transformers):
        G.add_edges_from(zip(network.transformers.bus0, network.transformers.bus1))

    islas = list(nx.connected_components(G))
    if verbose:
        print(f"Islas electricas: {len(islas)} -> {sorted((len(c) for c in islas), reverse=True)}")

    for i, comp in enumerate(islas):
        gens = network.generators[network.generators.bus.isin(comp)]
        if len(gens):
            slack = gens.p_set.idxmax()
            network.generators.loc[slack, 'control'] = 'Slack'
            if verbose:
                print(f"  isla {i} ({len(comp)} buses): slack = {slack} "
                      f"({network.generators.at[slack,'p_set']:.1f} MW)")
        else:
            cargas = network.loads[network.loads.bus.isin(comp)]
            bus = (cargas.p_set.idxmax() and
                   network.loads.at[cargas.p_set.idxmax(), 'bus']) \
                if len(cargas) else sorted(comp)[0]
            network.add("Generator", f"gen_slack_{i}", bus=bus,
                        p_set=0.0, control="Slack", v_set=1.0)
            if verbose:
                print(f"  isla {i} ({len(comp)} buses): sin generacion, "
                      f"slack creado en {bus}")


def run_power_flow(network, method='NR', verbose=True):
    """Ejecuta el flujo de carga y devuelve (ok, detalle).

    Comprueba de verdad la convergencia leyendo result.converged, que es lo
    que PyPSA usa para informar del resultado.
    """
    if method.upper() == 'DC':
        network.lpf()
        if verbose:
            print("Flujo DC (lineal) resuelto.")
        return True, None

    resultado = network.pf(x_tol=PF_TOLERANCE)
    conv = resultado.converged
    ok = bool(conv.values.all())

    detalle = pd.DataFrame({
        'converged': conv.iloc[0],
        'n_iter': resultado.n_iter.iloc[0],
        'error': resultado.error.iloc[0],
    })
    network.determine_network_topology()
    tam = {sn.name: len(sn.buses_i()) for sn in network.sub_networks.obj}
    detalle['buses'] = [tam.get(i, 0) for i in detalle.index]

    if verbose:
        if ok:
            print("Flujo AC convergido en todas las sub-redes.")
        else:
            print("EL FLUJO AC NO CONVERGIO en todas las sub-redes:")
        print(detalle.to_string())
    return ok, detalle


def comprobar_limites(network, verbose=True):
    """Informa de que elementos estan fuera de su limite. NO impone nada.

    POR QUE ES INFORMATIVO Y NO UNA RESTRICCION
    -------------------------------------------
    El caso base existe para reproducir el caso de referencia: toda su validez
    viene de parecerse al de PowerFactory. Imponer limites aqui lo apartaria de
    esa referencia para satisfacer parametros que son SUPUESTOS ---la curva de
    capacidad usa cos(phi) = 0,90 y k = 0,35, valores tipicos, no de
    fabricante---. Seria el peor intercambio: perder el anclaje a cambio de
    cumplir una hipotesis propia.

    Y hay una razon mas para no "arreglarlos": los generadores que salen fuera
    de curva son la unica medida disponible de cuanto se equivoca esa curva,
    mientras no lleguen los datos de fabricante. Imponer el limite borraria el
    termometro.

    En contingencias y cascada la asimetria se invierte ---no hay referencia
    contra la que contrastar, se extrapola a estados que nunca ocurrieron--- y
    alli los limites SI se aplican, via limites_reactivos.aplicar().

    Network.pf() de PyPSA tampoco podria imponerlos: es un Newton-Raphson que
    resuelve las ecuaciones de la red, no una optimizacion, asi que no tiene
    donde alojar una restriccion. s_nom solo sirve para dividir y medir.
    """
    out = {}

    # limite termico de lineas y transformadores
    n_term = tot = 0
    peor = 0.0
    for comp, tabla, pt, qt in (
            ('linea', network.lines, network.lines_t, network.lines_t),
            ('trafo', network.transformers, network.transformers_t,
             network.transformers_t)):
        if not len(tabla) or 'p0' not in pt:
            continue
        s_nom = tabla.s_nom.replace(0, np.nan)
        s = np.hypot(pt.p0.iloc[0], qt.q0.iloc[0])
        pct = (s / s_nom * 100).dropna()
        tot += len(pct)
        n_term += int((pct > 100.0).sum())
        if len(pct):
            peor = max(peor, float(pct.max()))
    out['termicos_fuera'] = n_term
    out['termicos_total'] = tot
    out['carga_max_pct'] = peor

    # curva de capacidad de los generadores
    try:
        from stn import limites_reactivos as LR
        d = LR.diagnostico(network)
        out['gen_fuera'] = len(d)
        out['gen_total'] = len(network.generators)
        out['detalle_gen'] = d
    except Exception as e:                                   # noqa: BLE001
        out['gen_fuera'] = None
        out['error'] = str(e)

    if verbose:
        print("\nComprobacion de limites (informativa, no se imponen)")
        print(f"  elementos sobre su limite termico : {n_term} de {tot} "
              f"(maximo {peor:.1f}%)")
        if out['gen_fuera'] is None:
            print(f"  generadores fuera de su curva     : no evaluable "
                  f"({out.get('error','')})")
        else:
            print(f"  generadores fuera de su curva     : "
                  f"{out['gen_fuera']} de {out['gen_total']}")
            if out['gen_fuera']:
                print("     curva parametrica cos(phi)=0,90, k=0,35: son "
                      "valores tipicos, no de fabricante.")
                print("     Que el caso de referencia los tenga produciendo "
                      "ese reactivo sugiere que la curva")
                print("     se queda corta, no que el caso este mal. Ver el "
                      "informe, seccion de datos faltantes.")
    return out


def resumen_resultados(network):
    """Tabla resumen del punto de operacion resuelto."""
    v = network.buses_t.v_mag_pu.iloc[0]
    carga_lineas = None
    if len(network.lines) and network.lines.s_nom.gt(0).any():
        s = np.hypot(network.lines_t.p0.iloc[0], network.lines_t.q0.iloc[0])
        carga_lineas = (s / network.lines.s_nom.replace(0, np.nan)).dropna()

    print("\nResultados del flujo de carga")
    print(f"  tension de bus  : min={v.min():.4f}  max={v.max():.4f}  "
          f"p.u.   fuera de [0,9-1,1]: {int(((v<0.9)|(v>1.1)).sum())}")
    p0 = network.lines_t.p0.iloc[0]
    print(f"  flujo de linea  : |P| max={p0.abs().max():.1f} MW   "
          f"NaN={int(p0.isna().sum())}")
    gen = network.generators_t.p.iloc[0].sum()
    load = network.loads_t.p.iloc[0].sum()
    print(f"  generacion total: {gen:.1f} MW")
    print(f"  demanda total   : {load:.1f} MW")
    print(f"  perdidas        : {gen-load:.1f} MW "
          f"({100*(gen-load)/max(gen,1e-9):.2f}% de la generacion)")
    if carga_lineas is not None and len(carga_lineas):
        print(f"  cargabilidad    : max={100*carga_lineas.max():.1f}%")
