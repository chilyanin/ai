#!/usr/bin/env python3
"""Local Asana-task monitor.

Polls "MY TASKS" (open tasks assigned to the authed user) every 15 minutes,
classifies each one into one of four buckets — offboarding, access requests,
procurement/renewals, other — and persists the snapshot to SQLite. A Flask
dashboard at http://localhost:5111 visualises the result with Chart.js,
shows a per-bucket breakdown of the current task list, and exposes a run log.

Usage:
    ./monitor.py                       # default port 5111, 15-min interval
    ./monitor.py --port 5050 --interval 600
    ./monitor.py --once                # run one snapshot and exit
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request

# Reuse the existing Asana client + the monitor package we just built.
from asana_client import AsanaError, fetch_my_open_tasks, fetch_task
from monitor.classifier import BUCKET_LABELS, BUCKETS, classify_many
from monitor.db import (
    get_triage,
    init_db,
    insert_actions,
    insert_run,
    latest_run,
    latest_tasks,
    recent_actions,
    recent_runs,
    upsert_triage,
)
from monitor.triage import TriageError, triage_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
)
log = logging.getLogger("monitor")


def analyze_once() -> dict:
    """Fetch + classify + persist a single snapshot. Returns the row dict."""
    started = time.monotonic()
    error: str | None = None
    counts = {b: 0 for b in BUCKETS}
    enriched: list[dict] = []
    try:
        tasks = fetch_my_open_tasks(limit=100)
        counts, enriched = classify_many(tasks)
    except AsanaError as e:
        error = f"asana: {e}"
        log.exception("asana fetch failed")
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        log.exception("classification failed")

    duration_ms = int((time.monotonic() - started) * 1000)
    run_id = insert_run(
        counts=counts,
        enriched_tasks=enriched,
        duration_ms=duration_ms,
        error=error,
    )
    summary = (
        f"run#{run_id}  count={sum(counts.values())}  "
        f"offb={counts.get('offboarding', 0)}  "
        f"acc={counts.get('access', 0)}  "
        f"proc={counts.get('procurement', 0)}  "
        f"other={counts.get('other', 0)}  "
        f"{duration_ms}ms"
    )
    if error:
        log.warning("%s  ERROR: %s", summary, error)
    else:
        log.info(summary)
    return {"run_id": run_id, "counts": counts, "duration_ms": duration_ms, "error": error}


# ---------------------------------------------------------------------------
# Scheduler — plain threading; no APScheduler dependency.
# ---------------------------------------------------------------------------

class Scheduler:
    """A trivial recurring scheduler: every `interval_s` seconds, call
    `analyze_once()`. Survives transient exceptions by logging and waiting
    out the next tick. Stops cleanly via `stop()`."""

    def __init__(self, interval_s: int):
        self.interval_s = int(interval_s)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="analyzer")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # Fire once on startup so the dashboard isn't empty.
        try:
            analyze_once()
        except Exception:  # noqa: BLE001
            log.exception("initial run failed")
        while not self._stop.wait(self.interval_s):
            try:
                analyze_once()
            except Exception:  # noqa: BLE001
                log.exception("scheduled run failed")


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
_scheduler: Scheduler | None = None

DASHBOARD_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Asana Task Monitor</title>
  <style>
    :root {
      --bg: #0f1115;
      --panel: #161a22;
      --panel-2: #1d222c;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --border: #262c38;
      --offb: #ef4444;
      --acc: #3b82f6;
      --proc: #f59e0b;
      --other: #6b7280;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: var(--bg); color: var(--text); }
    header { padding: 18px 28px; border-bottom: 1px solid var(--border);
             display: flex; align-items: baseline; justify-content: space-between; }
    header h1 { font-size: 18px; margin: 0; font-weight: 600; }
    header .meta { color: var(--muted); font-size: 13px; }
    header .actions { display: flex; gap: 8px; align-items: center; }
    header button { background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
                    padding: 6px 12px; border-radius: 6px; cursor: pointer; }
    header button:hover { background: #2a3140; }
    header button:disabled { opacity: 0.65; cursor: default; }
    header button.danger { border-color: rgba(239,68,68,0.45); color: #fca5a5; }
    header button.danger:hover { background: rgba(239,68,68,0.16); }
    header .date-input { background: var(--panel-2); color: var(--text);
                         border: 1px solid var(--border); padding: 5px 8px; border-radius: 6px; }
    header .chk { color: var(--muted); font-size: 12px; display: flex; gap: 4px;
                  align-items: center; cursor: pointer; user-select: none; }
    header .date-row { display: flex; gap: 10px; align-items: center; margin-top: 8px; }
    header .date-row label[for="monitor-date"] { color: var(--muted); font-size: 13px; }
    .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.55);
                     display: flex; align-items: center; justify-content: center; z-index: 50; }
    .modal-overlay[hidden] { display: none; }
    .modal { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
             width: min(880px, 92vw); max-height: 82vh; display: flex; flex-direction: column; }
    .modal-head { display: flex; justify-content: space-between; align-items: center;
                  padding: 12px 16px; border-bottom: 1px solid var(--border); font-weight: 600; }
    .modal-head button { background: none; border: none; color: var(--muted);
                         font-size: 18px; cursor: pointer; line-height: 1; }
    .offb-output { margin: 0; padding: 14px 16px; overflow: auto; flex: 1;
                   font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                   font-size: 12px; color: #c4cad8; white-space: pre-wrap; }
    .modal-foot { padding: 10px 16px; border-top: 1px solid var(--border);
                  color: var(--muted); font-size: 12px; display: flex;
                  justify-content: space-between; align-items: center; }
    .modal-foot .ok { color: #86efac; }
    .modal-foot .err { color: #fca5a5; }
    .grid { display: grid; grid-template-columns: 1.5fr 1fr; gap: 18px;
            padding: 18px 28px; }
    .panel { background: var(--panel); border: 1px solid var(--border);
             border-radius: 10px; padding: 16px; }
    .panel h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em;
                color: var(--muted); margin: 0 0 12px 0; font-weight: 600; }
    .totals { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
    .totals .pill { padding: 6px 10px; border-radius: 8px; background: var(--panel-2);
                    border: 1px solid var(--border); display: flex; gap: 8px; align-items: center; }
    .totals .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .dot.offb { background: var(--offb); }  .dot.acc { background: var(--acc); }
    .dot.proc { background: var(--proc); }  .dot.other { background: var(--other); }
    .totals .count { font-weight: 700; font-size: 16px; }
    .chart-wrap { position: relative; height: 260px; width: 100%; }
    .chart-wrap.small { height: 220px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
    th { color: var(--muted); font-weight: 500; }
    td a { color: var(--acc); text-decoration: none; }
    td a:hover { text-decoration: underline; }
    .bucket-cell { width: 1%; white-space: nowrap; }
    .bucket-tag { padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
    .bucket-tag.offb { background: rgba(239,68,68,0.18); color: #fca5a5; }
    .bucket-tag.acc  { background: rgba(59,130,246,0.18); color: #93c5fd; }
    .bucket-tag.proc { background: rgba(245,158,11,0.18); color: #fbbf24; }
    .bucket-tag.other{ background: rgba(107,114,128,0.25); color: #d1d5db; }
    .grid-2 { display: grid; grid-template-columns: 1.5fr 1fr; gap: 18px;
              padding: 0 28px 28px; }
    .log { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
           color: #c4cad8; max-height: 320px; overflow-y: auto; }
    .log .err { color: #fca5a5; }
    .log .row { display: flex; gap: 8px; align-items: baseline; padding: 2px 0; flex-wrap: wrap; }
    .log .ts { color: #6b7280; }
    .log .kind { padding: 0 6px; border-radius: 999px; font-size: 10px; font-weight: 700; text-transform: uppercase; }
    .log .kind.deactivate { background: rgba(239,68,68,0.18); color: #fca5a5; }
    .log .kind.grant { background: rgba(34,197,94,0.18); color: #86efac; }
    .log .svc { color: #9ca3af; }
    .log .tgt { color: #e5e7eb; }
    .log .st-ok { color: #86efac; }
    .log .st-bad { color: #fca5a5; }
    .log .st-warn { color: #fbbf24; }
    .log .st-neutral { color: #9ca3af; }
    .log .shot-link { text-decoration: none; }
    .suggest-btn { background: var(--panel-2); color: #93c5fd; border: 1px solid var(--border);
                   border-radius: 6px; font-size: 11px; padding: 2px 8px; cursor: pointer; margin-left: 6px; }
    .suggest-btn:hover { background: #2a3140; }
    .suggest-btn:disabled { opacity: 0.6; cursor: default; }
    .triage-row .triage-cell { background: var(--panel-2); padding: 10px 14px; }
    .triage { display: flex; flex-direction: column; gap: 6px; }
    .triage-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .triage-head .t-svc { color: #9ca3af; }
    .triage-head .t-cached { color: #6b7280; font-size: 11px; }
    .triage-head .t-rerun { margin-left: auto; background: none; border: 1px solid var(--border);
                            color: var(--muted); border-radius: 6px; cursor: pointer; padding: 0 8px; }
    .conf { padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; text-transform: uppercase; }
    .conf-high { background: rgba(34,197,94,0.18); color: #86efac; }
    .conf-med  { background: rgba(245,158,11,0.18); color: #fbbf24; }
    .conf-low  { background: rgba(107,114,128,0.25); color: #d1d5db; }
    .t-action { color: #e5e7eb; }
    .t-rat { color: #9ca3af; font-size: 12px; font-style: italic; }
    .empty { color: var(--muted); padding: 12px; text-align: center; }
    @media (max-width: 900px) {
      .grid, .grid-2 { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Asana Task Monitor</h1>
      <div class="meta" id="meta">loading…</div>
      <div class="date-row">
        <label for="monitor-date">Date:</label>
        <input id="monitor-date" type="date" class="date-input" title="Analyze tasks due on this date (also the offboard/onboard target date)">
        <label class="chk" title="Include tasks already marked complete">
          <input type="checkbox" id="include-completed"> incl. completed
        </label>
      </div>
    </div>
    <div class="actions">
      <label class="chk" title="Preview only — no destructive action (offboarding: find-only; onboarding: dry-run)">
        <input type="checkbox" id="action-preview" checked> preview
      </label>
      <button id="onboarding" title="Run grant.py (access/license grants) for the selected date">Onboarding</button>
      <button id="offboarding" class="danger" title="Run deactivate.py for the selected date">Offboarding</button>
      <button id="run-now" title="Take a fresh all-open snapshot now">Run now</button>
      <button id="stop-service" class="danger">Stop Service</button>
    </div>
  </header>

  <div id="action-modal" class="modal-overlay" hidden>
    <div class="modal">
      <div class="modal-head">
        <span id="action-title">Action</span>
        <button id="action-close" title="Close">&times;</button>
      </div>
      <pre id="action-output" class="offb-output"></pre>
      <div class="modal-foot">
        <span id="action-status">…</span>
        <span>Output streams live; closing this window does not stop the run.</span>
      </div>
    </div>
  </div>

  <section class="grid">
    <div class="panel">
      <h2>Bucket counts — last {{ window_h }}h</h2>
      <div class="chart-wrap"><canvas id="trend"></canvas></div>
    </div>
    <div class="panel">
      <h2>Current snapshot</h2>
      <div class="totals" id="totals"></div>
      <div class="chart-wrap small"><canvas id="pie"></canvas></div>
    </div>
  </section>

  <section class="grid-2">
    <div class="panel">
      <h2>Tasks due <span id="sel-date">…</span> (<span id="today-count">…</span>)</h2>
      <table>
        <thead><tr><th>Bucket</th><th>Title</th><th>Due</th></tr></thead>
        <tbody id="tasks-tbody"><tr><td colspan="3" class="empty">loading…</td></tr></tbody>
      </table>
    </div>
    <div class="panel">
      <h2>Deactivation log (newest first)</h2>
      <div class="log" id="action-log">loading…</div>
    </div>
  </section>

<script src="/static/chart.umd.min.js"></script>
<script>
const BUCKET_COLORS = {
  offboarding: '#ef4444', access: '#3b82f6',
  procurement: '#f59e0b', other: '#6b7280'
};
const BUCKET_LABEL = {{ bucket_labels|tojson }};
const SHORT = { offboarding: 'offb', access: 'acc', procurement: 'proc', other: 'other' };

let trendChart, pieChart;

async function fetchJSON(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
}

function fmtTs(iso) {
  try { return new Date(iso).toLocaleString(); } catch (e) { return iso; }
}

function renderTotals(latest) {
  const el = document.getElementById('totals');
  if (!latest) { el.innerHTML = '<span class="empty">no data yet</span>'; return; }
  el.innerHTML = ['offboarding','access','procurement','other'].map(b => `
    <div class="pill">
      <span class="dot ${SHORT[b]}"></span>
      <span>${BUCKET_LABEL[b]}</span>
      <span class="count">${latest[b]}</span>
    </div>`).join('');
}

function renderTrend(runs) {
  const labels = runs.map(r => fmtTs(r.ran_at).replace(/^\d+\/\d+\/\d+,?\s*/, ''));
  const datasets = ['offboarding','access','procurement','other'].map(b => ({
    label: BUCKET_LABEL[b],
    data: runs.map(r => r[b]),
    backgroundColor: BUCKET_COLORS[b],
    stack: 'stack1'
  }));
  const ctx = document.getElementById('trend');
  if (trendChart) trendChart.destroy();
  trendChart = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#e5e7eb' } } },
      scales: {
        x: { stacked: true, ticks: { color: '#9ca3af', maxRotation: 0, autoSkip: true } },
        y: { stacked: true, ticks: { color: '#9ca3af' }, grid: { color: '#262c38' } }
      }
    }
  });
}

function renderPie(latest) {
  const ctx = document.getElementById('pie');
  if (pieChart) pieChart.destroy();
  if (!latest) return;
  pieChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['offboarding','access','procurement','other'].map(b => BUCKET_LABEL[b]),
      datasets: [{
        data: ['offboarding','access','procurement','other'].map(b => latest[b]),
        backgroundColor: ['#ef4444','#3b82f6','#f59e0b','#6b7280']
      }]
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom', labels: { color: '#e5e7eb' } } }
    }
  });
}

function renderTasks(tasks, date, counts) {
  const tbody = document.getElementById('tasks-tbody');
  const countEl = document.getElementById('today-count');
  const dateEl = document.getElementById('sel-date');
  if (dateEl && date) dateEl.textContent = date;
  if (countEl) countEl.textContent = tasks.length;
  if (!tasks.length) { tbody.innerHTML = '<tr><td colspan="3" class="empty">nothing due on this date</td></tr>'; return; }
  const order = { offboarding: 0, access: 1, procurement: 2, other: 3 };
  tasks.sort((a,b) => (order[a.bucket]||9)-(order[b.bucket]||9) || a.name.localeCompare(b.name));
  tbody.innerHTML = tasks.map(t => {
    const title = t.permalink_url
      ? `<a href="${t.permalink_url}" target="_blank">${escapeHtml(t.name)}</a>`
      : escapeHtml(t.name);
    const suggest = t.bucket === 'other'
      ? ` <button class="suggest-btn" data-gid="${t.gid}" title="LLM suggestion (display only)">🔍 suggest</button>`
      : '';
    return `
    <tr>
      <td class="bucket-cell"><span class="bucket-tag ${SHORT[t.bucket]}">${BUCKET_LABEL[t.bucket]}</span></td>
      <td>${title}${suggest}</td>
      <td>${t.due_on || ''}</td>
    </tr>
    <tr class="triage-row" id="triage-${t.gid}" hidden><td colspan="3" class="triage-cell"></td></tr>`;
  }).join('');

  tbody.querySelectorAll('.suggest-btn').forEach(b =>
    b.addEventListener('click', () => triageTask(b.dataset.gid, b)));

  // Re-display any triage results we already have (survives the 60s refresh).
  for (const gid of Object.keys(triageCache)) showTriage(gid, triageCache[gid], true);
}

const triageCache = {};

function confClass(c) {
  return c === 'high' ? 'conf-high' : c === 'medium' ? 'conf-med' : 'conf-low';
}

function showTriage(gid, t, cached) {
  const row = document.getElementById('triage-' + gid);
  if (!row) return;   // task not in the current (today) list
  const cell = row.querySelector('.triage-cell');
  row.hidden = false;
  cell.innerHTML = `
    <div class="triage">
      <div class="triage-head">
        <span class="conf ${confClass(t.confidence)}">${escapeHtml(t.confidence || 'low')}</span>
        <strong>${escapeHtml(t.subcategory || '')}</strong>
        ${t.target_service ? `<span class="t-svc">→ ${escapeHtml(t.target_service)}</span>` : ''}
        ${cached ? '<span class="t-cached">cached</span>' : ''}
        <button class="t-rerun" data-gid="${gid}" title="Re-run triage">↻</button>
      </div>
      <div class="t-action">${escapeHtml(t.suggested_action || '')}</div>
      ${t.rationale ? `<div class="t-rat">${escapeHtml(t.rationale)}</div>` : ''}
    </div>`;
  const rerun = cell.querySelector('.t-rerun');
  if (rerun) rerun.addEventListener('click', () => triageTask(gid, rerun, true));
}

async function triageTask(gid, btn, force) {
  const row = document.getElementById('triage-' + gid);
  if (row) {
    row.hidden = false;
    row.querySelector('.triage-cell').innerHTML = '<span class="empty">analysing…</span>';
  }
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/triage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gid, force: !!force })
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || (r.status + ' ' + r.statusText));
    triageCache[gid] = j.triage;
    showTriage(gid, j.triage, j.cached);
  } catch (e) {
    if (row) row.querySelector('.triage-cell').innerHTML =
      '<span class="st-bad">triage failed: ' + escapeHtml(e.message) + '</span>';
  } finally {
    if (btn) btn.disabled = false;
  }
}

function statusClass(status) {
  const s = (status || '').toLowerCase();
  if (s === 'deactivated' || s === 'granted' || s.startsWith('invited') || s === 'removed') return 'st-ok';
  if (s.startsWith('failed') || s.startsWith('error')) return 'st-bad';
  if (s.startsWith('needs-confirmation') || s === 'user-not-found' || s.startsWith('missing')) return 'st-warn';
  return 'st-neutral';
}

function renderActions(actions) {
  const el = document.getElementById('action-log');
  if (!actions.length) { el.textContent = 'no deactivations recorded yet'; return; }
  el.innerHTML = actions.map(a => {
    const shot = a.screenshot
      ? ` <a class="shot-link" href="/shot?p=${encodeURIComponent(a.screenshot)}" target="_blank" title="${escapeHtml(a.screenshot)}">📷</a>`
      : '';
    return `
    <div class="row">
      <span class="ts">${escapeHtml(fmtTs(a.ran_at))}</span>
      <span class="kind ${a.kind === 'grant' ? 'grant' : 'deactivate'}">${a.kind === 'grant' ? 'grant' : 'deact'}</span>
      <span class="svc">${escapeHtml(a.service || '')}</span>
      <span class="tgt">${escapeHtml(a.target || '')}</span>
      <span class="${statusClass(a.status)}">${escapeHtml(a.status || '')}</span>${shot}
    </div>`;
  }).join('');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function selectedDate() {
  return document.getElementById('monitor-date').value || localToday();
}
function selectedInclCompleted() {
  return document.getElementById('include-completed').checked;
}

async function refresh() {
  try {
    const date = selectedDate();
    const incl = selectedInclCompleted() ? '1' : '0';
    const [{ runs }, { latest }, { actions }, byDate] = await Promise.all([
      fetchJSON('/api/runs'),
      fetchJSON('/api/current'),
      fetchJSON('/api/actions'),
      fetchJSON(`/api/by-date?date=${encodeURIComponent(date)}&include_completed=${incl}`)
    ]);
    const meta = document.getElementById('meta');
    meta.textContent = latest
      ? `Last snapshot: ${fmtTs(latest.ran_at)} — total open tasks: ${latest.task_count}`
      : 'No snapshot yet — first one is being taken.';
    renderTotals(latest);
    if (typeof Chart !== 'undefined') {
      renderTrend(runs.slice().reverse());   // oldest → newest on the chart
      renderPie(latest);
    } else {
      // Chart.js failed to load — show a clear message instead of silently
      // leaving the canvas blank.
      for (const id of ['trend', 'pie']) {
        const c = document.getElementById(id);
        if (c) c.replaceWith(Object.assign(document.createElement('div'),
          { className: 'empty', textContent: 'Chart.js failed to load (check /static/chart.umd.min.js)' }));
      }
    }
    renderTasks(byDate.tasks || [], byDate.date, byDate.counts || {});
    renderActions(actions || []);
  } catch (e) {
    document.getElementById('meta').textContent = 'refresh failed: ' + e.message;
  }
}

// Periodic refresh — replaces the old <meta http-equiv="refresh"> that did
// a full page reload every 60s (visually jarring and races with charts).
setInterval(refresh, 60_000);

document.getElementById('run-now').addEventListener('click', async () => {
  const btn = document.getElementById('run-now');
  btn.disabled = true; btn.textContent = 'Running…';
  try {
    await fetch('/api/run', { method: 'POST' });
    await refresh();
  } finally {
    btn.disabled = false; btn.textContent = 'Run now';
  }
});

document.getElementById('stop-service').addEventListener('click', async () => {
  const btn = document.getElementById('stop-service');
  btn.disabled = true; btn.textContent = 'Stopping…';
  try {
    await fetch('/api/stop', { method: 'POST' });
    document.getElementById('meta').textContent = 'Service is stopping…';
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Stop Service';
    document.getElementById('meta').textContent = 'stop failed: ' + e.message;
  }
});

// ---- Actions: Offboarding (deactivate.py) + Onboarding (grant.py) ----
function localToday() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
document.getElementById('monitor-date').value = localToday();
// Re-analyze when the date or completed-filter changes.
document.getElementById('monitor-date').addEventListener('change', refresh);
document.getElementById('include-completed').addEventListener('change', refresh);

const ACTION_META = {
  deactivate: { endpoint: '/api/offboard', label: 'Offboarding', btn: 'offboarding' },
  grant:      { endpoint: '/api/onboard',  label: 'Onboarding',  btn: 'onboarding'  }
};
let actionPollTimer = null;

function setActionButtons(disabled, runningKind, preview) {
  for (const [kind, m] of Object.entries(ACTION_META)) {
    const btn = document.getElementById(m.btn);
    if (!btn) continue;
    btn.disabled = disabled;
    btn.textContent = (disabled && kind === runningKind)
      ? (preview ? 'Previewing…' : 'Running…')
      : m.label;
  }
}

function renderActionStatus(s) {
  const out = document.getElementById('action-output');
  const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  out.textContent = (s.lines || []).join('\n');
  if (atBottom) out.scrollTop = out.scrollHeight;

  const st = document.getElementById('action-status');
  const label = s.kind === 'grant' ? 'Onboarding' : 'Offboarding';
  if (s.running) {
    st.className = '';
    st.textContent = `${label} ${s.preview ? '(preview)' : '(LIVE)'} for ${s.date}…`;
  } else if (s.returncode === null || s.returncode === undefined) {
    st.className = ''; st.textContent = 'idle';
  } else if (s.returncode === 0) {
    st.className = 'ok'; st.textContent = `${label} finished ${s.date} — exit 0`;
  } else {
    st.className = 'err'; st.textContent = `${label} finished ${s.date} — exit ${s.returncode}`;
  }
}

async function pollAction() {
  try {
    const s = await fetchJSON('/api/action/status');
    renderActionStatus(s);
    if (!s.running) {
      clearInterval(actionPollTimer); actionPollTimer = null;
      setActionButtons(false);
      refresh();   // bucket counts + deactivation log may have changed
    }
  } catch (e) { /* keep polling */ }
}

async function runAction(kind) {
  const m = ACTION_META[kind];
  const date = selectedDate();
  const preview = document.getElementById('action-preview').checked;
  if (!preview) {
    const verb = kind === 'grant'
      ? `grant access/licenses for ${date}`
      : `deactivate users for ${date}`;
    if (!confirm(`Run REAL ${m.label} — ${verb}?\n\nThis performs live changes in the connected services. ` +
                 `Uncheck "preview" only when you're ready.`)) {
      return;
    }
  }
  setActionButtons(true, kind, preview);
  document.getElementById('action-title').textContent =
    `${m.label} — ${date} ${preview ? '(preview)' : '(LIVE)'}`;
  document.getElementById('action-output').textContent = 'starting…';
  document.getElementById('action-modal').hidden = false;
  try {
    const r = await fetch(m.endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date, preview })
    });
    if (!r.ok && r.status !== 409) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error || (r.status + ' ' + r.statusText));
    }
  } catch (e) {
    document.getElementById('action-output').textContent = 'failed to start: ' + e.message;
    setActionButtons(false);
    return;
  }
  if (actionPollTimer) clearInterval(actionPollTimer);
  actionPollTimer = setInterval(pollAction, 1500);
  pollAction();
}

document.getElementById('offboarding').addEventListener('click', () => runAction('deactivate'));
document.getElementById('onboarding').addEventListener('click', () => runAction('grant'));
document.getElementById('action-close').addEventListener('click', () => {
  document.getElementById('action-modal').hidden = true;
});

refresh();
</script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    return render_template_string(
        DASHBOARD_HTML,
        bucket_labels=BUCKET_LABELS,
        window_h=24,
    )


@app.route("/api/runs")
def api_runs():
    limit = int(request.args.get("limit", 96))
    return jsonify({"runs": recent_runs(limit=limit)})


@app.route("/api/current")
def api_current():
    # Snapshot overview for totals/pie/trend — ALL open tasks (latest run).
    return jsonify({"latest": latest_run()})


@app.route("/api/by-date")
def api_by_date():
    """Fetch + classify the tasks DUE ON a given date (default: today),
    straight from Asana so the selected date is always accurate (not limited
    to the periodic snapshot's top-100 open tasks).

    Query: ?date=YYYY-MM-DD&include_completed=0|1
    """
    import re as _re
    from datetime import date as _date

    date_str = (request.args.get("date") or _date.today().isoformat()).strip()
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        return jsonify({"error": f"bad date: {date_str!r}"}), 400
    include_completed = request.args.get("include_completed", "0") in ("1", "true", "yes")

    try:
        # text_filter="" → every task assigned to me due on that date.
        from asana_client import fetch_tasks_due_on
        raw = fetch_tasks_due_on(date_str, text_filter="", include_completed=include_completed)
    except AsanaError as e:
        return jsonify({"error": f"asana: {e}"}), 502

    counts, enriched = classify_many(raw)
    tasks = [
        {
            "gid": t.get("gid", ""),
            "name": t.get("name", ""),
            "bucket": t.get("bucket", "other"),
            "due_on": t.get("due_on"),
            "permalink_url": t.get("permalink_url"),
            "completed": t.get("completed", False),
        }
        for t in enriched
    ]
    return jsonify({"date": date_str, "counts": counts, "tasks": tasks})


@app.route("/api/run", methods=["POST"])
def api_run():
    result = analyze_once()
    return jsonify(result)


HERE = Path(__file__).resolve().parent
_ACTION_MAX_LINES = 4000

# A single shared runner: deactivate.py and grant.py both drive the same
# persistent browser profile, so only one may run at a time.
_action_lock = threading.Lock()
_action: dict = {
    "running": False,
    "kind": None,          # 'deactivate' | 'grant'
    "date": None,
    "preview": False,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "pid": None,
    "lines": [],
}

# kind -> (script, preview_flag). grant.py has no --find-only; its preview is
# --dry-run (prints the plan and exits without touching a browser).
_ACTION_SPEC = {
    "deactivate": ("deactivate.py", "--find-only"),
    "grant": ("grant.py", "--dry-run"),
}


def _run_action(kind: str, date_str: str, preview: bool, services: list[str] | None = None) -> None:
    """Spawn deactivate.py / grant.py and stream output into `_action`.
    On a non-preview run, parse RESULT| lines and persist them to the
    action log. Runs in a daemon thread."""
    import subprocess

    script, preview_flag = _ACTION_SPEC[kind]
    cmd = [sys.executable, str(HERE / script), "--date", date_str, "--yes"]
    if preview:
        cmd.append(preview_flag)
    for svc in (services or []):
        cmd += ["--service", svc]

    results: list[dict] = []

    def _append(line: str) -> None:
        with _action_lock:
            _action["lines"].append(line)
            extra = len(_action["lines"]) - _ACTION_MAX_LINES
            if extra > 0:
                del _action["lines"][:extra]

    _append(f"$ {' '.join(cmd)}")
    env = {**os.environ, "ACTION_LOG_STREAM": "1"}
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(HERE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
    except Exception as e:  # noqa: BLE001
        _append(f"failed to start: {type(e).__name__}: {e}")
        with _action_lock:
            _action["running"] = False
            _action["returncode"] = -1
            _action["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return

    with _action_lock:
        _action["pid"] = proc.pid
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.rstrip("\n")
        if line.startswith("RESULT|"):
            # RESULT|gid|service|target|status[|screenshot]
            parts = line.split("|", 5)
            if len(parts) >= 5:
                gid, service, target, status = parts[1], parts[2], parts[3], parts[4]
                shot = parts[5] if len(parts) >= 6 else ""
                results.append({
                    "gid": gid, "service": service, "target": target,
                    "status": status, "screenshot": shot or None,
                })
            continue  # keep RESULT lines out of the human-readable stream
        _append(line)
    proc.wait()

    # Persist actual actions only (skip previews — they take no real action).
    if not preview and results:
        try:
            insert_actions(kind=kind, due_date=date_str, results=results)
        except Exception:  # noqa: BLE001
            log.exception("failed to persist action log")

    with _action_lock:
        _action["running"] = False
        _action["returncode"] = proc.returncode
        _action["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log.info(
        "%s finished: date=%s preview=%s rc=%s results=%d",
        kind, date_str, preview, proc.returncode, len(results),
    )


def _start_action(kind: str):
    """Shared endpoint body for /api/offboard and /api/onboard."""
    import re as _re
    from datetime import date as _date

    body = request.get_json(silent=True) or {}
    date_str = str(body.get("date") or _date.today().isoformat()).strip()
    # Accept either "preview" or the legacy "find_only" key.
    preview = bool(body.get("preview", body.get("find_only", False)))
    # Optional service filter (string or list) → deactivate.py --service.
    svc = body.get("service") or body.get("services") or []
    services = [svc] if isinstance(svc, str) else list(svc)

    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        return jsonify({"error": f"bad date: {date_str!r}"}), 400

    with _action_lock:
        if _action["running"]:
            return jsonify({
                "error": f"{_action['kind']} already running", "running": True
            }), 409
        _action.update({
            "running": True,
            "kind": kind,
            "date": date_str,
            "preview": preview,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "finished_at": None,
            "returncode": None,
            "pid": None,
            "lines": [],
        })

    threading.Thread(
        target=_run_action, args=(kind, date_str, preview, services), daemon=True, name=kind
    ).start()
    log.info("%s started: date=%s preview=%s services=%s", kind, date_str, preview, services)
    return jsonify({"status": "started", "kind": kind, "date": date_str,
                    "preview": preview, "services": services})


@app.route("/api/offboard", methods=["POST"])
def api_offboard():
    """Run deactivate.py for a date (default today). preview=True → --find-only."""
    return _start_action("deactivate")


@app.route("/api/onboard", methods=["POST"])
def api_onboard():
    """Run grant.py for a date (default today). preview=True → --dry-run."""
    return _start_action("grant")


@app.route("/api/action/status")
@app.route("/api/offboard/status")  # backwards-compatible alias
def api_action_status():
    with _action_lock:
        snap = {**_action, "lines": list(_action["lines"])}
    # Expose find_only too so older clients keep working.
    snap["find_only"] = snap.get("preview", False)
    return jsonify(snap)


@app.route("/api/actions")
def api_actions():
    limit = int(request.args.get("limit", 200))
    kind = request.args.get("kind") or None
    return jsonify({"actions": recent_actions(limit=limit, kind=kind)})


@app.route("/shot")
def api_shot():
    """Serve a proof screenshot for the deactivation log. Confined to the
    screenshots/ directory to prevent path traversal."""
    from flask import abort, send_file

    rel = request.args.get("p", "")
    shots_root = (HERE / "screenshots").resolve()
    target = (HERE / rel).resolve()
    # Must stay inside screenshots/ and exist.
    if not str(target).startswith(str(shots_root) + os.sep) or not target.is_file():
        abort(404)
    return send_file(str(target))


@app.route("/api/triage", methods=["POST"])
def api_triage():
    """LLM-triage a single 'Other' task. Display-only — no Asana writes.

    Body: {"gid": "...", "force": bool?}. Uses a cached result keyed by the
    task title hash unless `force` is set.
    """
    import hashlib

    body = request.get_json(silent=True) or {}
    gid = str(body.get("gid", "")).strip()
    force = bool(body.get("force", False))
    if not gid:
        return jsonify({"error": "gid required"}), 400

    try:
        task = fetch_task(gid)
    except AsanaError as e:
        return jsonify({"error": f"asana: {e}"}), 502

    name = task.get("name", "") or ""
    notes = task.get("notes", "") or ""
    title_hash = hashlib.sha256(f"{name}\n{notes}".encode("utf-8")).hexdigest()[:16]

    if not force:
        cached = get_triage(gid)
        if cached and cached.get("title_hash") == title_hash:
            return jsonify({"triage": cached, "cached": True})

    try:
        result = triage_task(name, notes)
    except TriageError as e:
        return jsonify({"error": f"triage: {e}"}), 502

    upsert_triage(gid, title_hash, result)
    return jsonify({"triage": {**result, "gid": gid, "title_hash": title_hash}, "cached": False})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    shutdown = request.environ.get("werkzeug.server.shutdown")

    def stop_later() -> None:
        time.sleep(0.35)
        if _scheduler is not None:
            _scheduler.stop()
        if callable(shutdown):
            shutdown()
        else:
            os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=stop_later, daemon=True, name="stop-service").start()
    return jsonify({"status": "stopping"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=5111, help="HTTP port (default: 5111)")
    ap.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    ap.add_argument("--interval", type=int, default=15 * 60,
                    help="Seconds between snapshots (default: 900 = 15 min)")
    ap.add_argument("--once", action="store_true",
                    help="Run a single snapshot then exit (no web server)")
    args = ap.parse_args()

    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
    init_db()

    if args.once:
        analyze_once()
        return 0

    global _scheduler
    _scheduler = Scheduler(interval_s=args.interval)
    _scheduler.start()

    log.info("dashboard: http://%s:%d  (interval=%ds)", args.host, args.port, args.interval)
    try:
        # use_reloader=False so the scheduler thread isn't duplicated in debug mode.
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    finally:
        if _scheduler is not None:
            _scheduler.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
