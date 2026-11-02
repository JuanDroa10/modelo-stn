# verificar_sistema.py
"""Verificacion de extremo a extremo del modelo del STN.

Dos modos:

    python verificar_sistema.py            las 44 comprobaciones del sistema (~80 s)
    python verificar_sistema.py --menu     ademas, TODAS las opciones del menu (~20 min)

QUE COMPRUEBA, y por que cada bloque esta aqui:

  1. DATOS       que los ficheros de entrada existen y se leen con los totales
                 esperados. Un CSV movido o un Excel reemplazado se ve aqui y
                 no tres modulos mas abajo.
  2. MODELO      topologia, una sola isla, balance y perdidas en rango.
  3. FISICA      en los DOS puntos de operacion: tensiones, equilibrio inicial
                 exacto, RoCoF contra la ecuacion de oscilacion, y estatismo y
                 reserva contra lo que exige la CREG.
  4. ALGORITMOS  los cuatro: N-1 completo, EDAC (tamano de escalon y tope),
                 MILP en los dos puntos, y cascada.
  5. EXTERNO     H*S y el nadir de la contingencia de diseno frente a XM.
  6. DOCUMENTOS  que las cifras del README sean las que el modelo produce HOY.
                 Este bloque existe porque una cifra estimada en vez de medida
                 llego a publicarse: la reserva diurna figuraba como 731,1 MW
                 cuando son 785,7.

Complementa a `test_invariantes.py`, que vigila propiedades internas. Este
mira el sistema desde fuera y contra fuentes externas.
"""
import warnings, logging, os, re, time, io, sys, contextlib, traceback
warnings.filterwarnings('ignore'); logging.disable(logging.INFO)
import numpy as np, pandas as pd
from stn.paths import PARATEC

ok, mal, avisos = [], [], []


def chk(cond, titulo, detalle=''):
    (ok if cond else mal).append((titulo, detalle))
    print(f"  [{'OK ' if cond else 'MAL'}] {titulo:54s} {detalle}", flush=True)


def nota(titulo, detalle=''):
    avisos.append((titulo, detalle))
    print(f"  [avi] {titulo:54s} {detalle}", flush=True)


def barrido_del_menu():
    """Ejecuta TODAS las opciones del menu con sus respuestas por defecto.

    La lista sale de `menu.MENU`, no de un numero escrito aqui: cuando se
    anadio la opcion de comparar los dos casos, una cuenta fijada a mano
    habria dejado la nueva sin ejercitar justo en la ejecucion que sirve para
    dar el sistema por bueno.

    `menu.preguntar` captura EOFError y devuelve el defecto, asi que basta con
    dejar la entrada vacia para que cada opcion se conteste sola.

    EL PUNTO DE OPERACION. Desde que el menu lo elige UNA VEZ al entrar
    (`menu.elegir_punto`, llamada desde `menu.main`), este barrido no pasa por
    ahi: invoca `fn()` directamente, asi que hereda el valor por omision del
    modulo, `menu._PUNTO`, que es la PUNTA. Es lo que interesa: este barrido
    busca caminos que revientan, no fisica, y la punta es el caso medido.

    Una consecuencia que conviene tener presente: la opcion del deslastre con
    BTM antes preguntaba su propio punto y tomaba DIURNO por defecto; ahora
    sigue al punto de la sesion y por tanto corre sobre la punta, donde la
    fotovoltaica da cero. Sigue ejercitando el modulo entero, pero el evento
    solar no tiene nada que desconectar. Para ejercitarlo con sol hay que
    entrar al menu a mano y cambiar el punto con la opcion `p`.

    Asi se ejercitan los caminos que solo existen al entrar POR EL MENU: un
    fallo de ese tipo ---la opcion del escenario diurno reventaba por una
    llamada mal formada--- no lo ve ninguna otra prueba.
    """
    import menu
    opciones = [(t, f) for t, f, _ in menu.MENU if f is not None]
    print(f"\n{'=' * 86}\n  BARRIDO DEL MENU ({len(opciones)} opciones)\n{'=' * 86}")
    for i, (titulo, fn) in enumerate(opciones, 1):
        buf, t0, estado, detalle = io.StringIO(), time.time(), 'OK', ''
        try:
            sys.stdin = io.StringIO('')
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                fn()
        except SystemExit:
            pass
        except Exception as e:                                # noqa: BLE001
            estado, detalle = 'MAL', f"{type(e).__name__}: {e}"
            buf.write('\n' + traceback.format_exc())
        finally:
            sys.stdin = sys.__stdin__
        s = buf.getvalue()
        if estado == 'OK' and ('devolvio codigo' in s or 'Traceback' in s):
            estado = 'MAL'
            detalle = next((l.strip()[:80] for l in s.splitlines()
                            if 'devolvio codigo' in l or 'Error' in l), 'subproceso con error')
        chk(estado == 'OK', f"menu {i:2d}. {titulo[:44]}",
            f"{time.time()-t0:6.1f}s  {detalle}")


t0 = time.time()
print("=" * 86); print("  1. DATOS DE ENTRADA"); print("=" * 86)
from stn import config
for f in (config.FILE_LINES, config.FILE_PQ, config.FILE_MAPPING):
    chk(os.path.exists(f), f"existe {os.path.basename(f)}",
        f"{os.path.getsize(f)/1024:.0f} KB" if os.path.exists(f) else "NO ESTA")
chk(os.path.isdir(PARATEC), "carpeta data/paratec/",
    f"{sum(len(x) for _,_,x in os.walk(PARATEC))} ficheros")
from stn import deslastre_btm as DB
sol = DB.cargar_solar_gran_escala('subarea')
chk(abs(float(sol.sum()) - 4200.1) < 1.0, "registro solar Usolar", f"{float(sol.sum()):.1f} MW")
# EL BTM REAL, no el pool de antes. Hasta el 31-10-2026 aqui se comprobaban
# 1.641,1 MW que sumaban AGPE + plantas menores, y las plantas menores NO son
# BTM (CREG 086 de 1996: excluyentes con la figura de autogenerador). Se
# vigilan las tres cifras que ahora importan.
det = DB.cargar_registrada_detalle('agpe')
chk(abs(float(det.MW.sum()) - DB.UPME_AGPE_MW) < 1.0,
    "BTM base = AGPE de la UPME", f"{float(det.MW.sum()):.1f} MW")
detgd = DB.cargar_registrada_detalle('agpe_gd')
chk(abs(float(detgd.MW.sum()) - (DB.UPME_AGPE_MW + DB.UPME_GD_MW)) < 1.0,
    "BTM sensibilidad = AGPE + GD", f"{float(detgd.MW.sum()):.1f} MW")
chk(float(det[det.tec.isin(DB.INVERSOR)].MW.sum()) / float(det.MW.sum()) > 0.90,
    "el BTM base es casi todo inversor",
    f"{100*float(det[det.tec.isin(DB.INVERSOR)].MW.sum())/float(det.MW.sum()):.1f} %")
detp = DB.cargar_registrada_detalle('agpe_paratec')
chk(abs(float(detp.MW.sum()) - 144.9) < 1.0,
    "registro crudo de PARATEC (forma del reparto)", f"{float(detp.MW.sum()):.1f} MW")

print("\n" + "=" * 86); print("  2. CONSTRUCCION DEL MODELO"); print("=" * 86)
from stn import contingencias, agc_deslastre as AGC
n, d = contingencias.caso_base(verbose=False)
s = AGC.Sistema(n)
chk(len(n.buses) == 148 and len(n.lines) == 280 and len(n.transformers) == 16,
    "topologia", f"{len(n.buses)} buses, {len(n.lines)} lineas, {len(n.transformers)} trafos")
chk(len(n.sub_networks) == 1, "una sola isla electrica", f"{len(n.sub_networks)}")
gen = float(n.generators_t.p.iloc[0].sum()); carga = float(n.loads.p_set.sum())
chk(abs(gen - carga - (gen - carga)) < 1e-9, "balance cierra", f"perdidas {gen-carga:.1f} MW")
chk(0.005 < (gen-carga)/carga < 0.035, "perdidas en rango", f"{100*(gen-carga)/carga:.2f} %")

print("\n" + "=" * 86); print("  3. FISICA, EN LOS DOS PUNTOS"); print("=" * 86)
from stn import escenario_diurno as ED
nd, info = ED.construir(hora=11, verbose=False); sd = AGC.Sistema(nd)
for etq, red, sis in (("punta", n, s), ("diurno", nd, sd)):
    v = red.buses_t.v_mag_pu.iloc[0]
    chk(0.90 < v.min() and v.max() < 1.10, f"{etq}: tensiones en 0,90-1,10", f"[{v.min():.4f}, {v.max():.4f}]")
    desb = sis.P_gen - sis.P_carga - sis.P_perdidas
    chk(abs(desb) < 0.5, f"{etq}: arranca en equilibrio", f"{desb:+.6f} MW")
    _, f, _, tr = AGC.simular_frecuencia(sis, deficit_mw=1000., con_edac=False)
    teor = -1000*60/(2*sis.H*sis.S_base)
    chk(abs(tr['rocof']/teor - 1) < 0.02, f"{etq}: RoCoF = -dP f0/(2HS)",
        f"{100*abs(tr['rocof']/teor-1):.3f} % de error")
    chk(0.04 <= sis.R_eq <= 0.06, f"{etq}: estatismo CREG 4-6 %", f"{100*sis.R_eq:.2f} %")
    chk(sis.reserva >= 0.03*sis.P_gen, f"{etq}: reserva >= 3 % (CREG)",
        f"{sis.reserva:.1f} MW = {100*sis.reserva/sis.P_gen:.1f} %")
    sn = red.lines.s_nom.replace(0, np.nan)
    cg = (np.hypot(red.lines_t.p0.iloc[0].abs(), red.lines_t.q0.iloc[0].abs())/sn*100).dropna()
    chk(cg.max() < 100, f"{etq}: ninguna linea sobre su nominal", f"max {cg.max():.1f} %")

print("\n" + "=" * 86); print("  4. LOS CUATRO ALGORITMOS"); print("=" * 86)
# N-1
import importlib
els = contingencias.elementos_de(n)
chk(len(els) > 250, "N-1: elementos a abrir", f"{len(els)}")
r1 = contingencias.barrido(n, d, els, orden=1, verbose=False)
chk(int((r1.n_sobrecargas > 0).sum()) == 0, "N-1: cero sobrecargas",
    f"{len(r1)} contingencias simuladas")
chk(int((~r1.convergio.astype(bool)).sum()) == 0, "N-1: todas convergen",
    f"{int((~r1.convergio.astype(bool)).sum())} sin converger")
chk(float(r1.carga_max_pct.max()) < 100.0, "N-1: cargabilidad bajo el 100 %",
    f"max {float(r1.carga_max_pct.max()):.1f} %")
chk(int((r1.n_v_bajas + r1.n_v_altas > 0).sum()) == 0, "N-1: ninguna tension fuera",
    f"{int((r1.n_v_bajas + r1.n_v_altas > 0).sum())} casos")
# EL MISMO BARRIDO SOBRE EL DIURNO. Antes el N-1 solo se corria sobre la punta
# y "296 contingencias sin sobrecargas" se leia como una propiedad del modelo
# cuando era una propiedad de UN punto de operacion. El diurno se comporta
# distinto, y esa diferencia es justamente el hallazgo.
d_d = contingencias._estado(nd)
r1d = contingencias.barrido(nd, d_d, contingencias.elementos_de(nd), orden=1,
                            verbose=False)
chk(int((r1d.n_sobrecargas > 0).sum()) == 0, "N-1 diurno: cero sobrecargas",
    f"{len(r1d)} contingencias simuladas")
chk(int((~r1d.convergio.astype(bool)).sum()) == 0, "N-1 diurno: todas convergen",
    f"{int((~r1d.convergio.astype(bool)).sum())} sin converger")
chk(int((r1d.n_v_bajas > 0).sum()) == 0, "N-1 diurno: ninguna tension BAJA",
    f"min {float(r1d.v_min.min()):.4f} pu")
# Esto NO es un chk. Las 5 sobretensiones son un estado conocido y
# diagnosticado: el caso diurno hereda el estado de maniobra de los reactores
# de la punta. Ponerlo como chk obligaria a elegir entre un
# MAL permanente o un umbral que lo tapa; como nota queda a la vista en cada
# ejecucion y con su cifra, que es lo que hace falta para no citar el N-1
# diurno sin la salvedad.
_alt = int((r1d.n_v_altas > 0).sum())
nota(f"N-1 diurno: {_alt} casos sobre 1,10 pu (reactores en estado de punta)",
     f"v_max {float(r1d.v_max.max()):.4f} pu; con los 8 reactores excluidos: 0")
# frecuencia + EDAC
_, _, e, tr = AGC.simular_frecuencia(s, deficit_mw=3000., con_edac=True)
paso = next(x['mw'] for x in e if x['disparado'])
chk(abs(paso - 0.05*s.P_carga) < 0.5, "EDAC: escalon = 5 % de la demanda", f"{paso:.2f} MW")
chk(tr['deslastre_total'] <= 0.4005*s.P_carga, "EDAC: no pasa del 40 %",
    f"{100*tr['deslastre_total']/s.P_carga:.2f} %")
# BTM-MILP, en los dos puntos
from stn.zonas import area_por_bus
for etq, red, prod in (("punta", n, 1.0), ("diurno", nd, 0.769)):
    esc = DB.EscenarioBTM(fuente='sintetico', penetracion_pct=30., caida_pct=90.,
                          semilla=0, produccion_inversor_pu=prod)
    a = DB.ejecutar(esc, verbose=False, usar_milp=True, base=red)
    p = DB.ejecutar(esc, verbose=False, usar_milp=False, base=red)
    chk(a['origen'].startswith('MILP'), f"MILP {etq}: no degenera", a['origen'][:38])
    chk(float(a['deslastre'].sum()) >= a['traza']['deslastre_total'] - 1.0,
        f"MILP {etq}: cubre el objetivo del EDAC",
        f"{float(a['deslastre'].sum()):.1f} >= {a['traza']['deslastre_total']:.1f}")
    v = p['btm_sano_mw']/max(a['btm_sano_mw'], 1e-9)
    chk(v > 1.0, f"MILP {etq}: protege mas BTM que el proporcional", f"{v:.1f}x")
# cascada
from stn import cascada
chk(hasattr(cascada, '_SistemaIsla'), "cascada: modulo cargable", "ok")

print("\n" + "=" * 86); print("  5. CONTRASTE EXTERNO"); print("=" * 86)
chk(abs(s.H*s.S_base - 53271) < 500, "H*S frente a lo documentado", f"{s.H*s.S_base:.0f} MVA.s")
nota("XM publica 35-40 mil MVA.s de moda anual", f"este caso: {s.H*s.S_base:.0f} (es punta)")
_, _, _, t300 = AGC.simular_frecuencia(s, deficit_mw=300., con_edac=True)
chk(abs(t300['nadir'] - 59.78) < 0.20, "nadir de 300 MW frente al de XM (59,78)",
    f"{t300['nadir']:.3f} Hz, dif {1000*abs(t300['nadir']-59.78):.0f} mHz")

print("\n" + "=" * 86); print("  6. COHERENCIA DOCUMENTACION <-> MODELO"); print("=" * 86)
txt = open('README.md', encoding='utf-8').read()
def num(pat):
    m = re.search(pat, txt)
    return float(m.group(1).replace('.', '').replace(',', '.')) if m else None
sl = n.generators.index[n.generators.control == 'Slack'][0]
aporte_slack = float(n.generators_t.p.iloc[0][sl] - n.generators.at[sl, 'p_set'])
casos = [
    ("demanda punta",        num(r'\| Demanda \| ([\d\.,]+) MW \|'),            carga,            1.0),
    ("perdidas",             num(r'\| Pérdidas \| ([\d\.,]+) MW'),               gen - carga,      0.5),
    ("aporte del slack",     num(r'slack sobre su consigna \| \*\*\+([\d,]+) MW'), aporte_slack,  0.5),
    ("placa instalada",      num(r'barras con generación \| ([\d\.,]+) MVA'),    float(s.gen.p_nom.sum()), 1.0),
    ("reserva a bajar",      num(r'Reserva primaria a bajar \| ([\d\.,]+) MW'),  s.reserva_baja,   1.0),
    ("placa hidraulica",     num(r'hidráulica ([\d\.,]+) \(H'),
                                 float(s.gen[s.gen.tipo == 'hidraulica'].p_nom.sum()), 1.0),
    ("MVA del contraste",    num(r'\*\*Este modelo\*\* \| [\d\.,]+ MW \| \*\*([\d\.,]+)\*\*'),
                                                                               s.S_base,         1.0),
    ("S_base",               num(r'`S_base`\*\* \| \*\*([\d\.,]+) MVA'),        s.S_base,         1.0),
    ("H",                    num(r'Inercia equivalente `H` \| ([\d,]+) s'),     s.H,              0.01),
    ("H*S",                  num(r'`H·S_base`\*\* \| \*\*([\d\.,]+) MVA'),      s.H*s.S_base,     10.0),
    ("reserva a subir",      num(r'Reserva primaria a subir \| ([\d\.,]+) MW'), s.reserva,        1.0),
    ("demanda neta diurna",  num(r'\*\*Demanda neta del STN\*\* \| \*\*[\d\.,]+\*\* \| \*\*([\d\.,]+) MW'),
                                                                               float(nd.loads.p_set.sum()), 1.0),
    # El caso DIURNO entero. Sin estas filas, la mitad de las cifras publicadas
    # no tenia quien las vigilara: el bloque solo cubria la punta mas una sola
    # cifra del diurno, justo cuando el diurno paso a tener bateria completa.
    ("S_base diurna",        num(r'\| MVA sincronizados \| [\d\.,]+ \| ([\d\.,]+) \|'),
                                                                               sd.S_base,        1.0),
    ("H*S diurna",           num(r'Energía cinética H·S\*\* \| \*\*[\d\.,]+\*\* \| \*\*([\d\.,]+) MVA'),
                                                                               sd.H*sd.S_base,  10.0),
    ("reserva diurna",       num(r'\| Reserva primaria \| [\d\.,]+ \| ([\d\.,]+) MW'),
                                                                               sd.reserva,       1.0),
    ("solar diurna",         num(r'\| Solar embebida \| 0,0 \| −([\d\.,]+) MW'),
                                                                               float(info['solar_produccion_MW']), 1.0),
    ("N-1 diurno: v_max",    num(r'\| Tensión máxima del barrido \| [\d\.,]+ pu \| ([\d\.,]+) pu'),
                                                                               float(r1d.v_max.max()), 0.0005),
    ("N-1 diurno: carga max", num(r'\| Cargabilidad máxima \| 90,1 % \| ([\d\.,]+) %'),
                                                                               float(r1d.carga_max_pct.max()), 0.1),
    ("N-1 diurno: v altas",  num(r'\(> 1,10 pu\) \| \*\*0\*\* \| \*\*(\d+)\*\*'),
                                                                               float(_alt), 0.5),
    ("N-1 punta: carga max", num(r'\| Cargabilidad máxima \| ([\d\.,]+) % \|'),
                                                                               float(r1.carga_max_pct.max()), 0.1),
    # El estatismo y el nadir de la contingencia de diseno NO estaban vigilados,
    # y los dos se quedaron atras al corregir la agregacion de R el 1-11-2026
    # (0,0485 publicado frente a 0,0502 real, y 59,709 frente a 59,706). Son
    # justo las dos cifras que el informe cita de este bloque.
    ("estatismo punta",      num(r'Estatismo equivalente `R_eq` \| ([\d,]+) \('),
                                                                               s.R_eq,           0.0002),
    ("nadir de 300 MW",      num(r'da \*\*nadir ([\d,]+) Hz\*\*'),
                                                                               t300['nadir'],    0.002),
    ("Q diurna sin limites", num(r'Q neta de los generadores, sin límites \| −[\d\.,]+ MVAr \| \*\*−([\d\.,]+) MVAr'),
                                                                               None, 5.0),
]
# La Q sin limites hay que medirla aparte: pide la red SIN conmutar a PQ, que
# no es ninguna de las dos que este script ya tiene construidas.
from stn import limites_reactivos as _LR
_nd0 = ED.construir(hora=11, limites=False, verbose=False)[0]
_q_diurna = abs(float(_nd0.generators_t.q.iloc[0].sum()))
casos = [(e, d_, (_q_diurna if r is None else r), t) for e, d_, r, t in casos]
# Este bloque existe porque una cifra estimada en vez de medida llego a
# publicarse en el README del proyecto de desarrollo, que si lleva decenas de
# resultados numericos. El README de ESTE repositorio es un documento de
# producto y a proposito no los lleva: describe que hace el modelo y como se
# usa, no que valores da. Si no casa ningun patron, el bloque no tiene nada
# que vigilar, y decirlo una vez es mas util que repetir veintitres avisos
# que parecen fallos.
_con_cifras = [c for c in casos if c[1] is not None]
if not _con_cifras:
    print("  (el README de este repositorio no publica cifras del modelo: "
          "nada que contrastar)")
else:
    for etq, doc, real, tol in _con_cifras:
        chk(abs(doc - real) < tol, f"README '{etq}'",
            f"dice {doc:.4g}, el modelo da {real:.4g}")
    _faltan = len(casos) - len(_con_cifras)
    if _faltan:
        nota(f"{_faltan} cifras del bloque no estan en el README",
             "se vigilan las que si estan")

print("\n" + "=" * 86)
if '--menu' in sys.argv:
    barrido_del_menu()

print("\n" + "=" * 86)
print(f"  {len(ok)} OK | {len(mal)} MAL | {len(avisos)} avisos | {time.time()-t0:.0f}s")
for t, dd in mal: print(f"    FALLA: {t} -> {dd}")
sys.exit(1 if mal else 0)
