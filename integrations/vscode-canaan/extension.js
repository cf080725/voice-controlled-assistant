// VS Code 插件：把任务 / 调试 / 长时间终端命令的进度报给本机的团团。
// 令牌放在 VS Code 的 SecretStorage 里（命令“团团：设置进度令牌”），只发往本机地址；报不出去时安静失败，状态栏显示一下。
"use strict";
const vscode = require("vscode");
const http = require("http");
const lib = require("./lib");

let statusItem;

function config() {
  return vscode.workspace.getConfiguration("canaan");
}

async function report(context, payload) {
  if (!payload) return;
  const token = await context.secrets.get("canaan.token");
  const base = config().get("apiUrl");
  if (!token || !lib.isLocalUrl(base)) {
    setStatus("未设置令牌", true);
    return;
  }
  const url = new URL("/api/progress", base);
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  await new Promise((resolve) => {
    const req = http.request(url, {
      method: "POST", timeout: 3000,
      headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json", "Content-Length": body.length }
    }, (res) => {
      res.resume();
      setStatus(res.statusCode === 200 ? "已连接" : "团团拒绝（" + res.statusCode + "）", res.statusCode !== 200);
      resolve();
    });
    req.on("error", () => { setStatus("团团没在运行", true); resolve(); });
    req.on("timeout", () => { req.destroy(); resolve(); });
    req.end(body);
  });
}

function setStatus(text, warn) {
  if (!statusItem) return;
  statusItem.text = (warn ? "$(warning) " : "$(pulse) ") + "团团";
  statusItem.tooltip = "团团进度提示：" + text;
}

function activate(context) {
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 0);
  statusItem.command = "canaan.setToken";
  setStatus("待连接", false);
  statusItem.show();
  context.subscriptions.push(statusItem);

  context.subscriptions.push(vscode.commands.registerCommand("canaan.setToken", async () => {
    const token = await vscode.window.showInputBox({
      prompt: "粘贴团团为 VS Code 配对的令牌（python canaan.py api --pair \"VS Code\" --caps progress）",
      password: true, ignoreFocusOut: true
    });
    if (token) {
      await context.secrets.store("canaan.token", token.trim());
      await report(context, { kind: "info", title: "VS Code 已连接", key: "hello" });
    }
  }));
  context.subscriptions.push(vscode.commands.registerCommand("canaan.test", () =>
    report(context, { kind: "info", title: "这是一条测试提示", key: "test" })));

  const keyOf = (e) => (e.execution && e.execution.task ? e.execution.task.name : "task") + ":" + (e.processId || "");
  context.subscriptions.push(vscode.tasks.onDidStartTaskProcess((e) => {
    if (config().get("reportTasks")) report(context, lib.taskStart(e.execution.task.name, keyOf(e)));
  }));
  context.subscriptions.push(vscode.tasks.onDidEndTaskProcess((e) => {
    if (config().get("reportTasks")) report(context, lib.taskEnd(e.execution.task.name, e.execution.task.name, e.exitCode));
  }));
  context.subscriptions.push(vscode.debug.onDidTerminateDebugSession((s) => {
    if (config().get("reportDebug")) report(context, lib.debugEnd(s.name, s.id));
  }));
  // 终端命令（需要 VS Code 1.93+ 的终端集成）：记开始时间，结束时按时长决定报不报
  const started = new Map();
  if (vscode.window.onDidStartTerminalShellExecution) {
    context.subscriptions.push(vscode.window.onDidStartTerminalShellExecution((e) => started.set(e.execution, Date.now())));
    context.subscriptions.push(vscode.window.onDidEndTerminalShellExecution((e) => {
      const t0 = started.get(e.execution);
      started.delete(e.execution);
      if (!t0 || !config().get("reportTerminal")) return;
      const line = e.execution.commandLine ? e.execution.commandLine.value : "";
      report(context, lib.terminalEnd(line, (Date.now() - t0) / 1000, e.exitCode, config().get("minCommandSeconds")));
    }));
  }
}

function deactivate() {}

module.exports = { activate, deactivate };
