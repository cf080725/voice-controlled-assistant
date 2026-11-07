// 团团拉起 Muse / Dots 时通过 NODE_OPTIONS=--require 预加载：
// 没写地址或写成 0.0.0.0 / :: 的 TCP 监听一律改为只监听本机 127.0.0.1，局域网和公网都访问不到。
// Expo 开发服务器（Muse 网页工作台 8081）默认监听所有网卡，这里在不改 Muse 源码的前提下收紧。
'use strict';
const net = require('net');

const ANY = new Set([undefined, null, '', '0.0.0.0', '::', '[::]']);
const original = net.Server.prototype.listen;

net.Server.prototype.listen = function (...args) {
  const first = args[0];
  if (first === undefined || typeof first === 'function') {
    args.splice(0, first === undefined ? 1 : 0, 0, '127.0.0.1'); // listen() / listen(cb)：随机端口
  } else if (first !== null && typeof first === 'object' && !Array.isArray(first)) {
    if (first.path === undefined && first.fd === undefined && first.handle === undefined && ANY.has(first.host)) {
      args[0] = { ...first, host: '127.0.0.1' };
    }
  } else if (typeof first === 'number' || (typeof first === 'string' && /^\d+$/.test(first))) {
    // listen(port[, host][, backlog][, cb])
    if (typeof args[1] === 'function' || args.length < 2) {
      args.splice(1, 0, '127.0.0.1');
    } else if (typeof args[1] === 'number') {
      args.splice(1, 0, '127.0.0.1'); // listen(port, backlog, cb)
    } else if (ANY.has(args[1])) {
      args[1] = '127.0.0.1';
    }
  }
  return original.apply(this, args);
};
