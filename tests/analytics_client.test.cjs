const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),crypto=require('node:crypto');
const source=fs.readFileSync(require('node:path').join(__dirname,'../public/analytics.js'),'utf8');
function setup(blockStorage=false) {
  let date='2026-10-03', posts=[], storage=new Map();
  const ctx={window:{crypto:crypto.webcrypto},localStorage:{getItem:k=>{if(blockStorage)throw Error();return storage.get(k);},setItem:(k,v)=>{if(blockStorage)throw Error();storage.set(k,v);}},
    Intl:{DateTimeFormat:class {format(){return date;}}},Date,Uint8Array,AbortController,setTimeout,clearTimeout,
    fetch:async(url,options)=>{posts.push({url,body:JSON.parse(options.body)});return {status:202};}};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  return {ctx,posts,storage,nextDay:()=>{date='2026-10-04';}};
}
test('first view then effective SPA transitions count, repeated mode does not',()=>{const a=setup();for(const page of ['startup','startup','book','book','atlas','flat','book'])a.ctx.window.SiteAnalytics.view(page);assert.equal(a.posts.length,5);assert.deepEqual(a.posts.map(p=>p.body.page),['startup','book','atlas','flat','book']);assert.equal(new Set(a.posts.map(p=>p.body.visitorId)).size,1);});
test('duplicate script initialization retains guard and event state',()=>{const a=setup();a.ctx.window.SiteAnalytics.view('startup');vm.runInContext(source,a.ctx);a.ctx.window.SiteAnalytics.view('startup');assert.equal(a.posts.length,1);});
test('daily rotation changes identity without comment token',()=>{const a=setup();a.ctx.window.SiteAnalytics.view('startup');a.nextDay();a.ctx.window.SiteAnalytics.view('book');assert.notEqual(a.posts[0].body.visitorId,a.posts[1].body.visitorId);assert.equal(a.posts[1].body.date,'2026-10-04');assert.deepEqual([...a.storage.keys()],['zmfAnalyticsDay']);});
test('blocked storage keeps one session identity',()=>{const a=setup(true);a.ctx.window.SiteAnalytics.view('startup');a.ctx.window.SiteAnalytics.view('atlas');assert.equal(a.posts[0].body.visitorId,a.posts[1].body.visitorId);});
test('unknown pages rejected locally, raw URL never sent',()=>{const a=setup();for(const p of ['admin','/api','https://example.com?secret=1','static'])a.ctx.window.SiteAnalytics.view(p);assert.equal(a.posts.length,0);a.ctx.window.SiteAnalytics.view('flat');assert.deepEqual(Object.keys(a.posts[0].body).sort(),['date','eventId','page','visitorId']);});
test('network failure remains nonblocking and does not retry indefinitely',async()=>{const a=setup();let attempts=0;a.ctx.fetch=async()=>{attempts++;throw Error('offline');};a.ctx.window.SiteAnalytics.view('startup');await new Promise(r=>setImmediate(r));assert.equal(attempts,1);});
test('no visibility/focus listeners that would inflate PV',()=>{assert.doesNotMatch(source,/addEventListener\(['"](?:focus|visibilitychange)/);});
test('refresh creates a new PV with persistent daily identity',()=>{const a=setup();a.ctx.window.SiteAnalytics.view('startup');const b=setup();b.ctx.localStorage=a.ctx.localStorage;vm.runInContext(source.replace('if (window.SiteAnalytics) return;','delete window.SiteAnalytics;'),b.ctx);b.ctx.window.SiteAnalytics.view('startup');assert.equal(a.posts[0].body.visitorId,b.posts[0].body.visitorId);assert.notEqual(a.posts[0].body.eventId,b.posts[0].body.eventId);});
