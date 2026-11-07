// 纯函数：事件 → 要报给团团的进度（不依赖 vscode，方便用 node --test 测）。
"use strict";

const MAX_TITLE = 80;

function clip(text) {
  const t = String(text || "").replace(/\s+/g, " ").trim();
  return t.length > MAX_TITLE ? t.slice(0, MAX_TITLE - 1) + "…" : t;
}

// 命令行只报“程序名 + 第一个子命令”，不报参数（参数里可能有路径、令牌）
function commandLabel(commandLine) {
  const parts = String(commandLine || "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "终端命令";
  const prog = parts[0].replace(/^.*[\\/]/, "");
  const sub = parts[1] && /^[a-z][\w:-]*$/i.test(parts[1]) ? " " + parts[1] : "";
  return prog + sub;
}

function taskStart(name, key) {
  return { kind: "progress", title: clip("任务开始：" + name), key: "task:" + key };
}

function taskEnd(name, key, exitCode) {
  const ok = exitCode === 0 || exitCode === undefined;
  return { kind: ok ? "done" : "failed", title: clip((ok ? "任务完成：" : "任务失败：") + name + (ok ? "" : "（退出码 " + exitCode + "）")), key: "task:" + key };
}

function debugEnd(name, key) {
  return { kind: "info", title: clip("调试结束：" + name), key: "debug:" + key };
}

function terminalEnd(commandLine, seconds, exitCode, minSeconds) {
  if (seconds < minSeconds) return null;          // 太快的命令不打扰
  const label = commandLabel(commandLine);
  const ok = exitCode === 0 || exitCode === undefined;
  return { kind: ok ? "done" : "failed", title: clip((ok ? "命令完成：" : "命令失败：") + label + "（" + Math.round(seconds) + " 秒）"),
           key: "term:" + label };
}

// 只允许本机地址，防止令牌被发到别处
function isLocalUrl(url) {
  try {
    const u = new URL(url);
    return (u.protocol === "http:" || u.protocol === "https:") && ["127.0.0.1", "localhost", "[::1]", "::1"].includes(u.hostname);
  } catch (e) {
    return false;
  }
}

module.exports = { clip, commandLabel, taskStart, taskEnd, debugEnd, terminalEnd, isLocalUrl, MAX_TITLE };
