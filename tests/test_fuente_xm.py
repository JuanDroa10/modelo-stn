# test_fuente_xm.py
"""Invariantes de la fuente de punto de operacion desde XM.

    python test_fuente_xm.py          # todo
    python test_fuente_xm.py -k cober # solo lo que case con 'cober'

POR QUE EXISTE. `fuentes/xm_snapshot.py` tiene que producir EXACTAMENTE el
mismo contrato que `data_integration.integrar()` + `desagregacion.desglose_de()`
para que la fase 2 pueda intercambiarlas sin que el motor de dinamica se entere.
Aqui se fija ese contrato, y la cobertura minima por debajo de la cual el punto
de operacion no sirve para un estudio de frecuencia.

NO comprueban que el resultado sea el del caso de PowerFactory: son casos
distintos, de dias distintos. Eso lo cubre `test_regresion_pf.py`, y solo para
la fuente de PowerFactory.
"""

import sys

import pandas as pd

from stn.fuentes import nombres as N
from stn.fuentes.xm_snapshot import FuenteXM, fechas_disponibles

FECHA = '2026-10-20'
PERIODO = 19

# Umbrales de aceptacion de la fase 1. Medidos sobre las 24 horas del
# 2026-10-20: generacion 98,54-99,26 %, demanda 98,92-99,08 %.
COBERTURA_GEN_MIN = 98.0
COBERTURA_DEM_MIN = 98.0
# El balance ubicado no puede alejarse del crudo mas que esto, o la demanda
# que falta se estaria colando como perdidas. Ver fuentes/calidad.py.
SESGO_MAX_MW = 60.0

_CACHE = {}


def fuente(periodo=PERIODO):
    if periodo not in _CACHE:
        _CACHE[periodo] = FuenteXM(FECHA, periodo, verbose=False)
    return _CACHE[periodo]


def _hay_snapshot():
    assert FECHA in fechas_disponibles(), (
        f'no hay snapshot de {FECHA}; las pruebas no pueden correr. '
        f'Disponibles: {fechas_disponibles()}')


# ---------------------------------------------------------------------------
# El contrato con el resto del modelo
# ---------------------------------------------------------------------------

def test_el_desglose_conserva_la_inyeccion_neta():
    """Gen(b) - Carga(b) = P(b). El mismo invariante que exige el modelo.

    Aqui deberia salir exacto, no aproximado: la generacion y la demanda
    llegan separadas de origen, asi que P es una RESTA, no una atribucion.
    """
    _hay_snapshot()
    for h in (1, 10, 19, 24):
        f = fuente(h)
        iny = f.inyecciones()
        gen, carga = f.desglose()
        d = (gen - carga - iny.P).abs().max()
        assert d < 1e-9, f'periodo {h}: |gen-carga-P| = {d:.3e} MW'


def test_las_inyecciones_tienen_las_columnas_del_contrato():
    _hay_snapshot()
    iny = fuente().inyecciones()
    assert list(iny.columns) == ['P', 'Q', 'v_set'], (
        f'columnas {list(iny.columns)}, se esperaba [P, Q, v_set]')
    assert iny.index.name == 'bus'
    assert not iny.P.isna().any(), 'hay P a NaN'
    assert not iny.Q.isna().any(), 'hay Q a NaN'


def test_todos_los_buses_existen_en_el_modelo():
    """Una inyeccion en un bus que la red no tiene se pierde sin aviso."""
    _hay_snapshot()
    f = fuente()
    sobran = sorted(set(f.inyecciones().index) - set(f.buses_modelo))
    assert not sobran, f'buses que el modelo no tiene: {sobran[:8]}'


def test_la_consigna_de_tension_es_uniforme_por_nivel():
    """v_set fijo por nivel, nunca heredado de otro caso.

    XM no publica tension por barra. Se fija una consigna, que es lo que un
    operador hace; heredar el perfil del caso de PowerFactory se midio y daba
    1,162 pu, porque ese perfil es la SOLUCION de otro despacho.
    """
    _hay_snapshot()
    from stn.fuentes.xm_snapshot import V_SET_POR_NIVEL
    iny = fuente().inyecciones()
    assert not iny.v_set.isna().any(), 'hay barras sin consigna'
    for bus, v in iny.v_set.items():
        _, nivel, _ = N.subestacion_de_bus(bus, {})
        esperado = V_SET_POR_NIVEL.get(N.nivel_modelo(nivel), 1.0)
        assert abs(v - esperado) < 1e-12, (
            f'{bus}: v_set {v}, se esperaba {esperado}')


def test_la_consigna_llega_a_las_barras_sin_inyeccion():
    """Si no, los 16 autotrafos se quedan en toma 1,0.

    `_ajustar_taps` solo ajusta un transformador si sus DOS barras traen
    consigna, y `asignar_inyecciones` solo recorre las filas de
    `inyecciones`. Las barras de 500 kV de transito no reciben ni carga ni
    generacion de XM: si el indice se limitara a las que tienen dato, se
    ajustarian 0 de 16.
    """
    _hay_snapshot()
    f = fuente()
    faltan = sorted(set(f.buses_modelo) - set(f.inyecciones().index))
    assert not faltan, (
        f'{len(faltan)} barras del modelo sin fila: {faltan[:6]}')


def test_las_barras_sin_dato_no_inventan_inyeccion():
    """Estan en el indice por su consigna, pero con P y Q a cero."""
    _hay_snapshot()
    f = fuente()
    iny, c = f.inyecciones(), f.calidad()
    assert c.buses_sin_datos, 'se esperaba alguna barra sin dato de XM'
    sub = iny.loc[list(c.buses_sin_datos)]
    assert (sub.P.abs() < 1e-12).all(), 'una barra sin dato trae P'
    assert (sub.Q.abs() < 1e-12).all(), 'una barra sin dato trae Q'


def test_la_demanda_entra_con_signo_de_consumo():
    """P<0 y Q<0 en las barras que solo tienen carga."""
    _hay_snapshot()
    f = fuente()
    iny = f.inyecciones()
    gen, carga = f.desglose()
    solo_carga = iny.index[(gen == 0) & (carga > 0)]
    assert len(solo_carga) > 20, 'muy pocas barras de solo carga; sospechoso'
    assert (iny.loc[solo_carga, 'P'] < 0).all(), 'una barra de carga inyecta P'
    assert (iny.loc[solo_carga, 'Q'] <= 0).all(), 'una barra de carga inyecta Q'


# ---------------------------------------------------------------------------
# Cobertura: lo que no se ubica tiene que ser poco Y estar contado
# ---------------------------------------------------------------------------

def test_la_cobertura_supera_el_umbral_en_las_24_horas():
    _hay_snapshot()
    malas = []
    for h in range(1, 25):
        c = FuenteXM(FECHA, h, verbose=False).calidad()
        if c.cobertura_despacho_pct < COBERTURA_GEN_MIN:
            malas.append(f'p{h} gen {c.cobertura_despacho_pct:.2f} %')
        if c.cobertura_demanda_pct < COBERTURA_DEM_MIN:
            malas.append(f'p{h} dem {c.cobertura_demanda_pct:.2f} %')
    assert not malas, 'cobertura por debajo del umbral: ' + ', '.join(malas)


def test_lo_que_no_se_ubica_esta_contado_y_con_razon():
    """Nada desaparece en silencio: cada MW ausente tiene fila y motivo."""
    _hay_snapshot()
    c = fuente().calidad()

    suma_g = c.recursos_no_ubicados.p_mw.sum() if len(
        c.recursos_no_ubicados) else 0.0
    assert abs(suma_g - c.p_despacho_no_ubicado_mw) < 1e-6, (
        f'generacion sin ubicar: el detalle suma {suma_g:.3f} MW pero el '
        f'agregado dice {c.p_despacho_no_ubicado_mw:.3f} MW')

    suma_d = c.nodos_no_ubicados.p_mw.sum() if len(
        c.nodos_no_ubicados) else 0.0
    assert abs(suma_d - c.p_demanda_no_ubicada_mw) < 1e-6, (
        f'demanda sin ubicar: el detalle suma {suma_d:.3f} MW pero el '
        f'agregado dice {c.p_demanda_no_ubicada_mw:.3f} MW')

    for df, col in ((c.recursos_no_ubicados, 'recurso'),
                    (c.nodos_no_ubicados, 'nodo')):
        if len(df):
            vacias = df[df.razon.isna() | (df.razon.astype(str).str.strip() == '')]
            assert not len(vacias), f'{len(vacias)} filas sin razon en {col}'


def test_la_demanda_ausente_no_se_disfraza_de_perdidas():
    """El balance ubicado no puede alejarse del crudo del snapshot.

    Si se aleja, la demanda que falta la esta absorbiendo el slack y aparece
    como perdidas. Con una version anterior del mapeo que dejaba 215 MW sin
    ubicar, las perdidas salian 267,8 MW en vez de 181,0.
    """
    _hay_snapshot()
    malas = []
    for h in range(1, 25):
        c = FuenteXM(FECHA, h, verbose=False).calidad()
        if abs(c.sesgo_de_perdidas_mw) > SESGO_MAX_MW:
            malas.append(f'p{h}: {c.sesgo_de_perdidas_mw:+.1f} MW')
    assert not malas, ('el balance se aleja del crudo: ' + ', '.join(malas))


def test_el_balance_es_positivo_y_del_orden_de_las_perdidas():
    """Generacion > demanda, y por un margen de perdidas creible (0,5-4 %)."""
    _hay_snapshot()
    malas = []
    for h in range(1, 25):
        c = FuenteXM(FECHA, h, verbose=False).calidad()
        pct = 100.0 * c.balance_mw / c.p_demanda_ubicada_mw
        if not 0.5 <= pct <= 4.0:
            malas.append(f'p{h}: {c.balance_mw:.1f} MW ({pct:.2f} %)')
    assert not malas, 'balance fuera del rango de perdidas: ' + ', '.join(malas)


def test_la_generacion_sin_ubicar_tambien_descalifica_el_punto():
    """No basta con vigilar la demanda, y eso costo descubrirlo.

    El 2026-09-16 tiene 99,0 % de demanda ubicada ---perfecto--- y se colaba
    como apto con un 95,4 % de generacion. Los MW que faltan no desaparecen:
    los pone el SLACK, una sola maquina, que acababa generando +380 MW sobre
    su programa en 20 de las 24 horas.
    """
    from stn.fuentes.calidad import (ReporteCalidad,
                                 UMBRAL_DESPACHO_NO_UBICADO_PCT)

    # demanda impecable, generacion coja: tiene que salir NO apto
    c = ReporteCalidad(
        p_demanda_total_mw=10000.0, p_demanda_ubicada_mw=9950.0,
        p_despacho_total_mw=10000.0, p_despacho_ubicado_mw=9500.0)
    assert not c.apto_para_frecuencia, (
        'un punto con el 5 % del despacho sin ubicar paso como apto')
    assert any('despacho' in m for m in c.motivos_no_apto), (
        f'el motivo no menciona el despacho: {c.motivos_no_apto}')

    # y justo por debajo del umbral, si
    falta = UMBRAL_DESPACHO_NO_UBICADO_PCT / 100.0 * 0.9
    ok = ReporteCalidad(
        p_demanda_total_mw=10000.0, p_demanda_ubicada_mw=9950.0,
        p_despacho_total_mw=10000.0,
        p_despacho_ubicado_mw=10000.0 * (1 - falta))
    assert ok.apto_para_frecuencia, ok.motivos_no_apto


def test_una_correccion_sin_evidencia_no_entra():
    """Mover cientos de MW de subestacion exige dejar la fuente."""
    import os
    import tempfile
    from stn.fuentes.xm_snapshot import cargar_correcciones
    with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False,
                                     encoding='utf-8') as fh:
        fh.write('recurso,subestacion,nota\nX,CERROMATOSO,\n')
        p = fh.name
    try:
        cargar_correcciones(p)
    except ValueError as e:
        assert 'nota' in str(e), f'mensaje poco claro: {e}'
        return
    finally:
        os.unlink(p)
    raise AssertionError('se acepto una correccion sin nota')


def test_la_correccion_curada_ubica_gecelca():
    """GECELCA no tiene NINGUNA linea en PARATEC: sin la correccion, 240 MW
    los ponia el slack en silencio."""
    _hay_snapshot()
    from stn.fuentes.xm_snapshot import cargar_correcciones
    corr = cargar_correcciones()
    assert 'GECELCA 32' in corr, 'falta la correccion de GECELCA 32'
    f = FuenteXM('2026-09-16', 11, verbose=False)
    sin = set(f.calidad().recursos_no_ubicados.recurso)
    assert 'GECELCA 32' not in sin, 'GECELCA 32 sigue sin ubicarse'
    tr = f.calidad().traslados
    assert (tr.tipo == 'generacion (curado)').any(), (
        'el traslado curado no quedo registrado en el reporte de calidad')


def test_el_punto_es_apto_para_un_estudio_de_frecuencia():
    _hay_snapshot()
    c = fuente().calidad()
    assert c.apto_para_frecuencia, (
        f'falta el {100*c.p_demanda_no_ubicada_mw/c.p_demanda_total_mw:.2f} % '
        f'de la demanda; el EDAC dimensionaria sobre una base incompleta')


# ---------------------------------------------------------------------------
# Que no se invente ni se pierda energia por el camino
# ---------------------------------------------------------------------------

def test_el_reparto_entre_barras_conserva_el_despacho():
    """Un recurso que cae en varias barras reparte su P, no la multiplica."""
    _hay_snapshot()
    f = fuente()
    c = f.calidad()
    gen, _ = f.desglose()
    assert abs(gen.sum() - c.p_despacho_ubicado_mw) < 1e-6, (
        f'la generacion por bus suma {gen.sum():.3f} MW y lo ubicado es '
        f'{c.p_despacho_ubicado_mw:.3f} MW')
    assert gen.sum() <= c.p_despacho_total_mw + 1e-6, (
        'se ubico mas generacion de la que hay en el despacho')


def test_la_demanda_incluye_las_perdidas_pstr():
    """P = Ener.Act. + Perd.PSTR, el mismo criterio que sinsim."""
    _hay_snapshot()
    f = fuente()
    d = f.tablas['demand']
    d = d[d.period == PERIODO]
    esperado = float((d.p_act_mw + d.p_pstr_mw).sum())
    assert abs(f.calidad().p_demanda_total_mw - esperado) < 1e-6, (
        'la demanda total no coincide con Ener.Act. + Perd.PSTR')
    solo_act = float(d.p_act_mw.sum())
    assert abs(f.calidad().p_demanda_total_mw - solo_act) > 1.0, (
        'parece que se esta ignorando Perd.PSTR')


def test_la_misma_entrada_da_el_mismo_resultado():
    """Determinista: el BFS recorre conjuntos, y un set no tiene orden."""
    _hay_snapshot()
    a = FuenteXM(FECHA, PERIODO, verbose=False).inyecciones()
    b = FuenteXM(FECHA, PERIODO, verbose=False).inyecciones()
    assert list(a.index) == list(b.index), 'el indice cambia entre corridas'
    d = (a[['P', 'Q']] - b[['P', 'Q']]).abs().max().max()
    assert d == 0.0, f'dos construcciones difieren en {d:.3e}'


# ---------------------------------------------------------------------------
# Nombres
# ---------------------------------------------------------------------------

def test_los_diacriticos_no_separan_barras():
    assert N.norm('CAÑO LIMON') == N.norm('CANO LIMON') == 'CANO LIMON'
    assert N.norm('OCAÑA') == 'OCANA'
    assert N.norm('  el   rio ') == 'EL RIO'


def test_el_nivel_agrupa_200_a_400_kv_en_230():
    for kv in (220, 230, 240):
        assert N.nivel_modelo(kv) == 230, f'{kv} kV no cae en 230'
    assert N.nivel_modelo(500) == 500
    assert N.nivel_modelo(115) == 115


def test_los_restos_del_nombre_del_tramo_se_limpian():
    casos = {'PALENQUE 1 230 KV__230': 'PALENQUE',
             'SOGAMOSO 2 230 KV__230': 'SOGAMOSO',
             'PURNIO (230)__230': 'PURNIO',
             'LA VIRGINIA (500 230)__500': 'LA VIRGINIA',
             'CANO LIMON__230': 'CANO LIMON'}
    for bus, esperado in casos.items():
        sub, _, _ = N.subestacion_de_bus(bus, {})
        assert sub == esperado, f'{bus!r} -> {sub!r}, se esperaba {esperado!r}'


def test_la_barra_de_unidad_no_desplaza_a_la_barra_base():
    """HIDROSOGAMOSO__230_U1 y SOGAMOSO__230 conviven; gana la base."""
    m = N.buses_por_subestacion(
        ['SOGAMOSO__230', 'SOGAMOSO__230_U1', 'SOGAMOSO__230_U2'], {})
    assert m['SOGAMOSO'] == 'SOGAMOSO__230', (
        f"la carga de SOGAMOSO subiria a {m['SOGAMOSO']!r}")


def test_una_colision_de_nombres_se_levanta_no_se_resuelve_sola():
    """Dos barras distintas del modelo en la misma clave tienen que fallar.

    Es el fallo que ya se pago con MIEL I y PORCE II: misma firma, placa
    repartida a partes iguales, y nadie se entero.
    """
    try:
        N.buses_por_subestacion(['GUAVIO__230', 'GUAVIO SUBT DUCTO__230'],
                                {'GUAVIO SUBT DUCTO': 'GUAVIO'})
    except ValueError as e:
        assert 'colision' in str(e).lower(), f'mensaje poco claro: {e}'
        return
    raise AssertionError('la colision no salto: dos barras se fusionaron')


def test_un_alias_sin_evidencia_no_entra():
    """La columna `nota` es obligatoria: un alias sin fuente es una suposicion."""
    import os
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False,
                                     encoding='utf-8') as f:
        f.write('juancho,paratec,nota\nA,B,\n')
        p = f.name
    try:
        N.cargar_alias(p)
    except ValueError as e:
        assert 'nota' in str(e), f'mensaje poco claro: {e}'
        return
    finally:
        os.unlink(p)
    raise AssertionError('se acepto un alias sin nota')


# ---------------------------------------------------------------------------
# Enlaces internacionales
# ---------------------------------------------------------------------------

def test_el_enlace_con_ecuador_sale_como_inactivo():
    """El 2026-10-20 los dos enlaces estan a 0 MW las 24 horas."""
    _hay_snapshot()
    for h in (1, 12, 19, 24):
        inac = FuenteXM(FECHA, h, verbose=False).enlaces_inactivos()
        assert any(e['recurso'] == 'ECUADOR230_1' for e in inac), (
            f'periodo {h}: ECUADOR230_1 no sale como inactivo')


def test_abrir_el_enlace_retira_solo_la_barra_del_exterior():
    """PIMAMPIRO es una hoja: abrir sus cuatro circuitos no arrastra nada mas."""
    _hay_snapshot()
    import logging
    logging.disable(logging.INFO)
    from stn.data_loader import load_lines
    from stn.network_builder import build_network
    from stn.fuentes.xm_snapshot import abrir_enlaces

    red = build_network(load_lines(verbose=False), verbose=False)
    n_buses, n_lineas = len(red.buses), len(red.lines)
    lin, buses = abrir_enlaces(red, fuente().enlaces_inactivos(), verbose=False)
    assert len(lin) == 4, f'se abrieron {len(lin)} circuitos, se esperaban 4'
    assert buses == ['PIMAMPIRO__230'], f'barras retiradas: {buses}'
    assert len(red.buses) == n_buses - 1
    assert len(red.lines) == n_lineas - 4


def test_un_enlace_activo_no_se_abre():
    """La regla es el programa, no el nombre del recurso."""
    _hay_snapshot()
    from stn.fuentes.xm_snapshot import cargar_enlaces
    f = fuente()
    d = f.tablas['dispatch']
    d = d[d.period == PERIODO]
    prog = {N.norm(r.resource): float(r.p_mw) for _, r in d.iterrows()}
    inac = {e['recurso'] for e in f.enlaces_inactivos()}
    for e in cargar_enlaces():
        p = prog.get(N.norm(e['recurso']), 0.0)
        if abs(p) > 1e-9:
            assert e['recurso'] not in inac, (
                f"{e['recurso']} despacha {p} MW y se marco inactivo")


def test_un_enlace_sin_evidencia_no_entra():
    """Abrir una linea del modelo sin dejar la fuente no se admite."""
    import os
    import tempfile
    from stn.fuentes.xm_snapshot import cargar_enlaces
    with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False,
                                     encoding='utf-8') as fh:
        fh.write('recurso,bus_colombia,bus_exterior,nota\nX,A__230,B__230,\n')
        p = fh.name
    try:
        cargar_enlaces(p)
    except ValueError as e:
        assert 'nota' in str(e), f'mensaje poco claro: {e}'
        return
    finally:
        os.unlink(p)
    raise AssertionError('se acepto un enlace sin nota')


def test_el_enlace_internacional_activo_se_ubica_en_su_barra():
    """Un enlace que despacha no puede caer en 'sin unidades en PARATEC'.

    Los enlaces no tienen unidades: su potencia entra por la barra de
    conexion. El 2026-10-20 estan a 0 y no se nota, que es justo lo que lo
    hacia facil de pasar por alto.
    """
    _hay_snapshot()
    c = fuente().calidad()
    if not len(c.recursos_no_ubicados):
        return
    malos = [r for r in c.recursos_no_ubicados.recurso
             if str(r).upper().startswith('ECUADOR')]
    assert not malos, f'enlaces reportados como no ubicados: {malos}'


# ---------------------------------------------------------------------------
# La costura: caso_base tiene que aceptar una fuente sin enterarse de cual
# ---------------------------------------------------------------------------

def test_las_dos_fuentes_cumplen_el_protocolo():
    from stn.fuentes.base import FuentePuntoOperacion
    from stn.fuentes.powerfactory import FuentePowerFactory
    for f in (FuentePowerFactory(verbose=False), FuenteXM(FECHA, PERIODO,
                                                          verbose=False)):
        assert isinstance(f, FuentePuntoOperacion), (
            f'{type(f).__name__} no cumple el protocolo')
        for m in ('inyecciones', 'desglose', 'calidad', 'preparar_red'):
            assert callable(getattr(f, m, None)), (
                f'{type(f).__name__} no tiene {m}()')


def test_sin_fuente_el_caso_base_sigue_siendo_el_de_powerfactory():
    """La omision no puede cambiar: es lo que defiende el golden."""
    import logging
    logging.disable(logging.INFO)
    from stn import contingencias as C
    from tools import golden_pf

    red, diag = C.caso_base(verbose=False)
    meta, _ = golden_pf.cargar()
    g = meta['diagnostico']
    assert abs(diag['carga_MW'] - g['carga_MW']) < 1e-3, (
        f"carga {diag['carga_MW']} vs golden {g['carga_MW']}")
    assert abs(diag['v_min'] - g['v_min']) < 1e-6


def test_el_caso_base_acepta_la_fuente_de_xm():
    """El mismo motor, otro punto: 147 buses porque Pimampiro se retira."""
    _hay_snapshot()
    import logging
    logging.disable(logging.INFO)
    from stn import contingencias as C

    f = FuenteXM(FECHA, PERIODO, verbose=False)
    red, diag = C.caso_base(verbose=False, fuente=f)
    assert len(red.buses) == len(f.buses_modelo) - 1, (
        f'{len(red.buses)} buses; se esperaba uno menos que '
        f'{len(f.buses_modelo)} al abrir el enlace')
    assert 'PIMAMPIRO__230' not in red.buses.index
    assert abs(diag['carga_MW'] - f.calidad().p_demanda_ubicada_mw) < 1.0, (
        'la carga de la red no es la demanda ubicada por la fuente')


def test_la_dinamica_corre_sobre_el_punto_de_xm():
    """Lo que justifica toda la integracion: el motor no se toca.

    Y de paso se ve el premio: la inercia deja de ser un numero fijo y pasa a
    depender del compromiso de unidades real de cada hora.
    """
    _hay_snapshot()
    import logging
    logging.disable(logging.INFO)
    from stn import agc_deslastre as AGC
    from stn import contingencias as C

    inercia = {}
    for h in (3, 19):
        red, _ = C.caso_base(verbose=False,
                             fuente=FuenteXM(FECHA, h, verbose=False))
        sis = AGC.Sistema(red)
        assert sis.P_carga > 0 and sis.S_base > 0 and sis.H > 0
        inercia[h] = sis.H * sis.S_base
    assert inercia[19] > inercia[3], (
        f'la punta deberia tener mas inercia que la madrugada: {inercia}')


def test_una_fuente_que_no_conserva_la_inyeccion_se_rechaza():
    """El invariante se comprueba ANTES de montar la red.

    Si no, el sintoma aparece mucho mas tarde ---en el nadir--- donde ya no se
    puede atribuir a la fuente.
    """
    import pandas as pd
    from stn.fuentes.base import FuenteBase, verificar_contrato

    class Rota(FuenteBase):
        nombre = 'rota'

        def inyecciones(self):
            return pd.DataFrame({'P': [10.0], 'Q': [0.0], 'v_set': [1.0]},
                                index=['A__230'])

        def desglose(self):
            return (pd.Series({'A__230': 10.0}),
                    pd.Series({'A__230': 5.0}))      # 10 - 5 != 10

    try:
        verificar_contrato(Rota())
    except ValueError as e:
        assert 'inyeccion neta' in str(e), f'mensaje poco claro: {e}'
        return
    raise AssertionError('se acepto una fuente que no conserva la inyeccion')


# ---------------------------------------------------------------------------
# El factor de produccion solar y su uso en el BTM
# ---------------------------------------------------------------------------

def test_la_solar_sigue_el_dia():
    """Cero de noche, maximo a mediodia. Si esto falla, el dato esta mal."""
    _hay_snapshot()
    pu = {}
    for h in (3, 7, 12, 19, 23):
        _, _, pu[h] = FuenteXM(FECHA, h, verbose=False).produccion_solar_pu()
    assert pu[3] == 0.0 and pu[23] == 0.0, f'la solar no es cero de noche: {pu}'
    assert pu[12] > 0.5, f'al mediodia solo {pu[12]:.3f} pu'
    assert pu[12] > pu[7] > pu[19], f'el perfil no sigue el dia: {pu}'
    for h, v in pu.items():
        assert 0.0 <= v <= 1.0, f'periodo {h}: {v} pu fuera de [0, 1]'


def test_la_placa_solar_no_cambia_con_la_hora():
    """La placa es del parque, no del momento: solo varia la produccion."""
    _hay_snapshot()
    placas = {h: FuenteXM(FECHA, h, verbose=False).produccion_solar_pu()[1]
              for h in (3, 12, 19)}
    assert len(set(round(p, 6) for p in placas.values())) == 1, (
        f'la placa cambia con la hora: {placas}')


def test_el_btm_sigue_siendo_exogeno_sobre_un_punto_de_xm():
    """El pool BTM sale de PARATEC, NO del despacho de XM.

    Es la propiedad que no se puede perder: las 536 plantas que PARATEC marca
    "No Despachada Centralmente" aparecen TODAS en el dDEC, asi que lo que
    esta detras del contador es invisible ahi. Si algun dia el pool empezara a
    salir del snapshot, estaria contando generacion que YA esta en el
    despacho, y el deficit del evento saldria inflado.
    """
    _hay_snapshot()
    import logging
    logging.disable(logging.INFO)
    from stn import contingencias as C
    from stn import deslastre_btm as DB
    from stn import zonas

    esc = DB.EscenarioBTM()
    placas = {}
    for h in (3, 12):
        red, _ = C.caso_base(verbose=False,
                             fuente=FuenteXM(FECHA, h, verbose=False))
        placas[h] = float(DB.repartir_btm(red, esc,
                                          zonas.area_por_bus(red)).sum())
    # La PLACA del BTM es la misma a las 3 y a las 12: es un inventario de
    # PARATEC. Lo que cambia con la hora es cuanto de ella produce.
    assert abs(placas[3] - placas[12]) < 1.0, (
        f'la placa del BTM depende de la hora del despacho: {placas}. '
        f'El pool deberia venir solo de PARATEC.')
    assert placas[12] > 0, 'el pool BTM salio vacio'


# ---------------------------------------------------------------------------
# El barrido de 24 horas
# ---------------------------------------------------------------------------

def test_el_barrido_mide_lo_que_dice_medir():
    """Tres periodos bastan: lo caro es la hora, no el recorrido.

    Se comprueba que las columnas existen y que son coherentes entre si, no
    que valgan un numero concreto: eso cambia legitimamente al mejorar los
    datos y seria una prueba tautologica.
    """
    _hay_snapshot()
    import logging
    logging.disable(logging.INFO)
    from stn import barrido

    df = barrido.barrer(FECHA, [3, 12, 19], evento_mw=1500.0, verbose=False)
    assert len(df) == 3
    assert df.convergio.all(), f'alguna hora no resolvio: {df.nota.tolist()}'
    for c in ('demanda_MW', 'HS_MVAs', 'S_base_MVA', 'H_s', 'nadir_Hz',
              'RoCoF_Hz_s', 'edac_escalones', 'cob_dem_pct', 'apto'):
        assert c in df.columns, f'falta la columna {c}'
    # H*S tiene que ser el producto de sus dos factores
    d = (df.HS_MVAs - df.H_s * df.S_base_MVA).abs().max()
    assert d < 1e-6, f'H*S no es H por S_base: {d:.3e}'
    # el nadir de un evento de 1500 MW tiene que estar por debajo de 60 Hz
    assert (df.nadir_Hz < 60.0).all(), 'un evento de 1500 MW no bajo la f'
    assert (df.nadir_Hz > 50.0).all(), 'nadir imposible'
    # la punta tiene mas maquinas acopladas que la madrugada
    assert df.loc[19, 'S_base_MVA'] > df.loc[3, 'S_base_MVA']


def test_el_barrido_marca_las_horas_que_no_son_de_fiar():
    """`apto` tiene que venir de la cobertura, no de la convergencia.

    Una hora puede resolver perfectamente y aun asi no servir para un estudio
    de frecuencia si le falta demanda por ubicar.
    """
    _hay_snapshot()
    import logging
    logging.disable(logging.INFO)
    from stn import barrido

    df = barrido.barrer(FECHA, [19], evento_mw=1500.0, verbose=False)
    fila = df.loc[19]
    cal = FuenteXM(FECHA, 19, verbose=False).calidad()
    assert bool(fila.apto) == cal.apto_para_frecuencia
    assert abs(fila.cob_dem_pct - cal.cobertura_demanda_pct) < 1e-9


# ---------------------------------------------------------------------------
# Errores de uso
# ---------------------------------------------------------------------------

def test_un_periodo_fuera_de_rango_falla_pronto():
    for h in (0, 25, -1):
        try:
            FuenteXM(FECHA, h, verbose=False)
        except ValueError:
            continue
        raise AssertionError(f'el periodo {h} no fallo')


def test_una_fecha_sin_snapshot_dice_como_descargarla():
    try:
        FuenteXM('1999-01-01', 1, verbose=False)
    except FileNotFoundError as e:
        assert 'sinsim scrape' in str(e), (
            f'el mensaje no dice como arreglarlo: {e}')
        return
    raise AssertionError('una fecha inexistente no fallo')


# ---------------------------------------------------------------------------

def main():
    filtro = None
    if '-k' in sys.argv:
        filtro = sys.argv[sys.argv.index('-k') + 1].lower()

    pruebas = [(k, v) for k, v in globals().items()
               if k.startswith('test_') and callable(v)
               and (filtro is None or filtro in k.lower())]
    if not pruebas:
        print('ninguna prueba coincide con el filtro')
        return 1

    print(f'{len(pruebas)} pruebas de la fuente XM ({FECHA})\n')
    fallos = []
    for nombre, fn in pruebas:
        etq = nombre[5:].replace('_', ' ')
        try:
            fn()
            print(f'  OK    {etq}')
        except AssertionError as e:
            print(f'  FALLA {etq}\n          {e}')
            fallos.append(nombre)
        except Exception as e:                               # noqa: BLE001
            print(f'  ERROR {etq}\n          {type(e).__name__}: {e}')
            fallos.append(nombre)

    print()
    if fallos:
        print(f'{len(fallos)} de {len(pruebas)} fallan')
        return 1
    print(f'las {len(pruebas)} pasan')
    return 0


if __name__ == '__main__':
    sys.exit(main())
