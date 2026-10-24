"""CSV wheel-speed metrics, computed by the non-RT reader, never by a controller.

CSP axes also expose sent_velocity on Kinco: that field is not a steering-position target.
Only fresh, Operation Enabled, acknowledged CSV samples count as speed tracking evidence.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

FEEDBACK_LIMIT_MS = 20.0
ACTIVE_SPEED_RAD_S = 0.01


def velocity_joints(fields: list[str]) -> list[str]:
    names = set(fields)
    return sorted(name.rsplit('/', 1)[0] for name in fields if name.endswith('/sent_velocity')
                  and name.rsplit('/', 1)[0] + '/velocity' in names)


def sample_error(row: dict, joint: str, max_age_ms: float = FEEDBACK_LIMIT_MS) -> tuple[float | None, str]:
    def get(key):
        return row.get(f'{joint}/{key}')

    def finite(value):
        return isinstance(value, (int, float)) and math.isfinite(value)

    target, actual = get('sent_velocity'), get('velocity')
    if not finite(target) or not finite(actual):
        return None, 'nonfinite'
    mode = get('mode_of_operation_display')
    if not finite(mode) or mode != 9:
        return None, 'not_csv'
    word = get('status_word')
    if not finite(word) or int(word) & 0x6F != 0x27:
        return None, 'not_enabled'
    age = get('feedback_age_ms')
    if not finite(age) or age < 0 or age > max_age_ms:
        return None, 'feedback_stale'
    # If an adapter exports validity evidence, require it. Missing optional fields are not invented.
    for key, expected in (('command_fresh', 1), ('mode_request_ack', 1), ('mode_request_error', 0)):
        if f'{joint}/{key}' in row and get(key) != expected:
            return None, key
    error = target - actual
    if not math.isfinite(error):
        return None, 'nonfinite'
    return error, ''


@dataclass
class VelocityMetrics:
    joint: str
    total: int = 0
    csv_samples: int = 0
    valid: int = 0
    active: int = 0
    square_error: float = 0.0
    active_square_error: float = 0.0
    error_sum: float = 0.0
    max_error: float = 0.0
    max_error_at_s: float = 0.0
    peak_target: float = 0.0
    peak_actual: float = 0.0
    max_feedback_age_ms: float | None = None
    excluded: Counter = field(default_factory=Counter)

    def add(self, row: dict, elapsed_s: float) -> None:
        self.total += 1
        self.csv_samples += row.get(f'{self.joint}/mode_of_operation_display') == 9
        age = row.get(f'{self.joint}/feedback_age_ms')
        if isinstance(age, (int, float)) and math.isfinite(age) and age >= 0:
            self.max_feedback_age_ms = max(self.max_feedback_age_ms or 0, age)
        error, reason = sample_error(row, self.joint)
        if error is None:
            self.excluded[reason] += 1
            return
        target, actual = row[f'{self.joint}/sent_velocity'], row[f'{self.joint}/velocity']
        self.valid += 1
        self.square_error += error**2
        self.error_sum += error
        self.peak_target = max(self.peak_target, abs(target))
        self.peak_actual = max(self.peak_actual, abs(actual))
        if abs(error) > self.max_error:
            self.max_error, self.max_error_at_s = abs(error), elapsed_s
        # A constant nonzero velocity is motion, even when the target never changes.
        if max(abs(target), abs(actual)) > ACTIVE_SPEED_RAD_S:
            self.active += 1
            self.active_square_error += error**2

    def result(self) -> dict:
        return {
            'kind': 'velocity', 'unit': 'rad/s', 'samples': self.valid, 'total_samples': self.total,
            'csv_samples': self.csv_samples,
            'active_samples': self.active, 'moved': self.active > 0, 'excluded': dict(self.excluded),
            'valid_fraction': self.valid / self.total if self.total else 0,
            'max_error_rad_s': self.max_error if self.valid else None,
            'max_error_at_s': self.max_error_at_s if self.valid else None,
            'rms_error_rad_s': math.sqrt(self.square_error / self.valid) if self.valid else None,
            'rms_error_active_rad_s': math.sqrt(self.active_square_error / self.active) if self.active else None,
            'mean_error_rad_s': self.error_sum / self.valid if self.valid else None,
            'peak_target_rad_s': self.peak_target if self.valid else None,
            'peak_actual_rad_s': self.peak_actual if self.valid else None,
            'max_feedback_age_ms': self.max_feedback_age_ms,
        }


def velocity_summary(metrics: dict) -> dict:
    valid = {j: m for j, m in metrics.items() if m['samples']}
    worst = max(valid, key=lambda j: valid[j]['max_error_rad_s']) if valid else None
    return {'tracking_kind': 'velocity', 'moving_joints': sum(m['moved'] for m in valid.values()),
            'valid_velocity_joints': len(valid), 'max_error_joint': worst,
            'max_error_rad_s': valid[worst]['max_error_rad_s'] if worst else None,
            'no_valid_tracking_data': not valid}
