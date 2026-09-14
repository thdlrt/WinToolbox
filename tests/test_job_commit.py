import time

from toolbox.app import App


def terminal(app, task):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = app.jobs.get(task['id'])
        if value['status'] not in ('running', 'queued', 'cancelling'):
            return value
        time.sleep(.01)
    raise AssertionError('Task did not finish')


def test_late_cancel_after_publication_reports_completed(tmp_path):
    app = App(tmp_path, register_features=False, register_live=False)
    try:
        def publish(job):
            job.mark_committed()
            app.jobs.cancel(job.id)
            job.cancel_event.set()  # Also cover a cancellation already in flight.
            job.progress(100, 'Published')
            return {'published': True}
        result = terminal(app, app.jobs.submit('test.publish', {}, publish))
        assert result['status'] == 'completed'
        assert result['result']['published'] is True
    finally:
        app.close()


def test_cancel_before_publication_remains_cancelled(tmp_path):
    app = App(tmp_path, register_features=False, register_live=False)
    try:
        def cancel(job):
            job.cancel_event.set()
            job.check_cancelled()
            raise AssertionError('Must not publish')
        result = terminal(app, app.jobs.submit('test.cancel', {}, cancel))
        assert result['status'] == 'cancelled'
    finally:
        app.close()
