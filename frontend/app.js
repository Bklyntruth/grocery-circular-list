const $ = (sel) => document.querySelector(sel);

const API_BASE = (() => {
  if (window.location.protocol === "file:" || window.location.origin === "null") {
    return "http://127.0.0.1:8000";
  }
  // Dev: frontend on :3333, backend on :8000
  if (window.location.port === "3333") return "http://127.0.0.1:8000";
  return window.location.origin;
})();
const api = (path) => `${API_BASE}${path}`;

let lastLocation = "";
let allStores = [];
const storeById = new Map();
const circularCache = new Map(); // storeId → CircularItem[]

// ---------- helpers ----------

function parsePriceAmount(price) {
  if (!price) return null;
  let m;
  if ((m = price.match(/(\d+)\s*\/\s*\$?([\d.]+)/))) return parseFloat(m[2]) / parseInt(m[1]);
  if ((m = price.match(/(\d+)\s+for\s+\$?([\d.]+)/i))) return parseFloat(m[2]) / parseInt(m[1]);
  if ((m = price.match(/\$?([\d.]+)/))) return parseFloat(m[1]);
  return null;
}

function computeUnitPrice(price, unit) {
  if (!price || !unit) return null;
  let dollars = parsePriceAmount(price);
  if (!dollars || dollars <= 0) return null;
  const patterns = [
    [/([\d.]+)\s*fl\.?\s*oz/i, "fl oz"],
    [/([\d.]+)\s*oz/i, "oz"],
    [/([\d.]+)\s*lb/i, "lb"],
    [/([\d.]+)\s*kg/i, "kg"],
    [/([\d.]+)\s*g\b/i, "g"],
    [/([\d.]+)\s*ml\b/i, "ml"],
    [/([\d.]+)\s*l\b/i, "L"],
    [/([\d.]+)\s*(?:ct|count|pcs?|piece)/i, "ct"],
    [/([\d.]+)\s*(?:pk|pack)/i, "pk"],
  ];
  for (const [re, label] of patterns) {
    const m = unit.match(re);
    if (m) {
      const qty = parseFloat(m[1]);
      if (qty > 0) return `$${(dollars / qty).toFixed(2)}/${label}`;
    }
  }
  return null;
}

function dealExpiry(validTo) {
  if (!validTo) return null;
  const days = Math.ceil((new Date(validTo + "T23:59:59") - Date.now()) / 86400000);
  if (days < 0) return null; // don't show expired
  if (days === 0) return "Expires today!";
  if (days === 1) return "Expires tomorrow";
  if (days <= 3) return `Expires in ${days} days`;
  return null;
}

const AISLE_ORDER = ["Produce", "Bakery", "Bread & Bakery", "Deli", "Meat & Seafood", "Meat", "Seafood", "Dairy", "Dairy & Eggs", "Frozen", "Breakfast & Cereal", "Pantry", "Beverages", "Snacks", "Condiments", "Household", "Personal Care", "Health & Beauty", "Baby", "Pet", "Kosher", "Natural & Organic", "Other"];

function aisleRank(cat) {
  const idx = AISLE_ORDER.indexOf(cat ?? "Other");
  return idx >= 0 ? idx : AISLE_ORDER.length;
}

// ---------- map ----------
let _map = null;
let _centerMarker = null;
let _storeLayer = null;

function makePin(cls) {
  return L.divIcon({ className: "", html: `<div class="map-pin ${cls}"></div>`, iconSize: [14, 14], iconAnchor: [7, 7], popupAnchor: [0, -8] });
}

function showMap(centerLat, centerLon, stores) {
  const el = document.getElementById("map");
  el.hidden = false;

  if (!_map) {
    _map = L.map("map");
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19,
    }).addTo(_map);
    _storeLayer = L.layerGroup().addTo(_map);
  }

  if (_centerMarker) _centerMarker.remove();
  _storeLayer.clearLayers();

  _centerMarker = L.marker([centerLat, centerLon], { icon: makePin("center") })
    .addTo(_map)
    .bindPopup("<strong>Your search location</strong>");

  const bounds = [[centerLat, centerLon]];
  stores.forEach((s) => {
    const miles = s.distance_m ? (s.distance_m / 1609).toFixed(1) : "?";
    const btn = s.adapter
      ? `<br><button onclick="loadCircular('${s.id}')">View circular</button>`
      : "";
    const popup = `<strong>${s.name}</strong><br><span style="color:#666;font-size:0.8rem">${s.address || ""}</span><br>${miles} mi away${btn}`;
    L.marker([s.lat, s.lon], { icon: makePin(s.adapter ? "has-circular" : "no-circular") })
      .addTo(_storeLayer)
      .bindPopup(popup);
    bounds.push([s.lat, s.lon]);
  });

  _map.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });
}

// ---------- store search ----------

document.getElementById("search").addEventListener("submit", async (e) => {
  e.preventDefault();
  lastLocation = $("#location").value.trim();
  const miles = parseFloat($("#radius").value);
  const radius_m = Math.round(miles * 1609);
  const results = $("#results");
  results.innerHTML = "<p>Searching...</p>";
  $("#circular").innerHTML = "";
  $("#filter-bar").hidden = true;
  $("#filter-input").value = "";
  document.getElementById("map").hidden = true;
  allStores = [];
  storeById.clear();
  try {
    const [storesR, geoR] = await Promise.all([
      fetch(api(`/api/stores?location=${encodeURIComponent(lastLocation)}&radius_m=${radius_m}`)),
      fetch(api(`/api/geocode?location=${encodeURIComponent(lastLocation)}`)),
    ]);
    if (!storesR.ok) throw new Error(await storesR.text());
    const stores = await storesR.json();
    if (!stores.length) {
      results.innerHTML = "<p class='empty'>No grocery stores found in that radius.</p>";
      return;
    }
    stores.forEach((s) => storeById.set(s.id, s));
    allStores = stores;
    renderStoreList(stores);
    if (stores.length > 4) {
      $("#filter-bar").hidden = false;
    }
    if (geoR.ok) {
      const { lat, lon } = await geoR.json();
      showMap(lat, lon, stores);
    }
  } catch (err) {
    results.innerHTML = `<p class='empty'>Error: ${err.message}</p>`;
  }
});

// ---------- filter ----------

$("#filter-input").addEventListener("input", () => {
  const q = $("#filter-input").value.trim().toLowerCase();
  const filtered = q
    ? allStores.filter((s) => s.name.toLowerCase().includes(q))
    : allStores;
  renderStoreList(filtered, q);
});

function renderStoreList(stores, filterQuery = "") {
  const results = $("#results");
  if (!stores.length) {
    results.innerHTML = filterQuery
      ? `<p class='empty'>No stores match "${filterQuery}".</p>`
      : "<p class='empty'>No stores found.</p>";
    return;
  }
  results.innerHTML = stores.map(renderStore).join("");
  results.querySelectorAll("button[data-id]").forEach((btn) => {
    btn.addEventListener("click", (e) => { e.stopPropagation(); loadCircular(btn.dataset.id); });
  });
  results.querySelectorAll(".store[data-store-id]").forEach((card) => {
    card.addEventListener("click", () => {
      const s = storeById.get(card.dataset.storeId);
      if (s && _map) _map.flyTo([s.lat, s.lon], 16, { duration: 0.8 });
      document.querySelectorAll(".store").forEach(el => el.classList.remove("active"));
      card.classList.add("active");
    });
  });
  $("#store-count").textContent =
    filterQuery && stores.length < allStores.length
      ? `${stores.length} of ${allStores.length} stores`
      : `${allStores.length} stores`;
}

function renderStore(s) {
  const miles = s.distance_m ? (s.distance_m / 1609).toFixed(1) : "?";
  const badge = s.adapter
    ? `<span class="badge">circular: ${s.adapter}</span>`
    : `<span class="badge muted">no parser yet</span>`;
  const btn = s.adapter
    ? `<button data-id="${s.id}">View circular</button>`
    : `<button disabled>No circular</button>`;
  return `
    <div class="store" data-store-id="${s.id}">
      <h3>${s.name} ${badge}</h3>
      <div class="meta">${miles} mi${s.address ? " &middot; " + s.address : ""}</div>
      ${btn}
    </div>
  `;
}

// ---------- circular ----------

function renderItem(it, store) {
  const img = it.image_url ? `<img class="item-img" src="${it.image_url}" alt="" loading="lazy">` : "";
  const sub = it.unit ? `<small class="item-unit">${escHtml(it.unit)}</small>` : "";
  const expiry = dealExpiry(it.valid_to);
  const unitPrice = computeUnitPrice(it.price, it.unit);
  const addBtn = `<button class="add-btn" title="Add to list" data-name="${escHtml(it.name)}" data-price="${escHtml(it.price||"")}" data-unit="${escHtml(it.unit||"")}" data-category="${escHtml(it.category||"")}" data-img="${escHtml(it.image_url||"")}" data-store-id="${escHtml(store.id)}" data-store-name="${escHtml(store.name)}">+</button>`;
  return `
    <div class="item">
      ${img}
      <div class="item-info">
        <span>${escHtml(it.name)}</span>
        ${sub}
        ${expiry ? `<small class="deal-expiry">${expiry}</small>` : ""}
      </div>
      <div class="item-price-col">
        <strong class="item-price">${it.price ?? ""}</strong>
        ${unitPrice ? `<small class="item-unit-price">${unitPrice}</small>` : ""}
      </div>
      ${addBtn}
    </div>
  `;
}

function escHtml(s) {
  return String(s).replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

async function loadCircular(id) {
  const store = storeById.get(id);
  if (!store) return;

  // Highlight the selected store card
  document.querySelectorAll(".store").forEach(el => el.classList.remove("active"));
  const activeCard = document.querySelector(`.store[data-store-id="${id}"]`);
  if (activeCard) activeCard.classList.add("active");

  // Zoom the map to this store
  if (_map && store.lat && store.lon) {
    _map.flyTo([store.lat, store.lon], 16, { duration: 0.8 });
  }

  const box = $("#circular");
  box.innerHTML = `
    <h2>${store.name}</h2>
    <p class="loading">Fetching circular... first load with prices takes 1–2 min.</p>
  `;
  box.scrollIntoView({ behavior: "smooth", block: "start" });

  const params = new URLSearchParams({
    location: lastLocation,
    name: store.name,
    lat: store.lat,
    lon: store.lon,
  });
  if (store.brand) params.set("brand", store.brand);
  if (store.ref) params.set("ref", store.ref);

  try {
    const r = await fetch(api(`/api/circulars/${encodeURIComponent(id)}?${params}`));
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    if (!data.items.length) {
      box.innerHTML = `
        <h2>${store.name}</h2>
        <p class='empty'>No circular available for this store in the requested area.</p>
      `;
      return;
    }
    box.innerHTML = `
      <h2>${store.name}</h2>
      <p class="meta">${data.items.length} items &middot; fetched ${new Date(data.fetched_at).toLocaleTimeString()}</p>
    ` + data.items.map((it) => renderItem(it, store)).join("");
    circularCache.set(id, data.items);
    updateBestStoreBanner();

    box.querySelectorAll(".add-btn").forEach((btn) => {
      btn.addEventListener("click", () => addToList(btn, store));
    });
  } catch (err) {
    box.innerHTML = `<h2>${store.name}</h2><p class='empty'>Error: ${err.message}</p>`;
  }
}

// ---------- grocery lists ----------

let allLists = [];
let activeListId = null;
let groceryItems = [];
let _listSectionOpen = false;

async function initLists() {
  try {
    const r = await fetch(api("/api/lists"));
    allLists = r.ok ? await r.json() : [];
  } catch {
    allLists = [];
  }

  if (!allLists.length) {
    try {
      const r = await fetch(api("/api/lists"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "My List" }),
      });
      if (r.ok) allLists = [await r.json()];
    } catch {}
  }

  const saved = localStorage.getItem("activeListId");
  activeListId = (saved && allLists.find((l) => l.id === saved))
    ? saved
    : allLists[0]?.id ?? null;

  if (activeListId) localStorage.setItem("activeListId", activeListId);

  renderListSelector();
  await loadListItems();
}

async function loadListItems() {
  if (!activeListId) { groceryItems = []; renderList(); updateListBadge(); return; }
  try {
    const r = await fetch(api(`/api/lists/${activeListId}/items`));
    groceryItems = r.ok ? await r.json() : [];
  } catch {
    groceryItems = [];
  }
  renderList();
  updateListBadge();
}

async function switchList(id) {
  activeListId = id;
  localStorage.setItem("activeListId", id);
  renderListSelector();
  await loadListItems();
}

function renderListSelector() {
  const sel = document.getElementById("list-select");
  if (!sel) return;
  sel.innerHTML = allLists.map((l) =>
    `<option value="${l.id}"${l.id === activeListId ? " selected" : ""}>${escHtml(l.name)} (${l.item_count})</option>`
  ).join("");
  const delBtn = document.getElementById("delete-list-btn");
  if (delBtn) delBtn.disabled = allLists.length <= 1;
}

document.getElementById("list-select").addEventListener("change", (e) => switchList(e.target.value));

document.getElementById("new-list-btn").addEventListener("click", async () => {
  const name = prompt("New list name:", "My List");
  if (!name?.trim()) return;
  try {
    const r = await fetch(api("/api/lists"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    if (!r.ok) throw new Error();
    const lst = await r.json();
    allLists.push(lst);
    await switchList(lst.id);
    showListSection();
  } catch {}
});

document.getElementById("rename-list-btn").addEventListener("click", async () => {
  const lst = allLists.find((l) => l.id === activeListId);
  if (!lst) return;
  const name = prompt("Rename list:", lst.name);
  if (!name?.trim() || name.trim() === lst.name) return;
  try {
    const r = await fetch(api(`/api/lists/${activeListId}`), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    if (!r.ok) throw new Error();
    lst.name = name.trim();
    renderListSelector();
  } catch {}
});

document.getElementById("delete-list-btn").addEventListener("click", async () => {
  if (allLists.length <= 1) { alert("Cannot delete the only list."); return; }
  const lst = allLists.find((l) => l.id === activeListId);
  if (!confirm(`Delete "${lst?.name}"? This cannot be undone.`)) return;
  try {
    const r = await fetch(api(`/api/lists/${activeListId}`), { method: "DELETE" });
    if (!r.ok) throw new Error();
    allLists = allLists.filter((l) => l.id !== activeListId);
    await switchList(allLists[0].id);
  } catch {}
});

document.getElementById("clear-list-btn").addEventListener("click", async () => {
  if (!activeListId || !groceryItems.length) return;
  if (!confirm("Clear all items from this list?")) return;
  await fetch(api(`/api/lists/${activeListId}/items`), { method: "DELETE" });
  groceryItems = [];
  const lst = allLists.find((l) => l.id === activeListId);
  if (lst) lst.item_count = 0;
  renderListSelector();
  renderList();
  updateListBadge();
});

document.getElementById("print-list-btn").addEventListener("click", () => {
  const lst = allLists.find((l) => l.id === activeListId);
  const listName = lst?.name || "Grocery List";
  const date = new Date().toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });

  const byStore = {};
  for (const it of groceryItems) {
    const sKey = it.store_name || "Unknown Store";
    if (!byStore[sKey]) byStore[sKey] = {};
    const cKey = it.category || "Other";
    if (!byStore[sKey][cKey]) byStore[sKey][cKey] = [];
    byStore[sKey][cKey].push(it);
  }

  let body = "";
  for (const [store, cats] of Object.entries(byStore)) {
    body += `<h2>${store}</h2>`;
    for (const [cat, items] of Object.entries(cats)) {
      body += `<h3>${cat}</h3>`;
      for (const it of items) {
        const strike = it.checked ? " style=\"text-decoration:line-through;color:#999\"" : "";
        const price = it.price ? ` <span class="price">${it.price}</span>` : "";
        const unit = it.unit ? ` <span class="unit">${it.unit}</span>` : "";
        body += `<div class="row"><div class="box${it.checked ? " done" : ""}"></div><span${strike}>${it.name}${unit}${price}</span></div>`;
      }
    }
  }

  const html = `<!doctype html><html><head><meta charset="utf-8"><title>${listName}</title><style>
    body{font-family:Arial,sans-serif;margin:2cm;color:#000;font-size:10pt}
    h1{font-size:16pt;margin:0 0 2pt}
    .sub{font-size:9pt;color:#666;margin:0 0 18pt}
    h2{font-size:13pt;margin:20pt 0 4pt;border-bottom:1.5pt solid #000;padding-bottom:2pt}
    h3{font-size:9pt;text-transform:uppercase;letter-spacing:.05em;color:#555;margin:10pt 0 4pt 6pt}
    .row{display:flex;align-items:baseline;gap:7pt;padding:3pt 0 3pt 14pt;border-bottom:.5pt solid #eee}
    .box{width:11pt;height:11pt;border:1pt solid #444;flex-shrink:0;margin-top:1pt}
    .box.done{background:#444}
    .unit{color:#888;font-size:8.5pt}
    .price{font-weight:bold;margin-left:auto;white-space:nowrap}
  </style></head><body>
  <h1>${listName}</h1><p class="sub">Printed ${date}</p>
  ${body || "<p style=\"color:#999\">No items.</p>"}
  </body></html>`;

  const w = window.open("", "_blank");
  if (w) { w.document.write(html); w.document.close(); w.print(); }
});

// Show the add-item bottom sheet; actual POST happens on confirm
function addToList(btn, store) {
  if (!activeListId) return;

  const popup   = document.getElementById("add-popup");
  const sheet   = document.getElementById("add-popup-sheet");
  const imgEl   = document.getElementById("add-popup-img");
  const nameEl  = document.getElementById("add-popup-name");
  const priceEl = document.getElementById("add-popup-price");
  const storeEl = document.getElementById("add-popup-store");
  const listLbl = document.getElementById("add-popup-list-name");
  const confirmBtn = document.getElementById("add-popup-confirm");
  const cancelBtn  = document.getElementById("add-popup-cancel");

  // Populate
  const name    = btn.dataset.name || "";
  const price   = btn.dataset.price || "";
  const imgSrc  = btn.dataset.img || "";
  const activeList = allLists.find(l => l.id === activeListId);

  nameEl.textContent  = name;
  priceEl.textContent = price;
  storeEl.textContent = store.name || "";
  listLbl.textContent = activeList?.name || "My List";

  if (imgSrc) {
    imgEl.src = imgSrc; imgEl.hidden = false;
  } else {
    imgEl.hidden = true;
  }

  confirmBtn.textContent = "Add to List";
  confirmBtn.disabled = false;
  confirmBtn.classList.remove("done");

  // Show popup
  popup.hidden = false;
  requestAnimationFrame(() => popup.classList.add("show"));

  function closePopup() {
    popup.classList.remove("show");
    sheet.addEventListener("transitionend", () => { popup.hidden = true; }, { once: true });
  }

  // Backdrop or cancel closes
  const backdrop = document.getElementById("add-popup-backdrop");
  const onBackdrop = () => { closePopup(); backdrop.removeEventListener("click", onBackdrop); };
  backdrop.addEventListener("click", onBackdrop);
  const onCancel = () => { closePopup(); cancelBtn.removeEventListener("click", onCancel); };
  cancelBtn.addEventListener("click", onCancel);

  // Confirm: POST then show success
  const onConfirm = async () => {
    confirmBtn.removeEventListener("click", onConfirm);
    confirmBtn.disabled = true;
    confirmBtn.textContent = "Adding…";

    const payload = {
      store_id: btn.dataset.storeId,
      store_name: btn.dataset.storeName,
      name,
      price: btn.dataset.price || null,
      unit: btn.dataset.unit || null,
      category: btn.dataset.category || null,
      image_url: imgSrc || null,
    };

    try {
      const r = await fetch(api(`/api/lists/${activeListId}/items`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error();
      const item = await r.json();
      groceryItems.push(item);
      const lst = allLists.find((l) => l.id === activeListId);
      if (lst) lst.item_count++;
      renderListSelector();
      btn.textContent = "✓";
      btn.disabled = true;
      renderList();
      updateListBadge();
      _updateListPanelCount();
      updateBestStoreBanner();

      // Show success state then close
      confirmBtn.textContent = "✓ Added!";
      confirmBtn.classList.add("done");
      setTimeout(() => {
        closePopup();
        backdrop.removeEventListener("click", onBackdrop);
        cancelBtn.removeEventListener("click", onCancel);
      }, 800);
    } catch {
      confirmBtn.textContent = "Add to List";
      confirmBtn.disabled = false;
      btn.textContent = "!";
    }
  };
  confirmBtn.addEventListener("click", onConfirm);
}

async function toggleItem(id, checked) {
  await fetch(api(`/api/lists/${activeListId}/items/${id}`), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ checked }),
  });
  const it = groceryItems.find((i) => i.id === id);
  if (it) it.checked = checked;
}

async function removeItem(id) {
  await fetch(api(`/api/lists/${activeListId}/items/${id}`), { method: "DELETE" });
  groceryItems = groceryItems.filter((i) => i.id !== id);
  const lst = allLists.find((l) => l.id === activeListId);
  if (lst) lst.item_count = Math.max(0, lst.item_count - 1);
  renderListSelector();
  renderList();
  updateListBadge();
  updateBestStoreBanner();
}

function updateListBadge() {
  const badge = $("#list-badge");
  const total = groceryItems.length;
  badge.textContent = total;
  badge.hidden = total === 0;
}

function openListPanel() {
  const panel = document.getElementById("list-panel");
  panel.hidden = false;
  requestAnimationFrame(() => panel.classList.add("open"));
  _updateListPanelCount();
  // mark nav tab active
  document.querySelectorAll(".bottom-nav a").forEach(a => a.classList.remove("active"));
  const navList = document.getElementById("nav-list");
  if (navList) navList.classList.add("active");
}

function closeListPanel() {
  const panel = document.getElementById("list-panel");
  panel.classList.remove("open");
  panel.addEventListener("transitionend", () => { panel.hidden = true; }, { once: true });
  document.querySelectorAll(".bottom-nav a").forEach(a => a.classList.remove("active"));
  const navStores = document.getElementById("nav-stores");
  if (navStores) navStores.classList.add("active");
}

function _updateListPanelCount() {
  const el = document.getElementById("list-panel-count");
  if (!el) return;
  const n = groceryItems.length;
  el.textContent = n > 0 ? n : "";
  el.hidden = n === 0;
}

function showListSection() { openListPanel(); }

function renderList() {
  const body = $("#list-body");

  if (!body) return;

  if (!groceryItems.length) {
    body.innerHTML = "<p class='empty'>This list is empty — add items from a store circular above.</p>";
    return;
  }

  const byStore = {};
  for (const it of groceryItems) {
    const sKey = it.store_name || "Unknown Store";
    if (!byStore[sKey]) byStore[sKey] = {};
    const cKey = it.category || "Other";
    if (!byStore[sKey][cKey]) byStore[sKey][cKey] = [];
    byStore[sKey][cKey].push(it);
  }

  let html = "";
  for (const [store, cats] of Object.entries(byStore)) {
    const allStoreItems = Object.values(cats).flat();
    const storeTotal = allStoreItems.reduce((s, it) => s + (parsePriceAmount(it.price) || 0), 0);
    const totalStr = storeTotal > 0 ? `<span class="list-store-meta">~$${storeTotal.toFixed(2)} est.</span>` : "";
    html += `<div class="list-store"><div class="list-store-header"><div><h3 class="list-store-name">${escHtml(store)}</h3>${totalStr}</div><button class="shop-mode-btn compact-btn" data-store="${escHtml(store)}">Shop</button></div>`;
    for (const [cat, items] of Object.entries(cats)) {
      html += `<div class="list-category"><h4 class="list-cat-name">${escHtml(cat)}</h4>`;
      for (const it of items) {
        const checked = it.checked ? "checked" : "";
        const rowCls = it.checked ? "list-item checked" : "list-item";
        const img = it.image_url ? `<img class="list-item-img" src="${it.image_url}" alt="" loading="lazy">` : "";
        html += `
          <div class="${rowCls}" data-id="${it.id}">
            <label class="list-check"><input type="checkbox" ${checked} data-id="${it.id}"></label>
            ${img}
            <div class="list-item-info">
              <span class="list-item-name">${escHtml(it.name)}</span>
              ${it.unit ? `<small class="list-item-unit">${escHtml(it.unit)}</small>` : ""}
            </div>
            <strong class="list-item-price">${escHtml(it.price || "")}</strong>
            <button class="remove-btn" data-id="${it.id}" title="Remove">&times;</button>
          </div>`;
      }
      html += `</div>`;
    }
    html += `</div>`;
  }

  body.innerHTML = html;

  body.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", () => {
      toggleItem(cb.dataset.id, cb.checked);
      const row = body.querySelector(`.list-item[data-id="${cb.dataset.id}"]`);
      if (row) row.classList.toggle("checked", cb.checked);
    });
  });

  body.querySelectorAll(".remove-btn").forEach((btn) => {
    btn.addEventListener("click", () => removeItem(btn.dataset.id));
  });

  body.querySelectorAll(".shop-mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => enterShopMode(btn.dataset.store));
  });
}

document.getElementById("list-btn").addEventListener("click", () => {
  showListSection();
});

// ---------- settings ----------

async function loadSettings() {
  try {
    const r = await fetch(api("/api/settings"));
    if (!r.ok) return;
    const { gemini_key_hint } = await r.json();
    if (gemini_key_hint) $("#gemini-key-input").placeholder = gemini_key_hint;
  } catch {}
}

document.getElementById("settings-btn").addEventListener("click", () => {
  $("#settings-modal").hidden = false;
});

document.getElementById("close-settings-btn").addEventListener("click", () => {
  $("#settings-modal").hidden = true;
});

document.getElementById("save-settings-btn").addEventListener("click", async () => {
  const key = $("#gemini-key-input").value.trim();
  if (!key) return;
  try {
    const r = await fetch(api("/api/settings"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gemini_key: key }),
    });
    if (r.ok) {
      $("#settings-modal").hidden = true;
      $("#gemini-key-input").value = "";
      alert("API key saved.");
    }
  } catch {}
});

// Close modal on backdrop click
document.getElementById("settings-modal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.hidden = true;
});

// ---------- shopping mode ----------

function enterShopMode(storeName) {
  const items = groceryItems
    .filter((it) => (it.store_name || "Unknown Store") === storeName && !it.checked)
    .sort((a, b) => aisleRank(a.category) - aisleRank(b.category));

  document.getElementById("shop-store-name").textContent = storeName;

  if (!items.length) {
    document.getElementById("shop-items").innerHTML = `<div class="shop-all-done">All items for ${escHtml(storeName)} are already checked!</div>`;
    document.getElementById("shop-progress").textContent = "0 left";
    document.getElementById("shop-mode").hidden = false;
    document.body.style.overflow = "hidden";
    return;
  }

  let html = "";
  let lastCat = null;
  for (const it of items) {
    const cat = it.category || "Other";
    if (cat !== lastCat) {
      html += `<div class="shop-cat-label">${escHtml(cat)}</div>`;
      lastCat = cat;
    }
    const img = it.image_url ? `<img class="shop-item-img" src="${it.image_url}" alt="">` : "";
    const unit = it.unit ? `<div class="shop-item-unit">${escHtml(it.unit)}</div>` : "";
    const price = it.price ? `<span class="shop-item-price">${escHtml(it.price)}</span>` : "";
    html += `<div class="shop-item" data-id="${it.id}">${img}<div class="shop-item-info"><div class="shop-item-name">${escHtml(it.name)}</div>${unit}</div>${price}<div class="shop-check"></div></div>`;
  }

  const container = document.getElementById("shop-items");
  container.innerHTML = html;
  updateShopProgress();

  container.querySelectorAll(".shop-item").forEach((el) => {
    el.addEventListener("click", () => handleShopTap(el));
  });

  document.getElementById("shop-mode").hidden = false;
  document.getElementById("shop-mode").scrollTop = 0;
  document.body.style.overflow = "hidden";
}

async function handleShopTap(el) {
  if (el.classList.contains("shop-done")) return;
  el.querySelector(".shop-check").textContent = "✓";
  el.querySelector(".shop-check").style.cssText = "background:#1d4ed8;color:white;border-color:#1d4ed8";
  el.classList.add("shop-done");
  const id = el.dataset.id;
  const it = groceryItems.find((i) => i.id === id);
  if (it) it.checked = true;
  await toggleItem(id, true);
  updateShopProgress();
}

function updateShopProgress() {
  const total = document.querySelectorAll(".shop-item").length;
  const done = document.querySelectorAll(".shop-item.shop-done").length;
  const remaining = total - done;
  document.getElementById("shop-progress").textContent = `${remaining} left`;
  if (remaining === 0 && total > 0) {
    setTimeout(() => {
      const c = document.getElementById("shop-items");
      if (c && !c.querySelector(".shop-all-done")) {
        c.innerHTML += `<div class="shop-all-done">All done! Great shopping!</div>`;
      }
    }, 400);
  }
}

function exitShopMode() {
  document.getElementById("shop-mode").hidden = true;
  document.body.style.overflow = "";
  renderList();
  updateListBadge();
}

document.getElementById("shop-exit-btn").addEventListener("click", exitShopMode);

// ---------- best store for list ----------

function updateBestStoreBanner() {
  const banner = document.getElementById("best-store-banner");
  if (!banner || !groceryItems.length || !circularCache.size) {
    if (banner) banner.hidden = true;
    return;
  }

  const listNames = groceryItems.map((it) => it.name.toLowerCase());
  const results = [];

  for (const [storeId, items] of circularCache) {
    const store = storeById.get(storeId);
    if (!store) continue;
    let matches = 0;
    for (const name of listNames) {
      const words = name.split(/\s+/).filter((w) => w.length >= 4);
      if (words.some((w) => items.some((ci) => ci.name.toLowerCase().includes(w)))) matches++;
    }
    if (matches > 0) results.push({ store, storeId, matches });
  }

  if (!results.length) { banner.hidden = true; return; }
  results.sort((a, b) => b.matches - a.matches);

  const best = results[0];
  const total = listNames.length;
  let html = `<strong>${escHtml(best.store.name)}</strong> has deals on <strong>${best.matches}</strong> of your <strong>${total}</strong> list item${total !== 1 ? "s" : ""}`;
  if (results.length > 1) {
    html += ` &nbsp;&middot;&nbsp;` + results.slice(1).map((r) => ` ${escHtml(r.store.name)}: ${r.matches}`).join(" &middot;");
  }
  html += `&nbsp; <button onclick="loadCircular('${best.storeId}')">View deals</button>`;

  banner.hidden = false;
  banner.innerHTML = html;
}

// ---------- bottom nav ----------
document.getElementById("nav-stores").addEventListener("click", (e) => {
  e.preventDefault();
  closeListPanel();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

document.getElementById("nav-list").addEventListener("click", (e) => {
  e.preventDefault();
  openListPanel();
});

document.getElementById("nav-settings").addEventListener("click", (e) => {
  e.preventDefault();
  document.getElementById("settings-btn").click();
  // keep stores tab visually active since settings is a modal
});

document.getElementById("list-panel-back").addEventListener("click", closeListPanel);

// ---------- init ----------
initLists();
loadSettings();
