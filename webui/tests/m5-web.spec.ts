import { expect, test } from '@playwright/test'
import path from 'node:path'

const shots = path.resolve('../tmp/m5_web_demo/screenshots')

test.describe.configure({ mode: 'serial' })

test('batch files are uploaded as multipart instead of a server path', async ({ page }) => {
  let contentType = ''
  let uploadedBody = ''
  await page.route(
    (url) => url.pathname === '/api/batches/upload',
    async (route) => {
      contentType = route.request().headers()['content-type'] || ''
      uploadedBody = route.request().postData() || ''
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        json: {
          job: { job_id: 101, kind: 'import_batch', status: 'queued' },
          upload: { batch_name: 'upload-test', file_count: 1, file_names: ['提交测试.xlsx'] },
        },
      })
    },
  )
  await page.goto('/')
  await page.getByLabel('复核人').fill('upload-reviewer')
  await page.getByLabel('选择文件').setInputFiles({
    name: '提交测试.xlsx',
    mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    buffer: Buffer.from('test-workbook'),
  })
  await expect(page.getByText('已选 1 个文件')).toBeVisible()
  await page.getByRole('button', { name: '导入批次', exact: true }).click()
  await expect(page.getByText('导入任务已开始，完成后可在批次列表启动审核。')).toBeVisible()
  expect(contentType).toContain('multipart/form-data')
  expect(uploadedBody).toContain('提交测试.xlsx')
})

test('batch stages gate three actions and audit confirmation carries digest', async ({ page }) => {
  const digest = 'a'.repeat(64)
  let confirmedDigest = ''
  const batchResponse = await page.request.get('/api/batches')
  const batchPayload = await batchResponse.json() as { batches: Array<Record<string, unknown>> }
  batchPayload.batches[0] = {
    ...batchPayload.batches[0],
    stages: {
      local: { status: 'done', attempt: 1, error_code: '' },
      m4: { status: 'pending', attempt: 0, error_code: '', item_counts: {} },
      m5: { status: 'pending', attempt: 0, error_code: '', case_counts: {}, required: false },
    },
    promotion_readiness: { can_promote: false, reasons: ['M4 尚未完成'] },
  }
  await page.route(
    (url) => url.pathname === '/api/batches',
    async (route) => route.fulfill({ status: 200, contentType: 'application/json', json: batchPayload }),
  )
  await page.route(
    (url) => url.pathname === '/api/batches/1/audit/preview',
    async (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      json: {
        batch_id: 1,
        probe_status: 'not_checked',
        preview_digest: digest,
        issues: [],
        candidate_targets: [{
          resource_code: '04050014',
          year: '2026',
          award_name: '示范科技奖',
          urls: ['https://official.example/award'],
          domains: ['official.example'],
          submitted_count: 128,
          probe_status: 'not_checked',
        }],
      },
    }),
  )
  await page.route(
    (url) => url.pathname === '/api/batches/1/audit',
    async (route) => {
      confirmedDigest = String(route.request().postDataJSON().preview_digest || '')
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        json: { job: { job_id: 99, kind: 'audit_batch', status: 'queued' } },
      })
    },
  )

  await page.setViewportSize({ width: 1440, height: 960 })
  await page.goto('/')
  await page.getByLabel('复核人').fill('browser-reviewer')
  const row = page.getByRole('row').filter({ hasText: '2026 年示范评奖批次' })
  const stepper = row.getByLabel(/审核进度/)
  await expect(stepper.getByText('本地检查', { exact: true })).toBeVisible()
  await expect(stepper.getByText('联网核对', { exact: true })).toBeVisible()
  await expect(stepper.getByText('深度取证', { exact: true })).toBeVisible()
  await expect(row.getByRole('button', { name: '联网核对', exact: true })).toBeEnabled()
  await expect(row.getByRole('button', { name: 'M5 审查', exact: true })).toBeDisabled()
  await expect(row.getByRole('button', { name: '入库', exact: true })).toBeDisabled()

  await row.getByRole('button', { name: '联网核对', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '2026 年示范评奖批次' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('示范科技奖')).toBeVisible()
  await expect(dialog.getByText('2026 · 04050014')).toBeVisible()
  await expect(dialog.getByText('尚未核验', { exact: true })).toBeVisible()
  await expect(dialog.getByText('尚未联网核验')).toBeVisible()
  await page.screenshot({ path: path.join(shots, 'desktop-audit-preview.png'), fullPage: true })
  await dialog.getByRole('button', { name: '确认并启动联网核对' }).click()
  await expect(dialog).toBeHidden()
  expect(confirmedDigest).toBe(digest)
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

test('completed M4 results show current evidence and M5 binding', async ({ page }) => {
  const batchResponse = await page.request.get('/api/batches')
  const batchPayload = await batchResponse.json() as { batches: Array<Record<string, any>> }
  batchPayload.batches[0].stages.m4 = {
    status: 'done', attempt: 1, error_code: '', item_counts: { done: 1 },
  }
  await page.route(
    (url) => url.pathname === '/api/batches',
    async (route) => route.fulfill({ status: 200, contentType: 'application/json', json: batchPayload }),
  )
  await page.route(
    (url) => url.pathname === '/api/batches/1/audit-results',
    async (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      json: {
        batch_id: 1,
        history_count: 2,
        items: [{
          stage_item_id: 1,
          resource_code: '04030052',
          year: '2025',
          stage_status: 'done',
          attempt: 1,
          stage_error_code: '',
          stage_error_message: '',
          current_result_id: 8,
          history_count: 2,
          award_name: '全国研究生渔菁英挑战赛',
          verdict: '需要人工复核',
          confidence: 'medium',
          triage: 'manual',
          review_status: '待复核',
          identity_version: 'identity-v2',
          source_kind: 'pdf',
          source_url: 'https://official.example/2025',
          source_urls: ['https://official.example/2025'],
          found_assets: ['https://official.example/roster.pdf'],
          page_year: '2025',
          extracted_count: 93,
          submitted_count: 93,
          missing: ['青海逐浪', '摸鱼能干队'],
          extra: ['菁海逐浪', '摸鱼能手队'],
          reason_codes: ['partial_overlap'],
          notes: '91 / 93 matched',
          created_at: '2026-08-04T00:00:00Z',
          binding: {
            case_id: 3,
            case_status: 'waiting_human',
            origin_m4_result_id: 8,
            is_current: true,
          },
        }],
      },
    }),
  )

  await page.setViewportSize({ width: 1440, height: 960 })
  await page.goto('/')
  await page.getByTestId('m4-results-1').click()
  const dialog = page.getByTestId('m4-results-dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('全国研究生渔菁英挑战赛')).toBeVisible()
  await expect(dialog.getByText('93 / 93')).toBeVisible()
  await expect(dialog.getByText('已绑定当前 M4 结果')).toBeVisible()
  await expect(dialog.getByText('案件 #3')).toBeVisible()
  await expect(dialog.getByRole('link', { name: 'https://official.example/2025' })).toBeVisible()
  await expect(dialog.getByRole('link', { name: 'https://official.example/roster.pdf' })).toBeVisible()
  await expect(dialog.getByText('青海逐浪')).toBeVisible()
  await expect(dialog.getByText('菁海逐浪')).toBeVisible()
  await page.screenshot({ path: path.join(shots, 'desktop-m4-results.png'), fullPage: true })
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

test('queued supplement can rerun a completed M5 stage', async ({ page }) => {
  const batchResponse = await page.request.get('/api/batches')
  const batchPayload = await batchResponse.json() as { batches: Array<Record<string, any>> }
  batchPayload.batches[0].stages.m4.status = 'done'
  batchPayload.batches[0].stages.m5.status = 'done'
  batchPayload.batches[0].case_counts.queued = 1
  await page.route(
    (url) => url.pathname === '/api/batches',
    async (route) => route.fulfill({ status: 200, contentType: 'application/json', json: batchPayload }),
  )

  await page.goto('/')
  const row = page.getByRole('row').filter({ hasText: '2026 年示范评奖批次' })
  await expect(row.getByRole('button', { name: 'M5 审查', exact: true })).toBeEnabled()
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

test('desktop batch, issue and evidence workflow', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '批次总览' })).toBeVisible()
  await expect(page.getByLabel('选择文件')).toBeVisible()
  await expect(page.getByRole('button', { name: '导入批次' })).toBeVisible()
  await expect(page.getByText('2026 年示范评奖批次')).toBeVisible()
  await expect(page.getByText('128', { exact: true })).toBeVisible()
  await page.screenshot({ path: path.join(shots, 'desktop-batches.png'), fullPage: true })

  await page.getByRole('button', { name: '问题' }).click()
  await expect(page.getByText('推荐单位为空')).toBeVisible()

  await page.getByRole('button', { name: '疑难案件' }).click()
  await page.getByText('示范科技奖').click()
  const dialog = page.getByRole('dialog', { name: '疑难案件详情' })
  await expect(dialog).toBeVisible()
  await expect(
    dialog.getByRole('heading', { name: '核验来源与过程' }),
  ).toBeVisible()
  await expect(page.getByText('当前审查结论')).toBeVisible()
  await expect(dialog).not.toContainText('[object Object]')
  await expect(dialog.getByRole('link', {
    name: 'https://official.example/award',
    exact: true,
  })).toHaveCount(2)
  await expect(dialog.getByRole('link', { name: '官方候选名单.xlsx' })).toBeVisible()
  await expect(dialog.getByText('名单比对基准（1 个文件）：demo-submitted.xlsx')).toHaveCount(2)
  await expect(dialog.getByRole('link', { name: /award-images/ })).toBeVisible()
  await expect(dialog.getByText('待识别网页图片：1 张')).toBeVisible()
  await expect(dialog.getByText(/自动取证预算达到上限/)).toBeVisible()
  await expect(dialog.getByText(/附件核验完成前不能判定来源整体未覆盖/)).toBeVisible()
  await expect(dialog.getByText('提交名单有，该来源未找到')).toBeVisible()
  await expect(dialog.getByText('来源使用群体名额，提交材料使用个人姓名')).toBeVisible()
  await expect(dialog.getByText('示例人员乙', { exact: true })).toBeVisible()
  await expect(dialog.getByText('该来源有，提交名单未提供')).toBeVisible()
  await expect(dialog.getByText('示例人员丙', { exact: true })).toBeVisible()
  await expect(dialog.getByText('等待附件继续核验')).toBeVisible()
  await expect(dialog.getByText('示例人员丁', { exact: true })).toBeVisible()
  await expect(dialog.getByText('联合记录如何计数')).toBeVisible()
  await expect(dialog.getByText('李桂枝、王伟江', { exact: true })).toBeVisible()
  await expect(dialog.getByText(/按 1 条联合记录计数/)).toBeVisible()
  await expect(dialog.getByText(/不属于“未找到”/)).toBeVisible()
  await expect(dialog.getByText('搜索候选网址')).toBeVisible()
  await expect(dialog.getByRole('link', {
    name: 'https://official.example/candidate-2',
    exact: true,
  })).toBeVisible()
  await expect(dialog.getByText('尚未访问')).toBeVisible()
  await page.getByRole('button', { name: /official-roster\.pdf/ }).click()
  await expect(page.locator('iframe[title="official-roster.pdf"]')).toBeVisible()
  await page.getByRole('button', { name: /scanned-page\.png/ }).click()
  await expect(page.locator('img[alt="scanned-page.png"]')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  await page.screenshot({ path: path.join(shots, 'desktop-case-evidence.png'), fullPage: true })
})

test('mobile navigation and drawer remain usable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '批次总览' })).toBeVisible()
  await page.getByRole('button', { name: '疑难案件' }).click()
  await page.getByText('示范科技奖').click()
  const dialog = page.getByRole('dialog', { name: '疑难案件详情' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('当前审查结论')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  await expect(dialog.getByRole('button', { name: '确认符合要求' })).toBeInViewport()
  await page.screenshot({ path: path.join(shots, 'mobile-case-drawer.png'), fullPage: true })
})

test('human decision explains its effect and requires confirmation', async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 850 })
  await page.goto('/')
  await page.getByRole('button', { name: '疑难案件' }).click()
  await page.getByText('示范科技奖').click()
  await page.getByRole('button', { name: '证据不足，暂缓结论' }).click()
  const confirmation = page.getByRole('alertdialog', { name: '确认人工决定' })
  await expect(confirmation).toBeVisible()
  await expect(confirmation).toContainText('不会发送给其他人员或外部系统')
  const detail = await page.request.get('/api/audit-cases/1')
  expect((await detail.json()).case.status).toBe('waiting_human')
  await confirmation.getByRole('button', { name: '取消' }).click()
  await expect(confirmation).toBeHidden()
})

test('stale human action is shown as an optimistic conflict', async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 800 })
  await page.goto('/')
  await page.getByLabel('复核人').fill('browser-reviewer')
  await page.getByRole('button', { name: '疑难案件' }).click()
  await page.getByText('示范科技奖').click()
  const detail = await page.request.get('/api/audit-cases/1')
  const payload = await detail.json()
  const version = payload.case.state_version as number
  const first = await page.request.post('/api/audit-cases/1/supplement', {
    headers: { 'X-Reviewer': 'second-reviewer' },
    data: { request: '第二客户端更新', expected_version: version },
  })
  expect(first.ok()).toBeTruthy()

  await page.getByPlaceholder('说明通过、不通过、补证或暂缓判断的具体原因').fill('使用已过期版本提交')
  await page.getByRole('button', { name: '确认符合要求' }).click()
  await expect(page.getByRole('alertdialog', { name: '确认人工决定' })).toBeVisible()
  await page.getByRole('button', { name: '提交终审通过' }).click()
  await expect(page.getByText('数据已被其他复核人更新，请刷新后重试。')).toBeVisible()
})
