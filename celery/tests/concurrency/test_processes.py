from __future__ import absolute_import

import errno
import socket
import time

from itertools import cycle

from mock import Mock, call, patch
from nose import SkipTest

from celery.five import items, range
from celery.utils.functional import noop
from celery.tests.case import AppCase
try:
    from celery.concurrency import processes as mp
except ImportError:

    class _mp(object):
        RUN = 0x1

        class TaskPool(object):
            _pool = Mock()

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

            def apply_async(self, *args, **kwargs):
                pass
    mp = _mp()  # noqa


class Object(object):   # for writeable attributes.

    def __init__(self, **kwargs):
        [setattr(self, k, v) for k, v in items(kwargs)]


class MockResult(object):

    def __init__(self, value, pid):
        self.value = value
        self.pid = pid

    def worker_pids(self):
        return [self.pid]

    def get(self):
        return self.value


class MockPool(object):
    started = False
    closed = False
    joined = False
    terminated = False
    _state = None

    def __init__(self, *args, **kwargs):
        self.started = True
        self._timeout_handler = Mock()
        self._result_handler = Mock()
        self.maintain_pool = Mock()
        self._state = mp.RUN
        self._processes = kwargs.get('processes')
        self._pool = [Object(pid=i, inqW_fd=1, outqR_fd=2)
                      for i in range(self._processes)]
        self._current_proc = cycle(range(self._processes))

    def close(self):
        self.closed = True
        self._state = 'CLOSE'

    def join(self):
        self.joined = True

    def terminate(self):
        self.terminated = True

    def terminate_job(self, *args, **kwargs):
        pass

    def restart(self, *args, **kwargs):
        pass

    def handle_result_event(self, *args, **kwargs):
        pass

    def grow(self, n=1):
        self._processes += n

    def shrink(self, n=1):
        self._processes -= n

    def apply_async(self, *args, **kwargs):
        pass


class ExeMockPool(MockPool):

    def apply_async(self, target, args=(), kwargs={}, callback=noop):
        from threading import Timer
        res = target(*args, **kwargs)
        Timer(0.1, callback, (res, )).start()
        return MockResult(res, next(self._current_proc))


class TaskPool(mp.TaskPool):
    Pool = BlockingPool = MockPool


class ExeMockTaskPool(mp.TaskPool):
    Pool = BlockingPool = ExeMockPool


class PoolCase(AppCase):

    def setup(self):
        try:
            import multiprocessing  # noqa
        except ImportError:
            raise SkipTest('multiprocessing not supported')


class test_AsynPool(PoolCase):

    def test_gen_not_started(self):

        def gen():
            yield 1
            yield 2
        g = gen()
        self.assertTrue(mp.gen_not_started(g))
        next(g)
        self.assertFalse(mp.gen_not_started(g))
        list(g)
        self.assertFalse(mp.gen_not_started(g))

    def test_select(self):
        ebadf = socket.error()
        ebadf.errno = errno.EBADF
        with patch('select.select') as select:
            select.return_value = ([3], [], [])
            self.assertEqual(
                mp._select(set([3])),
                ([3], [], 0),
            )

            select.return_value = ([], [], [3])
            self.assertEqual(
                mp._select(set([3]), None, set([3])),
                ([3], [], 0),
            )

            eintr = socket.error()
            eintr.errno = errno.EINTR
            select.side_effect = eintr

            readers = set([3])
            self.assertEqual(mp._select(readers), ([], [], 1))
            self.assertIn(3, readers)

        with patch('select.select') as select:
            select.side_effect = ebadf
            readers = set([3])
            self.assertEqual(mp._select(readers), ([], [], 1))
            select.assert_has_calls([call([3], [], [], 0)])
            self.assertNotIn(3, readers)

        with patch('select.select') as select:
            select.side_effect = MemoryError()
            with self.assertRaises(MemoryError):
                mp._select(set([1]))

        with patch('select.select') as select:

            def se(*args):
                select.side_effect = MemoryError()
                raise ebadf
            select.side_effect = se
            with self.assertRaises(MemoryError):
                mp._select(set([3]))

        with patch('select.select') as select:

            def se(*args):
                select.side_effect = socket.error()
                select.side_effect.errno = 1321
                raise ebadf
            select.side_effect = se
            with self.assertRaises(socket.error):
                mp._select(set([3]))

        with patch('select.select') as select:

            select.side_effect = socket.error()
            select.side_effect.errno = 34134
            with self.assertRaises(socket.error):
                mp._select(set([3]))

    def test_promise(self):
        fun = Mock()
        x = mp.promise(fun, (1, ), {'foo': 1})
        x()
        self.assertTrue(x.ready)
        fun.assert_called_with(1, foo=1)

    def test_Worker(self):
        w = mp.Worker(Mock(), Mock())
        w.on_loop_start(1234)
        w.outq.put.assert_called_with((mp.WORKER_UP, (1234, )))


class test_ResultHandler(PoolCase):

    def test_process_result(self):
        x = mp.ResultHandler(
            Mock(), Mock(), {}, Mock(),
            Mock(), Mock(), Mock(), Mock(),
            fileno_to_outq={},
            on_process_alive=Mock(),
        )
        self.assertTrue(x)
        x.on_state_change = Mock()
        proc = x.fileno_to_outq[3] = Mock()
        reader = proc.outq._reader
        reader.poll.return_value = False
        x.handle_event(6)  # KeyError
        x.handle_event(3)
        reader.poll.assert_called_with(0)
        self.assertFalse(x.on_state_change.called)

        reader.poll.reset()
        reader.poll.return_value = True
        task = reader.recv.return_value = (1, (2, 3))
        x.handle_event(3)
        reader.poll.assert_called_with(0)
        reader.recv.assert_called_with()
        x.on_state_change.assert_called_with(task)
        self.assertTrue(x._it)

        reader.recv.return_value = None
        x.handle_event(3)
        self.assertIsNone(x._it)

        x._state = mp.TERMINATE
        it = x._process_result()
        next(it)
        with self.assertRaises(mp.CoroStop):
            it.send(3)
        x.handle_event(3)
        self.assertIsNone(x._it)
        x._state == mp.RUN

        reader.recv.side_effect = EOFError()
        it = x._process_result()
        next(it)
        with self.assertRaises(mp.CoroStop):
            it.send(3)
        reader.recv.side_effect = None


class test_TaskPool(PoolCase):

    def test_start(self):
        pool = TaskPool(10)
        pool.start()
        self.assertTrue(pool._pool.started)
        self.assertTrue(pool._pool._state == mp.RUN)

        _pool = pool._pool
        pool.stop()
        self.assertTrue(_pool.closed)
        self.assertTrue(_pool.joined)
        pool.stop()

        pool.start()
        _pool = pool._pool
        pool.terminate()
        pool.terminate()
        self.assertTrue(_pool.terminated)

    def test_apply_async(self):
        pool = TaskPool(10)
        pool.start()
        pool.apply_async(lambda x: x, (2, ), {})

    def test_grow_shrink(self):
        pool = TaskPool(10)
        pool.start()
        self.assertEqual(pool._pool._processes, 10)
        pool.grow()
        self.assertEqual(pool._pool._processes, 11)
        pool.shrink(2)
        self.assertEqual(pool._pool._processes, 9)

    def test_info(self):
        pool = TaskPool(10)
        procs = [Object(pid=i) for i in range(pool.limit)]
        pool._pool = Object(_pool=procs,
                            _maxtasksperchild=None,
                            timeout=10,
                            soft_timeout=5)
        info = pool.info
        self.assertEqual(info['max-concurrency'], pool.limit)
        self.assertEqual(info['max-tasks-per-child'], 'N/A')
        self.assertEqual(info['timeouts'], (5, 10))

    def test_num_processes(self):
        pool = TaskPool(7)
        pool.start()
        self.assertEqual(pool.num_processes, 7)

    def test_restart(self):
        raise SkipTest('functional test')

        def get_pids(pool):
            return set([p.pid for p in pool._pool._pool])

        tp = self.TaskPool(5)
        time.sleep(0.5)
        tp.start()
        pids = get_pids(tp)
        tp.restart()
        time.sleep(0.5)
        self.assertEqual(pids, get_pids(tp))
