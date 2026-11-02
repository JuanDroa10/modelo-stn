# visualization.py
import matplotlib
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np

from stn.config import PLOT_GEOMAP, PLOT_TITLE, EXPORT_FOLDER, salida
from stn.paths import RED, dato

SALIDA_PNG = salida("red_stn.png")

# Contorno de Colombia y sus vecinos, para que el PNG no salga sobre blanco.
#
# POR QUE NO SE USA CARTOPY. `network.plot(geomap=True)` de PyPSA dibuja el
# fondo con cartopy, que no esta instalado y es una dependencia pesada (GEOS y
# PROJ). Ademas descarga los contornos de Natural Earth en cada arranque si no
# hay cache, lo que ya colgo el pipeline una vez.
#
# Aqui se lleva el mismo dato de Natural Earth ---110m, dominio publico, es la
# misma fuente que cartopy usa por debajo--- recortado a Colombia y los cinco
# paises que la rodean y guardado en el repositorio: 14 KB. Se dibuja con
# matplotlib a pelo, sin proyeccion, que para este rango de latitudes es
# visualmente indistinguible de PlateCarree. Sin dependencias nuevas y sin red.
CONTORNO = dato(RED, 'contorno_paises.geojson')


def _fondo_paises(ax):
    """Dibuja las siluetas de los paises.

    Devuelve la caja de Colombia (x0, x1, y0, y1) para poder encuadrar, o
    None si no se pudo dibujar.
    """
    import json
    import os
    if not os.path.exists(CONTORNO):
        return None
    try:
        with open(CONTORNO, encoding='utf-8') as f:
            geo = json.load(f)
    except (OSError, ValueError):
        return None

    def anillos(g):
        """Lista de anillos exteriores, sea Polygon o MultiPolygon."""
        if g['type'] == 'Polygon':
            return [g['coordinates'][0]]
        if g['type'] == 'MultiPolygon':
            return [p[0] for p in g['coordinates']]
        return []

    caja = None
    for feat in geo.get('features', []):
        es_col = feat.get('properties', {}).get('name') == 'Colombia'
        for anillo in anillos(feat.get('geometry', {})):
            xs = [p[0] for p in anillo]
            ys = [p[1] for p in anillo]
            # Colombia resaltada; los vecinos en gris claro, solo de contexto
            ax.fill(xs, ys, facecolor='#eef2f5' if es_col else '#f7f7f7',
                    edgecolor='#9aa5b1' if es_col else '#d0d0d0',
                    linewidth=0.9 if es_col else 0.5, zorder=0)
            if es_col:
                c = (min(xs), max(xs), min(ys), max(ys))
                caja = c if caja is None else (
                    min(caja[0], c[0]), max(caja[1], c[1]),
                    min(caja[2], c[2]), max(caja[3], c[3]))
    return caja


def _guardar_o_mostrar(fig, archivo):
    fig.savefig(archivo, dpi=150, bbox_inches='tight')
    print(f"  figura guardada en '{archivo}'")
    # solo intentar mostrar si el backend es interactivo
    if matplotlib.get_backend().lower() not in ('agg', 'pdf', 'ps', 'svg'):
        plt.show()
    plt.close(fig)


def plot_network(network, title=None, archivo=SALIDA_PNG):
    """Dibuja la red coloreando los buses por nivel de tension."""
    title = title or PLOT_TITLE
    try:
        if PLOT_GEOMAP:
            # OJO: `geomap=True` hace que cartopy DESCARGUE los contornos de
            # Natural Earth la primera vez, y esa descarga no lleva tiempo
            # limite. Sin red ---o con el servidor lento--- `main.py` se queda
            # colgado indefinidamente, sin mensaje y al 0 % de CPU, justo
            # despues de resolver el flujo. Parece que el modelo se ha roto y
            # en realidad esta esperando un fichero de mapa.
            #
            # El `except ImportError` de abajo no cubria eso: solo cubre que
            # falte cartopy. Se le pone un tope al socket para que falle
            # rapido y caiga al layout por coordenadas, que no necesita red.
            import socket
            antes = socket.getdefaulttimeout()
            try:
                import cartopy.crs as ccrs
                socket.setdefaulttimeout(20)
                fig = plt.figure(figsize=(11, 13))
                ax = plt.axes(projection=ccrs.PlateCarree())
                network.plot(ax=ax, geomap=True, line_widths=0.6, bus_sizes=0.01)
                ax.set_title(f"{title} - mapa geografico")
                return _guardar_o_mostrar(fig, archivo)
            except ImportError:
                # No es un problema: `_fondo_paises` dibuja el contorno desde
                # el GeoJSON cacheado y el resultado es equivalente para este
                # uso. Cartopy solo haria falta para reproyectar.
                pass
            except (OSError, socket.timeout) as e:
                print(f"  no se pudieron descargar los contornos del mapa "
                      f"({type(e).__name__}); se usa el layout por coordenadas")
                plt.close('all')
            finally:
                socket.setdefaulttimeout(antes)

        fig, ax = plt.subplots(figsize=(11, 13))
        caja = _fondo_paises(ax)
        network.plot(ax=ax, geomap=False, line_widths=0.6, bus_sizes=0.01)
        if caja is not None:
            # Se encuadra en COLOMBIA ENTERA, no en la red. El STN no llega a
            # la Amazonia ---la red abarca de 0,4 a 11,3 de latitud y el pais
            # de -4,3 a 12,4--- asi que ajustar a la red recortaba el sureste
            # y la silueta no se reconocia.
            x0, x1, y0, y1 = caja
            mx, my = 0.04 * (x1 - x0), 0.04 * (y1 - y0)
            ax.set_xlim(x0 - mx, x1 + mx)
            ax.set_ylim(y0 - my, y1 + my)
            ax.set_aspect('equal', adjustable='box')
            ax.set_axis_off()
            ax.set_title(f"{title} - sobre el contorno de Colombia")
        else:
            ax.set_title(f"{title} - coordenadas")
        return _guardar_o_mostrar(fig, archivo)

    except Exception as e:
        print(f"  network.plot fallo ({e}); se dibuja con networkx")
        G = nx.Graph()
        G.add_nodes_from(network.buses.index)
        G.add_edges_from(zip(network.lines.bus0, network.lines.bus1))
        if len(network.transformers):
            G.add_edges_from(zip(network.transformers.bus0,
                                 network.transformers.bus1))
        pos = {}
        for bus in network.buses.index:
            x, y = network.buses.at[bus, 'x'], network.buses.at[bus, 'y']
            pos[bus] = (0.0, 0.0) if (np.isnan(x) or np.isnan(y)) else (x, y)
        colores = ['crimson' if network.buses.at[b, 'v_nom'] >= 400 else 'steelblue'
                   for b in G.nodes]
        fig = plt.figure(figsize=(12, 14))
        nx.draw_networkx_edges(G, pos, alpha=0.4, edge_color='gray')
        nx.draw_networkx_nodes(G, pos, node_size=28, node_color=colores)
        nx.draw_networkx_labels(G, pos, font_size=4)
        plt.title(f"{title}  (rojo 500 kV / azul 220-230 kV)")
        plt.axis('off')
        return _guardar_o_mostrar(fig, archivo)


def export_network(network, folder=EXPORT_FOLDER):
    """Exporta la red a una carpeta de CSV."""
    network.export_to_csv_folder(folder)
    print(f"  red exportada a '{folder}'")
