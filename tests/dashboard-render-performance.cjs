const fs = require('fs');
const path = require('path');
const assert = require('assert');
const {chromium} = require('C:/Users/Deleg/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:900}});
    const assets = path.join(__dirname, '../windows-controller/dashboard');
    await page.route('http://noode.test/**', route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.startsWith('/api/')) return route.fulfill({json:{}});
      const file = pathname === '/' ? 'index.html' : pathname.slice(1);
      return route.fulfill({body:fs.readFileSync(path.join(assets,file)),contentType:file.endsWith('.css')?'text/css':file.endsWith('.js')?'text/javascript':'text/html'});
    });
    await page.goto('http://noode.test/');
    const result = await page.evaluate(() => {
      liveNodes = Array.from({length:1000}, (_,i)=>({ip:`192.0.${Math.floor(i/256)}.${i%256}`,port:443,country:'US',tcp_latency_ms:i+1,speed_mbps:5,_rank:i+1}));
      renderLiveNodes();
      const row = elements.liveNodeRows.firstElementChild;
      const observer = new MutationObserver(()=>{});
      observer.observe(elements.liveNodeRows,{childList:true});
      const start = performance.now();
      for(let i=0;i<20;i++) renderLiveNodes();
      const unchanged = {milliseconds:performance.now()-start,mutations:observer.takeRecords().length,sameRow:row===elements.liveNodeRows.firstElementChild};
      liveNodes[0].speed_mbps = 9;
      renderLiveNodes();
      const changed = observer.takeRecords().length;
      observer.disconnect();
      return {unchanged,changed,rows:elements.liveNodeRows.children.length};
    });
    console.log(JSON.stringify(result));
    assert.equal(result.unchanged.mutations,0,'unchanged polling rebuilt the table');
    assert.equal(result.unchanged.sameRow,true);
    assert.ok(result.changed>0,'new measurements must render');
    assert.equal(result.rows,1000);
    const updates = await page.evaluate(() => {
      const body = document.createElement('tbody');
      setTableMarkup(body, '<tr><td>A</td></tr><tr><td>B</td></tr>');
      const unchanged = body.children[1];
      setTableMarkup(body, '<tr><td>C</td></tr><tr><td>B</td></tr><tr><td>D</td></tr>');
      const retained = body.children[1] === unchanged;
      setTableMarkup(body, '<tr><td>D</td></tr><tr><td>C</td></tr>');
      const sorted = body.textContent;
      setTableMarkup(body, '');
      const empty = body.children.length === 0;
      const card = document.createElement('div');
      card.className = 'stage-card done';
      card.id = 'offscreen-test';
      card.style.cssText = 'position:absolute;top:20000px;width:100px;height:100px';
      document.body.appendChild(card);
      addCardAtmosphere(card);
      return {retained,sorted,empty};
    });
    assert.deepEqual(updates,{retained:true,sorted:'DC',empty:true});
    await page.waitForFunction(() => document.querySelector('#offscreen-test').classList.contains('motion-offscreen'));
    assert.equal(await page.$eval('#offscreen-test i', el=>getComputedStyle(el).animationPlayState),'paused');
    console.log('incremental updates, reorder, removal, offscreen animation: OK');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
