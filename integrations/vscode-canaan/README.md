# VS Code 插件：团团进度提示

把任务（Tasks）、调试会话、长时间终端命令的“开始 / 完成 / 失败”报给本机的团团，在灵动岛上提示。只报状态和标题：终端命令只报程序名和子命令，不报参数；不读代码，也不执行任何东西。

## 安装

1. 给 VS Code 单独配对一个只能报进度的令牌：团团看板“设置 → 外部客户端令牌”点“给 VS Code / 编程助手生成令牌（只能报进度）”。
   也可以用命令行：`python canaan.py api --pair "VS Code" --caps progress`
2. 安装插件（不用打包、不用联网）：`powershell -ExecutionPolicy Bypass -File integrations\vscode-canaan\install.ps1`，
   它把本文件夹复制到 `%USERPROFILE%\.vscode\extensions\local.canaan-progress-0.1.0\`；然后重启 VS Code。
   卸载：删掉这个目录。
3. 命令面板运行“团团：设置进度令牌”，粘贴第 1 步得到的令牌。令牌保存在 VS Code 的加密存储里，只会发往本机地址。
4. 运行“团团：发一条测试提示”，灵动岛应当出现“VS Code：这是一条测试提示”。

## 设置

- `canaan.minCommandSeconds`：终端命令至少跑多少秒才提示，默认 10 秒。
- `canaan.reportTasks` / `reportDebug` / `reportTerminal`：分别开关三类提示。

终端命令的提示需要 VS Code 1.93 及以上，并开启终端集成（默认就是开的）。
