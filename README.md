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

## 使用

```bash
python3 -m runtime_log serve                     # 记录 + 只读索引页 http://<host>:18200/
python3 -m runtime_log list                      # 列出运行
python3 -m runtime_log replay <运行ID前缀>        # 用当前 profile 重新分类并打印结论
python3 -m runtime_log import ~/.ros/log/<dir>   # 导入以前的 launch 目录（--scope/--domain/--hardware）
python3 -m runtime_log gc                        # 立即执行 7 天保留与磁盘保护
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
