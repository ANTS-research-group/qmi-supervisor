// Development-only browser acceptance test. Node >=22 and Chrome; no packages.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {setTimeout: delay} = require('node:timers/promises');

const root = path.resolve(__dirname, '../..');
// BROWSER_QUICK=1 covers the same behaviours on two viewports, one locale and
// one theme, without screenshots: use it while iterating, full mode before a
// release.
const quick = process.env.BROWSER_QUICK === '1';
const defaults = {
  modem: {device:'auto', serial:'', apn:'internet', plmn:'99910', mode:'connect'},
  network: {method:'qmi', route_metric:700, default_route:true, dns:'auto', route_policy:'fixed',routes:'',
    connectivity_check:true,connectivity_targets:'1.1.1.1:443,8.8.8.8:53',connectivity_interval:3,
    connectivity_timeout:1,connectivity_failures:2,connectivity_successes:2},
  recovery: {retry_initial:2, retry_max:60, registration_timeout:180, operation_timeout:20, poll_interval:10},
  telemetry: {enabled:true, interval:1, directory:'/tmp/metrics', csv:true, json:true,
    udp_host:'', udp_port:5006, collector_host:'', collector_port:5007, throughput:false, gps:false, gps_source:'gpsd',
    queue_size:64, flush_interval:1, gps_max_age:10, request_timeout:1, collection_budget:.8, profile:'full'},
  web: {enabled:true, host:'127.0.0.1', port:8080, history_size:120, max_workers:8, address_refresh:10, poll_interval:2,
    username:'admin',session_timeout:28800},
  system: {log_level:'INFO'},
  update: {mode:'notify', channel:'stable', window_start:'03:00', window_end:'05:00', check_interval_hours:24,
    require_signature:true, auto_rollback:true, allow_downgrade:true}
};

function fixture() {
  const now=Date.now(), iso=new Date(now).toISOString();
  return {state:'ONLINE', updated_at:iso, heartbeat_at_utc:iso,
    device:{path:'/dev/cdc-wdm1', interface:'wwan0', product:'Test modem', serial:'test'},
    log_label:'test',
    network:{address:'192.168.3.2/30', gateway:'192.168.3.1', route_metric:700},
    skipped_intervals:0,
    sample:{log_id:10, end_datetime:iso, static:{dms_value_imei:'test-identity',dms_value_imsi:'001010123456789'},
      timing:{run_id:'test-run', acquisition_duration_ms:320, schedule_lag_ms:.3, effective_interval_ms:1000},
      dynamic:{nas_value_5g_signal_strength_rsrp:-87, nas_value_5g_signal_strength_extended:-11,
        nas_value_5g_signal_strength_snr:17, nas_value_serving_system_registration_state:'registered',
        nas_value_serving_system_radio_interfaces:['5gnr'], nas_value_nr5g_arfcn:629952,
        nas_value_nr5g_cell_information_physical_cell_id:4, nas_value_nr5g_cell_information_plmn:'99910',
        wds_value_connection_status:'connected', wds_value_tx_bytes_ok:1234},
      collection:{requests:{'nas.get_signal_info':{status:'ok', duration_ms:100}}, units:{},
        connection:{requested_apn:'internet',reported_apn:'internet',apn_verification:'matched',reported_apn_source:'current_settings',attach:{status:'matched',profile_id:1,profile_apn:'internet',needs_reregister:false}}}},
    history:Array.from({length:20}, (_,i)=>({sample_id:i,run_id:'test-run',time:new Date(now-(19-i)*1000).toISOString(),
      rsrp:-87+i%3,rsrp_rat:'NR',rsrq:-11,rsrq_rat:'NR',snr:17,snr_rat:'NR',rat:'NR',
      acquisition_duration_ms:320, schedule_lag_ms:.3,lag_ms:.3,effective_interval_ms:1000,skipped_intervals:0})),
    radio:{rats:['nr'],cells:[{rat:'nr',plmn:'99910',pci:4,tac:101,cell_id:772,channel:629952,
      channel_type:'NR-ARFCN',frequency_mhz:3449.28,frequency_label:'NR reference frequency (calculated)'}],
      bands:[{rat:'nr',band:'n78',raw:269,label:'n78',channel:629952}],bandwidths:[{rat:'nr',raw:9,bandwidth_mhz:20}],neighbors:[]},
    telemetry:{acquired:10,persisted:10,pending:0,queued:0,queue_capacity:64,queue_age_ms:0,
      persistence_dropped:0,persistence_failed:0,acquisition_p95_ms:325,schedule_lag_p95_ms:1,
      write_p95_ms:2,flush_p95_ms:.5,last_write_sample_id:10,last_write_success:true,
      last_write_duration_ms:2,last_write_latency_ms:3,writer_alive:true},
    pipeline_history:[{time:iso,run_id:'test-run',pending:0,queue_age_ms:0,write_p95_ms:2,last_write_duration_ms:2}],
    resources:{sampled_at_utc:iso,interval_seconds:5,
      supervisor:{state:'ok',pid:111,cpu_percent:1.2,rss_bytes:30*1024*1024},
      web:{state:'ok',pid:222,cpu_percent:.3,rss_bytes:20*1024*1024}}
  };
}

test('Next export: login, scoped configuration drafts, dependent fields, inner tabs and responsive layout', {timeout:240000}, async () => {
  const out=path.join(root,'frontend','out');
  assert.ok(fs.existsSync(path.join(out,'dashboard','index.html')),'Run npm run build in frontend first');
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'qmi-next-browser-'));
  let chrome,ws,server,startupError,pendingStatus=null,delayStatus=false;
  const errors=[],posts=[],sessions=new Map();
  const password='Browser test passphrase 2026';
  let config=structuredClone(defaults),status=fixture(),requests=0,configRequests=0,sessionSequence=0,sessionFailures=1,sessionRequests=0,bearerRequests=0;
  let passwordEnabled=true;
  let fleetMode=false,secondDetected=true,fleetDiagnosis=null;
  const idleTools={running:false,kind:null,detail:{},lines:[],returncode:null,started:null,finished:null};let toolsStatus=idleTools;
  let delayedConfigProfile=null,pendingConfig=null;
  const secondConfig=structuredClone(defaults),postScopes=[];
  secondConfig.modem.apn='tenten';
  let freshness={heartbeat_age_ms:100,snapshot_age_ms:100,sample_age_ms:100,state_fresh:true};
  const updateRelease=(version,date,relation,extra={})=>({version,tag:'v'+version.replace(/-1$/,''),published_at_utc:date,prerelease:false,
    notes:'Changes in '+version+':\n- first\n- second',notes_truncated:false,relation,installable:relation!=='installed',reason:relation==='installed'?'installed':null,...extra});
  const updateBase=()=>({installed:{version:'1.2.0-1',commit:'abc1234',built_utc:'2026-11-19T16:47:00Z'},settings:structuredClone(config.update),
    check:{state:'ok',checked_at_utc:'2026-11-20T09:00:00Z',error_code:null,error:null,running:false,retry_after:0},
    releases:[updateRelease('1.2.0-1','2026-11-05T08:00:00Z','installed'),updateRelease('1.1.0-1','2026-10-26T08:00:00Z','older')],
    install:null,history:[],rollback:{available:false,version:null,running:false},state_error:null,units_error:null});
  let update=null,updateDown=false,updateGetFailures=0,updateGets=0,update503Count=0;const updatePosts=[];
  const updateAccept=(path,body)=>path!=='/api/update/check'&&body.password!==password?{status:403,body:{error:'Incorrect password.',code:'INVALID_PASSWORD'}}:{status:202,body:{started:true}};
  let updateReply=updateAccept;
  try {
    server=http.createServer(async(req,res)=>{
      const url=new URL(req.url,'http://localhost');
      if(!url.pathname.startsWith('/api/')){
        let filename=path.join(out,url.pathname);
        try{if(fs.statSync(filename).isDirectory())filename=path.join(filename,'index.html');}catch{}
        if(!filename.startsWith(out+path.sep)||!fs.existsSync(filename)||!fs.statSync(filename).isFile()){res.statusCode=404;return res.end('Not found');}
        const extension=path.extname(filename);
        res.setHeader('Content-Type',({'.js':'application/javascript','.css':'text/css','.html':'text/html','.txt':'text/plain','.svg':'image/svg+xml','.json':'application/json'})[extension]||'application/octet-stream');
        return res.end(fs.readFileSync(filename));
      }
      // During a restart the whole API disappears, /api/session included, until the new version answers.
      if(updateDown){req.socket.destroy();return;}
      res.setHeader('Content-Type','application/json');res.setHeader('Cache-Control','no-store');
      if(req.headers.authorization)bearerRequests++;
      const sid=(req.headers.cookie||'').match(/(?:^|;\s*)qmi_session=([^;]+)/)?.[1],session=sessions.get(sid);
      const info=()=>({authenticated:!!session,username:session?'admin':null,csrf_token:session?.csrf||null,expires_in:session?28800:null,methods:{password:passwordEnabled},readonly:false});
      if(url.pathname==='/api/session'){sessionRequests++;if(sessionFailures-->0){res.statusCode=503;return res.end(JSON.stringify({error:'Temporary startup failure'}));}return res.end(JSON.stringify(info()));}
      if(url.pathname==='/api/login'){
        if(!passwordEnabled){res.statusCode=503;return res.end(JSON.stringify({error:'The web account is not configured yet.'}));}
        let raw='';for await(const chunk of req)raw+=chunk;const data=JSON.parse(raw);
        if(data.username!=='admin'||data.password!==password){res.statusCode=401;return res.end(JSON.stringify({error:'Incorrect credentials'}));}
        const key='test-session-'+ ++sessionSequence,csrf='test-csrf-'+sessionSequence;sessions.set(key,{csrf});
        res.setHeader('Set-Cookie',`qmi_session=${key}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800`);
        return res.end(JSON.stringify({...info(),authenticated:true,username:'admin',csrf_token:csrf,expires_in:28800}));
      }
      if(!session){res.statusCode=401;return res.end(JSON.stringify({error:'Authentication required'}));}
      if(req.method==='POST'&&session&&req.headers['x-csrf-token']!==session.csrf){res.statusCode=403;return res.end(JSON.stringify({error:'Invalid CSRF token'}));}
      if(url.pathname==='/api/update'&&req.method==='GET'){
        updateGets++;
        if(updateGetFailures>0){updateGetFailures--;update503Count++;res.statusCode=503;return res.end(JSON.stringify({error:'The update information is not available.',code:'UPDATER_UNAVAILABLE'}));}
        return res.end(JSON.stringify(update??=updateBase()));
      }
      if(url.pathname.startsWith('/api/update/')&&req.method==='POST'){
        let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):{};
        updatePosts.push({path:url.pathname,body});
        const reply=updateReply(url.pathname,body);res.statusCode=reply.status;return res.end(JSON.stringify(reply.body));
      }
      if(url.pathname==='/api/logout'){await delay(300);sessions.delete(sid);res.setHeader('Set-Cookie','qmi_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0');return res.end(JSON.stringify({authenticated:false}));}
      if(url.pathname==='/api/tools'){return res.end(JSON.stringify({tools:toolsStatus,capabilities:{ping:true,iperf3:true,iperf3_bind_dev:true}}));}
      if(url.pathname==='/api/logs'){return res.end(JSON.stringify({unit:'qmi-supervisor',lines:['2026-10-26T10:00:00+0000 unit started'],requested:200}));}
      if(url.pathname==='/api/status'){
        requests++;if(status.sample){status.sample.log_id=10+requests;status.sample.collection.requests['nas.get_signal_info'].duration_ms=100+requests;}
        const snapshot=structuredClone(status);
        if(fleetMode){const entry=(id,profile,product,iface,metric)=>({id,detected:true,configured:true,interface:iface,state:'ONLINE',apn:profile.modem.apn,
            device:{manufacturer:'Test vendor',product,serial:id==='default'?'857df105':'18d94aeb',interface:iface,path:'/dev/cdc-wdm'+iface.slice(-1)},selector:{device:'auto',serial:''},features:{},
            connection_enabled:profile.modem.mode==='connect',monitoring_enabled:profile.telemetry.enabled,
            ...(id==='default'&&fleetDiagnosis?{diagnosis:fleetDiagnosis}:{}),
            network:{route_metric:metric,connectivity:{state:'up',route_mode:'requested',preferred:metric===700}}});
          snapshot.modems=[{...entry('default',config,'Primary test modem','wwan0',700),signal:{rat:'NR',band:'n78',operator:'Test operator',rsrp:-90,snr:12}},
            {...entry('second',secondConfig,'Second test modem','wwan1',800),detected:secondDetected}];
          snapshot.selected_modem=url.searchParams.get('modem')||'default';
          if(snapshot.selected_modem==='second')snapshot.device={...snapshot.device,interface:'wwan1',product:'Second test modem'};
        }
        const payload=JSON.stringify({snapshot,freshness});if(delayStatus){delayStatus=false;pendingStatus=()=>res.end(payload);return;}return res.end(payload);
      }
      const scopedConfig=fleetMode&&url.searchParams.get('modem')==='second'?secondConfig:config;
      if(url.pathname==='/api/config'&&req.method==='GET'){
        configRequests++;
        const response={config:structuredClone(scopedConfig),editable:!!session,listen_urls:[]};
        if(delayedConfigProfile===url.searchParams.get('modem')){
          delayedConfigProfile=null;response.config.modem.apn='stale-scope-response';
          pendingConfig=()=>res.end(JSON.stringify(response));return;
        }
        return res.end(JSON.stringify(response));
      }
      if(url.pathname==='/api/config'&&req.method==='POST'){
        let raw='';for await(const chunk of req)raw+=chunk;const data=JSON.parse(raw);posts.push(data);
        postScopes.push(url.searchParams.get('modem')||'default');
        // [web] is shared by every profile, as in fleet_config.py.
        for(const[section,values]of Object.entries(data.config))for(const[key,value]of Object.entries(values))if(key!=='password'){
          if(section==='web'||section==='update'){config[section][key]=value;secondConfig[section][key]=value;}else scopedConfig[section][key]=value;}
        return res.end(JSON.stringify({config:scopedConfig,restart_required:true,auto_apply:true}));
      }
      res.statusCode=404;res.end('{}');
    });
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const port=server.address().port;
    chrome=spawn(process.env.CHROME_BINARY||'google-chrome',['--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--no-first-run','--no-default-browser-check','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${directory}`,'about:blank'],{stdio:'ignore',detached:true});
    chrome.on('error',error=>{startupError=error;});
    let devtools;
    for(let i=0;i<160;i++){if(startupError)throw startupError;if(chrome.exitCode!==null)throw Error('Chrome exited');try{devtools=fs.readFileSync(path.join(directory,'DevToolsActivePort'),'utf8').split('\n')[0];break;}catch{}await delay(50);}
    assert.ok(devtools);
    const targets=await(await fetch(`http://127.0.0.1:${devtools}/json/list`)).json();ws=new WebSocket(targets.find(target=>target.type==='page').webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
    let sequence=0;const pending=new Map();
    ws.addEventListener('message',event=>{const message=JSON.parse(event.data);if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails);
      // Real input events give the page user activation, so a pending draft's beforeunload prompt would block reloads.
      if(message.method==='Page.javascriptDialogOpening')void send('Page.handleJavaScriptDialog',{accept:true});if(message.id){const handler=pending.get(message.id);if(!handler)return;pending.delete(message.id);clearTimeout(handler.timer);if(message.error)handler.reject(Error(JSON.stringify(message.error)));else handler.resolve(message.result);}});
    const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence,timer=setTimeout(()=>{pending.delete(id);reject(Error('Chrome command timed out: '+method+(params.expression?' '+params.expression.slice(0,160):'')));},6000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});
    const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value;};
    const wait=async(expression,message)=>{for(let i=0;i<160;i++){if(await evaluate(expression))return;await delay(50);}throw Error(message+'\n'+await evaluate('document.body.innerText'));};
    // Like wait(), but a page that reloads itself drops the execution context: keep polling through it.
    const waitLong=async(expression,message,seconds=20)=>{for(let i=0;i<seconds*20;i++){try{if(await evaluate(expression))return;}catch{}await delay(50);}throw Error(message);};
    const reload=async()=>{
      const marker='reload-'+Date.now();
      await evaluate(`window.reloadMarker=${JSON.stringify(marker)}`);await send('Page.reload');
      await wait(`window.reloadMarker!==${JSON.stringify(marker)}&&document.readyState==='complete'&&(!!document.querySelector('#session-open')||!!document.querySelector('.login-setup-message')||(!!document.querySelector('#session-connect')&&!document.querySelector('#session-connect').disabled))`,'fresh hydrated document');
    };
    const click=selector=>evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
    const input=(selector,value)=>evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});const proto=e instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    // Modem links carry ?modem=, so destinations are matched by path prefix. Modem views are reached
    // through the sidebar modem list; the shared system settings through the main navigation.
    const route=async target=>{
      if(await evaluate(`location.pathname===${JSON.stringify(target)}`))return;
      if(target==='/profile/'){
        await click('#account-button');
        await wait(`!!document.querySelector('#account-menu a[href="/profile/"]')`,'account menu');
        await click('#account-menu a[href="/profile/"]');
        await wait(`location.pathname==='/profile/'`,'client navigation to the user profile');
        await delay(80);return;
      }
      const selector=target==='/configuration/web/'?'#main-nav a[href="/configuration/web/"]':`#subnav a[href^="${target}?"],#modem-list a[href^="${target}?"]`;
      if(!(await evaluate(`!!document.querySelector(${JSON.stringify(selector)})`))){
        await click('#modem-list a.modem-link');await wait(`!!document.querySelector(${JSON.stringify(selector)})`,'modem navigation');
      }
      await click(selector);
      await wait(`location.pathname===${JSON.stringify(target)}`,'client navigation to '+target);
      await delay(80);
    };
    const configSections={modem:['connection','routing','verification','recovery','capture','gps','destinations'],web:['supervisor','log','access','updates'],account:['account']};
    const visible=selector=>evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});return !!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden'})()`);
    const openConfigNavigation=async()=>{
      if(await visible('#config-subnav-toggle')){
        if(!(await evaluate(`document.querySelector('#config-subnav-toggle').getAttribute('aria-expanded')==='true'`)))await click('#config-subnav-toggle');
        await wait(`document.querySelector('#config-subnav-toggle').getAttribute('aria-expanded')==='true'&&document.querySelector('#config-subnav-list').getClientRects().length>0`,'mobile configuration menu opens');
      }
    };
    const section=async(tab,name)=>{
      await route(tab==='account'?'/profile/':'/configuration/'+tab+'/');
      // The account has one section: its card fills the workbench without a one-entry index.
      if(tab==='account'){await wait(`!!document.querySelector('#config-panel-account-${name}')`,'account card');assert.equal(await evaluate('!!document.querySelector("#config-subnav-list")'),false,'a single section has no index');return;}
      await wait(`!!document.querySelector('#config-subnav-${name}')`,'inner configuration navigation '+tab);
      if(!(await evaluate(`document.querySelector('#config-subnav-${name}').getAttribute('aria-selected')==='true'`))){
        await openConfigNavigation();
        await evaluate(`document.querySelector('#config-subnav-${name}').scrollIntoView({block:'nearest',inline:'nearest'})`);await click('#config-subnav-'+name);
        if(await visible('#config-subnav-toggle'))await wait(`document.querySelector('#config-subnav-toggle').getAttribute('aria-expanded')==='false'&&document.querySelector('#config-subnav-list').getClientRects().length===0`,'selecting a configuration section closes the mobile menu');
      }
      await wait(`!!document.querySelector('#config-panel-${tab}-${name}')`,'active configuration subsection '+tab+'/'+name);
      assert.deepEqual(await evaluate(`(()=>{const nav=document.querySelector('.config-subnav'),selected=nav?.querySelector('[aria-selected="true"]'),panels=document.querySelectorAll('.config-subsection[role="tabpanel"]'),issues=[];if(nav?.id!=='config-subnav-list'||nav.getAttribute('aria-orientation')!=='vertical')issues.push('configuration navigation must be a vertical tablist');if(panels.length!==1)issues.push('only the active subsection is mounted');if(selected?.tabIndex!==0)issues.push('selected tab must be keyboard reachable');if(panels[0]?.id!==selected?.getAttribute('aria-controls'))issues.push('aria-controls does not identify the panel');if(panels[0]?.getAttribute('aria-labelledby')!==selected?.id)issues.push('panel is not labelled by its tab');if(Array.from(nav?.querySelectorAll('[role="tab"]')||[]).some(e=>e!==selected&&e.tabIndex!==-1))issues.push('inactive tabs must use roving focus');return issues})()`),[],'inner tab accessibility '+tab+'/'+name);
    };
    const disabled=async(selector,expected,message)=>{
      await wait(`!!document.querySelector(${JSON.stringify(selector)})`,'field exists '+selector);
      await wait(`document.querySelector(${JSON.stringify(selector)}).disabled===${expected}`,message||selector+' dependency');
      assert.equal(await evaluate(`document.querySelector(${JSON.stringify(selector)}).disabled`),expected,message||selector+' dependency');
    };
    const screenshot=async name=>{if(!quick)fs.writeFileSync('/tmp/qmi-next-'+name+'.png',Buffer.from((await send('Page.captureScreenshot',{captureBeyondViewport:true})).data,'base64'));};
    await send('Runtime.enable');await send('Page.enable');
    // A dialog can leave headless Chrome without window focus; focus events must still reach the page.
    await send('Emulation.setFocusEmulationEnabled',{enabled:true});
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
    await send('Page.navigate',{url:`http://127.0.0.1:${port}/dashboard/`});
    await wait('!!document.querySelector("#session-password") && !document.querySelector("#session-connect").disabled','React login must load');assert.ok(sessionRequests>=2,'bootstrap retries after a temporary failure');
    const assertLoginOnly=async()=>{
      assert.ok(await evaluate('!!document.querySelector(".auth-layout")'),'login has an independent layout');
      assert.ok(!(await evaluate('!!document.querySelector(".sidebar,.topbar,#main-nav,#subnav,#view,#save-bar,canvas,table,[data-setting]")')),'no application UI is mounted before login');
      assert.ok(!(await evaluate('!!document.querySelector("#session-token,.legacy-access") || document.body.innerText.includes("heredado")')),'no legacy login');
    };
    const assertSimplifiedShell=async()=>{
      assert.ok(!(await evaluate('!!document.querySelector(".sidebar-bottom,.sidebar-device,.sidebar-version,#web-status,#backend-status,.config-editable,#signal-reference")')),'removed status labels and signal reference stay absent');
      for(const label of ['Supervisor operativo','Supervisor local','Gestión local del dispositivo','Web conectada','Edición autenticada'])assert.ok(!(await evaluate(`document.body.innerText.includes(${JSON.stringify(label)})`)),'removed label: '+label);
    };
    const assertSaveBar=async expected=>{
      await wait((expected?'!!':'!')+'document.querySelector("#save-bar")',expected?'pending changes display the save bar':'clean settings hide the save bar');
      assert.equal(await evaluate('!!document.querySelector("#save-bar")'),expected,'save bar exists only for pending changes or saving');
      if(expected)assert.deepEqual(await evaluate(`(()=>{const bar=document.querySelector('#save-bar'),view=document.querySelector('#view'),form=document.querySelector('.config-sections'),issues=[];if(getComputedStyle(bar).position!=='sticky')issues.push('bar must stay reachable at the bottom while scrolling');if(bar.closest('main')!==document.querySelector('.main-content'))issues.push('bar is outside main flow');if(!(view.compareDocumentPosition(bar)&Node.DOCUMENT_POSITION_FOLLOWING))issues.push('bar must follow view');if(!['/configuration/','/profile/'].some(prefix=>location.pathname.startsWith(prefix)))issues.push('pending controls must stay inside configuration');if(form){const b=bar.getBoundingClientRect(),f=form.getBoundingClientRect();if(Math.abs(b.left-f.left)>2||Math.abs(b.width-f.width)>2)issues.push('save bar must align with the form column '+Math.round(b.left)+'/'+Math.round(b.width)+' versus '+Math.round(f.left)+'/'+Math.round(f.width));}return issues})()`),[],'save bar remains below content and aligns with the current form column');
    };
    const saveCurrent=async expected=>{
      const count=posts.length;await click('#global-save');
      for(let index=0;index<100&&posts.length===count;index++)await delay(30);
      assert.equal(posts.length,count+1,'one scoped write per save');assert.deepEqual(posts.at(-1),{config:expected});
      await assertSaveBar(false);
    };
    await assertLoginOnly();assert.equal(requests,0,'no metrics are fetched before authentication');assert.equal(configRequests,0,'no configuration is fetched before authentication');await screenshot('login');
    for(const[width,height]of (quick?[[390,844],[1440,1100]]:[[320,640],[390,844],[600,900],[768,1024],[844,390],[1024,768],[1920,1080]])){
      await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<768});
      assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'login fits '+width+'px');
      assert.ok(await evaluate('(()=>{const r=document.querySelector(".login-card").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth})()'),'login card fits '+width+'px');
      await assertLoginOnly();
    }
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
    await evaluate(`sessionStorage.setItem('qmi.token','browser-test-token')`);await reload();
    await wait('!!document.querySelector("#session-connect") && !document.querySelector("#session-connect").disabled','login after reload');await assertLoginOnly();assert.equal(bearerRequests,0,'the browser never sends a bearer token');assert.equal(requests,0);
    assert.equal(await evaluate('document.documentElement.lang'),'en');
    assert.equal(await evaluate('document.documentElement.dataset.theme'),'light');
    assert.ok(await evaluate('document.body.innerText.includes("Sign in")'));
    await input('#language-select','es');await wait('document.documentElement.lang==="es"','Spanish preference');
    await click('#theme-toggle');await wait('document.documentElement.dataset.theme==="dark"','dark login');
    await evaluate('window.preferenceReloadMarker=true');await reload();await wait('!window.preferenceReloadMarker&&document.querySelector("#theme-toggle")?.getAttribute("aria-pressed")==="true"&&document.documentElement.lang==="es"&&!document.querySelector("#session-connect")?.disabled','preferences restored');
    await click('#theme-toggle');await wait('document.documentElement.dataset.theme==="light"','light login');
    await input('#session-username','admin');await input('#session-password','wrong');await click('#session-connect');
    await wait('document.querySelector("#session-error")?.textContent.includes("incorrectos")','login error');
    await input('#session-password',password);await click('#session-connect');await wait('document.body.innerText.includes("Test modem")','authenticated dashboard');
    await assertSimplifiedShell();await assertSaveBar(false);
    assert.deepEqual(await evaluate(`(()=>{const v=document.querySelector('.account-version'),b=document.querySelector('#account-button'),a=document.querySelector('.account');return {text:v?.textContent,inside:!!v&&v.parentElement===a,before:!!v&&!!b&&(v.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING)>0,forbidden:!!document.querySelector('.sidebar-version,.sidebar-bottom')}})()`),{text:'vdev',inside:true,before:true,forbidden:false},'the account menu shows the build version before the account button');
    await wait('document.querySelector("#notification")?.textContent.includes("Sesión iniciada")','session notification');
    const toastStarted=Date.now();
    const toastPosition=await evaluate(`(()=>{const e=document.querySelector('.toast-region'),r=e.getBoundingClientRect(),s=getComputedStyle(e),side=document.querySelector('.sidebar').getBoundingClientRect();return{position:s.position,left:r.left-Math.max(0,side.right),bottom:innerHeight-r.bottom}})()`);
    assert.equal(toastPosition.position,'fixed');assert.ok(toastPosition.left>=0&&toastPosition.left<=32&&toastPosition.bottom<=32,'toast appears at the bottom left, beside the sidebar and its account menu');
    const toastRequests=requests;await route('/configuration/modem/');await route('/modem/');await delay(2300);assert.ok(await evaluate('!!document.querySelector("#notification")'),'notice remains readable during a status refresh');
    await wait('!document.querySelector("#notification")','session notification expires despite repeated polling');assert.ok(Date.now()-toastStarted<6000,'navigation never renews a session notice');assert.ok(requests>toastRequests,'data updates never restart the toast lifetime');
    for(const value of ['99910','n78','3449,28 MHz','001010123456789','20 MHz'])assert.ok(await evaluate(`document.body.innerText.includes(${JSON.stringify(value)})`),value);
    assert.ok(!(await evaluate('document.cookie.includes("qmi_session")')),'HttpOnly session');
    await reload();await wait('document.body.innerText.includes("Test modem")','session survives reload');
    const dashboard=status;
    status={...fixture(),state:'WAIT_REGISTRATION',sample:null,radio:null,history:[],network:{address:null},connection:{phase:'WAIT_REGISTRATION',requested_apn:'tenten',requested_plmn:'99910'}};
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('document.body.innerText.includes("Esperando registro")','registration without samples');
    assert.ok(await evaluate('document.body.innerText.includes("tenten")'),'requested APN before data session');
    assert.ok(!(await evaluate('document.body.innerText.includes("001010123456789")')),'old private sample is removed');
    status=dashboard;await evaluate('document.dispatchEvent(new Event("visibilitychange"))');await wait('document.body.innerText.includes("En línea")','online restored');
    await screenshot('dashboard');
    const hover=async(selector,unit)=>{
      await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'center'})`);
      const point=await evaluate(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return{x:r.x+r.width*.55,y:r.y+r.height*.5}})()`);
      await send('Input.dispatchMouseEvent',{type:'mouseMoved',...point});
      await wait(`Array.from(document.querySelectorAll('.metric-chart-tooltip')).some(e=>!e.hidden&&e.textContent.includes(${JSON.stringify(unit)}))`,'chart tooltip '+unit);
    };
    await hover('canvas','dBm');await evaluate('void(window.originalCanvas=document.querySelector("canvas"))');const beforeCanvas=requests;await delay(2250);
    assert.ok(requests>beforeCanvas);assert.ok(await evaluate('window.originalCanvas===document.querySelector("canvas")'),'React retains Canvas DOM');
    const beforeConfig=configRequests;
    // Basic mode: rarely changed settings and whole sections stay out of the way.
    await section('modem','routing');
    assert.ok(!(await evaluate('!!document.querySelector("#config-subnav-recovery")')),'advanced-only sections are not listed');
    assert.ok(!(await evaluate('!!document.querySelector("#field-network-dns")||!!document.querySelector("#field-network-method")')),'advanced rows are hidden');
    assert.ok(await evaluate('!!document.querySelector("#field-network-route_metric")'),'a fixed metric is a basic choice');
    await section('modem','capture');
    await assertSimplifiedShell();await assertSaveBar(false);
    assert.ok(!(await evaluate('!!document.querySelector("#field-telemetry-collection_budget")')),'the acquisition budget is advanced');
    await input('#field-telemetry-interval','.5');
    await assertSaveBar(true);
    assert.ok(await evaluate('document.querySelector("#global-save").disabled'),'an error blocks the modem save');
    await wait('document.querySelector("#field-telemetry-collection_budget")?.getAttribute("aria-invalid")==="true"','an advanced field with an error is never hidden');
    await click('#field-telemetry-enabled');await disabled('#field-telemetry-collection_budget',false,'an invalid inactive value stays editable for correction');
    await input('#field-telemetry-collection_budget','.4');await disabled('#field-telemetry-collection_budget',false,'a stopped objective remains configurable');
    await click('#field-telemetry-enabled');await input('#field-telemetry-interval','1');await input('#field-telemetry-collection_budget','.8');await assertSaveBar(false);
    assert.ok(!(await evaluate('!!document.querySelector(".config-card-header #advanced-toggle")')),'advanced settings are not a switch beside the section switch');
    await click('#advanced-toggle');await wait('document.querySelector("#advanced-toggle").getAttribute("aria-expanded")==="true"&&!!document.querySelector("#config-subnav-recovery")','advanced options reveal every section');
    const actualFields=new Set();
    for(const[tab,names]of Object.entries(configSections))for(const name of names){await section(tab,name);for(const field of await evaluate('Array.from(document.querySelectorAll("[data-setting]")).flatMap(e=>e.dataset.setting.split(" "))'))actualFields.add(field);}
    for(const[section,values]of Object.entries(defaults))for(const key of Object.keys(values))assert.ok(actualFields.has(section+'.'+key),'field '+section+'.'+key);
    await section('modem','connection');assert.ok(await evaluate('document.querySelector("#identify-auto").checked&&!document.querySelector("#field-modem-device")'),'automatic detection needs no selector');
    await click('#identify-port');await disabled('#field-modem-device',false);await disabled('#field-modem-serial',false);
    assert.equal(await evaluate('document.querySelector("#field-modem-device").value'),'/dev/cdc-wdm1','the detected control port is offered');
    await click('#identify-auto');await wait('!document.querySelector("#field-modem-device")','automatic detection hides the selectors');await assertSaveBar(false);
    await click('#field-modem-mode-monitor');await disabled('#field-modem-apn',false);await disabled('#field-modem-plmn',false);
    await section('modem','routing');for(const field of ['#field-network-method','#field-network-route_metric','#route-role','#field-network-dns'])await disabled(field,false,'stopped connection: '+field);
    await section('modem','verification');await disabled('#field-network-connectivity_check',false);await disabled('#field-network-connectivity_targets',false);
    await section('modem','recovery');await disabled('#field-recovery-registration_timeout',false);await disabled('#field-recovery-operation_timeout',false);
    await section('modem','connection');await click('#field-modem-mode-connect');await disabled('#field-modem-apn',false);
    assert.equal(await evaluate('document.querySelector("#field-modem-apn").value'),'internet','monitor never erases APN');
    assert.equal(await evaluate('document.querySelector("#field-modem-plmn").value'),'99910','monitor never erases operator selection');
    await section('modem','routing');await click('#route-role-none');await disabled('#field-network-route_metric',true);await disabled('#field-network-dns',true);await disabled('#route-role',false,'a role card restores the default route');
    await section('modem','verification');await disabled('#field-network-connectivity_check',true);await disabled('#field-network-connectivity_targets',true);
    await section('modem','routing');await click('#route-role-fixed');await disabled('#field-network-route_metric',false);
    await section('modem','verification');await disabled('#field-network-connectivity_targets',false);await click('#field-network-connectivity_check');await disabled('#field-network-connectivity_targets',true);
    await click('#field-network-connectivity_check');await disabled('#field-network-connectivity_targets',false);await assertSaveBar(false);
    await section('modem','capture');await click('#field-telemetry-enabled');await disabled('#field-telemetry-interval',false);
    await disabled('#field-telemetry-throughput',false);await section('modem','gps');await disabled('#field-telemetry-gps',false);
    await section('modem','capture');await click('#field-telemetry-enabled');await disabled('#field-telemetry-interval',false);
    await section('modem','gps');await disabled('#field-telemetry-gps_source',true);await click('#field-telemetry-gps');await click('#field-telemetry-gps_source-modem');await click('#field-telemetry-gps');await disabled('#field-telemetry-gps_source',true);
    assert.ok(await evaluate('document.querySelector("#field-telemetry-gps_source-modem").checked'),'disabling GPS preserves its source');
    await click('#field-telemetry-gps');assert.ok(await evaluate('document.querySelector("#field-telemetry-gps_source-modem").checked'));
    await section('modem','destinations');await disabled('#field-telemetry-udp_port',true);await disabled('#field-telemetry-collector_port',true);
    await input('#field-telemetry-udp_host','192.0.2.8');await input('#field-telemetry-udp_port','5200');await input('#field-telemetry-udp_host','');await disabled('#field-telemetry-udp_port',true);
    assert.equal(await evaluate('document.querySelector("#field-telemetry-udp_port").value'),'5200','an inactive destination keeps its port');
    await input('#field-telemetry-udp_host','192.0.2.8');await disabled('#field-telemetry-udp_port',false);assert.equal(await evaluate('document.querySelector("#field-telemetry-udp_port").value'),'5200');
    await click('#field-telemetry-csv');await click('#field-telemetry-json');await disabled('#field-telemetry-directory',true,'file persistence disabled: directory');
    for(const field of ['flush_interval','queue_size'])await disabled('#field-telemetry-'+field,true,'file persistence disabled: '+field);
    await click('#global-discard');await assertSaveBar(false);await section('modem','capture');assert.equal(await evaluate('document.querySelector("#field-telemetry-interval").value'),'1');
    await section('web','access');await click('#field-web-enabled');for(const field of ['#web-listen','#field-web-port','#field-web-history_size','#field-web-max_workers','#field-web-address_refresh','#field-web-poll_interval'])await disabled(field,true);
    await section('account','account');await disabled('#field-web-username',false);await disabled('#field-web-session_timeout',false);
    await disabled('#field-web-password',false);await section('web','access');await click('#field-web-enabled');await disabled('#field-web-port',false);await assertSaveBar(false);
    // Supervisor policy shares the page and its save seam.
    await section('web','log');await wait('!!document.querySelector(".system-log .log-line")&&document.querySelector(".system-log").innerText.includes("unit started")','the system log section shows the journal');
    assert.ok(await evaluate('document.querySelector("#system-log-follow").checked'),'the log follows new lines by default');
    assert.equal(await evaluate('document.querySelectorAll(".config-subsection .panel").length'),1,'the log is one section of the System page, not a second card');
    await section('web','supervisor');await click('#field-system-log_level-DEBUG');await wait('!!document.querySelector("#save-bar")','log level is a pending system change');
    assert.equal(await evaluate('document.querySelector("#field-system-log_level-DEBUG").checked'),true);await click('#global-discard');await assertSaveBar(false);
    await section('web','access');await input('#field-web-poll_interval','3');await wait('!!document.querySelector("#save-bar")','poll cadence is a pending system change');await click('#global-discard');
    assert.ok((await evaluate('document.querySelector("#poll-status").textContent')).includes('2'),'the footer states the saved cadence');

    // The vertical menu is a real roving-focus tablist, not a page reload.
    await section('modem','connection');await evaluate('document.querySelector("#config-subnav-connection").focus()');
    for(const[key,code,target]of [['ArrowDown',40,'routing'],['ArrowUp',38,'connection'],['End',35,'destinations'],['Home',36,'connection']]){
      await send('Input.dispatchKeyEvent',{type:'keyDown',key,code:key,windowsVirtualKeyCode:code});await send('Input.dispatchKeyEvent',{type:'keyUp',key,code:key,windowsVirtualKeyCode:code});
      await wait(`document.activeElement.id==='config-subnav-${target}'&&document.activeElement.getAttribute('aria-selected')==='true'`,'keyboard configuration tab '+key);
    }
    await section('modem','connection');await input('#field-modem-apn','private.example');
    await assertSaveBar(true);
    await route('/metrics/charts/');await click('#metrics-chart-performance');await assertSaveBar(false);await route('/modem/');await assertSaveBar(false);
    await section('account','account');await assertSaveBar(false);await assertSimplifiedShell();
    assert.equal(await evaluate('document.querySelector("#field-web-password").value'),'');
    assert.ok(!(await evaluate('!!document.querySelector("#field-web-password-confirm")')),'one password field replaces the confirmation');
    const pendingPassword='Pending account password 2026';await input('#field-web-password',pendingPassword);await assertSaveBar(true);
    assert.equal(await evaluate('document.querySelector("#field-web-password").type'),'password');await click('#field-web-password-toggle');
    assert.equal(await evaluate('document.querySelector("#field-web-password").type'),'text','Show reveals the new password');await click('#field-web-password-toggle');
    await route('/metrics/data/');assert.ok(!(await evaluate(`document.querySelector('#config-json').textContent.includes(${JSON.stringify(pendingPassword)})`)),'diagnostic JSON never includes a pending password');
    await section('modem','capture');await assertSaveBar(true);await input('#field-telemetry-interval','.5');assert.ok(await evaluate('document.querySelector("#global-save").disabled'),'a telemetry error blocks the modem save');
    await section('modem','connection');assert.equal(await evaluate('document.querySelector("#field-modem-apn").value'),'private.example','draft across Next routes');
    await click('#global-discard');await assertSaveBar(false);assert.equal(await evaluate('document.querySelector("#field-modem-apn").value'),'internet');
    await section('account','account');assert.equal(await evaluate('document.querySelector("#field-web-password").value'),pendingPassword,'discarding the modem preserves the shared web password');
    await section('modem','capture');assert.equal(await evaluate('document.querySelector("#field-telemetry-interval").value'),'1','discarding the modem includes its telemetry');
    await section('modem','connection');await input('#field-modem-apn','private.example');
    await assertSaveBar(true);assert.ok(!(await evaluate('document.querySelector("#global-save").disabled')),'web errors never block the modem save');
    await input('#field-modem-apn','private.example');await click('.refresh-button');assert.equal(await evaluate('document.querySelector("#field-modem-apn").value'),'private.example','manual refresh preserves the draft');await evaluate('document.querySelector("#field-modem-apn").focus();document.querySelector("#field-modem-apn").setSelectionRange(4,4)');
    const beforeDraft=requests;await delay(2200);assert.ok(requests>beforeDraft);assert.equal(await evaluate('document.activeElement.id'),'field-modem-apn');assert.equal(await evaluate('document.querySelector("#field-modem-apn").selectionStart'),4);
    assert.equal(configRequests,beforeConfig,'section navigation never reloads configuration or duplicates API polling');
    await saveCurrent({modem:{apn:'private.example'}});
    await section('account','account');assert.equal(await evaluate('document.querySelector("#field-web-password").value'),pendingPassword,'saving the modem preserves the shared web password');await assertSaveBar(true);
    await section('modem','routing');await click('#route-role-primary');await assertSaveBar(true);
    await section('modem','capture');await assertSaveBar(true);await section('modem','routing');
    assert.ok(await evaluate('document.querySelector("#route-role-primary").checked'),'the chosen role survives section changes');
    await click('.save-diff-toggle');await wait('document.querySelector("#save-diff")?.children.length===2','View changes lists every pending value');
    await saveCurrent({network:{route_metric:100,route_policy:'primary'}});
    await section('account','account');assert.equal(await evaluate('document.querySelector("#field-web-password").value'),pendingPassword);await click('#global-discard');await assertSaveBar(false);
    await section('web','access');await input('#field-web-port','8081');await saveCurrent({web:{port:8081}});

    // Version and updates (plans/updates/U4-web-ui.md): installed build, released versions,
    // password re-entry, the restart and the automatic reload, both result paths, the dot and the
    // settings. Never a notice or banner in a modem page.
    const stepsAt=(running,states={})=>['download','verify','prepare','install','restart','health']
      .map(id=>({id,state:states[id]??(id===running?'running':'pending')}));
    const releaseText=(version,selector)=>evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(`tbody[data-release="${version}"] ${selector}`)});return e?e.textContent:''})()`);
    const buildSelector='#update-installed .metric-key-values dd:not(#update-installed-version):not(#update-check-status)';
    // 1. Up to date.
    await section('web','updates');
    await wait('!!document.querySelector("#update-installed-version")','the System page shows the update section');
    assert.equal(await evaluate('document.querySelector("#update-installed-version").textContent'),'1.2.0-1','the installed version is shown');
    assert.equal(await evaluate('!!document.querySelector("#main-nav .nav-update-dot")'),false,'an up-to-date device shows no System dot');
    await wait(`document.querySelector(${JSON.stringify(buildSelector)}).textContent.includes("abc1234")`,'the installed build line shows the commit and its date');
    assert.match(await evaluate(`document.querySelector(${JSON.stringify(buildSelector)}).textContent`),/\d{4}/,'the build line shows a build date');
    update.installed.commit=null;update.installed.built_utc=null;
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait(`document.querySelector(${JSON.stringify(buildSelector)}).textContent==="No disponible"`,'an unknown build is reported as not available');
    update.installed.commit='abc1234';update.installed.built_utc='2026-11-19T16:47:00Z';
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait(`document.querySelector(${JSON.stringify(buildSelector)}).textContent.includes("abc1234")`,'the build line returns');
    // 2. A newer release: dot, sidebar line, plain-text notes, and nothing in the modem page.
    update.releases.unshift(updateRelease('1.5.0-1','2026-11-20T09:00:00Z','newer',{prerelease:true,installable:false,reason:'prerelease_channel'}));
    update.releases.unshift(updateRelease('1.3.0-1','2026-11-20T08:00:00Z','newer',{notes:'<b>bold</b> <img src=x onerror="window.injected=1">'}));
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('!!document.querySelector("#update-install-primary")','the new release appears after a refresh');
    await wait('!!document.querySelector(\'#main-nav a[href="/configuration/web/"] .nav-update-dot.tone-info\')','the System item shows the available update');
    assert.ok(await evaluate('document.querySelector("#update-available").textContent.includes("1.3.0-1")'),'the sidebar line names the available update');
    assert.ok((await releaseText('1.3.0-1','td:nth-child(2)')).includes('2026'),'each release shows its publication date');
    assert.equal((await releaseText('1.3.0-1','td:nth-child(3) .update-badge')).trim(),'Estable','a stable release is labelled');
    assert.equal((await releaseText('1.5.0-1','td:nth-child(3) .update-badge')).trim(),'Preliminar','a pre-release is labelled');
    assert.equal((await releaseText('1.3.0-1','td:nth-child(4) .update-badge')).trim(),'Más reciente','a newer release is labelled');
    assert.equal((await releaseText('1.2.0-1','td:nth-child(4) .update-badge')).trim(),'Instalada','the installed release is labelled');
    assert.equal((await releaseText('1.1.0-1','td:nth-child(4) .update-badge')).trim(),'Anterior','an older release is labelled');
    assert.ok((await releaseText('1.5.0-1','td:nth-child(5)')).includes('No instalable: Las versiones preliminares no están activadas'),'a non-installable release explains why');
    await evaluate('document.querySelector(\'details[data-notes="1.3.0-1"]\').open=true');
    assert.ok(await evaluate('(()=>{const d=document.querySelector(\'details[data-notes="1.3.0-1"]\');return d.textContent.includes("<b>bold</b>")&&!d.querySelector("img")&&window.injected===undefined})()'),'release notes are plain text, never HTML');
    await route('/modem/');
    assert.equal(await evaluate('!!document.querySelector("#view .metric-notice")'),false,'no update notice appears in a modem page');
    assert.ok(!(await evaluate('document.querySelector("#view").innerText.includes("1.3.0")')),'the modem page never carries update text');
    assert.ok(await evaluate('!!document.querySelector(\'#main-nav a[href="/configuration/web/"] .nav-update-dot.tone-info\')'),'the dot stays on the System item');
    await click('#update-available');
    await wait('!!document.querySelector("#config-panel-web-updates")','the sidebar line opens the update section');
    // 3. Check now asks the updater and the limit disables the button at once.
    await click('#update-check');
    for(let i=0;i<100&&!(updatePosts.length&&updatePosts.at(-1).path==='/api/update/check');i++)await delay(30);
    assert.deepEqual(updatePosts.at(-1),{path:'/api/update/check',body:{}},'Check now posts an empty body');
    await wait('document.querySelector("#update-check").disabled','the check limit disables the button');
    // The server's one-check-per-minute rule is explained and counts down on the button.
    await wait('!document.querySelector("#update-check").disabled','the first check finishes');
    updateReply=(path,body)=>path==='/api/update/check'
      ?{status:429,body:{error:'Wait a minute before checking again.',code:'RATE_LIMITED',retry_after:42}}
      :updateAccept(path,body);
    const limitedChecks=updatePosts.length;
    await click('#update-check');
    for(let i=0;i<100&&updatePosts.length===limitedChecks;i++)await delay(30);
    assert.deepEqual(updatePosts.at(-1),{path:'/api/update/check',body:{}},'the limited check also posts an empty body');
    await wait('document.querySelector("#notification")?.textContent.includes("Espera un minuto antes de volver a comprobarlo.")','the check limit is explained');
    await wait('/Nueva comprobación posible en [0-9]+ s/.test(document.querySelector("#update-check").textContent)','the check button counts down');
    assert.match(await evaluate('document.querySelector("#update-check").textContent'),/Nueva comprobación posible en (4[0-2]) s/,'the countdown follows the retry_after of the refusal');
    assert.ok(await evaluate('document.querySelector("#update-check").disabled'),'the button stays disabled during the countdown');
    updateReply=updateAccept;
    // 4. Install after re-entering the password, follow the steps, lose the API and reload by itself.
    await click('#update-install-primary');
    await wait('!!document.querySelector("#update-dialog")?.open','the confirmation dialog opens');
    assert.equal(await evaluate('document.activeElement.id'),'update-password','the password field takes focus');
    const dialogText=await evaluate('document.querySelector("#update-dialog-text").textContent');
    assert.ok(dialogText.includes('se reinicia')&&dialogText.includes('módems')&&dialogText.includes('se recarga'),'the dialog explains the restart, the modems and the reload');
    await input('#update-password','wrong-passphrase');
    await click('#update-confirm');
    await wait('document.querySelector("#update-dialog-error")?.textContent.includes("Contraseña incorrecta")','a wrong password is refused');
    assert.ok(await evaluate('!!document.querySelector("#session-open")'),'a wrong re-entry keeps the session');
    const installCount=updatePosts.length;
    await input('#update-password',password);
    await click('#update-confirm');
    for(let i=0;i<100&&updatePosts.length===installCount;i++)await delay(30);
    assert.deepEqual(updatePosts.at(-1),{path:'/api/update/install',body:{version:'1.3.0-1',allow_downgrade:false,password}},'the install request re-enters the password');
    await wait('!document.querySelector("#update-dialog")','the dialog closes when the request is accepted');
    update.install={action:'install',state:'running',version:'1.3.0-1',from_version:'1.2.0-1',step:'download',
      steps:stepsAt('download'),started_at_utc:'2026-11-20T10:00:00Z',finished_at_utc:null,error_code:null,error:null,running:true};
    await wait('!!document.querySelector(\'#update-steps li[data-step=download][data-state=running]\')','the download step is running');
    await wait('document.querySelector("#update-available")?.textContent.includes("Actualizando a 1.3.0-1")','the sidebar line follows the running install');
    assert.ok(await evaluate('!!document.querySelector(\'#main-nav a[href="/configuration/web/"] .nav-update-dot.tone-info\')'),'the System dot stays informative while installing');
    update.install={...update.install,step:'restart',steps:stepsAt('restart',{download:'done',verify:'done',prepare:'done',install:'done'})};
    await wait('!!document.querySelector(\'#update-steps li[data-step=restart][data-state=running]\')','the restart step is running');
    updateDown=true;
    await wait('!!document.querySelector("#update-restarting")','the page says the supervisor is restarting');
    await evaluate('window.updateReloadMarker=1');
    const successAt=new Date().toISOString();
    Object.assign(update,{install:{...update.install,state:'succeeded',step:null,
      steps:stepsAt(null,{download:'done',verify:'done',prepare:'done',install:'done',restart:'done',health:'done'}),
      finished_at_utc:successAt,running:false},history:[{action:'install',version:'1.3.0-1',from_version:'1.2.0-1',result:'succeeded',
      started_at_utc:'2026-11-20T10:00:00Z',finished_at_utc:successAt,error_code:null,error:null}]});
    update.installed.version='1.3.0-1';
    update.releases=[updateRelease('1.3.0-1','2026-11-20T08:00:00Z','installed'),updateRelease('1.2.0-1','2026-11-05T08:00:00Z','older'),
      updateRelease('1.1.0-1','2026-10-26T08:00:00Z','older')];
    updateDown=false;
    await waitLong('window.updateReloadMarker===undefined&&document.readyState==="complete"&&!!document.querySelector("#session-open")','the page reloads by itself after the restart, still signed in',30);
    await wait('document.querySelector("#notification")?.textContent.includes("1.3.0-1")','a success notice names the new version');
    await wait('!document.querySelector("#update-available")','the sidebar line disappears when the update finishes');
    assert.equal(await evaluate('!!document.querySelector("#main-nav .nav-update-dot")'),false,'the System dot disappears when the update finishes');
    await screenshot('updates');
    // 5. A failed update is rolled back: the previous version runs, the page does not reload.
    await section('web','updates');
    await evaluate('window.updateReloadMarker=2');
    update.releases.unshift(updateRelease('1.4.0-1','2026-11-21T08:00:00Z','newer'));
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('!!document.querySelector("#update-install-primary")','the newer release appears');
    await click('#update-install-primary');
    await wait('!!document.querySelector("#update-dialog")?.open','the update dialog reopens');
    await input('#update-password',password);
    await click('#update-confirm');
    await wait('!document.querySelector("#update-dialog")','the second install request is accepted');
    update.install={action:'install',state:'running',version:'1.4.0-1',from_version:'1.3.0-1',step:'health',
      steps:stepsAt('health',{download:'done',verify:'done',prepare:'done',install:'done',restart:'done'}),
      started_at_utc:'2026-11-20T11:00:00Z',finished_at_utc:null,error_code:null,error:null,running:true};
    await wait('!!document.querySelector(\'#update-steps li[data-step=health][data-state=running]\')','the health step is running');
    updateDown=true;
    await wait('!!document.querySelector("#update-restarting")','the page waits during the restart');
    const rollbackAt=new Date().toISOString();
    Object.assign(update,{install:{...update.install,state:'rolled_back',finished_at_utc:rollbackAt,running:false,
      error_code:'health_failed',error:'Health check failed'},history:[{action:'install',version:'1.4.0-1',from_version:'1.3.0-1',
      result:'rolled_back',started_at_utc:'2026-11-20T11:00:00Z',finished_at_utc:rollbackAt,error_code:'health_failed',error:'Health check failed'}]});
    updateDown=false;
    await wait('document.querySelector("#update-result")?.textContent.includes("1.4.0-1")','the result names the failed version');
    await wait('document.querySelector("#update-result")?.textContent.includes("1.3.0-1")','the result names the restored version');
    await wait('document.querySelector("#notification")?.textContent.includes("1.4.0-1")','a failure notice names the version');
    assert.equal(await evaluate('window.updateReloadMarker'),2,'a failed update that restored the version does not reload');
    // 6. An unseen failure turns the System dot bad, and reading the section clears it.
    await route('/modem/');
    const failedAt=new Date().toISOString();
    update.history.unshift({action:'automatic',version:'9.9.9',from_version:'1.3.0-1',result:'failed',
      started_at_utc:failedAt,finished_at_utc:failedAt,error_code:'bad_signature',error:'Signature mismatch'});
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('!!document.querySelector("#main-nav .nav-update-dot.tone-bad")','an unseen failure turns the System dot bad');
    assert.equal(await evaluate('!!document.querySelector("#view .metric-notice")'),false,'no update notice appears in the modem page');
    assert.ok(!(await evaluate('document.querySelector("#view").innerText.includes("1.4.0")')),'the modem page never carries update text');
    assert.ok(!(await evaluate('document.querySelector("#view").innerText.includes("9.9.9")')),'the modem page never carries the failed automatic version');
    await section('web','updates');
    await wait('document.querySelector("#update-history")?.textContent.includes("9.9.9")','the history lists the failed automatic update');
    await route('/modem/');
    await wait('!document.querySelector("#main-nav .nav-update-dot.tone-bad")','reading the section clears the bad dot');
    // 7. An older release is an advanced downgrade with its warning.
    await section('web','updates');
    assert.ok(await evaluate('!!document.querySelector(\'[data-update-version="1.2.0-1"][data-update-kind="downgrade"]\')'),'an older release offers an advanced downgrade');
    await click('[data-update-version="1.2.0-1"][data-update-kind="downgrade"]');
    await wait('!!document.querySelector("#update-dialog")?.open&&!!document.querySelector("#update-downgrade-warning")','the downgrade warning shows');
    const downgradePosts=updatePosts.length;
    await click('#update-cancel');
    await wait('!document.querySelector("#update-dialog")','cancel closes without a request');
    assert.equal(updatePosts.length,downgradePosts,'cancel posts nothing');
    await click('[data-update-version="1.2.0-1"][data-update-kind="downgrade"]');
    await wait('!!document.querySelector("#update-dialog")?.open','the downgrade dialog reopens');
    updateReply=()=>({status:409,body:{error:'An update is already running.',code:'UPDATE_IN_PROGRESS'}});
    await input('#update-password',password);
    await click('#update-confirm');
    await wait('document.querySelector("#update-dialog-error")?.textContent.includes("Ya hay una actualización en curso")','the server refusal is explained');
    assert.deepEqual(updatePosts.at(-1),{path:'/api/update/install',body:{version:'1.2.0-1',allow_downgrade:true,password}},'a downgrade is confirmed explicitly');
    await click('#update-cancel');
    await wait('!document.querySelector("#update-dialog")','the refused dialog closes');
    await click('#advanced-toggle');
    await wait('!document.querySelector(\'[data-update-version="1.2.0-1"][data-update-kind="downgrade"]\')','basic mode hides the downgrade');
    await click('#advanced-toggle');
    await wait('!!document.querySelector(\'[data-update-version="1.2.0-1"][data-update-kind="downgrade"]\')','advanced mode restores the downgrade');
    // 7b. The kept previous version can be restored: same password re-entry, a rollback request and its success.
    updateReply=updateAccept;
    update.rollback={available:true,version:'1.2.0-1',running:false};
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('!!document.querySelector("#update-restore")','the kept version offers a restore action');
    assert.ok(await evaluate('document.querySelector("#update-rollback").textContent.includes("1.2.0-1")'),'the kept version is named');
    await click('#update-restore');
    await wait('!!document.querySelector("#update-dialog")?.open','the restore dialog opens');
    assert.ok(await evaluate('document.querySelector("#update-dialog-title").textContent.includes("Restaurar la versión 1.2.0-1")'),'the dialog asks to restore the kept version');
    await input('#update-password','wrong-passphrase');
    await click('#update-confirm');
    await wait('document.querySelector("#update-dialog-error")?.textContent.includes("Contraseña incorrecta")','a wrong restore password is refused');
    assert.ok(await evaluate('!!document.querySelector("#session-open")'),'a wrong restore password keeps the session');
    const rollbackCount=updatePosts.length;
    await input('#update-password',password);
    await click('#update-confirm');
    for(let i=0;i<100&&updatePosts.length===rollbackCount;i++)await delay(30);
    assert.deepEqual(updatePosts.at(-1),{path:'/api/update/rollback',body:{password}},'the restore request re-enters the password');
    await wait('!document.querySelector("#update-dialog")','the restore dialog closes when accepted');
    const restoredAt=new Date().toISOString();
    update.install={action:'rollback',state:'succeeded',version:'1.2.0-1',from_version:'1.3.0-1',step:null,
      steps:stepsAt(null,{download:'done',verify:'done',prepare:'done',install:'done',restart:'done',health:'done'}),
      started_at_utc:'2026-11-20T12:00:00Z',finished_at_utc:restoredAt,error_code:null,error:null,running:false};
    await wait('document.querySelector("#update-result")?.textContent.includes("Se restauró la versión 1.2.0-1.")','the restore succeeds');
    await wait('document.querySelector("#notification")?.textContent.includes("Se restauró la versión 1.2.0-1.")','a success notice names the restored version');
    update.rollback={available:false,version:null,running:false};update.install=null;
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('!document.querySelector("#update-rollback")','the restore offer is cleared');
    // 8. The updater settings save through the normal System seam and keep their policy.
    await click('#field-update-mode-auto');
    await disabled('#field-update-window_start',false,'automatic updates need the maintenance window');
    await disabled('#field-update-window_end',false,'automatic updates need the maintenance window');
    await saveCurrent({update:{mode:'auto'}});
    assert.ok(await evaluate('document.querySelector("#field-update-mode-auto").checked'),'the saved setting stays checked');
    await click('#field-update-mode-notify');
    await saveCurrent({update:{mode:'notify'}});
    await wait('document.querySelector("#field-update-require_signature").disabled','the web cannot turn signature checks off');
    // 9. Reset: up to date, no dot, so later sections and checks see a quiet System item.
    updateReply=updateAccept;
    update.releases=[updateRelease('1.3.0-1','2026-11-20T08:00:00Z','installed'),updateRelease('1.2.0-1','2026-11-05T08:00:00Z','older'),
      updateRelease('1.1.0-1','2026-10-26T08:00:00Z','older')];
    update.install=null;update.history=[];update.rollback={available:false,version:null,running:false};
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('!document.querySelector("#main-nav .nav-update-dot")','the reset state leaves no dot');
    // 10. A status request that fails once and an unavailable updater keep the page honest.
    const getsBeforeUnavailable=updateGets;
    updateGetFailures=1;
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    for(let i=0;i<400&&updateGets===getsBeforeUnavailable;i++)await delay(30);
    assert.equal(update503Count,1,'GET /api/update answered 503 once');
    assert.ok(await evaluate('!!document.querySelector("#update-installed-version")'),'a failed status request keeps the last update data');
    update.units_error='unavailable';
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await waitLong('document.querySelector("#update-installed")?.textContent.includes("El actualizador no está disponible en este equipo.")','the unavailable updater is explained',12);
    assert.ok(await evaluate('document.querySelector("#update-check").disabled'),'check is disabled when the updater is unavailable');
    assert.ok(await evaluate('document.querySelector(\'[data-update-version="1.2.0-1"][data-update-kind="downgrade"]\').disabled'),'release actions are disabled when the updater is unavailable');
    update.units_error=null;
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await waitLong('!document.querySelector("#update-installed")?.textContent.includes("El actualizador no está disponible en este equipo.")&&!document.querySelector("#update-check").disabled','the updater actions return when it is available again',12);

    status.network={...status.network,route_metric:99,requested_route_metric:100,route_policy:'primary'};
    await route('/modem/');await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('document.body.innerText.includes("Métrica 99")','dashboard displays the effective routing metric');
    status.network={...status.network,route_metric:101,connectivity:{enabled:true,state:'checking',route_mode:'fallback',preferred:false}};
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('document.body.innerText.includes("Comprobando salida · Respaldo")','an unverified modem stays in backup');
    status.network={...status.network,route_metric:99,connectivity:{enabled:true,state:'up',route_mode:'requested',preferred:true}};
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('document.body.innerText.includes("Salida verificada · Preferente")','fresh positive TCP checks allow preference');
    status.network={...status.network,route_metric:101,connectivity:{enabled:true,state:'down',route_mode:'fallback',preferred:false}};
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('document.body.innerText.includes("Verificación fallida · Respaldo")','ONLINE never masks failed upstream checks');
    status.network={...status.network,connectivity:{enabled:true,state:'up',route_mode:'fallback',preferred:false,dns_error:'DNS installation failed'}};
    await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
    await wait('document.body.innerText.includes("Error de DNS · Respaldo")','DNS application errors keep the working fallback');
    assert.ok(!(await evaluate('!!document.querySelector(".metric-stat-connected")')),'a DNS failure never appears as a healthy cellular connection');
    status.network={...status.network,route_metric:99,connectivity:{enabled:true,state:'up',route_mode:'requested',preferred:true}};
    await section('modem','verification');
    await input('#field-network-connectivity_targets','localhost:443,');
    await wait('document.querySelector("#field-network-connectivity_targets").getAttribute("aria-invalid")==="true"&&document.querySelectorAll(".chip").length===3','invalid probe destination becomes a chip with a field error');
    assert.ok(await evaluate('document.querySelector(".chip-invalid")?.textContent.includes("localhost:443")'),'only the rejected target is marked');
    assert.ok(await evaluate('document.querySelector("#global-save").disabled'),'invalid connectivity targets cannot be saved');
    await click('.chip-invalid button');
    await assertSaveBar(false);
    await section('modem','capture');await click('#field-telemetry-throughput');await section('modem','gps');await click('#field-telemetry-gps');await click('#field-telemetry-gps_source-modem');
    await saveCurrent({telemetry:{throughput:true,gps:true,gps_source:'modem'}});
    status.sample.collection.interface_traffic={enabled:true,status:'ok',interface:'wwan0',rate_unit:'Kibit/s',rx_bytes:4096,tx_bytes:2048};status.sample.collection.gps={enabled:true,source:'qmi_loc',status:'fixed'};
    Object.assign(status.sample.dynamic,{throughput_download_speed_kbps:0,throughput_upload_speed_kbps:12.5,gnss_latitude:0,gnss_longitude:-1.123456789,gnss_horizontal_uncertainty_m:2.5,gnss_timestamp_utc:'2026-10-21T12:00:00.123Z'});
    for(const point of status.history)Object.assign(point,{throughput_download_speed_kbps:0,throughput_upload_speed_kbps:12.5});
    await route('/metrics/data/');await wait('!!document.querySelector("#metrics-search")','QMI search');await wait('document.body.innerText.includes("-1.123456789")','GPS precision');
    assert.equal(await evaluate('document.querySelectorAll("#subnav a").length'),5,'each modem has summary, metrics, configuration, diagnostics and tools');assert.equal(await evaluate('document.querySelectorAll("#view canvas").length'),0,'data tab never renders charts');assert.ok(await evaluate('document.querySelectorAll("#view table").length>0'),'data tables remain available');
    await input('#metrics-search','RSRP');await evaluate('document.querySelector("#metrics-search").focus();document.querySelector("#metrics-search").setSelectionRange(2,2)');await delay(2200);assert.equal(await evaluate('document.activeElement.id'),'metrics-search');assert.equal(await evaluate('document.querySelector("#metrics-search").selectionStart'),2);
    await input('#metrics-search','');
    await evaluate(`(()=>{const d=Array.from(document.querySelectorAll('#view details')).find(e=>e.querySelector('summary')?.textContent.includes('Última muestra'));d.open=true;window.originalDetails=d;window.originalPre=d.querySelector('pre');const n=originalPre.querySelector('span')?.firstChild||originalPre.firstChild;const range=document.createRange();range.setStart(n,0);range.setEnd(n,40);getSelection().removeAllRanges();getSelection().addRange(range);window.originalSelection=getSelection().toString();window.scrollTo(0,d.offsetTop+80);window.originalScroll=scrollY})()`);
    await delay(4300);assert.ok(await evaluate('originalDetails.isConnected&&originalDetails.open'));assert.ok(await evaluate('getSelection().toString()===originalSelection'),'selection survives live JSON changes');assert.ok(await evaluate('Math.abs(scrollY-originalScroll)<2'),'scroll survives polling');await evaluate('getSelection().removeAllRanges();scrollTo(0,0)');await screenshot('data');
    await route('/metrics/charts/');await click('#metrics-chart-signal');await hover('canvas','dBm');assert.equal(await evaluate('document.querySelectorAll("#view table").length'),0,'graphs tab excludes data tables');
    assert.ok(await evaluate(`(()=>{const legends=[...document.querySelectorAll('.signal-legend')];return legends.length>0&&legends.every(l=>{
      const items=[...l.querySelectorAll(':scope > span')], box=l.getBoundingClientRect();
      if(items.length!==4)return false;
      if(items.some(n=>{const b=n.getBoundingClientRect();return b.left<box.left-1||b.right>box.right+1||b.bottom>box.bottom+1}))return false;
      for(let i=0;i<items.length;i++)for(let j=i+1;j<items.length;j++){
        const a=items[i].getBoundingClientRect(),b=items[j].getBoundingClientRect();
        if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>0&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>0)return false;
      }
      const first=items[0].getBoundingClientRect(),second=items[1].getBoundingClientRect();
      return second.left-first.left>=4||second.top-first.top>=4;
    })})()`),
      'the signal quality legend stays inside its card with space between levels');await screenshot('signal');
    await assertSimplifiedShell();
    await click('#metrics-chart-performance');await wait('document.body.innerText.includes("CPU")','CPU metrics');assert.equal(await evaluate('document.querySelectorAll("#view table").length'),0,'performance graphs exclude data tables');
    await evaluate(`(()=>{const p=Array.from(document.querySelectorAll('section.panel')).find(e=>e.querySelector('h2')?.textContent==='Duración de adquisición');const c=p.querySelector('canvas');c.focus();c.dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true}))})()`);
    await wait(`Array.from(document.querySelectorAll('.metric-chart-tooltip')).some(e=>!e.hidden&&e.textContent.includes('ms'))`,'keyboard metric tooltip');await screenshot('performance');
    const layoutAudit=()=>evaluate(`(()=>{
      const issues=[];
      if(document.documentElement.scrollWidth>innerWidth+1)issues.push('document overflow '+document.documentElement.scrollWidth+'/'+innerWidth);
      for(const e of document.querySelectorAll('.main-content,.topbar,.page-heading,.panel,.metric-grid,.chart-grid,.widget-board,.widget,.subnav,.save-bar,.config-sections,.config-workbench,.config-subnav,.config-subsection,.metric-chart-canvas,.toast')){
        const s=getComputedStyle(e),r=e.getBoundingClientRect();if(s.display==='none'||!r.width)continue;
        if(r.left < -1 || r.right > innerWidth+1)issues.push(e.className+' outside viewport '+Math.round(r.left)+'..'+Math.round(r.right));
      }
      const actions=Array.from(document.querySelectorAll('#global-save,#global-discard'));
      for(const e of actions){const r=e.getBoundingClientRect();if(r.left<0||r.right>innerWidth+1)issues.push(e.id+' is clipped horizontally');}
      const bar=document.querySelector('#save-bar');
      if(bar&&getComputedStyle(bar).position!=='sticky')issues.push('save bar must stick to the bottom while scrolling');
      const workbench=document.querySelector('.config-workbench'),navigation=document.querySelector('#config-subnav-list'),form=document.querySelector('.config-sections'),toggle=document.querySelector('#config-subnav-toggle');
      if(workbench&&navigation&&form){
        const w=workbench.getBoundingClientRect(),n=navigation.getBoundingClientRect(),f=form.getBoundingClientRect();
        const shown=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
        if(form.querySelectorAll('.panel').length!==1)issues.push('configuration must have one form card');
        if(navigation.closest('.panel'))issues.push('configuration navigation must sit outside the form card');
        if(navigation.getAttribute('role')!=='tablist'||navigation.getAttribute('aria-orientation')!=='vertical')issues.push('configuration menu is not a vertical tablist');
        if(!toggle||toggle.getAttribute('aria-controls')!==navigation.id)issues.push('configuration toggle must control the menu');
        if(innerWidth>800){
          if(shown(toggle))issues.push('desktop configuration toggle must be hidden');
          if(!shown(navigation))issues.push('desktop configuration navigation must remain visible');
          if(n.right>f.left-8)issues.push('desktop configuration navigation and form must occupy separate columns');
          for(let e=navigation;e&&e!==workbench;e=e.parentElement){
            const s=getComputedStyle(e);
            if(!['transparent','rgba(0, 0, 0, 0)'].includes(s.backgroundColor)||s.boxShadow!=='none'||['Top','Right','Bottom','Left'].some(side=>parseFloat(s['border'+side+'Width'])>0))issues.push('desktop configuration navigation has a frame');
          }
          const tabs=Array.from(navigation.querySelectorAll('[role="tab"]'));
          for(let index=1;index<tabs.length;index++)if(tabs[index].getBoundingClientRect().top<tabs[index-1].getBoundingClientRect().bottom-1)issues.push('desktop configuration menu is not vertical');
        }else{
          if(!shown(toggle))issues.push('mobile configuration toggle must be visible');
          else if(f.top<toggle.getBoundingClientRect().bottom-1)issues.push('mobile form must follow its section toggle');
          if(Math.abs(f.left-w.left)>2||Math.abs(f.width-w.width)>2)issues.push('mobile configuration form must use the full workbench width');
          const expanded=toggle?.getAttribute('aria-expanded')==='true';
          if(expanded!==shown(navigation))issues.push('mobile configuration menu visibility disagrees with aria-expanded');
          if(expanded&&n.bottom>f.top+1)issues.push('mobile configuration menu must stack above the form');
          if(!expanded&&Array.from(navigation.querySelectorAll('[role="tab"]')).some(shown))issues.push('collapsed configuration tabs must be hidden');
        }
      }
      const sidebar=document.querySelector('.sidebar'),brand=document.querySelector('.brand'),copy=document.querySelector('.brand-copy');
      if(sidebar&&brand){const side=sidebar.getBoundingClientRect();if(side.left>=-1){
        const bounds=brand.getBoundingClientRect();
        if(bounds.left<side.left-1||bounds.right>side.right+1)issues.push('brand leaves sidebar');
        if(!copy)issues.push('brand-copy is missing');
        for(const label of brand.querySelectorAll('.brand-copy,strong,small')){const r=label.getBoundingClientRect();if(r.left<bounds.left-1||r.right>bounds.right+1)issues.push('brand text leaves its container');if(label.clientWidth&&label.scrollWidth>label.clientWidth+1)issues.push('brand text is clipped');}
        if(brand.querySelector('small'))issues.push('brand subtitle must be absent');
      }}
      // On narrow screens notices rise above the sticky save bar instead of covering its buttons.
      const region=document.querySelector('.toast-region');if(region&&innerHeight-region.getBoundingClientRect().bottom>(bar&&innerWidth<=1023?160:32))issues.push('toast incorrectly offset');
      // The signal quality legend must reflow with its card, never overflow it or crowd its levels.
      for(const legend of document.querySelectorAll('.signal-legend')){
        const box=legend.getBoundingClientRect(),items=[...legend.querySelectorAll(':scope > span')];
        for(const item of items){const b=item.getBoundingClientRect();if(b.left<box.left-1||b.right>box.right+1)issues.push('signal legend overflows its card');if(item.scrollWidth>item.clientWidth+1)issues.push('signal legend text is clipped');}
        for(let i=0;i<items.length;i++)for(let j=i+1;j<items.length;j++){const a=items[i].getBoundingClientRect(),b=items[j].getBoundingClientRect();
          if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>0&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>0)issues.push('signal legend levels overlap');}
      }
      return issues;
    })()`);
    const matrixPages=['/modem/','/metrics/charts/','/metrics/data/','/configuration/modem/','/configuration/web/'];
    for(const width of (quick?[390,1280]:[320,360,390,600,768,820,1024,1280,1440,1920,2560])){
      const height=width<768?844:width<1280?1024:1080;
      await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<768});
      await section('modem','connection');await input('#field-modem-apn','matrix-'+width+'.example');await click('#global-discard');
      await wait('document.querySelector("#notification")?.textContent.includes("descartados")','responsive toast fixture');
      await assertSaveBar(false);
      await input('#field-modem-apn','matrix-pending-'+width+'.example');await assertSaveBar(true);
      const mobileNavigation=await evaluate('getComputedStyle(document.querySelector(".topbar .mobile-menu")).display!=="none"');
      if(mobileNavigation){
        await evaluate('document.querySelector(".topbar .mobile-menu").focus()');
        await click('.topbar .mobile-menu');await wait('document.querySelector(".topbar .mobile-menu").getAttribute("aria-expanded")==="true"','responsive menu opens');
        await wait('document.querySelector(".sidebar").getBoundingClientRect().left>=-1','responsive sidebar transition finishes');
        assert.deepEqual(await layoutAudit(),[],width+'px open mobile sidebar and brand');
        await wait('document.activeElement.closest("#main-sidebar")!==null','mobile menu receives keyboard focus');
        await evaluate(`(()=>{window.menuFocusables=Array.from(document.querySelectorAll('#main-sidebar a[href],#main-sidebar button:not([disabled])')).filter(e=>getComputedStyle(e).visibility!=='hidden'&&e.getBoundingClientRect().width>0);menuFocusables.at(-1).focus()})()`);
        await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
        await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
        await wait('document.activeElement===menuFocusables[0]','Tab stays inside mobile navigation');
        await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9,modifiers:8});
        await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9,modifiers:8});
        await wait('document.activeElement===menuFocusables.at(-1)','Shift+Tab stays inside mobile navigation');
        await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
        await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
        await wait('document.querySelector(".topbar .mobile-menu").getAttribute("aria-expanded")==="false"','Escape closes responsive menu');
        await wait('document.activeElement===document.querySelector(".topbar .mobile-menu")','closing mobile navigation restores keyboard focus');
        await click('.topbar .mobile-menu');
        await wait('!!document.querySelector(".sidebar-close")','drawer has an explicit close control');
        await click('.sidebar-close');
        await wait('document.activeElement===document.querySelector(".topbar .mobile-menu")','explicit close restores keyboard focus');
        if(width===820){
          await click('.topbar .mobile-menu');await wait('document.querySelector(".sidebar").classList.contains("is-open")','drawer opens before desktop resize');
          await send('Emulation.setDeviceMetricsOverride',{width:1280,height,deviceScaleFactor:1,mobile:false});
          await wait('!document.querySelector(".sidebar").classList.contains("is-open")&&!document.querySelector(".main-workspace").inert','desktop resize releases the content');
          await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
        }
      }
      for(const page of matrixPages){
        await route(page);await delay(60);
        await evaluate('document.querySelectorAll("#view details").forEach(e=>{e.open=true})');await delay(30);
        const issues=await layoutAudit();assert.deepEqual(issues,[],width+'px '+page);
        await assertSaveBar(page==='/configuration/modem/');await assertSimplifiedShell();
        if(page.startsWith('/configuration/')){
          const tab=page.split('/')[2];
          for(const name of configSections[tab]){await section(tab,name);assert.deepEqual(await layoutAudit(),[],width+'px '+tab+'/'+name);await assertSaveBar(tab==='modem');}
          if(width<=800){
            await openConfigNavigation();assert.deepEqual(await layoutAudit(),[],width+'px '+tab+' open section selector');
            if(width===390&&tab==='modem'){
              await evaluate('document.querySelector("#config-subnav-connection").focus()');
              await send('Input.dispatchKeyEvent',{type:'keyDown',key:'ArrowDown',code:'ArrowDown',windowsVirtualKeyCode:40});
              await send('Input.dispatchKeyEvent',{type:'keyUp',key:'ArrowDown',code:'ArrowDown',windowsVirtualKeyCode:40});
              await wait('document.activeElement.id==="config-subnav-routing"&&document.activeElement.getAttribute("aria-selected")==="true"','mobile keyboard selects the next vertical section');
              assert.equal(await evaluate('document.querySelector("#config-subnav-toggle").getAttribute("aria-expanded")'),'true','arrow navigation keeps its tablist open');
              const beforeMenuPoll=requests;await delay(2200);assert.ok(requests>beforeMenuPoll,'open configuration navigation never pauses status polling');
              assert.equal(await evaluate('document.activeElement.id'),'config-subnav-routing','polling preserves the active vertical tab focus');
              await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
              await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
              await wait('document.querySelector("#config-subnav-toggle").getAttribute("aria-expanded")==="false"&&document.activeElement.id==="config-subnav-toggle"','Escape closes section selector and restores focus');
              await section('modem','connection');assert.equal(await evaluate('document.querySelector("#field-modem-apn").value'),'matrix-pending-390.example','mobile section changes and refresh preserve pending configuration');
              await openConfigNavigation();await evaluate('document.querySelector(".config-subnav [aria-selected=true]").focus()');
              await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r',unmodifiedText:'\r'});
              await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
              await wait('document.querySelector("#config-subnav-toggle").getAttribute("aria-expanded")==="false"&&document.activeElement.id==="config-subnav-toggle"','Enter selects a section and closes its mobile menu');
              await openConfigNavigation();
            }
            const active=await evaluate('document.querySelector(".config-subnav [aria-selected=true]").id');
            await click('#'+active);await wait('document.querySelector("#config-subnav-toggle").getAttribute("aria-expanded")==="false"&&document.activeElement.id==="config-subnav-toggle"','choosing the active section also closes the mobile menu and restores focus');
            assert.deepEqual(await layoutAudit(),[],width+'px '+tab+' closed section selector');
            if(width===390&&tab==='modem'){
              const panel=await evaluate('document.querySelector(".config-subnav [aria-selected=true]").getAttribute("aria-controls")');
              await evaluate('document.querySelector("#config-subnav-toggle").focus()');
              await send('Emulation.setDeviceMetricsOverride',{width:801,height,deviceScaleFactor:1,mobile:false});
              await wait(`document.querySelector('#config-subnav-list').getClientRects().length>0&&document.activeElement.id===${JSON.stringify(active)}`,'desktop resize moves focus from the hidden toggle to the selected tab');
              assert.ok(!(await visible('#config-subnav-toggle')),'desktop section toggle is hidden after resizing');
              assert.equal(await evaluate('document.querySelector(".config-subsection").id'),panel,'desktop resize preserves the selected configuration section');
              assert.deepEqual(await layoutAudit(),[],'801px vertical configuration layout');await assertSaveBar(true);
              await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:true});
              await wait('document.activeElement.id==="config-subnav-toggle"&&document.querySelector("#config-subnav-list").getClientRects().length===0','mobile resize returns focus before hiding its selected tab');
              assert.equal(await evaluate('document.querySelector(".config-subsection").id'),panel,'mobile resize preserves the selected configuration section');
              assert.equal(await evaluate('document.querySelector("#field-modem-apn").value'),'matrix-pending-390.example','responsive focus changes preserve the pending form value');
              assert.deepEqual(await layoutAudit(),[],'390px configuration after desktop resize');await assertSaveBar(true);
            }
          }
        }
        if(page==='/metrics/charts/'){
          for(const chart of ['signal','performance']){await click('#metrics-chart-'+chart);assert.deepEqual(await layoutAudit(),[],width+'px charts '+chart);}
        }
      }
    }
    await send('Emulation.setDeviceMetricsOverride',{width:2560,height:1440,deviceScaleFactor:1,mobile:false});
    await route('/metrics/charts/');await click('#metrics-chart-performance');
    assert.ok(await evaluate('document.querySelector(".main-content").getBoundingClientRect().width>1700'),'main container is wider on large screens');
    assert.ok(await evaluate('(()=>{const top=e=>Math.round(e.getBoundingClientRect().top),panel=Array.from(document.querySelectorAll("section.panel")).find(e=>e.querySelector("h2")?.textContent==="Duración de adquisición");return Array.from(document.querySelectorAll(".widget-board .widget")).filter(e=>top(e)===top(panel.closest(".widget"))).length>=3})()'),'a row of four charts shows them side by side on wide screens');
    await screenshot('wide');
    if(await evaluate('!!document.querySelector(".toast-close")')){await click('.toast-close');assert.ok(!(await evaluate('!!document.querySelector("#notification")')),'notices also close manually');}
    await send('Emulation.setDeviceMetricsOverride',{width:820,height:1180,deviceScaleFactor:1,mobile:false});await section('modem','capture');await screenshot('tablet');
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
    freshness={heartbeat_age_ms:30000,snapshot_age_ms:10,sample_age_ms:30000,state_fresh:false};await route('/modem/');await wait('document.body.innerText.includes("Estado sin confirmar")','dashboard reports a stale supervisor');
    await evaluate(`Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'))`);await delay(100);const hiddenRequests=requests;await delay(2250);assert.equal(requests,hiddenRequests,'hidden tab stops polling');
    freshness={heartbeat_age_ms:100,snapshot_age_ms:100,sample_age_ms:100,state_fresh:true};await evaluate(`Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'))`);await wait('!document.body.innerText.includes("Estado sin confirmar")','visible tab resumes and dashboard confirms the connection');await assertSimplifiedShell();
    const gpsMeta=status.sample.collection.gps;
    status.sample.collection.gps={...gpsMeta,enabled:false,status:'disabled'};await click('.refresh-button');
    const sameRow=`(()=>{const cells=document.querySelector('#view .metric-cell')?.closest('.panel'),traffic=document.querySelector('#view .metric-traffic-values')?.closest('.panel');return !!cells&&!!traffic&&Math.round(cells.getBoundingClientRect().top)===Math.round(traffic.getBoundingClientRect().top)})()`;
    await wait(sameRow,'one serving cell shares its row with the traffic panel');
    let panelRow=await evaluate(`[document.querySelector('#view .metric-cell').closest('.panel'),document.querySelector('#view .metric-traffic-values').closest('.panel')].map(panel=>Math.round(panel.getBoundingClientRect().top))`);
    assert.equal(new Set(panelRow).size,1,'one serving cell puts traffic on the same row');
    const panelHeights=await evaluate(`[document.querySelector('#view .metric-cell').closest('.panel'),document.querySelector('#view .metric-traffic-values').closest('.panel')].map(panel=>Math.round(panel.getBoundingClientRect().height))`);
    assert.equal(new Set(panelHeights).size,1,'serving cells and traffic share the row height');
    status.radio.cells.push({...status.radio.cells[0],pci:5});await click('.refresh-button');
    await wait(`document.querySelectorAll("#view .metric-cell").length===2&&!${sameRow}`,'a second serving cell moves the traffic panel to its own row');
    panelRow=await evaluate(`[document.querySelector('#view .metric-cell').closest('.panel'),document.querySelector('#view .metric-traffic-values').closest('.panel')].map(panel=>Math.round(panel.getBoundingClientRect().top))`);
    assert.equal(new Set(panelRow).size,2,'two serving cells keep one column per panel');
    status.radio.cells.pop();status.sample.collection.gps=gpsMeta;await click('.refresh-button');
    await wait('document.querySelectorAll("#view .metric-cell").length===1','serving cells restored');
    // Card arrangement: rows of one to four cards, mouse, keyboard and the hidden tray, kept per view in this browser.
    const liveCards=()=>evaluate(`Array.from(document.querySelectorAll('#view .widget-board[data-board] [data-widget]')).map(e=>e.dataset.widget)`);
    const liveRows=()=>evaluate(`Array.from(document.querySelectorAll('#view .widget-board[data-board] > .widget-row')).map(r=>r.dataset.columns+':'+Array.from(r.children).map(e=>e.dataset.widget).join(','))`);
    const tiles=()=>evaluate(`Array.from(document.querySelectorAll('.widget-rows [data-widget]')).map(e=>e.dataset.widget)`);
    const editorRows=()=>evaluate(`Array.from(document.querySelectorAll('.widget-rows [data-drop^="row:"]')).map(r=>r.dataset.columns+':'+Array.from(r.querySelectorAll('[data-widget]')).map(e=>e.dataset.widget).join(','))`);
    const hiddenTiles=()=>evaluate(`Array.from(document.querySelectorAll('[data-drop=widget-tray] > [data-widget]')).map(e=>e.dataset.widget)`);
    const sameLine=ids=>evaluate(`new Set(${JSON.stringify(ids)}.map(id=>Math.round(document.querySelector('#view [data-widget='+id+']').getBoundingClientRect().top))).size===1`);
    const fillsRow=id=>evaluate(`Math.abs(document.querySelector('#view [data-widget=${id}]').getBoundingClientRect().width-document.querySelector('#view .widget-board').getBoundingClientRect().width)<2`);
    const press=async(key,code,keyCode)=>{for(const type of ['keyDown','keyUp'])await send('Input.dispatchKeyEvent',{type,key,code,windowsVirtualKeyCode:keyCode,nativeVirtualKeyCode:keyCode});await delay(220);};
    const pointAt=(selector,fx=.5,fy=.5)=>evaluate(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return{x:r.x+r.width*${fx},y:r.y+r.height*${fy}}})()`);
    const pageCards=await liveCards(),pageRows=await liveRows();
    assert.deepEqual(pageCards.slice(0,3),['status','address','sample'],'the summary opens with its stat cards');
    assert.equal(pageRows[0],'3:status,address,sample','the stat cards share a row of three');
    assert.ok(await sameLine(['status','address','sample']),'a row of three shows its cards side by side');
    await click('#customize-summary');await wait('!!document.querySelector(".widget-editor .widget-rows [data-widget]")','arrangement editor');
    assert.deepEqual((await tiles()).filter(id=>pageCards.includes(id)),pageCards,'the editor starts from the page arrangement');
    assert.equal(await evaluate('document.activeElement?.id'),'widget-editor-title','the editor announces itself');
    assert.deepEqual(await layoutAudit(),[],'arrangement editor layout');
    await evaluate('scrollTo(0,0)');
    const from=await pointAt('.widget-rows [data-widget=sample] .widget-tile-text'),to=await pointAt('.widget-rows [data-widget=status] .widget-tile-card',.3,.5);
    await send('Input.dispatchMouseEvent',{type:'mouseMoved',...from,button:'left',buttons:0});await send('Input.dispatchMouseEvent',{type:'mousePressed',...from,button:'left',buttons:1,clickCount:1});
    for(let step=1;step<=18;step++){await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:from.x+(to.x-from.x)*step/18,y:from.y+(to.y-from.y)*step/18,button:'left',buttons:1});await delay(30);}
    await delay(150);assert.ok(await evaluate('!!document.querySelector(".widget-overlay .is-overlay")&&!!document.querySelector(".widget-tile.is-placeholder")'),'a dragged card floats above its drop place');
    await send('Input.dispatchMouseEvent',{type:'mouseReleased',...to,button:'left',buttons:0,clickCount:1});await delay(350);
    let arranged=await tiles();assert.ok(arranged.indexOf('sample')<arranged.indexOf('status'),'dragging with the mouse moves a card: '+arranged);
    const networkBefore=arranged.indexOf('network');
    await evaluate('document.querySelector(".widget-rows [data-widget=network] .widget-handle").focus()');
    await press(' ','Space',32);await press('ArrowUp','ArrowUp',38);await press(' ','Space',32);await delay(300);
    arranged=await tiles();assert.ok(arranged.indexOf('network')<networkBefore,'the keyboard moves a card: '+arranged);
    assert.equal(await evaluate('document.activeElement?.closest("[data-widget]")?.dataset.widget'),'network','focus stays on the moved card');
    assert.equal((await editorRows())[0].split(',').length,3,'a card dropped on a full row trades places with one of its cards');
    await click('.widget-rows [data-widget=connection] [data-action=hide]');await wait('!!document.querySelector("[data-drop=widget-tray] [data-widget=connection]")','a hidden card waits in the tray');
    await click('.widget-rows > [data-row]:first-child input[value="2"]');
    let rowsNow=await editorRows();assert.ok(/^2:\w+,\w+$/.test(rowsNow[0])&&/^2:\w+$/.test(rowsNow[1]),'a narrower row continues its cards in a row below: '+rowsNow);
    await click('.widget-add-row [data-columns="4"]');assert.equal((await editorRows()).at(-1),'4:','a new row starts empty');
    assert.equal(await evaluate('document.querySelectorAll(".widget-rows > [data-row]:last-child .widget-slot").length'),4,'an empty row offers its free places');
    await click('[data-drop=widget-tray] [data-widget=connection] [data-action=show]');rowsNow=await editorRows();
    assert.equal(rowsNow.at(-1),'4:connection','a card shown again joins the last row with room');
    await evaluate('scrollTo(0,0)');await screenshot('arrange');
    const editedCards=(await tiles()).filter(id=>pageCards.includes(id));
    await click('#widget-done');await wait('!document.querySelector(".widget-editor")&&document.activeElement?.id==="customize-summary"','the editor closes back to its button');
    assert.deepEqual(await liveCards(),editedCards,'the page follows the new arrangement');
    const savedRows=await liveRows();
    assert.ok(savedRows[0].startsWith('2:')&&savedRows.at(-1)==='4:connection','the page keeps the rows: '+savedRows);
    assert.ok(await fillsRow('connection'),'a card alone in its row takes the whole width');
    await reload();await wait('!!document.querySelector("#customize-summary")','summary after reload');
    assert.deepEqual(await liveRows(),savedRows,'the arrangement survives a reload');
    await click('#customize-summary');await wait('!!document.querySelector("#widget-cancel")','editor again');
    await click('.widget-rows > [data-row]:first-child [data-action=row-remove]');assert.equal((await hiddenTiles()).length,2,'a removed row hides its cards');
    await click('#widget-cancel');await wait('!document.querySelector(".widget-editor")','cancel closes the editor');
    assert.deepEqual(await liveRows(),savedRows,'cancel keeps the saved arrangement');
    await click('#customize-summary');await wait('!!document.querySelector("#widget-restore:not(:disabled)")','restore is offered for a custom arrangement');
    await click('#widget-restore');await click('#widget-done');await wait('!document.querySelector(".widget-editor")','restored');
    assert.deepEqual(await liveRows(),pageRows,'the default arrangement comes back');
    assert.equal(await evaluate('localStorage.getItem("qmi-supervisor.widgets.summary")'),null,'the default arrangement is not stored');
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await delay(150);assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'mobile overflow');await screenshot('mobile');
    assert.ok(await fillsRow('status')&&await sameLine(['address','sample']),'phones keep the connection state across the row above the other stat cards');
    await click('.topbar .mobile-menu');await wait('document.querySelector(".sidebar").classList.contains("is-open")','mobile navigation');await click('.mobile-backdrop');
    await route('/tools/');await wait('!!document.querySelector("#tools-start-ping")&&document.querySelector("#tool-ping").getAttribute("aria-selected")==="true"','network tests open on ping');
    assert.deepEqual(await evaluate(`(()=>{const panel=document.querySelector('#tool-panel-ping:not([hidden]) .tools-panel'),head=panel?.querySelector('.config-card-header h2'),issues=[];if(!panel)return['missing ping card'];if(getComputedStyle(panel).padding!=='0px')issues.push('the tests card must follow the configuration card');if(panel.querySelectorAll('.setting-row').length<3)issues.push('test options use setting rows');if(!head||!head.getClientRects().length||head.getBoundingClientRect().width<20)issues.push('the card title stays visible on phones');const sw=panel.querySelector('#tools-interface');if(!sw||!sw.checked||sw.disabled)issues.push('the modem interface is known without fleet inventory');if(!panel.querySelector('.tools-actions #tools-start-ping.button'))issues.push('actions use the standard buttons');if(!document.querySelector('#tool-panel-iperf').hidden)issues.push('only the selected tool is shown');return issues})()`),[],'ping follows the configuration language');
    // The advanced preference opened in the configuration also lists every option of the tests.
    assert.ok(await evaluate('document.querySelector("#tools-advanced-ping")?.getAttribute("aria-expanded")==="true"&&!!document.querySelector("#tools-ping-family")&&!!document.querySelector("[data-row=tools-ping-behaviour]")&&!!document.querySelector("[data-row=tools-ping-output]")'),'every ping option is listed with the advanced options');
    assert.deepEqual(await layoutAudit(),[],'ping layout on a phone');
    await click('#tool-iperf');await wait('!document.querySelector("#tool-panel-iperf").hidden&&document.querySelector("#tool-panel-ping").hidden&&new URLSearchParams(location.search).get("tool")==="iperf"','iperf3 has its own tab');
    assert.ok(await evaluate('document.querySelector("#tools-iperf-mode-client").checked'),'iperf3 starts in client mode');
    assert.ok(await evaluate('document.querySelector("#tools-start-iperf").disabled'),'a client needs a server to measure against');
    await input('#tools-iperf-host','10.0.0.9');await wait('!document.querySelector("#tools-start-iperf").disabled','a server address enables the client test');
    assert.ok(await evaluate('["direction","protocol","bitrate","limit","parallel","interface"].every(name=>!!document.querySelector("#tools-iperf-"+name))'),'client options: direction, protocol, rate, end of the test, streams and interface');
    assert.ok(await evaluate('document.querySelector("#tools-iperf-counters").disabled&&!document.querySelector("#tools-iperf-nodelay").disabled'),'TCP leaves the UDP options off');
    await click('#tools-iperf-protocol-udp');await wait('document.querySelector("#tools-iperf-bitrate").placeholder==="1"&&!document.querySelector("#tools-iperf-counters").disabled&&document.querySelector("#tools-iperf-nodelay").disabled','UDP shows its default rate and swaps the protocol options');
    assert.ok(await evaluate('document.querySelector("#tools-iperf-burst").disabled'),'bursts wait for a target rate');
    await input('#tools-iperf-bitrate','10');await wait('!document.querySelector("#tools-iperf-burst").disabled','a target rate allows bursts');
    await click('#tools-iperf-limit-bytes');await wait('!!document.querySelector("#tools-iperf-bytes")&&!document.querySelector("#tools-iperf-duration")','the test can end after an amount of data');
    await click('#tools-advanced-iperf');
    await wait('document.querySelector("#tools-advanced-iperf").getAttribute("aria-expanded")==="false"&&!document.querySelector("[data-row=tools-iperf-buffers]")&&/\\d/.test(document.querySelector("#tools-advanced-iperf").innerText)','the card foot hides the advanced options again and counts them');
    assert.ok(await evaluate('!document.querySelector("[data-row=tools-iperf-udp-options]")&&!!document.querySelector("[data-row=tools-iperf-rate]")'),'only the basic options stay');
    await click('#tools-iperf-mode-server');await wait('!!document.querySelector("#tools-iperf-listen")&&!document.querySelector("#tools-iperf-host")','server mode only asks for its port');
    assert.ok(await evaluate('document.querySelector(".tools-commands").innerText.includes("iperf3 -c")'),'server mode tells what to run on the other device');
    assert.deepEqual(await layoutAudit(),[],'iperf3 layout on a phone');
    await click('#tools-advanced-iperf');await wait('document.querySelector("#tools-advanced-iperf").getAttribute("aria-expanded")==="true"','the advanced options return for the rest of the run');
    assert.deepEqual(await layoutAudit(),[],'every iperf3 server option fits a phone');
    await click('#tools-iperf-mode-client');await wait('!!document.querySelector("#tools-iperf-host")&&!!document.querySelector("[data-row=tools-iperf-pacing]")','client mode with every option');
    assert.deepEqual(await layoutAudit(),[],'every iperf3 client option fits a phone');
    await click('#tool-ping');
    // Coming back while a test runs opens that tool, in its mode and with its parameters.
    toolsStatus={running:true,kind:'iperf',detail:{mode:'server',port:5300},lines:['Server listening on 5300'],returncode:null,started:1,finished:null};
    await route('/modem/');await route('/tools/');
    await wait('document.querySelector("#tool-iperf").getAttribute("aria-selected")==="true"&&document.querySelector("#tools-iperf-mode-server")?.checked&&document.querySelector("#tools-iperf-listen")?.value==="5300"','a running iperf3 server reopens in its tab and mode');
    assert.ok(await evaluate('document.querySelector("#tools-start-iperf").disabled&&!document.querySelector("#tools-stop-iperf").disabled'),'the running test can be stopped, not started twice');
    toolsStatus=idleTools;await route('/modem/');await route('/tools/');
    await screenshot('tools');

    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});await section('modem','connection');await input('#field-modem-apn','tenten');await assertSaveBar(true);await evaluate('scrollTo(0,0)');await screenshot('configuration');
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await wait('document.querySelector("#config-subnav-toggle")?.getClientRects().length>0','mobile configuration selector');await evaluate('scrollTo(0,0)');await screenshot('configuration-mobile');
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
    await section('modem','connection');await input('#field-modem-apn','preserved-objective.example');
    await route('/modem/');
    const beforeObjectives=posts.length;
    await click('#objective-telemetry');await wait('!document.querySelector("#objective-telemetry").checked&&!document.querySelector("#objective-telemetry").disabled','monitoring stopped');
    assert.deepEqual(posts.at(-1),{config:{telemetry:{enabled:false}}});
    await wait('!document.querySelector("#view [data-widget=connection],#view [data-widget=network],#view [data-widget=cells]")','monitoring off hides the metric panels');
    assert.equal(await evaluate('!!document.querySelector("#view .metric-notice")'),false,'monitoring disabled leaves no notice in the summary body');
    assert.equal(await evaluate('!!document.querySelector(".attention-indicator")'),false,'an info-only issue never raises the indicator');
    assert.equal(await evaluate('document.querySelectorAll(".metric-signal-card").length'),0,'monitoring off hides the signal charts');
    await click('#objective-connection');await wait('!document.querySelector("#objective-connection").checked&&!document.querySelector("#objective-connection").disabled','connection stopped');
    assert.deepEqual(posts.at(-1),{config:{modem:{mode:'monitor'}}});
    status.features={connection:{enabled:false,pending:true,effective:true,cleanup_pending:true,state:'STOPPING',
      error:'Owned connection cleanup is pending; reconnect the assigned modem',retry_in_seconds:3}};
    await click('.refresh-button');
    const pendingLocale=await evaluate('document.documentElement.lang');
    await wait(`document.querySelector('#objective-state-connection')?.innerText.trim()===${JSON.stringify(pendingLocale==='en'?'Stopping':'Deteniendo')}`,'the connection card still renders while its release is pending');
    assert.ok(await evaluate(`(()=>{const card=document.querySelector('#objective-connection')?.closest('.objective-card');return !!card&&!!card.querySelector('#objective-state-connection')})()`),'the connection card renders while its release is pending');
    assert.equal(await evaluate(`(()=>{const card=document.querySelector('#objective-connection')?.closest('.objective-card');return !!card?.querySelector('.objective-error')})()`),false,'a pending release shows no error in the connection card');
    assert.equal(await evaluate(`(()=>{const card=document.querySelector('#objective-connection')?.closest('.objective-card');return /Retry in|Reintento en/.test(card?.innerText||'')})()`),false,'a pending release shows no retry line in the connection card');
    delete status.features;
    await click('#objective-connection');await wait('document.querySelector("#objective-connection").checked&&!document.querySelector("#objective-connection").disabled','connection enabled');
    await click('#objective-telemetry');await wait('document.querySelector("#objective-telemetry").checked&&!document.querySelector("#objective-telemetry").disabled','monitoring enabled');
    await wait('document.querySelectorAll(".metric-signal-card").length===3','signal charts return when monitoring resumes');
    assert.equal(posts.length,beforeObjectives+4);
    await section('modem','connection');assert.equal(await evaluate('document.querySelector("#field-modem-apn").value'),'preserved-objective.example');
    assert.ok(await evaluate('document.querySelector("#field-modem-mode-connect").checked'),'an untouched form follows quick control state');
    // Attention indicator, overlay panel and clean body (plans/05-ui-clean-dashboard.md).
    await route('/modem/');
    status=fixture();
    await click('.refresh-button');
    await wait('!document.querySelector(".attention-indicator")&&!!document.querySelector("#view .widget-board .widget")','healthy summary with widgets');
    // a. A healthy fixture shows no attention surface anywhere.
    assert.deepEqual(await evaluate(`(()=>({indicator:!!document.querySelector('.attention-indicator'),notice:!!document.querySelector('.metric-notice'),inline:!!document.querySelector('.metric-inline-error'),objective:!!document.querySelector('.objective-error')}))()`),{indicator:false,notice:false,inline:false,objective:false},'a healthy fixture shows no attention surface');
    // b. The I1 diagnosis raises a bad indicator and an overlay panel without touching the body or the layout.
    const i1={code:'recovery.saved_client_unanswered',severity:'error',active:true,step:'OPEN',operation:'open.recover.saved_stop',attempts:3,
      failing_since_utc:'2026-11-20T10:00:00+00:00',detail:'Previous owned bearer stop was not confirmed'};
    status={...fixture(),diagnosis:{...i1},features:{connection:{enabled:true,state:'BACKOFF'}}};
    await click('.refresh-button');
    await wait('!!document.querySelector(".attention-indicator.tone-bad")','the diagnosis raises a bad attention indicator');
    assert.equal(await evaluate('!!document.querySelector("#view .metric-notice,#view .metric-inline-error,#view .objective-error")'),false,'the diagnosis stays out of the summary body');
    assert.equal(await evaluate('document.querySelector(".attention-indicator").getAttribute("aria-haspopup")'),'dialog','the indicator offers a dialog');
    const widgetBox=await evaluate(`(()=>{const e=document.querySelector('#view .widget-board .widget');if(!e)return null;const r=e.getBoundingClientRect();return[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]})()`);
    assert.ok(widgetBox,'the summary renders its first widget');
    await screenshot('attention-closed');
    await click('.attention-indicator');
    await wait('!!document.querySelector("#attention-panel")','the attention panel opens');
    assert.equal(await evaluate('getComputedStyle(document.querySelector("#attention-panel")).position'),'fixed','the panel overlays the page instead of pushing it');
    assert.equal(await evaluate('document.activeElement===document.querySelector(".attention-close")'),true,'focus moves into the panel');
    const attentionLocale=await evaluate('document.documentElement.lang');
    assert.ok(await evaluate(`document.querySelector('#objective-state-connection')?.textContent.includes(${JSON.stringify(attentionLocale==='en'?'Needs attention':'Requiere atención')})`),'the connection objective points to the attention panel');
    assert.ok(await evaluate(`document.querySelector('#objective-state-connection')?.classList.contains('tone-bad')`),'the connection state takes the error tone');
    const attentionTitle=attentionLocale==='en'?'The modem does not answer the data session left by a previous run':'El módem no responde a la sesión de datos que dejó una ejecución anterior';
    const attentionAction=attentionLocale==='en'?'If no other program uses this modem, unplug and replug it, power-cycle it or reboot the host.':'Si ningún otro programa usa este módem, desenchúfalo y vuelve a enchufarlo, apágalo y enciéndelo o reinicia el equipo.';
    const attentionLabel=attentionLocale==='en'?'What to do':'Qué hacer';
    const failingPrefix=attentionLocale==='en'?'Failing since':'Fallando desde';
    const attemptText=attentionLocale==='en'?'(3 attempts)':'(3 intentos)';
    assert.ok(await evaluate(`document.querySelector('#attention-panel .attention-issue h3')?.textContent.includes(${JSON.stringify(attentionTitle)})`),'the panel shows the translated title');
    assert.ok(await evaluate(`document.querySelector('#attention-panel .attention-action-label')?.textContent.includes(${JSON.stringify(attentionLabel)})&&document.querySelector('#attention-panel .attention-action p')?.textContent.includes(${JSON.stringify(attentionAction)})`),'the panel shows what to do with the translated action');
    assert.ok(await evaluate(`(()=>{const text=document.querySelector('#attention-panel .attention-meta')?.textContent||'';return text.includes(${JSON.stringify(failingPrefix)})&&text.includes(${JSON.stringify(attemptText)})})()`),'the panel shows when the failure started and how many attempts it took');
    const widgetBoxOpen=await evaluate(`(()=>{const e=document.querySelector('#view .widget-board .widget');if(!e)return null;const r=e.getBoundingClientRect();return[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]})()`);
    assert.deepEqual(widgetBoxOpen,widgetBox,'opening the panel never moves the dashboard cards');
    await screenshot('attention-open');
    await evaluate('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
    await wait('!document.querySelector("#attention-panel")&&document.activeElement===document.querySelector(".attention-indicator")','Escape closes the panel and returns focus to the indicator');
    // The close button and a click outside close it too, returning focus.
    await click('.attention-indicator');await wait('!!document.querySelector("#attention-panel")','the panel reopens');
    await click('.attention-close');
    await wait('!document.querySelector("#attention-panel")&&document.activeElement===document.querySelector(".attention-indicator")','the close button closes and returns focus');
    await click('.attention-indicator');await wait('!!document.querySelector("#attention-panel")','the panel reopens again');
    await evaluate('document.body.dispatchEvent(new MouseEvent("mousedown",{bubbles:true}))');
    await wait('!document.querySelector("#attention-panel")','a click outside closes the panel');
    // Phones get an icon-only trigger and a bottom sheet.
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    await click('.attention-indicator');await delay(200);
    await wait('!!document.querySelector("#attention-panel")','the phone panel opens');
    assert.deepEqual(await evaluate(`(()=>{const p=document.querySelector('#attention-panel'),r=p.getBoundingClientRect(),label=document.querySelector('.attention-indicator-label');return {bottomSheet:Math.abs(r.bottom-innerHeight)<=2&&r.width>=380,iconOnly:label?getComputedStyle(label).display==='none':true,overflow:document.documentElement.scrollWidth<=innerWidth+1}})()`),{bottomSheet:true,iconOnly:true,overflow:true},'phones get a full-width bottom sheet and an icon-only trigger');
    await evaluate('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
    await wait('!document.querySelector("#attention-panel")','the phone panel closes');
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
    await delay(150);
    // c. An older backend without a diagnosis still raises the indicator; the raw error stays in the technical detail.
    status={...fixture(),error:'Previous owned bearer stop was not confirmed'};
    delete status.diagnosis;
    await click('.refresh-button');
    await wait('!!document.querySelector(".attention-indicator")','an older backend error raises the attention indicator');
    await click('.attention-indicator');
    await wait('!!document.querySelector("#attention-panel .attention-technical")','the generic issue carries technical detail');
    const rawError='Previous owned bearer stop was not confirmed';
    assert.equal(await evaluate(`document.querySelector('#attention-panel').innerText.includes(${JSON.stringify(rawError)})`),false,'the raw error stays hidden while the technical detail is collapsed');
    await click('#attention-panel .attention-technical summary');
    await wait('document.querySelector("#attention-panel .attention-technical")?.open===true','the technical detail expands');
    const labelPrefix=attentionLocale==='en'?'Log label:':'Etiqueta en el registro:';
    assert.ok(await evaluate(`(()=>{const text=document.querySelector('#attention-panel .attention-technical-body')?.innerText||'';return text.includes(${JSON.stringify(labelPrefix)}+' test')})()`),'the technical detail carries the published log label');
    assert.deepEqual(await evaluate(`(()=>{const panel=document.querySelector('#attention-panel'),tech=panel.querySelector('.attention-technical'),raw=tech.querySelector('.attention-raw');return {inTechnical:!!raw&&raw.textContent.includes(${JSON.stringify(rawError)}),occurrences:panel.innerText.split(${JSON.stringify(rawError)}).length-1}})()`),{inTechnical:true,occurrences:1},'the raw error appears only inside the technical detail');
    await evaluate('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
    await wait('!document.querySelector("#attention-panel")','the technical panel closes');
    // d. A telemetry stall becomes an issue with its translated title, never the raw reason code.
    status=fixture();
    status.features={telemetry:{enabled:true,stall:{since_utc:'2026-11-20T10:00:00+00:00',reason_code:'modem_not_readable'}}};
    await click('.refresh-button');
    await wait('!!document.querySelector(".attention-indicator")','a telemetry stall raises the attention indicator');
    await click('.attention-indicator');
    await wait('!!document.querySelector("#attention-panel")','the stall issue opens');
    const stallLocale=await evaluate('document.documentElement.lang');
    const stallTitle=stallLocale==='en'?'Monitoring has stored no sample since':'La monitorización no guarda muestras desde';
    assert.ok(await evaluate(`(()=>{const text=document.querySelector('#attention-panel')?.innerText||'';return text.includes(${JSON.stringify(stallTitle)})&&!text.includes('modem_not_readable')})()`),'the stall shows its translated title and hides the raw reason code');
    await evaluate('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
    await wait('!document.querySelector("#attention-panel")','the stall panel closes');
    // d2. A state from a dead or hung worker never looks current: neutral chip, muted panel.
    freshness={heartbeat_age_ms:60000,snapshot_age_ms:60000,sample_age_ms:60000,state_fresh:false};
    status={...fixture(),diagnosis:{...i1},features:{connection:{enabled:true,state:'BACKOFF'}}};
    await click('.refresh-button');
    await wait('!!document.querySelector(".attention-indicator-stale")','a stale state shows the neutral chip');
    assert.equal(await evaluate('!!document.querySelector(".attention-indicator.tone-bad")'),false,'a stale diagnosis is never red');
    await click('.attention-indicator-stale');
    await wait('!!document.querySelector("#attention-panel.attention-panel-stale")','the stale panel opens muted');
    const observedLocale=await evaluate('document.documentElement.lang');
    assert.ok(await evaluate(`document.getElementById('attention-panel')?.querySelector('.attention-stale-note')?.textContent.includes(${JSON.stringify(observedLocale==='en'?'Observed':'Observado')})`),'the panel says how old the observation is');
    await evaluate('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
    await wait('!document.querySelector("#attention-panel")','the stale panel closes');
    freshness={heartbeat_age_ms:100,snapshot_age_ms:100,sample_age_ms:100,state_fresh:true};
    // e needs the I1 fixture restored before the locale/theme loop.
    status={...fixture(),diagnosis:{...i1},features:{connection:{enabled:true,state:'BACKOFF'}}};
    await click('.refresh-button');
    await wait('!!document.querySelector(".attention-indicator.tone-bad")','the I1 fixture returns for the locale loop');
    // Return to the configuration form the locale loop starts from.
    await section('modem','connection');
    for(const locale of (quick?['en']:['en','es'])){
      await input('#language-select',locale);await wait(`document.documentElement.lang===${JSON.stringify(locale)}`,'language changed');
      assert.equal(await evaluate('document.querySelector("#field-modem-apn").value'),'preserved-objective.example');
      for(const theme of (quick?['light']:['light','dark'])){
        if(await evaluate('document.documentElement.dataset.theme')!==theme)await click('#theme-toggle');
        await wait(`document.documentElement.dataset.theme===${JSON.stringify(theme)}`,'theme changed');
        for(const page of ['/modem/','/metrics/charts/','/metrics/data/','/configuration/modem/']){
          await route(page);assert.deepEqual(await layoutAudit(),[],locale+'/'+theme+' '+page);
          const background=await evaluate('getComputedStyle(document.querySelector(".sidebar")).backgroundColor');
          assert.equal(background,theme==='dark'?'rgb(20, 36, 49)':'rgb(255, 255, 255)');
          const expectedInk=theme==='dark'?'rgb(129, 159, 255)':'rgb(49, 93, 255)';
          await wait(`getComputedStyle(document.querySelector(".nav-link.active")).color===${JSON.stringify(expectedInk)}`,'active navigation maintains readable contrast');
          if(page==='/configuration/modem/')assert.ok(await evaluate(`document.querySelector('#draft-summary').textContent.includes(${JSON.stringify(locale==='en'?'1 change in Test modem':'1 cambio en Test modem')})`),'pending count names the modem and follows language');
          if(page==='/metrics/data/'){
            const badge=await evaluate(`document.querySelector('.metric-search-bar > .badge').textContent.trim()`);
            assert.match(badge,locale==='en'?/^\d+ fields?$/:/^\d+ campos?$/,'metric count badge follows language');
            const field=await evaluate(`(()=>{const svg=document.querySelector('.metric-search-field > div > svg'),input=document.querySelector('#metrics-search');
              const icon=svg.getBoundingClientRect(),box=input.getBoundingClientRect();
              return {iconRight:icon.right,textStart:box.left+parseFloat(getComputedStyle(input).paddingLeft)}})()`);
            assert.ok(field.textStart>=field.iconRight+2,'search icon never overlaps the placeholder');
          }
          if(page==='/modem/'){
            assert.deepEqual(await evaluate(`(()=>({notice:!!document.querySelector('#view .metric-notice'),inline:!!document.querySelector('#view .metric-inline-error'),objective:!!document.querySelector('#view .objective-error')}))()`),{notice:false,inline:false,objective:false},'the summary body stays clean with an active diagnosis '+locale+'/'+theme);
            await wait('!!document.querySelector(".attention-indicator.tone-bad")','the attention indicator exists '+locale+'/'+theme);
            await screenshot('attention-'+locale+'-'+theme+'-closed');
            await click('.attention-indicator');
            await wait('!!document.querySelector("#attention-panel")','the attention panel opens '+locale+'/'+theme);
            const title=locale==='en'?'The modem does not answer the data session left by a previous run':'El módem no responde a la sesión de datos que dejó una ejecución anterior';
            assert.ok(await evaluate(`document.querySelector('#attention-panel .attention-issue h3')?.textContent.includes(${JSON.stringify(title)})`),'the translated attention title '+locale);
            await screenshot('attention-'+locale+'-'+theme+'-open');
            await evaluate('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
            await wait('!document.querySelector("#attention-panel")','the attention panel closes '+locale+'/'+theme);
          }
        }
        await screenshot('configuration-'+locale+'-'+theme);
        // The update section follows the language and the theme like the other System sections.
        await section('web','updates');
        await wait('!!document.querySelector("#update-installed-version")','the update section opens '+locale+'/'+theme);
        assert.equal(await evaluate('document.querySelector("#update-installed-version").textContent'),'1.3.0-1','the installed version is shown '+locale+'/'+theme);
        assert.ok(await evaluate(`document.querySelector("#update-check").textContent.includes(${JSON.stringify(locale==='en'?'Check now':'Comprobar ahora')})`),'the check action follows the language '+locale+'/'+theme);
        assert.ok(await evaluate(`document.querySelector("#update-versions h3").textContent.includes(${JSON.stringify(locale==='en'?'Versions':'Versiones')})`),'the versions heading follows the language '+locale+'/'+theme);
        assert.deepEqual(await layoutAudit(),[],locale+'/'+theme+' updates');
        await screenshot('updates-'+locale+'-'+theme);
      }
      await section('modem','connection');
    }
    await route('/metrics/charts/');await evaluate('document.querySelector("canvas").focus()');
    await wait('!!document.querySelector(".chart-tooltip:not([hidden])")','selected chart sample');
    await evaluate('void(window.preferenceCanvas=document.querySelector("canvas"))');await evaluate('document.querySelector("#theme-toggle").focus()');await click('#theme-toggle');
    assert.ok(await evaluate('window.preferenceCanvas===document.querySelector("canvas")'),'theme keeps Canvas DOM');
    assert.ok(await evaluate('!!document.querySelector(".chart-tooltip:not([hidden])")'),'theme keeps selected chart sample');
    await input('#language-select','en');assert.ok(await evaluate('!!document.querySelector(".chart-tooltip:not([hidden])")'),'language keeps selected chart sample');
    await input('#language-select','es');
    if(await evaluate('document.documentElement.dataset.theme')!=='light')await click('#theme-toggle');
    await section('modem','connection');await click('#global-discard');
    fleetMode=true;await click('.refresh-button');
    await wait('document.querySelectorAll("#modem-list a[data-modem]").length===2&&!!document.querySelector("#main-nav a[href=\\"/dashboard/\\"]")','two modems appear in the sidebar and the fleet in the main navigation');
    const beforeFleetPosts=posts.length;
    const firstModemLabel=await evaluate('document.querySelector("#modem-list a[data-modem=\\"default\\"]").innerText');
    assert.ok(firstModemLabel.includes('Test vendor Primary test modem'),'the sidebar names a modem by vendor and model');
    assert.ok(firstModemLabel.includes('857df105 · wwan0'),'the serial and interface line sits under the name');
    await input('#field-modem-apn','first.pending');
    // A modem switch loads before it navigates: the target page never renders its empty state.
    await evaluate('window.__empty=0;window.__observer=new MutationObserver(()=>{if(document.querySelector(".config-loading"))window.__empty++});window.__observer.observe(document.querySelector("#view"),{subtree:true,childList:true})');
    await click('#modem-list a[data-modem="second"]');
    await wait('document.querySelector("#field-modem-apn")?.value==="tenten"&&location.search.includes("modem=second")','second modem loads its own APN and its link names it');
    await evaluate('window.__observer.disconnect()');assert.equal(await evaluate('window.__empty'),0,'switching modems never flashes the empty configuration page');
    await wait('!!document.querySelector("#modem-list a[data-modem=\\"default\\"] .nav-dirty")','the other modem shows its unsaved draft');
    await input('#field-modem-apn','second.pending');await click('#modem-list a[data-modem="default"]');
    await wait('document.querySelector("#field-modem-apn")?.value==="first.pending"','first modem draft survives selection');
    assert.equal(posts.length,beforeFleetPosts,'selecting modems never applies a configuration');
    await click('#modem-list a[data-modem="second"]');await wait('document.querySelector("#field-modem-apn")?.value==="second.pending"','second modem draft survives selection');
    await click('#global-save');await wait('!document.querySelector("#save-bar")','save affects only the selected modem');
    assert.equal(postScopes.at(-1),'second');assert.equal(secondConfig.modem.apn,'second.pending');
    await click('#main-nav a[href="/dashboard/"]');await wait('location.pathname==="/dashboard/"&&!!document.querySelector("#fleet-default-telemetry")','fleet page lists every modem');
    assert.ok(await evaluate('document.querySelector("#fleet-second-telemetry").checked&&document.querySelector("#fleet-default-telemetry").checked'));
    assert.ok(await evaluate('document.body.innerText.includes("Test operator · NR n78")&&document.body.innerText.includes("-90 dBm")'),'the fleet shows each modem\'s current signal');
    await click('#fleet-default-telemetry');await wait('!document.querySelector("#fleet-default-telemetry").checked&&!document.querySelector("#fleet-default-telemetry").disabled','fleet switch changes a modem that is not selected');
    assert.equal(postScopes.at(-1),'default');assert.deepEqual(posts.at(-1),{config:{telemetry:{enabled:false}}});assert.equal(config.telemetry.enabled,false);
    await click('#fleet-default-telemetry');await wait('document.querySelector("#fleet-default-telemetry").checked&&!document.querySelector("#fleet-default-telemetry").disabled','fleet switch restored');
    // f. A diagnosed modem shows its translated title, a bad tone and a heading count, never the raw code.
    fleetDiagnosis={code:'recovery.saved_client_unanswered',severity:'error',active:true,attempts:3,
      failing_since_utc:'2026-11-20T10:00:00+00:00'};
    await click('.refresh-button');
    const fleetLocale=await evaluate('document.documentElement.lang');
    const fleetTitle=fleetLocale==='en'?'The modem does not answer the data session left by a previous run':'El módem no responde a la sesión de datos que dejó una ejecución anterior';
    const fleetAttention=fleetLocale==='en'?'1 needs attention':'1 requiere atención';
    await wait(`document.querySelector('.page-heading p')?.innerText.includes(${JSON.stringify(fleetAttention)})`,'the fleet heading counts the diagnosed modem');
    assert.ok(await evaluate(`(()=>{const row=document.querySelector('#view .fleet-rows .fleet-row'),state=row?.querySelector('.fleet-cell.fleet-state'),sub=state?.querySelector('.fleet-sub'),pill=state?.querySelector('.state-pill');return !!row&&row.querySelector('.fleet-title')?.innerText.includes('Primary test modem')&&!!sub&&sub.innerText.includes(${JSON.stringify(fleetTitle)})&&!!pill&&pill.classList.contains('tone-bad')&&!state.innerText.includes('recovery.saved_client_unanswered')})()`),'the fleet row shows the translated title, a bad tone and no raw code');
    assert.ok(await evaluate(`document.querySelector('#modem-list a[data-modem="default"] .modem-dot')?.classList.contains('tone-bad')`),'the sidebar dot takes the attention tone');
    fleetDiagnosis=null;
    await click('.refresh-button');
    await wait(`!document.querySelector('.page-heading p')?.innerText.includes(${JSON.stringify(fleetAttention)})`,'the fleet heading clears after the diagnosis is gone');
    // An unplugged modem keeps its profile but leaves the fleet: the remaining one owns the single-modem view.
    secondDetected=false;await click('.refresh-button');
    await wait(`document.querySelectorAll("#modem-list a[data-modem]").length===1&&![...document.querySelectorAll("#main-nav a")].some(a=>a.pathname==="/dashboard/")`,'an unplugged modem leaves the fleet');
    assert.ok(await evaluate(`document.querySelectorAll("#modem-list a[data-modem]").length===1&&document.querySelector("#modem-list a[data-modem]").dataset.modem==="default"&&!!document.querySelector("#subnav")&&!document.querySelector("#fleet-default-telemetry")`),
      'the page moves to the remaining modem and uses the single-modem view');
    secondDetected=true;await click('.refresh-button');
    await wait(`document.querySelectorAll("#modem-list a[data-modem]").length===2&&!!document.querySelector("#fleet-default-telemetry")`,'the plugged modem returns to the fleet');
    await click('#modem-list a[data-modem="default"]');await route('/configuration/modem/');
    await wait('document.querySelector("#field-modem-apn")?.value==="first.pending"','quick action preserves the other modem draft');
    await send('Emulation.setDeviceMetricsOverride',{width:320,height:900,deviceScaleFactor:1,mobile:false});
    assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth+2'),'modem list fits mobile width');
    // An A -> B -> A selection must reject a response from the first A visit.
    const savedDefaultApn=config.modem.apn;
    await click('#modem-list a[data-modem="second"]');await wait('document.querySelector("#field-modem-apn")?.value==="second.pending"','second modem before delayed scope request');
    delayedConfigProfile='default';await click('#modem-list a[data-modem="default"]');
    for(let index=0;index<100&&!pendingConfig;index++)await delay(20);
    assert.ok(pendingConfig,'first default-profile request is delayed');
    await click('#modem-list a[data-modem="second"]');await click('#modem-list a[data-modem="default"]');
    await wait('document.querySelector("#field-modem-apn")?.value==="first.pending"','latest default profile visit keeps its draft');
    await delay(150);pendingConfig();pendingConfig=null;await delay(150);
    await click('#global-discard');
    await wait(`document.querySelector('#field-modem-apn')?.value===${JSON.stringify(savedDefaultApn)}`,'late response from an earlier visit cannot overwrite saved configuration');
    // A reload keeps the modem named by its link; drafts are memory-only, the saved profile is reloaded.
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
    await click('#modem-list a[data-modem="second"]');await wait('location.search.includes("modem=second")','second modem selected');
    await reload();await wait('document.querySelector("#modem-list a.active")?.dataset.modem==="second"&&document.querySelector("#field-modem-apn")?.value==="second.pending"','reload keeps the modem named by the link');
    await click('#modem-list a[data-modem="default"]');await wait('document.querySelector("#modem-list a.active")?.dataset.modem==="default"','default selected again');
    // The account menu sits at the bottom of the sidebar and closes with Escape.
    await evaluate('document.querySelector("#account-button").focus()');await click('#account-button');
    await wait('!document.querySelector("#account-menu").hidden&&document.querySelector("#account-button").getAttribute("aria-expanded")==="true"','account menu opens');
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    await wait('document.querySelector("#account-menu").hidden&&document.activeElement.id==="account-button"','Escape closes the account menu and restores focus');
    fleetMode=false;
    await input('#field-modem-apn','pending.example');sessions.clear();
    await wait('!!document.querySelector("#session-password")','expired session login');assert.ok(!(await evaluate('document.body.innerText.includes("001010123456789")')));assert.ok(!(await evaluate('!!document.querySelector("#field-modem-apn")')));
    await input('#session-password',password);await click('#session-connect');await wait('!!document.querySelector("#field-modem-apn")','relogin');assert.equal(await evaluate('document.querySelector("#field-modem-apn").value'),'pending.example','expiry preserves draft');
    await delay(150);delayStatus=true;await evaluate('document.dispatchEvent(new Event("visibilitychange"))');for(let i=0;i<100&&!pendingStatus;i++)await delay(30);assert.ok(pendingStatus);
    await click('#session-open');await wait('!!document.querySelector("#session-password")','logout clears data immediately');assert.ok(await evaluate('document.querySelector("#session-connect").disabled'),'login waits for logout cookie revocation');for(let i=0;i<100&&sessions.size;i++)await delay(30);assert.equal(sessions.size,0);pendingStatus();pendingStatus=null;await delay(150);assert.ok(!(await evaluate('document.body.innerText.includes("001010123456789")')),'late response stays private');
    const loggedOutRequests=requests,loggedOutConfigRequests=configRequests;passwordEnabled=false;await reload();await wait('document.body.innerText.includes("todavía no está configurada")','unconfigured account remains behind login');await assertLoginOnly();assert.ok(await evaluate('document.querySelector("#session-connect").disabled'));assert.equal(requests,loggedOutRequests);assert.equal(configRequests,loggedOutConfigRequests);
    assert.equal(errors.length,0,JSON.stringify(errors));console.log('Next screenshots: /tmp/qmi-next-{login,dashboard,configuration,configuration-mobile,signal,performance,data,mobile,wide,tablet,tools,arrange,updates,updates-en-light,updates-en-dark,updates-es-light,updates-es-dark}.png');
  } finally {
    if(pendingStatus)pendingStatus();
    if(pendingConfig)pendingConfig();
    if(ws?.readyState===WebSocket.OPEN)ws.send(JSON.stringify({id:999999,method:'Browser.close'}));await delay(200);ws?.close();
    if(chrome&&chrome.exitCode===null&&chrome.signalCode===null){const exited=new Promise(resolve=>chrome.once('exit',resolve));chrome.kill('SIGTERM');await Promise.race([exited,delay(2000)]);if(chrome.exitCode===null&&chrome.signalCode===null){chrome.kill('SIGKILL');await exited;}}
    if(chrome?.pid)try{process.kill(-chrome.pid,'SIGKILL');}catch(error){if(error.code!=='ESRCH')throw error;}
    if(server){server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
    try{fs.rmSync(directory,{recursive:true,force:true,maxRetries:8,retryDelay:100});}catch(error){console.warn(error.message);}
  }
});

