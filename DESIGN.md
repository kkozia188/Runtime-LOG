---
name: Runtime-LOG
description: 面向故障排查的浅色分析工作台，以清楚的字体和空间层级呈现证据。
colors:
  primary: "#3559d4"
  primary-soft: "#eef2ff"
  background: "#f5f5f6"
  surface: "#ffffff"
  subtle: "#f8f9fb"
  ink: "#20232a"
  muted: "#626873"
  border: "#e8e9ed"
  button-primary: "#252831"
  danger: "#b74442"
  danger-soft: "#fff0ed"
  warning: "#91601a"
  warning-soft: "#fff7e7"
  success: "#33785a"
typography:
  headline:
    fontFamily: '"PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif'
    fontSize: "32px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "-0.8px"
  title:
    fontSize: "20px"
    fontWeight: 600
    lineHeight: 1.4
  body:
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.65
  label:
    fontSize: "14px"
    fontWeight: 400
  measurement:
    fontFamily: 'ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace'
    fontSize: "15px"
rounded:
  badge: "6px"
  control: "8px"
  chart: "10px"
  panel: "12px"
  workspace: "16px"
spacing:
  tight: "8px"
  control: "12px"
  regular: "16px"
  panel: "24px"
  page: "38px"
components:
  button-primary:
    backgroundColor: "{colors.button-primary}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "7px 14px"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "7px 14px"
---

# Design System: Runtime-LOG

## Overview

**Creative North Star: "清晰的分析工作面"**

第二版按用户对第一版的否定重构。参考 Collect UI 的 Audit log、Document management 和 Orbit package tracking，采用固定侧栏与完整白色工作面。来源及借鉴范围见 `docs/collectui-design-reference.md`。

不再用一层层卡片或解释性副标题制造层级。优先通过字体、间距、对齐、少量语义颜色形成主次。功能与证据保留，重复说明删除或折叠。

## Colors

灰白作为应用外壳与工作面，黑灰色按钮承担主要操作，蓝色用于链接、选中态和导航反馈。异常区域有很浅的暖色背景，红黄保留为错误/警告语义色，不用于装饰。

曲线使用蓝色与棕橙色，选中事件用红色虚线，不改变数据定义。状态必须搭配文字，不能把“正常结束”解释为“性能通过”。

## Typography

中文优先采用苹方、微软雅黑、Noto Sans CJK SC；无外部字体请求。实际客户端没有前面的字体时使用后续回退。当前 Linux Chromium 经 CDP 确认使用 Noto Sans CJK SC。

- 正文、普通表格16px，关键标题32px（移动端28px），分区标题20px，事件标题16–17px。
- 标签、辅助信息、日志、图表刻度14px；测量数值15px。
- 界面主要采用400/500字重，600用于关键标题。避免普遍加粗造成视觉噪声。
- 等宽字体仅用于日志、时间和数值，不作为中文界面字体。
- 小字不重要时删除，不继续缩小。必要单位、时间基准、数据缺失和过期状态保留。
- 数值和单位不拆行。SVG 保留1220px最小绘图宽度并局部滚动，避免缩小字体。

## Layout

桌面使用216px浅灰侧栏，右侧白色工作面带16px外圆角、12px外间距，主体左右38px内边距。1350px以下侧栏190px、主体内边距24px。

800px以下侧栏转换为顶部导航，主体取消外壳圆角，页边距18px。不缩小正文。数据表使用局部滚动。

故障页左侧232px事件列表，右侧摘要、时间范围、波形和时段日志连续排列。1150px以下波形单列。无必要不对每一块区域加容器。

首页常用表格列默认显示；域、启动耗时、CPU、版本相关标识等详细列通过“详细字段”展开，数据仍完整存在并可搜索。

## Elevation & Depth

应用外壳为浅灰背景，完整白色工作面以细边框和极轻阴影区分。选中导航使用白底与轻微阴影；内容面以细分隔和留白组织，取消故障分析区的重复外框。

## Shapes

工作面16px圆角，主要容器12px，图表10px，按钮与输入8px，状态标签6px。标准控件至少40px高。侧栏图标采用统一1.6px线宽、24px视图的轮廓图标，不依赖远端图标服务。

## Components

### Navigation

主导航为异常分析、运行历史、实时状态。当前项用白色背景和蓝色图标表示；首页同页切换后更新选中态。页面辅助证据为文本式页内导航，避免一整排按钮。

### Controls

主要操作黑底白字，次操作白底细边框，保留明确悬停和焦点。150ms颜色反馈，减少动画偏好下关闭。日志搜索优先留在数据旁边。

### Progressive disclosure

“使用说明”集中通用解释；时间滑块与波形信号列表默认折叠。常驻仅展示诊断所需的时间、单位、采样模式和实际覆盖。加载失败、没有数据、连接中断和过期提示不能折叠。

### Charts

核心两图并排，浅冷灰底，14px刻度和一致图例。纵轴预留足够文字空间，浮点近零刻度简化为0，原始读数不变。缩放仍联动日志，数据缺口仍断线。

## Do's and Don'ts

### Do

- 保持16px正文与至少14px必要辅助文字。
- 精简重复说明，保留诊断事实。
- 让文字层级和间距承担分组，减少容器堆叠。
- 复用 `runtime_log/ui.py` 与 `runtime_log/ui.css`。

### Don't

- 不因参考图字号小就复制其字号。
- 不把业务说明、实现机制写在每块标题下面。
- 不添加虚假指标、装饰性图表或机器人控制入口。
- 不复制参考作品的素材、业务内容或背景装饰。
