// Local deployed-app browser acceptance. Credentials stay in process memory.
const { chromium } = require('playwright')
const { execFileSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

;(async () => {
  const out = path.resolve(__dirname, '../../docs/probes/m65')
  const password = execFileSync('docker', ['exec', 'shoprelay-m62-eval-delivery-1',
    'cat', '/delivery-state/demo-password'], { encoding: 'utf8' }).trim()
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  try {
    await page.goto('http://127.0.0.1:18065/', { waitUntil: 'networkidle' })
    await page.getByPlaceholder('输入用户名').fill('m65_owner')
    await page.getByPlaceholder('输入密码').fill(password)
    await page.getByRole('button', { name: '安全登录' }).click()
    await page.getByRole('button', { name: /新建会话/ }).waitFor()
    await page.getByRole('button', { name: /新建会话/ }).click()
    await page.getByPlaceholder('写下你想咨询的内容…').fill('请说明虚构政策中独立商业渠道的申请期限与适用条件。')
    await page.getByRole('button', { name: '发送消息', exact: true }).click()
    await page.getByText('m65-policy-window', { exact: true }).first().waitFor({ timeout: 40000 })
    await page.getByText(/本轮虚构条款说明/).first().waitFor()
    await page.getByRole('button', { name: '证据引用', exact: true }).click()
    await page.screenshot({ path: path.join(out, 'browser-evidence.png'), fullPage: true })
    await page.reload({ waitUntil: 'networkidle' })
    await page.getByText(/本轮虚构条款说明/).first().waitFor()
    await page.getByRole('button', { name: '执行过程', exact: true }).click()
    await page.screenshot({ path: path.join(out, 'browser-restored.png'), fullPage: true })
    if (errors.length) throw new Error('Browser page errors: ' + errors.join('; '))
    fs.writeFileSync(path.join(out, 'browser-e2e.json'), JSON.stringify({
      browser: 'installed Microsoft Edge / Playwright', provider: 'deterministic evidence stub',
      login: true, session: true, SSE_answer: true, citation_panel: true,
      reload_restores_record: true, execution_panel: true, page_errors: errors,
      paid_provider_calls: 0
    }, null, 2))
    console.log('Browser delivery E2E passed; password/cookies not logged')
  } catch (error) {
    await page.screenshot({ path: path.join(out, 'browser-failure.png'), fullPage: true })
    fs.writeFileSync(path.join(out, 'browser-failure.txt'), await page.locator('body').innerText())
    throw error
  } finally { await browser.close() }
})().catch(e => { console.error(e.message); process.exitCode = 1 })
