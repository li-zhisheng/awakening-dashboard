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
      if(['ran','one_line'].includes(row.fill))return {status:'cash',value:0};
      if(row.fill!=='filled')return {status:'unverified'};
    }
    const b=basis==='executed'?'open':basis;
    const path=row.paths?.[b]?.[horizon];
    if(!path)return {status:'pending'};
    if(!row.verified?.[b]?.[horizon])return {status:'uncorrected'};
    return {status:'ready',value:path[0]-cost,mfe:path[1],mae:path[2]};
  }
  const api={date8,sessionOf,beijingDay,freshness,latestSession,stockIndex,filterStocks,stocksCSV,holdingReturn};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else root.DashboardTools=api;
})(typeof globalThis!=='undefined'?globalThis:this);
