import asyncio
import datetime
import inspect
import json
import os
import pathlib
import sys
import threading
import time
import traceback
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor

import apscheduler
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers import SchedulerNotRunningError

from bot import logger, default_timestamp
from bot.utils.data import read_json_file, write_data_file
from bot.utils.watcher import SyncWithFile

try:
    from bot.debug_helpers.helpers_decorators import async_calling_function
except ImportError:
    from bot import fake_decorator as async_calling_function


class DuelLinkRunTimeOptions(object):
    _last_run_at = datetime.datetime.fromtimestamp(default_timestamp)

    @property
    def last_run_at(self):
        return self._last_run_at

    @last_run_at.setter
    def last_run_at(self, value):
        if not isinstance(value, datetime.datetime):
            self.runtime_error_options("last_run_at", datetime.datetime, type(value))
            return
        if self._last_run_at == value:
            return
        self._last_run_at = value
        frame = inspect.currentframe()
        logger.debug("Value {} modified to {}".format(inspect.getframeinfo(frame).function, value))
        self.timeout_dump()

    _next_run_at = datetime.datetime.fromtimestamp(default_timestamp)

    @property
    def next_run_at(self):
        return self._next_run_at

    @next_run_at.setter
    def next_run_at(self, value):
        if not isinstance(value, datetime.datetime):
            self.runtime_error_options("next_run_at", datetime.datetime, type(value))
            return
        if self._next_run_at == value:
            return
        self._next_run_at = value
        frame = inspect.currentframe()
        logger.debug("Value {} modified to {}".format(inspect.getframeinfo(frame).function, value))
        self.timeout_dump()
        self.handle_option_change('next_run_at')

    _run_now = False

    @property
    def run_now(self):
        return self._run_now

    @run_now.setter
    def run_now(self, value):
        if not isinstance(value, bool):
            self.runtime_error_options("run_now", bool, type(value))
            return
        if self._run_now == value:
            return
        self._run_now = value
        frame = inspect.currentframe()
        logger.debug("Value {} modified".format(inspect.getframeinfo(frame).function))
        self.timeout_dump()
        self.handle_option_change('run_now')

    _stop = False

    @property
    def stop(self):
        return self._stop

    @stop.setter
    def stop(self, stop):
        if not isinstance(stop, bool):
            self.runtime_error_options("stop", bool, type(stop))
            return
        if self._stop == stop:
            return
        self._stop = stop
        frame = inspect.currentframe()
        logger.debug("Value {} modified".format(inspect.getframeinfo(frame).function))
        self.timeout_dump()
        self.handle_option_change('stop')

    _playmode = "autoplay"
    _available_modes = ['autoplay','guided']
    @property
    def playmode(self):
        return self._playmode

    @playmode.setter
    def playmode(self, playmode):
        if not isinstance(playmode, str):
            self.runtime_error_options("playmode", str, type(playmode))
            return
        if playmode not in self._available_modes:
            return
        if self._playmode == playmode:
            return
        self._playmode = playmode
        frame = inspect.currentframe()
        logger.debug("Value {} modified".format(inspect.getframeinfo(frame).function))
        self.timeout_dump()
        self.handle_option_change('playmode')

    _battle_calls = {
        "beforeStart": [],
        "afterStart" : [],
        "beforeEnd"  : [],
        "afterEnd"   : []
    }

    @property
    def battle_calls(self):
        return self._battle_calls

    @battle_calls.setter
    def battle_calls(self, value):
        if not isinstance(value, dict):
            self.runtime_error_options("battle_calls", dict, type(value))
            return
        if self._battle_calls == value:
            return
        self._battle_calls = value
        frame = inspect.currentframe()
        logger.debug("Value {} modified".format(inspect.getframeinfo(frame).function))
        self.timeout_dump()
        self.handle_option_change('battle_calls')

    @abstractmethod
    def runtime_error_options(self, option, expecting_type, got_type):
        raise NotImplementedError("runtime_error_options not implemented")

    @abstractmethod
    def timeout_dump(self):
        raise NotImplementedError("timeout_dump not implemented")

    @abstractmethod
    def handle_option_change(self, value):
        raise NotImplementedError("handle_option_change not implemented")


class DuelLinkRunTime(DuelLinkRunTimeOptions):
    _file = None
    _unknown_options = []
    _scheduler = None
    _config = None
    _watcher = None
    _timeout_dump = None
    _executor = None
    _provider = None
    _loop = None
    _run_main = None
    _job = None
    _allow_event_change = True
    _disable_dump = False

    def __init__(self, config, scheduler, auto_start=True):
        self._config = config
        self._file = config.get('bot', 'runTimePersistence')
        self._scheduler = scheduler
        self.setUp()
        if auto_start:
            self.start()

    def start(self):
        self.setUp()
        logger.debug("Watching {} for runTime Options".format(self._file))
        self._watcher = SyncWithFile(self._file)
        self._watcher.settings_modified = self.settings_modified

    def setUp(self):
        self._loop = asyncio.get_event_loop()
        self._loop.set_default_executor(ThreadPoolExecutor())
        if os.path.dirname(self._file) == "":
            self._file = os.path.join(os.getcwd(), self._file)
        pathlib.Path(os.path.dirname(self._file)).mkdir(parents=True, exist_ok=True)
        if os.path.exists(self._file):
            self.update()

    def handle_option_change(self, value):
        if self._provider is None:
            return
        if value == 'stop':
            if self.stop and self._provider.current_thread is not None:
                for x in threading.enumerate():
                    if x == self._provider.current_thread:
                        self._provider.current_thread.do_run = False
                        logger.info("Stopping Bot Execution")
            elif self._provider.current_thread is not None:
                logger.info("Resuming Bot Execution")
            elif self.stop:
                self.stop = False
        if value == 'run_now' and self.run_now:
            logger.info("Forcing run now")
            if self._provider.current_thread is None:
                try:
                    self._scheduler.remove_job(self._job)
                except JobLookupError:
                    pass
                self._scheduler.add_job(self._run_main, id='cron_main_force')
            else:
                logger.debug("Thread is currently running")
            self.run_now = False
        if value == 'next_run_at' and self._allow_event_change:
            try:
                self._scheduler.remove_job(self._job)
            except JobLookupError:
                pass
            self.schedule_next_run()
            next_run_at = self.next_run_at
            self._job = 'cron_main_at_{}'.format(next_run_at.isoformat())
            self._scheduler.add_job(self._run_main, trigger='date', id=self._job,
                                    run_date=next_run_at)

    def get_provider(self):
        return self._provider

    def set_provider(self, provider):
        self._provider = provider

    def settings_modified(self, events):
        self.update()

    def update(self):
        self._unknown_options = []
        try:
            tmp_data = read_json_file(self._file)
        except json.decoder.JSONDecodeError:
            logger.error("runtime file error reading")
            return
        if tmp_data is None:
            self.dump()
            return
        for key, value, in tmp_data.items():
            if key.startswith('_'):
                continue
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                self._unknown_options.append(key)
        if len(self._unknown_options) > 0:
            logger.debug("Unknown options were passed in [{}]".format(','.join(self._unknown_options)))

    def dump_options(self):
        tmpdict = {}
        for attribute in [a for a in dir(self) if not a.startswith('__') \
                                                  and not a.startswith('_') \
                                                  and not inspect.ismethod(getattr(self, a))
                                                  and not inspect.isfunction(getattr(self, a))]:
            # print(attribute, type(getattr(self,attribute)))
            tmpdict[attribute] = getattr(self, attribute)
        return tmpdict

    def dump(self):
        if not self._disable_dump:
            self._watcher.stop_observer()
            tmpdict = self.dump_options()
            logger.debug("Dump Getting Called {}".format(tmpdict))
            write_data_file(tmpdict, self._file)
            self._watcher.start_observer()
        #self._timeout_dump = None

    def timeout_dump(self):
        if self._timeout_dump is not None:
            try:
                self._timeout_dump.remove()
            except apscheduler.jobstores.base.JobLookupError:
                pass
        time = datetime.datetime.now() + datetime.timedelta(seconds=5)
        self._timeout_dump = self._scheduler.add_job(self.dump, trigger='date',
                                                     run_date=time)
        logger.debug("Timeout dump Scheduled")

    @staticmethod
    def runtime_error(message):
        logger.error(
            "Runtime error: {}".format(message)
        )

    def runtime_error_options(self, option, expecting_type, got_type):
        mess = "option {} has wrong type associated with it. Fix it, no events will be notified.".format(option)
        self.runtime_error(mess)
        mess = "option {} expecting {} but got {}".format(option, expecting_type, got_type)
        self.runtime_error(mess)

    def schedule_next_run(self):
        if self._watcher.observer:
            self._watcher.stop_observer()
        if self.next_run_at == datetime.datetime.fromtimestamp(default_timestamp):
            self.next_run_at = datetime.datetime.now() + datetime.timedelta(seconds=5)
        elif datetime.datetime.now() > self.next_run_at:
            self.next_run_at = datetime.datetime.now() + datetime.timedelta(seconds=5)
        else:
            next_at = self.next_run_at - datetime.datetime.now()
            self.next_run_at = datetime.datetime.now(
            ) + datetime.timedelta(seconds=next_at.total_seconds())
        self._watcher.start_observer()

    def determine_playthrough(self, provider):
        """
        Determines the mode to run
        :param provider: Provider
        :return:
        """
        if self.playmode == 'autoplay':
            logger.info("starting auto play through")
            provider.auto()
            logger.info("completed auto play through")
        elif self.playmode == 'guided':
            logger.info("starting guided play through")
            provider.guided_mode()
            logger.info("guided play through interrupted")
        else:
            logger.critical("Unknown play through mode")

    def main(self):
        def schedule_shutdown():
            try:
                self._scheduler.shutdown()
            except SchedulerNotRunningError:
                pass

        def thread_shutdown():
            self.shutdown()
            schedule_shutdown()

        def handle_exception(e):
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            logger.error(e)
            logger.debug("{} {} {}".format(exc_type, fname, exc_tb.tb_lineno))
            logger.debug(traceback.format_exc())
            logger.critical("Provider does not have method correctly implemented cannot continue")
            tt = threading.Thread(target=thread_shutdown, args=())
            tt.start()  # (schedule_shutdown, args=(), id='shutdown')

        def in_main():
            self.last_run_at = datetime.datetime.now()
            provider = self.get_provider()
            try:
                if not provider.is_process_running():
                    provider.start_process()
                    provider.wait_for_ui(30)
                    provider.pass_through_initial_screen(False)
                else:
                    provider.pass_through_initial_screen(True)
                provider.compare_with_back_button()
                self.determine_playthrough(provider)
            except NotImplementedError as ee:
                handle_exception(ee)
                return
            except AttributeError as ee:
                handle_exception(ee)
                return
            except TypeError as ee:
                handle_exception(ee)
                return
            except Exception as e:
                exc_type, exc_obj, exc_tb = sys.exc_info()
                fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                logger.debug("{} {} {}".format(exc_type, fname, exc_tb.tb_lineno))
                logger.debug(traceback.format_exc())
            self._watcher.stop_observer()
            self._allow_event_change = False
            self.next_run_at = datetime.datetime.now() + datetime.timedelta(hours=4)
            next_run_at = self.next_run_at
            self._allow_event_change = True
            self._job = 'cron_main_at_{}'.format(next_run_at.isoformat())
            self._scheduler.add_job(in_main, trigger='date', id=self._job,
                                    run_date=next_run_at)
            self._watcher.start_observer()

        self._allow_event_change = False
        self._run_main = in_main
        if self._config.getboolean("bot", "startBotOnStartUp"):
            self.next_run_at = datetime.datetime.now() + datetime.timedelta(seconds=1)
        else:
            self.schedule_next_run()
        next_run_at = self.next_run_at
        self._job = 'cron_main_at_{}'.format(next_run_at.isoformat())
        self._scheduler.add_job(in_main, trigger='date', id=self._job,
                                run_date=next_run_at)
        self._watcher.start_observer()
        self._allow_event_change = True
        logger.info("Tracking %s" % (self._file))
        logger.info('Next run at %s' % (self.next_run_at.isoformat()))

    _shutdown = False

    def shutdown(self):
        """ Waits for the current thread execution to become None or else will not shutdown properly"""
        self._disable_dump = True  # will not write to run time options
        self.stop = True  # signals all long_running operations to not execute, os calls will not occur either
        while self._provider.current_thread is not None:
            logger.warning('waiting for thread to stop')
            time.sleep(5)
        self._scheduler.shutdown()
        self._shutdown = True

    def __exit__(self):
        self.dump()
