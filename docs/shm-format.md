# 共享内存传输环格式（v1）

写入者：`ros/runtime_log_recorder/include/runtime_log_recorder/shm_ring.hpp`（C++17，头文件）。
读者：`runtime_log/shm.py`（Python）。只支持 x86-64，小端。

## 段

- 名称：`/dev/shm/runtime-log.<写入者 PID>.<写入者名>`。写入者退出后段仍保留，供事后读取；由读者归档后清理。
- 创建在非实时路径（`on_configure`）：`shm_open` → `ftruncate` → `mmap` → 全段清零（预先触页）→ `mlock`。

| 偏移 | 内容 |
| --- | --- |
| 0–55 | `magic "RTLOGSHM"`、`version u32`、`header_size u32`、`slot_count u32`、`slot_size u32`、`field_count u32`、`writer_state u32`（0 已创建 / 1 激活 / 2 停用）、`writer_pid i64`、`created_realtime_ns i64`、`created_monotonic_ns i64` |
| 64–71 | `head u64`：最后提交的周期序号（从 1 开始），独占一条缓存行，只有写入者写 |
| 128–191 | 写入者名 |
| 192–319 | 附加信息 |
| 320– | 字段表，每项 64 B：`name[56]`、`offset u32`、`type u8`（1 = f64）、填充 |
| `header_size`– | 槽，`slot_count` 个，每个 `slot_size` 字节（64 B 对齐） |

槽：`seq u64`、`time_ns i64`、`period_ns i64`、`values f64[field_count]`。

## 协议（单写单读，写入者不等待）

写入者每周期（无分配、锁、系统调用）：

1. 槽 = `n mod slot_count`；`seq = BUSY`（全 1）；释放屏障；
2. 写 `time_ns`、`period_ns`、各字段值；
3. `seq = n`（release）；`head = n`（release）。

读者每批：

1. 读 `head`；本批范围 = 上次位置 … `head`，最早不早于 `head - slot_count + 2`（`head + 1` 的槽可能正在写）；
2. 对每个周期：读 `seq`（s1）→ 拷贝整槽 → 再读 `seq`（s2）；`s1 == s2 == n` 才有效，否则计一个缺口，不重试；
3. 读者进度只保存在读者内存里，不写回共享内存。

写入者存活：`writer_state`、`writer_pid` 是否存在、`head` 是否前进。
