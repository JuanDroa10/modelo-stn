# main.py
"""Modelo del STN colombiano (220/500 kV) sobre PyPSA.

Flujo de trabajo:

    1. Datos/extraer_pq_completo.py  <pdf> Datos/pq_completo.csv
    2. python generar_mapeo_stn.py   -> Datos/mapeo_STN.csv (revisar a mano)
    3. python main.py
"""

import argparse
import sys

from stn.config import PF_METHOD, PLOT_TITLE, salida
from stn.data_loader import load_lines
from stn.network_builder import build_network
from stn.data_integration import integrar, reconciliacion_demanda
from stn.power_flow import (asignar_inyecciones, designar_slack, run_power_flow,
                        resumen_resultados, comprobar_limites)
from stn.visualization import plot_network, export_network


def titulo(txt):
    print()
    print("=" * 64)
    print(f" {txt}")
    print("=" * 64)


def _construir_punta(fuente=None):
    """El pipeline de siempre, paso a paso. Devuelve (red, ok).

    `fuente` dice de donde sale el punto de operacion; por omision, del caso
    de PowerFactory. Es el mismo parametro que toma `contingencias.caso_base()`
    y la misma secuencia de llamadas, para que los dos caminos sigan
    resolviendo la misma red (lo comprueba
    `test_invariantes.test_los_cuatro_caminos_coinciden`).
    """
    if fuente is None:
        from stn.fuentes.powerfactory import FuentePowerFactory
        fuente = FuentePowerFactory(verbose=True)

    print("\n[1/5] Lineas de transmision")
    lines_df = load_lines()

    print("\n[2/5] Construccion de la red")
    network = build_network(lines_df)
    # Topologia que dependa del punto (enlaces internacionales sin programa).
    fuente.preparar_red(network)

    print(f"\n[3/5] Inyecciones equivalentes desde {fuente.nombre}")
    # El desglose bruto separa la inyeccion neta de cada barra en generacion y
    # demanda, para que los totales dejen de estar neteados contra la
    # generacion embebida bajo 200 kV. Misma llamada que hace
    # contingencias.caso_base(), para que los modulos de fallas vean el mismo
    # sistema que este pipeline.
    from stn.fuentes.base import verificar_contrato
    inyecciones = fuente.inyecciones()
    desglose = fuente.desglose()
    verificar_contrato(fuente)

    print("\n[4/5] Asignacion de generadores y cargas")
    asignar_inyecciones(network, inyecciones, desglose=desglose)
    designar_slack(network)

    print("\n[5/5] Flujo de carga")
    ok, detalle = run_power_flow(network, method=PF_METHOD)
    if not ok:
        return network, False

    # Mismo caso que ven los modulos de analisis. Sin esta linea el
    # pipeline principal informaba de una red distinta de la que todos
    # ellos usan; ver limites_reactivos.aplicar_si_procede().
    from stn import limites_reactivos
    n2 = limites_reactivos.aplicar_si_procede(network)
    if n2 is not network:
        network = n2
        print("  curva de capacidad de reactivo impuesta "
              f"({(network.generators.control == 'PQ').sum()} generadores "
              "conmutados a PQ; config.LIMITES_REACTIVOS)")
    return network, True


def _construir_diurno(hora):
    """El punto diurno, derivado del de punta. Devuelve (red, ok, ctx)."""
    from stn import punto_operacion as PO
    print(f"\n[1/2] Punto de operacion diurno (HourNN={hora})")
    # verbose=True imprime el informe comparativo propio del escenario, que
    # es la parte que explica de donde sale cada factor.
    network, _, ctx = PO.construir(hora=hora, verbose=True)
    print("\n[2/2] Flujo de carga")
    print("  resuelto dentro del punto fijo de perdidas del escenario")
    return network, True, ctx


def main():
    ap = argparse.ArgumentParser(
        description="Modelo del STN colombiano sobre PyPSA: construye el "
                    "punto de operacion, resuelve el flujo y lo reporta.")
    from stn import punto_operacion as PO
    PO.anadir_flag(ap)
    args = ap.parse_args()

    titulo("MODELO STN COLOMBIA")

    if args.fecha:
        from stn.fuentes.xm_snapshot import FuenteXM
        fuente = FuenteXM(args.fecha, args.periodo, verbose=True)
        network, ok = _construir_punta(fuente=fuente)
        ctx = {'hora': None, 'fecha': args.fecha, 'periodo': args.periodo,
               'etiqueta': PO.etiqueta(None, args.fecha, args.periodo),
               'sufijo': PO.sufijo(None, args.fecha, args.periodo),
               'info': None, 'calidad': fuente.calidad()}
    elif args.diurno is None:
        network, ok = _construir_punta()
        ctx = {'hora': None, 'fecha': None, 'periodo': None,
               'etiqueta': PO.etiqueta(None), 'sufijo': '',
               'info': None, 'calidad': None}
    else:
        network, ok, ctx = _construir_diurno(args.diurno)

    PO.cabecera(ctx)

    if ok:
        resumen_resultados(network)
        # Informativo: el flujo NO impone limites (ver el docstring de
        # comprobar_limites). Se reporta para que una violacion no quede
        # escondida en un modulo que hay que invocar aparte.
        comprobar_limites(network)

        if ctx['hora'] is None and not ctx.get('fecha'):
            # Por que la carga del modelo no es la demanda del SIN. Se imprime
            # siempre porque la diferencia (unos 3,6 GW) desconcierta a
            # cualquiera que compare con los informes de XM sin el desglose
            # delante.
            reconciliacion_demanda(network)
        elif ctx.get('fecha'):
            # La reconciliacion cuadra la carga del modelo contra el REPORTE
            # de PowerFactory. Un caso de XM no sale de ese reporte, asi que
            # el residuo no mediria un descuadre sino la distancia entre dos
            # dias distintos. Su equivalente es el reporte de calidad, que ya
            # sale en la cabecera.
            c = ctx['calidad']
            print("\nReconciliacion de demanda: no aplica al caso de XM.")
            print(f"  Su equivalente es el reporte de calidad de la fuente: "
                  f"{c.cobertura_demanda_pct:.2f} % de la demanda ubicada, "
                  f"{c.p_demanda_no_ubicada_mw:.1f} MW fuera.")
        else:
            # La reconciliacion cuadra la carga del modelo contra el REPORTE
            # de PowerFactory, que es de la punta. Correrla sobre el diurno
            # restaria la carga reescalada y neteada de solar de unos totales
            # de punta, y el residuo resultante ---que no seria otra cosa que
            # la propia transformacion horaria--- se leeria como un descuadre
            # del modelo. Se omite a proposito.
            print("\nReconciliacion de demanda: no aplica al caso diurno.")
            print("  Cuadra el modelo contra el reporte de PowerFactory, que "
                  "es de la punta;")
            print("  el diurno se deriva de el y no tiene reporte propio con "
                  "el que cuadrar.")

        from stn import zonas
        res = zonas.resumen(network)
        print("\nBalance por area operativa del STN")
        print(res.to_string())
        print(f"  {'TOTAL':14s} Gen={res.GenP.sum():9.1f}  "
              f"Load={res.LoadP.sum():9.1f}  Loss={res.LossP.sum():7.1f} MW")

        sub = zonas.resumen_subareas(network)
        print("\nDesglose por subarea")
        print(sub.to_string())

        # El contraste con Datos/resumen_por_zonas.csv no se imprime aqui
        # porque es otro escenario y confunde mas que ayuda. Sigue disponible
        # en zonas.comparar_con_referencia(res) si se quiere consultar.
    else:
        print("\nNo se analizan los resultados porque el flujo no convergio.")
        print("Revisa las sub-redes marcadas arriba.")

    print("\nVisualizacion y exportacion")
    # Los ficheros del diurno llevan sufijo. Sin el, correr el diurno pisaba
    # la figura, el mapa y la exportacion de la punta, y la carpeta de
    # resultados dejaba de decir a que caso pertenece cada cosa.
    suf = ctx['sufijo']
    titulo_fig = PLOT_TITLE + (f" - {ctx['etiqueta']}" if suf else "")
    try:
        plot_network(network, title=titulo_fig,
                     archivo=salida(f"red_stn{suf}.png"))
    except Exception as e:
        print(f"  no se pudo graficar: {e}")
    try:
        from stn.mapa_interactivo import construir
        construir(network, archivo=salida(f"mapa_stn_interactivo{suf}.html"))
    except ImportError:
        print("  mapa interactivo omitido (falta folium: pip install folium)")
    except Exception as e:
        print(f"  no se pudo generar el mapa interactivo: {e}")
    from stn.config import EXPORT_FOLDER
    export_network(network, folder=EXPORT_FOLDER + suf)

    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
