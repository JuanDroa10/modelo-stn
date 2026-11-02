# fuentes/base.py
# El contrato que toda fuente de punto de operacion tiene que cumplir.
#
# -------------------------------------------------------------------------
# QUE ES UNA FUENTE
# -------------------------------------------------------------------------
# Un objeto que sabe responder a cuatro preguntas sobre UN punto de operacion
# concreto. Nada mas. No construye la red, no resuelve el flujo y no sabe que
# existe PyPSA mas alla de `preparar_red`.
#
#   inyecciones()   DataFrame por bus con P [MW], Q [Mvar] y v_set [pu]
#   desglose()      (gen, carga) en MW por bus, o None
#   calidad()       ReporteCalidad: que no se pudo ubicar y por que
#   preparar_red(n) retoques de topologia que dependen del punto (opcional)
#
# -------------------------------------------------------------------------
# POR QUE `preparar_red` EXISTE
# -------------------------------------------------------------------------
# Porque hay topologia que depende del dia. El caso que lo obligo: la
# interconexion con Ecuador. Un enlace con programa de intercambio nulo esta
# ABIERTO, no energizado en vacio, y dejarlo cerrado convierte Pimampiro en un
# radial sin carga que sube a 1,25 pu. Eso no se puede arreglar con una
# inyeccion ni con una consigna: hay que abrir los cuatro circuitos.
#
# La fuente de PowerFactory no lo usa ---su caso ya viene con la topologia que
# tiene--- y por eso la implementacion por defecto no hace nada.
#
# -------------------------------------------------------------------------
# EL ORDEN IMPORTA
# -------------------------------------------------------------------------
# `preparar_red` corre DESPUES de `build_network` y ANTES de
# `asignar_inyecciones`. Si corriera despues, habria que retirar a mano los
# generadores y las cargas de las barras que se van; corriendo antes, no
# llegan a existir.

from typing import Protocol, runtime_checkable

from stn.fuentes.calidad import ReporteCalidad


@runtime_checkable
class FuentePuntoOperacion(Protocol):
    """Lo que `contingencias.caso_base()` necesita de una fuente."""

    nombre: str

    def inyecciones(self):
        """DataFrame indexado por bus con P, Q y v_set.

        P y Q en convenio de INYECCION: positivo es que la barra entrega al
        sistema. `v_set` puede ser NaN, y entonces `power_flow` cae a 1,0.
        """

    def desglose(self):
        """(gen, carga) en MW por bus, o None si la fuente no lo sabe.

        Tiene que cumplir `gen - carga == inyecciones.P` bus a bus con 1e-6 de
        tolerancia. Sin el, los modulos de fallas dimensionan el EDAC sobre una
        demanda neteada contra la generacion embebida.
        """

    def calidad(self):
        """ReporteCalidad del punto."""

    def preparar_red(self, network):
        """Retoques de topologia que dependen del punto. Por omision, nada."""


class FuenteBase:
    """Implementacion minima: hereda de aqui y no tendras que escribir
    `preparar_red` ni `calidad` si tu fuente no los necesita."""

    nombre = 'sin nombre'

    def inyecciones(self):
        raise NotImplementedError

    def desglose(self):
        return None

    def calidad(self):
        return ReporteCalidad(fuente=self.nombre)

    def preparar_red(self, network):
        return None


def sesgo_de_slack(network):
    """Cuanto se aparta cada generador de su programa al cerrar el balance.

    Devuelve (total_MW, peor_generador, peor_MW, peor_pct, n_movidos).

    POR QUE SE MIDE. El flujo tiene que cuadrar generacion, demanda y
    perdidas, y lo que sobra o falta lo absorbe el slack. Con el caso de
    PowerFactory eso es ruido ---las inyecciones ya vienen de un caso resuelto
    y el ajuste es de 2 a 4 MW--- pero con un despacho de XM no: el programa
    de XM cubre el SIN entero y este modelo es un equivalente de 220/500, asi
    que la diferencia es real y cae ENTERA sobre una sola maquina. Medido el
    2026-10-20: 70,8 MW en el periodo 19 y 167,0 MW en el 3, que son el 7,4 %
    y el 14,7 % del programa de esa maquina.

    No es inocuo aunque el nadir apenas se mueva (8,9 y 24,6 mHz en esos dos
    casos, con los mismos escalones de EDAC): deja el estado del modelo
    incoherente con su propio despacho, y cualquier logica que mezcle `p_set`
    con la `p` resuelta hereda ese error.

    LA CAUSA NO SON LAS PERDIDAS. El hueco que declara XM correlaciona con la
    demanda a -0,648: BAJA cuando la demanda sube, y unas perdidas I2R hacen
    justo lo contrario. Es mas probable que sea la asignacion de AGC, que pesa
    relativamente mas de madrugada.
    """
    if not len(network.generators) or not len(network.generators_t.p):
        return 0.0, None, 0.0, 0.0, 0
    g = network.generators
    d = network.generators_t.p.iloc[0] - g.p_set
    if not len(d):
        return 0.0, None, 0.0, 0.0, 0
    peor = d.abs().idxmax()
    base = max(float(g.at[peor, 'p_set']), 1e-9)
    return (float(d.sum()), peor, float(d[peor]),
            100.0 * float(d[peor]) / base, int((d.abs() > 0.01).sum()))


def verificar_contrato(fuente, tol=1e-6):
    """Comprueba el invariante del desglose. Devuelve el peor error en MW.

    Se llama desde `caso_base()` antes de montar la red: una fuente que no
    conserva la inyeccion neta produce un sistema en el que la generacion y la
    demanda no cuadran con los flujos, y el sintoma aparece mucho mas tarde,
    en el nadir, donde ya no se puede atribuir.
    """
    iny = fuente.inyecciones()
    des = fuente.desglose()
    if des is None:
        return 0.0
    gen, carga = des
    d = (gen.reindex(iny.index).fillna(0.0)
         - carga.reindex(iny.index).fillna(0.0) - iny['P']).abs()
    peor = float(d.max()) if len(d) else 0.0
    if peor > tol:
        raise ValueError(
            f'la fuente {fuente.nombre!r} no conserva la inyeccion neta: '
            f'|gen - carga - P| = {peor:.3e} MW en {d.idxmax()!r} '
            f'(tolerancia {tol:g})')
    return peor
