"""Bounded waveform queries over archived MCAP. Never reads the live RT ring."""
from __future__ import annotations

import json
import math
from pathlib import Path

from mcap.reader import make_reader

from .velocity_experiment import sample_error

RAD = 180.0 / math.pi


def waveform(path: Path, joint: str, start: float = 0, end: float | None = None,
             width: int = 1000) -> dict:
    if not math.isfinite(start) or start < 0 or (end is not None and (not math.isfinite(end) or end <= start)):
        raise ValueError("时间范围无效")
    width = max(100, min(2000, width))
    with path.open("rb") as stream:
        reader = make_reader(stream)
        first = next(reader.iter_messages(), None)
        if first is None:
            return {"series": [], "samples": 0, "duration": 0, "start": 0, "end": 0}
        origin = first[2].log_time
        row = json.loads(first[2].data)
        if f"{joint}/position" not in row:
            raise ValueError("归档中没有这个关节")
        summary = reader.get_summary()
        if not summary or not summary.statistics:
            raise ValueError("归档缺少索引，请下载 CSV 查看")
        duration = max(0, (summary.statistics.message_end_time - origin) / 1e9)
        end = min(duration, end) if end is not None else duration
        if start > duration:
            raise ValueError("时间范围超出实验长度")
        span = max(end - start, 1e-9)
        csv_tracking = f'{joint}/sent_velocity' in row
        speed_scale = 1 if csv_tracking else RAD
        speed_unit = 'rad/s' if csv_tracking else '°/s'
        specs = [("target_position", "目标位置", "°", RAD), ("position", "实际位置", "°", RAD),
                 ("error", "位置跟随误差", "°", RAD), ("velocity", "实际速度", speed_unit, speed_scale),
                 ("sent_velocity", "已发速度（仅 CSV 有效）", "rad/s", 1),
                 ("velocity_error", "CSV 速度跟随误差", "rad/s", 1),
                 ("torque_permille", "力矩", "‰", 1), ("period_us", "控制周期", "µs", 1),
                 ("status_word", "驱动状态字", "原始值", 1),
                 ("feedback_age_ms", "反馈年龄", "ms", 1), ("ready", "传感器就绪", "0/1", 1),
                 ("mode_of_operation_display", "实际模式（8=CSP，9=CSV）", "模式", 1),
                 ("mode_request_error", "适配器故障锁存", "0/1", 1),
                 ("command_fresh", "指令有效", "0/1", 1)]
        specs = [s for s in specs if s[0] == "period_us" or f"{joint}/{s[0]}" in row or
                 (s[0] == "error" and f"{joint}/target_position" in row) or
                 (s[0] == "velocity_error" and csv_tracking)]
        series = [{"key": key, "label": label, "unit": unit, "points": []} for key, label, unit, _ in specs]
        # Per bin keep first, last, minimum, maximum in chronological order. O(width) memory.
        buckets = [{} for _ in specs]
        counts = 0
        missing = 0
        last_seq = None
        last_time = None

        def check_budget():
            if any(len(output["points"]) > 20000 for output in series):
                raise ValueError("缺失片段过多，请缩小时间范围或下载 CSV")

        def flush():
            check_budget()
            for output, bucket in zip(series, buckets):
                if bucket:
                    unique = {p[0]: p for p in bucket.values()}
                    output["points"].extend(unique[t] for t in sorted(unique))
                    bucket.clear()

        current_bin = -1
        for _, _, message in reader.iter_messages(start_time=origin + round(start * 1e9),
                                                  end_time=origin + round(end * 1e9) + 1):
            values = json.loads(message.data)
            t = (message.log_time - origin) / 1e9
            index = min(width - 1, int((t - start) / span * width))
            seq = values.get("seq")
            gap = last_seq is not None and seq is not None and seq > last_seq + 1
            if index != current_bin or gap:
                flush()
                current_bin = index
            if gap:
                missing += seq - last_seq - 1
                for output in series:
                    output["points"].append([(last_time + t) / 2, None])
            last_seq, last_time = seq, t
            counts += 1
            if counts % 1000 == 0:
                check_budget()
            for spec, bucket, output in zip(specs, buckets, series):
                key, _, _, scale = spec
                if key == "error":
                    target = values.get(f"{joint}/target_position")
                    position = values.get(f"{joint}/position")
                    value = target - position if isinstance(target, (int, float)) and isinstance(position, (int, float)) else math.nan
                elif key == 'velocity_error':
                    value, _ = sample_error(values, joint)
                elif key == 'sent_velocity':
                    value = values.get(f'{joint}/{key}') if values.get(f'{joint}/mode_of_operation_display') == 9 else None
                else:
                    value = values.get(key if key == "period_us" else f"{joint}/{key}", math.nan)
                value = value * scale if isinstance(value, (float, int)) else math.nan
                if not math.isfinite(value):
                    # Preserve gaps; do not connect valid samples through unknown values.
                    if bucket:
                        unique = {p[0]: p for p in bucket.values()}
                        output["points"].extend(unique[x] for x in sorted(unique))
                        bucket.clear()
                    if not output["points"] or output["points"][-1][1] is not None:
                        output["points"].append([t, None])
                    continue
                point = [t, value]
                if not bucket:
                    bucket.update(first=point, last=point, low=point, high=point)
                else:
                    bucket["last"] = point
                    if value < bucket["low"][1]:
                        bucket["low"] = point
                    if value > bucket["high"][1]:
                        bucket["high"] = point
        flush()
        return {"joint": joint, "duration": duration, "start": start, "end": end,
                "samples": counts, "missing": missing, "series": series,
                "aggregated": any(len(s["points"]) < counts for s in series)}
