(() => {
  'use strict';
  const $=id=>document.getElementById(id), fmt=n=>new Intl.NumberFormat('zh-CN').format(n);
  let days=7, metric='pv', page=1, total=0, data=null, generation=0;
  class AuthExpired extends Error {}
  async function api(url, options={}) {
    const response=await fetch(url,{cache:'no-store',credentials:'same-origin',...options});
    let body;
    try {body=await response.json();} catch {throw new Error('服务器暂时无法返回统计数据，请稍后重试。');}
    if(response.status===401) {showLogin('管理会话已过期，请重新登录。');throw new AuthExpired();}
    if(!response.ok) throw new Error(body.error||'读取失败，请稍后重试。');
    return body;
  }
  function showLogin(message='') {$('panel').hidden=true;$('login').hidden=false;$('login-error').textContent=message;generation++;}
  function notice(message) {$('notice').textContent=message;$('notice').hidden=!message;}
  function message(error) {return error instanceof TypeError ? '网络暂时不可用，请稍后重试。' : error.message||'读取失败，请稍后重试。';}
  function node(tag,text,cls) {const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
  function distribution(id, rows, labels={}) {
    const parent=$(id);parent.replaceChildren();
    const sum=rows.reduce((n,r)=>n+r.count,0);
    if(!sum){parent.append(node('div','这个时间段还没有访问数据。','empty'));return;}
    rows.forEach(r=>{const wrap=node('div'), label=node('div',undefined,'rank-label');
      label.append(node('span',labels[r.label]||r.label),node('small',`${fmt(r.count)} · ${(100*r.count/sum).toFixed(1)}%`));
      const bar=node('div',undefined,'bar'),fill=node('span');fill.style.width=`${100*r.count/sum}%`;bar.append(fill);wrap.append(label,bar);parent.append(wrap);});
  }
  function chart() {
    const parent=$('chart');parent.replaceChildren();$('chart-note').textContent='悬停或聚焦圆点查看当天数据';
    const rows=data.trend;
    if(!rows.some(r=>r[metric])) {parent.append(node('div','这段时间还没有访问记录。有人来访后，趋势会出现在这里。','empty'));return;}
    const ns='http://www.w3.org/2000/svg', svg=document.createElementNS(ns,'svg');
    svg.setAttribute('viewBox','0 0 1000 250');svg.setAttribute('role','img');svg.setAttribute('aria-label',`最近 ${days} 天 ${metric.toUpperCase()} 访问趋势`);
    const add=(tag,attrs,text)=>{const el=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,String(v)));if(text!==undefined)el.textContent=text;svg.append(el);return el;};
    const max=Math.max(1,...rows.map(r=>r[metric])), x=i=>55+i*920/(rows.length-1), y=n=>205-170*n/max;
    for(let i=0;i<=4;i++){const yy=205-i*42.5;add('line',{x1:55,x2:975,y1:yy,y2:yy,stroke:'#e6ded5','stroke-dasharray':'3 6'});add('text',{x:40,y:yy+4,'text-anchor':'end',fill:'#817a73','font-size':12},String(Math.round(max*i/4)));}
    add('polyline',{points:rows.map((r,i)=>`${x(i)},${y(r[metric])}`).join(' '),fill:'none',stroke:'#99515d','stroke-width':2.5,'stroke-linejoin':'round'});
    rows.forEach((r,i)=>{const label=`${r.day} · ${metric.toUpperCase()} ${fmt(r[metric])}`;
      const point=add('circle',{cx:x(i),cy:y(r[metric]),r:4.5,fill:'#fffcf8',stroke:'#99515d','stroke-width':2,tabindex:0,'aria-label':label});
      const title=document.createElementNS(ns,'title');title.textContent=label;point.append(title);
      point.addEventListener('mouseenter',()=>{$('chart-note').textContent=label;});point.addEventListener('focus',()=>{$('chart-note').textContent=label;});
      if(i===0||i===rows.length-1||i===Math.floor(rows.length/2))add('text',{x:x(i),y:238,'text-anchor':'middle',fill:'#817a73','font-size':12},r.day.slice(5));});
    parent.append(svg);
  }
  function renderSummary() {
    const c=data.cards;
    for(const [id,value] of [['today-pv',c.todayPv],['today-uv',c.todayUv],['last7-pv',c.last7Pv],['today-errors',c.todayErrors]])$(id).textContent=fmt(value);
    $('period').textContent=`${data.today} · 北京时间（Asia/Shanghai）`;
    chart();distribution('page-rank',data.dimensions.page,data.pages);distribution('device-rank',data.dimensions.device);
    distribution('browser-rank',data.dimensions.browser);distribution('os-rank',data.dimensions.os);
    $('errors').replaceChildren();
    if(!data.errors.length)$('errors').append(node('p','这个时间段没有记录到应用异常。','muted'));
    else data.errors.forEach(r=>{const e=node('div',`${r.day} · HTTP ${r.status}`,'error-pill');e.append(node('strong',fmt(r.count)));$('errors').append(e);});
  }
  function renderVisits(result) {
    total=result.total;const pages=Math.max(1,Math.ceil(total/20));
    $('visits-body').replaceChildren();$('visits-empty').hidden=!!total;$('visits-table').hidden=!total;
    result.items.forEach(r=>{const tr=node('tr'),time=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(r.at));
      for(const [i,text] of [time,data.pages[r.page]||r.page,r.visitor,r.device,r.browser,r.os].entries()){const td=node('td');td.append(node(i===2?'code':'span',text));tr.append(td);}$('visits-body').append(tr);});
    $('page-number').textContent=`${page} / ${pages} · ${fmt(total)} 条`;$('previous').disabled=page<=1;$('next').disabled=page>=pages;
  }
  async function load(reset=true) {
    const ticket=++generation;if(reset)page=1;
    $('login').hidden=true;$('panel').hidden=false;$('loading').hidden=false;$('content').hidden=true;notice('');$('refresh').disabled=true;
    try {const [summary,visits]=await Promise.all([api(`/api/manage/analytics/summary?days=${days}`),api(`/api/manage/analytics/visits?page=${page}`)]);
      if(ticket!==generation)return;data=summary;renderSummary();renderVisits(visits);$('content').hidden=false;
    } catch(error) {if(!(error instanceof AuthExpired)&&ticket===generation)notice(message(error));}
    finally {if(ticket===generation){$('loading').hidden=true;$('refresh').disabled=false;}}
  }
  async function visitsPage(next) {
    const ticket=++generation, old=page;page=next;$('previous').disabled=true;$('next').disabled=true;
    try {const result=await api(`/api/manage/analytics/visits?page=${page}`);if(ticket===generation){renderVisits(result);notice('');}}
    catch(error){page=old;if(!(error instanceof AuthExpired))notice(message(error));}
    finally {if(ticket===generation){$('previous').disabled=page<=1;$('next').disabled=page>=Math.ceil(total/20);}}
  }
  document.querySelectorAll('[data-days]').forEach(button=>button.addEventListener('click',()=>{days=Number(button.dataset.days);document.querySelectorAll('[data-days]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));void load();}));
  document.querySelectorAll('[data-metric]').forEach(button=>button.addEventListener('click',()=>{metric=button.dataset.metric;document.querySelectorAll('[data-metric]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));if(data)chart();}));
  $('refresh').addEventListener('click',()=>void load());$('previous').addEventListener('click',()=>void visitsPage(page-1));$('next').addEventListener('click',()=>void visitsPage(page+1));
  $('login').addEventListener('submit',async event=>{event.preventDefault();const button=$('login').querySelector('button');button.disabled=true;
    try {await api('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('password').value})});$('password').value='';void load();}
    catch(error){$('login-error').textContent=error.message||'登录失败，请重试。';}finally{button.disabled=false;}});
  $('logout').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST'});showLogin();}catch(error){if(!(error instanceof AuthExpired))notice('退出失败，请重试。');}});
  void load();
})();
