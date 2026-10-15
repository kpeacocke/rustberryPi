"""Bounded, workload-matched analysis. No server control or third-party runtime."""
import json
import logging
import math
from pathlib import Path
from statistics import median

MIN_SAMPLES = 120
METRICS = {
    'fps': ('Server FPS', 'lower', 5.0),
    'cpu_mean': ('CPU usage', 'higher', 10.0),
    'memory_percent': ('Memory usage', 'higher', 5.0),
    'swap_used_mib': ('Swap usage', 'higher', 64.0),
    'temperature': ('Temperature', 'higher', 3.0),
    'read_kib_s': ('Disk reads', 'higher', 128.0),
    'write_kib_s': ('Disk writes', 'higher', 128.0),
    'a2s_latency_ms': ('Game-query latency', 'higher', 5.0),
    'rcon_latency_ms': ('RCON latency', 'higher', 20.0),
}
MAX_CONTEXTS = 24


def finite(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def eligible(row):
    return (row.get('schema') == 1 and finite(row.get('at')) and row['at'] > 0
            and row.get('active_state') == 'active' and row.get('rcon_ok') is True
            and row.get('a2s_ok') is True and finite(row.get('game_uptime_seconds'))
            and row['game_uptime_seconds'] >= 300
            and all(finite(row.get(key)) and row[key] >= 0
                    for key in ('fps', 'player_count', 'entities')))


def context(row):
    return f"{int(row['player_count'])}:{int(row['entities'] // 5000)}"


def clean(row):
    return {key: float(row[key]) for key in METRICS if finite(row.get(key)) and row[key] >= 0}


def summaries(rows):
    result = {}
    if len(rows) < MIN_SAMPLES:
        return result
    for key, (_, _, floor) in METRICS.items():
        values = [row[key] for row in rows if key in row]
        if len(values) >= MIN_SAMPLES * 0.8:
            center = median(values)
            result[key] = {'median': center,
                           'scale': max(floor, 1.4826 * median(abs(value - center) for value in values))}
    return result


def evidence(values, baseline):
    result = []
    for key, reference in baseline.items():
        if key not in values:
            continue
        label, direction, _ = METRICS[key]
        change = values[key] - reference['median']
        score = change / reference['scale'] * (-1 if direction == 'lower' else 1)
        if score >= 3:
            result.append({'metric': key, 'title': label + ' outside learned range',
                           'observed': round(values[key], 2), 'baseline': round(reference['median'], 2),
                           'score': round(score, 2), 'severity': 'review',
                           'detail': f"Observed {values[key]:.1f}; comparable baseline {reference['median']:.1f}."
                                     ' This is a deviation, not a diagnosed cause.'})
    return result


class HealthMonitor:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.contexts = {}
        self.recent = {}
        self.event = None
        self.before = {}
        self.after = {}
        self.last_at = 0
        self.last_write = 0
        self.streaks = {}
        self.last_context = None
        self.storage_error = None
        self.load_failed = False
        if self.path and self.path.exists():
            try:
                if self.path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError('Health state exceeds size limit')
                state = json.loads(self.path.read_text())
                self._validate(state)
                self.contexts = state['contexts']
                self.recent = state.get('recent', {})
                self.event = state.get('event')
                self.before = state.get('before', {})
                self.after = state.get('after', {})
            except (OSError, ValueError, TypeError, KeyError) as error:
                self.storage_error = type(error).__name__
                self.load_failed = True
                logging.warning('Server health state unavailable (%s); original preserved', self.storage_error)

    @staticmethod
    def _validate(state):
        if not isinstance(state, dict) or state.get('version') != 1:
            raise ValueError('Unsupported health state')
        for name in ('contexts', 'recent', 'before', 'after'):
            groups = state.get(name, {})
            if not isinstance(groups, dict) or len(groups) > MAX_CONTEXTS:
                raise ValueError('Invalid health contexts')
            for key, rows in groups.items():
                if not isinstance(key, str) or len(key) > 32 or not isinstance(rows, list) or len(rows) > MIN_SAMPLES:
                    raise ValueError('Invalid health samples')
                for row in rows:
                    if not isinstance(row, dict) or 'fps' not in row or any(
                            metric not in METRICS or not finite(value) or value < 0 for metric, value in row.items()):
                        raise ValueError('Invalid health metric')
        if state.get('event') is not None:
            validate_event(state['event'])

    def _save(self, at, force=False):
        if not self.path or self.load_failed or (not force and at - self.last_write < 60):
            return
        temporary = self.path.with_suffix('.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open('w', encoding='utf-8') as handle:
                temporary.chmod(0o600)
                json.dump({'version': 1, 'contexts': self.contexts, 'event': self.event,
                           'recent': self.recent, 'before': self.before, 'after': self.after},
                          handle, allow_nan=False)
            temporary.replace(self.path)
            self.last_write = at
            self.storage_error = None
        except (OSError, ValueError) as error:
            category = type(error).__name__
            if category != self.storage_error:
                logging.warning('Server health persistence unavailable (%s)', category)
            self.storage_error = category

    def _comparison(self, key):
        if not self.event:
            return {'status': 'not-recorded', 'metrics': []}
        result = {**self.event, 'metrics': [], 'samples': len(self.after.get(key, []))}
        baseline = summaries(self.before.get(key, []))
        after = summaries(self.after.get(key, []))
        if not baseline:
            return {**result, 'status': 'unmatched'}
        if not after:
            return {**result, 'status': 'collecting'}
        for metric, old in baseline.items():
            if metric in after:
                new = after[metric]['median']
                result['metrics'].append({'metric': metric, 'title': METRICS[metric][0],
                                          'before': round(old['median'], 2), 'after': round(new, 2),
                                          'change': round(new - old['median'], 2)})
        result['status'] = 'review' if evidence(
            {metric: value['median'] for metric, value in after.items()}, baseline) else 'comparable'
        return result

    def update(self, row, maintenance=False, event=None):
        result = {'backend': 'cpu-statistical', 'advisory_only': True, 'samples': 0,
                  'required_samples': MIN_SAMPLES, 'evidence': [], 'storage_error': self.storage_error,
                  'maintenance_error': None,
                  'comparison': {'status': 'not-recorded', 'metrics': []}}
        if event is not None:
            try:
                validate_event(event)
            except (TypeError, ValueError) as error:
                result['maintenance_error'] = type(error).__name__
                event = None
        at = row.get('at')
        if maintenance or not eligible(row) or at <= self.last_at:
            self.streaks.clear()
            return {**result, 'status': 'maintenance' if maintenance else 'unavailable'}
        if at - self.last_at > 20:
            self.streaks.clear()
        self.last_at = at
        key = context(row)
        if key != self.last_context:
            self.streaks.clear()
        self.last_context = key
        values = clean(row)
        event_changed = event is not None and event != self.event
        if event_changed:
            self._validate({'version': 1, 'contexts': {}, 'event': event})
            self.event = event
            self.before = {key: list(rows) for key, rows in self.recent.items()}
            self.after = {}
        rows = self.contexts.setdefault(key, [])
        if len(self.contexts) > MAX_CONTEXTS:
            self.contexts.pop(next(iter(self.contexts)))
        baseline = summaries(rows)
        deviations = evidence(values, baseline)
        current = {item['metric'] for item in deviations}
        self.streaks = {metric: self.streaks.get(metric, 0) + 1 for metric in current}
        stable = not any(row.get(flag) is True for flag in
                         ('throttled_now', 'throttle_cpu_now', 'watchdog_blocked'))
        if not baseline and stable and not deviations and len(rows) < MIN_SAMPLES:
            rows.append(values)
        if self.event and at > self.event['completed_at']:
            post = self.after.setdefault(key, [])
            if len(self.after) > MAX_CONTEXTS:
                self.after.pop(next(iter(self.after)))
            if len(post) < MIN_SAMPLES:
                post.append(values)
        recent = self.recent.setdefault(key, [])
        recent.append(values)
        del recent[:-MIN_SAMPLES]
        if len(self.recent) > MAX_CONTEXTS:
            self.recent.pop(next(iter(self.recent)))
        status = ('review' if any(count >= 6 for count in self.streaks.values()) else
                  'watching' if deviations else 'normal' if baseline else 'learning')
        self._save(at, force=event_changed or (len(rows) == MIN_SAMPLES and not baseline))
        return {**result, 'status': status, 'context': key, 'samples': len(rows),
                'evidence': deviations, 'covered_metrics': list(baseline),
                'missing_metrics': [key for key in METRICS if key not in values or key not in baseline],
                'storage_error': self.storage_error,
                'comparison': self._comparison(key)}


def read_document(path, limit=65536):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError('Health document exceeds size limit')
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise ValueError('Health document must be an object')
    return result


def validate_event(event):
    if (not isinstance(event, dict) or event.get('version') != 1
            or set(event) != {'version', 'completed_at', 'target'}
            or not finite(event.get('completed_at')) or event['completed_at'] <= 0
            or event.get('target') not in ('os', 'fex', 'rust', 'os-rust', 'all')):
        raise ValueError('Invalid maintenance event')


def hat_status(path, now):
    if not path:
        return {'status': 'disabled', 'backend': 'hailo', 'advisory_only': True}
    try:
        result = read_document(path)
        if (result.get('version') != 1 or result.get('backend') != 'hailo'
                or result.get('status') not in ('ready', 'review', 'error', 'unavailable')
                or not finite(result.get('at')) or not 0 <= now - result['at'] <= 20):
            raise ValueError('Stale or invalid HAT result')
        if result['status'] in ('ready', 'review'):
            if not finite(result.get('source_at')) or not 0 <= now - result['source_at'] <= 20:
                raise ValueError('Stale HAT source')
            if (not isinstance(result.get('evidence'), list)
                    or not isinstance(result.get('prediction'), dict)
                    or set(result['prediction']) != set(METRICS)
                    or any(key not in METRICS or not finite(value)
                           for key, value in result['prediction'].items())):
                raise ValueError('Invalid HAT prediction')
            if len(result['evidence']) > len(METRICS) or any(
                    not isinstance(item, dict) or item.get('metric') not in METRICS
                    or not finite(item.get('score')) or not finite(item.get('observed'))
                    or not finite(item.get('baseline'))
                    or not isinstance(item.get('title'), str) or len(item['title']) > 100
                    or not isinstance(item.get('detail'), str) or len(item['detail']) > 300
                    for item in result['evidence']):
                raise ValueError('Invalid HAT evidence')
        return {**result, 'advisory_only': True}
    except (OSError, ValueError, TypeError) as error:
        return {'status': 'unavailable', 'backend': 'hailo', 'advisory_only': True,
                'error': type(error).__name__}
