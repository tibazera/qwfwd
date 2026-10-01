// Run: node tests/test_site_udp.cjs
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(require('node:path').join(__dirname, '../.gh-pages-worktree/index.html'), 'utf8');
for (const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new Function(script[1]);
assert(!/registerPanel|handleRegisterClick|player-register/.test(html));
assert(!/haversineKm|estimatePlayerLegMs|RTCPeerConnection|nearby/i.test(html));
assert(html.includes('navigator.geolocation?.getCurrentPosition'));
assert(html.includes("player: 'Você'"));
assert(html.includes('const pts = [player, ...route.path_geo'));
assert(html.includes("i === route.path.length - 1 ? tr('server')"));
assert(html.includes("<b>${tr('command')}:</b>"));
assert(html.includes(': `connect ${connectAddress}`'));
assert(html.includes('qualityRankMs: playerLegMs +'));
assert.equal((html.match(/addEventListener\('click', event => \{/g) || []).length, 1);
assert(html.includes("closest('[data-udp-destination]')"));
assert(!html.includes("querySelectorAll('.proxy-choice').forEach"));
const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
async function scenario(samples, apiFails = false, helperMissing = false, meshPing = 20) {
  const calls = [];
  const context = vm.createContext({
    document:{getElementById(){return null;}}, setTimeout, clearTimeout, navigator: {}, language: 'pt',
    liveData: { proxies: { '1.1.1.1:1': {city:'A'}, '2.2.2.2:2': {city:'B'} } },
    extensionRequest: async () => helperMissing ? ({error:'helper_unavailable'}) : ({ok:true}), udpMode: { active:true }, clearCityGroup(){}, renderUdpState(){},
    resolveServerAddress: async () => '3.3.3.3:3',
    measureViaLocalApp: async addr => samples[addr] ?? null,
    confirmedGeo: () => null, COLLECTOR_BASE: 'http://test', AbortSignal,
    fetch: async url => {
      calls.push(url);
      if (apiFails) throw Error('offline');
      return {ok:true, json:async()=>({routes:[
        {known:true,entry:'1.1.1.1:1',total_ping_ms:meshPing,quality_cost_ms:meshPing+100,path:['1.1.1.1:1','3.3.3.3:3']},
        {known:true,entry:'2.2.2.2:2',total_ping_ms:1,path:['2.2.2.2:2','3.3.3.3:3']}
      ]})};
    }
  });
  vm.runInContext(extract('function udpEntryCandidates()', 'function renderUdpState()') + extract('function locateUdpPlayer(', 'function renderUdpResults()'), context);
  await vm.runInContext("chooseUdpDestination('game.example:3')", context);
  return {state:context.udpMode,calls};
}
(async()=>{
  let success, failure, watchdog;
  const locationContext = vm.createContext({document:{getElementById(){return null;}},setTimeout(fn){watchdog=fn;return 1;},clearTimeout(){},navigator:{geolocation:{getCurrentPosition(ok,fail){success=ok;failure=fail;}}},udpMode:{phase:'results'},renderUdpResults(){}});
  vm.runInContext(extract('function locateUdpPlayer(', 'async function chooseUdpDestination('),locationContext);
  vm.runInContext('locateUdpPlayer()',locationContext);
  failure({code:1}); assert.equal(locationContext.udpMode.locationError,1);
  vm.runInContext('locateUdpPlayer()',locationContext);
  success({coords:{latitude:-23.3,longitude:-51.1}}); assert.equal(locationContext.udpMode.location.lat,-23.3); assert.equal(locationContext.udpMode.locationPending,false);

  vm.runInContext('locateUdpPlayer()',locationContext);
  watchdog(); assert.equal(locationContext.udpMode.locationPending,false); assert.equal(locationContext.udpMode.locationError,3);
  success({coords:{latitude:1,longitude:1}}); assert.equal(locationContext.udpMode.location.lat,-23.3);

  const missing = await scenario({},false,true);
  assert.equal(missing.state.helperError,'helper_unavailable');
  assert.equal(missing.calls.length,0);
  let result = await scenario({});
  assert.equal(result.state.routes.length,0); assert.equal(result.calls.length,0);
  result = await scenario({'1.1.1.1:1':10,'3.3.3.3:3':50});
  assert.equal(result.state.routes.length,2);
  assert.equal(result.state.routes[0].totalMeasuredLegsMs,30);
  assert(!result.calls[0].includes('game.example'));
  assert(!result.state.routes.some(r=>r.entry==='2.2.2.2:2'));
  result = await scenario({'1.1.1.1:1':56},false,false,112);
  assert.equal(result.state.routes[0].totalMeasuredLegsMs,168);
  result = await scenario({'1.1.1.1:1':10,'2.2.2.2:2':40,'3.3.3.3:3':50});
  assert.deepEqual(Array.from(result.state.routes,r=>r.totalMeasuredLegsMs),[30,41,50]);
  result = await scenario({'1.1.1.1:1':10,'3.3.3.3:3':30});
  assert.equal(result.state.routes[0].path.length,1);
  result = await scenario({'1.1.1.1:1':10,'3.3.3.3:3':50},true);
  assert.equal(result.state.routes.length,1); assert(result.state.collectorFailed);
  console.log('PASS: syntax, no geographic fallback, unmeasured proxy excluded, DNS target, direct route survives API failure');
})().catch(error=>{console.error(error);process.exitCode=1;});
