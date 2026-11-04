// Bounded browser acceptance over new synthetic sessions. No transcript replay.
// Server-side receipt selection accounts for every classifier and answer call.
const {chromium}=require('playwright')
const {execFileSync}=require('node:child_process')
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict')
const root=path.resolve(__dirname,'../..')
const planPath=process.env.ACCEPTANCE_PLAN
const plan=planPath ? JSON.parse(fs.readFileSync(planPath,'utf8')) : null
if(plan){assert.equal(plan.provider_calls,20);assert.equal(plan.estimated_usd,1);assert.match(plan.batch,/^customer-orders-/);assert.match(plan.username,/^acceptance_orders_/)}
const base=plan ? path.resolve(root,plan.output) : path.join(root,'docs/probes/customer-multiturn-r1-20261101')
if(!base.startsWith(path.join(root,'docs','probes')+path.sep))throw Error('Evidence directory escaped')
const segment=process.argv[2] || 'segment-1'
if(!/^segment-[1-9][0-9]?$/.test(segment)) throw Error('Invalid segment name')
const out=path.join(base,segment),receiptPath=path.join(out,'receipt.json')
fs.mkdirSync(out,{recursive:true})
const resume=process.env.ACCEPTANCE_RESUME==='1'
if(fs.existsSync(receiptPath) && !resume) throw Error('Segment already entered: inspect persisted evidence; never replay unknown outcome')
if(resume && (!plan || !fs.existsSync(receiptPath)))throw Error('Resume requires exact existing custom receipt')
const save=(file,data)=>{const temp=file+'.tmp';fs.writeFileSync(temp,JSON.stringify(data,null,2));fs.renameSync(temp,file)}
const docker=(code,input)=>execFileSync('docker',['exec','-i','shoprelay-m62-eval-delivery-1','/opt/evalvenv/bin/python','-c',code],{encoding:'utf8',input:input || ''})
const taskDir=plan?.task_directory || '/delivery-ledger/orders-task'
const fixtureReceipt=plan?.fixture_receipt || '/delivery-ledger/orders-task-fixture.json'
assert.match(taskDir,/^\/delivery-ledger\/orders-task(?:-[a-z0-9-]+)?$/)
assert.match(fixtureReceipt,/^\/delivery-ledger\/orders-task-fixture(?:-[a-z0-9-]+)?\.json$/)
const orderOnly=process.env.ACCEPTANCE_ORDER_ONLY==='true'
const batch=plan?.batch || (orderOnly?'customer-followup-order-r1-20261101':'customer-multiturn-r1-20261101')
;(async()=>{
  const password=execFileSync('docker',['exec','shoprelay-m62-eval-delivery-1','cat',plan ? taskDir+'/password' : '/delivery-state/demo-password'],{encoding:'utf8'}).trim()
  assert.equal(execFileSync('docker',['exec','shoprelay-m62-eval-delivery-1','cat','/delivery-ledger/scope'],{encoding:'utf8'}).trim(),'USER_INITIATED')
  const browser=await chromium.launch({channel:'msedge',headless:true})
  const page=await browser.newPage({viewport:{width:1440,height:1000}})
  const receipt=resume ? JSON.parse(fs.readFileSync(receiptPath,'utf8')) : {batch,synthetic_owner:plan?.username || 'm65_owner',segment,entered_at:new Date().toISOString(),purpose:'NEW_SYNTHETIC_REAL_MODEL_ACCEPTANCE',session_ids:[],run_ids:[],scenarios:[],errors:[],fixture_receipt:fixtureReceipt}
  const scenarios=plan ? plan.scenarios : orderOnly ? [{name:'order',questions:['我付钱了吗？','查询我的订单 ORD-M65-901 的付款状态']}] : process.env.ACCEPTANCE_ONLY_ADDRESS ? [{name:'address',questions:['退货寄到哪个地址？','那我接下来怎么办？']}] : [
    {name:'multiturn',questions:['普通商品的无理由退货有哪些条件？','什么意思，简单说一下','那是不是提交申请就已经批准了？']},
    {name:'address',questions:['退货寄到哪个地址？','那我接下来怎么办？']}]
  page.on('pageerror',e=>{receipt.errors.push(e.message);save(receiptPath,receipt)})
  await page.route('**/api/chat/stream',async route=>{
    const body=route.request().postDataJSON()
    if(!receipt.session_ids.includes(body.session_id)) return route.abort()
    const scenario=receipt.scenarios.find(s=>s.session_id===body.session_id)
    const step=scenario.steps.at(-1)
    assert.equal(body.message,step.question)
    step.client_request_id=body.client_request_id;save(receiptPath,receipt)
    return route.continue()
  })
  try{
    await page.goto('http://127.0.0.1:18065/')
    await page.getByPlaceholder('输入用户名').fill(plan?.username || 'm65_owner')
    await page.getByPlaceholder('输入密码').fill(password)
    await page.getByRole('button',{name:'安全登录'}).click()
    await page.getByRole('button',{name:/新建会话/}).waitFor()
    const csrf=(await(await page.request.get('http://127.0.0.1:18065/api/demo/csrf')).json()).csrf_token
    if(!resume) for(const scenario of scenarios){
      const response=await page.request.post('http://127.0.0.1:18065/api/sessions',{headers:{Origin:'http://127.0.0.1:18065','X-CSRF-Token':csrf},data:{title:`合成验收 · ${segment} · ${scenario.name}`}})
      assert.equal(response.status(),200)
      const session=(await response.json()).session
      receipt.session_ids.push(session.session_id)
      receipt.scenarios.push({...scenario,session_id:session.session_id,steps:[]});save(receiptPath,receipt)
    }
    // Only the trusted controller writes the accounting receipt. No browser flag
    // can select an account; existing user sessions stay on their unchanged ledger.
    if(plan && !resume){
      docker(`import json,sys\nfrom pathlib import Path\nf=json.loads(Path('${fixtureReceipt}').read_text())\na=json.load(sys.stdin)\nassert a['batch_id']==f['batch'] and a['provider_calls']==20 and a['estimated_usd']==1\np=Path('/delivery-ledger/acceptance-account.json')\nassert json.loads(p.read_text())['status']=='CLOSED'\nd=Path('${taskDir}');d.mkdir(exist_ok=True)\nassert not (d/'account.json').exists() and not (d/'closed-account.json').exists()\n(d/'previous-account.json').write_bytes(p.read_bytes())\n(d/'account.json').write_text(json.dumps(a))\np.write_text(json.dumps(a))\nprint('New synthetic account registered; no Provider request')`,JSON.stringify({batch_id:batch,status:'OPEN',approval_event:plan.approval_event,session_ids:receipt.session_ids,provider_calls:20,total_tokens:400000,estimated_usd:1}))
    }else if(!resume){
    docker(`import json,sys\nfrom pathlib import Path\np=Path('/delivery-ledger/acceptance-account.json')\nnew=json.load(sys.stdin)\nif p.exists():\n old=json.loads(p.read_text())\n if old['status']=='CLOSED':\n  assert new['batch_id']=='customer-followup-order-r1-20261101' and old['batch_id']=='customer-multiturn-r1-20261101'\n  parent=json.loads(Path('/delivery-ledger/customer-multiturn-r1/closed-account.json').read_text())\n  assert parent['snapshot']['provider_requests']==15 and new['provider_calls']==20-parent['snapshot']['provider_requests']\n  new['estimated_usd']=1-parent['snapshot']['estimated_usd']\n  new['total_tokens']=400000-parent['snapshot']['tokens']\n else:\n  assert old['batch_id']==new['batch_id'] and old['status']=='OPEN'\n  assert not set(old['session_ids']) & set(new['session_ids'])\n  new['session_ids']=old['session_ids']+new['session_ids']\n  for field in ['provider_calls','total_tokens','estimated_usd']: new[field]=old[field]\np.write_text(json.dumps(new))\nprint('Synthetic receipt registered; no model request')`,JSON.stringify({batch_id:batch,status:'OPEN',approval_event:orderOnly?'01a19765-3353-79a2-89a1-b3ecb06f92f8':'01a19757-89f0-7d43-b522-e3c4c445c482',session_ids:receipt.session_ids,provider_calls:orderOnly?5:20,total_tokens:400000,estimated_usd:1}))
    }
    if(plan){
      docker(`from scripts.m65_local_delivery import application\nfrom app.m1.delivery_provider import acceptance_request\nimport json\nfrom pathlib import Path\na=json.loads(Path('/delivery-ledger/acceptance-account.json').read_text())\nassert a['status']=='OPEN'\napp=application()\ntoken=acceptance_request.set((a['batch_id'],a['session_ids'][0],'budget-registration'))\ntry: app.state.chat_provider.client.ledger()\nfinally: acceptance_request.reset(token)\nprint('Independent account registered before next application request; zero Provider calls')`)
    }
    for(const scenario of receipt.scenarios){
      await page.goto(`http://127.0.0.1:18065/chat/${scenario.session_id}`)
      await page.waitForLoadState('networkidle')
      if((orderOnly || plan) && !resume){
        for(const width of [1440,768,390]){
          await page.setViewportSize({width,height:width===390?844:1000})
          await page.screenshot({path:path.join(out,`welcome-${width}.png`),fullPage:true})
          assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true)
        }
        if(orderOnly) await page.getByRole('button',{name:'查询我的订单 ORD-M65-901 的记录状态',exact:true}).click()
        if(orderOnly) assert.equal(await page.getByPlaceholder('写下你想咨询的内容…').inputValue(),'查询我的订单 ORD-M65-901 的记录状态')
        assert.equal(scenario.steps.length,0)
        await page.setViewportSize({width:1440,height:1000})
      }
      if(resume){
        receipt.resume_events=receipt.resume_events || []
        receipt.resume_events.push({at:new Date().toISOString(),action:'reconcile persisted terminal Runs; skip consumed messages'})
        for(const step of scenario.steps){
          assert.ok(step.run_id,'Unknown run creation requires manual reconciliation')
          const existing=await page.request.get(`http://127.0.0.1:18065/api/runs/${step.run_id}`)
          assert.equal(existing.status(),200)
          step.run=(await existing.json()).run
          assert.ok(['SUCCEEDED','FAILED','MODEL_UNAVAILABLE','CANCELLED'].includes(step.run.status))
          step.execution=(await(await page.request.get(`http://127.0.0.1:18065/api/runs/${step.run_id}/execution`)).json()).execution
          step.events=(await(await page.request.get(`http://127.0.0.1:18065/api/runs/${step.run_id}/events`)).json()).events
          step.state='RECONCILED_NO_REPLAY';save(receiptPath,receipt)
        }
        if(scenario.steps.length===1 && scenario.steps[0].run.status==='SUCCEEDED'){
          await page.locator('.order-options button').first().click()
          assert.match(await page.getByPlaceholder('写下你想咨询的内容…').inputValue(),/ORD-/)
        }
      }
      const consumedBeforeResume=resume ? scenario.steps.length : 0
      for(const [questionIndex,question] of scenario.questions.entries()){
        if(questionIndex<consumedBeforeResume)continue

        const step={question,state:'ENTERED',entered_at:new Date().toISOString()}
        scenario.steps.push(step);save(receiptPath,receipt)
        await page.getByPlaceholder('写下你想咨询的内容…').fill(question)
        const responsePromise=page.waitForResponse(r=>r.url().endsWith('/api/chat/stream'))
        await page.getByRole('button',{name:'发送消息',exact:true}).click()
        const response=await responsePromise
        step.http_status=response.status();step.run_id=response.headers()['x-run-id']
        if(!step.run_id){step.state='NO_RUN_HEADER';save(receiptPath,receipt);throw Error('No persisted run header; reconcile before any further request')}
        receipt.run_ids.push(step.run_id);save(receiptPath,receipt)
        for(let i=0;i<240;i++){
          const r=await page.request.get(`http://127.0.0.1:18065/api/runs/${step.run_id}`)
          r.ok() || assert.fail('Cannot reconcile Run')
          step.run=(await r.json()).run
          if(!['CREATED','RUNNING'].includes(step.run.status))break
          await page.waitForTimeout(500)
        }
        step.execution=(await(await page.request.get(`http://127.0.0.1:18065/api/runs/${step.run_id}/execution`)).json()).execution
        step.events=(await(await page.request.get(`http://127.0.0.1:18065/api/runs/${step.run_id}/events`)).json()).events
        step.budget=JSON.parse(docker(`import json\nfrom pathlib import Path\nfrom eval.m6_1.budget import BudgetLedger,BudgetLimits\nfrom eval.m6_1.store import ResultStore\na=json.loads(Path('/delivery-ledger/acceptance-account.json').read_text())\ns=ResultStore(Path('/delivery-ledger/requests.sqlite'))\nl=BudgetLimits(provider_calls=a['provider_calls'],total_tokens=a['total_tokens'],estimated_usd=a['estimated_usd'],max_concurrent_trials=1,max_duration_seconds=120,max_retries=0)\nprint(json.dumps(BudgetLedger(s,'delivery-'+a['batch_id'],l).snapshot()))`))
        step.state='RECORDED';save(receiptPath,receipt)
        if(step.budget.active_reservations || step.budget.token_status==='UNKNOWN_USAGE') throw Error('Usage reconciliation required; no further request permitted')
        await page.waitForFunction(()=>!document.querySelector('.execution-card'))
        await page.screenshot({path:path.join(out,`${scenario.name}-${scenario.steps.length}.png`),fullPage:true})
        if(plan && scenario.steps.length===1 && step.run.status==='SUCCEEDED'){
          const before=receipt.run_ids.length
          await page.locator('.order-options button').first().click()
          assert.match(await page.getByPlaceholder('写下你想咨询的内容…').inputValue(),/ORD-/)
          assert.equal(receipt.run_ids.length,before)
        }
        if(step.run.status!=='SUCCEEDED'){
          if(scenario.name==='order')continue // The next explicit order query is independent, not candidate reuse.
          break // An elliptical follow-up must use delivered context.
        }
        assert.ok(step.run.answer?.answer)
        await page.getByText(step.run.answer.answer,{exact:true}).first().waitFor()
      }
      scenario.session=(await(await page.request.get(`http://127.0.0.1:18065/api/sessions/${scenario.session_id}`)).json()).session
      save(receiptPath,receipt)
      if(orderOnly || plan){
        for(const width of [1440,768,390]){
          await page.setViewportSize({width,height:width===390?844:1000})
          await page.waitForTimeout(200)
          await page.screenshot({path:path.join(out,`conversation-${width}.png`),fullPage:true})
          assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true)
          if(width<1101){
            await page.getByRole('button',{name:'打开菜单'}).click()
            await page.getByRole('button',{name:'关闭菜单'}).click()
            await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().right<=1)
          }
        }
        await page.getByRole('button',{name:/查看依据与执行/}).last().click()
        await page.screenshot({path:path.join(out,'sources-390.png'),fullPage:true})
        await page.getByRole('button',{name:'执行过程',exact:true}).click()
        await page.screenshot({path:path.join(out,'execution-390.png'),fullPage:true})
        await page.keyboard.press('Escape')
        assert.equal(await page.locator('.run-panel').isVisible(),false)
        await page.reload({waitUntil:'networkidle'})
        await page.locator('.message-text').first().waitFor()
        assert.equal(await page.locator('.welcome-state').count(),0)
        const input=page.getByPlaceholder('写下你想咨询的内容…')
        await input.fill('只读输入检查');await input.press('Enter')
        assert.match(await input.inputValue(),/\n/);await input.fill('')
        await page.setViewportSize({width:1440,height:1000})
        const html=await page.evaluate(()=>{
          const doc=document.documentElement.cloneNode(true)
          doc.querySelectorAll('script').forEach(e=>e.remove())
          doc.querySelectorAll('link[href]').forEach(e=>{e.removeAttribute('crossorigin');e.setAttribute('href',new URL(e.getAttribute('href'),location.origin).href)})
          return '<!doctype html>'+doc.outerHTML
        })
        fs.writeFileSync(path.join(out,'authenticated.html'),html)
      }
    }
    receipt.completed_at=new Date().toISOString();save(receiptPath,receipt)
    console.log(JSON.stringify({batch,segment,sessions:receipt.session_ids.length,runs:receipt.run_ids.length,scenarios:receipt.scenarios.map(s=>({name:s.name,states:s.steps.map(x=>x.run?.status)}))}))
  }catch(e){receipt.failure_type=e.name;receipt.failure_message=e.message.slice(0,300);save(receiptPath,receipt);throw e}
  finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1})
