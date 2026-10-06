/* Read-only paper-account panel. Real trading has no entry point here. */
(() => {
  const panel = document.createElement('section');
  panel.id = 'trend-paper';
  panel.className = 'realreturns';
  panel.innerHTML = `<h2>强弱多空 · 实时模拟账户</h2>
    <p id="paper-status" role="status">正在读取模拟账户…</p>
    <p id="paper-method" style="color:var(--sub);font-size:12px"></p>
    <div id="paper-summary" style="display:flex;gap:24px;flex-wrap:wrap;margin:16px 0"></div>
    <details><summary>逐股模拟账户与成交明细</summary>
      <input id="paper-search" type="search" placeholder="股票代码或名称" aria-label="查找模拟账户" style="margin:12px 0;padding:8px">
      <div style="overflow:auto;max-height:420px"><table style="width:100%;white-space:nowrap">
        <thead><tr><th>股票</th><th>持股</th><th>已实现盈亏</th><th>浮动盈亏</th><th>最大浮盈</th><th>最大浮亏</th><th>状态</th><th>报价时间</th></tr></thead>
        <tbody id="paper-accounts"></tbody></table></div>
      <h3>最近模拟成交</h3><div id="paper-fills" style="overflow:auto"></div>
      <h3>最近信号与未成交原因</h3><div id="paper-signals" style="overflow:auto"></div>
    </details>`;
  const anchor = document.getElementById('realreturns');
  if (!anchor) return;
  anchor.before(panel);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {
    cash:'现金', holding_t1:'持仓 / T+1', t1_locked:'T+1待卖', pending_sell:'等待卖出',
    corporate_action_review:'除权待核对', stale_quote:'报价过期', holding:'持仓',
    await_next_quote:'等待下一笔报价', no_executable_book:'无可成交盘口',
    insufficient_cash_or_depth:'资金或深度不足', limit_up:'涨停未成交',
    limit_down:'跌停未成交', outside_price_limit:'价格越界',
    simulated_next_quote:'下一笔报价模拟成交', flat:'空仓',
    already_holding:'已持仓', st_or_non_mainboard:'ST或非主板',
    one_entry_per_day:'当日已买入', signal_disappeared:'信号已消失',
  };
  const label = value => labels[value] || value;
  const currency = value => `<span style="color:${value > 0 ? '#f87171' : value < 0 ? '#34d399' : 'var(--sub)'}">¥${Number(value || 0).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>`;
  let data = null;
  const render = () => {
    if (!data) return;
    const query = document.getElementById('paper-search').value.trim().toLowerCase();
    const rows = (data.stocks || []).filter(s => !query || s.code.includes(query) ||
      s.name.toLowerCase().includes(query)).sort((a,b) => b.quantity-a.quantity || a.code.localeCompare(b.code));
    document.getElementById('paper-accounts').innerHTML = rows.slice(0,100).map(s =>
      `<tr><td>${escape(s.name)} ${escape(s.code)}</td><td>${s.quantity}</td>
      <td>${currency(s.realized_pnl)}</td><td>${currency(s.unrealized_pnl)}</td>
      <td>${currency(s.max_floating_profit)}</td><td>${currency(s.max_floating_loss)}</td>
      <td>${escape(label(s.status))}</td><td>${escape(s.mark_at || '尚无报价')}</td></tr>`).join('');
  };
  document.getElementById('paper-search').addEventListener('input', render);
  const refresh = async () => {
    try {
      const response = await fetch('trend-paper.json', {cache:'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      data = await response.json();
      if (data.schema !== 'awakening-trend-paper-v1') throw new Error('模拟账户尚未初始化');
      const s = data.summary, health = data.health;
      const live = location.hostname === '127.0.0.1' || location.hostname === 'localhost';
      const error = health?.error || data.last_error?.message;
      document.getElementById('paper-status').textContent = error ? `运行异常：${error}` :
        `${live ? '本地模拟' : '已发布快照'} · ${s.account_count}只非ST主板 · ` +
        (data.last_processed_at ? `账户更新 ${data.last_processed_at}` :
          `尚未产生模拟成交${health?.next_trading_day ? ' · 下次开市 '+health.next_trading_day : ''}`) +
        ` · ${live && health?.updated_at ? '心跳 '+health.updated_at : '快照 '+data.generated_at}` +
        (live && health?.updated_at && Date.now()-Date.parse(health.updated_at)>180000 ? ' · 扫描心跳过期' : '');
      document.getElementById('paper-method').textContent = data.methodology;
      document.getElementById('paper-summary').innerHTML = [
        `已实现 ${currency(s.realized_pnl)}`, `浮动 ${currency(s.unrealized_pnl)}`,
        `合计 ${currency(s.net_pnl)}`, `持仓 ${s.holding_count}只`,
        `待卖 ${s.pending_exit_count}只`, `模拟成交 ${s.fill_count}笔`,
      ].map(text => `<div>${text}</div>`).join('');
      document.getElementById('paper-fills').innerHTML = (data.recent_fills || []).map(f =>
        `<p>${escape(f.quote_at)} · ${escape(f.name)} ${escape(f.code)} · `+
        `${f.strength==='strong'?'强':'弱'}${f.side==='buy'?'多买入':'空卖出'} ${f.quantity}股 × ¥${f.price} · `+
        `费用 ¥${f.fee}${f.pnl==null?'': ' · 已实现 '+currency(f.pnl)}</p>`).join('') || '暂无模拟成交';
      document.getElementById('paper-signals').innerHTML = (data.recent_signals || []).map(s =>
        `<p>${escape(s.observed_at)} · ${escape(s.code)} · ${s.strength==='strong'?'强':'弱'}${s.direction==='LONG'?'多':'空'} · ${escape(label(s.reason))}</p>`).join('') || '等待交易时段信号';
      render();
    } catch (error) {
      document.getElementById('paper-status').textContent = `模拟账户暂不可用：${error.message}`;
    }
  };
  refresh();
  setInterval(refresh, 30000);
})();
