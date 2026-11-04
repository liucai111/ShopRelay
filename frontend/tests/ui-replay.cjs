// Offline API replay of an archived synthetic response. Never writes to the server.
const { chromium } = require('playwright')
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
;(async () => {
  const root=path.resolve(__dirname,'../..')
  const row=JSON.parse(fs.readFileSync(path.join(root,'docs/probes/customer-experience-r1-20261101/real-budget-final/real-browser-results.json'),'utf8')).rows[0]
  const run=row.run, sid=run.session_id
  const out=path.join(root,'docs/probes/customer-ui-polish-20261101/replay')
  fs.mkdirSync(out,{recursive:true})
  const browser=await chromium.launch({channel:'msedge',headless:true})
  const page=await browser.newPage({viewport:{width:1440,height:1000}})
  const writes=[],errors=[]
  page.on('pageerror',e=>errors.push(e.message))
  await page.route('**/api/**',async route=>{
    const req=route.request(), p=new URL(req.url()).pathname
    if(req.method()!=='GET'){writes.push(p);return route.abort()}
    let data
    if(p==='/api/demo/me') data={user:{display_name:'离线合成记录回放'}}
    else if(p==='/api/runtime') data={mode:'OFFLINE_ACCEPTANCE',notice:'离线界面回放，非新增试次；未连接模型。',suggestions:[]}
    else if(p==='/api/sessions') data={sessions:[{session_id:sid,title:'离线回放：同文档不同条款',created_at:run.created_at}]}
    else if(p===`/api/sessions/${sid}`) data={session:{session_id:sid,title:'离线回放：同文档不同条款',created_at:run.created_at,runs:[run],messages:[{message_id:'replay-user',role:'user',content:row.question,status:'SAVED',created_at:run.created_at},{message_id:'replay-answer',role:'assistant',content:run.answer.answer,status:'SAVED',created_at:run.updated_at}]}}
    else if(p.endsWith('/execution')) data={execution:row.execution}
    else if(p.endsWith('/events')) data={status:run.status,events:[],events_cursor:0}
    else if(p===`/api/runs/${run.run_id}`) data={run}
    else if(p.endsWith('/actions')) data={actions:[]}
    else return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({code:'OFFLINE_UNAVAILABLE'})})
    await route.fulfill({contentType:'application/json',body:JSON.stringify(data)})
  })
  try{
    await page.goto(`http://127.0.0.1:18065/chat/${sid}`)
    await page.getByRole('button',{name:/查看依据与执行/}).click()
    await page.getByRole('button',{name:'查看政策条款',exact:true}).first().waitFor()
    assert.equal(await page.locator('.source-document').count(),1)
    assert.equal(await page.locator('.evidence-card').count(),2)
    await page.getByRole('button',{name:'查看政策条款',exact:true}).first().click()
    assert.equal(await page.locator('.evidence-content').count(),1)
    await page.screenshot({path:path.join(out,'distinct-clauses.png'),fullPage:true})
    await page.getByRole('button',{name:'执行过程',exact:true}).click()
    await page.getByText('技术记录',{exact:false}).first().click()
    assert.ok(await page.locator('.technical-id').count())
    await page.screenshot({path:path.join(out,'recorded-spans.png'),fullPage:true})
    assert.deepEqual(writes,[]);assert.deepEqual(errors,[])
    fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify({mode:'OFFLINE_ARCHIVE_REPLAY',source_run:run.run_id,citations:2,documents:1,distinct_clauses_preserved:true,writes,errors,provider_calls:0,new_runs:0},null,2))
  }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1})
