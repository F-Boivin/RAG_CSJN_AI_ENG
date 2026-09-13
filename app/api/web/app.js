// La interfaz: POST para crear la consulta, EventSource para seguirla.
//
// EventSource solo hace GET, y por eso el POST y el stream son dos endpoints. La ventaja
// práctica es que recargar la página vuelve a leer el stream desde el principio sin volver
// a cobrar cupo ni a correr el grafo.

const $ = (id) => document.getElementById(id);
const form = $("busqueda");
const entrada = $("consulta");
const boton = $("preguntar");
const banner = $("banner");
const progreso = $("progreso");
const pasos = $("pasos");
const respuesta = $("respuesta");
const texto = $("texto");
const citas = $("citas");
const cierre = $("cierre");
const cupo = $("cupo");

let fuente = null;
// Queda en true mientras EventSource está reintentando, para poder sacar el aviso en cuanto
// vuelve a llegar un evento.
let reconectando = false;

document.querySelectorAll(".ejemplo").forEach((b) => {
  b.addEventListener("click", () => {
    entrada.value = b.textContent.trim();
    form.requestSubmit();
  });
});

form.addEventListener("submit", async (evento) => {
  evento.preventDefault();
  const consulta = entrada.value.trim();
  if (consulta.length < 8) return;
  limpiar();
  boton.disabled = true;

  try {
    const r = await fetch("/consultas", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ consulta }),
    });
    const cuerpo = await r.json();
    if (!r.ok) {
      // Un 409 dice "ya tenés una consulta en curso" y trae su id. Engancharse a ese stream
      // muestra la corrida que de todos modos se está pagando, en vez de dejar a quien
      // pregunta esperando sin nada en pantalla.
      if (r.status === 409 && cuerpo.consulta_id) {
        mostrarBanner(cuerpo.detail || "Ya tenés una consulta en curso.");
        seguir(cuerpo.consulta_id);
        return;
      }
      mostrarBanner(cuerpo.motivo || cuerpo.detail || "No se pudo procesar la consulta.");
      boton.disabled = false;
      return;
    }
    if (typeof cuerpo.cupo_restante === "number") pintarCupo(cuerpo.cupo_restante);
    seguir(cuerpo.consulta_id);
  } catch (e) {
    mostrarBanner("No se pudo contactar al buscador. Probá de nuevo.");
    boton.disabled = false;
  }
});

function seguir(consultaId) {
  progreso.hidden = false;
  // Cada anuncio de fase abre una redacción nueva, y el texto que llega después la reemplaza
  // en vez de agregarse. Sin esto, una corrección concatenaba el borrador viejo con el nuevo
  // y quien miraba veía la respuesta dos veces, pegadas.
  let redaccionNueva = false;
  fuente = new EventSource(`/consultas/${consultaId}/stream`);

  fuente.addEventListener("estado", (e) => {
    reconectado();
    redaccionNueva = true;
    paso(JSON.parse(e.data));
  });
  fuente.addEventListener("texto", (e) => {
    reconectado();
    respuesta.hidden = false;
    if (redaccionNueva) {
      texto.textContent = "";
      citas.replaceChildren();
      redaccionNueva = false;
    }
    texto.textContent += JSON.parse(e.data).delta;
  });
  fuente.addEventListener("cita", (e) => { reconectado(); pintarCita(JSON.parse(e.data)); });
  fuente.addEventListener("final", (e) => {
    const datos = JSON.parse(e.data);
    // El cierre trae el texto autoritativo: si hubo un reintento, lo que se fue mostrando
    // es de la redacción anterior.
    if (datos.respuesta) {
      respuesta.hidden = false;
      texto.textContent = datos.respuesta;
    }
    const c = datos.calidad || {};
    cierre.textContent =
      `${c.citas_en_el_texto || 0} citas verificadas · ` +
      `${c.intentos ? c.intentos + " corrección(es) · " : ""}` +
      `${Math.round((datos.duracion_ms || 0) / 1000)} s`;
    terminar();
  });
  fuente.addEventListener("sin_base", (e) => {
    const datos = JSON.parse(e.data);
    // El borrador que se fue viendo es exactamente lo que el sistema decidió no publicar.
    // Dejarlo en pantalla debajo del aviso ofrece como respuesta lo que no se sostiene, que
    // es lo contrario de lo que el aviso dice.
    texto.textContent = "";
    citas.replaceChildren();
    mostrarBanner(datos.motivo);
    const d = datos.diagnostico || {};
    if (d.citas_propuestas !== undefined) {
      cierre.textContent =
        `${d.citas_verificadas || 0} de ${d.citas_propuestas || 0} citas propuestas existen ` +
        `en el corpus · ${d.intentos || 0} intento(s)`;
      respuesta.hidden = false;
    }
    terminar();
  });
  // Dos cosas distintas llegan a este mismo listener. Un `event: error` del servidor trae
  // `data` y es definitivo. Una caída de transporte no trae nada, y EventSource reconecta
  // solo mientras nadie lo cierre: cerrarlo ahí abandonaba una corrida que el servidor sigue
  // corriendo y cuyos eventos quedan guardados para reproducir con `Last-Event-ID`.
  fuente.addEventListener("error", (e) => {
    if (e.data) {
      mostrarBanner(JSON.parse(e.data).mensaje);
      terminar();
      return;
    }
    if (fuente && fuente.readyState === EventSource.CLOSED) {
      mostrarBanner("Se cortó la conexión con el buscador. Recargá la página para " +
                    "ver en qué quedó la consulta.");
      terminar();
      return;
    }
    reconectando = true;
    mostrarBanner("Se cortó la conexión; reintentando…");
  });
}

function paso({ fase, detalle }) {
  progreso.hidden = false;
  const previo = pasos.querySelector("li.viva");
  if (previo) previo.classList.remove("viva");
  const li = document.createElement("li");
  li.className = "viva";
  li.textContent = detalle || fase;
  pasos.appendChild(li);
}

// La ficha se arma con nodos y no con una plantilla de HTML. Hoy los dos campos que se
// insertaban en crudo son seguros —`fallo` sale de `normalizar_cita`, que devuelve dígitos, y
// `url` sale del padrón, que construye la ingesta—, pero eso es un invariante que vive en
// otros dos módulos: con nodos, la página no depende de que nadie lo rompa.
function pintarCita({ fallo, url, subseccion, afirmacion, respaldo }) {
  respuesta.hidden = false;
  const div = document.createElement("div");
  div.className = "cita";
  let cabeza;
  if (url) {
    cabeza = document.createElement("a");
    cabeza.href = url;
    cabeza.target = "_blank";
    cabeza.rel = "noopener noreferrer";
  } else {
    cabeza = document.createElement("strong");
  }
  cabeza.textContent = fallo;
  div.appendChild(cabeza);
  const contexto = [afirmacion, subseccion].filter(Boolean).join(" — ");
  if (contexto) {
    const span = document.createElement("span");
    span.className = "contexto";
    span.textContent = contexto;
    div.appendChild(span);
  }
  // El pasaje del corpus, que es lo que el verificador comprobó. Va debajo de la afirmación
  // justamente para que se lean juntos: el sistema garantiza que esta oración está en el
  // documento, y el salto de la oración a la afirmación lo juzga quien lee.
  if (respaldo) {
    const cita = document.createElement("blockquote");
    cita.className = "respaldo";
    cita.textContent = respaldo;
    div.appendChild(cita);
  }
  citas.appendChild(div);
}

function mostrarBanner(mensaje) {
  banner.hidden = false;
  banner.textContent = mensaje;
}

function reconectado() {
  // Llega un evento: la conexión volvió, y el aviso de reintento ya no corresponde.
  if (reconectando) {
    reconectando = false;
    banner.hidden = true;
  }
}

function pintarCupo(restante) {
  cupo.textContent = restante === 1
    ? "Te queda 1 consulta hoy"
    : `Te quedan ${restante} consultas hoy`;
}

function terminar() {
  if (fuente) { fuente.close(); fuente = null; }
  reconectando = false;
  boton.disabled = false;
  const viva = pasos.querySelector("li.viva");
  if (viva) viva.classList.remove("viva");
}

function limpiar() {
  if (fuente) { fuente.close(); fuente = null; }
  reconectando = false;
  banner.hidden = true;
  pasos.replaceChildren();
  texto.textContent = "";
  citas.replaceChildren();
  cierre.textContent = "";
  respuesta.hidden = true;
  progreso.hidden = true;
}

fetch("/salud")
  .then((r) => r.json())
  .then((s) => {
    if (typeof s.cupo_restante === "number") pintarCupo(s.cupo_restante);
    if (s.modo === "lectura") {
      mostrarBanner("El buscador alcanzó su techo de consultas de hoy. Vuelve a las 00:00.");
      boton.disabled = true;
    }
    if (s.indice !== "listo") {
      mostrarBanner(s.detalle || "El buscador está cargando el corpus.");
      boton.disabled = true;
    }
  })
  .catch(() => {});
