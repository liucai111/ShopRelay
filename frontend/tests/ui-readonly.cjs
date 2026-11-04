// Read-only browser acceptance. Only this browser's normal login/logout may write.
const { chromium } = require('playwright')
const { execFileSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
;(async () => {
  const phase = process.argv[2] || 'before'
  const out = process.env.READONLY_OUTPUT || path.resolve(__dirname, '../../docs/probes/customer-ui-polish-20261101', phase)
  const sessionId = process.env.READONLY_SESSION_ID || '5bef35b2-244b-46cc-a641-8efc47ea82bf'
  fs.mkdirSync(out, { recursive: true })
  const owner=process.env.READONLY_OWNER || 'm65_owner'
  if(owner!=='m65_owner' && !/^acceptance_orders_/.test(owner))throw Error('Synthetic owner required')
  const password = execFileSync('docker', ['exec','shoprelay-m62-eval-delivery-1','cat',owner==='m65_owner'?'/delivery-state/demo-password':'/delivery-ledger/orders-task/password'], {encoding:'utf8'}).trim()
  const browser = await chromium.launch({channel:'msedge',headless:true})
  const page = await browser.newPage({viewport:{width:1440,height:1000}})
  const errors = [], blocked = [], writes = []
  page.on('pageerror', e => errors.push(e.message))
  await page.route('**/api/**', async route => {
    const req = route.request(), pathname = new URL(req.url()).pathname
    if (!['GET','HEAD'].includes(req.method())) {
      if (!['/api/demo/login','/api/demo/logout'].includes(pathname)) {
        blocked.push(pathname); return route.abort()
      }
      writes.push(pathname)
    }
    await route.continue()
  })
  try {
    await page.goto('http://127.0.0.1:18065/')
    await page.getByPlaceholder('输入用户名').fill(owner)
    await page.getByPlaceholder('输入密码').fill(password)
    await page.getByRole('button',{name:'安全登录'}).click()
    await page.getByRole('button',{name:/新建会话/}).waitFor()
    if (process.env.READONLY_ADMIN === '1') {
      await page.goto('http://127.0.0.1:18065/admin/observability')
      await page.locator('.kpi-grid').first().waitFor()
      for (const width of [1440,768,390]) {
        await page.setViewportSize({width,height:width===390?844:1000})
        await page.screenshot({path:path.join(out,`${width}.png`),fullPage:true})
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true)
      }
      if (phase !== 'before') {
        const bars=await page.locator('.trend-bar').evaluateAll(elements=>elements.map(e=>({height:e.style.height,title:e.title})))
        assert.ok(bars.every(b=>b.title.includes('至')))
        assert.equal(await page.locator('.kpi-grid').first().locator('.kpi-card').count(),5)
        assert.ok(await page.getByText('以下筛选仅影响运行列表', {exact:false}).count())
      }
      const html=await page.evaluate(()=>{
        const root=document.documentElement.cloneNode(true)
        root.querySelectorAll('script').forEach(e=>e.remove())
        root.querySelectorAll('link[href]').forEach(e=>{e.removeAttribute('crossorigin');e.setAttribute('href',new URL(e.getAttribute('href'),location.origin).href)})
        return '<!doctype html>'+root.outerHTML
      })
      fs.writeFileSync(path.join(out,'authenticated.html'),html)
      assert.deepEqual(errors,[]); assert.deepEqual(blocked,[])
      fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify({phase,errors,blocked,writes,application_runs_created:0,provider_calls:0},null,2))
      return
    }
    await page.goto(`http://127.0.0.1:18065/chat/${sessionId}`)
    await page.waitForLoadState('networkidle')
    await page.locator('.message-text').first().waitFor()
    const cdp = await page.context().newCDPSession(page)
    await cdp.send('DOM.enable'); await cdp.send('CSS.enable')
    const doc = await cdp.send('DOM.getDocument')
    const node = await cdp.send('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'.message-text'})
    const fonts = await cdp.send('CSS.getPlatformFontsForNode',{nodeId:node.nodeId})
    const computed = await page.locator('.message-text').first().evaluate(e => ({font:getComputedStyle(e).fontFamily,size:getComputedStyle(e).fontSize}))
    for (const width of [1440,768,390]) {
      await page.setViewportSize({width,height:width===390?844:1000})
      await page.waitForTimeout(220)
      await page.locator('.chat-scroll').evaluate(e => {e.scrollTop=e.scrollHeight})
      await page.screenshot({path:path.join(out,`${width}.png`),fullPage:true})
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true)
      if (phase !== 'before' && width<1101) {
        await page.getByRole('button',{name:'打开菜单'}).click()
        await page.getByRole('button',{name:'关闭菜单'}).click()
        await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().right <= 1)
      }
    }
    await page.setViewportSize({width:1440,height:1000})
    await page.getByRole('button',{name:'执行详情',exact:true}).click()
    await page.getByRole('button',{name:'执行过程',exact:true}).click()
    await page.screenshot({path:path.join(out,'execution.png'),fullPage:true})
    if (phase !== 'before') {
      await page.getByText('技术记录',{exact:false}).first().click()
      await page.keyboard.press('Escape')
      assert.equal(await page.locator('.run-panel').isVisible(),false)
      await page.getByRole('button',{name:'执行详情',exact:true}).click()
    }
    for (const tab of ['证据引用','会话记忆','操作提案']) {
      await page.getByRole('button',{name:tab,exact:true}).click()
      await page.screenshot({path:path.join(out,`${tab}.png`),fullPage:true})
    }
    if (phase !== 'before') {
      await page.setViewportSize({width:768,height:1000})
      await page.screenshot({path:path.join(out,'drawer-768.png'),fullPage:true})
      const panel = await page.locator('.run-panel').boundingBox()
      assert.ok(panel && panel.width <= 768 && panel.x >= 0)
      await page.keyboard.press('Escape')
      await page.getByRole('button',{name:'执行详情',exact:true}).click()
      await page.keyboard.press('Shift+Tab')
      assert.equal(await page.locator('.run-panel').evaluate(e => e.contains(document.activeElement)),true)
      await page.setViewportSize({width:390,height:844})
      await page.screenshot({path:path.join(out,'drawer-390.png'),fullPage:true})
    }
    await page.getByRole('button',{name:'关闭详情'}).click()
    await page.setViewportSize({width:1440,height:1000})
    await page.reload({waitUntil:'networkidle'})
    assert.ok(await page.locator('.message-text').count())
    if (phase !== 'before') {
      await page.locator('.message-source').nth(1).click()
      await page.getByRole('button',{name:'执行过程',exact:true}).click()
      await page.getByText(/读取已保存的业务结果|核对业务记录/).first().waitFor()
      await page.screenshot({path:path.join(out,'business-execution.png'),fullPage:true})
      await page.getByRole('button',{name:'关闭详情'}).click()
    }
    const html = await page.evaluate(() => {
      const root=document.documentElement.cloneNode(true)
      root.querySelectorAll('script').forEach(e=>e.remove())
      root.querySelectorAll('link[href]').forEach(e=>{e.removeAttribute('crossorigin');e.setAttribute('href',new URL(e.getAttribute('href'),location.origin).href)})
      return '<!doctype html>'+root.outerHTML
    })
    fs.writeFileSync(path.join(out,'authenticated.html'),html)
    assert.deepEqual(errors,[]); assert.deepEqual(blocked,[])
    fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify({phase,fonts,computed,errors,blocked,writes,application_runs_created:0,provider_calls:0,session_preserved:true},null,2))
  } finally { await browser.close() }
})().catch(e=>{console.error(e);process.exitCode=1})
