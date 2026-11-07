"use strict";
const test = require("node:test");
const assert = require("node:assert");
const lib = require("../lib");

test("命令行只报程序名和子命令，不报参数", () => {
  assert.strictEqual(lib.commandLabel("npm run build --token=abc123"), "npm run");
  assert.strictEqual(lib.commandLabel("C:\\tools\\python.exe D:\\secret\\x.py"), "python.exe");
  assert.strictEqual(lib.commandLabel(""), "终端命令");
});

test("任务结束按退出码分完成 / 失败", () => {
  assert.strictEqual(lib.taskEnd("build", "build", 0).kind, "done");
  const f = lib.taskEnd("build", "build", 2);
  assert.strictEqual(f.kind, "failed");
  assert.ok(f.title.includes("退出码 2"));
});

test("太快的终端命令不报，长的报完成 / 失败", () => {
  assert.strictEqual(lib.terminalEnd("ls", 1, 0, 10), null);
  assert.strictEqual(lib.terminalEnd("pytest -q", 42, 1, 10).kind, "failed");
  assert.ok(lib.terminalEnd("pytest -q", 42, 0, 10).title.startsWith("命令完成：pytest"));
});

test("标题截短到 80 字", () => {
  assert.ok(lib.clip("x".repeat(200)).length <= lib.MAX_TITLE);
});

test("只允许本机地址", () => {
  assert.ok(lib.isLocalUrl("http://127.0.0.1:8789"));
  assert.ok(lib.isLocalUrl("http://localhost:8789"));
  assert.ok(!lib.isLocalUrl("http://evil.example.com:8789"));
  assert.ok(!lib.isLocalUrl("file:///C:/x"));
  assert.ok(!lib.isLocalUrl("not a url"));
});
