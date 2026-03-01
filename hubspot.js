const el = {
  status: document.getElementById("connection-status"),
  alert: document.getElementById("alert"),
  forceRefresh: document.getElementById("force-refresh"),
  tabs: Array.from(document.querySelectorAll(".tab")),
  searchInput: document.getElementById("search-input"),
  searchButton: document.getElementById("search-button"),
  dealFilters: document.getElementById("deal-filters"),
  pipelineFilter: document.getElementById("pipeline-filter"),
  stageFilter: document.getElementById("stage-filter"),
  tableTitle: document.getElementById("table-title"),
  tableMeta: document.getElementById("table-meta"),
  dataTableHead: document.querySelector("#data-table thead"),
  dataTableBody: document.querySelector("#data-table tbody"),
  prevPage: document.getElementById("prev-page"),
  nextPage: document.getElementById("next-page"),
  pageIndicator: document.getElementById("page-indicator"),
  detailEmpty: document.getElementById("detail-empty"),
  detailBody: document.getElementById("detail-body"),
  detailTitle: document.getElementById("detail-title"),
  detailSubtitle: document.getElementById("detail-subtitle"),
  detailProperties: document.getElementById("detail-properties"),
  detailAssociations: document.getElementById("detail-associations"),
};

const TAB_LABELS = {
  deals: "Deals",
  pipelines: "Pipelines",
  contacts: "Contactos",
  companies: "Empresas",
};

const state = {
  activeTab: "deals",
  busy: false,
  pipelines: [],
  pipelineMap: new Map(),
  stageMap: new Map(),
  filters: {
    deals: { q: "", pipelineId: "", stageId: "" },
    contacts: { q: "" },
    companies: { q: "" },
  },
  tabs: {
    deals: { items: [], selectedId: "", history: [null], index: 0, nextAfter: null },
    contacts: { items: [], selectedId: "", history: [null], index: 0, nextAfter: null },
    companies: { items: [], selectedId: "", history: [null], index: 0, nextAfter: null },
    pipelines: { items: [], selectedId: "" },
  },
};

const TABLE_COLUMNS = {
  deals: [
    {
      label: "Deal",
      value: (item) => item.name || `Deal ${item.id}`,
    },
    {
      label: "Amount",
      value: (item) => formatMoney(item.amount),
    },
    {
      label: "Pipeline",
      value: (item) => pipelineLabel(item.pipelineId),
    },
    {
      label: "Stage",
      value: (item) => stageLabel(item.stageId),
    },
    {
      label: "Close Date",
      value: (item) => formatDate(item.closeDate),
    },
    {
      label: "Owner",
      value: (item) => item.ownerId || "-",
      className: "mono",
    },
  ],
  contacts: [
    {
      label: "Nombre",
      value: (item) => [item.firstname, item.lastname].filter(Boolean).join(" ") || `Contacto ${item.id}`,
    },
    {
      label: "Email",
      value: (item) => item.email || "-",
    },
    {
      label: "Teléfono",
      value: (item) => item.phone || "-",
    },
    {
      label: "Empresa",
      value: (item) => item.company || "-",
    },
    {
      label: "Actualizado",
      value: (item) => formatDate(item.updatedAt),
    },
  ],
  companies: [
    {
      label: "Empresa",
      value: (item) => item.name || `Empresa ${item.id}`,
    },
    {
      label: "Dominio",
      value: (item) => item.domain || "-",
    },
    {
      label: "Teléfono",
      value: (item) => item.phone || "-",
    },
    {
      label: "País",
      value: (item) => item.country || "-",
    },
    {
      label: "Industria",
      value: (item) => item.industry || "-",
    },
  ],
  pipelines: [
    {
      label: "Pipeline",
      value: (item) => item.label || `Pipeline ${item.id}`,
    },
    {
      label: "ID",
      value: (item) => item.id || "-",
      className: "mono",
    },
    {
      label: "Stages",
      value: (item) => String((item.stages || []).length),
    },
  ],
};

function setStatus(text, level = "ok") {
  el.status.textContent = text;
  el.status.classList.toggle("error", level === "error");
}

function setAlert(text = "") {
  if (!text) {
    el.alert.classList.add("hidden");
    el.alert.textContent = "";
    return;
  }
  el.alert.classList.remove("hidden");
  el.alert.textContent = text;
}

function syncControlAvailability() {
  const isPipelines = state.activeTab === "pipelines";
  const isDeals = state.activeTab === "deals";

  el.forceRefresh.disabled = state.busy;
  el.searchInput.disabled = state.busy || isPipelines;
  el.searchButton.disabled = state.busy || isPipelines;
  el.pipelineFilter.disabled = state.busy || !isDeals;
  el.stageFilter.disabled = state.busy || !isDeals;
  el.prevPage.disabled = state.busy;
  el.nextPage.disabled = state.busy;
}

function setBusy(value) {
  state.busy = value;
  syncControlAvailability();
}

function formatDate(value) {
  if (!value) {
    return "-";
  }
  const asDate = new Date(value);
  if (Number.isNaN(asDate.getTime())) {
    return String(value);
  }
  return asDate.toLocaleDateString();
}

function formatMoney(value) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const amount = Number(value);
  if (Number.isNaN(amount)) {
    return String(value);
  }
  return `${amount.toFixed(2)} USD`;
}

function pipelineLabel(pipelineId) {
  if (!pipelineId) {
    return "-";
  }
  return state.pipelineMap.get(String(pipelineId)) || String(pipelineId);
}

function stageLabel(stageId) {
  if (!stageId) {
    return "-";
  }
  return state.stageMap.get(String(stageId)) || String(stageId);
}

async function apiGet(path, params = {}) {
  const url = new URL(path, window.location.origin);
  Object.entries(params).forEach(([key, value]) => {
    if (value === null || value === undefined || value === "") {
      return;
    }
    url.searchParams.set(key, String(value));
  });

  const response = await fetch(url.toString(), {
    method: "GET",
    headers: { Accept: "application/json" },
  });

  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    payload = {};
  }

  if (!response.ok) {
    const message = payload.error || payload.message || "Error consultando el backend.";
    throw new Error(message);
  }

  return payload;
}

function clearDetail(message = "Selecciona un registro para ver propiedades y asociaciones.") {
  el.detailBody.classList.add("hidden");
  el.detailEmpty.classList.remove("hidden");
  el.detailEmpty.textContent = message;
  el.detailProperties.innerHTML = "";
  el.detailAssociations.innerHTML = "";
}

function renderAssociations(associations) {
  const groups = ["contacts", "companies", "deals"];
  el.detailAssociations.innerHTML = "";

  groups.forEach((groupName) => {
    const container = document.createElement("section");
    container.className = "association-group";

    const title = document.createElement("h5");
    title.textContent = groupName;
    container.appendChild(title);

    const list = document.createElement("ul");
    const values = Array.isArray(associations[groupName]) ? associations[groupName] : [];

    if (values.length === 0) {
      const empty = document.createElement("li");
      empty.textContent = "Sin asociaciones";
      list.appendChild(empty);
    } else {
      values.forEach((row) => {
        const li = document.createElement("li");
        li.textContent = row.label ? `${row.label} (${row.id})` : String(row.id || "-");
        list.appendChild(li);
      });
    }

    container.appendChild(list);
    el.detailAssociations.appendChild(container);
  });
}

function renderPropertyList(entries) {
  el.detailProperties.innerHTML = "";

  entries.forEach(([label, value]) => {
    const wrapper = document.createElement("div");
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value === null || value === undefined || value === "" ? "-" : String(value);

    wrapper.appendChild(dt);
    wrapper.appendChild(dd);
    el.detailProperties.appendChild(wrapper);
  });
}

function detailFields(tab, item) {
  if (tab === "deals") {
    return [
      ["ID", item.id],
      ["Nombre", item.name],
      ["Amount", formatMoney(item.amount)],
      ["Pipeline", pipelineLabel(item.pipelineId)],
      ["Stage", stageLabel(item.stageId)],
      ["Close Date", formatDate(item.closeDate)],
      ["Owner", item.ownerId],
      ["Creado", formatDate(item.createdAt)],
      ["Actualizado", formatDate(item.updatedAt)],
    ];
  }

  if (tab === "contacts") {
    return [
      ["ID", item.id],
      ["Nombre", [item.firstname, item.lastname].filter(Boolean).join(" ")],
      ["Email", item.email],
      ["Teléfono", item.phone],
      ["Empresa", item.company],
      ["Creado", formatDate(item.createdAt)],
      ["Actualizado", formatDate(item.updatedAt)],
    ];
  }

  return [
    ["ID", item.id],
    ["Nombre", item.name],
    ["Dominio", item.domain],
    ["Teléfono", item.phone],
    ["Ciudad", item.city],
    ["País", item.country],
    ["Industria", item.industry],
    ["Creado", formatDate(item.createdAt)],
    ["Actualizado", formatDate(item.updatedAt)],
  ];
}

function renderPipelineDetail(item) {
  el.detailEmpty.classList.add("hidden");
  el.detailBody.classList.remove("hidden");
  el.detailTitle.textContent = item.label || `Pipeline ${item.id}`;
  el.detailSubtitle.textContent = `ID ${item.id}`;

  renderPropertyList([
    ["ID", item.id],
    ["Label", item.label],
    ["Stages", String((item.stages || []).length)],
  ]);

  const stageAssociations = {
    contacts: [],
    companies: [],
    deals: (item.stages || []).map((stage) => ({
      id: stage.id,
      label: `${stage.label} (orden ${stage.displayOrder})`,
    })),
  };
  renderAssociations(stageAssociations);
}

function renderDetail(tab, item, associations) {
  el.detailEmpty.classList.add("hidden");
  el.detailBody.classList.remove("hidden");

  if (tab === "pipelines") {
    renderPipelineDetail(item);
    return;
  }

  if (tab === "deals") {
    el.detailTitle.textContent = item.name || `Deal ${item.id}`;
    el.detailSubtitle.textContent = `Deal ID ${item.id}`;
  } else if (tab === "contacts") {
    const fullName = [item.firstname, item.lastname].filter(Boolean).join(" ");
    el.detailTitle.textContent = fullName || `Contacto ${item.id}`;
    el.detailSubtitle.textContent = `Contacto ID ${item.id}`;
  } else {
    el.detailTitle.textContent = item.name || `Empresa ${item.id}`;
    el.detailSubtitle.textContent = `Empresa ID ${item.id}`;
  }

  renderPropertyList(detailFields(tab, item));
  renderAssociations(associations || {});
}

function buildTableHead(tab) {
  const columns = TABLE_COLUMNS[tab] || [];
  const row = document.createElement("tr");

  columns.forEach((column) => {
    const th = document.createElement("th");
    th.textContent = column.label;
    row.appendChild(th);
  });

  el.dataTableHead.innerHTML = "";
  el.dataTableHead.appendChild(row);
}

function renderTableRows(tab, items) {
  const columns = TABLE_COLUMNS[tab] || [];
  const selectedId = state.tabs[tab].selectedId;

  el.dataTableBody.innerHTML = "";
  if (!Array.isArray(items) || items.length === 0) {
    const row = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = columns.length || 1;
    td.textContent = "Sin datos en esta página.";
    row.appendChild(td);
    el.dataTableBody.appendChild(row);
    return;
  }

  items.forEach((item) => {
    const row = document.createElement("tr");
    row.dataset.id = item.id;

    if (selectedId && selectedId === item.id) {
      row.classList.add("selected");
    }

    columns.forEach((column) => {
      const td = document.createElement("td");
      if (column.className) {
        td.classList.add(column.className);
      }
      td.textContent = column.value(item);
      row.appendChild(td);
    });

    row.addEventListener("click", () => {
      void selectRow(tab, item.id);
    });
    el.dataTableBody.appendChild(row);
  });
}

function renderPager(tab) {
  if (tab === "pipelines") {
    el.prevPage.disabled = true;
    el.nextPage.disabled = true;
    el.pageIndicator.textContent = "Sin paginación";
    return;
  }

  const tabState = state.tabs[tab];
  const pageNumber = tabState.index + 1;
  el.pageIndicator.textContent = `Página ${pageNumber}`;
  el.prevPage.disabled = state.busy || tabState.index <= 0;
  el.nextPage.disabled = state.busy || !tabState.nextAfter;
}

function renderTable(tab) {
  const tabState = state.tabs[tab];
  const items = tabState.items || [];
  el.tableTitle.textContent = TAB_LABELS[tab];
  el.tableMeta.textContent = `${items.length} resultados`;

  buildTableHead(tab);
  renderTableRows(tab, items);
  renderPager(tab);
}

function rebuildPipelineIndexes() {
  state.pipelineMap = new Map();
  state.stageMap = new Map();

  state.pipelines.forEach((pipeline) => {
    state.pipelineMap.set(pipeline.id, pipeline.label || pipeline.id);
    (pipeline.stages || []).forEach((stage) => {
      state.stageMap.set(stage.id, stage.label || stage.id);
    });
  });
}

function renderPipelineOptions() {
  const selectedPipeline = state.filters.deals.pipelineId;
  const selectedStage = state.filters.deals.stageId;

  el.pipelineFilter.innerHTML = "";
  const defaultPipeline = document.createElement("option");
  defaultPipeline.value = "";
  defaultPipeline.textContent = "Todos los pipelines";
  el.pipelineFilter.appendChild(defaultPipeline);

  state.pipelines.forEach((pipeline) => {
    const option = document.createElement("option");
    option.value = pipeline.id;
    option.textContent = pipeline.label || pipeline.id;
    if (pipeline.id === selectedPipeline) {
      option.selected = true;
    }
    el.pipelineFilter.appendChild(option);
  });

  const stagePool = selectedPipeline
    ? (state.pipelines.find((pipeline) => pipeline.id === selectedPipeline)?.stages || [])
    : state.pipelines.flatMap((pipeline) => pipeline.stages || []);

  const uniqueStages = new Map();
  stagePool.forEach((stage) => {
    if (!uniqueStages.has(stage.id)) {
      uniqueStages.set(stage.id, stage);
    }
  });

  el.stageFilter.innerHTML = "";
  const defaultStage = document.createElement("option");
  defaultStage.value = "";
  defaultStage.textContent = "Todos los stages";
  el.stageFilter.appendChild(defaultStage);

  Array.from(uniqueStages.values())
    .sort((left, right) => Number(left.displayOrder || 0) - Number(right.displayOrder || 0))
    .forEach((stage) => {
      const option = document.createElement("option");
      option.value = stage.id;
      option.textContent = stage.label || stage.id;
      if (stage.id === selectedStage) {
        option.selected = true;
      }
      el.stageFilter.appendChild(option);
    });

  const stageStillAvailable = Array.from(uniqueStages.keys()).includes(selectedStage);
  if (selectedStage && !stageStillAvailable) {
    state.filters.deals.stageId = "";
    el.stageFilter.value = "";
  }
}

async function loadPipelines(forceRefresh = false) {
  const payload = await apiGet("/api/hubspot/pipelines", {
    objectType: "deals",
    forceRefresh: forceRefresh ? "1" : "",
  });

  state.pipelines = Array.isArray(payload.items) ? payload.items : [];
  state.tabs.pipelines.items = state.pipelines;
  rebuildPipelineIndexes();
  renderPipelineOptions();
}

function resetTabCursor(tab) {
  state.tabs[tab].history = [null];
  state.tabs[tab].index = 0;
  state.tabs[tab].nextAfter = null;
}

function currentTabCursor(tab) {
  return state.tabs[tab].history[state.tabs[tab].index] || "";
}

async function loadTabData(tab, { resetCursor = false, forceRefresh = false } = {}) {
  if (tab === "pipelines") {
    renderTable(tab);
    if (!state.tabs.pipelines.selectedId) {
      clearDetail();
    }
    return;
  }

  const tabState = state.tabs[tab];
  if (resetCursor) {
    resetTabCursor(tab);
    tabState.selectedId = "";
    clearDetail();
  }

  const params = {
    limit: 25,
    after: currentTabCursor(tab),
    q: state.filters[tab].q || "",
    forceRefresh: forceRefresh ? "1" : "",
  };

  if (tab === "deals") {
    params.pipelineId = state.filters.deals.pipelineId || "";
    params.stageId = state.filters.deals.stageId || "";
  }

  setBusy(true);
  try {
    const payload = await apiGet(`/api/hubspot/${tab}`, params);
    tabState.items = Array.isArray(payload.items) ? payload.items : [];
    tabState.nextAfter = payload.paging?.nextAfter || null;
    renderTable(tab);
    setStatus("HubSpot conectado", "ok");
    setAlert();
  } catch (error) {
    setStatus("Error de conexión", "error");
    setAlert(error.message);
    tabState.items = [];
    tabState.nextAfter = null;
    renderTable(tab);
  } finally {
    setBusy(false);
    renderPager(tab);
  }
}

async function selectRow(tab, rowId) {
  if (state.busy) {
    return;
  }

  const tabState = state.tabs[tab];
  tabState.selectedId = rowId;
  renderTable(tab);

  if (tab === "pipelines") {
    const pipeline = tabState.items.find((item) => item.id === rowId);
    if (pipeline) {
      renderDetail("pipelines", pipeline, {});
    }
    return;
  }

  setBusy(true);
  clearDetail("Cargando detalle...");
  try {
    const payload = await apiGet(`/api/hubspot/${tab}/${encodeURIComponent(rowId)}`);
    renderDetail(tab, payload.item || {}, payload.associations || {});
  } catch (error) {
    clearDetail(`No se pudo cargar el detalle: ${error.message}`);
    setAlert(error.message);
  } finally {
    setBusy(false);
    renderPager(tab);
  }
}

async function goToNextPage() {
  const tab = state.activeTab;
  if (tab === "pipelines" || state.busy) {
    return;
  }

  const tabState = state.tabs[tab];
  if (!tabState.nextAfter) {
    return;
  }

  const nextIndex = tabState.index + 1;
  if (tabState.history.length <= nextIndex) {
    tabState.history.push(tabState.nextAfter);
  } else {
    tabState.history[nextIndex] = tabState.nextAfter;
  }
  tabState.index = nextIndex;
  await loadTabData(tab);
}

async function goToPrevPage() {
  const tab = state.activeTab;
  if (tab === "pipelines" || state.busy) {
    return;
  }

  const tabState = state.tabs[tab];
  if (tabState.index <= 0) {
    return;
  }

  tabState.index -= 1;
  await loadTabData(tab);
}

async function applySearch() {
  if (state.busy) {
    return;
  }

  const tab = state.activeTab;
  if (tab === "pipelines") {
    return;
  }

  state.filters[tab].q = el.searchInput.value.trim();
  await loadTabData(tab, { resetCursor: true });
}

async function setActiveTab(tab) {
  if (!TAB_LABELS[tab] || state.activeTab === tab) {
    return;
  }

  state.activeTab = tab;
  el.tabs.forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });

  if (tab === "pipelines") {
    el.searchInput.value = "";
    el.dealFilters.classList.add("hidden");
    syncControlAvailability();
    renderTable("pipelines");
    clearDetail();
    return;
  }

  el.searchInput.value = state.filters[tab].q || "";
  el.dealFilters.classList.toggle("hidden", tab !== "deals");
  syncControlAvailability();
  renderPager(tab);

  await loadTabData(tab);
}

async function refreshCurrentTab() {
  if (state.busy) {
    return;
  }

  const tab = state.activeTab;
  if (tab === "pipelines") {
    await loadPipelines(true);
    renderTable("pipelines");
    return;
  }

  await loadTabData(tab, { forceRefresh: true });
}

async function initializeHealth() {
  try {
    const health = await apiGet("/api/hubspot/health");
    if (health.ok) {
      setStatus("HubSpot listo", "ok");
      setAlert();
      return;
    }
    setStatus("HubSpot sin token", "error");
    setAlert(health.message || "Falta configuración de HubSpot.");
  } catch (error) {
    setStatus("HubSpot no disponible", "error");
    setAlert(error.message);
  }
}

function bindEvents() {
  el.tabs.forEach((button) => {
    button.addEventListener("click", () => {
      void setActiveTab(button.dataset.tab);
    });
  });

  el.searchButton.addEventListener("click", () => {
    void applySearch();
  });

  el.searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void applySearch();
    }
  });

  el.pipelineFilter.addEventListener("change", () => {
    state.filters.deals.pipelineId = el.pipelineFilter.value;
    state.filters.deals.stageId = "";
    renderPipelineOptions();
    if (state.activeTab === "deals") {
      void loadTabData("deals", { resetCursor: true });
    }
  });

  el.stageFilter.addEventListener("change", () => {
    state.filters.deals.stageId = el.stageFilter.value;
    if (state.activeTab === "deals") {
      void loadTabData("deals", { resetCursor: true });
    }
  });

  el.prevPage.addEventListener("click", () => {
    void goToPrevPage();
  });

  el.nextPage.addEventListener("click", () => {
    void goToNextPage();
  });

  el.forceRefresh.addEventListener("click", () => {
    void refreshCurrentTab();
  });
}

async function boot() {
  bindEvents();
  clearDetail();
  syncControlAvailability();
  setStatus("Inicializando...", "ok");

  await initializeHealth();

  try {
    await loadPipelines();
  } catch (error) {
    setAlert(error.message);
  }

  renderTable("deals");
  await loadTabData("deals", { resetCursor: true });
}

void boot();
