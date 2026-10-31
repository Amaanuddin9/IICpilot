import * as vscode from 'vscode';

function nonce(): string {
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    let text = '';
    for (let i = 0; i < 32; i++) {
        text += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return text;
}

/**
 * The whole panel: one HTML document with inline CSS/JS under a strict CSP
 * (nonce-gated scripts, no remote loads). Colors come from VS Code theme
 * variables so it tracks light/dark without a second stylesheet.
 */
export function chatHtml(webview: vscode.Webview): string {
    const n = nonce();
    return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; script-src 'nonce-${n}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  :root {
    --agent-control: var(--vscode-foreground);
    --agent-dse: var(--vscode-charts-yellow, #d7ba7d);
    --agent-eda: var(--vscode-charts-blue, #4fc1ff);
    --agent-memory: var(--vscode-charts-green, #89d185);
    --agent-tool: var(--vscode-charts-purple, #c586c0);
    --agent-floorplan: var(--vscode-charts-orange, #d18616);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0;
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
    background: var(--vscode-sideBar-background, var(--vscode-editor-background));
    display: flex; flex-direction: column; height: 100vh;
  }
  #log { flex: 1 1 auto; overflow-y: auto; padding: 10px 10px 4px; }
  .row { margin: 6px 0; line-height: 1.45; }

  .user {
    background: var(--vscode-input-background);
    border: 1px solid var(--vscode-input-border, transparent);
    border-radius: 6px; padding: 6px 9px; white-space: pre-wrap;
  }
  .assistant { padding: 4px 2px; white-space: pre-wrap; }

  .logline {
    font-family: var(--vscode-editor-font-family, monospace);
    font-size: 0.86em; display: flex; gap: 6px; margin: 1px 0;
    opacity: 0.95;
  }
  .logline .label { flex: 0 0 auto; font-weight: 600; }
  .logline .body { flex: 1 1 auto; white-space: pre-wrap; word-break: break-word; }
  .Control .label { color: var(--agent-control); }
  .DSE     .label { color: var(--agent-dse); }
  .EDA     .label { color: var(--agent-eda); }
  .Memory  .label { color: var(--agent-memory); }
  .Tool    .label { color: var(--agent-tool); }
  .Floorplan .label { color: var(--agent-floorplan); }
  .Floorplan .body  { color: var(--agent-floorplan); opacity: 0.9; }
  .Verdict .label { color: var(--vscode-charts-red, #f14c4c); }

  /* Floorplan pictures arrive as SVG text and are shown through a data: URL
     <img>, which never runs script even if the SVG contained any. */
  .figure img { display: block; max-width: 100%; height: auto; border-radius: 4px; margin: 4px 0; }

  .thinking { opacity: 0.7; font-style: italic; padding: 2px 2px; }

  .card {
    border: 1px solid var(--vscode-panel-border, rgba(128,128,128,0.35));
    border-radius: 6px; padding: 8px 10px; margin: 8px 0;
    background: var(--vscode-editorWidget-background, transparent);
  }
  .card h4 { margin: 0 0 6px; font-size: 0.95em; display: flex; align-items: center; gap: 6px; }
  .metrics { display: grid; grid-template-columns: auto auto; gap: 2px 14px; font-size: 0.88em; }
  .metrics dt { opacity: 0.75; }
  .metrics dd { margin: 0; font-family: var(--vscode-editor-font-family, monospace); }

  .badge {
    font-size: 0.75em; font-weight: 700; padding: 1px 6px; border-radius: 9px;
    border: 1px solid currentColor; text-transform: uppercase; letter-spacing: 0.04em;
  }
  .warm { color: var(--agent-memory); }
  .cold { color: var(--agent-eda); }
  .clean { color: var(--agent-memory); }
  .real { color: var(--agent-eda); }
  .estimated { color: var(--vscode-editorWarning-foreground, #cca700); }
  .verdict { font-size: 0.88em; opacity: 0.9; margin: 2px 0 4px; }
  .seeded { color: var(--agent-tool); }
  .bad { color: var(--vscode-errorForeground); }

  .diff {
    font-family: var(--vscode-editor-font-family, monospace); font-size: 0.88em;
    margin: 4px 0 8px;
  }
  .buttons { display: flex; gap: 6px; flex-wrap: wrap; }
  button {
    font-family: inherit; font-size: 0.9em; padding: 3px 10px; border-radius: 3px;
    border: 1px solid var(--vscode-button-border, transparent);
    background: var(--vscode-button-background); color: var(--vscode-button-foreground);
    cursor: pointer;
  }
  button:hover { background: var(--vscode-button-hoverBackground); }
  button.secondary {
    background: var(--vscode-button-secondaryBackground);
    color: var(--vscode-button-secondaryForeground);
  }
  button.secondary:hover { background: var(--vscode-button-secondaryHoverBackground); }
  button:disabled { opacity: 0.5; cursor: default; }

  .edit-grid { display: grid; grid-template-columns: auto 90px; gap: 4px 8px; margin: 6px 0; align-items: center; }
  input[type=number] {
    width: 100%; font-family: inherit; padding: 2px 4px;
    background: var(--vscode-input-background); color: var(--vscode-input-foreground);
    border: 1px solid var(--vscode-input-border, rgba(128,128,128,0.4)); border-radius: 3px;
  }

  .error {
    border-left: 3px solid var(--vscode-errorForeground);
    background: var(--vscode-inputValidation-errorBackground, rgba(255,0,0,0.08));
    padding: 8px 10px; margin: 8px 0; white-space: pre-wrap;
    font-family: var(--vscode-editor-font-family, monospace); font-size: 0.86em;
  }

  #controls {
    flex: 0 0 auto; display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    padding: 6px 10px; border-top: 1px solid var(--vscode-panel-border, rgba(128,128,128,0.3));
    font-size: 0.85em;
  }
  #controls label { display: flex; align-items: center; gap: 4px; cursor: pointer; }
  #composer { flex: 0 0 auto; display: flex; gap: 6px; padding: 0 10px 10px; }
  textarea {
    flex: 1 1 auto; resize: none; min-height: 32px; max-height: 140px;
    font-family: inherit; font-size: inherit; padding: 6px 8px; border-radius: 4px;
    background: var(--vscode-input-background); color: var(--vscode-input-foreground);
    border: 1px solid var(--vscode-input-border, rgba(128,128,128,0.4));
  }
  .hint { opacity: 0.6; padding: 0 10px 8px; font-size: 0.8em; }

  #runstatus {
    flex: 0 0 auto; padding: 4px 10px; font-size: 0.85em;
    font-family: var(--vscode-editor-font-family, monospace);
    color: var(--agent-eda);
    border-top: 1px solid var(--vscode-panel-border, rgba(128,128,128,0.3));
  }
  #runstatus[hidden] { display: none; }
</style>
</head>
<body>
  <div id="log">
    <div class="row hint">Ask for a run, e.g. <b>run ibex at 80% utilization</b>, <b>optimize ibex</b> or <b>compare floorplans for tinyRocket</b>.</div>
  </div>

  <div id="controls">
    <label><input type="checkbox" id="hitl"> Approve each proposal (HITL)</label>
    <button class="secondary" id="reset">Reset memory</button>
    <button class="secondary" id="restart">Restart backend</button>
    <button class="secondary" id="logs">Logs</button>
  </div>

  <div id="runstatus" hidden></div>

  <div id="composer">
    <textarea id="input" rows="1" placeholder="Message IICPilot…  (Enter to send, Shift+Enter for newline)"></textarea>
    <button id="send">Send</button>
  </div>

<script nonce="${n}">
(function () {
  const vscode = acquireVsCodeApi();
  const log = document.getElementById('log');
  const input = document.getElementById('input');
  const hitl = document.getElementById('hitl');
  const sendButton = document.getElementById('send');
  const runStatus = document.getElementById('runstatus');
  let thinkingEl = null;

  // Live ORFS run state. The backend reports elapsed time only at stage
  // transitions, so the clock ticks locally from the last report.
  let busy = false;
  let runTimer = null;
  let runStage = '';
  let runElapsedS = 0;
  let runReceivedAt = 0;

  function mmss(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  }
  function renderRun() {
    const elapsed = runElapsedS + (Date.now() - runReceivedAt) / 1000;
    runStatus.textContent = 'running (' + runStage + ', ' + mmss(elapsed) + ')';
  }
  function stopRun() {
    if (runTimer) { clearInterval(runTimer); runTimer = null; }
    runStatus.hidden = true;
  }
  function setBusy(value) {
    busy = value;
    sendButton.disabled = value;
  }

  function atBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  }
  function scroll(wasAtBottom) {
    if (wasAtBottom) { log.scrollTop = log.scrollHeight; }
  }
  function add(el) {
    const wasAtBottom = atBottom();
    log.appendChild(el);
    scroll(wasAtBottom);
    return el;
  }
  function div(cls, text) {
    const el = document.createElement('div');
    el.className = cls;
    if (text !== undefined) { el.textContent = text; }
    return el;
  }
  function num(value) {
    return (value === null || value === undefined) ? '—' : String(value);
  }

  function clearThinking() {
    if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
  }

  function addLogLine(agent, text) {
    const row = div('row logline ' + agent);
    row.appendChild(div('label', '[' + agent + ']'));
    row.appendChild(div('body', text));
    add(row);
  }

  function metricsCard(frame) {
    const best = frame.best || {};
    const card = div('card');
    const head = document.createElement('h4');
    head.textContent = 'Run result';

    // Verdict and provenance come straight from the tool result, never from
    // the model's prose — an 8B model has been seen to invert them.
    const verdict = String(best.verdict || '');
    if (verdict.startsWith('CLEAN')) {
      head.appendChild(div('badge clean', 'clean'));
    } else if (verdict.startsWith('CONGESTED')) {
      head.appendChild(div('badge bad', 'congested'));
    } else if (verdict.startsWith('FAILED')) {
      head.appendChild(div('badge bad', 'failed'));
    } else if (best.congested) {
      head.appendChild(div('badge bad', 'congested'));
    }

    const source = String(frame.source || '');
    if (source === 'ESTIMATED_MOCK' || source === 'ESTIMATED_FROM_REAL') {
      head.appendChild(div('badge estimated', 'estimated'));
    } else if (source === 'CACHED_ORFS' || source === 'REAL_ORFS') {
      head.appendChild(div('badge real', 'real'));
    }

    if (typeof frame.seeded === 'boolean') {
      const badge = div('badge ' + (frame.seeded ? 'warm' : 'cold'), frame.seeded ? 'warm' : 'cold');
      head.appendChild(badge);
    }
    card.appendChild(head);

    if (verdict.length > 0) {
      card.appendChild(div('row verdict', verdict));
    }
    if (source.length > 0) {
      card.appendChild(div('row hint', 'source: ' + source +
        (frame.source_detail ? ' — ' + frame.source_detail : '')));
    }

    const dl = document.createElement('dl');
    dl.className = 'metrics';
    // "n/a" rather than 0: a metric the source does not provide (congestion
    // outside the measured range, say) must not read as a measured zero.
    function metric(value, unit) {
      if (value === null || value === undefined || value === '') { return 'n/a'; }
      return unit ? value + unit : String(value);
    }
    const rows = [
      ['flow runs', metric(frame.flow_runs, '')],
      ['die area', metric(best.die_area_um2, ' um2')],
      ['congestion', metric(best.congestion, '')],
      ['power', metric(best.power_mw, ' mW')],
      ['WNS', metric(best.wns_ns, ' ns')],
      ['reg-to-reg fmax', metric(best.fmax_mhz, ' MHz')]
    ];
    const pipe = frame.pipeline;
    if (pipe) {
      rows.push(['screens', pipe.screens_total + ' (' + pipe.screens_replayed + ' replayed, ' + pipe.screens_run + ' run)']);
      rows.push(['pruned / survivors', (pipe.pruned || []).length + ' / ' + (pipe.survivors || []).length]);
      rows.push(['full flows', pipe.full_runs + ' (' + pipe.full_runs_replayed + ' replayed, ' + pipe.full_runs_run + ' run)']);
      if (pipe.time_saved_s !== null && pipe.time_saved_s !== undefined) {
        rows.push(['time saved (est.)', Math.round(pipe.time_saved_s / 60) + ' min']);
      }
    }
    if (frame.best_utilization_percent !== undefined) {
      rows.splice(1, 0, ['best util', frame.best_utilization_percent + '%']);
    }
    if (best.congestion === null || best.congestion === undefined) {
      const note = best.congestion_note || best.range_note;
      if (note) { card.appendChild(div('row hint', 'congestion/WNS: ' + note)); }
    }
    for (const [key, value] of rows) {
      const dt = document.createElement('dt'); dt.textContent = key;
      const dd = document.createElement('dd'); dd.textContent = value;
      dl.appendChild(dt); dl.appendChild(dd);
    }
    card.appendChild(dl);
    add(card);
  }

  function svgFigure(svg, alt) {
    const figure = div('figure');
    const img = document.createElement('img');
    img.alt = alt || 'floorplan';
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(String(svg));
    figure.appendChild(img);
    return figure;
  }

  // OpenROAD's own renders (placement, congestion heat map) arrive as base64
  // PNG; the data: URL is allowed by the CSP's img-src.
  function pngFigure(b64, alt) {
    const figure = div('figure');
    const img = document.createElement('img');
    img.alt = alt || 'OpenROAD render';
    img.src = 'data:image/png;base64,' + String(b64);
    figure.appendChild(img);
    return figure;
  }

  function imageCard(frame) {
    const card = div('card');
    const head = document.createElement('h4');
    head.textContent = frame.title || 'Floorplan';
    card.appendChild(head);
    // The verdict comes from the tool result, never from the model's prose.
    if (frame.verdict) { card.appendChild(div('row verdict', frame.verdict)); }
    if (frame.png) {
      card.appendChild(pngFigure(frame.png, frame.title));
    } else if (frame.svg) {
      card.appendChild(svgFigure(frame.svg, frame.title));
    }
    if (frame.caption) { card.appendChild(div('row hint', frame.caption)); }
    if (frame.path) { card.appendChild(div('row hint', 'saved: ' + frame.path)); }
    add(card);
  }

  function proposalCard(frame) {
    const card = div('card');
    const head = document.createElement('h4');
    head.textContent = 'Proposal · ' + frame.triggered_by;
    if (frame.seeded_from_memory) {
      head.appendChild(div('badge seeded', 'memory-seeded'));
    }
    card.appendChild(head);
    card.appendChild(div('row', frame.rationale));
    // A floorplan proposal shows the layout being approved.
    if (frame.image_svg) { card.appendChild(svgFigure(frame.image_svg, 'proposed floorplan')); }

    const changes = frame.changes || {};
    const diff = div('diff');
    for (const key of Object.keys(changes)) {
      diff.appendChild(div('', '+ ' + key + ' = ' + changes[key]));
    }
    card.appendChild(diff);

    const buttons = div('buttons');
    const accept = document.createElement('button');
    accept.textContent = 'Accept';
    const edit = document.createElement('button');
    edit.textContent = 'Edit'; edit.className = 'secondary';
    const reject = document.createElement('button');
    reject.textContent = 'Reject'; reject.className = 'secondary';
    buttons.appendChild(accept); buttons.appendChild(edit); buttons.appendChild(reject);
    card.appendChild(buttons);

    function resolve(decision, edited) {
      vscode.postMessage({ type: 'decision', id: frame.id, decision: decision, changes: edited });
      for (const b of card.querySelectorAll('button')) { b.disabled = true; }
      const note = decision === 'edit'
        ? 'edited → ' + JSON.stringify(edited)
        : decision;
      card.appendChild(div('row hint', 'you: ' + note));
    }

    accept.addEventListener('click', () => resolve('accept'));
    reject.addEventListener('click', () => resolve('reject'));
    edit.addEventListener('click', () => {
      edit.disabled = true;
      const grid = div('edit-grid');
      const inputs = {};
      for (const key of Object.keys(changes)) {
        grid.appendChild(div('', key));
        const field = document.createElement('input');
        field.type = 'number'; field.step = 'any'; field.value = changes[key];
        inputs[key] = field;
        grid.appendChild(field);
      }
      card.insertBefore(grid, buttons);
      const apply = document.createElement('button');
      apply.textContent = 'Apply edit';
      buttons.appendChild(apply);
      apply.addEventListener('click', () => {
        const edited = {};
        for (const key of Object.keys(inputs)) {
          const parsed = parseFloat(inputs[key].value);
          edited[key] = isNaN(parsed) ? changes[key] : parsed;
        }
        resolve('edit', edited);
      });
    });

    add(card);
  }

  function errorBlock(text, offerRestart) {
    const box = div('error', text);
    if (offerRestart) {
      const buttons = div('buttons');
      const restart = document.createElement('button');
      restart.textContent = 'Restart backend';
      restart.addEventListener('click', () => vscode.postMessage({ type: 'restart' }));
      buttons.appendChild(restart);
      box.appendChild(buttons);
    }
    add(box);
  }

  window.addEventListener('message', (event) => {
    const message = event.data;
    if (message.type === 'user') {
      add(div('row user', message.text));
      return;
    }
    if (message.type === 'hitl') { hitl.checked = !!message.value; return; }
    if (message.type === 'status') { addLogLine('Control', message.text); return; }
    if (message.type === 'backend_down') {
      clearThinking(); stopRun(); setBusy(false); errorBlock(message.text, true); return;
    }
    if (message.type === 'clear_error') { return; }

    if (message.type !== 'frame') { return; }
    const frame = message.frame;

    switch (frame.type) {
      case 'ready':
        addLogLine('Control', 'ready · model=' + frame.model + ' · backend=' + frame.backend + ' · memory=' + frame.memory);
        break;
      case 'thinking':
        clearThinking();
        thinkingEl = add(div('row thinking', 'thinking…'));
        break;
      case 'log':
        addLogLine(frame.agent || 'Control', frame.text || '');
        break;
      case 'assistant':
        clearThinking();
        add(div('row assistant', frame.text || ''));
        break;
      case 'report':
        clearThinking();
        metricsCard(frame);
        break;
      case 'proposal':
        clearThinking();
        proposalCard(frame);
        break;
      case 'image':
        imageCard(frame);
        break;
      case 'error':
        clearThinking();
        errorBlock(frame.text || 'Unknown error', false);
        break;
      case 'run_status':
        if (frame.state === 'running') {
          runStage = frame.stage || 'starting';
          runElapsedS = Number(frame.elapsed_s) || 0;
          runReceivedAt = Date.now();
          runStatus.hidden = false;
          renderRun();
          if (!runTimer) { runTimer = setInterval(renderRun, 1000); }
          // Send stays disabled until turn_end, not just until the run ends.
          setBusy(true);
        } else {
          stopRun();
        }
        break;
      case 'turn_end':
        clearThinking();
        stopRun();
        setBusy(false);
        break;
    }
  });

  function send() {
    if (busy) { return; }
    const text = input.value.trim();
    if (text.length === 0) { return; }
    vscode.postMessage({ type: 'user', text: text });
    input.value = '';
    input.style.height = 'auto';
  }

  sendButton.addEventListener('click', send);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  });

  hitl.addEventListener('change', () => {
    vscode.postMessage({ type: 'hitl', value: hitl.checked });
  });
  document.getElementById('reset').addEventListener('click', () => {
    vscode.postMessage({ type: 'reset_memory' });
  });
  document.getElementById('restart').addEventListener('click', () => {
    vscode.postMessage({ type: 'restart' });
  });
  document.getElementById('logs').addEventListener('click', () => {
    vscode.postMessage({ type: 'show_logs' });
  });
}());
</script>
</body>
</html>`;
}
