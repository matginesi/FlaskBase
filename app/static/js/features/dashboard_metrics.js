(function(){
  'use strict';

  function $(id){ return document.getElementById(id); }

  function clamp(n, a, b){
    n = Number(n);
    if(!Number.isFinite(n)) return a;
    return Math.max(a, Math.min(b, n));
  }

  function cssVar(name, fallback){
    try{
      const v = getComputedStyle(document.documentElement).getPropertyValue(name);
      return (v && String(v).trim()) ? String(v).trim() : fallback;
    }catch(e){
      return fallback;
    }
  }

  function ensureCanvasSize(canvas){
    if(!canvas) return null;
    const dpr = window.devicePixelRatio || 1;
    const cw = Math.max(1, Math.floor(canvas.clientWidth));
    const chAttr = Number(canvas.getAttribute('height') || 180);
    const cssHeight = Math.max(120, Math.floor(canvas.clientHeight || chAttr || 180));
    const ch = cssHeight;
    const wantW = Math.floor(cw * dpr);
    const wantH = Math.floor(ch * dpr);

    // Avoid flicker: only resize when needed
    if(canvas.width !== wantW || canvas.height !== wantH){
      canvas.width = wantW;
      canvas.height = wantH;
    }
    return {w: wantW, h: wantH, dpr};
  }

  function drawGrid(ctx, w, h, pad, opts){
    const grid = (opts && opts.grid) ? opts.grid : {x:5, y:4};
    const gx = Math.max(2, Number(grid.x || 5));
    const gy = Math.max(2, Number(grid.y || 4));
    const minorX = gx * 2;
    const minorY = gy * 2;

    ctx.save();
    ctx.fillStyle = cssVar('--chart-surface', 'rgba(248,250,252,.92)');
    ctx.fillRect(0, 0, w, h);

    ctx.strokeStyle = cssVar('--chart-grid-minor', 'rgba(148,163,184,.14)');
    ctx.globalAlpha = 1;
    ctx.lineWidth = 1;
    if(ctx.setLineDash) ctx.setLineDash([]);

    for(let i=0;i<=minorX;i++){
      const x = pad + (w - pad*2) * (i/minorX);
      ctx.beginPath();
      ctx.moveTo(x, pad);
      ctx.lineTo(x, h - pad);
      ctx.stroke();
    }
    for(let j=0;j<=minorY;j++){
      const y = pad + (h - pad*2) * (j/minorY);
      ctx.beginPath();
      ctx.moveTo(pad, y);
      ctx.lineTo(w - pad, y);
      ctx.stroke();
    }

    ctx.strokeStyle = cssVar('--chart-grid-major', 'rgba(100,116,139,.28)');
    if(ctx.setLineDash) ctx.setLineDash([4, 6]);

    // verticals
    for(let i=0;i<=gx;i++){
      const x = pad + (w - pad*2) * (i/gx);
      ctx.beginPath();
      ctx.moveTo(x, pad);
      ctx.lineTo(x, h - pad);
      ctx.stroke();
    }
    // horizontals
    for(let j=0;j<=gy;j++){
      const y = pad + (h - pad*2) * (j/gy);
      ctx.beginPath();
      ctx.moveTo(pad, y);
      ctx.lineTo(w - pad, y);
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawLineChart(canvas, values, opts){
    if(!canvas) return;
    const ctx = canvas.getContext('2d');
    if(!ctx) return;

    const size = ensureCanvasSize(canvas);
    if(!size) return;

    const w = size.w;
    const h = size.h;
    const dpr = size.dpr;

    const pad = 10 * dpr;
    const maxV = (opts && Number.isFinite(opts.max)) ? Number(opts.max) : Math.max(1, ...values);
    const minV = (opts && Number.isFinite(opts.min)) ? Number(opts.min) : 0;

    ctx.clearRect(0,0,w,h);

    // grid + baseline
    ctx.strokeStyle = cssVar('--chart-grid-major', 'rgba(100,116,139,.28)');
    drawGrid(ctx, w, h, pad, opts);

    // baseline
    ctx.save();
    ctx.globalAlpha = 0.25;
    ctx.beginPath();
    ctx.moveTo(pad, h - pad);
    ctx.lineTo(w - pad, h - pad);
    ctx.stroke();
    ctx.restore();

    if(!values || values.length < 2) return;

    const n = values.length;
    const dx = (w - pad*2) / (n - 1);

    // line
    ctx.strokeStyle = (opts && opts.color) ? opts.color : cssVar('--accent', '#2563eb');
    ctx.lineWidth = 2 * dpr;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    ctx.beginPath();
    for(let i=0;i<n;i++){
      const v = clamp(values[i], minV, maxV);
      const t = (v - minV) / (maxV - minV || 1);
      const x = pad + dx * i;
      const y = (h - pad) - t * (h - pad*2);
      if(i===0) ctx.moveTo(x,y);
      else ctx.lineTo(x,y);
    }
    ctx.stroke();

    // subtle fill
    ctx.save();
    ctx.globalAlpha = 0.08;
    ctx.fillStyle = ctx.strokeStyle;
    ctx.lineTo(w - pad, h - pad);
    ctx.lineTo(pad, h - pad);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  function widthClassFor(val){
    return 'pct-w-' + String(clamp(Math.round(Number(val) || 0), 0, 100));
  }

  function setWidthClass(el, val){
    if(!el) return;
    const pct = clamp(val, 0, 100);
    for(const cls of Array.from(el.classList)){
      if(cls.startsWith('pct-w-')) el.classList.remove(cls);
    }
    el.classList.add(widthClassFor(pct));
    el.setAttribute('aria-valuenow', String(Math.round(pct)));
  }

  function setText(id, value){
    const el = $(id);
    if(el) el.textContent = value;
  }

  function renderResource(prefix, pct, used, total, unit){
    const safePct = clamp(pct, 0, 100);
    setWidthClass($(prefix + 'PctBar'), safePct);
    setText(prefix + 'PctVal', safePct.toFixed(1) + '%');
    const usedTxt = Number.isFinite(used) ? used.toFixed(unit === 'GB' ? 1 : 0) + ' ' + unit : '—';
    const totalTxt = Number.isFinite(total) ? total.toFixed(unit === 'GB' ? 1 : 0) + ' ' + unit : '—';
    setText(prefix + 'UsedVal', usedTxt);
    setText(prefix + 'TotalVal', totalTxt);
    setText(prefix + 'UsageCopy', (Number.isFinite(used) && Number.isFinite(total)) ? (usedTxt + ' / ' + totalTxt) : '—');
  }

  function setWidth(id, pct){
    const el = $(id);
    if(!el) return;
    setWidthClass(el, pct);
  }

  function renderWorkers(workers, threads, capacity, timeout){
    const w = Number(workers);
    const t = Number(threads);
    const c = Number(capacity);
    if(Number.isFinite(w)) setText('gunicornWorkersVal', String(w));
    if(Number.isFinite(t)) setText('gunicornThreadsVal', String(t));
    if(Number.isFinite(c)) setText('gunicornCapacityVal', String(c));
    if(Number.isFinite(timeout)) setText('gunicornTimeoutVal', String(timeout));
    const denom = Number.isFinite(c) && c > 0 ? c : 1;
    setWidth('gunicornWorkersBar', (Number.isFinite(w) ? (w / denom) * 100 : 0));
  }

  function renderJobs(queued, running, completed, failed, queuesEnabled, queuesPaused, threadAlive, mode, backend){
    const q = Number(queued);
    const r = Number(running);
    const c = Number(completed);
    const f = Number(failed);
    if(Number.isFinite(q)) setText('jobQueuedVal', String(q));
    if(Number.isFinite(r)) setText('jobRunningVal', String(r));
    if(Number.isFinite(c)) setText('jobCompletedVal', String(c));
    if(Number.isFinite(f)) setText('jobFailedVal', String(f));
    if(Number.isFinite(Number(queuesEnabled))) setText('jobQueuesEnabledVal', String(Number(queuesEnabled)));
    if(Number.isFinite(Number(queuesPaused))) setText('jobQueuesPausedVal', String(Number(queuesPaused)));
    if(typeof threadAlive !== 'undefined' && $('jobThreadAliveVal')) $('jobThreadAliveVal').textContent = threadAlive ? 'online' : 'offline';
    if(mode) setText('jobRuntimeModeVal', String(mode));
    if(backend) setText('jobRuntimeBackendVal', String(backend));
    const total = Math.max(1, (Number.isFinite(q) ? q : 0) + (Number.isFinite(r) ? r : 0) + (Number.isFinite(c) ? c : 0) + (Number.isFinite(f) ? f : 0));
    setWidth('jobQueuedBar', ((Number.isFinite(q) ? q : 0) / total) * 100);
    setWidth('jobRunningBar', ((Number.isFinite(r) ? r : 0) / total) * 100);
    setWidth('jobCompletedBar', ((Number.isFinite(c) ? c : 0) / total) * 100);
    setWidth('jobFailedBar', ((Number.isFinite(f) ? f : 0) / total) * 100);
  }

  const root = $('sysMetrics');
  if(!root) return;

  const refreshSec = clamp(root.getAttribute('data-refresh-sec') || 8, 3, 10);

  // history buffers (~60s)
  const maxPoints = Math.max(12, Math.floor(60 / refreshSec));
  const cpuHist = [];
  const loadHist = [];
  const connectedHist = String(root.getAttribute('data-connected-series') || '')
    .split(',')
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value));
  const connectedLabels = String(root.getAttribute('data-connected-labels') || '')
    .split('|')
    .map((value) => String(value || '').trim())
    .filter(Boolean);

  function pushHist(arr, v){
    arr.push(Number.isFinite(Number(v)) ? Number(v) : 0);
    while(arr.length > maxPoints) arr.shift();
  }

  // init
  pushHist(cpuHist, Number(root.getAttribute('data-initial-cpu') || 0));
  pushHist(loadHist, Number(root.getAttribute('data-initial-load') || 0));
  renderResource('ram', Number(root.getAttribute('data-initial-ram-pct') || 0), Number(root.getAttribute('data-initial-ram-used') || 0), Number(root.getAttribute('data-initial-ram-total') || 0), 'MB');
  renderResource('disk', Number(root.getAttribute('data-initial-disk-pct') || 0), Number(root.getAttribute('data-initial-disk-used') || 0), Number(root.getAttribute('data-initial-disk-total') || 0), 'GB');
  renderWorkers(
    Number(root.getAttribute('data-gunicorn-workers') || 1),
    Number(root.getAttribute('data-gunicorn-threads') || 1),
    Number(root.getAttribute('data-gunicorn-capacity') || 1),
    Number(root.getAttribute('data-gunicorn-timeout') || 120)
  );
  renderJobs(
    Number(root.getAttribute('data-job-queued') || 0),
    Number(root.getAttribute('data-job-running') || 0),
    Number(root.getAttribute('data-job-completed') || 0),
    Number(root.getAttribute('data-job-failed') || 0),
    Number(root.getAttribute('data-job-queues-enabled') || 0),
    Number(root.getAttribute('data-job-queues-paused') || 0),
    root.getAttribute('data-job-thread-alive') === 'true',
    root.getAttribute('data-job-runtime-mode') || '',
    root.getAttribute('data-job-runtime-backend') || ''
  );

  const cpuColor = cssVar('--accent', '#2563eb');
  const loadColor = cssVar('--success', '#16a34a');
  const connectedColor = cssVar('--info', '#0891b2');

  function redraw(){
    drawLineChart($('cpuChart'), cpuHist, {min:0, max:100, color: cpuColor, grid:{x:6,y:4}});
    const mx = Math.max(1, ...loadHist);
    drawLineChart($('loadChart'), loadHist, {min:0, max: mx * 1.25, color: loadColor, grid:{x:6,y:4}});
    if(connectedHist.length){
      drawLineChart($('connectedUsersChart'), connectedHist, {
        min:0,
        max: Math.max(1, ...connectedHist),
        color: connectedColor,
        grid:{x:6,y:4}
      });
      setText('connectedChartCurrent', String(connectedHist[connectedHist.length - 1] ?? 0));
      setText('connectedChartPeak', String(Math.max(...connectedHist, 0)));
    }
    if($('cpuChartCurrent')) $('cpuChartCurrent').textContent = (cpuHist[cpuHist.length - 1] ?? 0).toFixed(1) + '%';
    if($('cpuChartPeak')) $('cpuChartPeak').textContent = Math.max(...cpuHist, 0).toFixed(1) + '%';
    if($('loadChartCurrent')) $('loadChartCurrent').textContent = String((loadHist[loadHist.length - 1] ?? 0).toFixed ? (loadHist[loadHist.length - 1] ?? 0).toFixed(2) : (loadHist[loadHist.length - 1] ?? 0));
    if($('loadChartPeak')) $('loadChartPeak').textContent = Math.max(...loadHist, 0).toFixed(2);
  }

  redraw();

  let inflight = false;
  async function tick(){
    if(inflight) return;
    inflight = true;
    try{
      const res = await fetch('/metrics', {headers:{'Accept':'application/json'}});
      if(!res.ok) return;
      const data = await res.json();

      if(typeof data.cpu_pct !== 'undefined'){
        const cpu = Number(data.cpu_pct);
        if(Number.isFinite(cpu)){
          if($('cpuPctVal')) $('cpuPctVal').textContent = cpu.toFixed(1) + '%';
          setWidthClass($('cpuPctBar'), cpu);
          pushHist(cpuHist, cpu);
        }
      }

      if(typeof data.load_1 !== 'undefined'){
        const l1 = data.load_1;
        const l5 = data.load_5;
        const l15 = data.load_15;

        if($('load1Val')) $('load1Val').textContent = (l1 === null || typeof l1 === 'undefined') ? '—' : String(l1);
        if($('load5Val')) $('load5Val').textContent = (l5 === null || typeof l5 === 'undefined') ? '—' : String(l5);
        if($('load15Val')) $('load15Val').textContent = (l15 === null || typeof l15 === 'undefined') ? '—' : String(l15);

        const loadNum = Number(l1);
        if(Number.isFinite(loadNum)){
          pushHist(loadHist, loadNum);
        }
      }

      // RAM: compute pct if server doesn't provide it
      let ramPct = (typeof data.ram_used_pct !== 'undefined') ? Number(data.ram_used_pct) : NaN;
      if(!Number.isFinite(ramPct)){
        const used = Number(data.ram_used_mb);
        const tot = Number(data.ram_total_mb);
        if(Number.isFinite(used) && Number.isFinite(tot) && tot > 0){
          ramPct = (used / tot) * 100.0;
        }
      }
      if(Number.isFinite(ramPct)){
        renderResource('ram', ramPct, Number(data.ram_used_mb), Number(data.ram_total_mb), 'MB');
      }

      // Disk: compute pct if missing
      let diskPct = (typeof data.disk_used_pct !== 'undefined') ? Number(data.disk_used_pct) : NaN;
      if(!Number.isFinite(diskPct)){
        const used = Number(data.disk_used_gb);
        const tot = Number(data.disk_total_gb);
        if(Number.isFinite(used) && Number.isFinite(tot) && tot > 0){
          diskPct = (used / tot) * 100.0;
        }
      }
      if(Number.isFinite(diskPct)){
        renderResource('disk', diskPct, Number(data.disk_used_gb), Number(data.disk_total_gb), 'GB');
      }

      renderWorkers(data.gunicorn_workers, data.gunicorn_threads, data.gunicorn_capacity, data.gunicorn_timeout);
      renderJobs(
        data.job_queued,
        data.job_running,
        data.job_completed_24h,
        data.job_failed_24h,
        data.job_queues_enabled,
        data.job_queues_paused,
        data.job_runtime_thread_alive,
        data.job_runtime_mode,
        data.job_runtime_backend
      );
      if(typeof data.connected_window_min !== 'undefined'){
        setText('connectedWindowVal', String(data.connected_window_min));
      }
      if(typeof data.connected_count !== 'undefined'){
        setText('connectedCountVal', String(data.connected_count));
        setText('connectedChartCurrent', String(data.connected_count));
      }
      if(typeof data.connected_peak !== 'undefined'){
        setText('connectedPeakVal', String(data.connected_peak));
        setText('connectedChartPeak', String(data.connected_peak));
      }
      if(Array.isArray(data.connected_series)){
        connectedHist.splice(0, connectedHist.length, ...data.connected_series.map((value) => Number(value)).filter((value) => Number.isFinite(value)));
      }
      if(Array.isArray(data.connected_labels)){
        connectedLabels.splice(0, connectedLabels.length, ...data.connected_labels.map((value) => String(value || '').trim()).filter(Boolean));
      }
      if(typeof data.connected_latest_seen !== 'undefined' && $('connectedLatestVal')){
        $('connectedLatestVal').textContent = data.connected_latest_seen || '—';
      }

      // paint on next frame (no flicker)
      window.requestAnimationFrame(redraw);
    }catch(e){
      // silent: dashboard must remain usable even if metrics fail
    }finally{
      inflight = false;
    }
  }

  // First refresh quickly, then interval
  window.setTimeout(tick, 350);
  window.setInterval(tick, refreshSec * 1000);

  // Optional manual refresh button (if present)
  const btn = $('dashRefreshBtn');
  if(btn){
    btn.addEventListener('click', (e) => { e.preventDefault(); tick(); });
  }

  // Redraw on resize (debounced)
  let resizeT = null;
  const queueRedraw = () => {
    if(resizeT) window.clearTimeout(resizeT);
    resizeT = window.setTimeout(() => window.requestAnimationFrame(redraw), 120);
  };
  window.addEventListener('resize', queueRedraw);

  if(typeof ResizeObserver !== 'undefined'){
    const observer = new ResizeObserver(() => queueRedraw());
    const cpuCanvas = $('cpuChart');
    const loadCanvas = $('loadChart');
    const connectedCanvas = $('connectedUsersChart');
    if(cpuCanvas && cpuCanvas.parentElement) observer.observe(cpuCanvas.parentElement);
    if(loadCanvas && loadCanvas.parentElement) observer.observe(loadCanvas.parentElement);
    if(connectedCanvas && connectedCanvas.parentElement) observer.observe(connectedCanvas.parentElement);
  }
})();
