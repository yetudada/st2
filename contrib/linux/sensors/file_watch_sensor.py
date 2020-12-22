# Copyright 2020 The StackStorm Authors.
# Copyright 2019 Extreme Networks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
import os
import pathlib
import signal
import time
import sys

import eventlet
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

try:
    from st2reactor.sensor.base import Sensor
except ImportError:
    Sensor = object


class EventHandler(FileSystemEventHandler):
    def __init__(self, *args, callbacks=None, **kwargs):
        self.callbacks = callbacks or {}

    def dispatch(self, event):
        if not event.is_directory:
            super().dispatch(event)

    def on_created(self, event):
        cb = self.callbacks.get('created')
        if cb:
            cb(event=event)

    def on_modified(self, event):
        cb = self.callbacks.get('modified')
        if cb:
            result = cb(event=event)

    def on_moved(self, event):
        cb = self.callbacks.get('moved')
        if cb:
            cb(event=event)

    def on_deleted(self, event):
        cb = self.callbacks.get('deleted')
        if cb:
            cb(event=event)


class SingleFileTail(object):
    def __init__(self, path, handler, follow=False, read_all=False, observer=None, fd=None):
        self._path = None
        self.fd = fd
        self.handler = handler
        self.follow = follow
        self.read_all = read_all
        self.buffer = ''
        self.observer = observer or Observer()
        self.watch = None
        self.parent_watch = None

        if path:
            self.set_path(path)
            self.open()

    def get_path(self):
        return self._path

    # Set all of these when the path updates
    def set_path(self, new_path):
        self._path = pathlib.Path(new_path)
        self.parent_dir = self.get_parent_path(self._path)
        self.abs_path = (self.parent_dir / self._path).resolve()

    path = property(get_path, set_path)

    def get_parent_path(self, path):
        if path.is_absolute():
            return pathlib.Path(path).parent
        else:
            return (pathlib.Path.cwd() / path).parent

    def get_event_src_path(self, event):
        return (pathlib.Path.cwd() / pathlib.Path(event.src_path)).resolve()

    def read_chunk(self, fd, chunk_size=1024):
        # Buffer 1024 bytes at a time
        try:
            buffer = os.read(fd, chunk_size)
        except (OSError, FileNotFoundError):
            buffer = b""

        # If the 1024 bytes cuts the line off in the middle of a multi-byte
        # utf-8 character then decoding will raise an UnicodeDecodeError.
        try:
            buffer = buffer.decode(encoding='utf8')
        except UnicodeDecodeError as e:
            # Grab the first few bytes of the partial character
            # e.start is the first byte of the decoding issue
            first_byte_of_partial_character = buffer[e.start]
            number_of_bytes_read_so_far = e.end - e.start

            # Try to read the remainder of the character
            # You could replace these conditionals with bit math, but that's a
            # lot more difficult to read
            if first_byte_of_partial_character & 0xF0 == 0xC0:
                char_length = 2
            elif first_byte_of_partial_character & 0xF0 == 0xE0:
                char_length = 3
            elif first_byte_of_partial_character & 0xF0 == 0xF0:
                char_length = 4
            else:
                # We could have run into an issue besides reading a partial
                # character, so raise that exception
                raise e

            number_of_bytes_to_read = char_length - number_of_bytes_read_so_far

            buff = os.read(fd, number_of_bytes_to_read)
            if len(buff) == number_of_bytes_to_read:
                buffer += buff
                return buffer.decode(encoding='utf8')

            # If we did not successfully read a complete character, there's
            # nothing else we can really do but reraise the exception
            raise e
        else:
            return buffer

    def read(self, event=None):
        while True:
            # Read a chunk of bytes
            buff = self.read_chunk(self.fd)

            if not buff:
                return

            # Append to previous buffer
            if self.buffer:
                buff = self.buffer + buff
                self.buffer = ''

            lines = buff.splitlines(True)
            # If the last character of the last line is not a newline
            if lines and lines[-1] and lines[-1][-1] != '\n':  # Incomplete line in the buffer
                self.buffer = lines[-1]  # Save the last line fragment
                lines = lines[:-1]

            for line in lines:
                self.handler(self.path, line[:-1])

    def reopen_and_read(self, event=None, skip_to_end=False):
        # Directory watches will fire events for unrelated files
        # Ignore all events except those for our path
        if event and self.get_event_src_path(event) != self.abs_path:
            return

        # Save our current position into the file (this is a little wonky)
        pos = os.lseek(self.fd, 0, os.SEEK_CUR)

        # The file was moved and not recreated
        # If we're following the file, don't emit the remainder of the last
        # line
        emit = not self.follow
        self.close(event=event, emit_remaining=emit)

        # If we aren't following then don't reopen the file
        # When the file is created again that will be handled by open_and_read
        if not self.follow:
            return

        # Use the file's new location
        self.path = event.dest_path
        # Seek to where we left off
        self.open(event=event, seek_to=pos)
        self.read(event=event)

    def open_and_read(self, event=None, seek_to=None):
        # Directory watches will fire events for unrelated files
        # Ignore all events except those for our path
        if event and self.get_event_src_path(event) != self.abs_path:
            return

        self.read_all = True

        self.open(event=event, seek_to=seek_to)
        self.read(event=event)

    def open(self, event=None, seek_to=None):
        # Use self.watch as a guard
        if not self.watch:
            try:
                self.stat = os.stat(self.path)
            except FileNotFoundError:
                # If the file doesn't exist when we are asked to monitor it, set
                # this flag so we read it all if/when it does appear
                self.read_all = True
            else:
                self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)

                if self.read_all or seek_to == 'start':
                    os.lseek(self.fd, 0, os.SEEK_SET)

                if not self.read_all or seek_to == 'end':
                    os.lseek(self.fd, 0, os.SEEK_END)

                file_event_handler = EventHandler(callbacks={
                    'created': self.open,
                    'deleted': self.close,
                    'modified': self.read,
                    'moved': self.reopen_and_read,
                })

                self.watch = self.observer.schedule(file_event_handler, self.path)

        # Avoid watching this twice
        if not self.parent_watch:
            dir_event_handler = EventHandler(callbacks={
                'created': self.open_and_read,
                'moved': self.reopen_and_read,
            })

            self.parent_watch = self.observer.schedule(dir_event_handler, self.parent_dir)

    def close(self, event=None, emit_remaining=True):
        # Reset the guard
        if self.buffer and emit_remaining:
            self.handler(self.path, self.buffer)
            self.buffer = ''
        if self.fd:
            os.close(self.fd)
            self.fd = None
        if self.watch:
            self.observer.unschedule(self.watch)
            self.watch = None


class TailManager(object):
    def __init__(self, *args, **kwargs):
        self.tails = {}
        self.observer = Observer()

    def tail_file(self, path, handler, follow=False, read_all=False):
        if handler not in self.tails.setdefault(path, {}):
            sft = SingleFileTail(path, handler,
                                 follow=follow, read_all=read_all,
                                 observer=self.observer)
            self.tails[path][handler] = sft

    def stop_tailing_file(self, path, handler):
        tailed_file = self.tails.get(path, {}).pop(handler)
        tailed_file.close()
        # Amortize some cleanup while we're at it
        if not self.tails.get(path):
            self.tails.pop(path)

    def run(self):
        self.start()
        try:
            while True:
                time.sleep(1)
        finally:
            self.stop()

    def start(self):
        self.observer.start()

    def stop(self):
        for handlers in self.tails.values():
            for tailed_file in handlers.values():
                tailed_file.close()
        self.observer.stop()
        self.observer.join()


class FileWatchSensor(Sensor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stop = False
        self.trigger = None
        self.logger = self.sensor_service.get_logger(__name__)

    def setup(self):
        self.tail_manager = TailManager()

    def run(self):
        self.tail_manager.run()
        while not self._stop:
            eventlet.sleep(60)

    def cleanup(self):
        self._stop = True
        self.tail_manager.stop()

    def add_trigger(self, trigger):
        file_path = trigger['parameters'].get('file_path', None)

        if not file_path:
            self.logger.error('Received trigger type without "file_path" field.')
            return

        self.trigger = trigger.get('ref', None)

        if not self.trigger:
            raise Exception('Trigger %s did not contain a ref.' % trigger)

        self.tail_manager.tail_file(file_path, self._handle_line)
        self.logger.info('Added file "%s"' % (file_path))

    def update_trigger(self, trigger):
        pass

    def remove_trigger(self, trigger):
        file_path = trigger['parameters'].get('file_path', None)

        if not file_path:
            self.logger.error('Received trigger type without "file_path" field.')
            return

        self.tail_manager.stop_tailing_file(file_path, self._handle_line)
        self.trigger = None

        self.logger.info('Removed file "%s"' % (file_path))

    def _handle_line(self, file_path, line):
        payload = {
            'file_path': file_path,
            'file_name': pathlib.Path(file_path).name,
            'line': line
        }
        self.logger.debug('Sending payload %s for trigger %s to sensor_service.',
                          payload, self.trigger)
        self.sensor_service.dispatch(trigger=self.trigger, payload=payload)


if __name__ == '__main__':
    tm = TailManager()
    tm.tail_file(__file__, handler=print)
    tm.run()

    def halt(sig, frame):
        tm.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, halt)
