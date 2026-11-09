# officecli —— 第三方工具的技能封装（来源说明）

本目录是对第三方命令行工具 **OfficeCLI** 的 Agent Skill 封装，不是本课程作业的
原创内容；`SkillsCatalog` / `loadSkill` 的加载机制本身由本仓库实现。

- 工具：OfficeCLI v1.0.158，单文件 CLI，用于创建 / 读取 / 修改 / 校验
  `.docx` / `.xlsx` / `.pptx`，无需安装 Office。
- 来源与许可：上游发布页与许可证以官方仓库为准；本仓库**不附带** `officecli.exe`
  二进制（`office/` 目录已被 `.gitignore` 排除），使用前请自行下载并按
  `SKILL.md` 的说明放置。
- 本目录仅保留技能说明与参考文档（`references/`）以及包装脚本
  `scripts/officecli.cmd`；如不需要该技能，直接删除本目录即可，不影响 xAgent 启动。
