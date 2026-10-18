# Runtime-LOG

机器人实时控制栈的运行记录站（P0：M3 启动时间线与资源）。每次 `ros2 launch` 运行自动生成一份记录：

- 结果：正常结束 / 失败 / 未正常结束 / 记录中断。失败时给出失败步骤、原文和最后成功的启动步骤。
- 启动时间线：进程、启动步骤、停止、控制事件（使能/失能/轨迹）、EtherCAT 内核消息、各从站 AL 状态。
- 资源：各进程 CPU% 与 RSS，`ros2_control_node` 各线程的调度策略、RT 优先级、允许 CPU、实际运行 CPU、CPU%。
- EtherCAT 主站计数（帧、丢帧、发送错误）与本次运行增量。
- 异常计数：重扫、应答从站数变化、报文超时/不匹配、DC 未同步、AL 状态超时、ERROR 行、启动中进程退出等。
- 部署与版本：产物文件哈希、控制器、硬件插件、`-p` 参数覆盖、仓库 HEAD/未提交文件/diff 哈希、主机信息。

## 第一原则：不影响 1 kHz 控制周期

- 只读：跟随 `launch.log`、读 `/proc`、`journalctl -k`，`ethercat` 只执行 `master` 与 `slaves`（读主站缓存，不发总线报文；其余子命令在代码里拒绝）。
- 不向控制栈发送任何 ROS 消息、服务或参数，不改任何进程的调度和亲和性。
- 服务本身 `Nice=10`、IO idle，启动时把隔离核（`/sys/devices/system/cpu/isolated`）从自己的亲和性中去掉。
- 只在运行期间记录；没有运行时只保留索引页。

## P1：1 kHz 黑匣子

- 记录控制器 `ros/runtime_log_recorder`（ros2_control 插件 `runtime_log_recorder/RecorderController`）：
  每周期把配置的状态接口定长拷贝进共享内存环（`/dev/shm/runtime-log.<pid>.<名字>`，格式见 `docs/shm-format.md`）。
  update 内无分配、锁、系统调用；共享内存不可用时只停用自己。微基准：25 轴档每周期写入最坏 4.7 µs。
- robot_driver 通过单元部署配置 `runtime_log_recorder: {enabled: true}` 加载它（组合器自动列出全部状态接口，
  独立且非致命的 spawner）。插件在工控机上单独编译：`deploy/build_recorder.sh ~/rt-control-electri127`。
- 服务发现本次运行控制进程写的段，读者线程每 50 ms 取数、保留 30 s；触发后保存前 10 s、后 2 s 的 MCAP
  （JSON + zstd，PlotJuggler/Foxglove 可打开）、触发原因与内核日志，在报告页下载。
- 触发器：驱动 Fault、离开 Operation Enabled、WKC 错误计数增加、过程数据年龄 > 3 ms、从站离开 OP、
  应答从站数下降、链路断开、周期 > 3 倍名义值、写入者停止或消失、内核 EtherCAT 错误、手动（`runtime-log trigger`）。
  `/rt/disable` 或停机前后 2 s 内的状态切换算预期，只记事件不存窗口。
- 每次运行都有周期统计（名义/平均/最小/最大周期、> 1.5 倍次数），来自记录控制器收到的实际周期。
- 停机后：主站不在 Operation 时，SDO 读一次各驱动 0x603F/0x6041（只放行这两个对象）。
- 依赖：`pip3 install --user -r requirements.txt`（mcap、zstandard）。
- 使能管理器部署开启时把状态机（22 个字段）写入自己的段：阶段变化进时间线；紧急快停、紧急失能、失败和失败阶段
  触发黑匣子（枚举名在 `profiles/rt_control.yaml` 的 `blackbox.enums`）。两份写入头文件（本仓库与 robot_driver
  `rt_control_semantic_components`）用同一个黄金样本 `tests/fixtures/shm_format_v1.bin` 保证一致。
- 黑匣子窗口目录：`window.mcap`、`trigger.json`、`kernel.log`、`timeline.json`（前 10 s / 后 3 s 的时间线片段与进程快照）。
- 台架零差配置 `zeroerr_cia402_csp_diag` 额外记录速度、力矩（‰）、错误码，以及实际发出的目标位置与控制字
  （ICube 补丁 0014 的 RxPDO 镜像）。

## P2：1 kHz 性能实验（M2）

- 自动：每个 JTC 轨迹目标（日志 `Accepted new action goal` → `Goal reached`/中止）记录一次实验，范围为目标前
  0.2 s 到结束后 1 s；名字里带上 5 s 内的操作台操作（如“去位姿 home；档位 low”）。
- 手动：`runtime-log experiment start 名字` / `runtime-log experiment stop`（最长 10 分钟）。
- 数据：`experiments/<时刻>-<名字>/data.mcap`（全部记录字段，1 kHz）、`meta.json`（指标）、`plot.json`。
- 指标（每个关节）：行程、运动时长、最大/运动中 RMS 跟随误差（实际发出的目标 − 实际位置）、超调、
  到位时间（误差 < 0.05° 保持 0.1 s）、终点误差、静止抖动、峰值速度与力矩。
- 页面：运行报告的“1 kHz 性能实验”表、实验详情（指标 + 曲线）、勾选两次实验对比（可跨运行）。

## 页面

- `/`：运行列表（筛选、搜索、黑匣子与实验数、勾选两次运行对比）。
- `/<运行>/`：单次运行报告。`/compare?a=&b=`：两次运行对比。
- `/live`：实时状态（运行中记录段的最新周期，0.5 s 刷新）。
- `/<运行>/experiments/<实验>/`、`/compare_experiments?a=<运行>/<实验>&b=…`。

## 使用

```bash
python3 -m runtime_log serve                     # 记录 + 只读索引页 http://<host>:18200/
python3 -m runtime_log list                      # 列出运行
python3 -m runtime_log replay <运行ID前缀>        # 用当前 profile 重新分类并打印结论
python3 -m runtime_log import ~/.ros/log/<dir>   # 导入以前的 launch 目录（--scope/--domain/--hardware）
python3 -m runtime_log gc                        # 立即执行 7 天保留与磁盘保护
python3 -m runtime_log trigger "原因"            # 手动保存黑匣子窗口
```

主站轮询频率：启动期 5 Hz，就绪 5 s 后 1 Hz（`profiles/rt_control.yaml`）。
`serve --ethercat-hz 0` 关闭主站轮询，`--ethercat-hz 1,1` 固定 1 Hz（A/B 验证用）。

## 部署（工控机，systemd 用户服务）

```bash
git clone <repo> ~/Runtime-LOG    # 或从开发机 git push
~/Runtime-LOG/deploy/install.sh
sudo loginctl enable-linger $USER  # 一次性：注销后服务继续运行、开机自启
systemctl --user enable --now runtime-log
journalctl --user -u runtime-log -f
```

额外参数写在 `~/.config/runtime-log.env`：`RUNTIME_LOG_SERVE_ARGS=--ethercat-hz 0`。

## 存档

`~/runtime-log/runs/<YYYYmmdd-HHMMSS>-d<域>-<scope>/`：

| 文件 | 内容 |
| --- | --- |
| `run.json` | 元数据与结论（索引页数据源） |
| `events.jsonl` | 全部事件（启动步骤、失败、控制、内核、轮询） |
| `resources.jsonl` | 1 Hz 进程与线程采样 |
| `ethercat.jsonl` | 主站状态、计数与增量、从站状态 |
| `launch.log.gz` | 原始 launch.log |
| `report.html` | 单次运行报告（自包含） |

保留 7 天；磁盘剩余低于 10 GB 时从最旧的已结束运行开始删除。服务重启时，正在进行的运行沿用原目录并重建事件。

## 开发

```bash
uv run --python 3.10 --with pytest --with pyyaml python -m pytest -q tests
```

只依赖 Python 3.10 标准库与 PyYAML（工控机系统自带）。项目规则在 `profiles/rt_control.yaml`，全部来自真实 launch.log。
