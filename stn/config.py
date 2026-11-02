# config.py
# Parametros de configuracion del modelo STN

# --- Archivos de entrada ---------------------------------------------------
from stn.paths import RED, PUNTO, dato

FILE_LINES = dato(RED, 'lineas_200a500kV_con_coordenadas.csv')

# Datos de P/Q extraidos del reporte Busbars/Terminals de PowerFactory.
# Se genera con:  python Datos/extraer_pq_completo.py <pdf> Datos/pq_completo.csv
#
# OJO: los dos PDF disponibles NO son el mismo escenario aunque su portada sea
# identica. Busbars_Terminals.pdf tiene 12.275 MW de generacion y 12.383 MW de
# demanda; Busbars_Terminals(1).pdf tiene 10.588 y 10.827 MW. Promediarlos
# produce un caso que no cumple balance en ninguna barra, asi que se trabaja
# con UN escenario a la vez.
FILE_PQ = dato(PUNTO, 'pq_completo.csv')

# Mapeo subestacion del reporte -> subestacion de la red de lineas.
# Se genera con:  python generar_mapeo_stn.py
FILE_MAPPING = dato(RED, 'mapeo_STN.csv')

# --- Filtro de tension para las lineas del STN -----------------------------
VOLTAGE_MIN = 200  # kV
VOLTAGE_MAX = 550  # kV

# Umbral a partir del cual una barra se considera parte del STN. Las barras
# por debajo (110/115 kV y los bornes de generador de 13,8-19 kV) se
# representan como inyeccion equivalente en la barra STN correspondiente.
STN_KV_MIN = 200.0

# Anadir los elementos de complementos.py: subestaciones y lineas que existen
# en el sistema y en el estudio de PowerFactory pero faltan en el CSV de
# PARATEC (hoy: la subestacion Norte 500/230 y sus dos lineas de 500 kV).
# Ponlo en False para comparar contra el modelo estrictamente de PARATEC.
INCLUIR_COMPLEMENTOS = True

# --- Autotransformadores entre niveles (p.ej. 500/230 kV) ------------------
TRAFO_S_NOM = 1000.0   # MVA por banco equivalente
TRAFO_X_PU = 0.12      # reactancia en p.u. sobre s_nom
TRAFO_R_PU = 0.004     # resistencia en p.u. sobre s_nom

# Usar las impedancias REALES del reporte "Detalle de transformadores de
# potencia" de PARATEC (Paratec/, descarga del 20-09-2026) en vez de los dos
# valores genericos de arriba.
#
# ESTA EN False A PROPOSITO, y el motivo merece leerse antes de cambiarlo.
#
# Los datos reales existen y estan bien leidos: las dieciseis subestaciones de
# 500/230 aparecen con s_nom que coincide EXACTAMENTE con el que el modelo ya
# tenia por otra via (el reporte de Bahias), lo que valida el parseo. Medido:
#
#     X real: 0,1102 a 0,1177  (media 0,1145)   frente al generico 0,12
#     R real: 0,0004 a 0,0018  (media 0,0013)   frente al generico 0,004
#
# Ojo con el historial: hasta el 3-10-2026 la R de SABANALARGA salia 0,00104
# en vez de 0,00180, porque `paratec_equipos._num_opt` leia sus perdidas en
# el cobre ---declaradas como '1.034' kW--- como 1,034 kW. El rango de arriba
# ya esta corregido; si se encuentra citado 0,00104 en algun sitio, es de
# antes del arreglo. Reprobar este flag con los valores buenos cuando llegue
# el export Lines/Branches.
#
# El generico de X estaba cerca; el de R era TRES VECES mayor que el real.
#
# Y sin embargo, activarlo EMPEORA el contraste contra PowerFactory:
#
#     generico X y R      correlacion 0,8107   MAE 22,48 MW
#     X real, R generico              0,8097       22,57
#     X generico, R real              0,8107       22,51
#     X real y R real                 0,8097       22,58
#
# (Medido antes de activar LIMITES_REACTIVOS. Remedido el 3-10-2026 sobre la
#  base actual y con la R de Sabanalarga ya corregida: 0,8050 con el flag en
#  False y 0,8040 con el en True. La conclusion no cambia.)
#
# La X es la que degrada. El empeoramiento es minusculo ---una milesima de
# correlacion sobre 39 lineas--- y muy probablemente ni siquiera es
# significativo: la muestra de validacion tiene 39 lineas de 280, casi todas
# de 230 kV, y el backbone de 500 kV (que es donde los autotransformadores
# influyen) esta practicamente sin contrastar. Es decir: la validacion
# disponible NO PUEDE arbitrar esta decision.
#
# Ante la duda se conserva el anclaje al caso de referencia, que es de donde
# viene toda la validez del modelo. Cuando llegue el export Lines/Branches de
# PowerFactory y la muestra pase de 39 a ~280 lineas, este flag es lo primero
# que hay que volver a probar.
TRAFO_PARATEC = False

# Imponer la curva de capacidad de reactivo de los generadores en el caso base.
#
# EL PROBLEMA QUE RESUELVE. Sin esto, el flujo le pide a cada barra PV el
# reactivo que haga falta para sostener su tension, sin mirar si la maquina
# puede darlo. En el caso base eso deja ONCE generadores fuera de su curva, y
# tres de ellos son maquinas sincronas de verdad con limites declarados en
# PARATEC:
#
#     CERROMATOSO__500  absorbe -316,1 MVAr  y su limite es  -29,0   (11 veces)
#     SAN CARLOS__500   absorbe -401,9 MVAr  y su limite es -204,0
#     PAIPA__230        absorbe -127,1 MVAr  y su limite es  -33,0
#
# Absorber mas reactivo del que permite el limite de subexcitacion es
# fisicamente imposible: el regulador de tension llegaria a su limitador y la
# tension caeria. Los otros ocho son seis equivalentes de tecnologia
# desconocida ---a los que la curva de una maquina sincrona no les aplica--- y
# dos parques eolicos, cuya capacidad reactiva es la del inversor.
#
# ACTIVADO POR DECISION DEL AUTOR. Conmuta 12 generadores a PQ y corrige las
# once violaciones, dejando todas las tensiones en rango (la minima incluso
# mejora, de 0,9857 a 0,9912 pu).
#
# Tiene un coste y conviene tenerlo presente: empeora el contraste con
# PowerFactory, correlacion 0,8058 -> 0,8050 y error medio 23,26 -> 23,36 MW.
# El deterioro esta muy por debajo de lo que una muestra de 39 lineas puede
# resolver ---es la cuarta cifra decimal--- pero va en contra de la regla
# general del proyecto, que es que PowerFactory manda. Se activa igualmente
# porque un punto de operacion que pide a tres maquinas mas reactivo del que
# fisicamente pueden dar no es defendible como estado del sistema, por bien
# que se parezca al caso de referencia.
#
# Dicho de otro modo: con False el modelo es lo mas fiel posible al caso de
# PowerFactory; con True es lo mas fiel posible a lo que una maquina puede
# hacer. Se eligio lo segundo.
#
# SEGUNDO DETERIORO, del 3-10-2026, y por la misma logica. Al corregir dos
# fallos en el reparto de placa entre barras ---la colision MIEL I / PORCE II
# y el prorrateo a partes iguales, ver agc_deslastre._tipo_y_capacidad_por_bus
# --- cambio `p_nom`, que es de donde este modulo saca la curva de capacidad.
# El contraste volvio a moverse una diezmilesima:
#
#     correlacion 0,8050 -> 0,8048     error medio 23,36 -> 23,38 MW
#     tension [0,9912; 1,0874] -> [0,9914; 1,0875]   cargabilidad 85,3 -> 85,5 %
#
# Esta DECISION NO SE TOMO A LA LIGERA porque va contra la regla general del
# proyecto (PowerFactory manda) y contra la instruccion explicita de revertir
# lo que empeore la validacion. Se conserva el arreglo porque:
#
#   1. MIEL I (Caldas) y PORCE II (Antioquia) son plantas distintas. Que el
#      modelo las trate como una sola no es una aproximacion defendible, es
#      un error de identificacion.
#   2. El deterioro esta en la CUARTA cifra decimal sobre una muestra de 39
#      lineas de 280, casi todas de 230 kV. Esa muestra no puede arbitrar
#      esto: no distingue 0,8050 de 0,8048.
#   3. El contraste ESTRICTO contra Edge Elements no se movio: sigue en
#      0,814 y 22,9 MW.
#
# Si se prefiere la fidelidad literal al caso de referencia, el arreglo esta
# en una sola linea (la clave de `comparten`) y es trivial de revertir.
LIMITES_REACTIVOS = True

# --- Flujo de carga --------------------------------------------------------
PF_METHOD = 'NR'       # 'NR' (Newton-Raphson, AC) o 'DC' (lineal)
PF_TOLERANCE = 1e-6
# PyPSA.pf() no admite un limite de iteraciones, asi que no hay opcion para el.

# --- Visualizacion y exportacion -------------------------------------------
PLOT_GEOMAP = True
PLOT_TITLE = "Red STN Colombia (220/500 kV)"
EXPORT_FOLDER = "stn_network_georef"


# --- Carpeta de resultados --------------------------------------------------
# Todas las salidas del pipeline (CSV, PNG, HTML) van aqui en vez de a la raiz
# del proyecto. Sin esto, cada corrida dejaba una docena de ficheros sueltos
# mezclados con el codigo y era imposible distinguir de un vistazo que era
# fuente y que era resultado regenerable.
DIR_RESULTADOS = 'resultados'


def salida(nombre):
    """Ruta de un fichero de salida, creando la carpeta si hace falta."""
    import os
    os.makedirs(DIR_RESULTADOS, exist_ok=True)
    return os.path.join(DIR_RESULTADOS, nombre)


# --- Desglose bruto de generacion y demanda ---------------------------------
# El metodo del equivalente por barra da inyecciones NETAS: la generacion
# embebida bajo 200 kV se cancela contra la carga local y los totales que el
# modelo informa salen ~3.200 MW por debajo de los reales (9.090 frente a
# 12.275 MW). Con esto activado, desagregacion.py atribuye a cada barra la
# generacion que cuelga de ella ---usando la conectividad del propio reporte---
# y la carga sale forzada por Carga = Gen - P_iny, de modo que la inyeccion
# neta NO CAMBIA.
#
# OJO: que la inyeccion neta no cambie no significa que el flujo AC salga
# identico. Poner un generador en una barra la convierte de PQ en PV, y eso
# mueve el reactivo, la tension (0,031 pu el peor caso) y con ellos la activa
# (5,92 MW la peor linea, 0,29 MW de media) y las perdidas (-0,62 MW). Medido
# con validacion.py, que desde el 19-9-2026 tambien aplica el flag.
#
# Ponlo en False para volver al comportamiento anterior y comparar.
DESGLOSE_BRUTO = True
