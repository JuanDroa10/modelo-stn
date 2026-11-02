"""Descarga de XM el despacho y el pronostico de demanda de una fecha.

POR QUE EXISTE ESTE FICHERO
---------------------------
El modelo sabia leer snapshots de XM, pero no conseguirlos: habia que bajarlos
con `sinsim`, que es otro programa, exige Python 3.12 exacto
(`requires-python = ">=3.12,<3.13"`) y arrastra pandapower. Quien clonaba el
repositorio se encontraba con un menu que solo ofrecia las fechas que venian
dentro, sin ninguna forma de pedir otra.

Aqui se descarga cualquier fecha sin salir del entorno del modelo y sin
dependencias nuevas: solo `urllib` de la libreria estandar y el `pandas` que
ya estaba.

QUE ES DE LA FECHA Y QUE NO
---------------------------
Un snapshot tiene nueve tablas, pero solo DOS dependen de la fecha:

    dispatch   <- dDEC<MMDD>.txt         despacho programado del dia
    demand     <- PRON_BARRA<MMDD>.txt   pronostico de demanda por barra

Las otras siete salen de PARATEC, que es el REGISTRO del sistema y no tiene
parametro de fecha: su API devuelve siempre el estado de hoy. Pedirle el
2026-01-15 y pedirle el 2026-11-01 da exactamente la misma respuesta, de modo
que descargarla otra vez por cada fecha no añade informacion.

Asi que esas siete se heredan del snapshot mas reciente que haya en disco, y
el manifiesto deja escrito de cual. El modelo solo usa tres de ellas y solo
como DIRECTORIO ---`buses` para saber en que subestacion esta cada barra,
`lines` para el grafo que sube de STR a STN, `generators` para situar cada
recurso del despacho---; nunca lee de ahi ni el estado operativo ni un
parametro electrico, que vienen de `data/red/` y de `data/paratec/`.

Lo que esa herencia cuesta, medido entre los snapshots que trae el
repositorio: una fila cada tres o cuatro dias, y siempre generacion
distribuida pequeña dandose de alta. El unico cambio que aparecio en `lines`
entre el 20 y el 21 de octubre fue un lazo de JAMONDINO 230 consigo misma,
que el grafo descarta de todas formas. Un recurso que el despacho nombre y el
directorio heredado no conozca NO se cuela en silencio: cae en «sin ubicar» y
baja la cobertura, que es lo que el menu enseña antes de dejar seguir.

EL PORTAL DE XM
---------------
La biblioteca publica consume una API no documentada, que puede cambiar sin
aviso:

    listar     GET {BASE}?ruta=<carpeta>/<AAAA-MM>&contenedor=storageportalxm
                          &nombre=<prefijo>
    descargar  GET {BASE}/descarga-archivo?ruta=<carpeta>/<AAAA-MM>/<fichero>
                          &nombreBlobContainer=storageportalxm

El despacho se publica el dia ANTERIOR por la tarde, asi que la fecha mas
nueva que se puede pedir es mañana, y solo despues de que XM lo haya colgado.

COMO SE USA
-----------
    python -m stn.fuentes.descarga_xm --fecha 2026-11-01
    python -m stn.fuentes.descarga_xm --listar

o la opcion 3 del menu, que lo ofrece cuando la fecha que se escribe no esta.
"""

import argparse
import csv
import datetime as _dt
import io
import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from stn.paths import XM

# ---------------------------------------------------------------------------
# El portal de ficheros publicos
# ---------------------------------------------------------------------------

BASE_PORTAL = 'https://api-portalxm.xm.com.co/administracion-archivos/ficheros'
CONTENEDOR = 'storageportalxm'
DIR_DESPACHO = '/M:/InformacionAgentes/Usuarios/Publico/DESPACHO'
DIR_DEMANDA = '/M:/InformacionAgentes/Usuarios/Publico/DEMANDAS/Pronostico Oficial'

AGENTE = 'modelo-stn/1.0 (+https://github.com/; estudio academico del STN)'
ESPERA_S = 0.5          # entre peticiones; el portal es de XM, no nuestro
TIEMPO_MAX_S = 60

PERIODOS = 24
TOLERANCIA_TOTAL_MW = 0.01

# El nombre del despacho lleva version: dDEC1101.txt es la V0 y dDEC1101_V2.txt
# la segunda redeclaracion. Manda la mayor.
_RE_DDEC = re.compile(r'^dDEC(?P<mmdd>\d{4})(?:_V(?P<v>\d+))?\.txt$', re.I)

COLUMNAS_DESPACHO = ['resource', 'period', 'p_mw', 'source_id']
COLUMNAS_DEMANDA = ['ucp', 'node', 'vn_kv', 'period',
                    'p_act_mw', 'p_pstr_mw', 'q_mvar', 'source_id']

# Las tres etiquetas de PRON_BARRA, con y sin punto final.
_CONCEPTOS = {'ener.act': 'p_act_mw',
              'ener.reac': 'q_mvar',
              'perd.pstr': 'p_pstr_mw'}

ATRIBUCION = ('Informacion publica de XM S.A. E.S.P. (PARATEC, dDEC, '
              'PRON_BARRA). Describe un programa de despacho, no el estado '
              'real del SIN.')


class ErrorDescarga(Exception):
    """Algo impidio traer la fecha. El mensaje dice que hacer."""


_ultima = [0.0]


def _esperar():
    falta = ESPERA_S - (time.monotonic() - _ultima[0])
    if falta > 0:
        time.sleep(falta)
    _ultima[0] = time.monotonic()


def _get(url, params=None):
    """GET al portal, con el mensaje util cuando falla el certificado.

    El fallo de TLS merece trato aparte porque es el que se encuentra todo el
    mundo detras de un proxy corporativo, y el error de Python
    ---CERTIFICATE_VERIFY_FAILED--- no dice en ninguna linea que la raiz de la
    empresa no esta en el almacen de certificados de Python.
    """
    _esperar()
    if params:
        url = url + '?' + urllib.parse.urlencode(params)
    pet = urllib.request.Request(
        url, headers={'User-Agent': AGENTE,
                      'Accept': 'application/json, text/plain, */*'})
    try:
        with urllib.request.urlopen(pet, timeout=TIEMPO_MAX_S) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ErrorDescarga(f'el portal de XM no tiene {url} (404)') from e
        raise ErrorDescarga(f'el portal de XM respondio {e.code} a {url}') from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            raise ErrorDescarga(
                'el certificado de api-portalxm.xm.com.co no se pudo '
                'verificar.\n'
                'Si estas en la red de una empresa, el trafico pasa por un '
                'proxy que firma con una raiz propia que Python no conoce.\n'
                'Apunta SSL_CERT_FILE al almacen que SI la tiene, por '
                'ejemplo:\n'
                '    macOS   export SSL_CERT_FILE=/opt/homebrew/etc/'
                'ca-certificates/cert.pem\n'
                '    Windows set SSL_CERT_FILE=C:\\ruta\\a\\cacert.pem') from e
        raise ErrorDescarga(f'no se pudo conectar con el portal de XM: '
                            f'{e.reason}') from e


def _params_listado(carpeta, mes, prefijo):
    return {'ruta': f'{carpeta}/{mes}', 'contenedor': CONTENEDOR,
            'ordenarPor': 'nombre', 'orden': 'DESC', 'pagina': '1',
            'resultadosPorPagina': '50', 'nombre': prefijo}


def listar(carpeta, mes, prefijo):
    """Ficheros de una carpeta y mes que empiezan por `prefijo`.

    Devuelve [(nombre, fecha de modificacion o None)].
    """
    crudo = _get(BASE_PORTAL, _params_listado(carpeta, mes, prefijo))
    try:
        doc = json.loads(crudo.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ErrorDescarga('el listado del portal de XM no es JSON: la API '
                            'habra cambiado') from e
    items = doc if isinstance(doc, list) else None
    if items is None and isinstance(doc, dict):
        for v in doc.values():
            if isinstance(v, list):
                items = v
                break
    if items is None:
        raise ErrorDescarga('el listado del portal de XM no trae ninguna '
                            'lista: la API habra cambiado')
    salida, vistos = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        nombre = next((str(it[k]) for k in ('nombre', 'name', 'Nombre', 'Name')
                       if k in it), '').strip()
        if not nombre or nombre in vistos:
            continue
        if not nombre.upper().startswith(prefijo.upper()):
            continue
        vistos.add(nombre)
        mod = next((it[k] for k in ('fechaModificacion', 'FechaModificacion',
                                    'modified', 'lastModified') if k in it),
                   None)
        salida.append((nombre, str(mod) if mod else None))
    return salida


def bajar_fichero(carpeta, mes, nombre):
    """Un fichero del portal, en bytes."""
    crudo = _get(BASE_PORTAL + '/descarga-archivo',
                 {'ruta': f"{carpeta.lstrip('/')}/{mes}/{nombre}",
                  'nombreBlobContainer': CONTENEDOR})
    if not crudo.strip():
        raise ErrorDescarga(f'{nombre}: el portal devolvio un fichero vacio')
    return crudo


def _texto(crudo):
    try:
        return crudo.decode('utf-8')
    except UnicodeDecodeError:
        return crudo.decode('latin-1')


# ---------------------------------------------------------------------------
# Los dos formatos de XM
# ---------------------------------------------------------------------------

def version_ddec(nombre):
    """Version de un `dDEC<MMDD>[_V<n>].txt`; 0 si no lleva sufijo, None si
    el nombre no es un despacho."""
    m = _RE_DDEC.match(nombre)
    return int(m.group('v') or 0) if m else None


def _campos(linea):
    """Parte una linea respetando las comillas. El separador es ';' o ','
    segun cual abunde, porque XM ha usado los dos."""
    sep = ';' if linea.count(';') >= linea.count(',') else ','
    return next(csv.reader(io.StringIO(linea), delimiter=sep,
                           skipinitialspace=True))


def leer_ddec(texto, fuente='dDEC'):
    """`dDEC` -> tabla `dispatch`, comprobando la fila Total.

    El estandar (XM-CND-2014-045) da ENERGIA del periodo en MWh y los periodos
    duran una hora, asi que el numero es ya la potencia media en MW.

    La fila `Total` no es decoracion: es la suma que declara XM. Si no cuadra
    con la de los recursos, el fichero llego mal y vale mas no construir nada
    que construir un despacho incompleto.
    """
    filas, totales, sumas = [], None, [0.0] * PERIODOS
    for n, cruda in enumerate(texto.splitlines(), start=1):
        if not cruda.strip():
            continue
        campos = _campos(cruda)
        nombre = campos[0].strip().strip('"')
        valores = campos[1:]
        if len(valores) != PERIODOS:
            raise ErrorDescarga(
                f'{fuente} linea {n}: se esperaban {PERIODOS} valores y hay '
                f'{len(valores)}; el formato de XM habra cambiado')
        try:
            nums = [float(v.strip()) for v in valores]
        except ValueError as e:
            raise ErrorDescarga(f'{fuente} linea {n}: valor no numerico') from e
        if nombre.lower() == 'total':
            totales = nums
            continue
        for periodo, mwh in enumerate(nums, start=1):
            sumas[periodo - 1] += mwh
            filas.append({'resource': nombre, 'period': periodo,
                          'p_mw': mwh, 'source_id': f'{fuente}:{n}'})
    if totales is None:
        raise ErrorDescarga(f'{fuente}: falta la fila Total')
    for periodo, (s, t) in enumerate(zip(sumas, totales), start=1):
        if abs(s - t) > TOLERANCIA_TOTAL_MW:
            raise ErrorDescarga(
                f'{fuente} periodo {periodo}: la suma de los recursos '
                f'({s:.4f} MW) no cuadra con el Total que declara XM '
                f'({t:.4f} MW)')
    return pd.DataFrame(filas, columns=COLUMNAS_DESPACHO)


def leer_pron_barra(texto, fuente='PRON_BARRA'):
    """`PRON_BARRA` -> tabla `demand`.

    Formato: `UCP, NODO, kV, CONCEPTO, HORA01..HORA24`, con tres conceptos por
    nodo. La activa del caso es `Ener.Act. + Perd.PSTR`, pero aqui se guardan
    separadas, como hace el snapshot, y quien las suma es el modelo.
    """
    nodos = {}
    for n, cruda in enumerate(texto.splitlines(), start=1):
        if not cruda.strip():
            continue
        campos = _campos(cruda)
        if len(campos) != 4 + PERIODOS:
            raise ErrorDescarga(
                f'{fuente} linea {n}: se esperaban {4 + PERIODOS} campos y '
                f'hay {len(campos)}; el formato de XM habra cambiado')
        ucp, nodo, kv_txt, concepto = (c.strip() for c in campos[:4])
        col = _CONCEPTOS.get(concepto.strip().rstrip('.').lower())
        if col is None:
            raise ErrorDescarga(f'{fuente} linea {n}: concepto desconocido '
                                f'{concepto!r}')
        try:
            kv = float(kv_txt.replace(',', '.'))
            valores = [float(v) for v in campos[4:]]
        except ValueError as e:
            raise ErrorDescarga(f'{fuente} linea {n}: valor no numerico') from e
        e = nodos.setdefault((ucp, nodo, kv), {'ids': []})
        e[col] = valores
        e['ids'].append(f'{fuente}:{n}')

    filas = []
    for (ucp, nodo, kv), e in nodos.items():
        faltan = [c for c in _CONCEPTOS.values() if c not in e]
        if faltan:
            raise ErrorDescarga(f'{fuente}: el nodo {nodo} ({kv:g} kV) no trae '
                                f'{", ".join(faltan)}')
        ids = ';'.join(e['ids'])
        for periodo in range(1, PERIODOS + 1):
            i = periodo - 1
            filas.append({'ucp': ucp, 'node': nodo, 'vn_kv': kv,
                          'period': periodo,
                          'p_act_mw': e['p_act_mw'][i],
                          'p_pstr_mw': e['p_pstr_mw'][i],
                          'q_mvar': e['q_mvar'][i],
                          'source_id': ids})
    return pd.DataFrame(filas, columns=COLUMNAS_DEMANDA)


# ---------------------------------------------------------------------------
# El snapshot
# ---------------------------------------------------------------------------

HEREDADAS = ('buses', 'lines', 'trafos2w', 'trafos3w', 'generators',
             'shunts', 'svc_statcom')


def _raiz(raiz=None):
    return str(raiz) if raiz else str(XM)


def fechas_en_disco(raiz=None):
    """Las fechas que ya tienen snapshot, ordenadas."""
    d = os.path.join(_raiz(raiz), 'paratec')
    if not os.path.isdir(d):
        return []
    return sorted(x for x in os.listdir(d)
                  if os.path.isdir(os.path.join(d, x, 'normalized')))


def _donante(raiz, excepto=None):
    """El snapshot mas reciente del que heredar el directorio de PARATEC."""
    for f in reversed(fechas_en_disco(raiz)):
        if f == excepto:
            continue
        base = os.path.join(_raiz(raiz), 'paratec', f, 'normalized')
        if all(os.path.exists(os.path.join(base, f'{t}.parquet'))
               for t in ('buses', 'lines', 'generators')):
            return f
    return None


def comprobar_fecha(texto):
    """'AAAA-MM-DD' -> date, con los dos errores que de verdad ocurren."""
    try:
        f = _dt.date.fromisoformat(str(texto).strip())
    except ValueError as e:
        raise ErrorDescarga(f'{texto!r} no es una fecha AAAA-MM-DD') from e
    limite = _dt.date.today() + _dt.timedelta(days=1)
    if f > limite:
        raise ErrorDescarga(
            f'{f} es posterior a mañana ({limite}). XM publica el despacho de '
            f'un dia la tarde anterior, asi que todavia no existe.')
    return f


def descargar_fecha(fecha, raiz=None, forzar=False, verbose=True):
    """Trae una fecha de XM y deja el snapshot listo para usar.

    Escribe primero en `.tmp-<fecha>/` y solo renombra al final: si algo falla
    a mitad ---la red, el formato, el disco--- no queda un snapshot a medias
    que el modelo leeria como si estuviera completo.

    Devuelve la ruta del snapshot.
    """
    dia = comprobar_fecha(fecha)
    raiz_s = _raiz(raiz)
    final = os.path.join(raiz_s, 'paratec', dia.isoformat())
    if os.path.isdir(final) and not forzar:
        raise ErrorDescarga(f'{dia} ya esta descargada en {final}. '
                            f'Usa --forzar para volver a bajarla.')

    donante = _donante(raiz_s, excepto=dia.isoformat() if forzar else None)
    if donante is None:
        raise ErrorDescarga(
            'no hay ningun snapshot en disco del que heredar el directorio '
            'de PARATEC (buses, lines, generators), asi que no se puede '
            'construir uno nuevo. El repositorio trae cuatro fechas; si se '
            f'han borrado, recuperalas o apunta SINSIM_DATA a una carpeta '
            f'que las tenga. Buscado en {raiz_s}/paratec.')

    mes, mmdd = dia.strftime('%Y-%m'), dia.strftime('%m%d')
    di = (lambda t: print(f'   {t}')) if verbose else (lambda t: None)

    # --- despacho: todas las versiones del dia, manda la mayor -------------
    di(f'buscando dDEC{mmdd}* en el portal de XM...')
    cand = [(version_ddec(n), n, m) for n, m in
            listar(DIR_DESPACHO, mes, f'dDEC{mmdd}')]
    cand = [c for c in cand if c[0] is not None]
    if not cand:
        raise ErrorDescarga(
            f'XM no tiene dDEC{mmdd}*.txt en {DIR_DESPACHO}/{mes}. '
            f'O la fecha no se ha publicado aun, o es tan antigua que ya no '
            f'esta en la biblioteca publica.')
    cand.sort()
    v_usada, nombre_ddec, mod_ddec = cand[-1]
    disponibles = [f'V{v}' for v, _, _ in cand]
    di(f'despacho {nombre_ddec} (V{v_usada} de {", ".join(disponibles)})')
    ddec = bajar_fichero(DIR_DESPACHO, mes, nombre_ddec)

    # --- pronostico de demanda por barra -----------------------------------
    di(f'buscando PRON_BARRA{mmdd}* ...')
    pron = listar(DIR_DEMANDA, mes, f'PRON_BARRA{mmdd}')
    if not pron:
        raise ErrorDescarga(
            f'XM no tiene PRON_BARRA{mmdd} en {DIR_DEMANDA}/{mes}. '
            f'Sin pronostico de demanda no hay punto de operacion.')
    nombre_pron, mod_pron = pron[0]
    di(f'demanda {nombre_pron}')
    prb = bajar_fichero(DIR_DEMANDA, mes, nombre_pron)

    # --- normalizar ---------------------------------------------------------
    di('interpretando los ficheros...')
    despacho = leer_ddec(_texto(ddec), nombre_ddec)
    demanda = leer_pron_barra(_texto(prb), nombre_pron)

    # --- escribir, atomico --------------------------------------------------
    tmp = os.path.join(raiz_s, 'paratec', f'.tmp-{dia.isoformat()}')
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        norm = os.path.join(tmp, 'normalized')
        os.makedirs(norm)
        despacho.to_parquet(os.path.join(norm, 'dispatch.parquet'),
                            index=False)
        demanda.to_parquet(os.path.join(norm, 'demand.parquet'), index=False)

        origen = os.path.join(raiz_s, 'paratec', donante, 'normalized')
        heredadas = []
        for t in HEREDADAS:
            p = os.path.join(origen, f'{t}.parquet')
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(norm, f'{t}.parquet'))
                heredadas.append(t)
        di(f'directorio de PARATEC heredado de {donante} '
           f'({len(heredadas)} tablas)')

        crudo = os.path.join(tmp, 'raw', 'xm')
        os.makedirs(crudo)
        with open(os.path.join(crudo, nombre_ddec), 'wb') as f:
            f.write(ddec)
        with open(os.path.join(crudo, nombre_pron), 'wb') as f:
            f.write(prb)

        manifiesto = {
            'snapshot_date': dia.isoformat(),
            'extracted_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
            'scraper_version': f'modelo-stn/{_version()}',
            'attribution': ATRIBUCION,
            'dispatch_version': {'used': f'V{v_usada}',
                                 'available': disponibles},
            'sources': [
                {'entity': 'xm.dispatch', 'file': nombre_ddec,
                 'source_modified_at': mod_ddec,
                 'standard_version': 'XM-CND-2014-045 ed. 2025'},
                {'entity': 'xm.demand', 'file': nombre_pron,
                 'source_modified_at': mod_pron,
                 'standard_version': 'XM-CND-2014-045 ed. 2025'},
            ],
            # Lo que NO se descargo, y de donde salio. Sin esto, dentro de seis
            # meses nadie sabra que el directorio de este snapshot es prestado.
            'paratec_heredado_de': donante,
            'paratec_tablas_heredadas': heredadas,
            'record_counts': {
                'xm.dispatch.resources': int(despacho.resource.nunique()),
                'xm.demand.nodes': int(
                    demanda.groupby(['node', 'vn_kv']).ngroups),
            },
        }
        with open(os.path.join(tmp, 'manifest.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(manifiesto, f, indent=2, ensure_ascii=False)

        if os.path.isdir(final):
            shutil.rmtree(final)
        os.rename(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    di(f'listo: {despacho.resource.nunique()} recursos y '
       f'{demanda.groupby(["node", "vn_kv"]).ngroups} nodos de demanda')
    return final


def _version():
    try:
        from stn import __version__
        return __version__
    except Exception:                                        # noqa: BLE001
        return '?'


# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        description='Descarga de XM el despacho y el pronostico de demanda '
                    'de una fecha.')
    p.add_argument('--fecha', help='AAAA-MM-DD')
    p.add_argument('--listar', action='store_true',
                   help='enseña las fechas que ya estan en disco')
    p.add_argument('--forzar', action='store_true',
                   help='vuelve a bajar una fecha que ya esta')
    a = p.parse_args(argv)

    if a.listar or not a.fecha:
        fechas = fechas_en_disco()
        print(f'\nsnapshots en {XM}/paratec:')
        for f in fechas:
            print('  ', f)
        if not fechas:
            print('   (ninguno)')
        if not a.fecha:
            print('\nPara traer otra:  python -m stn.fuentes.descarga_xm '
                  '--fecha AAAA-MM-DD')
            return 0

    try:
        destino = descargar_fecha(a.fecha, forzar=a.forzar)
    except ErrorDescarga as e:
        print(f'\nNo se pudo: {e}\n', file=sys.stderr)
        return 1
    print(f'\n{destino}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
