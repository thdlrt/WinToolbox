"""One scheduler for mergeable application data; automatic runs are not jobs."""
import contextlib
from datetime import datetime
import json
import threading
import time

from .settings import atomic_json


class SyncContext:
    def __init__(self, manager):
        self.manager = manager
        self.params = {}

    def check_cancelled(self):
        if self.manager.stop.is_set():
            raise RuntimeError('数据同步已停止')

    def progress(self, *_args, **_kwargs):
        self.check_cancelled()


class DataSync:
    def __init__(self, app, *, interval=60, debounce=1, retry_base=5, clock=time.monotonic):
        self.app, self.interval, self.debounce, self.retry_base = app, interval, debounce, retry_base
        self.clock = clock
        self.path = app.data_dir / 'data-sync-state.json'
        try:
            self.saved = json.loads(self.path.read_text('utf-8'))
        except (OSError, ValueError):
            self.saved = {}
        self.lock = threading.RLock()
        self.stop, self.wake = threading.Event(), threading.Event()
        self.services = {}
        self.worker = None
        self.closing = False
        self.connection_generation = 0

    def register(self, identifier, label, configured, run):
        with self.lock:
            prior = self.saved.get('services', {}).get(identifier, {})
            self.services[identifier] = dict(id=identifier, label=label, configured=configured, run=run,
                syncing=False, last_sync=prior.get('last_sync'), error=prior.get('error'),
                due=self.clock(), failures=0, generation=0, completed=-1, job=None, idle=threading.Event())
            self.services[identifier]['idle'].set()

    def request(self, identifier=None, *, immediate=True):
        with self.lock:
            for key, service in self.services.items():
                if identifier is None or key == identifier:
                    service['generation'] += 1
                    service['due'] = self.clock() + (0 if immediate else self.debounce)
            self.wake.set()

    def connection_changed(self):
        """Success belongs to a connection, never carry it to a new endpoint."""
        with self.lock:
            self.connection_generation += 1
            self.saved['last_sync'] = None
            for service in self.services.values():
                service.update(last_sync=None, error=None, failures=0, completed=-1)
            self.saved['services'] = {s['id']: {'last_sync': None, 'error': None}
                                      for s in self.services.values()}
            atomic_json(self.path, self.saved)
            self.request()

    def start(self):
        with self.lock:
            if self.worker is not None or self.stop.is_set():
                return
            self.request()
            self.worker = threading.Thread(target=self._loop, name='data-sync', daemon=True)
            self.worker.start()

    def _configured(self, service):
        try:
            return bool(service['configured']())
        except Exception as exc:
            with self.lock:
                service['error'] = str(exc)
            return False

    def status(self, _=None):
        # Call service readers outside the scheduler lock: local writes may wake
        # the scheduler while holding their own store locks.
        services = list(self.services.values())
        readiness = {s['id']: self._configured(s) for s in services}
        with self.lock:
            rows = [{key: s[key] for key in ('id', 'label', 'syncing', 'last_sync', 'error')}
                    | {'configured': readiness[s['id']]} for s in services]
            errors = [s['label'] + '：' + s['error'] for s in rows if s['error'] and s['configured']]
            result = {'configured': any(readiness.values()), 'syncing': any(s['syncing'] for s in rows),
                    'last_sync': self.saved.get('last_sync'), 'error': '\n'.join(errors) or None,
                    'services': rows}
        result['transfers'] = self.transfers()
        return result

    def transfers(self):
        """Observe user-selected file operations without authorizing new copies."""
        rows = []
        jobs = self.app.jobs.list() if hasattr(getattr(self.app, 'jobs', None), 'list') else []
        for identifier, label, mode in [('filesync', '文件夹同步', 'rules'), ('relay', '文件中转', 'manual')]:
            feature = getattr(self.app, identifier, None)
            if feature is None:
                continue
            related = [job for job in jobs if job.get('tool', '').startswith(identifier + '.')]
            active = any(job.get('status') in ('queued', 'running', 'cancelling') for job in related)
            try:
                if identifier == 'filesync':
                    rules = feature.rules()
                    last = [rule['last'] for rule in rules if rule.get('last')]
                    successful = [row['time'] for row in last if row.get('ok') and row.get('time')]
                    error = '\n'.join(row.get('message', '') for row in last if not row.get('ok')) or None
                    configured = bool(rules)
                    last_sync = max(successful) if successful else None
                else:
                    config = feature.config()
                    configured = bool(config.get('shared_configured'))
                    error = config.get('migration', {}).get('warning')
                    last_sync = None
                    operations = [job for job in related if job.get('tool') in ('relay.upload', 'relay.download')]
                    for job in operations:
                        if job.get('status') == 'completed':
                            try:
                                last_sync = datetime.fromisoformat(job['finished_at']).timestamp()
                            except (KeyError, ValueError, TypeError):
                                pass
                            break
                    if operations and operations[0].get('status') == 'failed':
                        error = error or operations[0].get('error') or operations[0].get('message')
                rows.append(dict(id=identifier, label=label, mode=mode, configured=configured,
                                 syncing=active, last_sync=last_sync, error=error))
            except Exception as exc:
                rows.append(dict(id=identifier, label=label, mode=mode, configured=False,
                                 syncing=active, last_sync=None, error=str(exc)))
        return rows

    def now(self, _=None):
        self.request()
        return self.status()

    def _persist(self, readiness):
        with self.lock:
            configured = [s for s in self.services.values() if readiness.get(s['id'])]
            if configured and all(s['last_sync'] and not s['error'] and not s['syncing']
                                  and s['completed'] == s['generation'] for s in configured):
                self.saved['last_sync'] = min(s['last_sync'] for s in configured)
            self.saved['services'] = {s['id']: {key: s[key] for key in ('last_sync', 'error')}
                                      for s in self.services.values()}
            atomic_json(self.path, self.saved)

    def run(self, identifier, job=None):
        service = self.services[identifier]
        # Maintenance takes the same short gate before replacing store files.
        # Never wait for network completion while holding app.data_lock.
        with getattr(self.app, 'data_lock', contextlib.nullcontext()):
            with self.lock:
                if self.closing or getattr(self.app, 'maintenance', False) or self.stop.is_set() and job is None:
                    return {'skipped': True, 'reason': 'maintenance'}
                if not service['idle'].is_set():
                    self.request(identifier)
                    return {'queued': True, 'reason': 'already_syncing'}
                service['syncing'] = True
                service['job'] = job
                service['idle'].clear()
                generation = service['generation']
                connection_generation = self.connection_generation
        error = None
        try:
            if not self._configured(service):
                raise ValueError('请先配置此服务的同步连接')
            result = service['run'](job or SyncContext(self))
            if isinstance(result, dict):
                if result.get('warnings'):
                    error = '；'.join(str(row.get('message', row)) if isinstance(row, dict) else str(row)
                                     for row in result['warnings'])
                if result.get('migration_pending'):
                    error = error or '旧位置数据尚未全部合并，将继续重试'
                if result.get('skipped'):
                    error = error or '同步尚未完成，将继续重试'
        except Exception as exc:
            error = str(exc) or '同步已中断'
            raise
        finally:
            with self.lock:
                service['syncing'] = False
                if connection_generation != self.connection_generation:
                    # The operation may have reached the old server. Its result
                    # says nothing about the newly configured connection.
                    service['due'] = self.clock()
                elif error:
                    service['error'] = error
                    service['failures'] += 1
                    retry = min(300, self.retry_base * 2 ** min(service['failures'] - 1, 6))
                    service['due'] = self.clock() + retry
                else:
                    service['error'] = None
                    service['failures'] = 0
                    service['last_sync'] = time.time()
                    service['completed'] = generation
                    if generation == service['generation']:
                        service['due'] = self.clock() + self.interval
                self.wake.set()
            try:
                readiness = {s['id']: self._configured(s) for s in list(self.services.values())}
                self._persist(readiness)
                if hasattr(self.app, 'emit'):
                    self.app.emit('data_sync.changed', **self.status())
            finally:
                service['idle'].set()
        with self.lock:
            if connection_generation != self.connection_generation:
                return {'queued': True, 'reason': 'connection_changed'}
            return result

    def tick(self):
        if self.stop.is_set() or getattr(self.app, 'maintenance', False):
            return
        for service in list(self.services.values()):
            with self.lock:
                due = not service['syncing'] and service['due'] <= self.clock()
            if due and not self._configured(service):
                with self.lock:
                    service['due'] = self.clock() + self.interval
            elif due:
                try:
                    self.run(service['id'])
                except Exception:
                    # run persists per-service errors and schedules bounded retry.
                    pass

    def _loop(self):
        while not self.stop.is_set():
            self.tick()
            self.wake.wait(.25)
            self.wake.clear()

    def assert_idle(self):
        with self.lock:
            if any(not s['idle'].is_set() for s in self.services.values()):
                raise RuntimeError('数据同步正在运行，请等待完成后再恢复备份')

    def stop_worker(self):
        self.stop.set()
        self.wake.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join()

    def close(self):
        with self.lock:
            self.closing = True
            for service in self.services.values():
                job = service.get('job')
                if job is not None and hasattr(job, 'cancel_event'):
                    job.cancel_event.set()
        self.stop_worker()
        for service in self.services.values():
            service['idle'].wait()


def ensure_manager(app):
    if not hasattr(app, 'data_sync'):
        app.data_sync = DataSync(app)
        app.register('data_sync.status', app.data_sync.status)
        app.register('data_sync.now', app.data_sync.now)
    return app.data_sync
