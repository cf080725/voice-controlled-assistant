/** Local-owner device review UI. Credentials stay inside the injected API closure. */
const LABELS = {
  preparing: '正在理解请求', awaiting_input: '待补充信息', awaiting_confirmation: '待你确认',
  running: '电脑正在处理', succeeded: '已完成', failed: '未完成', cancelled: '已取消', unknown: '结果待核对',
};
const CANCELLABLE = new Set(['preparing', 'awaiting_input', 'awaiting_confirmation']);
const ERROR_TEXT = {
  stale_confirmation: '任务内容已更新，请重新核对后再操作。',
  invalid_confirmation: '请重新打开任务，核对最新内容。',
  binding_changed: '设备所属会话已改变，请先核对当前使用者和设备授权。',
  context_changed: '当前会话已改变，请重新核对设备授权。',
  client_unavailable: '此客户端已撤销或无法提出任务，请先检查客户端登记。',
  invalid_token: '本机连接已失效，请重新连接。',
  local_owner_required: '请在电脑本机连接后进行确认。',
  cannot_cancel_running: '电脑已经开始处理，请在电脑上核对结果。',
  busy: '任务仍在处理中，请稍后刷新。',
  not_found: '此任务已不可用，请刷新列表。',
  forbidden: '当前连接无法进行此操作，请核对本机权限。',
  authentication_required: '请先连接本机控制中心。',
};

const copy = value => JSON.parse(JSON.stringify(value));
const positiveVersion = value => Number.isSafeInteger(value) && value > 0;
const taskId = row => row && typeof row.task_id === 'string' && row.task_id.length > 0 ? row.task_id : null;
const text = value => typeof value === 'string' ? value : '';
const shorten = (value, length) => Array.from(value).slice(0, length).join('');
const canonical = value => {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  return value;
};
const reviewSignature = row => JSON.stringify(canonical({
  task_id: row.task_id, client_id: row.client_id, state: row.state, text: row.text,
  preview: row.preview, question: row.question, input_version: row.input_version,
  confirmation_version: row.confirmation_version, answers: row.answers,
}));
const canConfirm = (row, catalogue) => row.state === 'awaiting_confirmation' && positiveVersion(row.confirmation_version)
  && typeof row.text === 'string' && row.preview !== null && typeof row.preview === 'object' && !Array.isArray(row.preview)
  && describePlan(row.preview, catalogue).reviewable;
const canCancel = row => CANCELLABLE.has(row.state) && positiveVersion(row.confirmation_version);

const RESULT_KEYS = ['protocol_version', 'format_version', 'request_id', 'task_id', 'result_revision',
  'content', 'document_digest', 'page_count', 'document_bytes', 'document_scalars'];
const exactKeys = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const sha256Text = async value => {
  if (!globalThis.crypto?.subtle || typeof TextEncoder !== 'function') throw new Error('INTEGRITY_UNAVAILABLE');
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
};
const scalarCount = value => {
  if (typeof value !== 'string') throw new Error('Invalid Unicode text');
  let count = 0;
  for (const char of value) {
    const point = char.codePointAt(0);
    if (point >= 0xd800 && point <= 0xdfff) throw new Error('Invalid Unicode text');
    count += 1;
  }
  return count;
};

const resultSnapshot = value => {
  if (typeof TextEncoder !== 'function') throw new Error('INTEGRITY_UNAVAILABLE');
  let serialized;
  try { serialized = typeof value === 'string' ? value : JSON.stringify(value); }
  catch { throw new Error('Invalid result snapshot'); }
  if (typeof serialized !== 'string' || new TextEncoder().encode(serialized).length > 8192) {
    throw new Error('Invalid result snapshot');
  }
  return JSON.parse(serialized);
};

/** Read already captured data through the owner's API closure, never execute. */
export async function readDeviceQueryResult(api, row, digestText = sha256Text, isCurrent = () => true) {
  if (typeof api !== 'function' || typeof digestText !== 'function') throw new Error('Invalid result reader');
  // Only these row identity fields participate in result reads. Capture them
  // before the first await without copying unrelated, potentially long history.
  row = resultSnapshot({protocol_version: row?.protocol_version, request_id: row?.request_id, task_id: row?.task_id});
  if (!taskId(row)) throw new Error('Invalid result reader');
  const current = () => { if (!isCurrent()) throw new Error('STALE_VIEW'); };
  current();
  const path = 'device-text/review/' + encodeURIComponent(row.task_id) + '/result';
  const manifest = resultSnapshot(await api(path));
  current();
  const hash = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
  if (!exactKeys(manifest, RESULT_KEYS) || ![1, 2].includes(manifest.protocol_version) || manifest.protocol_version !== row.protocol_version
      || manifest.request_id !== row.request_id || manifest.task_id !== row.task_id
      || manifest.format_version !== 1 || manifest.content !== 'canaan.query.document.v2'
      || !positiveVersion(manifest.result_revision) || !hash(manifest.document_digest)
      || !positiveVersion(manifest.page_count) || manifest.page_count > 2048
      || !positiveVersion(manifest.document_bytes) || manifest.document_bytes > 2097152
      || !positiveVersion(manifest.document_scalars) || manifest.document_scalars > 2097152
      || typeof TextEncoder !== 'function') throw new Error('Invalid result manifest');
  const encoder = new TextEncoder();
  const chunks = [];
  let bytes = 0, scalars = 0;
  for (let index = 0; index < manifest.page_count; index += 1) {
    current();
    const page = resultSnapshot(await api(path + '/' + manifest.result_revision + '/' + index));
    current();
    if (!exactKeys(page, [...RESULT_KEYS, 'page_index', 'text', 'page_digest'])
        || !RESULT_KEYS.every(key => page[key] === manifest[key]) || page.page_index !== index
        || typeof page.text !== 'string' || !hash(page.page_digest)
        || scalarCount(page.text) < 1 || scalarCount(page.text) > 1024
        || encoder.encode(JSON.stringify(page)).length > 8192
        || await digestText(page.text) !== page.page_digest) throw new Error('Invalid result page');
    bytes += encoder.encode(page.text).length;
    scalars += scalarCount(page.text);
    if (bytes > manifest.document_bytes || scalars > manifest.document_scalars) throw new Error('Result budget exceeded');
    chunks.push(page.text);
  }
  const raw = chunks.join('');
  if (bytes !== manifest.document_bytes || scalars !== manifest.document_scalars
      || await digestText(raw) !== manifest.document_digest) throw new Error('Result integrity mismatch');
  const record = JSON.parse(raw);
  if (!exactKeys(record, ['schema', 'plan_steps', 'completed_steps', 'expected_query_steps', 'stop_reason', 'answers'])
      || record.schema !== 'canaan.query.document.v2' || !positiveVersion(record.plan_steps) || record.plan_steps > 32
      || !Number.isInteger(record.completed_steps) || record.completed_steps < 0 || record.completed_steps > record.plan_steps
      || !Array.isArray(record.expected_query_steps) || !Array.isArray(record.answers)
      || !['completed', 'confirmation_required', 'failed', 'cancelled'].includes(record.stop_reason)
      || record.answers.length > 32 || record.answers.some((answer, index) => !answer || typeof answer.full_text !== 'string'
        || typeof answer.target !== 'string' || !positiveVersion(answer.step) || answer.step > record.completed_steps
        || index > 0 && answer.step <= record.answers[index - 1].step)) throw new Error('Invalid complete query record');
  current();
  return {manifest, record};
}

function problem(error) {
  if (error && Object.hasOwn(ERROR_TEXT, error.code)) return ERROR_TEXT[error.code];
  if (error?.status === 401) return '请先连接本机控制中心。';
  if (error?.status === 503) return '设备任务暂未就绪，请稍后刷新。';
  if (error?.status === 403) return '当前连接无法进行此操作，请核对本机权限。';
  // Arbitrary backend exception strings can contain paths, credentials or stacks.
  return '暂时无法完成操作，请稍后刷新；没有自动重试执行。';
}

function publicDescription(value) {
  if (typeof value === 'string') return value.trim();
  if (Array.isArray(value) && value.every(item => typeof item === 'string')) return value.join('\n').trim();
  return '';
}

function humanParameter(value, schema = {}, depth = 0) {
  if (depth > 8) return '';
  if (typeof value === 'string') {
    if (depth === 0) {
      try {
        const parsed = JSON.parse(value);
        if (parsed && typeof parsed === 'object') return humanParameter(parsed, schema, depth + 1);
      } catch { /* An ordinary target or message remains original text. */ }
    }
    return value;
  }
  if (typeof value === 'number') return String(value);
  if (typeof value === 'boolean') return value ? '开启' : '关闭';
  if (Array.isArray(value)) return value.map(item => humanParameter(item, schema.items || {}, depth + 1)).filter(Boolean).join('、');
  if (!value || typeof value !== 'object') return '';
  const fields = [
    ['recipient', '收件人'], ['receiver', '收件人'], ['contact', '收件人'], ['to', '收件人'],
    ['text', '正文'], ['message', '正文'], ['content', '内容'], ['body', '正文'],
    ['app', '应用'], ['name', '目标'], ['target', '目标'], ['url', '网页'],
    ['file', '文件'], ['path', '路径'], ['file_path', '文件'], ['folder', '文件夹'],
    ['directory', '文件夹'], ['src', '原文件'], ['dst', '目标文件'],
    ['from_path', '原路径'], ['to_path', '目标路径'], ['files', '文件'], ['paths', '路径'],
    ['time', '时间'], ['when', '时间'], ['at', '时间'], ['date', '日期'],
    ['minutes', '分钟后'], ['seconds', '秒后'], ['value', '内容'], ['instruction', '处理内容'],
  ];
  const labels = new Map(fields);
  const hidden = new Set(['source', 'confidence', 'conf', 'pconf', 'hmac', 'evidence', 'signature',
    'authority', 'authorization', 'owner_token', 'bearer_token', 'plan_evidence']);
  const properties = schema && typeof schema.properties === 'object' && schema.properties !== null ? schema.properties : {};
  const lines = [];
  for (const key of Object.keys(value)) {
    if (key.startsWith('_') || (hidden.has(key.toLowerCase()) && !Object.hasOwn(properties, key))) continue;
    const item = value[key];
    const property = Object.hasOwn(properties, key) ? properties[key] : {};
    const label = labels.get(key) || publicDescription(property?.title) || publicDescription(property?.description)
      || (/[^\x00-\x7f]/.test(key) ? key : '内容');
    const readable = humanParameter(item, property || {}, depth + 1);
    if (readable) lines.push(label + '：' + readable);
  }
  return lines.join('\n');
}

function describePlan(preview, catalogue = new Map(), depth = 0) {
  if (!preview || typeof preview !== 'object' || depth > 5) {
    return {description: '完整计划尚不可用，请稍后刷新。', reviewable: false};
  }
  const plan = preview.pending && typeof preview.pending === 'object'
    ? {...preview.pending, reply: preview.reply} : preview;
  const declared = publicDescription(preview.readback) || publicDescription(preview.summary)
    || publicDescription(plan.readback) || publicDescription(plan.summary);
  if (plan.action === 'multi_action') {
    let steps = plan.param;
    if (typeof steps === 'string') {
      try { steps = JSON.parse(steps); } catch { steps = null; }
    }
    if (Array.isArray(steps) && steps.length) {
      const readable = steps.map(step => describePlan(step, catalogue, depth + 1));
      return {
        description: [declared, ...readable.map((step, index) => `${index + 1}. ${step.description}`)].filter(Boolean).join('\n'),
        reviewable: readable.every(step => step.reviewable),
      };
    }
  }
  const registered = catalogue.get(plan.action);
  const action = registered ? registered.description : '';
  const parameter = humanParameter(plan.param, registered?.input_schema || {});
  const required = registered?.input_schema?.required;
  const requiresParameter = Array.isArray(required) && required.length > 0;
  const reviewable = Boolean(declared || (registered && action && (parameter || !requiresParameter)));
  return {
    description: [declared || action, parameter].filter(Boolean).join('\n')
      || '计划内容还不可核对，请刷新后再决定是否执行。',
    reviewable,
  };
}

export function createDeviceTasksPanel({api, document, taskList, notice, bindings, bindingNotice, digestText = sha256Text}) {
  if (typeof api !== 'function' || !document || !taskList || !notice || !bindings || !bindingNotice) {
    throw new TypeError('设备任务界面缺少必要连接。');
  }
  let reviews = [];
  let reviewState = 'idle';
  let catalogue = new Map();
  let clients = [];
  let refreshing = null;
  const busy = new Set();
  const queryResults = new Map();
  let resultEpoch = 0;

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function announce(node, message) { node.textContent = message; }

  function button(label, key, operation) {
    const node = element('button', '', label);
    node.type = 'button';
    node.disabled = busy.has(key);
    node.onclick = async () => {
      if (busy.has(key)) return;
      busy.add(key);
      node.disabled = true;
      try { await operation(); }
      finally {
        busy.delete(key);
        renderReviews();
        renderBindings();
      }
    };
    return node;
  }

  function replaceReview(row) {
    const id = taskId(row);
    if (!id) return;
    reviews = reviews.map(old => old.task_id === id ? copy(row) : old);
    renderReviews();
  }

  async function taskAction(row, verb) {
    const id = taskId(row);
    const path = 'device-text/review/' + encodeURIComponent(id);
    try {
      const latest = await api(path);
      if (taskId(latest) !== id || reviewSignature(latest) !== reviewSignature(row)) {
        if (taskId(latest) === id) replaceReview(latest);
        announce(notice, '任务内容已更新，请重新核对后再操作。');
        return;
      }
      if ((verb === 'confirm' && !canConfirm(latest, catalogue)) || (verb === 'cancel' && !canCancel(latest))) {
        announce(notice, '此任务当前不能这样操作，请核对最新状态。');
        return;
      }
      await api(path + '/' + verb, {expected_version: row.confirmation_version});
      announce(notice, verb === 'confirm' ? '已提交本机确认，完成情况以任务状态为准。' : '已提交取消，请核对更新后的任务状态。');
      await refresh({preserveNotice: true});
    } catch (error) {
      announce(notice, problem(error));
      await refresh({preserveNotice: true});
    }
  }

  async function details(row) {
    const id = taskId(row);
    try {
      const latest = await api('device-text/review/' + encodeURIComponent(id));
      if (taskId(latest) !== id) throw new Error('Task receipt mismatch');
      replaceReview(latest);
      announce(notice, '已更新此任务，请核对原话和将要执行的内容。');
    } catch (error) { announce(notice, problem(error)); }
  }

  async function queryDetails(row) {
    const epoch = resultEpoch;
    try {
      const result = await readDeviceQueryResult(api, row, digestText, () => epoch === resultEpoch);
      if (epoch !== resultEpoch) return;
      // At most one bounded full result in this transient desktop view.
      queryResults.clear();
      queryResults.set(row.task_id, result);
      announce(notice, '已读取并核对完整查询结果。');
    } catch (error) {
      if (epoch !== resultEpoch) return;
      queryResults.delete(row.task_id);
      announce(notice, error?.code === 'result_unavailable' ? '此任务没有可读取的完整查询结果。'
        : error?.message === 'INTEGRITY_UNAVAILABLE' ? '当前页面暂时无法核对完整结果，请保留任务并稍后查看。' : problem(error));
    }
  }

  function renderReviews() {
    taskList.replaceChildren();
    if (!reviews.length) {
      const message = reviewState === 'error' ? '暂时无法读取设备任务。请刷新后再核对。'
        : reviewState === 'idle' ? '连接本机控制中心后查看设备任务。'
          : '目前没有设备任务。设备授权不代表手表已连接。';
      taskList.append(element('div', 'empty', message));
      return;
    }
    for (const source of reviews) {
      const id = taskId(source);
      if (!id) continue;
      const row = copy(source);
      const key = 'task:' + id;
      const card = element('article', 'task-card');
      card.dataset.taskId = id;
      const heading = element('div', 'task-heading');
      heading.append(element('h3', '', shorten(text(row.text), 100) || '设备发来的任务'));
      const attention = ['awaiting_input', 'awaiting_confirmation', 'unknown'].includes(row.state);
      heading.append(element('span', 'task-state' + (attention ? ' attention' : ''), Object.hasOwn(LABELS, row.state) ? LABELS[row.state] : '状态待核对'));
      card.append(heading);
      card.append(element('p', 'task-byline', (text(row.client_name) || '外部设备') + ' · 在这台电脑处理'));
      const original = element('details');
      original.append(element('summary', '', '查看原话与处理内容'));
      const answers = Array.isArray(row.answers) ? row.answers.filter(answer => answer && typeof answer.text === 'string') : [];
      const answerText = answers.length ? '\n\n补充信息：\n' + answers.map((answer, index) => `${index + 1}. ${answer.text}`).join('\n') : '';
      original.append(element('pre', '', '设备原话：\n' + text(row.text) + answerText
        + '\n\n将要处理：\n' + describePlan(row.preview, catalogue).description));
      original.open = row.state === 'awaiting_confirmation';
      card.append(original);
      if (row.state === 'awaiting_input') {
        card.append(element('p', 'task-note', (text(row.question) || '任务还需要补充信息。') + ' 请在发起设备上补充；这一步不会批准执行。'));
      } else if (row.state === 'unknown') {
        card.append(element('p', 'task-note', '上次处理结果尚未确认，请在电脑核对。这里不会自动重做。'));
      } else if (row.reply) {
        card.append(element('p', 'task-note', row.protocol_version === 2 ? text(row.reply) : shorten(text(row.reply), 240)));
      }
      if (row.state === 'awaiting_confirmation' && !canConfirm(row, catalogue)) {
        card.append(element('p', 'task-note', '处理内容还不可核对，暂不能执行。请刷新完整详情。'));
      }
      const actions = element('div', 'task-actions');
      actions.append(button('核对最新详情', key, () => details(row)));
      actions.append(button('查看完整查询结果', key, () => queryDetails(row)));
      if (canConfirm(row, catalogue)) actions.append(button('核对并执行', key, () => taskAction(row, 'confirm')));
      if (canCancel(row)) actions.append(button('取消待执行部分', key, () => taskAction(row, 'cancel')));
      card.append(actions);
      const query = queryResults.get(id);
      if (query) {
        const complete = element('details');
        complete.open = true;
        complete.append(element('summary', '', '完整查询结果（已核对）'));
        if (query.record.stop_reason !== 'completed') complete.append(element('p', 'task-note', '任务尚未全部完成，以下保留已完成部分的查询结果。'));
        complete.append(element('pre', '', query.record.answers.map(answer => `第${answer.step}项（${answer.target}）：${answer.full_text}`).join('\n')));
        card.append(complete);
      }
      taskList.append(card);
    }
  }

  async function refresh({preserveNotice = false} = {}) {
    if (refreshing) return refreshing;
    resultEpoch += 1;
    refreshing = Promise.resolve().then(async () => {
      try {
        const [rows, actions] = await Promise.all([api('device-text/review'), api('actions')]);
        if (!Array.isArray(rows) || rows.some(row => !taskId(row))) throw new Error('Invalid review list');
        if (!Array.isArray(actions)) throw new Error('Invalid action catalogue');
        const nextCatalogue = new Map();
        for (const action of actions) {
          if (!action || typeof action.name !== 'string' || !action.name || typeof action.description !== 'string') continue;
          const description = publicDescription(action.description);
          if (!description) continue;
          const entry = {description, input_schema: action.input_schema || {}};
          nextCatalogue.set(action.name, entry);
          if (Array.isArray(action.aliases)) for (const alias of action.aliases) {
            if (typeof alias === 'string' && alias) nextCatalogue.set(alias, entry);
          }
        }
        catalogue = nextCatalogue;
        queryResults.clear();
        reviews = copy(rows);
        reviewState = 'ready';
        renderReviews();
        if (!preserveNotice) announce(notice, '已更新设备任务。授权或登记不代表真机已经连接。');
        return copy(reviews);
      } catch (error) {
        reviews = [];
        reviewState = 'error';
        renderReviews();
        if (!preserveNotice) announce(notice, problem(error));
        return [];
      } finally { refreshing = null; }
    });
    return refreshing;
  }

  async function bindingAction(client, revoke) {
    try {
      if (revoke) await api('device-text/bindings/' + encodeURIComponent(client.id) + '/revoke', {});
      else await api('device-text/bindings', {client_id: client.id});
      announce(bindingNotice, revoke ? '已取消此设备的会话授权。客户端令牌登记保持原状。'
        : '已允许此设备在当前会话使用。换人或会话改变后需重新授权；这不代表手表已连接。');
    } catch (error) { announce(bindingNotice, problem(error)); }
  }

  function renderBindings(next = clients) {
    clients = Array.isArray(next) ? copy(next) : [];
    bindings.replaceChildren();
    const eligible = clients.filter(client => client && typeof client.id === 'string' && client.id
      && !client.revoked_at && Array.isArray(client.caps) && client.caps.includes('propose'));
    if (!eligible.length) {
      bindings.append(element('p', 'task-note', '先在上方生成可提出任务的客户端令牌，再单独授权需要的设备。'));
      return;
    }
    for (const client of eligible) {
      const row = element('div', 'task-card');
      row.dataset.clientId = client.id;
      row.append(element('h3', '', text(client.name) || '已登记的客户端'));
      row.append(element('p', 'task-note', '需要时逐个授权当前会话；登记名称不是设备已连接的证明。'));
      const actions = element('div', 'task-actions');
      const key = 'client:' + client.id;
      actions.append(button('允许在当前会话使用', key, () => bindingAction(client, false)));
      actions.append(button('取消此设备授权', key, () => bindingAction(client, true)));
      row.append(actions);
      bindings.append(row);
    }
  }

  function clear() {
    resultEpoch += 1;
    queryResults.clear();
    reviews = [];
    reviewState = 'idle';
    clients = [];
    taskList.replaceChildren(element('div', 'empty', '连接本机控制中心后查看设备任务。'));
    bindings.replaceChildren(element('p', 'task-note', '连接后显示可单独授权的客户端。'));
    announce(notice, '');
    announce(bindingNotice, '');
  }

  return {refresh, renderBindings, clear};
}
