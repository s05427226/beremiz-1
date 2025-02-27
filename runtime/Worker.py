#!/usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of Beremiz runtime.
#
# Copyright (C) 2018: Edouard TISSERANT
#
# See COPYING.Runtime file for copyrights details.

from __future__ import absolute_import
import sys
from threading import Lock, Condition, Thread

import six
from six.moves import _thread


class job(object):
    """
    job to be executed by a worker
    """
    def __init__(self, call, *args, **kwargs):
        self.job = (call, args, kwargs)
        self.result = None
        self.success = None
        self.exc_info = None

    def do(self):
        """
        do the job by executing the call, and deal with exceptions
        """
        try:
            call, args, kwargs = self.job
            self.result = call(*args, **kwargs)
            self.success = True
        except Exception:
            self.success = False
            self.exc_info = sys.exc_info()


class worker(object):
    """
    serialize main thread load/unload of PLC shared objects
    """
    def __init__(self):
        # Only one job at a time
        self._finish = False
        self._threadID = None
        self.mutex = Lock()
        self.todo = Condition(self.mutex)
        self.done = Condition(self.mutex)
        self.free = Condition(self.mutex)
        self.job = None
        self.enabled = False
        self.stopper = None
        self.own_thread = None

    def reraise(self, job):
        """
        reraise exception happend in a job
        @param job: job where original exception happend
        """
        exc_type = job.exc_info[0]
        exc_value = job.exc_info[1]
        exc_traceback = job.exc_info[2]
        six.reraise(exc_type, exc_value, exc_traceback)

    def runloop(self, *args, **kwargs):
        """
        meant to be called by worker thread (blocking)
        """
        self._threadID = _thread.get_ident()
        self.mutex.acquire()
        self.enabled = True
        if args or kwargs:
            _job = job(*args, **kwargs)
            _job.do()
            # _job.success can't be None after do()
            if not _job.success:
                self.reraise(_job)

        while not self._finish:
            self.todo.wait()
            if self.job is not None:
                self.job.do()
                self.done.notify()
            else:
                break

        self.mutex.release()

    def interleave(self, waker, stopper, *args, **kwargs):
        """
        as for twisted reactor's interleave, it passes all jobs to waker func
        additionaly, it creates a new thread to wait for new job.
        """
        self.feed = Condition(self.mutex)
        self._threadID = _thread.get_ident()
        self.stopper = stopper

        def wakerfeedingloop():
            self.mutex.acquire()
            self.enabled = True
            if args or kwargs:
                def first_job_todo():
                    _job = job(*args, **kwargs)
                    _job.do()
                    if not _job.success:
                        self.reraise(_job)
                    self.mutex.acquire()
                    self.feed.notify()
                    self.mutex.release()
                waker(first_job_todo)
                self.feed.wait()

            while not self._finish:
                self.todo.wait()
                def job_todo():
                    self.mutex.acquire()
                    if self.job is not None:
                        self.job.do()
                        self.feed.notify()
                        self.done.notify()
                    self.mutex.release()
                if self._finish:
                    break
                waker(job_todo)
                self.feed.wait()

            self.mutex.release()
        self.own_thread = Thread(target = wakerfeedingloop)
        self.own_thread.start()

    def stop(self):
        """
        !interleave
        """
        self.mutex.acquire()
        self._finish = True
        self.enabled = False
        self.job = None
        self.todo.notify()
        self.done.notify()
        self.mutex.release()
        self.own_thread.join()

    def call(self, *args, **kwargs):
        """
        creates a job, execute it in worker thread, and deliver result.
        if job execution raise exception, re-raise same exception
        meant to be called by non-worker threads, but this is accepted.
        blocking until job done
        """

        _job = job(*args, **kwargs)

        if self._threadID == _thread.get_ident():
            # if caller is worker thread execute immediately
            _job.do()
        else:
            # otherwise notify and wait for completion
            self.mutex.acquire()
            if not self.enabled:
                self.mutex.release()
                raise EOFError("Worker is disabled")

            while self.job is not None:
                self.free.wait()

            self.job = _job
            self.todo.notify()
            self.done.wait()
            self.job = None
            self.free.notify()
            self.mutex.release()

        if _job.success is None:
            raise EOFError("Worker job was interrupted")

        if _job.success:
            return _job.result
        else:
            self.reraise(_job)

    def quit(self):
        """
        unblocks main thread, and terminate execution of runloop()
        """
        # mark queue
        self._finish = True
        self.mutex.acquire()
        self.enabled = False
        self.job = None
        self.todo.notify()
        self.done.notify()
        self.mutex.release()

    def finish(self):
        if self.own_thread is None:
            self.quit()
        if self.stopper is not None:
            self.stopper()
