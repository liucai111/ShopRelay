// Deployed customer workspace. Creates a synthetic session; never sends old history.
const { chromium } = require('playwright')
const { execFileSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
;(async () => {
  const phase = process.argv[2] || 'before'
  const isReal = ['real', 'real-final', 'real-budget-final'].includes(phase)
  if (isReal && execFileSync('docker', ['exec', 'shoprelay-m62-eval-delivery-1', 'cat', '/delivery-ledger/scope'], { encoding:'utf8' }).trim() !== 'AUTOMATED_SYNTHETIC') throw new Error('Automated acceptance is closed')
  const out = path.resolve(__dirname, '../../docs/probes/customer-experience-r1-20261101', phase)
  fs.mkdirSync(out, { recursive: true })
  if (isReal && fs.existsSync(path.join(out, 'receipt.json'))) throw new Error('Real browser acceptance already entered; do not replay')
  const password = execFileSync('docker', ['exec', 'shoprelay-m62-eval-delivery-1', 'cat', '/delivery-state/demo-password'], { encoding: 'utf8' }).trim()
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  try {
    await page.goto('http://127.0.0.1:18065/')
    await page.getByPlaceholder('输入用户名').fill('m65_owner')
    await page.getByPlaceholder('输入密码').fill(password)
    await page.getByRole('button', { name: '安全登录' }).click()
    await page.getByRole('button', { name: /新建会话/ }).waitFor()
    await page.getByRole('button', { name: /新建会话/ }).click()
    await page.waitForURL(/\/chat\/[0-9a-f-]{36}$/)
    await page.waitForLoadState('networkidle')
    await page.locator('.loading-history').waitFor({state:'hidden'})
    await page.waitForTimeout(3300) // transient sign-in toast, not content under review
    const sessionId = new URL(page.url()).pathname.split('/').pop()
    assert.notEqual(sessionId, '71b78efe-6eb9-4659-b92e-e4c19a489f4f')
    fs.writeFileSync(path.join(out, 'receipt.json'), JSON.stringify({ session_id: sessionId, purpose: 'SYNTHETIC_UI_TEST', paid_calls: isReal ? null : 0, ledger: isReal ? 'shared automated ledger' : null }))
    if (isReal) {
      const rows = []
      const questions = phase === 'real-budget-final' ? ['商品要退货，怎么退'] : ['请说明普通商品无理由退货的申请条件。', '什么意思，请通俗解释', '退货应该寄到哪个地址？', '查询我的订单 ORD-M65-901 的记录状态']
      for (const question of questions) {
        if (question === questions[1] && rows[0]?.run.status !== 'SUCCEEDED') continue
        await page.getByPlaceholder('写下你想咨询的内容…').fill(question)
        const responsePromise = page.waitForResponse(r => r.url().endsWith('/api/chat/stream'))
        await page.getByRole('button', { name: '发送消息', exact: true }).click()
        const response = await responsePromise
        const runId = response.headers()['x-run-id']
        if (!runId) throw new Error('No persisted Run ID on stream')
        let run
        for (let i = 0; i < 120; i++) {
          run = (await (await page.request.get(`http://127.0.0.1:18065/api/runs/${runId}`)).json()).run
          if (!['CREATED','RUNNING'].includes(run.status)) break
          await page.waitForTimeout(500)
        }
        const execution = (await (await page.request.get(`http://127.0.0.1:18065/api/runs/${runId}/execution`)).json()).execution
        rows.push({ question, run, execution })
        fs.writeFileSync(path.join(out, 'real-browser-results.json'), JSON.stringify({ session_id: sessionId, rows }, null, 2))
        await page.getByRole('button', { name: '发送消息', exact: true }).waitFor()
        await page.waitForFunction(() => !document.querySelector('.execution-card'))
        if (['UNKNOWN_RESULT','MODEL_UNAVAILABLE','RUNNING'].includes(run.status)) break
      }
      await page.reload({ waitUntil: 'networkidle' })
      await page.locator('.loading-history').waitFor({ state: 'hidden' })
      assert.ok((await page.locator('.message-row').count()) >= rows.length)
      const sourceButtons = page.getByRole('button', { name: '来源与执行', exact: true })
      if (await sourceButtons.count()) await sourceButtons.first().click()
      else {
        await page.getByRole('button', { name: '执行详情', exact: true }).click()
        await page.getByRole('button', { name: '证据引用', exact: true }).click()
      }
      const clause = page.getByRole('button', { name: '查看政策条款', exact: true })
      if (await clause.count()) await clause.first().click()
      await page.screenshot({ path: path.join(out, 'references.png'), fullPage: true })
      await page.getByRole('button', { name: '执行过程', exact: true }).click()
      await page.screenshot({ path: path.join(out, 'execution.png'), fullPage: true })
      await page.getByRole('button', { name: '关闭详情' }).click()
    }
    for (const width of [1440, 768, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 })
      await page.waitForTimeout(250)
      await page.screenshot({ path: path.join(out, `${width}.png`), fullPage: true })
      if (phase !== 'before') {
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
        if (width < 1101) {
          await page.getByRole('button', { name: '打开菜单' }).click()
          await page.getByRole('button', { name: '关闭菜单' }).click()
          await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().right <= 1)
        }
      }
    }
    await page.setViewportSize({ width: 1440, height: 1000 })
    // An inspectable frozen copy of the actually rendered authenticated workspace.
    // No credentials, scripts, trace payloads or existing user messages are exported.
    const html = await page.evaluate(() => {
      const doc = document.documentElement.cloneNode(true)
      doc.querySelectorAll('script').forEach(e => e.remove())
      doc.querySelectorAll('link[href]').forEach(e => { e.removeAttribute('crossorigin'); e.setAttribute('href', new URL(e.getAttribute('href'), location.origin).href) })
      return '<!doctype html>' + doc.outerHTML
    })
    fs.writeFileSync(path.join(out, 'authenticated.html'), html)
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ phase, synthetic_session: sessionId, viewports: [1440, 768, 390], page_errors: 0, provider_calls: isReal ? 'SEE_SHARED_LEDGER' : 0 }))
  } finally {
    const logout = page.getByRole('button', { name: '退出', exact: true })
    if (await logout.count() && await logout.isVisible()) await logout.click().catch(() => {})
    await browser.close()
  }
})().catch(e => { console.error(e.message); process.exitCode = 1 })
