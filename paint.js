const canvas = document.getElementById("paint-canvas");
const ctx = canvas.getContext("2d");

const pencilButton = document.getElementById("tool-pencil");
const eraserButton = document.getElementById("tool-eraser");
const colorPicker = document.getElementById("color-picker");
const sizePicker = document.getElementById("size-picker");
const sizeValue = document.getElementById("size-value");
const undoButton = document.getElementById("undo-action");
const redoButton = document.getElementById("redo-action");
const clearButton = document.getElementById("clear-canvas");
const saveButton = document.getElementById("save-image");
const refreshMailsButton = document.getElementById("refresh-mails");
const mailStatus = document.getElementById("mail-status");
const mailList = document.getElementById("mail-list");

const PAPER_COLOR = "#fffdf7";
const MAX_HISTORY = 28;
const MAX_MAILS = 3;
const MAILS_ENDPOINT = "/api/mails";

let drawing = false;
let movedDuringStroke = false;
let activeTool = "pencil";
let undoStack = [];
let redoStack = [];
let pointerId = null;

function updateSizeLabel() {
  sizeValue.textContent = `${sizePicker.value} px`;
}

function getCssSize() {
  const rect = canvas.getBoundingClientRect();

  return {
    width: Math.max(1, rect.width),
    height: Math.max(1, rect.height),
  };
}

function setupContext() {
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.imageSmoothingEnabled = true;
}

function fillPaper() {
  const { width, height } = getCssSize();
  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = PAPER_COLOR;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.restore();

  ctx.fillStyle = PAPER_COLOR;
  ctx.fillRect(0, 0, width, height);
}

function setTool(tool) {
  activeTool = tool;
  document.body.dataset.tool = tool;
  pencilButton.classList.toggle("is-active", tool === "pencil");
  eraserButton.classList.toggle("is-active", tool === "eraser");
}

function getPointerPosition(event) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: event.clientX - rect.left,
    y: event.clientY - rect.top,
  };
}

function beginStroke(event) {
  if (event.pointerType === "mouse" && event.button !== 0) return;

  drawing = true;
  movedDuringStroke = false;
  pointerId = event.pointerId;

  canvas.setPointerCapture(pointerId);

  const point = getPointerPosition(event);
  ctx.beginPath();
  ctx.moveTo(point.x, point.y);
}

function draw(event) {
  if (!drawing || pointerId !== event.pointerId) return;

  const point = getPointerPosition(event);
  ctx.lineWidth = Number(sizePicker.value);
  ctx.strokeStyle = activeTool === "eraser" ? PAPER_COLOR : colorPicker.value;
  ctx.lineTo(point.x, point.y);
  ctx.stroke();
  movedDuringStroke = true;
}

function endStroke(event) {
  if (!drawing || pointerId !== event.pointerId) return;

  drawing = false;
  ctx.closePath();
  canvas.releasePointerCapture(pointerId);
  pointerId = null;

  if (movedDuringStroke) {
    commitHistory();
  }
}

function commitHistory() {
  const snapshot = canvas.toDataURL("image/png");
  if (undoStack[undoStack.length - 1] === snapshot) return;

  undoStack.push(snapshot);
  if (undoStack.length > MAX_HISTORY) {
    undoStack = undoStack.slice(undoStack.length - MAX_HISTORY);
  }

  redoStack = [];
  updateHistoryButtons();
}

function restoreFromDataUrl(dataUrl) {
  return new Promise((resolve) => {
    const image = new Image();

    image.onload = () => {
      const { width, height } = getCssSize();
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = PAPER_COLOR;
      ctx.fillRect(0, 0, width, height);
      ctx.drawImage(image, 0, 0, width, height);
      resolve();
    };

    image.src = dataUrl;
  });
}

function updateHistoryButtons() {
  undoButton.disabled = undoStack.length <= 1;
  redoButton.disabled = redoStack.length === 0;
}

async function undo() {
  if (undoStack.length <= 1) return;

  const current = undoStack.pop();
  redoStack.push(current);
  await restoreFromDataUrl(undoStack[undoStack.length - 1]);
  updateHistoryButtons();
}

async function redo() {
  if (redoStack.length === 0) return;

  const next = redoStack.pop();
  undoStack.push(next);
  await restoreFromDataUrl(next);
  updateHistoryButtons();
}

function clearCanvas() {
  const { width, height } = getCssSize();
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = PAPER_COLOR;
  ctx.fillRect(0, 0, width, height);
  commitHistory();
}

function saveImage() {
  const link = document.createElement("a");
  link.download = "sketchroom.png";
  link.href = canvas.toDataURL("image/png");
  link.click();
}

function resizeCanvas({ preserveDrawing = true } = {}) {
  const { width: cssWidth, height: cssHeight } = getCssSize();
  const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
  const targetWidth = Math.floor(cssWidth * pixelRatio);
  const targetHeight = Math.floor(cssHeight * pixelRatio);

  if (targetWidth === canvas.width && targetHeight === canvas.height) return;

  let snapshot = null;
  if (preserveDrawing && canvas.width > 0 && canvas.height > 0) {
    snapshot = document.createElement("canvas");
    snapshot.width = canvas.width;
    snapshot.height = canvas.height;
    snapshot.getContext("2d").drawImage(canvas, 0, 0);
  }

  canvas.width = targetWidth;
  canvas.height = targetHeight;
  ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  setupContext();

  ctx.fillStyle = PAPER_COLOR;
  ctx.fillRect(0, 0, cssWidth, cssHeight);

  if (snapshot) {
    ctx.drawImage(snapshot, 0, 0, snapshot.width, snapshot.height, 0, 0, cssWidth, cssHeight);
  }
}

function handleKeyboardShortcuts(event) {
  const metaOrCtrl = event.metaKey || event.ctrlKey;
  if (!metaOrCtrl) return;

  const key = event.key.toLowerCase();

  if (key === "z" && event.shiftKey) {
    event.preventDefault();
    redo();
    return;
  }

  if (key === "z") {
    event.preventDefault();
    undo();
    return;
  }

  if (key === "y") {
    event.preventDefault();
    redo();
  }
}

async function fetchLatestMails(limit = MAX_MAILS) {
  const response = await fetch(`${MAILS_ENDPOINT}?limit=${encodeURIComponent(String(limit))}`);
  const payload = await response.json().catch(() => ({}));

  if (!response.ok) {
    const message = payload?.error || `Error ${response.status} al cargar mails`;
    throw new Error(message);
  }

  return Array.isArray(payload?.messages) ? payload.messages : [];
}

function getHeader(headers, name) {
  if (!Array.isArray(headers)) return "";
  const found = headers.find((header) => header?.name?.toLowerCase() === name.toLowerCase());
  return found?.value ?? "";
}

function cleanPreview(rawText) {
  const plain = String(rawText ?? "")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/\s+/g, " ")
    .trim();

  if (!plain) return "Sin vista previa.";
  return plain.length > 180 ? `${plain.slice(0, 177)}...` : plain;
}

function formatMailDate(value) {
  if (!value) return "";

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";

  return new Intl.DateTimeFormat(navigator.language || "es-CL", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(date);
}

function createMailCard(message) {
  const from = message?.sender || getHeader(message?.payload?.headers, "From") || "Remitente";
  const subject = message?.subject || message?.preview?.subject || "(Sin asunto)";
  const previewText = cleanPreview(message?.preview || message?.preview?.body || message?.messageText);
  const dateText = formatMailDate(message?.messageTimestamp);

  const li = document.createElement("li");
  li.className = "mail-card";

  const top = document.createElement("div");
  top.className = "mail-card-top";

  const fromNode = document.createElement("p");
  fromNode.className = "mail-from";
  fromNode.textContent = from;

  const dateNode = document.createElement("p");
  dateNode.className = "mail-date";
  dateNode.textContent = dateText;

  top.append(fromNode, dateNode);

  const subjectNode = document.createElement("p");
  subjectNode.className = "mail-subject";
  subjectNode.textContent = subject;

  const previewNode = document.createElement("p");
  previewNode.className = "mail-preview";
  previewNode.textContent = previewText;

  li.append(top, subjectNode, previewNode);
  return li;
}

function renderMailList(messages) {
  mailList.replaceChildren();

  if (!messages.length) {
    const empty = document.createElement("li");
    empty.className = "mail-empty";
    empty.textContent = "No hay correos para mostrar.";
    mailList.append(empty);
    return;
  }

  messages.slice(0, MAX_MAILS).forEach((message) => {
    mailList.append(createMailCard(message));
  });
}

async function loadLatestMails() {
  if (window.location.protocol === "file:") {
    mailStatus.textContent =
      "Para cargar correos, abre la app con servidor local (ejemplo: python3 server.py).";
    renderMailList([]);
    return;
  }

  refreshMailsButton.disabled = true;
  mailStatus.textContent = "Cargando correos...";

  try {
    const latestMessages = await fetchLatestMails(MAX_MAILS);
    renderMailList(latestMessages);
    mailStatus.textContent =
      latestMessages.length > 0
        ? `Mostrando ${latestMessages.length} correo(s) recientes.`
        : "No se encontraron correos recientes.";
  } catch (error) {
    renderMailList([]);
    mailStatus.textContent = `No se pudieron cargar los correos: ${error.message}`;
  } finally {
    refreshMailsButton.disabled = false;
  }
}

function initCanvas() {
  resizeCanvas({ preserveDrawing: false });
  fillPaper();
  undoStack = [canvas.toDataURL("image/png")];
  redoStack = [];
  updateHistoryButtons();
}

function attachEvents() {
  canvas.addEventListener("pointerdown", beginStroke);
  canvas.addEventListener("pointermove", draw);
  canvas.addEventListener("pointerup", endStroke);
  canvas.addEventListener("pointercancel", endStroke);
  canvas.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "mouse") endStroke(event);
  });

  pencilButton.addEventListener("click", () => setTool("pencil"));
  eraserButton.addEventListener("click", () => setTool("eraser"));

  sizePicker.addEventListener("input", updateSizeLabel);
  undoButton.addEventListener("click", undo);
  redoButton.addEventListener("click", redo);
  clearButton.addEventListener("click", clearCanvas);
  saveButton.addEventListener("click", saveImage);
  refreshMailsButton.addEventListener("click", loadLatestMails);

  window.addEventListener("keydown", handleKeyboardShortcuts);

  const resizeObserver = new ResizeObserver(() => {
    resizeCanvas({ preserveDrawing: true });
  });
  resizeObserver.observe(canvas);
}

setTool("pencil");
updateSizeLabel();
initCanvas();
attachEvents();
loadLatestMails();
