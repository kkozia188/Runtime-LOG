# 排查：12:01:06 控制周期卡顿 8.4 ms，7 个驱动故障（2026-10-19）

## 现象（黑匣子 `20261019-120106.549-cycle_overrun`）

- 12:01:04.518 会话 160ad178（192.168.100.40，运控同事）取得操作台控制权，之后没有任何操作台指令。
- 12:01:06.537 JTC `Accepted new action goal`：轨迹由同事的工具直接发到 `/left_arm_jtc/follow_joint_trajectory`。
- 下一个周期（seq 32193962）间隔 **8380.7 µs**；该周期里目标位置开始变化，说明 JTC 首次处理新轨迹的那次
  `update()` 多用了约 7.4 ms。整次运行（9 h，3200 万周期）中这是唯一一次超时。
- 随后 8 个周期间隔只有 17–35 µs：`ros2_control_node`（Humble）按 `next += period` 连续补跑落下的周期，
  帧发得过快，WKC 错误 1→9，过程数据年龄持续上升。
- 驱动 8 ms 没有正常同步数据：7 轴 0x1637 → 0x1208（故障），从站 AL 0x001A 同步错误、掉到 SAFEOP；
  使能管理器：已使能 → 紧急失能 → 失败（fault_detected），之后使能被拒绝，需要重启/断电。

## 根因

上游 Humble `joint_trajectory_controller` 在 **实时线程的 `update()`** 里处理新轨迹：

```cpp
// joint_trajectory_controller.cpp (Humble), update()
fill_partial_goal(*new_external_msg);        // 关节不全时遍历所有点补值
sort_to_local_joint_order(*new_external_msg); // 遍历所有点，每点为 pos/vel/acc/effort 各新建 std::vector
traj_external_point_ptr_->update(*new_external_msg);
```

耗时与轨迹点数成正比，且每点有堆分配。

## 复现（仿真栈，ROS 域 92，mock 硬件，轨迹点全部等于当前位置）

| 点数 | 开始周期 | 补跑周期 |
| --- | --- | --- |
| 10 / 100 / 1000 | 1003–1004 µs | 0 |
| 3000 | 1810 µs | 1 |
| 10000 | 6163 µs | 5 |
| 30000 | 18444 µs | 18 |

约 0.6 µs/点；1000 点以内被周期余量吸收，约 1500 点以上超出 1 ms。台架 8.4 ms 约对应 13000–14000 点；
同一运行的 goal-3（17 s 轨迹）开始周期 1225 µs，约 2000 点，没有引发故障。

## 建议与处理

1. 使用约束：发给 `left_arm_jtc` 的单条轨迹不超过 1000 点，按 10–50 ms 的间隔取点即可，JTC 会在点之间做样条插值。
   长轨迹分段发送，或者改用流式的 rolling_trajectory_controller。
2. **已修复（robot_driver #97，补丁 `patches/ros2_controllers/0004-limit-trajectory-points.patch`）**：
   - JTC 新增参数 `max_trajectory_points`，默认 2000，由用户指定。
   - 超过上限的轨迹由 `validate_trajectory_msg` 拒绝。拒绝发生在非实时的话题回调和 goal 回调里，控制器日志写明原因。
   - mock 实测：

     | 点数 | 结果 | 最长周期 |
     | --- | --- | --- |
     | 2001 / 3000 / 30000 | 拒绝 | ≤ 1005 µs |
     | 2000 | 接受 | 新轨迹到达的那个周期大约 1.17 ms（超出约 170 µs，没有补跑突发） |
     | 1000 | 接受 | 1004 µs |

   - 需要每个周期都不超时的运动方，仍应按第 1 条控制在 1000 点以内。
   - 没有把 `fill_partial_goal` 和 `sort_to_local_joint_order` 移出实时线程。原因如下：
     - 上游 humble、jazzy 和 master 分支都还在实时线程里做这两步。PR #68（2020）是有意把它们放进实时线程的，没有可以参照的新版实现。
     - 只移这两步，仍然无法覆盖首个周期的其他开销（释放旧轨迹、采样）。
3. 待关注：Humble 的 `ros2_control_node` 超时后会连续补跑落下的周期，在 EtherCAT 上产生连发。可以评估超时后重新对齐节拍。

## Runtime-LOG 侧的改进（本次）

- 驱动故障/离开使能/动力电断开/快停后 1 s 结束正在录的实验（JTC 不打印中止日志）。
- 实验指标只统计关节处于运行使能的周期；"在动"判据改为 2×10⁻⁵ rad（约 2 个编码器计数），
  不再把保持目标的编码器噪声当成运动。
- 实验与黑匣子窗口可下载 CSV。
