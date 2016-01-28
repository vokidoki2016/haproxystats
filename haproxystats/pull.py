#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
# pylint: disable=no-member
#
"""Pulls statistics from HAProxy daemon over UNIX socket(s)

Usage:
    haproxystats-pull [-f <file> ] [-p | -P]

Options:
    -f, --file <file>  configuration file with settings
                       [default: /etc/haproxystats.conf]
    -p, --print        show default settings
    -P, --print-conf   show configuration
    -h, --help         show this screen
    -v, --version      show version
"""
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
import sys
import time
import signal
import shutil
import logging
from configparser import ConfigParser, ExtendedInterpolation
import copy
import glob
from docopt import docopt

from haproxystats import __version__ as VERSION
from haproxystats import DEFAULT_OPTIONS
from haproxystats.utils import (is_unix_socket, CMD_SUFFIX_MAP)


LOG_FORMAT = ('%(asctime)s [%(process)d] [%(threadName)-10s:%(funcName)s] '
              '%(levelname)-8s %(message)s')
logging.basicConfig(format=LOG_FORMAT)
log = logging.getLogger('root')  # pylint: disable=I0011,C0103
CMDS = ['show info', 'show stat']


def shutdown():
    """Performs a clean shutdown"""
    log.info('received stop signal, cancelling tasks...')
    for task in asyncio.Task.all_tasks():
        task.cancel()
    log.info('bye, exiting...')


def write_file(filename, data):
    """Writes data to a file.

    Returns:
        True if succeeds False otherwise.
    """
    try:
        with open(filename, 'w') as file_handle:
            file_handle.write(data.decode())
    except OSError as exc:
        log.critical('failed to write data %s', exc)
        return False
    else:
        log.debug('data saved in %s', filename)
        return True


@asyncio.coroutine
def get(socket_file, cmd, storage_dir, loop, executor, timeout):
    """Fetches data from a UNIX socket.

    Sends a command to HAProxy over UNIX socket, reads the response and then
    offloads the writing of the received data to a thread, so we don't block
    this coroutine.

    Arguments:
        socket_file (str): The full path of the UNIX socket file to connect to.
        cmd (str): The command to send.
        storage_dir (str): The full path of the directory to save the response.
        loop (obj): A base event loop from asyncio module.
        executor (obj): A Threader executor to execute calls asynchronously.
        timeout (int): Timeout for the connection to the socket.

    Returns:
        True if statistics from a UNIX sockets are save False otherwise.
    """
    # try to connect to the UNIX socket
    connect = asyncio.open_unix_connection(socket_file)
    log.debug('connecting to UNIX socket %s', socket_file)
    try:
        reader, writer = yield from asyncio.wait_for(connect, timeout)
    except (ConnectionRefusedError, PermissionError, OSError) as exc:
        log.critical(exc)
        return False
    else:
        log.debug('connection established to UNIX socket %s', socket_file)

    log.debug('sending command "%s" to UNIX socket %s', cmd, socket_file)
    writer.write('{c}\n'.format(c=cmd).encode())
    data = yield from reader.read()
    writer.close()

    if len(data) == 0:
        log.critical('received zero data')
        return False

    log.debug('received data from UNIX socket %s', socket_file)

    suffix = CMD_SUFFIX_MAP.get(cmd.split()[1])
    filename = os.path.basename(socket_file) + suffix
    filename = os.path.join(storage_dir, filename)
    log.debug('going to save data to %s', filename)
    # Offload the writing to a thread so we don't block ourselves.
    result = yield from loop.run_in_executor(executor,
                                             write_file,
                                             filename,
                                             data)

    return result


@asyncio.coroutine
def pull_stats(config, storage_dir, loop, executor):
    """Launches coroutines for pulling statistics from UNIX sockets.

    This a delegating routine.

    Arguments:
        config (obj): A configParser object which holds configuration.
        storage_dir (str): The absolute directory path to save the statistics.
        loop (obj): A base event loop.
        executor(obj): A ThreadPoolExecutor object.

    Returns:
        True if statistics from *all* UNIX sockets are fetched False otherwise.
    """
    # absolute directory path which contains UNIX socket files.
    socket_dir = config.get('pull', 'socket-dir')
    timeout = config.getint('pull', 'timeout')
    socket_files = [f for f in glob.glob(socket_dir + '/*')
                    if is_unix_socket(f)]

    log.debug('pull statistics')
    coroutines = [get(socket_file, cmd, storage_dir, loop, executor, timeout)
                  for socket_file in socket_files
                  for cmd in CMDS]
    # Launch all connections.
    status = yield from asyncio.gather(*coroutines)

    return len(set(status)) == 1 and True in set(status)


def supervisor(loop, config):
    """Coordinates the pulling of HAProxy statistics from UNIX sockets.

    This is the client routine which launches requests to all HAProxy
    UNIX sockets for retrieving statistics and save them to file-system.
    It runs indefinitely until main program is terminated.

    Arguments:
        loop (obj): A base event loop from asyncio module.
        config (obj): A configParser object which holds configuration.
    """
    dst_dir = config.get('pull', 'dst-dir')
    tmp_dst_dir = config.get('pull', 'tmp-dst-dir')
    executor = ThreadPoolExecutor(max_workers=config.getint('pull', 'workers'))
    exit_code = 1

    while True:
        start_time = int(time.time())
        # HAProxy statistics are stored in a directory and we use retrieval
        # time(seconds since the Epoch) as a name of the directory.
        # We first store them in a temporary place until we receive statistics
        # from all UNIX sockets.
        storage_dir = os.path.join(tmp_dst_dir, str(start_time))

        # If our storage directory can't be created we can't do much, thus
        # abort main program.
        try:
            os.makedirs(storage_dir)
        except OSError as exc:
            msg = "failed to make directory {d}:{e}".format(d=storage_dir,
                                                            e=exc)
            log.critical(msg)
            log.critical('a fatal error has occurred, exiting..')
            break

        try:
            # Launch the delegating coroutine
            result = loop.run_until_complete(pull_stats(config, storage_dir,
                                                        loop, executor))
        except asyncio.CancelledError:
            log.info('Received CancelledError exception')
            exit_code = 0
            break

        # if and only if we received statistics from all sockets then move
        # statistics to the permanent directory.
        # NOTE: when temporary and permanent storage directory are on the same
        # file-system the move is actual a rename, which is an atomic
        # operation.
        if result:
            log.debug('move %s to %s', storage_dir, dst_dir)
            try:
                shutil.move(storage_dir, dst_dir)
            except OSError as exc:
                log.critical("failed to move %s to %s: %s", storage_dir,
                             dst_dir, exc)
                log.critical('a fatal error has occurred, exiting..')
                break
            else:
                log.info('statistics are stored in %s', os.path.join(
                    dst_dir, os.path.basename(storage_dir)))
        else:
            log.critical('failed to pull stats')
            log.debug('removing temporary directory %s', storage_dir)
            shutil.rmtree(storage_dir)

        # calculate sleep time which is interval minus elapsed time.
        sleep = config.getint('pull', 'pull-interval') - (time.time() -
                                                          start_time)
        if 0 < sleep < config.getint('pull', 'pull-interval'):
            log.debug('sleeping for %.3fs secs', sleep)
            time.sleep(sleep)

    # It is very unlikely that threads haven't finished their job by now, but
    # they perform disk IO operations which can take some time in certain
    # situations, thus we want to wait for them in order to perform a clean
    # shutdown.
    executor.shutdown(wait=True)
    loop.close()
    sys.exit(exit_code)


def main():
    """Parses CLI arguments and launches main program."""
    args = docopt(__doc__, version=VERSION)

    config = ConfigParser(interpolation=ExtendedInterpolation())
    # Set defaults for all sections
    config.read_dict(copy.copy(DEFAULT_OPTIONS))
    # Load configuration from a file. NOTE: ConfigParser doesn't warn if user
    # sets a filename which doesn't exist, in this case defaults will be used.
    config.read(args['--file'])

    if args['--print']:
        for section in sorted(DEFAULT_OPTIONS):
            print("[{}]".format(section))
            for key, value in sorted(DEFAULT_OPTIONS[section].items()):
                print("{k} = {v}".format(k=key, v=value))
            print()
        sys.exit(0)
    if args['--print-conf']:
        for section in sorted(config):
            print("[{}]".format(section))
            for key, value in sorted(config[section].items()):
                print("{k} = {v}".format(k=key, v=value))
            print()
        sys.exit(0)

    log.setLevel(getattr(logging, config.get('pull', 'loglevel').upper(),
                         None))
    # Setup our event loop
    loop = asyncio.get_event_loop()

    # Register shutdown to signals
    loop.add_signal_handler(signal.SIGHUP, shutdown)
    loop.add_signal_handler(signal.SIGTERM, shutdown)

    # a temporary directory to store fetched data
    tmp_dst_dir = config['pull']['tmp-dst-dir']
    # a permanent directory to move data from the temporary directory. Data are
    # picked up by the process daemon from that directory.
    dst_dir = config['pull']['dst-dir']
    for directory in dst_dir, tmp_dst_dir:
        try:
            os.makedirs(directory)
        except OSError as exc:
            # errno 17 => file exists
            if exc.errno != 17:
                sys.exit("failed to make directory {d}:{e}".format(d=directory,
                                                                   e=exc))
    supervisor(loop, config)

# This is the standard boilerplate that calls the main() function.
if __name__ == '__main__':
    main()