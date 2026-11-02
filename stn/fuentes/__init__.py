# fuentes/
# De donde sale el punto de operacion del modelo.
#
# Hasta la fase 0 solo habia una fuente, cableada: el reporte de PowerFactory
# (`Datos/pq_completo.csv`), leido por `data_integration.integrar()`. Este
# paquete abre la puerta a que haya mas de una sin tocar el motor de dinamica.
#
# Todas las fuentes entregan lo MISMO, que es el contrato que ya consumian
# `power_flow.asignar_inyecciones()` y, a traves de el, `agc_deslastre`,
# `cascada` y `deslastre_btm`:
#
#   inyecciones  DataFrame indexado por bus ("SAN CARLOS__230")
#                  P      MW    >0 la barra INYECTA, <0 CONSUME
#                  Q      Mvar  mismo convenio
#                  v_set  pu    consigna, NaN permitido
#
#   desglose     (gen, carga), dos Series en MW por bus
#                  invariante: gen - carga == inyecciones.P   (tol 1e-6)
#
#   calidad      ReporteCalidad: lo que NO se pudo ubicar, con su razon.
#
# El tercero es el que marca la diferencia. La fuente de PowerFactory lo
# entrega casi vacio porque el mapeo esta curado a mano desde hace meses; la
# de XM no, y lo que se queda fuera tiene que llegar hasta el analisis de
# frecuencia en vez de desaparecer en el slack. Ver `fuentes/calidad.py`.
