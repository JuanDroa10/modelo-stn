# Guía de uso del modelo del STN

Esta guía explica **cómo se usa el modelo**, opción por opción y pregunta por
pregunta. El [README](README.md) cuenta qué es el modelo y cómo se instala;
aquí empezamos donde aquel termina, con el menú ya abierto.

```bash
python menu.py
```

---

## Índice

1. [Lo primero: el punto de operación](#1-lo-primero-el-punto-de-operación)
2. [Cómo se navega el menú](#2-cómo-se-navega-el-menú)
3. [Simular](#3-simular) — opciones 1 a 8
4. [Consultar el modelo](#4-consultar-el-modelo) — opciones 9 a 14
5. [Análisis](#5-análisis) — opciones 15 a 18
6. [Lo mismo desde la línea de órdenes](#6-lo-mismo-desde-la-línea-de-órdenes)
7. [Antes de citar una cifra](#7-antes-de-citar-una-cifra)
8. [Problemas frecuentes](#8-problemas-frecuentes)

---

## 1. Lo primero: el punto de operación

El menú **no pregunta qué análisis quieres, sino sobre qué sistema**. Esa
elección vale para toda la sesión y aparece en una banda en la cabecera de
todas las pantallas.

```
SOBRE QUE SISTEMA QUIERES TRABAJAR
   (aqui el menu explica el respaldo de cada uno)
   Punto de operacion:
     * 1) punta nocturna 18-22 h  (el caso MEDIDO y validado)
       2) diurno 10-11 h  (con sol; DERIVADO de la punta, no validado)
       3) una fecha concreta  (despacho y pronostico reales de XM)
```

El `*` marca el valor por defecto: pulsar Enter elige la punta.

### Los tres puntos no tienen el mismo respaldo

Esto es lo más importante de toda la guía, y por eso el menú lo repite en
color en cada pantalla:

| Punto | De dónde sale | Qué respaldo tiene |
|---|---|---|
| **Punta nocturna** | caso de flujo de carga de PowerFactory | **MEDIDO** y contrastado contra PowerFactory |
| **Diurno** | se deriva de la punta con factores horarios de XM | **DERIVADO**; hereda la validación de la red, no la del punto |
| **Una fecha** | despacho `dDEC` y pronóstico `PRON_BARRA` de XM | **CONSTRUIDO** desde cero; hereda la validación de la red, no la del punto |

Que los tres pasen por el mismo código no los pone al mismo nivel de
evidencia. Un resultado sobre el diurno o sobre una fecha es tan bueno como el
despacho del que parte.

### Opción 1 — punta nocturna

No pregunta nada más. Es el caso de referencia: 18-22 h, sin sol, máxima
demanda.

### Opción 2 — diurno

Pregunta la hora:

```
Que hora:
  * 1) 10:00-11:00 (maxima penetracion solar)
    2) 13:00-14:00
```

La primera es la de **máxima penetración solar**, que es el peor caso para la
inercia. La clave horaria que usa XM (`HourNN`) es 11 y 13 respectivamente.

### Opción 3 — una fecha concreta

Lista las fechas que ya están en disco y, **en la última línea**, la entrada
para traer una nueva:

```
Fechas en disco:
   1) 2026-09-16
   2) 2026-10-20
   3) 2026-10-21
   4) 2026-10-24
   5) otra fecha  ---  descargarla de XM ahora
```

Eligiendo la última se escribe la fecha en formato `AAAA-MM-DD` (por defecto,
mañana) y el modelo se trae del portal público de XM los dos ficheros que de
verdad son de ese día: el **despacho programado** (`dDEC`) y el **pronóstico de
demanda por barra** (`PRON_BARRA`). Pesan unos 150 kB entre los dos y es lo
único para lo que hace falta internet.

> **Qué NO se descarga.** El directorio de PARATEC —qué subestación, qué
> línea, qué unidad— se hereda del snapshot más reciente que ya esté en disco,
> porque **PARATEC no tiene histórico**: su API devuelve siempre el registro de
> hoy, se le pida la fecha que se le pida. El `manifest.json` deja escrito de
> cuál se heredó. Si un recurso del despacho no estuviera en ese directorio no
> se colaría en silencio: bajaría la cobertura, que es justo lo que el menú
> enseña a continuación.

XM publica el despacho de un día la tarde anterior, así que **la fecha más
nueva que se puede pedir es mañana**. Si la fecha ya estaba en disco, el menú
no vuelve a bajarla: la selecciona y lo dice.

Después se pide el **periodo horario**: el índice 1-24 que usa XM en sus
ficheros. Por defecto el 19, que es el de la punta nocturna.

### El aviso de calidad

Elegida una fecha, el menú **construye ese punto y enseña su cobertura** antes
de dejar seguir:

```
generacion ubicada:  99.92 %   demanda ubicada:  99.02 %
cobertura suficiente para estudios de frecuencia
```

(Las cifras son un ejemplo real del 2026-11-01; cada fecha da las suyas.)

Si falta demanda por ubicar sale un aviso en rojo. No es cosmético: **el EDAC
dimensiona sobre la demanda que ve**, así que cortaría de menos y el nadir
saldría OPTIMISTA. Las fechas anteriores a septiembre de 2026 usan códigos de
nodo antiguos y rara vez pasan el filtro.

---

## 2. Cómo se navega el menú

| Tecla | Qué hace |
|---|---|
| `1`…`18` | abre esa opción |
| `p` | cambia el punto de operación sin salir |
| `q` | sale |
| Enter a secas | no hace nada (no cierra la sesión) |

Tres costumbres del menú que conviene conocer:

- **Cada opción se explica antes de lanzarse.** Lo que lees arriba del todo no
  es decoración: dice qué hace, qué supone y cuánto tarda.
- **Enter acepta el valor por defecto** en todas las preguntas. Una sesión
  entera a base de Enter es un recorrido válido. Dónde se ve ese defecto
  depende del tipo de pregunta: en las listas cortas de opciones va marcado con
  `*`, y en todo lo demás —incluidas **las listas numeradas de fechas y de
  elementos**, que no llevan `*`— va entre corchetes al final de la línea,
  `[3000.0]`.
- **Las opciones que lanzan un módulo aparte enseñan la orden que construyen**
  antes de ejecutarla:

  ```
  $ python -m stn.contingencias --fecha 2026-10-20 --periodo 19
  ```

  Está ahí a propósito: así se ve qué banderas se emiten, se puede repetir la
  ejecución fuera del menú y se puede pegar en un cuaderno de trabajo. Son las
  ocho de **Simular** y la 18. Las de **Consultar** y las tres primeras de
  **Análisis** corren dentro del propio menú y no imprimen ninguna orden,
  porque no hay otra que imprimir.

### Dónde quedan las salidas

En `resultados/`, con **un sufijo por punto de operación** (`_diurno11`,
`_20261020p19`) para que una ejecución no pise a la otra. La punta no lleva
sufijo.

### Por qué la primera consulta tarda

El caso base tarda unos 20 segundos en construirse y resolverse. El menú lo
construye **una vez por sesión** y lo reutiliza, así que a partir de la segunda
vez las consultas son inmediatas. Al cambiar de punto con `p`, ese caché se
descarta: sería enseñar consultas de un sistema y simulaciones de otro.

Las simulaciones largas se lanzan en un proceso aparte, porque modifican la red
y no conviene que contaminen el caso cacheado.

---

## 3. Simular

### Opción 1 — Caso base y flujo de carga

Reconstruye la red desde los datos, resuelve el **flujo AC de Newton-Raphson**
y muestra el balance por área, la reconciliación de la demanda y los totales.
Genera además el mapa y los ficheros de exportación.

**No pregunta nada.** Tarda alrededor de un minuto. Es el punto de partida de
todo lo demás: si esto no sale, nada de lo que sigue tiene sentido.

---

### Opción 2 — Simular contingencias

Saca elementos de servicio y comprueba tres cosas: que el flujo **siga
convergiendo**, que nadie quede **sobrecargado** sobre su límite de emergencia,
y que ninguna **tensión** se salga de 0,9-1,1 pu.

> **Cómo se cuentan las violaciones, que no es igual para las dos.** Las
> **tensiones** fuera de banda son incrementales: se restan las del caso base,
> así que `n_v_bajas` y `n_v_altas` son sólo las que *añade* la contingencia.
> El radial de Arauca ya tiene 4 barras bajo 0,9 pu sin que pase nada, y
> contarlas en los 296 casos sería contar 296 veces el mismo problema.
> Las **sobrecargas** no: `n_sobrecargas` cuenta todos los elementos por encima
> de su límite tras la contingencia, incluidos los que ya lo estuvieran antes.

**Primera pregunta — qué simular:**

```
Que quieres simular:
  * 1) el barrido completo (lineas y trafos, 296 casos, ~2 min)
    2) solo 500 kV (55 casos, rapido)
    3) un area concreta
    4) UNA contingencia concreta: el N-1 de un elemento que eliges
    5) dobles contingencias N-2 en 500 kV
```

- **Barrido completo** — cada línea y cada transformador, uno a uno.
- **Solo 500 kV** — acota al nivel de 500 kV. Es la vía rápida.
- **Un área concreta** — pide el nombre del área. Valores válidos: `Caribe`,
  `Antioquia`, `Oriental`, `Nordeste`, `Suroccidental` (por defecto `Caribe`).
- **Una contingencia concreta** — ver abajo.
- **N-2 en 500 kV** — todos los pares de elementos de 500 kV.

**El selector de elemento (opción 4).** Los nombres del modelo son largos
(`ANTIOQUIA 500 - CERROMATOSO 500 1`) y nadie se los sabe de memoria, así que
no te los pide a pelo:

```
parte del nombre (Enter = las mas cargadas) []:
```

- Escribiendo un trozo de texto, lista las coincidencias ordenadas **de más a
  menos cargada**, hasta 25. Si hay más, lo dice y pide afinar la búsqueda.
- Pulsando Enter sin escribir nada, lista **las más cargadas del caso base**,
  que son las contingencias que de verdad interesan.

Cada línea muestra el tipo, el nombre y su carga como porcentaje de `s_nom`.
Se elige por número; `0` cancela.

El resultado de una contingencia concreta termina con un **veredicto en una
línea**: N-1 limpio, converge pero con sobrecargas o tensiones fuera de banda,
la red se separa en islas (diciendo cuánta carga y cuánta generación quedan
fuera), o el flujo no converge.

**Segunda pregunta — la curva de capacidad:**

```
Imponer la curva de capacidad de los generadores (PV->PQ)?
  * 1) no (mas rapido)
    2) si (mas lento, detecta colapsos de tension)
```

Esto importa más de lo que parece. **Sin límites, las barras PV sostienen su
tensión con el reactivo que haga falta y el colapso de tensión es imposible por
construcción**: el barrido no puede encontrarlo aunque esté ahí. Con límites,
cada máquina que excede su curva se conmuta a PQ y el caso necesita varios
flujos en vez de uno.

Con límites, el barrido completo tarda unos 15 minutos (el menú avisa). Una
sola contingencia son segundos.

---

### Opción 3 — Dinámica de frecuencia (EDAC y AGC)

Simula qué pasa cuando se pierde generación de golpe: integra la **ecuación de
oscilación**, dispara los escalones del EDAC según el **Acuerdo 2105 del CNO**,
reparte el deslastre y deja que el AGC absorba el resto. Termina con un flujo
AC del estado post-evento.

**Primera pregunta — qué evento:**

```
Que evento simular:
  * 1) perdida de N MW de generacion
    2) perdida de un area completa
    3) perdida de generadores concretos
```

| Opción | Qué pide | Por defecto |
|---|---|---|
| N MW | los MW que se pierden | `3000` |
| un área | el nombre del área | `Antioquia` |
| generadores concretos | nombres separados por espacio | `gen_ANTIOQUIA__500` |

Para saber cómo se llama un generador, usa antes la opción 10 (*Buscar una
barra, generador o línea*).

**Segunda pregunta — cómo repartir el deslastre:**

```
Como repartir el deslastre:
  * 1) edac (proporcional, como el esquema regulado)
    2) optimo (OPF que elige donde cortar)
    3) comparar los dos
```

- **edac** — el reparto proporcional del esquema regulado. Es lo que haría el
  sistema real.
- **optimo** — un OPF que elige dónde cortar.
- **comparar** — corre los dos y los pone uno al lado del otro.

---

### Opción 4 — Comparar dos horas lado a lado

Somete **dos puntos de operación** a las mismas pruebas y los imprime en
columnas con la variación ya calculada: la red, el balance de potencia, el
estado de la red resuelta, lo que decide la frecuencia, la respuesta a la misma
perturbación, la contingencia más grande de cada uno, el balance por área, el
N-1 y el pool de BTM.

La **columna A es siempre el punto activo** de la sesión (el que cambias con
`p`). Lo único que se pregunta es contra qué compararlo, y las opciones se
adaptan a lo que tengas elegido:

| Punto activo | Opciones que ofrece |
|---|---|
| una fecha de XM | **otro periodo de la misma fecha**, la punta, el diurno, otra fecha |
| diurno | la punta, el diurno a otra hora, una fecha |
| punta | el diurno a una hora, una fecha |

Con una fecha activa, *otro periodo de la misma fecha* va la primera a
propósito: es la comparación que más se pide —la hora crítica contra la punta
de ese día—. El defecto que ofrece depende de dónde estés: **si el periodo
activo no es el 5, propone el 5**, porque el barrido de las 24 horas sitúa la
hora crítica en la 4 o la 5 de la madrugada y nunca en la de máxima demanda; y
**si ya estás en el 5, propone el 19**, que es la punta. Dicho de otro modo:
pulsando Enter siempre obtienes el otro extremo del día.

Si las dos columnas acabaran siendo el mismo punto, lo dice y no corre nada.

**Después pregunta:**

- **La perturbación de referencia en MW** (por defecto `3000`). Es el mismo
  evento en MW absolutos sobre los dos sistemas: ésa es la comparación que
  interesa.
- **El barrido N-1**: leer los CSV ya generados (rápido, el defecto) o
  correrlo ahora, que tarda varios minutos *por columna*.

**Cómo leer la salida.** Los comentarios que acompañan a cada bloque se
calculan de las dos columnas, no están escritos de antemano. Si una columna
tiene más demanda y aun así menos energía cinética, lo dice; si el número de
generadores cambia entre las dos, lo explica (una máquina está en el modelo si
el despacho la pone a producir esa hora, y ese compromiso de unidades es lo que
mueve la inercia de una hora a otra). La casilla de *demanda BRUTA* sale en
blanco en las columnas que vienen de una fecha de XM, porque el `PRON_BARRA`
publica demanda NETA y el modelo no tiene con qué separar lo embebido.

---

### Opción 5 — Deslastre con BTM *[la contribución]*

El algoritmo central del trabajo. Reparte generación detrás del contador por
las barras, desconecta de golpe la parte conectada por inversor, y resuelve un
**MILP** que asigna barras a los escalones del EDAC teniendo en cuenta cuánta
generación embebida **sana** se llevaría por delante cada corte.

> **Aviso en la punta.** Si el punto activo es la punta nocturna, el menú avisa
> de que la solar da CERO y el evento que estudia este módulo **no puede
> ocurrir**: el BTM que cae será sólo el no solar. Para verlo de verdad, cambia
> el punto con `p` a diurno o a una fecha con sol.

**Pregunta 1 — de dónde sale el BTM:**

```
De donde sale el BTM:
  * 1) agpe (CASO BASE: el BTM real de hoy, 740,8 MW segun UPME)
    2) agpe_gd (sensibilidad: + generacion distribuida, 1.346,8 MW)
    3) sintetico (penetracion supuesta, para escenarios futuros)
    4) ambas (el pool viejo AGPE+menores; NO es BTM, solo para reproducir
       cifras previas al 31-10-2026)
```

La cuarta está ahí sólo para reproducir cifras antiguas: **las plantas menores
no son BTM**, porque la CREG 086 de 1996 las declara excluyentes con la figura
de autogenerador.

**Pregunta 2 — sólo si elegiste `sintetico`:** la penetración supuesta en % de
la demanda (por defecto `20`).

**Pregunta 3 —** el % del BTM con inversor que se desconecta (por defecto
`80`). Es la profundidad del evento.

**Pregunta 4 —** pérdida convencional adicional en MW (por defecto `0`, que
significa ninguna). Sirve para superponer la caída del BTM a una pérdida de
generación síncrona.

**Pregunta 5 — con qué vara exigir la suficiencia del deslastre:**

```
Con que vara exigir la suficiencia del deslastre:
  * 1) peor_escenario (robustez de peor caso, recomendada)
    2) estimado (se fia de la estimacion)
    3) piso (garantia literal de Das, sobrecorta)
```

Esta pregunta es sobre **qué hacer con el error de estimación del BTM**:
`peor_escenario` se protege contra él, `estimado` se fía de la estimación, y
`piso` aplica la garantía literal de Das, que sobrecorta.

**Pregunta 6 —** el peso **lambda** de la penalización por destruir BTM sano
(por defecto `5`). Antes de moverlo, mira la opción 17.

**Pregunta 7 —** si comparar contra el reparto proporcional del EDAC actual
(por defecto, sí). Es lo que convierte la ejecución en un resultado publicable:
sin la comparación sólo tienes una cifra sin referencia.

---

### Opción 6 — Cascada multironda

A diferencia del N-1, **no supone que el sistema aguanta el primer golpe**:
encadena aperturas, deslastre y disparos de protección en una sola línea de
tiempo, ronda tras ronda, hasta que el sistema se estabiliza o colapsa.

**Pregunta 1 — qué escenario:**

```
Que escenario:
  * 1) btm (cae el BTM con inversor)
    2) iberia (secuencia tipo apagon iberico)
    3) cascada (4 perdidas de 600 MW encadenadas)
    4) simple (una perdida en t=0)
    5) abrir las N lineas mas cargadas
```

**Preguntas que dependen del escenario:**

| Escenario | Pregunta | Por defecto |
|---|---|---|
| `btm` | fracción del BTM que cae | `0.9` |
| `iberia`, **sólo en la punta** | solar EN PRODUCCIÓN en MW | `0.0` |
| `abrir las N` | cuántas líneas | `6` |

La solar sólo se pregunta en la punta, y es deliberado: en el diurno y en una
fecha **el punto de operación ya sabe cuánta hay** —medida del despacho, en el
caso de la fecha—, así que preguntarla sería dar a elegir entre un dato y una
suposición. Si la respondes sobre la punta nocturna, el menú avisa de que esa
solar es una hipótesis, no un dato.

---

### Opción 7 — Barrido de las 24 horas de una fecha

Somete las 24 horas de una fecha a **las mismas pruebas y al mismo evento en MW
absolutos**, y deja la tabla en un CSV. Es lo que convierte «cuál es la hora
crítica» de una elección en una medida: sale la 4 o la 5 de la madrugada, y
nunca la de máxima demanda.

Esta opción **no sigue al punto de operación**, y con razón: un barrido horario
sólo tiene sentido sobre una fecha de XM, porque la punta y el diurno son UNA
hora cada uno, no un día. Si el punto activo es una fecha, la propone la
primera; si no, pide una.

Las fechas se listan **de la más reciente a la más antigua**, porque el
catálogo de nodos se curó a finales de octubre de 2026 y las fechas anteriores
a septiembre rara vez pasan el filtro de cobertura (el barrido excluye sus
horas del resumen).

**Preguntas:**

| Pregunta | Por defecto |
|---|---|
| cuál (número de la lista) | `1` |
| barrer más fechas para comparar (sólo si hay más de una en disco) | `no, solo esa` |
| evento de generación perdida en MW | `3000` |
| gráfico de las 24 horas | `si` |

Con varias fechas las compara al final, que es la única forma de distinguir una
propiedad del sistema de una casualidad del día. Unos **3 minutos por fecha**.

---

### Opción 8 — Mapa interactivo

Genera un HTML con la red georreferenciada: líneas coloreadas por cargabilidad,
barras por tensión y los autotransformadores marcados. **No pregunta nada.**

El trazado es el mismo en todas las fechas; lo que cambia son los flujos y los
colores. Lo que el punto deje fuera de servicio **sale a trazos, no
desaparece**, para que dos fechas se puedan comparar de un vistazo sin
preguntarse si una línea falta o nunca estuvo.

---

## 4. Consultar el modelo

Estas seis opciones leen el caso cacheado: **no preguntan casi nada y
responden al instante** a partir de la segunda vez.

### Opción 9 — Resumen del sistema

Las cifras de una ojeada, en cinco bloques:

- **RED** — buses, líneas, trafos, generadores y cargas.
- **POTENCIA** — consignas, demanda, pérdidas, y cuántos MW pone el slack para
  cerrar el balance.
- **TENSIÓN** — rango en pu y cuántas barras están bajas o altas.
- **FRECUENCIA** — inercia equivalente *H*, capacidad acoplada *S_base*,
  energía cinética *H·S*, estatismo equivalente y reserva.
- **MEZCLA POR TECNOLOGÍA** — MW de placa por tecnología, indicando **cuántas
  máquinas de cada una tienen H declarado** y cuántas usan el valor por
  defecto.

Ese último paréntesis es el que no hay que saltarse: dice cuánto de la inercia
es dato y cuánto es supuesto.

### Opción 10 — Buscar una barra, generador o línea

Única pregunta: el texto a buscar. Busca por texto libre, sin distinguir
mayúsculas, y muestra lo que el modelo sabe de lo que encuentre:

- **Barras** — tensión, generación, carga y **todas las líneas conectadas** con
  su flujo visto desde esa barra y su cargabilidad.
- **Generadores** — potencia despachada, tecnología, placa en MVA e inercia,
  diciendo si la H es real o por tecnología.
- **Líneas** — flujo y `s_nom`, hasta 25. Ojo: este bloque **sólo sale si la
  búsqueda no casó con ninguna barra**. Buscando por el nombre de una
  subestación verás sus líneas en el bloque de barras, que es más completo;
  para listar líneas sueltas, busca por algo que no sea nombre de barra.

Es la vía para averiguar el nombre exacto de un generador antes de usarlo en la
opción 3.

### Opción 11 — Cobertura de datos: qué es dato y qué es supuesto

Dos listas. La primera cuenta **cuántas máquinas tienen declarado** cada
parámetro frente al total: inercia H (y qué porcentaje de la placa cubren),
estatismo, Q_min/Q_max, tecnología, y cuántas líneas son contrastables contra
PowerFactory.

La segunda lista **lo que sigue siendo literatura o supuesto propio**, con su
valor: el amortiguamiento de carga, la constante del gobernador, la reserva
primaria, el factor de potencia nominal, el límite de subexcitación, la caída
síncrona, la correlación espacial del BTM y las impedancias genéricas de los 16
autotransformadores.

Si vas a defender un resultado, ésta es la pantalla que te dice por dónde te
pueden preguntar.

### Opción 12 — Qué información falta

La contabilidad de la **energía que el modelo no ve**: la generación total del
reporte, los nodos sin camino trazable al STN, lo que el modelo informa y los
MW ocultos por el filtro. Lista los mayores nodos sin atribuir y termina con
**qué pedir, por orden de retorno**.

### Opción 13 — El BTM colombiano

Composición del BTM registrado por tecnología y por área. El BTM real son los
**AGPE, 740,83 MW según la UPME**, de los que el **96,9 % es solar** —y la
solar es lo único que se desconecta en bloque ante un hueco de tensión—. La
forma por subárea y tecnología sale de PARATEC; el total, de la UPME.

La tabla por área ordena por porcentaje de inversor, que en tres de las cinco
áreas es el 100 %.

Termina con tres cifras que conviene leer juntas: la penetración con inversor
sobre la demanda del modelo, la misma cifra añadiendo generación distribuida
(que es una sensibilidad, no el caso base) y el umbral a partir del cual el
EDAC actúa, con su horquilla. Si la primera supera al tercero, el sistema está
por encima del umbral.

Incluye un aviso explícito: **las plantas menores no son BTM** (CREG 086 de
1996), y hasta el 31-10-2026 este panel las sumaba.

### Opción 14 — Balance por área operativa

Generación, demanda, pérdidas e intercambio de cada una de las cinco áreas.
`InterP` positivo significa que el área exporta.

Debajo, un contraste con los **límites de importación que publica XM** en sus
informes de planeamiento operativo, con qué fija cada límite. No están en
PARATEC, y varios los fija la **estabilidad** y no el límite térmico, así que
el N-1 de este modelo —que sólo mira térmico y tensión— no los detecta por su
cuenta.

> Ojo al interpretarlo: el caso de PowerFactory del que parte el modelo declara
> una red futura, mientras que esos límites son de 2024. Una red reforzada
> admite más transferencia, así que exceder uno **no prueba** que el caso sea
> irrealizable.

---

## 5. Análisis

Estas cuatro reproducen los resultados del trabajo. Son las más lentas y las
que más conviene leer con el texto que las acompaña.

### Opción 15 — Umbral de penetración con inversor

Barre la penetración de BTM con inversor y busca **a partir de qué punto una
desconexión masiva llega a disparar el EDAC**.

Única pregunta: el % del BTM con inversor que se desconecta (por defecto `90`).

Recorre diez niveles de penetración (del 5 % al 40 %) y para cada uno imprime
cuántos MW se caen, el nadir y cuántos escalones del EDAC disparan. Lleva una
marca `<- dispara` **en cada fila en la que el EDAC actúa**, no sólo en la
primera: el umbral es el primer nivel marcado, y de ahí para abajo la columna
queda limpia.

**Esta opción lleva un aviso que no conviene saltarse.** El umbral depende del
punto de operación sobre el que se mida, y las dos medidas son correctas porque
describen sistemas distintos:

- sobre **el caso de referencia**, cuyo parque es el de 2042, el umbral central
  es el 5,15 % y Colombia (6,25 %) queda **por encima**;
- sobre un punto construido desde **el despacho real de XM**, donde la
  generación por convertidor es mucho menor, el umbral sube al 8-9 % y Colombia
  queda **por debajo**, con unos tres puntos de margen.

Lo que las dos sostienen a la vez no es una cifra sino una **trayectoria**: hoy
hay margen, y se estrecha a medida que la penetración por convertidor crece.
Para verlo, elige una fecha con `p` y vuelve a correr esto.

### Opción 16 — Sensibilidad del umbral

Cuánto se mueve ese umbral si cambian las hipótesis. **No pregunta nada.**

Imprime una tabla con cuatro hipótesis —el parque entero acoplado, una unidad
de más por planta, el mínimo que cubre el despacho (la del caso base), y esa
misma pero con la reserva del 3 % que exige la CREG— y para cada una la
capacidad síncrona, la H, la reserva y el umbral resultante.

Las dos primeras filas necesitan saber **cuántas unidades tiene cada planta**;
si el punto activo no trae ese dato, la tabla sale con dos filas en vez de
cuatro. Con los datos de PARATEC que trae el repositorio, salen las cuatro.

Las dos hipótesis que de verdad lo mueven son **cuántas unidades están
acopladas** y **cuánta reserva hay**, y ninguna de las dos es un dato: el
despacho por unidad no lo tenemos, y la reserva admite dos lecturas legítimas.

Sirve para saber si el resultado es firme o depende de lo que no sabemos. La
conclusión defendible no es una cifra sino la horquilla.

### Opción 17 — Barrido del peso lambda

Recorre el peso de la penalización por destruir BTM sano en **tres regímenes**:
el evento de BTM, la operación normal de hoy y la operación normal con
penetración alta.

Pregunta sólo si continuar, porque **tarda unos 8 minutos: son 24 MILP
completos** (cuatro valores de lambda × tres regímenes × dos semillas).

El resultado es el que justifica el valor por defecto de lambda en la opción 5:
calibrando sólo sobre el evento se elegiría un lambda alto, pero con penetración
alta y BTM que *no* se cae, a ese lambda al optimizador le sale más barato no
cortar y **el esquema deja de actuar**. El límite lo fija el régimen normal, no
el evento. La tabla marca con `<- el rele no corta` las filas donde eso pasa.

### Opción 18 — MILP frente al EDAC proporcional

Corre el mismo evento con los dos métodos y compara cuánta generación embebida
sana destruye cada uno **para conseguir el mismo alivio**. No pregunta nada: usa
un escenario fijo (sintético al 20 %, cae el 90 %, más 600 MW de pérdida
convencional).

Mira también la última columna: el MILP **concentra el corte en muchas menos
barras**, y eso deja la red más cargada que el reparto proporcional. Es el coste
del método, y conviene citarlo junto al beneficio.

---

## 6. Lo mismo desde la línea de órdenes

Las opciones de **Simular** y la 18 son envoltorios de un módulo, y el menú te
enseña la orden exacta antes de lanzarla. La tabla completa está en el
[README](README.md#comandos).

Las opciones de **Consultar** (9-14) y las de **Análisis** 15, 16 y 17 **no
tienen equivalente en línea de órdenes**: están escritas dentro de `menu.py` y
sólo se alcanzan desde el menú.

Lo que conviene saber de las que sí son módulos:

Los módulos de simulación aceptan las mismas banderas de punto de operación:

```bash
python -m stn.contingencias --fecha 2026-10-20 --periodo 19
python -m stn.deslastre_btm --diurno 11 --comparar
```

Sin ninguna bandera, corren sobre la punta nocturna.

`stn.comparar_casos` es la excepción, porque trabaja con **dos** puntos a la
vez. Cada columna se escribe como `punta`, `diurno:HORA` o `AAAA-MM-DD:PERIODO`:

```bash
python -m stn.comparar_casos --caso-a 2026-10-20:5 --caso-b 2026-10-20:19
```

Y para traer una fecha sin pasar por el menú:

```bash
python -m stn.fuentes.descarga_xm --fecha 2026-11-01
python -m stn.fuentes.descarga_xm --listar
```

---

## 7. Antes de citar una cifra

Cinco comprobaciones que evitan la mayoría de los errores de lectura:

1. **Mira de qué punto es.** Toda salida lleva su cabecera. Una cifra del
   diurno comparada con una de la punta sin decirlo es un error, no un
   hallazgo.
2. **Mira la cobertura si es una fecha.** Demanda sin ubicar significa que el
   EDAC dimensionó por debajo y el nadir sale optimista.
3. **Un nadir sólo vale si no hubo colapso.** Si el sistema cruza los 57,5 Hz,
   el mínimo de la traza es donde corta el integrador, no un nadir físico.
   Los módulos no lo publican en ese caso, a propósito.
4. **En el N-1, las tensiones son incrementales y las sobrecargas no.** Las
   barras fuera de banda se cuentan respecto al caso base; los elementos
   sobrecargados, en absoluto. No los sumes como si fueran la misma clase de
   cifra.
5. **Sin `--con-limites`, el colapso de tensión no puede aparecer.** Las barras
   PV sostienen su tensión con el reactivo que haga falta.

---

## 8. Problemas frecuentes

**`python` dice que la versión es 3.10 o menor.** PyPSA 1.x no publica ruedas
para versiones anteriores a 3.11 y la instalación falla con un muro de texto
que no menciona la versión. Comprueba con `py --list` (Windows) o
`python3 --version` (macOS/Linux) y crea el entorno con un intérprete más
nuevo. El modelo lo detecta al arrancar y lo dice en una frase.

**El menú sólo ofrece cuatro fechas.** Son las que trae el repositorio. La
quinta entrada de la lista —*otra fecha, descargarla de XM ahora*— trae
cualquier otra.

**Error de certificado al descargar una fecha.** Habitual detrás del proxy de
una empresa, que firma con una raíz propia que Python no conoce. Hay que
apuntar `SSL_CERT_FILE` a un almacén que sí la tenga; el mensaje de error da la
orden exacta para tu sistema. En Windows suele no hacer falta.

**No hay ninguna fecha en disco.** El descargador necesita al menos una para
heredar el directorio de PARATEC. Recupera las que trae el repositorio en
`data/xm/paratec/`.

**Una fecha antigua no pasa el filtro de cobertura.** Las anteriores a
septiembre de 2026 usan códigos de nodo que la tabla de equivalencias aún no
cubre. Úsalas sabiendo que el nadir saldrá optimista, o elige una más reciente.

**El barrido N-1 de la opción 4 dice que faltan resultados.** Esa comparación
lee los CSV ya generados. O corres antes el N-1 de cada punto, o vuelves a
llamarla pidiendo que lo corra ella (tarda varios minutos por columna).

---

## Para quien quiera verificar el modelo

El menú es la vía de **uso**. Las comprobaciones internas no están en él, pero
siguen existiendo como comandos:

```bash
python -m stn.validacion            # contraste contra PowerFactory
python -m tests.test_invariantes    # 34 invariantes del modelo
python -m tests.test_regresion_pf   # 18 regresiones contra el caso congelado
python -m tests.test_fuente_xm      # 52 de la fuente de datos de XM
python -m tests.verificar_sistema   # 44 comprobaciones externas
python -m stn.limites_reactivos     # curva de capacidad de reactivo
python -m stn.escenario_diurno      # cómo se deriva el caso diurno
```
