# eventos.py
"""Catalogo de perturbaciones para simular sobre el modelo del STN.

Por que existe este modulo
--------------------------
Antes solo se podia pedir "pierde N MW de generacion", todo en t=0. Un sistema
real no se cae asi. En el apagon iberico del 28 de abril de 2025 el disparo fue
LOCAL -un transformador en Granada- y a partir de ahi se fueron desconectando
en CASCADA parques fotovoltaicos, termosolares y eolicos en Badajoz, Segovia,
Huelva, Sevilla y Caceres, con oscilaciones y excursiones de tension por medio:
15.000 MW en cinco segundos, no de golpe.

Que un evento se reparta en el tiempo no es un detalle cosmetico. El EDAC actua
con retardos de 0,2 a 4 s, asi que el ORDEN y el INSTANTE de cada perdida
deciden que escalones llegan a disparar. Medido sobre este modelo, los mismos
3.000 MW dan nadir 58,14 Hz si caen de golpe y 58,43 Hz si caen repartidos en
cinco segundos, con un escalon menos y 450 MW menos de deslastre.

Como se usa
-----------
Cada evento devuelve dos cosas que hay que mantener coherentes:

  - la PERTURBACION para la dinamica de frecuencia: (t, dP_gen, dP_carga)
  - la MODIFICACION DE LA RED para el flujo AC posterior

`construir()` traduce una especificacion en las dos. La generacion detras del
contador entra siempre como `dP_carga`, nunca como `dP_gen`: el operador no la
ve desaparecer, la ve aparecer como mas demanda.

Tipos de evento
---------------
  gen            pierde generacion convencional (MW o unidades por nombre)
  btm            se desconecta generacion detras del contador
  solar          se desconecta solar de gran escala (inversores)
  eolica         se desconecta eolica
  linea          se abren lineas o transformadores
  interconexion  se pierde el enlace con Ecuador
  demanda        escalon de demanda

Escenarios predefinidos
-----------------------
  simple      una sola perdida de generacion
  btm         solo caida de BTM
  cascada     perdidas encadenadas, cada una unos segundos despues
  iberia      analogo del apagon iberico: disparo local y desconexion masiva
              de recursos basados en inversores en pocos segundos

Uso:
    from stn.eventos import ESCENARIOS, construir
    spec = ESCENARIOS['iberia'](escala=0.45)
    perturbaciones, cambios = construir(network, spec, mapa_areas)
"""

from dataclasses import dataclass, field


# Instantes tipicos de la cascada iberica, en segundos desde el disparo.
# El informe factual de ENTSO-E describe el colapso en unos cinco segundos.
IBERIA_T = (0.0, 0.5, 1.2, 2.0, 3.0, 4.2, 5.0)


@dataclass
class Evento:
    """Una perturbacion con su instante."""
    tipo: str
    t: float = 0.0
    mw: float = 0.0              # magnitud; para 'btm' se ignora si hay fraccion
    fraccion: float = 0.0        # parte del recurso que se pierde, 0-1
    areas: tuple = ()            # areas afectadas; vacio = todas
    elementos: tuple = ()        # nombres concretos (lineas, generadores)
    etiqueta: str = ''

    def descripcion(self):
        if self.etiqueta:
            return self.etiqueta
        if self.elementos:
            return f"{self.tipo}: {', '.join(self.elementos[:2])}"
        if self.fraccion:
            return f"{self.tipo}: {100*self.fraccion:.0f}%"
        return f"{self.tipo}: {self.mw:.0f} MW"


# ---------------------------------------------------------------------------
# Escenarios predefinidos
# ---------------------------------------------------------------------------

def _simple(mw=1500.0, **kw):
    return [Evento('gen', t=0.0, mw=mw, etiqueta=f'perdida de {mw:.0f} MW')]


def _btm(caida=0.8, areas=(), **kw):
    return [Evento('btm', t=0.0, fraccion=caida, areas=tuple(areas),
                   etiqueta=f'cae el {100*caida:.0f}% del BTM')]


def _cascada(mw=2400.0, pasos=4, intervalo=1.5, **kw):
    """Perdidas encadenadas: la primera arrastra a las siguientes."""
    cuota = mw / max(pasos, 1)
    return [Evento('gen', t=i * intervalo, mw=cuota,
                   etiqueta=f'cascada {i+1}/{pasos}: {cuota:.0f} MW')
            for i in range(pasos)]


def _iberia(escala=0.45, caida_btm=0.9, **kw):
    """Analogo del apagon iberico del 28 de abril de 2025.

    La secuencia real: un disparo local, oscilaciones y excursiones de tension,
    y a continuacion desconexion masiva de recursos basados en inversores
    -fotovoltaica de gran escala, termosolar, eolica y generacion distribuida-
    en cascada durante unos cinco segundos.

    `escala` es la fraccion de la generacion del sistema que se pierde en
    total. En Espana fueron 15 GW sobre una demanda del orden de 25 GW, o sea
    ~0,6. Se deja configurable porque el tamano relativo del evento es
    justamente lo que se quiere explorar, y porque el parque colombiano no
    tiene la penetracion de inversores del espanol: con los valores por
    defecto de hoy el sistema aguanta, y ese resultado tambien es informativo.

    El reparto entre tecnologias sigue el orden en que cedieron en Iberia:
    primero el disparo local, luego la solar de gran escala, despues la eolica
    y la generacion distribuida, y por ultimo el resto de inversores.
    """
    return [
        Evento('linea', t=IBERIA_T[0], elementos=(),
               etiqueta='disparo local que inicia la perturbacion'),
        Evento('gen', t=IBERIA_T[1], fraccion=0.08 * escala / 0.45,
               etiqueta='primera tanda de generacion convencional'),
        Evento('solar', t=IBERIA_T[2], fraccion=1.0,
               etiqueta='desconexion de solar de gran escala por tension'),
        Evento('eolica', t=IBERIA_T[3], fraccion=1.0,
               etiqueta='desconexion de eolica por tension'),
        Evento('btm', t=IBERIA_T[4], fraccion=caida_btm,
               etiqueta='desconexion de generacion distribuida (BTM)'),
        Evento('gen', t=IBERIA_T[5], fraccion=0.12 * escala / 0.45,
               etiqueta='segunda tanda por sobretension'),
        Evento('interconexion', t=IBERIA_T[6],
               etiqueta='se pierde el enlace con Ecuador'),
    ]


ESCENARIOS = {
    'simple': _simple,
    'btm': _btm,
    'cascada': _cascada,
    'iberia': _iberia,
}


# ---------------------------------------------------------------------------
# Traduccion a perturbaciones y cambios de red
# ---------------------------------------------------------------------------

def _generadores_de_tipo(network, sis, tipo):
    """Generadores del modelo cuya tecnologia es la pedida."""
    if sis is None or not hasattr(sis, 'gen'):
        return []
    g = sis.gen
    return [i for i in g.index
            if i in network.generators.index and g.at[i, 'tipo'] == tipo]


def construir(network, spec, mapa_areas=None, sis=None, btm=None,
              caido_btm=None):
    """Traduce una lista de Evento en perturbaciones y cambios de red.

    Devuelve (perturbaciones, cambios) donde

      perturbaciones = [{'t','dP_gen','dP_carga','etiqueta'}, ...]
                       para simular_frecuencia
      cambios        = {'generadores_fuera': [...],
                        'lineas_fuera': [...],
                        'carga_extra': Serie por barra,
                        'deficit_mw': float}
                       para aplicar_evento

    `btm` y `caido_btm` son Series por barra con el BTM instalado y el que se
    cae; las calcula deslastre_btm y se pasan aqui para no duplicar la logica.
    """
    import pandas as pd

    perturbaciones = []
    gen_fuera, lin_fuera = [], []
    carga_extra = pd.Series(dtype=float)
    deficit = 0.0

    p_set = network.generators.p_set

    for e in spec:
        dP_gen = dP_carga = 0.0

        if e.tipo == 'gen':
            if e.elementos:
                nom = [g for g in e.elementos if g in network.generators.index]
                dP_gen = float(p_set[nom].sum())
                gen_fuera.extend(nom)
            elif e.fraccion:
                dP_gen = float(p_set.sum()) * float(e.fraccion)
            else:
                dP_gen = float(e.mw)
            deficit += dP_gen

        elif e.tipo in ('solar', 'eolica'):
            nom = _generadores_de_tipo(network, sis, e.tipo)
            if nom:
                dP_gen = float(p_set[nom].sum()) * float(e.fraccion or 1.0)
                if (e.fraccion or 1.0) >= 0.999:
                    gen_fuera.extend(nom)
            deficit += dP_gen

        elif e.tipo == 'btm':
            if caido_btm is not None and len(caido_btm):
                sel = caido_btm
                if e.areas and mapa_areas is not None:
                    sel = sel[[b for b in sel.index
                               if mapa_areas.get(b, ('', ''))[1] in e.areas]]
                dP_carga = float(sel.sum())
                carga_extra = carga_extra.add(sel, fill_value=0.0)
            else:
                dP_carga = float(e.mw)

        elif e.tipo == 'linea':
            nom = [l for l in e.elementos if l in network.lines.index]
            lin_fuera.extend(nom)
            # abrir una linea no es de por si un desbalance de potencia: su
            # efecto lo recoge el flujo AC, no la ecuacion de oscilacion

        elif e.tipo == 'interconexion':
            # el enlace con Ecuador se modela como el generador de Pimampiro
            nom = [g for g in network.generators.index if 'PIMAMPIRO' in g]
            if nom:
                dP_gen = float(p_set[nom].sum())
                gen_fuera.extend(nom)
                deficit += dP_gen

        elif e.tipo == 'demanda':
            dP_carga = float(e.mw)

        else:
            raise ValueError(f"tipo de evento desconocido: '{e.tipo}'")

        if abs(dP_gen) > 1e-9 or abs(dP_carga) > 1e-9 or e.tipo == 'linea':
            perturbaciones.append({
                't': float(e.t), 'dP_gen': dP_gen, 'dP_carga': dP_carga,
                'etiqueta': e.descripcion(),
            })

    cambios = {
        'generadores_fuera': list(dict.fromkeys(gen_fuera)),
        'lineas_fuera': list(dict.fromkeys(lin_fuera)),
        'carga_extra': carga_extra,
        'deficit_mw': deficit,
    }
    return perturbaciones, cambios


def resumen(perturbaciones):
    """Texto de la secuencia, para el informe."""
    if not perturbaciones:
        return ["  (sin perturbaciones)"]
    out = []
    acum = 0.0
    for p in sorted(perturbaciones, key=lambda x: x['t']):
        d = p['dP_gen'] + p['dP_carga']
        acum += d
        detalle = ''
        if abs(p['dP_gen']) > 1e-9:
            detalle += f"gen -{p['dP_gen']:.0f} MW "
        if abs(p['dP_carga']) > 1e-9:
            detalle += f"carga +{p['dP_carga']:.0f} MW "
        out.append(f"  t={p['t']:5.2f} s  {detalle:26s} acumulado {acum:8.1f} MW"
                   f"   {p['etiqueta']}")
    return out


# ---------------------------------------------------------------------------
# Curva de disparo por subfrecuencia de los inversores
# ---------------------------------------------------------------------------

# raiz de la descarga de PARATEC, definida en un solo sitio
from stn.paratec_equipos import DIR_PARATEC

ARCHIVO_USOLAR = f'{DIR_PARATEC}/Generacion/PARATEC_Usolar_06-09-2026.xlsx'


def curva_disparo_solar(archivo=ARCHIVO_USOLAR, verbose=False):
    """MW de solar que se desconecta a cada umbral de frecuencia.

    No es un supuesto: PARATEC declara el ajuste de proteccion de frecuencia
    de cada una de las 370 plantas solares del pais, y `capacidad_plantas()`
    da su potencia. Las 370 emparejan por nombre, 4.200 MW en total.

    El reparto que sale es revelador:

        f < 60,00 Hz      5 plantas      52,7 MW
        f < 59,80 Hz     14 plantas      19,5 MW
        f < 57,50 Hz     85 plantas   2.614,9 MW   <-- el precipicio
        f < 57,00 Hz    252 plantas   1.461,9 MW

    Dos cosas que esto revela sobre el sistema colombiano:

    1. Hay 19 plantas cuyo ajuste esta en 59,8 Hz o mas. Disparan ante
       CUALQUIER hueco de frecuencia, antes incluso de que actue el primer
       escalon del EDAC, que esta en 59,4 Hz.
    2. A 57,50 Hz se van 2.615 MW de golpe. Ese umbral coincide con el de
       colapso del modelo, asi que quien llegue ahi se lleva por delante otros
       2,6 GW: es un precipicio, no una pendiente.

    Esta es la cascada endogena. No hay que programarla: emerge del propio
    transitorio, igual que en Iberia, donde la desconexion masiva de recursos
    basados en inversores fue consecuencia de la perturbacion, no su causa.

    Devuelve una lista lista para `simular_frecuencia(disparos=...)`.
    """
    import pandas as pd
    from stn.data_loader import parse_float
    from stn.paratec_equipos import capacidad_plantas

    d = pd.read_excel(archivo, sheet_name='Usolar', header=5,
                      dtype=str).dropna(how='all')
    d = d[d['Nombre'].notna()].copy()
    d['fmin'] = d['Frecuencia mínima [Hz]'].apply(parse_float)
    d['n'] = d['Nombre'].astype(str).str.upper().str.strip()

    cap = capacidad_plantas()
    solar = cap[cap.tipo == 'solar'].copy()
    solar['n'] = solar.planta.astype(str).str.upper().str.strip()

    m = d.merge(solar[['n', 'mw']], on='n', how='left').dropna(
        subset=['mw', 'fmin'])
    g = m.groupby('fmin').agg(mw=('mw', 'sum'), n=('mw', 'size'))

    out = []
    for fmin, r in g.iterrows():
        out.append({
            'f': float(fmin),
            'mw': float(r['mw']),
            # el inversor no se va en el instante exacto del cruce: las normas
            # de conexion dan una ventana corta de aguante
            'retardo': 0.1,
            'etiqueta': f"{int(r['n'])} plantas solares con ajuste {fmin:.2f} Hz",
        })
    if verbose:
        print(f"  curva de disparo solar: {len(out)} umbrales, "
              f"{sum(o['mw'] for o in out):.0f} MW en total")
    return sorted(out, key=lambda o: -o['f'])


def curva_disparo_btm(btm_total_mw, f_umbral=58.5, retardo=0.2):
    """Curva de disparo de la generacion distribuida.

    A diferencia de la solar de gran escala, el BTM domestico NO tiene ajustes
    declarados en ningun registro: son miles de inversores pequenos sin
    telemedida. Se modela con un unico umbral, y ese umbral es un SUPUESTO.

    58,5 Hz es una eleccion conservadora dentro de lo que permite la norma
    colombiana; los inversores domesticos suelen llevar ajustes mas estrechos
    que los de planta, porque priorizan la proteccion de la instalacion sobre
    el aguante de la red. Si dispones del dato real, cambialo.
    """
    if btm_total_mw <= 0:
        return []
    return [{'f': float(f_umbral), 'mw': float(btm_total_mw),
             'retardo': float(retardo),
             'etiqueta': f'generacion distribuida (BTM), ajuste supuesto '
                         f'{f_umbral:.2f} Hz'}]
