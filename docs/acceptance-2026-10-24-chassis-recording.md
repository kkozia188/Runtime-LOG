# 底盘性能实验与黑匣子部署检查（2026-10-24）

结论：非实时侧轮速实验、指标和网页已部署。底盘记录器尚未默认开启：96字段微基准仍有超过5µs的写入，性能前置门禁未通过，未进入记录器开启组B。

## 已完成的软件

- CSV轮速误差 = sent_velocity - velocity，单位rad/s。
- 只有CSV模式、Operation Enabled、反馈新鲜及可用的适配器有效性标志通过时计入指标。
- 失能、故障、过期、非CSV及非有限值分别计入排除原因；无有效样本不冒充零误差。
- 可手动记录全部所选字段；转向及编码器只显示已有位置/反馈年龄/ready，不伪造转向目标。
- 性能实验和黑匣子窗口均支持网页选轴、缩放、光标和CSV下载；支持跨实验比较速度指标。
- 97项测试通过；浏览器用明确标记的合成数据检查轮速/编码器选择、rad/s单位、黑匣子路由和移动端，无JS异常。
- 尚未进行底盘真实运动实验，未使能或发送运动指令。

## 基线A：记录插件未加载

CPU6、FIFO80，EtherCAT-OP FIFO79，调频powersave/EPP performance，驱动OP且IDLE；日志站运行。
600秒sched_switch采样：

| 指标 | A |
| --- | --- |
| 周期数 | 600006 |
| 标准差 | 1.796µs |
| 周期范围 | 981–1020µs |
| 偏差p99 / p99.9 / 最大 | 5 / 8 / 20µs |
| 偏差>50µs | 0 |
| Tx errors增量 | 0 |

rtcan-master按现有底盘架构同绑CPU6（普通调度）；它被旧分析工具归入foreign，但不是未经配置的外来线程。
IgH Lost frames快照有无符号下溢（Rx短暂大于Tx）；不能直接把该字段差值当作真实丢帧。

## 96字段候选与微基准

为减少周期写入，候选去掉模式切换辅助字段 feedback_sequence / sent_sequence / feedback_sequence_at_send / mode_switch_ready。
保留全部八轴位置、速度、已发速度、实际模式、状态字、反馈年龄、command_fresh、mode_request_ack、mode_request_error，及4编码器状态和EtherCAT总线状态，共96字段。

robot_driver现场加入可选recording接口列表能力，默认仍记录全部状态；显式列表必须非空、唯一且全部可导出。8项组合器测试通过。候选配置仅保存，尚未启用。

每组120000周期、2000槽、CPU6/FIFO80、1kHz，读者CPU0/2/4每50ms读一次：

| CPU6策略 | 读者 | 写入p99.9 | 写入最大 | >5µs |
| --- | --- | --- | --- | --- |
| powersave / EPP performance | 无 | 0.395µs | 8.281µs | 2 |
| powersave / EPP performance | 有 | 0.411µs | 8.266µs | 2 |
| 临时performance | 无 | 0.417µs | 8.360µs | 3 |
| 临时performance | 有 | 0.400µs | 8.395µs | 3 |

计时为写槽+提交两次CLOCK_MONOTONIC读数间的墙钟耗时，含区间内系统打断/时钟读取开销。仅凭无读者组也出现长尾不能证明影响为零，更不能将其从最坏值预算排除。
CPU6临时performance未改善，测试后恢复powersave，未持久化频率变更。

## 当前交付边界

- 日志站非实时功能已更新；记录控制器包已安装但部署开关仍关闭。
- 当前没有完成B组及黑匣子/实验真实归档链路验收，因此不能宣称底盘1kHz记录已上线。
- 没有把这次失败改写成PASS或降低预算。
- 后续需定位写入区间长尾（更细中断/计时证据），或取得明确的预算调整决定；之后再继续B组600秒及真实未使能窗口保存验证。

## 收尾时的硬件状态

测试后恢复原CPU6 powersave策略，并尝试恢复未加载记录器的控制栈。启动被can0不存在阻断。
内核日志确认2026-10-24 14:56:26发生 `usb 1-8: USB disconnect, device number 5`；该USB路径原为gs_usb适配器，当前没有CAN网络接口，也不是改名为can1。
当前控制栈未运行，EtherCAT八轴PREOP，日志服务正常。未更改CAN绑定到其他设备，需现场恢复USB-CAN连接后再继续。

原始证据在底盘主机 `~/runtime-log/acceptance/2026-10-24-chassis-recorder/`。
主机配置备份及96字段候选在该目录的config-backup-path.txt指定位置。
