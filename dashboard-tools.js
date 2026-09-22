/* Pure view helpers: no market requests, no mutation of frozen source data. */
(function(root){
  'use strict';
  const date8=key=>String(key||'').slice(0,8);
  const sessionOf=key=>String(key).endsWith('pm')?'pm':'am';
  function beijingDay(now=new Date()){
    const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',
      year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(now);
    return ['year','month','day'].map(k=>parts.find(p=>p.type===k).value).join('');
  }
  function freshness(data,session,now=new Date()){
    const keys=[...new Set([...Object.keys(data.holdings||{}),...Object.keys(data.selection||{})])]
      .filter(k=>/^\d{8}(pm)?$/.test(k)&&sessionOf(k)===session).sort();
    const key=keys[keys.length-1]||'', today=beijingDay(now);
    const state=!key?'missing':date8(key)===today?'today':date8(key)<today?'historical':'future';
    return {key,today,state};
  }
  function latestSession(data){
    const keys=[...Object.keys(data.holdings||{}),...Object.keys(data.selection||{})]
      .filter(k=>/^\d{8}(pm)?$/.test(k)).sort();
    return keys.length?sessionOf(keys[keys.length-1]):'am';
  }
  function batches(data){
    return [...new Set([...Object.keys(data.holdings||{}),...Object.keys(data.selection||{})])]
      .filter(k=>/^\d{8}(pm)?$/.test(k)).sort().reverse();
  }
  function batchLabel(key){
    return `${String(key).slice(0,4)}-${String(key).slice(4,6)}-${String(key).slice(6,8)} · ${sessionOf(key)==='pm'?'午盘':'早盘'}`;
  }
  function fillBreakdown(model){
    if(!model?.picks)return {available:false,filled:0,blocked:0,unknown:0,vacant:0};
    const f=model.fills||{}, filled=f.filled||0;
    const blocked=(f.ran||0)+(f.one_line||0)+(f.open_locked||0);
    const slots=model.slots||0;
    return {available:true,filled,blocked,unknown:Math.max(0,slots-filled-blocked),vacant:Math.max(0,5-slots)};
  }
  function stockIndex(data,cohort){
    const stocks=new Map();
    const names=new Map((data.models||[]).map(m=>[m.key,m.name]));
    names.set('quality','质量精选');names.set('ensemble','模型共识');names.set('hot100','热榜基准');
    for(const [model,rows] of Object.entries(data.holdings?.[cohort]||{})){
      for(const row of rows){
        const code=String(row.code||'');
        if(!/^\d{6}$/.test(code))continue;
        if(!stocks.has(code))stocks.set(code,{code,name:row.name||code,sources:[],focusRank:null,qualityScore:null,sector:''});
        const stock=stocks.get(code);
        if(stock.sources.some(s=>s.key===model))continue;
        stock.sources.push({key:model,name:names.get(model)||model,rank:row.rank,
          slot:row.slot||0,fill:row.fill||'pending'});
        if(model==='quality'){
          stock.focusRank=row.selection?.focus_rank||null;
          stock.qualityScore=row.pick_score??null;
          stock.sector=row.selection?.sector||'';
        }
      }
    }
    return [...stocks.values()].sort((a,b)=>{
      if(!!a.focusRank!==!!b.focusRank)return a.focusRank?-1:1;
      if(a.focusRank&&b.focusRank)return a.focusRank-b.focusRank;
      const count=s=>s.sources.filter(x=>!['ensemble','quality','hot100'].includes(x.key)).length;
      return count(b)-count(a)||a.code.localeCompare(b.code);
    });
  }
  function filterStocks(stocks,query='',source='all'){
    const q=query.trim().toLocaleLowerCase();
    return stocks.filter(s=>(!q||s.code.includes(q)||s.name.toLocaleLowerCase().includes(q))
      &&(source==='all'||(source==='focus'?!!s.focusRank:s.sources.some(x=>x.key===source))));
  }
  function csvCell(value){
    let s=String(value??'');
    // Quote fields and neutralize spreadsheet formulas, including leading whitespace.
    if(/^[\s]*[=+\-@]/.test(s)||/^[\t\r\n]/.test(s))s="'"+s;
    return '"'+s.replace(/"/g,'""')+'"';
  }
  function stocksCSV(stocks,cohort){
    const rows=[['批次','代码（文本）','名称','重点名次','质量规则分','行业','推荐来源及原始名次']];
    for(const s of stocks)rows.push([cohort,"'"+s.code,s.name,s.focusRank,s.qualityScore,s.sector,
      s.sources.map(x=>`${x.name} #${x.rank}`).join('；')]);
    return '\ufeff'+rows.map(r=>r.map(csvCell).join(',')).join('\r\n');
  }
  function holdingReturn(row,basis,horizon,cost=0){
    if(basis==='executed'){
      if(!(row.slot>0&&row.slot<=5))return {status:'outside'};
      if(['ran','one_line','open_locked'].includes(row.fill))return {status:'cash',value:0};
      if(row.fill!=='filled')return {status:'unverified'};
    }
    const b=basis==='executed'?'open':basis;
    const path=row.paths?.[b]?.[horizon];
    if(!path)return {status:'pending'};
    if(!row.verified?.[b]?.[horizon])return {status:'uncorrected'};
    return {status:'ready',value:path[0]-cost,mfe:path[1],mae:path[2]};
  }
  // 真实收益主表：各实体Top5槽位，只按确认成交计算。
  // 等权5槽资金：filled→真实收益；ran/one_line/open_locked→现金0(没买到)；
  // 未核验/未到日不猜值，分别计 unknown/pending；总值按5槽资金口径=Σ/5。
  function realReturns(data,cohort,cost=0){
    const holdings=data.holdings?.[cohort]||{};
    const order=[...(data.models||[]).filter(m=>!m.virtual).map(m=>m.key)];
    if(!order.includes('quality')&&Array.isArray(holdings.quality))order.push('quality');
    const names=new Map((data.models||[]).map(m=>[m.key,m.name]));
    names.set('quality','质量精选 · 独立策略');
    const colors=new Map((data.models||[]).map(m=>[m.key,m.color]));
    const cols=['oc','d1','d3','d5'];
    return order.map(key=>{
      const rows=holdings[key]||[];
      const bySlot={};
      rows.forEach(r=>{if(0<r.slot&&r.slot<=5&&!(r.slot in bySlot))bySlot[r.slot]=r;});
      const slots=[];
      for(let s=1;s<=5;s++){
        const r=bySlot[s];
        if(!r){slots.push({slot:s,name:'—',oc:'pending',d1:'pending',d3:'pending',d5:'pending'});continue;}
        const c={slot:s,name:r.name||r.code,code:r.code};
        const isCash=['ran','one_line','open_locked'].includes(r.fill);
        c.oc=r.fill==='filled'?(r.oc??'unknown'):isCash?0:'unknown';
        for(const h of [1,3,5]){
          const z=holdingReturn(r,'executed',String(h),cost);
          c['d'+h]=z.status==='ready'?z.value:z.status==='cash'?0
            :z.status==='pending'?'pending':'unknown';
        }
        slots.push(c);
      }
      const metrics={};
      for(const col of cols){
        let sum=0,unknown=0,pending=0;
        slots.forEach(c=>{const v=c[col];
          if(v==='pending')pending++;else if(v==='unknown')unknown++;else sum+=v;});
        metrics[col]={value:+(sum/5*100).toFixed(2),unknown,pending};
      }
      return {key,name:names.get(key)||key,color:colors.get(key)||'',
        filled:slots.filter(c=>bySlot[c.slot]?.fill==='filled').length,
        cash:slots.filter(c=>['ran','one_line','open_locked'].includes(bySlot[c.slot]?.fill)).length,
        slots,metrics};
    });
  }
  const api={date8,sessionOf,beijingDay,freshness,latestSession,batches,batchLabel,fillBreakdown,stockIndex,filterStocks,stocksCSV,holdingReturn,realReturns};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else root.DashboardTools=api;
})(typeof globalThis!=='undefined'?globalThis:this);
