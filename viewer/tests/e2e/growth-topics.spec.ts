import { expect, test } from '@playwright/test';

const sourceId = '00000000-0000-4000-8000-000000000101';
const targetId = '00000000-0000-4000-8000-000000000102';
const sourceNote = '00000000-0000-4000-8000-000000000201';
const targetNote = '00000000-0000-4000-8000-000000000202';

test('topic maintenance requires merge approval and sends explicit split links', async ({ page }) => {
  let sourceStatus: 'active' | 'archived' = 'active';
  let sourceRevision = 1;
  let merged = false;
  let mergeBody: Record<string, unknown> | null = null;
  let splitBody: Record<string, unknown> | null = null;
  let noteBody: Record<string, unknown> | null = null;

  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON() as Record<string, unknown> | null;
    let status = 200;
    let json: unknown = {};
    if (path === '/api/auth/token') json = { token: 'local-token' };
    else if (path === '/api/locale') json = { locale: 'en' };
    else if (path === '/api/growth/local') json = {
      projects: [], bindings: [], features: [], snapshots: [], analysis_runs: [],
      opportunities: [], topic_proposals: [], growth_tasks: [],
    };
    else if (path === '/api/growth/local/sync') json = {
      account_id: '00000000-0000-4000-8000-000000000001',
      device_id: '00000000-0000-4000-8000-000000000002',
      cursor: 0, acknowledged_cursor: 0, pulled: 0, uploaded: 0, queued: 0,
      outbox: { pending: 0, acked: 0, conflict: 0, deleted: 0 },
    };
    else if (path === '/api/growth/local/sync/events') json = { events: [] };
    else if (path === '/api/growth/local/sync/workspace') json = {
      accounts: [], notes: [], tasks: [],
    };
    else if (path === '/api/growth/local/cloud/chats') json = { sessions: [] };
    else if (path === '/api/growth/local/cloud/topics') json = { topics: [
      { topic_id: sourceId, title: '登录机制', status: sourceStatus,
        revision: sourceRevision, parent_topic_id: null,
        canonical_topic_id: merged ? targetId : null },
      { topic_id: targetId, title: '认证与授权', status: 'active',
        revision: merged ? 2 : 1, parent_topic_id: null, canonical_topic_id: null },
    ] };
    else if (path === `/api/growth/local/cloud/topics/${sourceId}`
      || path === `/api/growth/local/cloud/topics/${targetId}`) {
      const isSource = path.endsWith(sourceId);
      json = {
        requested_topic_id: isSource ? sourceId : targetId,
        canonical_topic_id: isSource && merged ? targetId : isSource ? sourceId : targetId,
        is_alias: isSource && merged,
        topic: { topic_id: isSource && !merged ? sourceId : targetId,
          title: isSource && !merged ? '登录机制' : '认证与授权',
          status: isSource && !merged ? sourceStatus : 'active',
          revision: isSource && !merged ? sourceRevision : merged ? 2 : 1,
          parent_topic_id: null },
        notes: isSource && !merged ? [
          { note_id: sourceNote, topic_id: sourceId, content_text: '登录是身份识别', revision: 1 },
        ] : [
          { note_id: sourceNote, topic_id: targetId,
            content_text: '登录是身份识别', revision: 1 },
          { note_id: targetNote, topic_id: targetId,
            content_text: '授权决定可执行操作', revision: 1 },
        ],
        tasks: [], children: [], moves: [],
      };
    } else if (path === `/api/growth/local/cloud/topics/${sourceId}/notes`) {
      noteBody = body;
      status = 201;
      json = { note_id: body?.note_id };
    } else if (path === `/api/growth/local/cloud/topics/${sourceId}/update`) {
      sourceStatus = body?.status as 'active' | 'archived';
      sourceRevision += 1;
      json = { topic_id: sourceId, status: sourceStatus, revision: sourceRevision };
    } else if (path === `/api/growth/local/cloud/topics/${sourceId}/merge`) {
      mergeBody = body;
      merged = true;
      json = { source_topic_id: sourceId, target_topic_id: targetId };
    } else if (path === `/api/growth/local/cloud/topics/${targetId}/split`) {
      splitBody = body;
      status = 201;
      json = { revision: 3, children: [] };
    }
    await route.fulfill({ status, contentType: 'application/json',
      body: JSON.stringify(json) });
  });

  await page.goto('/#/growth');
  await page.locator('#growth-cloud-url').fill('http://cloud.test');
  await page.locator('#growth-cloud-token').fill('token');
  await page.getByRole('button', { name: 'Connect and sync' }).click();
  await expect(page.locator('#growth-topic-select')).toBeVisible();

  await page.locator('#growth-topic-note').fill('原始笔记');
  await page.getByRole('button', { name: 'Save note' }).click();
  await expect.poll(() => noteBody?.content_text).toBe('原始笔记');
  await page.getByRole('button', { name: 'Archive topic' }).click();
  await expect(page.getByRole('button', { name: 'Reactivate topic' })).toBeVisible();
  await page.getByRole('button', { name: 'Reactivate topic' }).click();

  const merge = page.getByRole('button', { name: 'Merge', exact: true });
  await expect(merge).toBeDisabled();
  await page.getByLabel('I checked the source and target and approve the merge').check();
  await merge.click();
  await expect.poll(() => mergeBody?.target_topic_id).toBe(targetId);

  await page.locator('#growth-topic-select').selectOption(targetId);
  await page.locator('#growth-topic-child-a').fill('身份认证');
  await page.locator('#growth-topic-child-b').fill('资源授权');
  await page.getByLabel('Association 登录是身份识别').selectOption('a');
  await page.getByLabel('Association 授权决定可执行操作').selectOption('b');
  await page.getByRole('button', { name: 'Split', exact: true }).click();
  await expect.poll(() => splitBody).not.toBeNull();
  const children = (splitBody as Record<string, unknown> | null)?.children as Array<{
    title: string; note_ids: string[];
  }>;
  expect(children.map((item) => [item.title, item.note_ids])).toEqual([
    ['身份认证', [sourceNote]], ['资源授权', [targetNote]],
  ]);
});
