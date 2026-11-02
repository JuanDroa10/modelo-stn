# data_integration.py
"""Convierte el reporte de PowerFactory en inyecciones por bus del STN.

El modelo solo representa la red de 220/500 kV, pero el 98% de la demanda del
sistema esta en 110/115 kV y toda la generacion en 13,8-19 kV. En vez de
intentar aproximar esa demanda, se usa el equivalente exacto que el propio
reporte proporciona: el flujo de los transformadores en cada barra del STN
resume todo lo que cuelga por debajo de ella.

Para una barra del STN:

    inyeccion = generacion_directa - (trafos + cargas + shunts)

con el convenio del reporte (positivo = sale de la barra, salvo las maquinas
que inyectan). Un transformador con P>0 baja potencia hacia 110/115 kV y
representa demanda; con P<0 la sube desde los generadores y representa
generacion.

La suma de todas las inyecciones asi calculadas da +296 MW sobre el escenario
de Busbars_Terminals.pdf, que son las perdidas del STN: el equivalente cierra.
"""

import numpy as np
import pandas as pd

from stn.config import FILE_MAPPING, STN_KV_MIN
from stn.generar_mapeo_stn import cargar_reporte, nodos_stn


def _tension_reportada(df):
    """Tension en p.u. reportada por PowerFactory para cada nodo del STN.

    Se usa como consigna de los generadores PV, de modo que el flujo de carga
    reproduzca el punto de operacion del estudio original.
    """
    stn = df[df['nivel'] >= STN_KV_MIN].copy()
    stn['v'] = pd.to_numeric(stn['Voltage Magnitude p.u.'], errors='coerce')
    v = stn.dropna(subset=['v']).groupby(['nodo', 'nivel'])['v'].mean()
    return v[(v > 0.8) & (v < 1.2)]


def cargar_mapeo(path=FILE_MAPPING):
    mapeo = pd.read_csv(path, encoding='utf-8-sig')
    mapeo.columns = mapeo.columns.str.strip()
    mapeo['Bus_red'] = mapeo['Bus_red'].fillna('').astype(str).str.strip()
    return mapeo


def integrar(verbose=True):
    """Devuelve (inyecciones, resumen).

    inyecciones: DataFrame indexado por bus con columnas P, Q y v_set.
        P > 0  -> el bus inyecta (generacion equivalente)
        P < 0  -> el bus consume (demanda equivalente)
    """
    df = cargar_reporte()
    nodos = nodos_stn(df)
    tension = _tension_reportada(df)
    mapeo = cargar_mapeo()

    asignados = mapeo[mapeo.Bus_red != ''].copy()
    idx = pd.MultiIndex.from_arrays(
        [asignados.Nodo_PDF, asignados.kV_PDF.astype(float)])
    asignados = asignados.set_index(idx)

    filas = []
    no_encontrados = []
    for clave, fila in asignados.iterrows():
        if clave not in nodos.index:
            no_encontrados.append(clave)
            continue
        n = nodos.loc[clave]
        filas.append({
            'bus': fila['Bus_red'],
            'P': float(n['P_iny']),
            'Q': float(n['Q_iny']),
            'v_set': float(tension.get(clave, np.nan)),
        })

    if not filas:
        raise RuntimeError(
            "El mapeo no asigno ningun nodo. Ejecuta generar_mapeo_stn.py.")

    iny = pd.DataFrame(filas)
    # varios nodos del reporte pueden caer en el mismo bus (p.ej. las cuatro
    # unidades de Ituango en ANTIOQUIA 500): se suman sus inyecciones
    agg = iny.groupby('bus').agg(P=('P', 'sum'), Q=('Q', 'sum'),
                                 v_set=('v_set', 'mean'))

    # Barras que a proposito no llevan Bus_red y no son un olvido:
    #   irrelevante     -> no aportan P ni Q
    #   sin_equivalente -> no existen en el CSV de lineas
    #   modelado_aparte -> ya entran al modelo desde complementos.py
    resueltas = ['irrelevante', 'sin_equivalente', 'modelado_aparte']
    pendientes = mapeo[(mapeo.Bus_red == '') & (~mapeo.Metodo.isin(resueltas))]
    sin_equiv = mapeo[mapeo.Metodo == 'sin_equivalente']
    sin_asignar = pendientes
    resumen = {
        'nodos_reporte': len(nodos),
        'nodos_asignados': len(iny),
        'buses_con_datos': len(agg),
        'P_total': float(agg.P.sum()),
        'P_inyeccion': float(agg.P[agg.P > 0].sum()),
        'P_demanda': float(-agg.P[agg.P < 0].sum()),
        'nodos_sin_asignar': len(sin_asignar),
        'P_sin_asignar': float(sin_asignar.Inyeccion_MW.abs().sum()),
        'nodos_sin_equivalente': len(sin_equiv),
        'P_sin_equivalente': float(sin_equiv.Inyeccion_MW.abs().sum()),
        'clave_no_encontrada': no_encontrados,
    }

    if verbose:
        print(f"Nodos del reporte asignados : {resumen['nodos_asignados']} "
              f"-> {resumen['buses_con_datos']} buses")
        print(f"  generacion equivalente    : {resumen['P_inyeccion']:9.1f} MW")
        print(f"  demanda equivalente       : {resumen['P_demanda']:9.1f} MW")
        print(f"  balance (gen - demanda)   : {resumen['P_total']:9.1f} MW "
              f"(deben ser las perdidas del STN)")
        if resumen['nodos_sin_equivalente']:
            print(f"  sin equivalente en el CSV de lineas (esperado): "
                  f"{resumen['nodos_sin_equivalente']} nodos, "
                  f"{resumen['P_sin_equivalente']:.1f} MW")
        if resumen['nodos_sin_asignar']:
            print(f"  PENDIENTES DE REVISAR: {resumen['nodos_sin_asignar']} nodos, "
                  f"{resumen['P_sin_asignar']:.1f} MW (revisa {FILE_MAPPING})")
        if no_encontrados:
            print(f"  AVISO: {len(no_encontrados)} filas del mapeo no existen "
                  f"en el reporte: {no_encontrados[:3]}")
    return agg, resumen


# ---------------------------------------------------------------------------
# Reconciliacion: de la demanda del SIN a la carga del modelo
# ---------------------------------------------------------------------------

# Picos de demanda del SIN publicados por XM para contrastar el escenario.
# Es un dato externo, no sale del reporte: sirve para saber si el caso que se
# esta reproduciendo es de punta o de valle.
XM_PICOS_MW = "XM reporta picos de 12.449-12.475 MW en mayo y junio"


def reconciliacion_demanda(network, verbose=True):
    """Explica por que la carga del modelo no es la demanda del SIN.

    Son dos magnitudes distintas y la diferencia desconcierta si no se
    desglosa. El modelo representa solo el STN, asi que su "carga" es lo que
    la red de 220/500 kV ENTREGA, neto por barra; la demanda del SIN es lo que
    consume el pais. Entre una y otra hay tres restas:

      1. Lo que se sirve por debajo de 200 kV sin tocar el STN. Generacion
         embebida en 110/115 kV: plantas menores, AGPE, autoproductores.
      2. El neteo por barra. Wilches sube 750 MW de Hidrosogamoso y baja
         129,5 MW a distribucion en la MISMA barra; PyPSA solo necesita el
         neto, +620,5 MW, asi que esos 129,5 MW de carga real no aparecen en
         la suma de cargas. Es correcto, pero hay que decirlo.
      3. Los nodos que el mapeo no resuelve.

    Todos los terminos se calculan aqui, ninguno esta fijado a mano.
    """
    df = cargar_reporte()
    stn = df[df['nivel'] >= STN_KV_MIN]
    p = 'Active Power MW'

    dem_sin = float(df[df['Element type'] == 'General Load'][p].sum())

    tr = stn[stn.cat == 'trafo']
    baja = tr[tr[p] > 0].groupby(['nodo', 'nivel'])[p].sum()
    sube = -tr[tr[p] < 0].groupby(['nodo', 'nivel'])[p].sum()
    carga_dir = stn[stn.cat == 'carga'].groupby(['nodo', 'nivel'])[p].sum()
    gen_dir = stn[stn.cat == 'gen'].groupby(['nodo', 'nivel'])[p].sum()

    idx = baja.index.union(sube.index).union(carga_dir.index).union(gen_dir.index)
    abajo = (baja.reindex(idx).fillna(0.0) + carga_dir.reindex(idx).fillna(0.0))
    arriba = (sube.reindex(idx).fillna(0.0) + gen_dir.reindex(idx).fillna(0.0))

    entrega_stn = float(abajo.sum())
    local = dem_sin - entrega_stn
    # lo que se cancela dentro de cada barra y por tanto no llega a ser "carga"
    neteo = float(np.minimum(abajo, arriba).sum())
    carga_modelo = float(network.loads.p_set.sum())
    residuo = entrega_stn - neteo - carga_modelo

    if verbose:
        print("\nDe la demanda del SIN a la carga del modelo")
        print(f"  {dem_sin:10.1f} MW   demanda del SIN en el caso de referencia")
        print(f"  {'':10s}      {XM_PICOS_MW}: el caso ES de punta")
        print(f"  {-local:10.1f} MW   servida localmente sin pasar por el STN"
              f"   ({100*local/max(dem_sin,1e-9):.1f}%)")
        print(f"  {entrega_stn:10.1f} MW   = lo que el STN entrega de verdad")
        print(f"  {-neteo:10.1f} MW   neteo contra generacion en la misma barra")
        print(f"  {-residuo:10.1f} MW   nodos sin mapear y residuo")
        print(f"  {carga_modelo:10.1f} MW   = CARGA DEL MODELO")
        print("  La carga del modelo NO es la demanda del SIN: es lo que el STN"
              " transporta, neto por barra.")

    return {'demanda_sin': dem_sin, 'servida_local': local,
            'entrega_stn': entrega_stn, 'neteo': neteo,
            'residuo': residuo, 'carga_modelo': carga_modelo}
