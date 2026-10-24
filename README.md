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

## 在网页或 PlotJuggler 中查看波形

运行报告 → 性能实验 → 点击实验名称 → **查看波形**。新生成的运行报告也在“数据”列提供直达入口。

- 选择关节，查看目标/实际位置、跟随误差、实际速度、力矩、控制周期和驱动状态字。
- 拖拽时间区间放大，或输入起止秒数；各图共用时间范围。点击“全程”恢复。
- 悬停或点击读数。全程图保留时间区间的首尾和峰谷，放大后重新读取原始 MCAP；页面明确标注摘要或原始采样。
- 时间零点为归档中的第一条采样。位置、误差使用 °，速度使用 °/s，力矩使用额定力矩的 ‰，周期使用 µs。
- 缺失周期和非有限值显示为断线。波形包含故障后的归档数据，需结合状态字解释，不能把它们当成有效运动指标。
- 兼容已有实验，无需重新采集。查询只读取归档文件，在非隔离 CPU 上执行；同时只处理一个波形请求。

使用 PlotJuggler：下载实验的 **CSV**，从 Data Loader 打开，分隔符选择逗号，时间列选择 **time_s**（秒）。将信号拖到图中，例如：

| CSV 信号 | 含义 / 单位 |
| --- | --- |
| `left_joint1/target_position` | 实际发给驱动的目标位置，rad |
| `left_joint1/position` | 实测位置，rad |
| `left_joint1/velocity` | 实际速度，rad/s |
| `left_joint1/torque_permille` | 力矩，额定值的 ‰ |
| `period_us` | 控制周期，µs |

目标与实测位置可拖入同一图比较；不同单位的信号放在不同图。CSV 保留原始 1 kHz 采样（已有丢帧不会补齐）。
MCAP 使用 JSON 消息编码，能否直接打开取决于 PlotJuggler 的插件与版本；CSV 不依赖 MCAP 插件。

## 底盘性能实验与黑匣子波形

底盘段包含 `sent_velocity` 与 `velocity` 时，可使用现有手动实验入口录制全部状态接口：

```bash
cd ~/Runtime-LOG
python3 -m runtime_log experiment start wheel-speed-check
# 由操作人员通过原操作台发运动指令，日志站不会使能或发送命令。
python3 -m runtime_log experiment stop
```

- 行走轮指标：最大/RMS/平均速度误差、运动中 RMS、峰值目标/实际速度、有效采样占比和排除原因。
- 速度误差为 `sent_velocity - velocity`（rad/s）。仅统计模式9、Operation Enabled、反馈年龄在0–20 ms、以及可用的适配器有效性标志通过的采样。
- 没有有效采样显示“—”，未使能的静止记录不会冒充跟随性能通过；恒定非零速度同样算运动。
- CSP转向轴和外置编码器可查看位置、模式、反馈年龄与ready波形。缺少已发转向目标时不计算转向跟随误差。
- 实验指标不声称地面速度、打滑率、响应延迟或超调；这些还需要控制器参考量或独立真值。
- 实验详情可选轮轴/转向/编码器，按图中单位读数，支持放大与CSV导出。
- 黑匣子窗口也提供“查看波形”：`/<运行>/blackbox/<窗口>/`，可查看触发原因、相对时间、波形和内核日志。
- 当前底盘实验使用手动起止；JTC自动实验仍只针对机械臂。单段最长600 s，故障或进程退出会收尾。

采集插件是否在某台主机上默认开启，取决于该机的微基准与实机A/B验收；只有网页和指标代码部署成功不等于实时采集已经验收。

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

### 每台工控机一个站

同一套引擎和项目配置，部署到每台控制工控机，各自归档、各自在 `:18200` 提供页面。机械臂和底盘以后由同一台工控机控制时，
那台的站会同时记录两者，不需要改引擎。

只有主机相关的事实写进 `~/.config/runtime-log/host.yaml`（示例 `deploy/host.example.yaml`）。启动时它会合并到
`profiles/rt_control.yaml` 上：映射逐层合并，列表和数值直接替换。可以写的内容：

- 站点名和其他站点的链接（`station`），显示在首页；
- 代码仓库路径（`git_repos`）；
- 操作台审计日志路径；
- 停机后读错误码的驱动名匹配和预期错误码。

阶段规则、触发器、枚举仍只写在项目配置里。

控制栈和记录服务都以普通用户运行。主站设备按 udev 规则 `KERNEL=="EtherCAT[0-9]*", MODE="0660", GROUP="realtime"`
授权给 `realtime` 组。用户刚加入该组、用户级 systemd 还没重新登录时，可用 drop-in 让服务带上该组启动：

```ini
# ~/.config/systemd/user/runtime-log.service.d/realtime.conf
[Service]
ExecStart=
ExecStart=/usr/bin/sg realtime -c "exec /usr/bin/python3 -m runtime_log --archive %h/runtime-log/runs serve --host 0.0.0.0 --port 18200 ${RUNTIME_LOG_SERVE_ARGS}"
```

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
