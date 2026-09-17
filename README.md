# RAG_CSJN_AI_ENG

Buscador público sobre la doctrina de la Corte Suprema de Justicia de la Nación en materia de
sentencias arbitrarias. Cualquiera entra, pregunta en lenguaje natural, y recibe una respuesta
con citas de fallos reales y sus links oficiales.

---

## Qué hace

Un equipo de cuatro agentes responde cada consulta, con un supervisor que decide quién sigue.

| nodo | qué hace | modelo |
|---|---|---|
| `supervisor` | elige el próximo paso y deja asentado el motivo | sí, con salida estructurada |
| `investigador` | busca doctrina en el corpus y arma una síntesis con sus citas | sí, ReAct |
| `verificador` | contrasta cada cita contra el padrón, contra lo que el investigador leyó y contra el texto que la respalda | **no** |
| `redactor` | escribe la respuesta usando solo lo verificado, con sus links | sí, ReAct |

**El verificador es código.** Un fallo está en el padrón o no está, y eso se computa. Poner un
modelo a juzgarlo mudaría la alucinación al que audita.

**Y comprueba tres cosas, no una.**

1. **Que la cita exista**, contra el padrón.
2. **Que el investigador la haya leído**: que el fallo haya salido de un fragmento que la
   búsqueda le sirvió en esta consulta.
3. **Que el pasaje con el que se sostiene esté en lo leído**: cada cita viene con una oración
   copiada del texto, y un pasaje que no figura en ningún fragmento servido es un pasaje
   inventado.

Las dos últimas faltaban. Con el corpus que el buscador llegó a tener —9.005 citas reales—,
«existe» era una barra baja: el buscador llegó a contestar sobre el impuesto al valor agregado con cuatro fallos
anteriores a que el IVA existiera, los cuatro ciertos y ninguno leído. Se los había dado una
herramienta que repartía las citas de una subsección entera —hasta 264 para una búsqueda cuyos
fragmentos traían una—. Esa herramienta se retiró: el investigador tiene una sola, la búsqueda,
que le devuelve cada fragmento con los fallos que ese fragmento cita y anota de dónde salió
cada uno y qué decía.

La tercera comprobación **se midió antes de dejarla rechazar**. Sobre 20 consultas reales: 100
citas verificadas y 1 sin respaldo, o sea 1 consulta de 20. Con ese número a la vista pasó de
señal a veto, y la cita sin respaldo sale de la lista de verificadas en vez de solo bajar el
veredicto: agotadas las tres correcciones el sistema publica sobre lo verificado, y una cita
que solo bajara `aprobado` volvería igual al redactor en esa última vuelta. Medido: así pasaba
en 2 de 20 consultas.

**El pasaje se le muestra al lector solo si sale de un fragmento que cita ese mismo fallo.**
Con la ficha mostrando el pasaje en producción, 4 de 14 citas publicadas traían una oración de
un documento distinto al que citaba el fallo: 243:190, citado en una nota sobre honorarios,
salió con un pasaje del suplemento de Decretos de Necesidad y Urgencia. La cita pasaba las tres
comprobaciones —el pasaje estaba en lo leído— y la ficha lo presentaba como su respaldo.

Se probó vetar con esa vara más estricta, y se midió sobre 20 consultas antes de dejarla: cero
pasajes ajenos, pero 31 correcciones contra 9, la latencia mediana de 18 a 40 segundos, y tres
consultas que el corpus responde —marcas y patentes, lesa humanidad, gravamen irreparable—
terminaron sin base. En los suplementos el fragmento que trata el tema casi nunca cita el fallo:
el 66% no cita ninguno, así que la vara estricta los dejaba sin nada con qué respaldar. Por eso
decide lo que se muestra y no lo que se publica: la cita con un pasaje ajeno sale con su
afirmación y su link oficial, sin el pasaje a la vista, y la telemetría cuenta cuántas son.

**Y esto es lo que le da al buscador la respuesta «no tengo esto».** Sin citas que sobrevivan
las tres comprobaciones no se llega al mínimo, y el desenlace es sin base suficiente. Una
consulta sobre jurisprudencia de la Corte en materia de criptomonedas —dentro del alcance, y
fuera de lo que el corpus trata— termina ahí.

**Ninguna URL sale de un modelo.** Los PDF de la Secretaría de Jurisprudencia traen los links
oficiales embebidos, anclados al texto de cada cita, y de ahí sale el padrón. Cuando un
documento no enlaza una cita, el link lo arma este código con la plantilla oficial de la Corte
a partir del tomo y la página que el corpus escribió.

**La respuesta sale o no sale.** El sistema se autocorrige hasta tres veces. Agotadas las
correcciones, escribe sobre las citas que sí verificaron si alcanzan el mínimo; si no llegan,
el buscador dice que no hay base suficiente en el corpus y registra la consulta.

El verificador rechaza la investigación entera cuando una sola cita es inventada, y ese rechazo
es lo que hace corregir. Una vez sin intentos, tirar dos citas ciertas por una inventada pierde
una respuesta buena sin proteger nada: el redactor solo recibe `verificadas`, y el control del
texto final lo compara contra esa misma lista. Se midió el caso: en la consulta sobre el
recurso extraordinario, el modelo propone el mismo fallo inexistente en las cuatro pasadas
—lo cita de memoria— mientras las otras dos citas verifican.

**Una cita es un fallo, no una afirmación.** El investigador propone el mismo fallo varias veces
cuando sostiene varios puntos con él, y eso está bien; el verificador lo agrupa por tomo y
página antes de contar. Contarlo una vez por afirmación mentía en las tres puntas: en «habeas
corpus colectivo» el corpus responde con Verbitsky y nada más, el investigador lo propuso cuatro
veces, y el sistema informaba «4 de 4 citas existen en el corpus» sobre un solo fallo, daba por
cumplido el mínimo con duplicados y mandaba a corregir tres veces a un redactor que escribía la
única cita que tenía, para terminar diciendo que no había base. Agrupadas, el rechazo dice «1 de
1» y vuelve al investigador, que es quien puede buscar más doctrina: la consulta publica en la
primera corrección con dos fallos distintos.

---

## El corpus

El **cuadernillo de doctrina sobre sentencias arbitrarias** de la Secretaría de Jurisprudencia
de la CSJN, en cuatro documentos: el concepto de arbitrariedad, sus causales, la improcedencia
del recurso y su trámite.

| | |
|---|---|
| documentos | 4 |
| fragmentos | 322 |
| subsecciones | 27 |
| citas en el padrón | 557, todas con su link oficial, que trae el propio cuadernillo |
| índice | 8 MB en disco |

### Las notas y los suplementos, fuera del índice

El corpus llegó a tener 128 documentos: sumaba 82 notas de jurisprudencia y 42 suplementos.
Salieron del índice y siguen en `catalogo.json` con `indexar: false`, junto con toda la ingesta
de PDF, así que volver a sumarlos es cambiar esa bandera y reconstruir. Lo que sigue de esta
sección documenta ese corpus y esa ingesta, medidos con ellos adentro.

| fuente | documentos | páginas | fragmentos |
|---|---|---|---|
| Cuadernillo de doctrina sobre sentencias arbitrarias | 4 | — | 322 |
| Notas de jurisprudencia | 82 | 654 | 1.408 |
| Suplementos temáticos | 27 | 4.513 | 8.694 |
| Suplementos del Archivo Histórico | 15 | 5.680 | 15.451 |
| **Total indexado** | **128** | **10.847** | **25.875** |

El padrón llegó a **9.005 citas**, todas con su link oficial: 3.338 salían de los hipervínculos
que traen los PDF y 5.667 se armaban con la plantilla de la Corte a partir del tomo y la
página. Ese índice ocupaba **436 MB** en disco y **194 MB** comprimido como artefacto.

El **Archivo Histórico** son 15 suplementos de ediciones 2009-2016 —Competencia Originaria,
Decretos de Necesidad y Urgencia, Habeas Corpus, Habeas Data, Derecho Electoral, Derecho del
Trabajo, Movilidad Jubilatoria, Marcas y Patentes, entre otros—. Doce cubren temas que ningún
otro documento del corpus toca, y los otros tres son ediciones anteriores de suplementos que
siguen publicados. Ninguno trae hipervínculos, así que sus citas se recuperan del texto y su
URL se sintetiza, igual que las de la serie Ambiental: de ahí sale el salto del padrón. Son
más de la mitad de los fragmentos y aportan seis de las consultas del set etiquetado, que
sirven para comprobar que quedaron alcanzables.

### De dónde salen

El sitio es una SPA: `/homeSJ/notas/inicia` y `/homeSJ/suplementos/inicia` son rutas que el
JavaScript reescribe en la barra de direcciones, y los datos viajan por AJAX contra endpoints
JSON. Los documentos son PDF servidos directo.

```
GET https://sj.csjn.gov.ar/homeSJ/notas/                                  → JSON
GET https://sj.csjn.gov.ar/homeSJ/notas/nota/{id}/documento               → PDF
GET https://sj.csjn.gov.ar/homeSJ/suplementos/categoria/{cat}/suplementos → JSON
GET https://sj.csjn.gov.ar/homeSJ/suplementos/suplemento/{id}/documento   → PDF
```

Las 17 categorías de suplementos se leen del HTML de la home y se comparan contra las
declaradas en `constantes.py`: una discrepancia corta la ingesta, porque indexar de menos sería
silencioso.

### Cómo se lee un PDF

Cuatro decisiones, todas medidas contra los documentos reales:

- **Las notas van a dos columnas.** `page.get_text()` respeta el orden de lectura columna por
  columna. `get_text(sort=True)` intercala las dos y arruina el texto.
- **La cita sale de la URL cuando la URL la trae.** `buscarTomoPagina?tomo=348&pagina=821` son
  parámetros que escribió la propia Secretaría; el texto anclado queda como control cruzado y
  los desacuerdos se cuentan en el manifiesto. Medido sobre el peor documento: 3 desacuerdos
  sobre 356 links, 0,8%.
- **Las anotaciones cubren más citas que el regex.** En las 82 notas: 2.440 citas únicas por
  hipervínculo contra 1.720 por patrón sobre el texto, de las cuales 1.600 ya venían por
  hipervínculo. El regex aporta el complemento.
- **El desacuerdo entre el ancla y la URL es de 47 en los 124 documentos**, menos del 1% de
  los links. Cuando difieren gana la URL, que es un parámetro y no texto interpretado.

Veintinueve documentos —los quince tomos del Archivo Histórico, la serie Ambiental, las
ediciones viejas de Fallos Relevantes, «Citas de doctrina»— no traen un solo link: sus citas se
recuperan del texto y su URL se sintetiza. Son la mayor parte del corpus por páginas, y por eso
los links sintetizados superan a los embebidos.

**No toda URL embebida sirve desde afuera.** Cuatro correcciones, todas encontradas revisando el
padrón host por host:

- Las que apuntan a `sjintranet.csjn.gov.ar` con el path del servicio público se reescriben al
  host de afuera; las de cualquier otro path interno se descartan.
- Una URL con **puerto explícito** es un servicio interno —seis citas apuntaban a
  `csjn14.csjn.gov.ar:7003`— y desde afuera no abre.
- El aplicativo **`tomosFallos.do` de `sj.csjn.gov.ar` fue retirado**: las 17 citas que lo
  enlazaban responden 200 y sirven la home del sitio. El status no alcanza para detectarlo; hay
  que comparar el cuerpo contra esa home, que es como se comprobaron las diecisiete.
- Las 304 que venían en **`http` pasan a `https`**: un buscador público que enlaza en claro
  degrada la conexión de quien lo sigue. Los tres hosts que quedan lo soportan.

Una cita cuyo link se descarta no se queda sin link: entra al padrón con URL vacía y se le arma
la oficial con la plantilla de tomo y página. Después de esto el padrón usa dos hosts,
`sjconsulta.csjn.gov.ar` (8.872) y `sjservicios.csjn.gov.ar` (133), todo en `https`.

### El filtro de calidad

Dos documentos salen mal del PDF: una nota cuya fuente no trae mapa a Unicode —su texto son
bytes de control— y un suplemento de manuscritos escaneados, cuya capa de texto es ruido de
OCR. A eso se suman los números de página sueltos y los renglones de índice con puntos.

El ruido no solo no aporta: `bm25()` normaliza por longitud, así que un fragmento de tres
caracteres que matchea un término se lleva un puntaje enorme y desplaza a la doctrina.

Un fragmento entra al índice con **120 caracteres y 40% de letras** como piso. Los dos números
salen de medir el corpus: la proporción de letras tiene mediana 80% y percentil 5 en 60%, y
entre 55% y 62% viven las listas de citas y las tablas de «Citas de doctrina», que son valiosas
justamente por ser densas en números. El corte deja todo eso adentro, saca 275 fragmentos de
ruido, y cuesta **2 citas de las 9.005** del padrón.

### Las subsecciones

Un suplemento de 862 páginas son ~1.500 fragmentos. La subsección es la procedencia que el
sistema muestra debajo de cada cita, y con una sola para todo el documento esa procedencia no
diría nada. Se resuelven en cascada, y el método elegido queda anotado en la ficha de cada
documento:

Sobre el corpus real: 24 documentos se segmentaron por tipografía, 1 por outline y 99 por
bloques de páginas. El fallback domina porque 75 de las 82 notas tienen 7 páginas de mediana
y un solo tema, así que su título ya dice de qué tratan, y porque los tomos del Archivo
Histórico son transcripciones sin jerarquía tipográfica. Entre los suplementos temáticos, que
son los largos y sí están titulados, 16 de 27 se segmentaron por tipografía.

1. El outline del PDF, si sus entradas son de menos de 25 páginas.
2. Los títulos que marca la tipografía: tamaño por encima de la mediana del cuerpo —ponderada
   por caracteres, para que el cuerpo domine— o negrita, con un filtro de forma que descarta
   el renglón suelto y la cita destacada.
3. Bloques de páginas: `"págs. 41-50"`.

El nombre lleva siempre el prefijo del documento (`"Migraciones 2024 · 13.3 Beneficio de
litigar sin gastos"`). Con ~800 subsecciones, los "Introducción" de distintos suplementos
colisionarían sin él.

---

## Arquitectura

Un servicio, un volumen.

```
Railway
└── servicio web (FastAPI + grafo en proceso, uvicorn --workers 1, numReplicas 1)
    ├── GET  /                        interfaz
    ├── POST /consultas               202 {consulta_id}
    ├── GET  /consultas/{id}/stream   SSE
    ├── GET  /consultas/{id}          resultado terminado
    ├── GET  /salud  /metricas  /corpus  /acerca-de  /privacidad
    └── volumen /datos
        ├── indice/          Chroma + lexico.sqlite3 + MANIFIESTO.json   (solo lectura)
        └── estado.sqlite3   cupos, gasto, registro de consultas          (escritura)
```

### La recuperación es híbrida

`EnsembleRetriever` fusiona por rango recíproco dos retrievers que ven el mismo corpus:

- **Léxico**, sobre FTS5 de SQLite. Encuentra la cita escrita literal, que un embedding diluye
  entre párrafos parecidos. `tokenchars ':'` mantiene `316:2343` como un token.
- **Vectorial**, sobre Chroma con distancia coseno. Encuentra la consulta parafraseada.

Los dos pesan igual.

El índice léxico tiene tres columnas —texto, citas y **título**— y el título pesa el triple.
Sin esa columna, una consulta que nombra un tema compite solo contra el cuerpo de los
fragmentos, y gana el que repite esas palabras aunque sea de otra cosa: una pregunta sobre el
recurso extraordinario traía transcripciones de fallos ambientales, que lo mencionan en cada
página, antes que la doctrina que lo explica. Las palabras vacías tampoco entran a la búsqueda,
por lo mismo.

Medido con 26 consultas etiquetadas (`scripts.medir_lexico`), precisión en el top-4:

| | sin la columna de título | con ella |
|---|---|---|
| Léxico | 74% | **96%** |
| Vectorial | 75% | 75% |
| Híbrido | — | **88%** |

El peso exacto del título es indiferente entre 2 y 12 —los cinco dan lo mismo— y en 0 se cae a
74%. El 3 está en el medio de esa meseta.

**El híbrido queda por debajo del léxico en esta tabla, y aun así los dos lados pesan igual.**
Las consultas etiquetadas nombran los temas con el vocabulario del corpus, que es la cancha del
léxico: cada una declara qué documentos la responden, y esa etiqueta sale del título del
documento. Sobre consultas parafraseadas —la misma doctrina dicha con otras palabras— la
relación se da vuelta: 50% el léxico contra 66% el vectorial. Subir el peso léxico a 0,6 o más
colapsa el ensamble a puro léxico y pierde ese segundo caso, que es el que trae cualquiera que
no conozca la jerga. La medición dice qué gana cada rama, no cuál sacrificar.

La búsqueda vectorial va partida en dos spans medibles —embeber la consulta es red, buscar el
vecino es trabajo local— porque medirlas juntas da un número que no se puede atribuir.

### El padrón vive en disco

El padrón de citas, los nombres de subsección y las citas por subsección se calculan **durante
la ingesta** y se guardan en `lexico.sqlite3`. En consulta solo se leen.

**Una conexión, un lock, y toda lectura pasa por el mismo camino.** La conexión se abre con
`check_same_thread=False` porque las consultas corren en `asyncio.to_thread` y el hilo del pool
cambia entre llamadas; esa bandera apaga el control de sqlite3 y deja la exclusión a cargo de
quien la usa. Varios caminos entran desde hilos distintos —los cuatro recuperadores del
ensamble, dos pools por dos lados, y el padrón que leen el verificador y la búsqueda—, y con
tres consultas concurrentes se pisaban. Reproducido sin llamar a ningún modelo: ocho hilos sobre esta clase
daban `IndexError: tuple index out of range` y `bad parameter or other API misuse` desde adentro
de `fetchall`, y eso se llevaba puesta la consulta entera **después** de cobrarle el cupo al
visitante. Serializar las lecturas no cuesta: cada una es de microsegundos contra un índice en
disco, y lo caro de una corrida son las llamadas al modelo.

Antes se armaban leyendo el corpus entero a RAM en el arranque. Con 322 fragmentos eso costaba
~100 ms; con 7.400 son cientos de MB antes de atender la primera consulta, y el proceso no
entra en el contenedor.

**La clave del padrón es `tomo:pagina`, y el que escribe la impone.** Todo lector normaliza
—`existe`, `link` y el evento de cita pasan por `normalizar_cita`—, así que una entrada
guardada con la forma cruda queda inalcanzable aunque tenga su URL oficial adentro. Cada fuente
escribe la cita a su manera: los hipervínculos de los PDF ya vienen normalizados, y el
cuadernillo la escribe como la imprimió, «Fallos: 112:384». Medido sobre un índice construido
sin este control: 557 claves sin normalizar, **330 de ellas sin gemela**, o sea 330 citas
reales del corpus que el verificador daba por inventadas —rechazando la investigación entera y
quemando una corrección— y que, de pasar, se publicaban sin link. Por la misma puerta entraban
86 entradas que no son citas: números de expediente, nombres de caso y etiquetas de voto como
«(Disidencia del juez Lorenzetti)», texto de anclas que el corpus no puede respaldar. El padrón
es de tomo y página, y lo que no lo trae queda afuera.

### El stream

`POST /consultas` y `GET /consultas/{id}/stream` van separados por tres razones: `EventSource`
del navegador solo hace GET; el cupo se cobra en un único lugar; y una reconexión vuelve a leer
sin volver a cobrar ni a ejecutar.

Los eventos:

```
event: estado    {"fase": "investigando", "detalle": "Buscando doctrina en el corpus."}
event: estado    {"fase": "verificado",   "detalle": "5 de 7 citas existen en el corpus."}
event: texto     {"delta": "La Corte ha sostenido…"}
event: cita      {"fallo": "Fallos: 311:2437", "url": "…", "subseccion": "…"}
event: final     {"publicado": true, "calidad": {…}, "duracion_ms": 38210}
event: sin_base  {"motivo": "…", "diagnostico": {…}}
```

El anuncio de cada especialista sale del update del **supervisor**, que llega antes y trae
`siguiente`. `stream_mode="updates"` emite después de que un nodo corre, así que "buscando
doctrina" llegaría con la búsqueda ya terminada.

El stream se pide con **`subgraphs=True`**, y eso es lo que hace visible al redactor. Los tres
especialistas son agentes ReAct, o sea grafos compilados aparte que corren adentro de su nodo:
sin esa bandera el stream de mensajes solo trae al supervisor, que es el único que llama al
modelo desde el grafo de arriba. Medido: 355 chunks del redactor con la bandera, cero sin ella.
Los `values` y los `updates`, en cambio, se toman solo del grafo de arriba: los del subgrafo
dejarían el estado final con el del agente en vez del orquestador.

Que el navegador se vaya deja la corrida andando: el visitante ya gastó una consulta de su cupo
y puede volver a `GET /consultas/{id}`. Los eventos quedan en una lista acotada, y
`Last-Event-ID` dice desde dónde reproducir. **Es el id del último evento que el cliente
recibió**, así que el servidor sigue desde el siguiente; tomándolo como punto de partida, cada
reconexión repetía ese evento —un delta de texto duplicado, o una cita pintada dos veces—.

Del lado del navegador, una caída de transporte y un error del servidor llegan **al mismo
listener**: EventSource despacha las dos cosas como `error`, y lo que las distingue es que la
del servidor trae `data`. Cerrar la fuente en las dos abandonaba una corrida que el servidor
sigue corriendo y cuyos eventos están guardados para reproducir. Ahora una caída transitoria
deja que el navegador reconecte solo —con un aviso que se va en cuanto vuelve a llegar un
evento— y solo se corta cuando `readyState` queda en `CLOSED`.

Dos detalles de lo que se ve mientras tanto. **Cada anuncio de fase abre una redacción nueva**,
y el texto que llega después la reemplaza en vez de agregarse: sin eso, una corrección
concatenaba el borrador viejo con el nuevo y quien miraba veía la respuesta dos veces, pegadas.
Y cuando la corrida termina sin base, **el borrador se borra**: es exactamente lo que el
sistema decidió no publicar, y dejarlo debajo del aviso lo ofrece como respuesta. Un 409 trae
el id de la corrida que ya está en vuelo, y la página se engancha a ese stream en vez de dejar
esperando sin nada en pantalla.

Las fichas de cita y la tabla del corpus se arman **con nodos y no con plantillas de HTML**.
Los valores que se insertaban en crudo hoy son seguros —`fallo` sale de `normalizar_cita`, que
devuelve dígitos, y las URLs salen del padrón, que construye la ingesta—, pero eso es un
invariante que vive en otros módulos: con nodos, la página no depende de que nadie lo rompa.

---

## Los límites

### De tema

El alcance es la doctrina de la Corte sobre sentencias arbitrarias. Dos capas, antes de crear
la corrida, en `app/servicio/admision.py`:

1. **Piso de similitud** sobre el corpus (~USD 0,000002). Frena lo que no es jurídico.
2. **Clasificador** de una llamada con salida estructurada (~USD 0,00006). Separa la
   arbitrariedad del resto de la jurisprudencia de la Corte, que es donde las similitudes se
   pisan.

Se admite cuando las dos pasan. Calibrado contra el índice del cuadernillo con 30 consultas en
tema y 30 fuera, doce de ellas sobre temas de la Corte que el corpus tuvo antes: las consultas
de tema arrancan en 0,487 de similitud y las no jurídicas no pasan de 0,381, así que el umbral
quedó en 0,40. Las dos capas juntas admiten 30 de 30 y rechazan 30 de 30. La primera versión
del prompt rechazaba dos consultas del cuadernillo que no nombraban la palabra
«arbitrariedad»; el prompt ahora lista la estructura del cuadernillo. Una consulta fuera de tema ya falla cerrada sin este control
—cero citas, el verificador rechaza, tres reintentos—; lo que la admisión evita es pagar
cuarenta segundos para llegar ahí.

### De uso

| contador | tope | ventana |
|---|---|---|
| consultas por visitante | 5 | 24 h rodantes |
| consultas por IP | 15 | 24 h rodantes |
| intentos por visitante | 20 | 24 h rodantes |
| intentos por IP | 60 | 24 h rodantes |
| consultas del sitio | 50 | día calendario |
| gasto | USD 30 | mes |

El **intento** se cobra siempre, incluso a lo que la admisión rechaza: sin ese contador,
martillear consultas fuera de tema sería gratis.

**Cada cuenta se mira y se cobra en la misma transacción.** Contar y anotar eran dos
operaciones, y entre el control y el cobro de la consulta hay una llamada de red —la
admisión—: varios pedidos leían el mismo número, lo encontraban por debajo del tope y cobraban
todos. El tope se pasaba disparando pedidos a la vez, que es justo lo que hace quien lo quiere
pasar. Medido con cinco pedidos simultáneos contra un tope de dos: pasa el que queda, los
otros reciben 429.

**Ninguna espera es infinita.** Cada llamada al proveedor tiene 60 segundos y la corrida
entera 180; los dos números salen de lo medido —una consulta publicada tarda entre 18 y 33 s—
con holgura grande, porque lo que tienen que cortar es lo que se colgó y no lo que tarda. Sin
ellos, el SDK espera diez minutos por llamada y la corrida no termina nunca: tres colgadas
tapaban el semáforo media hora larga, con la cola detrás y el cupo de cada uno ya cobrado. El
tope va adentro del semáforo, sobre el trabajo y no sobre la espera, así una corrida encolada
no se cae por haber esperado su turno.

**Se cobra contra la cookie y contra la IP.** Colgado solo de la cookie no contaba nada: quien
no la devuelve recibe una nueva en cada request, así que sus intentos arrancaban siempre en
cero. Y el tope de consultas no lo alcanzaba, porque la consulta se cobra recién cuando la
admisión aprueba: una consulta rechazada paga un embedding y una llamada al clasificador, y era
gratis repetirla sin límite. La cookie sigue siendo el primer control porque es la que reparte
con equidad; la IP es el piso que hace que el tope exista, con la misma proporción de tres a uno
que el de consultas para no castigar a un estudio entero detrás de un NAT.

**La admisión paga y se anota.** Corre en toda consulta, la admita o no, y su costo entra al
gasto del mes como el de cualquier corrida: sin eso, el camino más barato de martillear era
también el único invisible para el freno por presupuesto. Medido, una llamada al clasificador
son USD 0,00006. El embedding del piso de similitud queda afuera de esa cuenta —no pasa por el
callback de LangChain— y es unas cien veces más barato.

**Un modelo que no esté en la tabla de precios avisa la primera vez que aparece.** Cero dólares
se lee igual que «no gastó nada»: el gasto se queda quieto, el freno no dispara nunca, y desde
afuera el sistema parece gratis. Cambiar `MODELO_SUPERVISOR` por uno sin tarifar alcanzaba para
eso, y nada lo delataba.

La cuota por visitante **reparte con equidad**; quien quiera eludirla puede. Lo que protege el
presupuesto es el techo diario del sitio y el corte por gasto, que se calcula con el consumo
real de tokens y no con una estimación.

### De privacidad

El registro guarda el texto de la consulta, la respuesta, las citas, la latencia y el costo.
**No guarda** IP, user agent, referer ni la cookie del visitante: para los cupos se guarda
`sha256(SAL + valor)`, durante 48 horas. Rotar `SAL_VISITANTE` olvida a todos.

**Sin sal el servicio no arranca**, y es lo único que el arranque exige sin red de contención
—un índice que falta se informa por `/salud` y el sitio sigue sirviendo la interfaz—. La razón
es que esa promesa tiene que ser cierta: con una sal conocida, recorrer las 4.300 millones de
IPv4 alcanza para revertir el hash, y `pulsos` pasaría a ser un padrón de direcciones reales de
quien usó un buscador de jurisprudencia. Por eso `SAL_VISITANTE` no tiene default: un default
acá sería un secreto publicado en un repositorio público. El largo mínimo de 16 tapa el otro
modo de falla, que hace el mismo daño: una sal corta escrita a mano en el panel del proveedor
se adivina igual que un literal publicado.

`anonimizar()` reemplaza por `[dato removido]` lo que reconoce como DNI, CUIT/CUIL, correo y
teléfono, antes de que el texto toque el disco. Un nombre propio en prosa no se distingue de la
carátula de un fallo; para eso está el aviso visible sobre el cuadro de búsqueda.

**El filtro vive en `registro.cerrar`, no en cada llamador.** Solo la consulta pasaba por él;
todo lo demás entraba crudo. El caso concreto es `motivo_inadmision`, que lo escribe el
clasificador «en una oración, dirigida a quien preguntó» y cuya causal más frecuente es pedir
asesoramiento sobre un caso propio: o sea justo las consultas donde alguien escribió un nombre,
un DNI o un expediente, devueltas en prosa y guardadas doce meses. El traceback de una corrida
fallida arrastra lo mismo.

**La respuesta conserva sus links oficiales, y por eso el filtro no es uno solo.**
`idDocumento=6952172` son siete dígitos, o sea la forma exacta de un DNI: aplicado a ciegas, el
filtro se los come y el link deja de abrir. Medido sobre las respuestas ya guardadas, pasaba en
3 de 15, y `/consultas/{id}` las devuelve tal cual a quien reabre el enlace. Cada columna se
filtra según de dónde viene su texto: lo que escribe el visitante o un modelo sobre él va con el
filtro entero —ahí un link con un DNI adentro es justo lo que hay que tapar—, y la respuesta
conserva los links de la Corte, que los arma este código desde el padrón.

**El filtro protege el registro, no la salida.** El texto viaja a OpenAI tal como se escribió
—una vez para la admisión y otra para responder—, y a LangSmith cuando las trazas están
encendidas. La página de privacidad lo dice con esas palabras: decía «sin terceros» en la
sección de cookies, que es cierto de las cookies y se lee como si el texto no saliera del
sitio.

La tabla `consultas` no lleva ningún identificador de persona, y eso es lo que hace publicable
`/metricas`.

---

## Correr el proyecto

### Requisitos

Python 3.12 o superior. El `.env` vive **fuera del árbol del repositorio**; `find_dotenv` lo
encuentra subiendo desde el proyecto, y `ARCHIVO_ENV` permite apuntar a otra ruta.

```bash
python -m venv C:\rag_csjn\.venv        # fuera de OneDrive, igual que el índice
C:\rag_csjn\.venv\Scripts\pip install -r requirements.txt
cp .env.example .env                    # y completar, fuera del repo
```

**El entorno virtual va fuera del árbol sincronizado**, por la misma razón que el índice y la
caché. Un venv son cientos de miles de archivos chicos, y OneDrive los pierde: uno adentro del
árbol se quedó sin 15 de los 3.056 archivos que el `RECORD` de `openai` declara, con lo que la
suite dejó de importar. Nada en el código lo delata —la versión instalada seguía siendo la
pinneada—, así que conviene comprobarlo contra el `RECORD` cuando aparezca un `ModuleNotFoundError`
de un módulo interno de una dependencia.

### Construir el índice

Cinco etapas con caché. Correrlo dos veces sin cambios en la fuente no descarga, no extrae y no
gasta un solo embedding: lo que decide es el sha256 del PDF, que es la única señal de cambio
que los endpoints exponen. Un arreglo en el código de extracción no mueve ese sha256, así que
para que llegue al índice hay que borrar la caché de texto de los documentos afectados.

```bash
python -m scripts.construir_indice                        # los 124 documentos
python -m scripts.construir_indice --solo nota-224        # unos pocos
python -m scripts.construir_indice --etapa segmentar      # hasta una etapa, sin claves
python -m scripts.verificar_indice                        # antes de publicarlo
python -m scripts.empaquetar_indice                       # el artefacto y sus variables
```

Las tres primeras etapas son HTTP y PyMuPDF: corren sin ninguna clave. La que embebe la exige.

### Levantar el servicio

```bash
uvicorn app.api.main:app --reload
```

`--workers 1` y `numReplicas: 1` son parte del diseño: los contadores de cupo y el registro de
corridas viven en memoria del proceso.

### Los tests

```bash
pytest
```

437 tests. Corren sin red, sin claves y sin servicios: un fixture autouse parchea
`socket.socket.connect` y convierte cualquier salida a la red en un fallo del test. El PDF de
`test_pdf.py` se construye en memoria con PyMuPDF, así que no hay binario versionado que nadie
sepa reproducir.

---

## Desplegar en Railway

1. Construir y verificar el índice local.
2. `python -m scripts.empaquetar_indice` → publicar `indice.tar.zst` como asset de un Release.
3. Crear el servicio, montar un volumen en `/datos`, cargar las variables.
4. Desplegar. El arranque atiende enseguida y baja el artefacto en paralelo.

**El proceso atiende primero y prepara el índice después.** Mientras el arranque no termina,
uvicorn no abre el socket: bajar ahí adentro los ~200 MB dejaba el puerto cerrado hasta diez
minutos —el timeout de la descarga— y el healthcheck de la plataforma, que corta a los cinco,
marcaba el despliegue como fallido; el contenedor siguiente volvía a empezar la descarga desde
cero. Ahora la descarga corre en una tarea de fondo, `/salud` informa `indice: "descargando"` y
la interfaz explica que el corpus está cargando.

**El healthcheck mira `/vivo`, no `/salud`.** `/salud` contesta 503 mientras no haya índice, que
es lo correcto para la interfaz y lo contrario de lo que un healthcheck necesita: con `/salud`
ahí, un artefacto que no baja tumbaba el despliegue entero, incluido el código que sí estaba
bien. Lo que decide si el deploy sirve es que el proceso conteste; si el buscador puede
responder lo dice `/salud`, y eso lo mira una persona.

**Una descarga que falla deja el servicio en pie.** El arranque atrapaba una lista de tipos que
no incluía ninguno de los que la descarga puede tirar —`HTTPError`, `HTTPStatusError`,
`ConnectError`, `ZstdError`, `TarError`, ninguna es `OSError`—, así que un Release movido de
lugar mataba el proceso. Comprobado en el contenedor con una `INDICE_URL` inexistente: `/vivo`
responde 200 a los cuatro segundos, `/salud` dice `ausente` con la causa, y la consulta recibe
un 503 que se entiende.

`INDICE_HUELLA` es el sha256 del `.tar.zst`, y el arranque lo usa para dos cosas distintas:
comprobar lo que bajó antes de extraerlo, y decidir si hace falta bajarlo. Lo segundo se
resuelve contra la marca que dejó la descarga anterior en el volumen, no contra la huella del
manifiesto: esa resume los sha256 de los PDF de origen, así que no cambia cuando cambia el
código de extracción y además nunca puede ser igual a un sha256 completo. Comparando esa,
el servicio se bajaba los cien megabytes en cada arranque.

**Con qué se construyó el índice lo dice el índice.** El modelo de embeddings, las dimensiones
y el nombre de la colección salen del manifiesto, y el entorno solo puede contradecirlos: si
declara otra cosa, el arranque corta ruidoso; si no declara nada, manda el manifiesto. Antes
mandaba el entorno, y faltar `DIMENSIONES_EMBEDDINGS` era la peor falla posible — la guarda
quedaba desarmada por su propio `if`, el proveedor devolvía vectores de 1536 contra una
colección de 512, y el arranque no se caía. `/salud` contestaba «listo», la plataforma marcaba
el deploy sano, y cada consulta moría en la recuperación después de haber cobrado el cupo del
visitante y pagado la llamada de admisión.

Variables del servicio:

```
OPENAI_API_KEY          DIRECTORIO_INDICE=/datos/indice
LLM_PROVIDER            ARCHIVO_ESTADO=/datos/estado.sqlite3
MODELO_EMBEDDINGS       INDICE_URL          INDICE_HUELLA
DIMENSIONES_EMBEDDINGS  SAL_VISITANTE
CONSULTAS_POR_VISITANTE=5   CONSULTAS_POR_IP=15   CONSULTAS_POR_DIA=50
INTENTOS_POR_VISITANTE=20   CONSULTAS_CONCURRENTES=3   PRESUPUESTO_USD_MES=30
UMBRAL_SIMILITUD        LANGSMITH_API_KEY   PROYECTO_LANGSMITH
TOKEN_RESPALDO          INTENTOS_POR_IP=60
```

El modo serverless queda apagado: el arranque carga el índice, y un cold start rompería la
primera consulta.

**El contenedor arranca como root y baja privilegios enseguida.** El volumen persistente lo
monta la plataforma y su dueño lo decide ella: Railway lo entrega como `root:root` con modo
755, así que el proceso no-root no puede escribir adentro y el servicio moría con
`sqlite3.OperationalError: unable to open database file` antes de abrir el puerto. El
`entrypoint.sh` corrige el dueño del punto de montaje y hace `exec setpriv` a `buscador`: root
vive lo que tarda un `chown`. **Un volumen nombrado de Docker no reproduce el problema** —hereda
el dueño del directorio de la imagen—, así que probarlo local pasa y el despliegue falla igual;
para verlo hay que montar un directorio ajeno, o desplegar.

**Las tres rutas de datos las trae la imagen**, no solo el panel del proveedor: el `Dockerfile`
fija `DIRECTORIO_INDICE`, `DIRECTORIO_CACHE` y `ARCHIVO_ESTADO` sobre `/datos`. Los defaults
del código son los de desarrollo y caen en `RAIZ/datos`, que adentro de la imagen es
`/srv/datos`: existe, es escribible y muere con el contenedor. Eso no falla —el servicio
arranca igual— y cada despliegue se lleva los cupos, el registro y el gasto acumulado, con lo
que el techo de USD 30 al mes pasa a ser un techo por contenedor. El arranque imprime en qué
rutas quedó parado, que es la forma barata de ver esto en el log.

### El respaldo

`estado.sqlite3` es lo único irreemplazable del despliegue —el índice se reconstruye con un
script—: adentro están el registro de consultas, que es el tercer objetivo del proyecto, los
cupos y el gasto del mes.

```bash
python -m scripts.respaldar https://el-buscador.up.railway.app --destino ./respaldos
```

`GET /respaldo` devuelve una copia detrás de `TOKEN_RESPALDO`. **Sin esa variable la ruta no
existe**, y con un token que no coincide contesta lo mismo que una ruta inventada: 404, porque
anunciar que hay algo detrás solo le sirve a quien busca. La comparación es en tiempo
constante.

La copia la hace SQLite con su API de respaldo en caliente, no copiando el archivo. La base va
en WAL, así que el `.sqlite3` solo no es la base —lo último escrito vive en el `-wal`— y
copiarlo a mano da un respaldo al que le puede faltar justo lo que se quería guardar.

---

## Lo medido

Sobre el cuadernillo, con 20 consultas de arbitrariedad corridas de punta a punta contra el grafo:

| | |
|---|---|
| Publicadas | 19 de 20; la restante reveló un error del verificador, ya corregido y repetido |
| Latencia mediana de una consulta | 18 s |
| Costo mediano por consulta | USD 0,0026 (`gpt-4o-mini`) |
| Correcciones en las 20 | 6 |
| Citas publicadas con su pasaje a la vista | 61 de 61, ninguna con un pasaje de otro fallo |
| La subsección que responde entra al top-6 | 19 de 20 consultas etiquetadas |
| Admisión, 30 consultas en tema y 30 fuera | 30 de 30 admitidas y 30 de 30 rechazadas |

Medido con el corpus de 128 documentos, antes de que quedara solo el cuadernillo:

| | |
|---|---|
| Memoria del proceso con el índice abierto | 294 MB |
| Tres consultas en paralelo | 35 s, las tres publicadas |
| 512 dimensiones contra 1536 | 84% de solapamiento del top-4 |
| FTS5 contra rank-bm25 | 92% contra 89% de cobertura de términos en el top-3 |
| Precisión del híbrido, 26 consultas etiquetadas | 88% en el top-4 |

## Alcance

- El sistema comprueba que una cita **exista en el corpus**, que el investigador **la haya
  leído** en esta consulta, y que el pasaje con el que se sostiene **esté en lo leído**. Que el
  fallo, leído entero, sostenga la afirmación lo comprueba quien lee.
- El control alcanza a las citas con números. Una referencia como "la doctrina de Colalillo"
  queda afuera; el prompt del redactor la prohíbe.
- Las referencias por expediente y fecha (`C. 623. XLV. "Compañía Financiera", 10/12/2013`),
  ~10% de los links de algunos suplementos, quedan fuera del padrón, que es de tomo y página.
- Los embeddings son de OpenAI aun cuando el chat corra con Anthropic: Anthropic no ofrece una
  API de embeddings.
- Las respuestas son un punto de partida para leer los fallos originales.
- **El modelo puede citar de memoria y no ceder.** Un fallo inexistente que el investigador
  repite en cada corrección no llega nunca a la respuesta, y tampoco impide publicarla: el
  sistema escribe sobre las citas que sí verificaron. Lo que no se comprueba es que el fallo
  citado **sostenga** la afirmación que lo invoca; eso lo comprueba quien lee.

## Licencia

Copyright (C) 2026 Felipe Boivin.

Este programa es software libre: podés redistribuirlo y modificarlo bajo los términos de la
**GNU Affero General Public License versión 3**, tal como la publica la Free Software
Foundation. Se distribuye con la esperanza de que sea útil, pero **sin ninguna garantía**, ni
siquiera la garantía implícita de comerciabilidad o aptitud para un propósito determinado. El
texto completo está en [LICENSE](LICENSE), copiado sin cambios de
[gnu.org](https://www.gnu.org/licenses/agpl-3.0.txt).

**La AGPL es la licencia que corresponde acá, y no una elegida al pasar.** PyMuPDF, con el que
se leen los PDF de la Secretaría, es AGPL: un trabajo combinado con él se distribuye bajo esos
mismos términos. `pypdf` (BSD) es la alternativa si eso alguna vez molesta, con menos fidelidad
en la extracción de anotaciones.

Su artículo 13 es el que importa para un servicio como este: **quien lo usa por la red tiene
derecho a su fuente**, aunque nunca baje el programa. Por eso las tres páginas del sitio linkean
al repositorio en su pie, y `/acerca-de` lo dice con todas las letras. Un despliegue propio que
cambie el código tiene que ofrecer ese código a quien lo consulte, y para eso alcanza con
apuntar ese link al repositorio de esa versión.
