/* Daily site-only random IDs. No comment identity, URL, referrer or fingerprint collection. */
(() => {
  'use strict';
  if (window.SiteAnalytics) return;
  const allowed = new Set(['startup', 'atlas', 'flat', 'book']);
  const key = 'zmfAnalyticsDay';
  let current = null, memory = null;
  const day = () => new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit'}).format(new Date());
  function uuid() {
    if (!window.crypto?.getRandomValues) return null;
    if (window.crypto.randomUUID) return window.crypto.randomUUID();
    const bytes = new Uint8Array(16); window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const s = Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
    return `${s.slice(0,8)}-${s.slice(8,12)}-${s.slice(12,16)}-${s.slice(16,20)}-${s.slice(20)}`;
  }
  function identity() {
    const date = day();
    try {
      const saved = JSON.parse(localStorage.getItem(key) || 'null');
      if (saved?.day===date && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(saved.id)) memory=saved;
    } catch {}
    if (!memory || memory.day!==date) {
      const id=uuid(); if(!id) return null;
      memory={day:date,id};
      try {localStorage.setItem(key,JSON.stringify(memory));} catch {}
    }
    return memory;
  }
  async function send(page, retry=false) {
    try {
      const visitor=identity(), event=uuid(); if(!visitor || !event) return;
      const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),2000);
      try {
        const response=await fetch('/api/analytics/pageview',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({page,date:visitor.day,visitorId:visitor.id,eventId:event}),
          credentials:'same-origin',keepalive:true,signal:controller.signal});
        // One bounded midnight retry; ordinary failures never retry or block the site.
        if(response.status===400 && !retry && day()!==visitor.day) await send(page,true);
      } finally {clearTimeout(timer);}
    } catch {}
  }
  window.SiteAnalytics=Object.freeze({view(page) {
    if(!allowed.has(page) || current===page) return;
    current=page; void send(page);
  }});
})();
