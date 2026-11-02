# fuentes/calidad.py
# Lo que no se pudo ubicar, con su razon.
#
# -------------------------------------------------------------------------
# POR QUE ESTO ES UN OBJETO Y NO UNOS PRINTS
# -------------------------------------------------------------------------
# Porque tiene que llegar hasta el analisis de frecuencia.
#
# `agc_deslastre.Sistema` toma `P_carga = network.loads.p_set.sum()` y el EDAC
# deslastra una FRACCION de esa base (config `EDAC_FRACCION = 0.05` por
# escalon). Si X MW de demanda no se ubicaron, `P_carga` se queda corta, cada
# escalon corta menos de lo que cortaria el esquema real, y el nadir sale
# OPTIMISTA. El error va en la direccion peligrosa.
#
# Y no se nota: el desbalance que deja la demanda ausente lo absorbe el slack
# y aparece disfrazado de perdidas. Medido sobre el 2026-10-20 p19, con una
# version anterior del mapeo que dejaba 215 MW sin ubicar, las "perdidas"
# salian 267,8 MW (2,24 %) cuando el balance real del snapshot es 181,0 MW
# (1,49 %). Nadie habria mirado dos veces un 2,2 % de perdidas.
#
# De ahi la regla, que es la de sinsim: lo que no se ubica se REPORTA, nunca
# se reparte en silencio sobre las demas barras.

from dataclasses import dataclass, field

import pandas as pd


# Por encima de esto, los modulos de frecuencia no deberian correr: un nadir
# calculado sobre una base de demanda incompleta no es un resultado, es un
# artefacto. 2 % es la mitad de un escalon del EDAC (5 %).
UMBRAL_DEMANDA_NO_UBICADA_PCT = 2.0

# Y lo mismo por el lado de la generacion, que al principio NO se vigilaba.
#
# El sintoma es distinto y por eso se escapo: la generacion que no se ubica no
# desaparece, la pone el SLACK. Una maquina sola acaba generando cientos de MW
# que su programa no tiene, y si el evento la tumba, `eventos.py` calcula el
# deficit con su `p_set` y subestima lo que se pierde. Medido el 2026-09-16,
# con un 95,4 % de cobertura de generacion: el slack se aparta +380 MW (30 %
# de su programa) en 20 de las 24 horas. En el 2026-10-14, +437,6 MW (50,9 %),
# y ahi el nadir sale 185 mHz optimista y con un escalon de EDAC de menos.
#
# El umbral es el mismo 2 %: sobre un despacho de 12.000 MW son 240 MW en una
# sola maquina, que ya es del orden de la mayor contingencia del sistema.
UMBRAL_DESPACHO_NO_UBICADO_PCT = 2.0


@dataclass
class ReporteCalidad:
    """Cobertura de un punto de operacion construido desde una fuente externa."""

    fuente: str = ''
    fecha: str = ''
    periodo: int = 0

    p_despacho_total_mw: float = 0.0
    p_despacho_ubicado_mw: float = 0.0
    p_demanda_total_mw: float = 0.0
    p_demanda_ubicada_mw: float = 0.0
    q_demanda_total_mvar: float = 0.0
    q_demanda_ubicada_mvar: float = 0.0

    # Detalle, nunca solo el agregado: una media se conserva mientras dos
    # errores se compensan.
    recursos_no_ubicados: pd.DataFrame = field(default_factory=pd.DataFrame)
    nodos_no_ubicados: pd.DataFrame = field(default_factory=pd.DataFrame)
    traslados: pd.DataFrame = field(default_factory=pd.DataFrame)
    buses_sin_datos: tuple = ()

    procedencia: dict = field(default_factory=dict)

    # -- derivados ---------------------------------------------------------

    @property
    def p_demanda_no_ubicada_mw(self):
        return self.p_demanda_total_mw - self.p_demanda_ubicada_mw

    @property
    def p_despacho_no_ubicado_mw(self):
        return self.p_despacho_total_mw - self.p_despacho_ubicado_mw

    @property
    def cobertura_demanda_pct(self):
        t = self.p_demanda_total_mw
        return 100.0 * self.p_demanda_ubicada_mw / t if t else 0.0

    @property
    def cobertura_despacho_pct(self):
        t = self.p_despacho_total_mw
        return 100.0 * self.p_despacho_ubicado_mw / t if t else 0.0

    @property
    def balance_mw(self):
        """Generacion menos demanda, ambas ya ubicadas. Son las perdidas que
        tendra que absorber el slack... MAS lo que no se ubico. Por eso hace
        falta `balance_crudo_mw` al lado."""
        return self.p_despacho_ubicado_mw - self.p_demanda_ubicada_mw

    @property
    def balance_crudo_mw(self):
        """El mismo balance sobre los totales del snapshot, sin ubicar nada.

        Es la referencia honesta: si `balance_mw` se aleja de esta, la
        diferencia es demanda o generacion perdida disfrazada de perdidas.
        """
        return self.p_despacho_total_mw - self.p_demanda_total_mw

    @property
    def sesgo_de_perdidas_mw(self):
        return self.balance_mw - self.balance_crudo_mw

    @property
    def pct_demanda_no_ubicada(self):
        t = self.p_demanda_total_mw
        return 100.0 * self.p_demanda_no_ubicada_mw / t if t else 100.0

    @property
    def pct_despacho_no_ubicado(self):
        t = self.p_despacho_total_mw
        return 100.0 * self.p_despacho_no_ubicado_mw / t if t else 100.0

    @property
    def motivos_no_apto(self):
        """Por que este punto no sirve para un estudio de frecuencia. Lista."""
        m = []
        if not self.p_demanda_total_mw or not self.p_despacho_total_mw:
            return ['el punto esta vacio']
        if self.pct_demanda_no_ubicada > UMBRAL_DEMANDA_NO_UBICADA_PCT:
            m.append(
                f"falta el {self.pct_demanda_no_ubicada:.2f} % de la demanda "
                f"({self.p_demanda_no_ubicada_mw:.0f} MW): el EDAC dimensiona "
                f"sobre lo que ve y cortaria de MENOS")
        if self.pct_despacho_no_ubicado > UMBRAL_DESPACHO_NO_UBICADO_PCT:
            m.append(
                f"falta el {self.pct_despacho_no_ubicado:.2f} % del despacho "
                f"({self.p_despacho_no_ubicado_mw:.0f} MW): lo pone el slack, "
                f"una sola maquina, y el deficit de un evento sale corto")
        return m

    @property
    def apto_para_frecuencia(self):
        """Si el punto sirve para dimensionar frecuencia.

        Mira los DOS lados. Al principio solo vigilaba la demanda, y por eso
        se escapo un caso peor: el 2026-09-16 tiene 99,0 % de demanda ubicada
        ---perfecto--- y 95,4 % de generacion, con el slack tapando el hueco.
        """
        return not self.motivos_no_apto

    # -- presentacion ------------------------------------------------------

    def resumen(self):
        """Texto de una pantalla. Se imprime SIEMPRE, tambien cuando todo va
        bien: una cobertura del 99 % que nadie ve es una cobertura en la que
        nadie repara cuando baja al 88 %."""
        L = []
        L.append(f"Calidad del punto de operacion  [{self.fuente}]"
                 f"  {self.fecha} periodo {self.periodo}")
        L.append(f"  generacion : {self.p_despacho_ubicado_mw:9.1f} de "
                 f"{self.p_despacho_total_mw:9.1f} MW "
                 f"({self.cobertura_despacho_pct:6.2f} %)")
        L.append(f"  demanda    : {self.p_demanda_ubicada_mw:9.1f} de "
                 f"{self.p_demanda_total_mw:9.1f} MW "
                 f"({self.cobertura_demanda_pct:6.2f} %)")
        L.append(f"  balance    : {self.balance_mw:9.1f} MW "
                 f"(crudo {self.balance_crudo_mw:.1f}; "
                 f"sesgo {self.sesgo_de_perdidas_mw:+.1f} MW "
                 f"que el slack cobraria como perdidas)")
        if len(self.recursos_no_ubicados):
            L.append(f"  {len(self.recursos_no_ubicados)} recursos sin ubicar "
                     f"({self.p_despacho_no_ubicado_mw:.1f} MW):")
            for _, r in self.recursos_no_ubicados.nlargest(
                    4, 'p_mw').iterrows():
                L.append(f"      {r.recurso:<26s} {r.p_mw:8.2f} MW  {r.razon}")
        if len(self.nodos_no_ubicados):
            L.append(f"  {len(self.nodos_no_ubicados)} nodos de demanda sin "
                     f"ubicar ({self.p_demanda_no_ubicada_mw:.1f} MW):")
            for _, r in self.nodos_no_ubicados.nlargest(4, 'p_mw').iterrows():
                L.append(f"      {r.nodo:<26s} {r.p_mw:8.2f} MW  {r.razon}")
        if self.buses_sin_datos:
            L.append(f"  {len(self.buses_sin_datos)} buses del modelo sin "
                     f"dato de XM: {list(self.buses_sin_datos)[:5]}")
        for m in self.motivos_no_apto:
            L.append(f"  AVISO: {m}")
        return '\n'.join(L)

    def a_dict(self):
        """Para serializar junto a los resultados. Sin los DataFrame."""
        return {
            'fuente': self.fuente, 'fecha': self.fecha,
            'periodo': self.periodo,
            'p_despacho_total_mw': self.p_despacho_total_mw,
            'p_despacho_ubicado_mw': self.p_despacho_ubicado_mw,
            'p_demanda_total_mw': self.p_demanda_total_mw,
            'p_demanda_ubicada_mw': self.p_demanda_ubicada_mw,
            'cobertura_despacho_pct': self.cobertura_despacho_pct,
            'cobertura_demanda_pct': self.cobertura_demanda_pct,
            'balance_mw': self.balance_mw,
            'balance_crudo_mw': self.balance_crudo_mw,
            'sesgo_de_perdidas_mw': self.sesgo_de_perdidas_mw,
            'n_recursos_no_ubicados': len(self.recursos_no_ubicados),
            'n_nodos_no_ubicados': len(self.nodos_no_ubicados),
            'n_traslados': len(self.traslados),
            'n_buses_sin_datos': len(self.buses_sin_datos),
            'apto_para_frecuencia': self.apto_para_frecuencia,
            'procedencia': self.procedencia,
        }
