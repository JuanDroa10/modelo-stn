# fuentes/powerfactory.py
# El camino de siempre, envuelto en la interfaz de fuente.
#
# -------------------------------------------------------------------------
# ESTE MODULO NO TIENE LOGICA
# -------------------------------------------------------------------------
# Y es deliberado. Llama a `data_integration.integrar()` y a
# `desagregacion.desglose_de()` con los mismos argumentos y en el mismo orden
# que los llamaba `contingencias.caso_base()`, y devuelve lo que devuelven.
#
# Si aqui hubiera una sola operacion propia ---un redondeo, un reindex, un
# fillna--- la validacion contra PowerFactory dejaria de ser comparable con la
# de antes, y el congelado de `Datos/golden_pf/` empezaria a fallar por un
# cambio de fontaneria en vez de por un cambio de modelo. La prueba de que no
# la tiene es que `test_regresion_pf.py` sigue en verde al introducirlo.
#
# Lo unico que se añade es la CACHE: `integrar()` cuesta varios segundos y se
# consultaba dos veces (una para las inyecciones y otra para el desglose, que
# las necesita). Cachear no cambia el resultado porque la funcion es pura
# respecto a sus ficheros de entrada.

from stn.fuentes.base import FuenteBase
from stn.fuentes.calidad import ReporteCalidad
from stn.paths import PUNTO, dato


class FuentePowerFactory(FuenteBase):
    """Punto de operacion del caso de referencia (`Datos/pq_completo.csv`).

    Es la fuente por omision de todo el modelo. Mientras lo sea, los numeros
    tienen que ser los de siempre: eso lo exige `test_regresion_pf.py`.
    """

    nombre = 'PowerFactory (caso de referencia)'

    def __init__(self, verbose=False):
        self.verbose = verbose
        self._iny = None
        self._resumen = None
        self._des = None
        self._hecho = False

    def _calcular(self):
        if self._hecho:
            return
        from stn.data_integration import integrar
        from stn import desagregacion

        self._iny, self._resumen = integrar(verbose=self.verbose)
        self._des = desagregacion.desglose_de(self._iny, verbose=self.verbose)
        self._hecho = True

    def inyecciones(self):
        self._calcular()
        return self._iny

    def desglose(self):
        self._calcular()
        return self._des

    def calidad(self):
        """Lo que `integrar()` ya sabia decir, en el formato comun.

        Se rellenan solo los campos que significan lo mismo en las dos
        fuentes. Los totales brutos se quedan a cero A PROPOSITO: el reporte
        de PowerFactory entrega inyecciones NETAS por nodo, asi que no existe
        un "despacho total" ni una "demanda total" que comparar con los de XM.
        Inventar la equivalencia haria que dos cifras con el mismo nombre
        midieran cosas distintas, que es peor que no tenerlas.
        """
        self._calcular()
        r = self._resumen or {}
        import pandas as pd

        sin_asignar = float(r.get('P_sin_asignar', 0.0) or 0.0)
        sin_equiv = float(r.get('P_sin_equivalente', 0.0) or 0.0)
        filas = []
        if sin_asignar:
            filas.append(('(nodos sin asignar)', 0.0, sin_asignar, 0.0,
                          f"{r.get('nodos_sin_asignar', 0)} nodos del reporte "
                          f"sin bus en el mapeo"))
        if sin_equiv:
            filas.append(('(nodos sin equivalente)', 0.0, sin_equiv, 0.0,
                          f"{r.get('nodos_sin_equivalente', 0)} nodos "
                          f"marcados sin equivalente en el mapeo"))
        cols = ['nodo', 'vn_kv', 'p_mw', 'q_mvar', 'razon']
        return ReporteCalidad(
            fuente=self.nombre,
            fecha='caso de referencia (sin fecha de operacion)',
            periodo=0,
            nodos_no_ubicados=pd.DataFrame(filas, columns=cols),
            procedencia={
                'fichero': dato(PUNTO, 'pq_completo.csv'),
                'nodos_reporte': r.get('nodos_reporte'),
                'nodos_asignados': r.get('nodos_asignados'),
                'buses_con_datos': r.get('buses_con_datos'),
            },
        )

    def preparar_red(self, network):
        """Nada. La topologia del caso de referencia es la que es."""
        return None
