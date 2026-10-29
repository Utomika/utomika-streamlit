/* Keep the map and existing tile DOM alive across Streamlit render messages. */
(() => {
  "use strict";
  let map, boundary, args, preferences, city, sceneId;
  const layers = new Map();
  const rows = new Map();
  let eventNumber = 0;
  const eventPrefix = `${Date.now()}-${Math.random()}`;
  let exportLibrary;
  let exporting = false;
  let exportError = null;
  const status = document.getElementById("status");

  function send(type, payload = {}) {
    window.parent.postMessage({isStreamlitMessage: true, type, ...payload}, "*");
  }

  function emit(retry = null) {
    send("streamlit:setComponentValue", {dataType: "json", value: {
      ...preferences, scene_id: sceneId, retry,
      event_id: `${eventPrefix}-${++eventNumber}`,
    }});
  }

  function visible(key) { return preferences.visible.includes(key); }

  function syncVisibility() {
    for (const [key, entry] of layers) {
      const wanted = visible(key);
      if (wanted && !map.hasLayer(entry.layer)) map.addLayer(entry.layer);
      if (!wanted && map.hasLayer(entry.layer)) map.removeLayer(entry.layer);
      if (key === "cloud") entry.layer.setOpacity(preferences.cloud_opacity);
    }
    if (preferences.boundary && !map.hasLayer(boundary)) map.addLayer(boundary);
    if (!preferences.boundary && map.hasLayer(boundary)) map.removeLayer(boundary);
  }

  function updateStatus() {
    if (!args) return;
    const waiting = args.layers.filter(item => visible(item.key) && !item.url && !item.error);
    const fetching = args.layers.filter(item => {
      const entry = layers.get(item.key);
      return visible(item.key) && entry && entry.layer.isLoading();
    });
    const failed = args.layers.filter(item => visible(item.key) &&
      (item.error || layers.get(item.key)?.tileError));
    const totalReady = args.layers.filter(item => item.url).length;
    if (exportError) {
      status.textContent = exportError;
    } else if (waiting.length) {
      status.textContent = `Preparing ${waiting.map(item => item.name).join(", ")} for this scene…`;
    } else if (fetching.length) {
      status.textContent = `Loading ${fetching.map(item => item.name).join(", ")} image tiles…`;
    } else if (failed.length) {
      status.textContent = `${failed.map(item => item.name).join(", ")} could not be loaded. Use Retry in the layer menu.`;
    } else if (!preferences.visible.length) {
      status.textContent = "Select a scene layer in the menu.";
    } else {
      status.textContent = totalReady === args.layers.length
        ? "All map layers are ready."
        : `Selected layers are ready. Preparing other layers (${totalReady}/${args.layers.length} ready)…`;
    }
    // Do not capture a mixture of old and partially loaded new pixels.
    const camera = document.getElementById("export-map");
    if (camera) camera.disabled = exporting || !!(waiting.length || fetching.length || failed.length);
  }

  function updateRows() {
    for (const item of args.layers) {
      const row = rows.get(item.key);
      const tileError = layers.get(item.key)?.tileError;
      row.checkbox.checked = visible(item.key);
      row.state.textContent = item.error || tileError ? "Failed" : item.url ? "Ready" : "Preparing…";
      row.state.className = item.error || tileError ? "failed" : "";
      row.state.title = item.error || tileError || "";
      row.retry.hidden = !(item.error || tileError);
    }
  }

  function addMenu() {
    const control = L.control({position: "topright"});
    control.onAdd = () => {
      const panel = L.DomUtil.create("div", "scene-layers");
      panel.setAttribute("aria-label", "Map layers");
      L.DomEvent.disableClickPropagation(panel);
      L.DomEvent.disableScrollPropagation(panel);
      for (const item of args.layers) {
        const label = document.createElement("label");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.setAttribute("aria-label", item.name);
        checkbox.checked = visible(item.key);
        const title = document.createElement("span");
        title.textContent = item.name;
        const state = document.createElement("small");
        const retry = document.createElement("button");
        retry.type = "button";
        retry.textContent = "Retry";
        retry.setAttribute("aria-label", `Retry ${item.name}`);
        retry.hidden = true;
        retry.addEventListener("click", event => {
          event.preventDefault();
          event.stopPropagation();
          const entry = layers.get(item.key);
          if (entry) { map.removeLayer(entry.layer); layers.delete(item.key); }
          const current = args.layers.find(layer => layer.key === item.key);
          current.url = null;
          current.error = null;
          updateRows();
          updateStatus();
          emit(item.key);
        });
        checkbox.addEventListener("change", () => {
          preferences.visible = preferences.visible.filter(key => key !== item.key);
          if (checkbox.checked) {
            preferences.visible.push(item.key);
            if (item.key !== "cloud") preferences.preferred = item.key;
          }
          syncVisibility();
          updateStatus();
          emit();
        });
        label.append(checkbox, title, state, retry);
        panel.append(label);
        rows.set(item.key, {checkbox, state, retry});
      }
      const divider = document.createElement("div");
      divider.className = "scene-divider";
      panel.append(divider);
      const opacityLabel = document.createElement("label");
      opacityLabel.textContent = "Cloud opacity";
      const opacity = document.createElement("input");
      opacity.type = "range";
      opacity.min = 0;
      opacity.max = 1;
      opacity.step = 0.05;
      opacity.value = preferences.cloud_opacity;
      opacity.setAttribute("aria-label", "Cloud mask opacity");
      opacity.addEventListener("input", () => {
        preferences.cloud_opacity = Number(opacity.value);
        syncVisibility();
      });
      opacity.addEventListener("change", () => emit());
      opacityLabel.append(opacity);
      panel.append(opacityLabel);
      const boundaryLabel = document.createElement("label");
      const boundaryCheckbox = document.createElement("input");
      boundaryCheckbox.type = "checkbox";
      boundaryCheckbox.checked = preferences.boundary;
      boundaryCheckbox.addEventListener("change", () => {
        preferences.boundary = boundaryCheckbox.checked;
        syncVisibility();
        emit();
      });
      boundaryLabel.append(boundaryCheckbox, document.createTextNode("City boundary"));
      panel.append(boundaryLabel);
      return panel;
    };
    control.addTo(map);
  }

  function loadExportLibrary() {
    if (window.htmlToImage) return Promise.resolve(window.htmlToImage);
    if (!exportLibrary) {
      exportLibrary = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "https://cdn.jsdelivr.net/npm/html-to-image@1.11.11/dist/html-to-image.js";
        script.onload = () => resolve(window.htmlToImage);
        script.onerror = () => {
          script.remove();
          exportLibrary = null;
          reject(new Error("Capture library did not load"));
        };
        document.head.append(script);
      });
    }
    return exportLibrary;
  }

  function addExport() {
    const control = L.control({position: "topleft"});
    control.onAdd = () => {
      const button = L.DomUtil.create("button", "export-control leaflet-bar");
      button.id = "export-map";
      button.type = "button";
      button.textContent = "📷";
      button.title = "Download the current map view as PNG";
      button.setAttribute("aria-label", button.title);
      L.DomEvent.disableClickPropagation(button);
      button.addEventListener("click", async () => {
        exporting = true;
        exportError = null;
        updateStatus();
        const exportScene = sceneId;
        const filename = `utomika_${args.image_id}.png`;
        button.textContent = "…";
        try {
          const library = await loadExportLibrary();
          const blob = await library.toBlob(map.getContainer(), {
            skipFonts: true, cacheBust: false,
            filter: node => !(node.classList?.contains("export-control")),
          });
          if (!blob) throw new Error("Empty image");
          if (sceneId !== exportScene) throw new Error("Scene changed during export; please retry");
          const url = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = url;
          link.download = filename;
          document.body.append(link);
          link.click();
          link.remove();
          setTimeout(() => URL.revokeObjectURL(url), 15000);
        } catch (error) {
          exportError = `PNG export failed: ${error.message || "an image could not be captured; check the connection and retry"}`;
        } finally {
          button.textContent = "📷";
          exporting = false;
          updateStatus();
        }
      });
      return button;
    };
    control.addTo(map);
  }

  function render(next) {
    args = next;
    if (!window.L) {
      status.textContent = "The map library could not load. Check your connection and reload the page.";
      return;
    }
    if (!map) {
      preferences = {...next.preferences, visible: [...next.preferences.visible]};
      map = L.map("map").setView(next.center, 11);
      L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png", {
        attribution: '&copy; OpenStreetMap contributors &copy; CARTO', crossOrigin: true,
      }).addTo(map);
      L.control.scale().addTo(map);
      // Fixed stacking order prevents late arrivals from covering the mask.
      ["true_color", "ndvi", "lst", "eci", "cloud"].forEach((key, index) => {
        map.createPane(key).style.zIndex = 300 + index * 10;
      });
      addMenu();
      addExport();
      new ResizeObserver(() => map.invalidateSize()).observe(document.getElementById("map"));
    }
    if (sceneId !== next.scene_id) {
      for (const entry of layers.values()) map.removeLayer(entry.layer);
      layers.clear();
      sceneId = next.scene_id;
      exportError = null;
    }
    if (city !== next.city) {
      if (boundary) map.removeLayer(boundary);
      boundary = L.geoJSON(next.boundary, {
        style: {color: "#e4b900", weight: 2, fill: false},
      });
      city = next.city;
      map.setView(next.center, 11);
    }
    for (const item of next.layers) {
      let entry = layers.get(item.key);
      if (entry && entry.url !== item.url) {
        map.removeLayer(entry.layer);
        layers.delete(item.key);
        entry = null;
      }
      if (item.url && !entry) {
        entry = {url: item.url, tileError: null, layer: L.tileLayer(item.url, {
          attribution: "Google Earth Engine", crossOrigin: true, pane: item.key,
          opacity: item.key === "cloud" ? preferences.cloud_opacity : 1,
        })};
        const ownEntry = entry;
        const ownScene = sceneId;
        entry.layer.on("load loading", () => {
          if (sceneId === ownScene && layers.get(item.key) === ownEntry) updateStatus();
        });
        entry.layer.on("tileerror", () => {
          if (sceneId !== ownScene || layers.get(item.key) !== ownEntry) return;
          ownEntry.tileError = "Image tiles could not load. Retry to request a fresh layer.";
          updateRows();
          updateStatus();
        });
        layers.set(item.key, entry);
      }
    }
    syncVisibility();
    updateRows();
    updateStatus();
    send("streamlit:setFrameHeight", {height: 600});
  }

  window.addEventListener("message", event => {
    if (event.source === window.parent && event.data?.type === "streamlit:render") {
      render(event.data.args);
    }
  });
  send("streamlit:componentReady", {apiVersion: 1});
})();
