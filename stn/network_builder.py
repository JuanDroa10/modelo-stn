# network_builder.py
"""Construccion de la red PyPSA a partir del DataFrame de lineas.

Cada subestacion se representa con un bus por nivel de tension y los niveles
de una misma subestacion se unen con un autotransformador. Esto corrige el
modelo anterior, que fusionaba los niveles en un unico nodo y hacia que 56
lineas conectaran buses de distinto voltaje nominal: PyPSA convierte la
impedancia de cada linea a por-unidad usando el v_nom de su bus0, de modo que
una linea de 500 kV colgada de un bus declarado a 230 kV quedaba con un error
de (500/230)^2 ~ 4,7 en su impedancia.
"""

import numpy as np
import pandas as pd
from pypsa import Network

from stn.config import (TRAFO_S_NOM, TRAFO_X_PU, TRAFO_R_PU,
                    INCLUIR_COMPLEMENTOS, TRAFO_PARATEC)

# PARATEC asigna los dos bancos de Heliconia a la subestacion MEDELLIN, pero
# los transformadores se llaman literalmente "HELICONIA 1 450 MVA 500/230/34.5
# KV" y "HELICONIA 2 ...". Es una inconsistencia del propio reporte: el campo
# Subestacion dice MEDELLIN y el nombre del activo dice HELICONIA. Se resuelve
# por el nombre del activo, que es el que identifica la maquina, y queda
# declarado aqui en vez de corregirse en silencio.
ALIAS_TRAFO_SUB = {'HELICONIA': 'MEDELLIN'}


def _coordenadas_por_bus(lines_df):
    """Promedia las coordenadas de los extremos de linea que llegan a cada bus."""
    acc = {}
    for _, r in lines_df.iterrows():
        for bus, lat, lon in ((r.bus0, r.lat0, r.lon0), (r.bus1, r.lat1, r.lon1)):
            if pd.isna(lat) or pd.isna(lon):
                continue
            acc.setdefault(bus, {'lat': [], 'lon': []})
            acc[bus]['lat'].append(float(lat))
            acc[bus]['lon'].append(float(lon))
    return {b: (float(np.mean(v['lat'])), float(np.mean(v['lon'])))
            for b, v in acc.items()}


def build_network(lines_df, verbose=True):
    """Devuelve un objeto Network con buses, lineas y transformadores."""
    network = Network()

    # --- buses -----------------------------------------------------------
    buses = {}
    for _, r in lines_df.iterrows():
        buses[r.bus0] = (r.sub0, r.nivel)
        buses[r.bus1] = (r.sub1, r.nivel)

    coords = _coordenadas_por_bus(lines_df)
    # Si un nivel de la subestacion no tiene coordenadas propias, se hereda
    # la del otro nivel de la misma subestacion.
    por_sub = {}
    for bus, (sub, _) in buses.items():
        if bus in coords:
            por_sub.setdefault(sub, coords[bus])

    # Si un bus sigue sin coordenadas se coloca en el centroide de sus vecinos
    # electricos. Antes caia en (0, 0), es decir en el golfo de Guinea, y
    # deformaba el mapa entero.
    sin_coord = [b for b in buses if b not in coords and buses[b][0] not in por_sub]
    if sin_coord:
        vecinos = {}
        for _, r in lines_df.iterrows():
            vecinos.setdefault(r.bus0, set()).add(r.bus1)
            vecinos.setdefault(r.bus1, set()).add(r.bus0)
        for b in sin_coord:
            pts = [coords[v] for v in vecinos.get(b, ()) if v in coords]
            if pts:
                coords[b] = (float(np.mean([p[0] for p in pts])),
                             float(np.mean([p[1] for p in pts])))
                if verbose:
                    print(f"  {b} sin coordenadas: se situa en el centroide "
                          f"de sus vecinos ({coords[b][0]:.4f}, {coords[b][1]:.4f})")

    for bus in sorted(buses):
        sub, nivel = buses[bus]
        lat, lon = coords.get(bus, por_sub.get(sub, (np.nan, np.nan)))
        network.add("Bus", bus,
                    v_nom=float(nivel),
                    x=np.nan if pd.isna(lon) else lon,
                    y=np.nan if pd.isna(lat) else lat)

    # --- lineas ----------------------------------------------------------
    for _, r in lines_df.iterrows():
        network.add("Line", r['name'],
                    bus0=r.bus0,
                    bus1=r.bus1,
                    length=float(r.length),
                    r=float(r.r),
                    x=float(r.x),
                    b=float(r.b),
                    s_nom=float(r.get('s_nom', 0.0) or 0.0))

    # Limite de emergencia, que es el criterio con el que se evalua una linea
    # despues de una contingencia. PyPSA no tiene un campo propio para el, asi
    # que se guarda como columna adicional del DataFrame.
    if 's_eme' in lines_df.columns:
        network.lines['s_eme'] = lines_df.set_index('name')['s_eme'].reindex(
            network.lines.index).fillna(0.0).astype(float)

    # --- autotransformadores entre niveles de una misma subestacion ------
    niveles_por_sub = {}
    for bus, (sub, nivel) in buses.items():
        niveles_por_sub.setdefault(sub, set()).add(nivel)

    # Potencia nominal real de los bancos, del reporte de Bahias de PARATEC.
    # Antes todos los autotransformadores usaban el mismo valor generico.
    ratings = {}
    try:
        from stn.paratec_equipos import autotransformadores
        ratings = autotransformadores()
    except Exception as e:
        print(f"  aviso: no se pudieron leer los datos de PARATEC ({e}); "
              f"se usa TRAFO_S_NOM generico")

    # Impedancias REALES del reporte "Detalle de transformadores de potencia"
    # (Paratec/, descarga del 20-09-2026). Sustituyen a TRAFO_X_PU y
    # TRAFO_R_PU, que eran valores genericos: 0,12 y 0,004 para los dieciseis.
    # Medido, la X real cae entre 0,1102 y 0,1177 ---el 0,12 se acercaba--- pero
    # la R real ronda 0,0013, TRES VECES menor que el generico.
    # Solo si config.TRAFO_PARATEC lo pide: por defecto NO, porque activarlo
    # empeora el contraste contra PowerFactory. La discusion completa, con las
    # cuatro medidas, esta en el comentario de ese flag.
    zpar = {}
    if TRAFO_PARATEC:
        try:
            from stn.paratec_equipos import autotrafos_stn
            zpar = autotrafos_stn().to_dict('index')
        except Exception as e:                               # noqa: BLE001
            print(f"  aviso: sin impedancias de transformador de PARATEC ({e}); "
                  f"se usan TRAFO_X_PU/TRAFO_R_PU genericos")

    n_trafos = 0
    n_reales = 0
    n_z_reales = 0
    for sub, niveles in sorted(niveles_por_sub.items()):
        niveles = sorted(niveles)
        for baja, alta in zip(niveles, niveles[1:]):
            b0, b1 = f"{sub}__{int(alta)}", f"{sub}__{int(baja)}"
            dato = ratings.get((sub, alta, baja))
            # x e r estan en p.u. sobre s_nom; los bancos en paralelo comparten
            # la misma p.u. sobre la base combinada, asi que solo cambia s_nom
            s_nom = dato[0] if dato else TRAFO_S_NOM
            if dato:
                n_reales += 1

            x, r = TRAFO_X_PU, TRAFO_R_PU
            z = zpar.get(sub) or zpar.get(ALIAS_TRAFO_SUB.get(sub, ''))
            if z and float(alta) >= 400 and float(baja) >= 200:
                # las p.u. de PARATEC estan sobre SU s_nom agregado; si el
                # modelo usa otro s_nom hay que rebasar o la reactancia fisica
                # cambiaria sin querer
                s_par = float(z['s_nom'])
                if s_par > 0 and float(s_nom) > 0:
                    k = float(s_nom) / s_par
                    x = float(z['x']) * k
                    if z['r'] == z['r']:
                        r = float(z['r']) * k
                    n_z_reales += 1
            network.add("Transformer", f"trafo_{sub}_{int(alta)}_{int(baja)}",
                        bus0=b0, bus1=b1,
                        s_nom=float(s_nom),
                        x=float(x),
                        r=float(r))
            n_trafos += 1

    # --- reactores de linea declarados en complementos.py ----------------
    n_react = 0
    if INCLUIR_COMPLEMENTOS:
        from stn import complementos
        for r in complementos.reactores():
            if r['bus'] in network.buses.index:
                network.add("ShuntImpedance", r['name'], bus=r['bus'], b=r['b'])
                n_react += 1

    if verbose:
        print(f"Red construida: {len(network.buses)} buses, "
              f"{len(network.lines)} lineas, {n_trafos} transformadores"
              + (f" ({n_reales} con MVA real de PARATEC)" if n_reales else "")
              + (f", {n_react} reactores" if n_react else ""))
        # comprobacion: ninguna linea debe unir buses de distinto v_nom
        v0 = network.lines.bus0.map(network.buses.v_nom)
        v1 = network.lines.bus1.map(network.buses.v_nom)
        n_mal = int((v0.values != v1.values).sum())
        if n_mal:
            print(f"  ADVERTENCIA: {n_mal} lineas unen buses de distinto v_nom")
        else:
            print("  OK: todas las lineas unen buses del mismo nivel de tension")
    return network
