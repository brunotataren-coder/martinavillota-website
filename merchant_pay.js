const elements = {
  merchantName: document.getElementById("merchant-name"),
  merchantPhone: document.getElementById("merchant-phone"),
  merchantBalance: document.getElementById("merchant-balance"),
  customerName: document.getElementById("customer-name"),
  customerPhone: document.getElementById("customer-phone"),
  customerBalance: document.getElementById("customer-balance"),
  salesToday: document.getElementById("sales-today"),
  txCount: document.getElementById("tx-count"),
  feeRate: document.getElementById("fee-rate"),
  walletProvider: document.getElementById("wallet-provider"),
  walletNetwork: document.getElementById("wallet-network"),
  merchantWallet: document.getElementById("merchant-wallet"),
  customerWallet: document.getElementById("customer-wallet"),
  walletNote: document.getElementById("wallet-note"),
  status: document.getElementById("connection-status"),
  messageList: document.getElementById("message-list"),
  pendingCard: document.getElementById("pending-card"),
  pendingSummary: document.getElementById("pending-summary"),
  pendingExpiry: document.getElementById("pending-expiry"),
  confirmPayment: document.getElementById("confirm-payment"),
  cancelPayment: document.getElementById("cancel-payment"),
  chatForm: document.getElementById("chat-form"),
  actorSelect: document.getElementById("actor-select"),
  chatInput: document.getElementById("chat-input"),
  sendButton: document.getElementById("send-message"),
  resetDemo: document.getElementById("reset-demo"),
  quickButtons: Array.from(document.querySelectorAll(".quick")),
};

let pendingTimer = null;
let lastMessageId = 0;
let locked = false;

function formatMoney(value) {
  return `${Number(value || 0).toFixed(2)} USDC`;
}

function formatTime(isoDate) {
  try {
    const date = new Date(isoDate);
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch (error) {
    return "--:--";
  }
}

function shortAddress(value) {
  const text = String(value || "").trim();
  if (!text) {
    return "-";
  }
  if (text.length <= 20) {
    return text;
  }
  return `${text.slice(0, 10)}...${text.slice(-8)}`;
}

function setStatus(text, level = "ok") {
  elements.status.textContent = text;
  elements.status.classList.remove("ok", "error");
  elements.status.classList.add(level);
}

function lockControls(value) {
  locked = value;
  elements.sendButton.disabled = value;
  elements.confirmPayment.disabled = value;
  elements.cancelPayment.disabled = value;
  elements.resetDemo.disabled = value;
}

async function jsonRequest(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    const message = payload.error || payload.message || "No se pudo completar la accion.";
    throw new Error(message);
  }
  return payload;
}

function renderParticipants(participants) {
  elements.merchantName.textContent = participants.merchant.name;
  elements.merchantPhone.textContent = participants.merchant.phone;
  elements.merchantBalance.textContent = formatMoney(participants.merchant.balanceUsdc);

  elements.customerName.textContent = participants.customer.name;
  elements.customerPhone.textContent = participants.customer.phone;
  elements.customerBalance.textContent = formatMoney(participants.customer.balanceUsdc);
}

function renderWallet(wallet, participants) {
  elements.walletProvider.textContent = (wallet.provider || "-").toUpperCase();
  elements.walletNetwork.textContent = wallet.network || "-";
  elements.merchantWallet.textContent = shortAddress(participants.merchant.walletAddress);
  elements.customerWallet.textContent = shortAddress(participants.customer.walletAddress);

  if (wallet.provider === "cdp") {
    elements.walletNote.textContent =
      "Fondea la wallet del cliente con USDC y ETH (gas) para ejecutar pagos on-chain.";
  } else {
    elements.walletNote.textContent = "Modo demo local. No usa blockchain.";
  }
}

function renderStats(stats) {
  elements.salesToday.textContent = formatMoney(stats.salesTodayUsdc);
  elements.txCount.textContent = String(stats.transactionsToday || 0);
  elements.feeRate.textContent = `${Number((stats.feeRate || 0) * 100).toFixed(2)}%`;
}

function scrollIfNeeded() {
  const shouldStickBottom =
    elements.messageList.scrollTop + elements.messageList.clientHeight >=
    elements.messageList.scrollHeight - 70;
  if (shouldStickBottom) {
    elements.messageList.scrollTop = elements.messageList.scrollHeight;
  }
}

function renderMessages(chat) {
  if (!Array.isArray(chat) || chat.length === 0) {
    elements.messageList.innerHTML = '<p class="empty">Sin mensajes todavia.</p>';
    return;
  }

  const newestId = chat[chat.length - 1].id;
  const shouldScroll = newestId > lastMessageId;
  lastMessageId = newestId;

  const list = document.createDocumentFragment();
  chat.forEach((message) => {
    const bubble = document.createElement("article");
    bubble.className = `message ${message.senderRole}`;

    const text = document.createElement("div");
    text.textContent = message.body;
    bubble.appendChild(text);

    const meta = document.createElement("p");
    meta.className = "meta";
    meta.textContent = `${message.senderName} • ${formatTime(message.createdAt)}`;
    bubble.appendChild(meta);

    list.appendChild(bubble);
  });

  elements.messageList.innerHTML = "";
  elements.messageList.appendChild(list);

  if (shouldScroll) {
    elements.messageList.scrollTop = elements.messageList.scrollHeight;
  } else {
    scrollIfNeeded();
  }
}

function stopPendingTimer() {
  if (pendingTimer) {
    clearInterval(pendingTimer);
    pendingTimer = null;
  }
}

function paintPendingExpiry(expiresAt) {
  const now = Date.now();
  const expires = new Date(expiresAt).getTime();
  const remaining = Math.max(0, expires - now);
  const remainingSec = Math.floor(remaining / 1000);
  const minutes = Math.floor(remainingSec / 60);
  const seconds = remainingSec % 60;
  elements.pendingExpiry.textContent = `Expira en ${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function renderPending(pendingRequest) {
  stopPendingTimer();
  if (!pendingRequest) {
    elements.pendingCard.classList.add("hidden");
    return;
  }

  elements.pendingCard.classList.remove("hidden");
  elements.pendingSummary.textContent = `${formatMoney(pendingRequest.amountUsdc)} • Token ${pendingRequest.token}`;
  paintPendingExpiry(pendingRequest.expiresAt);

  pendingTimer = window.setInterval(() => {
    const expires = new Date(pendingRequest.expiresAt).getTime();
    if (Date.now() >= expires) {
      elements.pendingExpiry.textContent = "Expirada. Actualizando estado...";
      stopPendingTimer();
      void loadState();
      return;
    }
    paintPendingExpiry(pendingRequest.expiresAt);
  }, 1000);
}

function applyState(state) {
  renderParticipants(state.participants);
  renderWallet(state.wallet, state.participants);
  renderStats(state.stats);
  renderMessages(state.chat);
  renderPending(state.pendingRequest);
}

async function loadState() {
  try {
    const state = await jsonRequest("/api/state");
    applyState(state);
    if (state.wallet && state.wallet.error) {
      setStatus(state.wallet.error, "error");
    } else {
      setStatus("Conectado", "ok");
    }
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function sendMessage(actor, text) {
  if (locked) {
    return;
  }

  lockControls(true);
  try {
    const payload = await jsonRequest("/api/chat", {
      method: "POST",
      body: JSON.stringify({ actor, text }),
    });
    applyState(payload.state);
    setStatus("Mensaje enviado", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    lockControls(false);
  }
}

async function confirmPayment() {
  if (locked) {
    return;
  }
  lockControls(true);
  try {
    const payload = await jsonRequest("/api/payment/confirm", { method: "POST", body: "{}" });
    applyState(payload.state);
    setStatus(payload.message, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    lockControls(false);
  }
}

async function cancelPayment() {
  if (locked) {
    return;
  }
  lockControls(true);
  try {
    const payload = await jsonRequest("/api/payment/cancel", { method: "POST", body: "{}" });
    applyState(payload.state);
    setStatus(payload.message, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    lockControls(false);
  }
}

async function resetDemo() {
  if (locked) {
    return;
  }
  lockControls(true);
  try {
    const payload = await jsonRequest("/api/reset", { method: "POST", body: "{}" });
    applyState(payload.state);
    setStatus("Demo reiniciada", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    lockControls(false);
  }
}

function bindEvents() {
  elements.chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const actor = elements.actorSelect.value;
    const text = elements.chatInput.value.trim();
    if (!text) {
      return;
    }
    elements.chatInput.value = "";
    void sendMessage(actor, text);
  });

  elements.confirmPayment.addEventListener("click", () => {
    void confirmPayment();
  });

  elements.cancelPayment.addEventListener("click", () => {
    void cancelPayment();
  });

  elements.resetDemo.addEventListener("click", () => {
    void resetDemo();
  });

  elements.quickButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const actor = button.dataset.actor || "customer";
      const template = button.dataset.template || "";
      elements.actorSelect.value = actor;
      elements.chatInput.value = template;
      elements.chatInput.focus();
    });
  });
}

function boot() {
  bindEvents();
  setStatus("Sincronizando...", "ok");
  void loadState();
  window.setInterval(() => {
    void loadState();
  }, 15000);
}

boot();
