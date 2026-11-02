# mapa_interactivo.py
"""Mapa interactivo del STN con los resultados del flujo de carga.

Genera un HTML con capas que se pueden encender y apagar:

  - lineas separadas por nivel de tension (500 kV y 220/230 kV)
  - lineas coloreadas por cargabilidad, para ver los corredores exigidos
  - autotransformadores 500/230
  - subestaciones por nivel, con el detalle de cada una al pasar el raton
  - subestaciones coloreadas por tension de barra, para localizar las que
    quedan fuera del rango operativo

Uso:
    python mapa_interactivo.py          # resuelve el modelo y escribe el HTML
"""

import numpy as np
import pandas as pd
from stn.config import salida

SALIDA = salida('mapa_stn_interactivo.html')

COLOR_NIVEL = {500.0: '#c0392b', 230.0: '#2471a3'}
NOMBRE_NIVEL = {500.0: '500 kV', 230.0: '220/230 kV'}


def _color_carga(pct):
    """Verde -> amarillo -> naranja -> rojo segun la cargabilidad."""
    if pct is None or not np.isfinite(pct):
        return '#7f8c8d'
    if pct < 25:
        return '#27ae60'
    if pct < 50:
        return '#f1c40f'
    if pct < 75:
        return '#e67e22'
    if pct < 90:
        return '#d35400'
    return '#c0392b'


def _color_tension(v):
    """Azul si esta alta, verde en rango, rojo si esta baja."""
    if not np.isfinite(v):
        return '#7f8c8d'
    if v < 0.90:
        return '#c0392b'
    if v < 0.95:
        return '#e67e22'
    if v <= 1.05:
        return '#27ae60'
    if v <= 1.10:
        return '#2980b9'
    return '#8e44ad'


def _datos_por_bus(network, topologia=None):
    """Generacion, demanda, tension y zona de cada bus.

    `topologia` es la red COMPLETA, de la que sale la geometria. Los valores
    salen siempre de `network`, que es la resuelta.

    POR QUE DOS REDES. El punto de operacion puede retirar elementos: un
    enlace internacional sin programa se abre, y con el se va la barra del
    otro extremo (ver `fuentes.xm_snapshot.abrir_enlaces`). Si el mapa se
    dibujara solo con la red resuelta, esa barra y esas lineas DESAPARECERIAN
    del dibujo, y dos fechas no serian comparables de un vistazo: uno no
    sabria si una linea no esta porque esta abierta o porque nunca estuvo.
    Dibujando la topologia completa y marcando lo que esta fuera, el mapa
    ENSEÑA que el enlace esta abierto, que es informacion, no ruido.
    """
    topologia = topologia if topologia is not None else network
    v = network.buses_t.v_mag_pu.iloc[0] if len(network.buses_t.v_mag_pu) else None
    gen = network.generators.groupby('bus').p_set.sum()
    load = network.loads.groupby('bus').p_set.sum()
    try:
        from stn.zonas import area_por_bus
        zona = area_por_bus(topologia)
    except Exception:
        zona = {}
    activos = set(network.buses.index)
    filas = []
    for b in topologia.buses.index:
        sub, _, niv = b.rpartition('__')
        vivo = b in activos
        filas.append({
            'bus': b,
            'sub': sub,
            'v_nom': float(topologia.buses.at[b, 'v_nom']),
            'lat': float(topologia.buses.at[b, 'y']),
            'lon': float(topologia.buses.at[b, 'x']),
            'v_pu': float(v[b]) if (vivo and v is not None and b in v.index)
                    else np.nan,
            'gen': float(gen.get(b, 0.0)),
            'load': float(load.get(b, 0.0)),
            'area': zona.get(b, ('', ''))[1],
            'subarea': zona.get(b, ('', ''))[0],
            'activo': vivo,
        })
    return pd.DataFrame(filas).set_index('bus')


def construir(network, archivo=SALIDA, verbose=True, topologia=None):
    import folium

    topologia = topologia if topologia is not None else network
    bd = _datos_por_bus(network, topologia)
    validos = bd[np.isfinite(bd.lat) & np.isfinite(bd.lon)]
    centro = [validos.lat.mean(), validos.lon.mean()]

    # OpenStreetMap como base: las teselas de CartoDB pasaron a exigir
    # una API key y se renderizaban con la marca "API KEY REQUIRED".
    m = folium.Map(location=centro, zoom_start=6, tiles='OpenStreetMap',
                   control_scale=True)

    # --- flujos y cargabilidad de las lineas -----------------------------
    hay_flujo = len(network.lines_t.p0) > 0
    if hay_flujo:
        p0 = network.lines_t.p0.iloc[0]
        q0 = network.lines_t.q0.iloc[0]
        s = np.hypot(p0, q0)
        carga = (s / network.lines.s_nom.replace(0, np.nan) * 100)
    else:
        p0 = pd.Series(0.0, index=network.lines.index)
        carga = pd.Series(np.nan, index=network.lines.index)

    capas_linea = {}
    for niv in sorted(COLOR_NIVEL, reverse=True):
        capas_linea[niv] = folium.FeatureGroup(
            name=f"Lineas {NOMBRE_NIVEL[niv]}", show=True)
    capa_carga = folium.FeatureGroup(name='Lineas por cargabilidad', show=False)

    # Se recorre la topologia COMPLETA, no la red resuelta: las lineas tienen
    # que estar siempre, cambie lo que cambie el punto de operacion. Ver
    # `_datos_por_bus`.
    activas = set(network.lines.index)
    for ln in topologia.lines.index:
        b0 = topologia.lines.at[ln, 'bus0']
        b1 = topologia.lines.at[ln, 'bus1']
        if b0 not in bd.index or b1 not in bd.index:
            continue
        pts = [(bd.at[b0, 'lat'], bd.at[b0, 'lon']),
               (bd.at[b1, 'lat'], bd.at[b1, 'lon'])]
        if not all(np.isfinite(c) for p in pts for c in p):
            continue
        niv = bd.at[b0, 'v_nom']
        viva = ln in activas
        pct = float(carga.get(ln, np.nan)) if viva else np.nan
        flujo = float(p0.get(ln, 0.0)) if viva else 0.0
        grosor = 2.0 + 4.0 * min(abs(flujo) / 600.0, 1.0)

        # El detalle se guarda como TEXTO y cada PolyLine recibe su propio
        # objeto Popup. Reutilizar un mismo Popup en dos capas hacia que
        # folium emitiera el bindPopup contra la variable de la segunda linea
        # dentro del bloque de la primera, con lo que el script se abortaba
        # con un ReferenceError y no se dibujaba nada.
        detalle = (
            f"<b>{ln}</b><br>"
            f"{bd.at[b0,'sub']} &rarr; {bd.at[b1,'sub']}<br>"
            f"Nivel: {NOMBRE_NIVEL.get(niv, niv)}<br>"
            f"Longitud: {topologia.lines.at[ln,'length']:.1f} km<br>"
            f"R={topologia.lines.at[ln,'r']:.2f} &Omega; &nbsp; "
            f"X={topologia.lines.at[ln,'x']:.2f} &Omega;<br>"
            f"Capacidad: {topologia.lines.at[ln,'s_nom']:.0f} MVA<br>")
        if viva:
            detalle += f"<b>Flujo: {flujo:.1f} MW</b>"
            if np.isfinite(pct):
                detalle += f"<br>Cargabilidad: {pct:.1f}%"
        else:
            detalle += ("<b>FUERA DE SERVICIO en este punto de operacion</b>"
                        "<br><i>la linea existe; en esta fecha no transporta"
                        "</i>")

        # Fuera de servicio: gris, fino y a trazos. Se ve que esta y se ve que
        # no transporta, que son dos cosas distintas y las dos informan.
        estilo = ({} if viva else
                  {'dash_array': '6,8', 'opacity': 0.45, 'color': '#95a5a6'})
        folium.PolyLine(pts, color=estilo.get('color',
                                              COLOR_NIVEL.get(niv, '#555')),
                        weight=grosor if viva else 2.0,
                        opacity=estilo.get('opacity', 0.8),
                        dash_array=estilo.get('dash_array'),
                        tooltip=(f"{ln} · {flujo:.0f} MW" if viva
                                 else f"{ln} · fuera de servicio"),
                        popup=folium.Popup(detalle, max_width=340)
                        ).add_to(capas_linea[niv])
        folium.PolyLine(pts,
                        color=_color_carga(pct) if viva else '#95a5a6',
                        weight=(grosor + 1) if viva else 2.0,
                        opacity=0.9 if viva else 0.45,
                        dash_array=estilo.get('dash_array'),
                        tooltip=(f"{ln} · {pct:.0f}%" if np.isfinite(pct)
                                 else f"{ln} · fuera de servicio"),
                        popup=folium.Popup(detalle, max_width=340)
                        ).add_to(capa_carga)

    for c in capas_linea.values():
        c.add_to(m)
    capa_carga.add_to(m)

    # --- transformadores --------------------------------------------------
    if len(network.transformers):
        capa_tr = folium.FeatureGroup(name='Autotransformadores 500/230', show=True)
        st = (np.hypot(network.transformers_t.p0.iloc[0],
                       network.transformers_t.q0.iloc[0])
              if len(network.transformers_t.p0) else
              pd.Series(0.0, index=network.transformers.index))
        for tr in network.transformers.index:
            b0 = network.transformers.at[tr, 'bus0']
            if b0 not in bd.index or not np.isfinite(bd.at[b0, 'lat']):
                continue
            s_nom = network.transformers.at[tr, 's_nom']
            pct = 100 * float(st.get(tr, 0.0)) / s_nom if s_nom else np.nan
            folium.RegularPolygonMarker(
                location=[bd.at[b0, 'lat'], bd.at[b0, 'lon']],
                number_of_sides=4, radius=8, rotation=45,
                color='#000', weight=1, fill_color='#f39c12', fill_opacity=0.9,
                tooltip=f"{tr} · {pct:.0f}%",
                popup=folium.Popup(
                    f"<b>{tr}</b><br>Capacidad: {s_nom:.0f} MVA<br>"
                    f"Flujo: {float(st.get(tr,0.0)):.1f} MVA<br>"
                    f"Cargabilidad: {pct:.1f}%", max_width=300)
            ).add_to(capa_tr)
        capa_tr.add_to(m)

    # --- subestaciones ----------------------------------------------------
    capas_bus = {niv: folium.FeatureGroup(
        name=f"Subestaciones {NOMBRE_NIVEL[niv]}", show=True)
        for niv in sorted(COLOR_NIVEL, reverse=True)}
    capa_v = folium.FeatureGroup(name='Subestaciones por tension de barra',
                                 show=False)

    for b, r in bd.iterrows():
        if not (np.isfinite(r.lat) and np.isfinite(r.lon)):
            continue
        radio = 4 + 6 * min(max(r.gen, r.load) / 600.0, 1.0)
        detalle = (
            f"<b>{r.sub}</b><br>"
            f"Nivel: {NOMBRE_NIVEL.get(r.v_nom, r.v_nom)}<br>"
            f"Area: {r.area} / {r.subarea}<br>"
            f"Tension: {r.v_pu:.4f} p.u.<br>"
            f"Generacion: {r.gen:.1f} MW<br>"
            f"Demanda: {r.load:.1f} MW")
        etiqueta = f"{r.sub} ({NOMBRE_NIVEL.get(r.v_nom,'')}) · {r.v_pu:.3f} p.u."
        cap = capas_bus.get(r.v_nom)
        if cap is not None:
            folium.CircleMarker(
                [r.lat, r.lon], radius=radio,
                color='#1b2631', weight=1,
                fill=True, fill_color=COLOR_NIVEL.get(r.v_nom, '#555'),
                fill_opacity=0.85, tooltip=etiqueta,
                popup=folium.Popup(detalle, max_width=300)).add_to(cap)
        folium.CircleMarker(
            [r.lat, r.lon], radius=radio, color='#1b2631', weight=1,
            fill=True, fill_color=_color_tension(r.v_pu), fill_opacity=0.9,
            tooltip=etiqueta,
            popup=folium.Popup(detalle, max_width=300)).add_to(capa_v)

    for c in capas_bus.values():
        c.add_to(m)
    capa_v.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    m.get_root().html.add_child(folium.Element(_leyenda()))
    m.save(archivo)
    if verbose:
        print(f"  mapa interactivo guardado en '{archivo}'")
    return archivo


def _leyenda():
    return """
<div style="position:fixed; bottom:24px; left:24px; z-index:9999;
  background:rgba(255,255,255,.94); padding:12px 14px; border-radius:8px;
  border:1px solid #b3b3b3; font:12px/1.5 system-ui,sans-serif;
  box-shadow:0 2px 8px rgba(0,0,0,.2); max-width:260px">
<b>Red STN Colombia</b><br>
<span style="color:#c0392b">&#9644;&#9644;</span> 500 kV &nbsp;
<span style="color:#2471a3">&#9644;&#9644;</span> 220/230 kV<br>
El grosor de la linea crece con el flujo.<br>
<hr style="margin:7px 0;border:none;border-top:1px solid #ddd">
<b>Cargabilidad</b> (capa opcional)<br>
<span style="color:#27ae60">&#9632;</span> &lt;25%
<span style="color:#f1c40f">&#9632;</span> 25-50%
<span style="color:#e67e22">&#9632;</span> 50-75%
<span style="color:#d35400">&#9632;</span> 75-90%
<span style="color:#c0392b">&#9632;</span> &gt;90%<br>
<hr style="margin:7px 0;border:none;border-top:1px solid #ddd">
<b>Tension de barra</b> (capa opcional)<br>
<span style="color:#c0392b">&#9679;</span> &lt;0,90
<span style="color:#e67e22">&#9679;</span> 0,90-0,95
<span style="color:#27ae60">&#9679;</span> 0,95-1,05
<span style="color:#2980b9">&#9679;</span> 1,05-1,10<br>
<hr style="margin:7px 0;border:none;border-top:1px solid #ddd">
<span style="color:#f39c12">&#9670;</span> Autotransformador 500/230<br>
<i>Pasa el raton por encima para identificar;
haz clic para ver el detalle.</i>
</div>"""


def main():
    """Dibuja el mapa del punto de operacion que se le pida.

    ANTES REPLICABA EL PIPELINE A MANO ---load_lines, build_network, integrar,
    desglose, asignar, slack, flujo, limites--- que era la QUINTA copia de la
    misma secuencia en el proyecto. Cada copia es una oportunidad de que un
    flag quede cableado en unas y no en otras, que es el fallo que
    `test_invariantes.test_los_cuatro_caminos_coinciden` existe para cazar.
    Ahora entra por `punto_operacion`, como todo lo demas, y de paso acepta
    `--diurno` y `--fecha` sin codigo propio.

    LA TOPOLOGIA SE DIBUJA SIEMPRE COMPLETA. El mapa se usa para comparar
    fechas de un vistazo, y para eso las lineas tienen que estar siempre en el
    mismo sitio: lo que cambia entre fechas son los flujos y los colores, no
    el trazado. Lo que el punto de operacion deje fuera de servicio ---los
    circuitos de un enlace internacional sin programa, por ejemplo--- se
    dibuja a trazos y en gris, no se omite. Ver `_datos_por_bus`.
    """
    import argparse

    from stn import punto_operacion as PO
    from stn.config import salida
    from stn.data_loader import load_lines
    from stn.network_builder import build_network

    ap = argparse.ArgumentParser(
        description='Mapa interactivo del STN sobre un punto de operacion.')
    PO.anadir_flag(ap)
    args = ap.parse_args()

    red, _, ctx = PO.de_args(args, verbose=False)
    PO.cabecera(ctx)

    # La red de referencia: toda la topologia, sin inyecciones. Es la misma
    # para cualquier fecha, que es justo lo que se necesita.
    topologia = build_network(load_lines(verbose=False), verbose=False)

    destino = SALIDA
    if ctx['sufijo']:
        destino = salida(f"mapa_stn_interactivo{ctx['sufijo']}.html")
    fuera = len(topologia.lines) - len(red.lines)
    if fuera:
        print(f"  {fuera} lineas fuera de servicio en este punto: se dibujan "
              f"a trazos, no se omiten")
    construir(red, archivo=destino, topologia=topologia)


if __name__ == '__main__':
    main()
