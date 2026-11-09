# tianjicesuan-1.0.0 —— 第三方技能包（来源说明）

本目录**不是**本课程作业的原创内容，保留在此仅用于演示 `loadSkill` 的
「目录 + SKILL.md + references / scripts」加载形态。

- 来源：第三方 Agent Skill 包，原作者与原始地址未随包提供（`SKILL.md` 的
  frontmatter 中 `metadata.author` 为 `computer`，版本 `4.0`）。
- 许可：未声明。**请勿**在未经权利人许可的情况下二次分发。
- 本仓库对其**未做任何修改**，也不将其内容计入课程作业成果；
  `createSkill` / `SkillsCatalog` / `loadSkill` 的机制本身是本仓库自行实现的。
- 如需移除，直接删除本目录即可，不影响 xAgent 启动（`SkillsCatalog` 会把
  缺失或不合法的技能记为 `issues`，不会报错）。
