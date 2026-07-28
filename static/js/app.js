/* LogResolve FastAPI frontend */

const state = {
  sid: null,
  loaded: false,
  categories: [],
  selected: new Set(),
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || res.statusText || "Request failed");
  }
  return data;
}

function $(id) {
  return document.getElementById(id);
}

function show(el, on = true) {
  el.classList.toggle("hidden", !on);
}

function setHtml(el, html) {
  el.innerHTML = html;
}

function currentProvider() {
  return $("provider").value;
}

function updateProviderPanels() {
  const p = currentProvider();
  document.querySelectorAll(".provider-panel").forEach((n) => n.classList.add("hidden"));
  const panel = $(`creds-${p}`);
  if (panel) panel.classList.remove("hidden");
  updateBadge();
}

const CONFIG_VISIBLE_KEY = "logresolve_config_visible";

function setConfigVisible(visible) {
  const layout = $("app-layout");
  const btn = $("btn-toggle-config");
  if (!layout || !btn) return;
  layout.classList.toggle("sidebar-collapsed", !visible);
  btn.setAttribute("aria-expanded", visible ? "true" : "false");
  const label = visible ? "Hide config" : "Show config";
  btn.setAttribute("aria-label", label);
  btn.title = label;
  try {
    localStorage.setItem(CONFIG_VISIBLE_KEY, visible ? "1" : "0");
  } catch (_) {
    /* ignore */
  }
}

function initConfigToggle() {
  const btn = $("btn-toggle-config");
  if (!btn) return;
  let visible = true;
  try {
    const stored = localStorage.getItem(CONFIG_VISIBLE_KEY);
    if (stored === "0") visible = false;
  } catch (_) {
    /* ignore */
  }
  setConfigVisible(visible);
  btn.addEventListener("click", () => {
    const layout = $("app-layout");
    setConfigVisible(layout.classList.contains("sidebar-collapsed"));
  });
}

function gatherSettings() {
  return {
    provider: currentProvider(),
    openai_api_key: $("openai_api_key").value,
    openai_model: $("openai_model").value,
    anthropic_api_key: $("anthropic_api_key").value,
    anthropic_model: $("anthropic_model").value,
    gemini_api_key: $("gemini_api_key").value,
    gemini_model: $("gemini_model").value,
    watsonx_api_key: $("watsonx_api_key").value,
    watsonx_project_id: $("watsonx_project_id").value,
    watsonx_url: $("watsonx_url").value,
    watsonx_model: $("watsonx_model").value,
    watsonx_model_suggestion: $("watsonx_model_suggestion").value,
    ollama_base_url: $("ollama_base_url").value,
    ollama_model: $("ollama_model").value,
  };
}

function gatherLlmPayload() {
  const p = currentProvider();
  const s = gatherSettings();
  if (p === "openai") {
    return { provider: p, api_key: s.openai_api_key, model: s.openai_model };
  }
  if (p === "anthropic") {
    return { provider: p, api_key: s.anthropic_api_key, model: s.anthropic_model };
  }
  if (p === "gemini") {
    return { provider: p, api_key: s.gemini_api_key, model: s.gemini_model };
  }
  if (p === "watsonx") {
    return {
      provider: p,
      api_key: s.watsonx_api_key,
      project_id: s.watsonx_project_id,
      url: s.watsonx_url,
      model: s.watsonx_model,
    };
  }
  return {
    provider: "ollama",
    base_url: s.ollama_base_url,
    model: s.ollama_model,
    api_key: "ollama",
  };
}

function updateBadge() {
  const llm = gatherLlmPayload();
  $("active-llm").textContent = `Active LLM: ${llm.provider} · ${llm.model || "(no model)"}`;
  $("analyze-limit-caption").textContent =
    `Max error chunks: ${$("max-chunks").value} (edit under Categories / Analysis limit).`;
}

function visibleCategories(needle) {
  const q = (needle || "").trim().toLowerCase();
  if (!q) return [...state.categories];
  return state.categories.filter((c) => c.toLowerCase().includes(q));
}

function renderCategories() {
  const needle = $("category-search").value;
  const visible = visibleCategories(needle);
  $("cat-meta").textContent = `Showing ${visible.length} of ${state.categories.length} categor(ies).`;
  const list = $("cat-list");
  list.innerHTML = "";
  visible.forEach((cat) => {
    const id = `cat_${CSS.escape ? cat : cat}`;
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.selected.has(cat);
    cb.addEventListener("change", () => {
      if (cb.checked) state.selected.add(cat);
      else state.selected.delete(cat);
      updateCatWarn();
      syncSelection();
    });
    label.appendChild(cb);
    label.appendChild(document.createTextNode(cat));
    list.appendChild(label);
  });
  updateCatWarn();
  fillExploreCategorySelect();
}

function updateCatWarn() {
  const needle = $("category-search").value.trim();
  const visible = new Set(visibleCategories(needle));
  const hiddenSelected = [...state.selected].filter((c) => !visible.has(c));
  const box = $("cat-warn");
  if (needle && hiddenSelected.length) {
    box.innerHTML = `<div class="warn">Search is active: <strong>${state.selected.size}</strong> selected overall
      (${hiddenSelected.length} selected but hidden). Click <strong>Keep only visible</strong> to keep only checked items in this filter.</div>`;
  } else if (!state.selected.size) {
    box.innerHTML = `<div class="warn">No categories selected — pick at least one before exploring or analyzing.</div>`;
  } else {
    box.innerHTML = `<div class="hint">${state.selected.size} of ${state.categories.length} categor(ies) selected overall.</div>`;
  }
  $("btn-analyze").disabled = !(state.loaded && state.selected.size);
}

async function syncSelection() {
  if (!state.loaded) return;
  await api("/api/selection", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      selected_categories: [...state.selected],
      max_chunks: Number($("max-chunks").value || 10),
    }),
  });
  updateBadge();
}

function fillExploreCategorySelect() {
  const sel = $("explore-category");
  const current = sel.value;
  sel.innerHTML = `<option value="(all selected)">(all selected)</option>`;
  [...state.selected].sort().forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c;
    opt.textContent = c;
    sel.appendChild(opt);
  });
  if ([...sel.options].some((o) => o.value === current)) sel.value = current;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderResults(results, meta = {}) {
  const section = $("section-results");
  show(section, results && results.length);
  if (!results || !results.length) return;
  let metaHtml = "";
  if (meta.llm_label) metaHtml += `Last run: ${escapeHtml(meta.llm_label)}. `;
  if (meta.partial) {
    metaHtml += `Partial run — showing ${results.length}` +
      (meta.total_found ? ` of ${meta.total_found}` : "") +
      " chunk(s).";
  }
  $("results-meta").textContent = metaHtml;
  const list = $("results-list");
  list.innerHTML = "";
  results.forEach((res, idx) => {
    const details = document.createElement("details");
    details.className = "result-card";
    details.open = idx === 0;
    const summary = document.createElement("summary");
    summary.textContent = res.label || `Chunk ${idx + 1}`;
    details.appendChild(summary);
    const body = document.createElement("div");
    body.className = "body";
    if (res.llm_label) {
      body.innerHTML += `<div class="hint">Provider: ${escapeHtml(res.llm_label)}</div>`;
    }
    if (res.error) {
      body.innerHTML += `<div class="error">Chunk analysis failed: ${escapeHtml(res.error)}</div>`;
      if (res.chunk_text) {
        body.innerHTML += `<pre class="log">${escapeHtml(res.chunk_text)}</pre>`;
      }
    } else {
      const evidence = res.extracted_exceptions || res.chunk_text || "";
      body.innerHTML += `<strong>Exception evidence (masked)</strong><pre class="log">${escapeHtml(evidence)}</pre>`;
      body.innerHTML += `<div class="grid-2">
        <div class="lr-panel lr-panel-diagnosis"><h4>Root Cause Diagnosis</h4>${escapeHtml(res.root_cause_diagnosis || "No diagnosis").replaceAll("\n", "<br>")}</div>
        <div class="lr-panel lr-panel-playbook"><h4>Resolution Playbook</h4>${escapeHtml(res.resolution_steps || "No playbook").replaceAll("\n", "<br>")}</div>
      </div>`;
      const passed = res.validation_passed;
      body.innerHTML += `<div class="hint">Validation: ${passed ? "PASSED" : "FAILED"} (retry_count=${res.retry_count || 0}).</div>`;
    }
    details.appendChild(body);
    list.appendChild(details);
  });
}

async function init() {
  const sess = await api("/api/session");
  state.sid = sess.sid;

  initConfigToggle();

  $("provider").addEventListener("change", updateProviderPanels);
  ["openai_model", "anthropic_model", "gemini_model", "watsonx_model", "ollama_model"].forEach((id) => {
    $(id).addEventListener("input", updateBadge);
  });
  $("max-chunks").addEventListener("change", () => {
    syncSelection();
    updateBadge();
  });

  $("watsonx_model_suggestion").addEventListener("change", () => {
    const v = $("watsonx_model_suggestion").value;
    if (v && v !== "Custom (type below)") $("watsonx_model").value = v;
    updateBadge();
  });

  $("do_mask").addEventListener("change", () => {
    show($("mask-warn"), !$("do_mask").checked);
  });

  $("btn-save-settings").addEventListener("click", async () => {
    try {
      await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(gatherSettings()),
      });
      $("settings-status").textContent = "Saved to llm_settings.json";
    } catch (e) {
      $("settings-status").textContent = e.message;
    }
  });

  $("btn-clear-settings").addEventListener("click", async () => {
    await api("/api/settings", { method: "DELETE" });
    $("settings-status").textContent = "Cleared local settings file.";
  });

  $("btn-load").addEventListener("click", async () => {
    const files = $("file-input").files;
    if (!files || !files.length) {
      setHtml($("load-status"), `<div class="error">Please upload at least one file.</div>`);
      return;
    }
    const fd = new FormData();
    [...files].forEach((f) => fd.append("files", f));
    fd.append("do_mask", $("do_mask").checked ? "true" : "false");
    setHtml($("load-status"), `<div class="info">Extracting and indexing…</div>`);
    try {
      const res = await fetch("/api/load", { method: "POST", body: fd, credentials: "same-origin" });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || "Load failed");
      state.loaded = true;
      state.categories = data.categories || [];
      state.selected = new Set(state.categories);
      show($("section-categories"), true);
      show($("section-explore"), true);
      renderCategories();
      await syncSelection();
      setHtml(
        $("load-status"),
        `<div class="success">Loaded ${data.count} categor(ies): ${escapeHtml(state.categories.join(", "))}</div>`
      );
      $("btn-analyze").disabled = false;
    } catch (e) {
      setHtml($("load-status"), `<div class="error">${escapeHtml(e.message)}</div>`);
    }
  });

  document.querySelectorAll("[data-cat-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const action = btn.getAttribute("data-cat-action");
      const visible = visibleCategories($("category-search").value);
      if (action === "all") state.selected = new Set(state.categories);
      if (action === "none") state.selected = new Set();
      if (action === "visible") visible.forEach((c) => state.selected.add(c));
      if (action === "deselect-visible") visible.forEach((c) => state.selected.delete(c));
      if (action === "keep-visible") {
        const keep = new Set(visible.filter((c) => state.selected.has(c)));
        // Also include currently checked visible ones from DOM
        state.selected = new Set(visible.filter((c) => {
          // re-read from current selected ∩ visible — user checks first then keep
          return state.selected.has(c);
        }));
        // Actually keep-visible means: deselect hidden; leave visible checks as-is
        state.selected = new Set([...state.selected].filter((c) => visible.includes(c)));
      }
      renderCategories();
      syncSelection();
    });
  });

  $("category-search").addEventListener("input", renderCategories);

  $("btn-explore").addEventListener("click", async () => {
    try {
      const data = await api("/api/explore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          selected_categories: [...state.selected],
          category_filter: $("explore-category").value,
          keyword: $("explore-keyword").value,
          start: $("explore-start").value ? new Date($("explore-start").value).toISOString() : null,
          end: $("explore-end").value ? new Date($("explore-end").value).toISOString() : null,
          reveal: $("explore-reveal").checked,
        }),
      });
      $("explore-meta").textContent = `Showing ${data.count} matching lines.`;
      const tbody = $("explore-table").querySelector("tbody");
      tbody.innerHTML = "";
      (data.rows || []).forEach((r) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${escapeHtml(r.category)}</td><td>${r.line_number}</td><td>${escapeHtml(r.timestamp || "")}</td><td>${escapeHtml(r.line || "")}</td>`;
        tbody.appendChild(tr);
      });
      if (!(data.rows || []).length) {
        $("explore-meta").textContent = "No log lines match the current filters (or no parseable timestamps in range).";
      }
    } catch (e) {
      $("explore-meta").textContent = e.message;
    }
  });

  $("btn-analyze").addEventListener("click", () => runAnalyze());

  // Seed watsonx suggestion select
  const savedModel = (window.LOGRESOLVE_BOOT.saved || {}).watsonx_model;
  const sug = $("watsonx_model_suggestion");
  if (savedModel && [...sug.options].some((o) => o.value === savedModel)) {
    sug.value = savedModel;
  }

  updateProviderPanels();
  updateBadge();
}

function runAnalyze() {
  if (!state.sid || !state.selected.size) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/analyze`);
  show($("progress-wrap"), true);
  $("progress-fill").style.width = "0%";
  $("progress-text").textContent = "Starting…";
  setHtml($("analyze-status"), "");
  $("btn-analyze").disabled = true;

  const collected = [];

  ws.onopen = () => {
    ws.send(
      JSON.stringify({
        sid: state.sid,
        selected_categories: [...state.selected],
        max_chunks: Number($("max-chunks").value || 10),
        do_mask: $("do_mask").checked,
        llm: gatherLlmPayload(),
      })
    );
  };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "error") {
      setHtml($("analyze-status"), `<div class="error">${escapeHtml(msg.error)}</div>`);
      $("btn-analyze").disabled = false;
      return;
    }
    if (msg.type === "start") {
      setHtml(
        $("analyze-status"),
        `<div class="info">Found ${msg.total_found} error chunk(s); analyzing ${msg.total} across: ${escapeHtml((msg.categories || []).join(", "))}.${msg.skipped ? ` (${msg.skipped} remaining.)` : ""}</div>`
      );
    }
    if (msg.type === "progress") {
      const pct = Math.round((msg.index / msg.total) * 100);
      $("progress-fill").style.width = `${pct}%`;
      $("progress-text").textContent = `Analysed ${msg.index}/${msg.total}: ${msg.label || ""}`;
      if (msg.result) collected.push(msg.result);
      renderResults(collected, { llm_label: msg.result?.llm_label });
    }
    if (msg.type === "done") {
      $("progress-fill").style.width = "100%";
      $("progress-text").textContent = "Analysis complete";
      setHtml(
        $("analyze-status"),
        `<div class="success">Analysis complete — ${msg.results.length} chunk(s) via ${escapeHtml(msg.llm_label || "")}.</div>`
      );
      renderResults(msg.results || collected, {
        llm_label: msg.llm_label,
        partial: msg.partial,
        total_found: msg.total_found,
      });
      $("btn-analyze").disabled = false;
    }
  };

  ws.onerror = () => {
    setHtml($("analyze-status"), `<div class="error">WebSocket error during analysis.</div>`);
    $("btn-analyze").disabled = false;
  };

  ws.onclose = () => {
    $("btn-analyze").disabled = !(state.loaded && state.selected.size);
  };
}

document.addEventListener("DOMContentLoaded", () => {
  init().catch((e) => console.error(e));
});
