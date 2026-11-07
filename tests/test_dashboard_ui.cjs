const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'shells', 'canaan_control.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
assert.equal(ids.length, new Set(ids).size, '控件编号不能重复');
for(const match of script.matchAll(/\$\('([^']+)'\)/g))assert.ok(ids.includes(match[1]), '缺少调用者引用的控件：'+match[1]);
const context = vm.createContext({URL});
vm.runInContext(script.match(/const actionNames=.*;/)[0] + '\n' + script.slice(script.indexOf('const owners='), script.indexOf('function renderTasks(')), context);
const rows = (local, services) => JSON.parse(JSON.stringify(context.boardRows(local, services)));
const local = (state, detail = {}) => ({id:'local1', action:'openmuse_task', param:'研究网页', state, created_at:1, result:{detail}});
const remote = {id:'remote1', title:'研究网页', status:'running', updated:2, steps:[1,3], current:'检查来源', note:''};
const linked = local('succeeded', {provider:'muse', task_id:'remote1', completed:false});

let actual = rows([linked], {muse:{online:true, tasks:[remote]}});
assert.equal(actual.length, 1, '同一远端任务只显示一次');
assert.equal(actual[0].status, 'running', '提交成功不能冒充执行完成');
assert.equal(actual[0].progress, '检查来源');
assert.equal(actual[0].canCancel, false, '本机提案取消不能冒充远端取消');

actual = rows([linked], {muse:{online:true, tasks:[{...remote,status:'failed',note:'网络错误'}]}});
assert.equal(actual[0].status, 'failed');
assert.equal(actual[0].note, '网络错误');
assert.equal(context.taskSituation(actual[0]), '网络错误');
assert.equal(context.taskSituation({status:'failed',note:'Research exceeded the 90 second time limit.'}), '研究超时（90 秒），任务未完成。');
assert.equal(context.taskSituation({status:'failed',note:'Research exceeded the 300 second time limit.'}), '研究超时（300 秒），任务未完成。');
assert.equal(context.taskSituation({status:'unknown',action:'daily_briefing',progress:'正在汇总消息'}), '上次总结运行被中断，结果未确认。请查看最新总结或重新运行。');
assert.equal(context.taskSituation({status:'failed',note:'{"error":',progress:'已完成 0 / 4 步'}), '任务执行失败，详细错误见任务详情或工作台。');
actual = rows([linked], {muse:{online:true, tasks:[{...remote,status:'done',note:'42'}]}});
assert.equal(actual[0].status, 'done');
assert.equal(actual[0].note, '42');
actual = rows([linked], {muse:{online:true, tasks:[{...remote,status:'waiting'}]}});
assert.equal(actual[0].awaiting, false, '远端等待输入不能误用本机确认接口');

assert.equal(rows([linked], {muse:{online:false,tasks:[remote]}})[0].status, 'unknown');
assert.equal(rows([linked], {muse:{online:true,tasks:[]}})[0].status, 'unknown');
assert.equal(rows([linked], {})[0].status, 'unknown');
actual = rows([local('awaiting_confirmation')], {muse:{online:true,tasks:[remote]}});
assert.equal(actual[0].status, 'waiting');
assert.equal(actual[0].awaiting, true);
assert.equal(actual[0].canCancel, true);

actual = rows([
 {id:'summary',action:'daily_briefing',state:'succeeded',param:{sources:['imapp']},result:{detail:{todos:['通知'],highlights:['要点']}}},
 {id:'pending',action:'computer_use',state:'queued',param:'打开记事本',result:{}},
], {dots:{online:true,tasks:[{id:'d1',title:'每天整理资料',status:'scheduled',updated:2}]}});
assert.equal(actual.find(t=>t.key==='canaan:summary').title, '整理聊天软件消息');
assert.equal(actual.find(t=>t.key==='canaan:summary').note, '已整理 1 条行动通知、1 条要点。');
assert.equal(context.visibleBoardRows(actual,'all','active').length, 2);
assert.equal(context.visibleBoardRows(actual,'dots','active').length, 1);
assert.equal(context.visibleBoardRows(actual,'canaan','done').length, 1);
assert.equal(context.visibleBoardRows(actual,'all','attention').length, 0);
assert.equal(context.visibleBoardRows(actual,'all','all','每天').length,1);
assert.equal(context.visibleBoardRows(actual,'all','all','不存在的任务').length,0);
assert.equal(context.displayAgentResult({text:'真实结果'}),'真实结果');
assert.ok(context.displayAgentResult({text:'a'.repeat(5000)+'结束',sample:true}).startsWith('演示数据，请核对\n结果较长'));
assert.ok(context.displayAgentResult({text:'a'.repeat(5000)+'结束'}).endsWith('结束'));

assert.equal(context.safeWorkbenchUrl('http://localhost:4311'), 'http://localhost:4311/');
assert.equal(context.safeWorkbenchUrl('http://[::1]:8081/'), 'http://[::1]:8081/');
for(const value of ['javascript:alert(1)','https://example.com/','http://localhost.evil.test','http://user:pass@localhost:4311',undefined])assert.equal(context.safeWorkbenchUrl(value),null);

let healthText='';
context.show=(id,value)=>{assert.equal(id,'integrationHealth');healthText=value;};
context.timeLabel=()=> '测试时间';
vm.runInContext(script.slice(script.indexOf('function integrationHealth('),script.indexOf('async function connections(')),context);
context.integrationHealth({feishu:{listener:{state:'connected',last_event_at:0}},claw:{registration:{client:'AutoClaw',tools:19,probed_at:1}}});
assert.ok(healthText.includes('长连接已连通 · 本次连接尚无新事件'));
assert.ok(healthText.includes('已登记团团 · 最近探测 19 个工具'));
assert.ok(healthText.includes('云端只读调用尚未验收'));
context.integrationHealth({claw:{registration:{client:'AutoClaw',tools:21,probed_at:1,cloud_readonly_verified_at:2}}});
assert.ok(healthText.includes('云端只读调用已验收 · 测试时间'));
assert.ok(!healthText.includes('尚未验收'));
context.integrationHealth({claw:{registration:{client:'AutoClaw',cloud_proposal_verified_at:3}}});
assert.ok(healthText.includes('云端任务提案已验收 · 仍需你确认执行'));
context.integrationHealth({feishu:{listener:{state:'offline',last_event_at:1}}});
assert.ok(healthText.includes('监听心跳已过期 · 最近事件 测试时间'));
assert.ok(healthText.includes('尚未登记本机客户端'));

console.log('Dashboard task merging, filtering, confirmation and local link checks passed.');
