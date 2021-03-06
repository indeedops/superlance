#!/usr/bin/env python
##############################################################################
#
# Copyright (c) 2007 Agendaless Consulting and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the BSD-like license at
# http://www.repoze.org/LICENSE.txt.  A copy of the license should accompany
# this distribution.  THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL
# EXPRESS OR IMPLIED WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND
# FITNESS FOR A PARTICULAR PURPOSE
#
##############################################################################

# A event listener meant to be subscribed to TICK_60 (or TICK_5)
# events, which restarts processes that are children of
# supervisord based on the response from an HTTP port.

# A supervisor config snippet that tells supervisor to use this script
# as a listener is below.
#
# [eventlistener:httpok]
# command=python -u /bin/httpok http://localhost:8080/tasty/service
# events=TICK_60

doc = """\
httpok.py [-p processname] [-a] [-g] [-D] [-t timeout] [-c status_code] [-b inbody]
    [-B restart_string] [-m mail_address] [-s sendmail] [-r restart_threshold]
    [-n restart_timespan] [-x external_script] [-G grace_period]
    [-o grace_count] URL

Options:

-p -- specify a supervisor process_name.  Restart the supervisor
      process named 'process_name' if it's in the RUNNING state when
      the URL returns an unexpected result or times out.  If this
      process is part of a group, it can be specified using the
      'group_name:process_name' syntax.

-a -- Restart any child of the supervisord under in the RUNNING state
      if the URL returns an unexpected result or times out.  Overrides
      any -p parameters passed in the same httpok process
      invocation.

-g -- The ``gcore`` program.  By default, this is ``/usr/bin/gcore
      -o``.  The program should accept two arguments on the command
      line: a filename and a pid.

-d -- Core directory.  If a core directory is specified, httpok will
      try to use the ``gcore`` program (see ``-g``) to write a core
      file into this directory against each hung process before we
      restart it.  Append gcore stdout output to email.

-t -- The number of seconds that httpok should wait for a response
      before timing out.  If this timeout is exceeded, httpok will
      attempt to restart processes in the RUNNING state specified by
      -p or -a.  This defaults to 10 seconds.

-c -- specify an expected HTTP status code from a GET request to the
      URL.  If this status code is not the status code provided by the
      response, httpok will attempt to restart processes in the
      RUNNING state specified by -p or -a.  This defaults to the
      string, "200".

-C -- specify 'stdout' or 'stderr' for generating PROCESS_COMMUNICATION
      events from httpok. This needs that the provided stream is placed
      in the 'capture mode' by setting std{err, out}_capture_maxbytes.

-b -- specify a string which should be present in the body resulting
      from the GET request.  If this string is not present in the
      response, the processes in the RUNNING state specified by -p
      or -a will be restarted.  The default is to ignore the
      body.

-B -- specify a string which should NOT be present in the body resulting
      from the GET request. If this string is present in the
      response, the processes in the RUNNING state specified by -p
      or -a will be restarted.  This option is the opposite of the -b option
      and can be specified multiple times and it may be specified along with
      the -b option. The default is to ignore the restart string.

-s -- the sendmail command to use to send email
      (e.g. "/usr/sbin/sendmail -t -i").  Must be a command which accepts
      header and message data on stdin and sends mail.
      Default is "/usr/sbin/sendmail -t -i".

-m -- specify an email address.  The script will send mail to this
      address when httpok attempts to restart processes.  If no email
      address is specified, email will not be sent.

-e -- "eager":  check URL / emit mail even if no process we are monitoring
      is in the RUNNING state.  Enabled by default.

-E -- not "eager":  do not check URL / emit mail if no process we are
      monitoring is in the RUNNING state.

-r -- specify the maximum number of times program should be restarted if it
      does not return successful result while issuing a GET. 0 - for unlimited
      number of restarts. Default is 3.

-n -- specify the time span in minutes during which the maximum number of
      restarts could happen. This prevents loop restarts when the application
      is running fine for configured TICK seconds then starts to fail again.
      Default is 60.

-x -- optionally specify an external script to restart the program, e.g.
      /etc/init.d/myprogramservicescript.

-G -- specify the grace period in minutes before starting to act on programs
      which are failing their healthchecks. Grace period is counted since
      the last time program was (re)started. Default is 0.

-o -- specify the grace count to ignore a number of errors before restarting
      programs which are failing their healthchecks. Grace count is counted
      since the last time program was (re)started and is checked before
      program restart counter (-n option). Default is 0.

-D -- dry run mode which will prevent httpok from restarting the monitored
      program. Useful for testing purposes. In this mode, httpok will log
      all actions as usual, however httpok restart attempts won't take effect.

URL -- The URL to which to issue a GET request.

The -p option may be specified more than once, allowing for
specification of multiple processes.  Specifying -a overrides any
selection of -p.

A sample invocation:

httpok.py -p program1 -p group1:program2 http://localhost:8080/tasty

"""

import copy
import os
import socket
import sys
import time
import traceback
import urllib

from collections import defaultdict

from superlance.compat import urlparse
from superlance.compat import xmlrpclib
from superlance.utils import ExternalService, Log

from supervisor import childutils
from supervisor.states import ProcessStates
from supervisor.options import make_namespec

from superlance import timeoutconn

def usage():
    print(doc)
    sys.exit(255)

class HTTPOk:
    connclass = None
    # For backward compatibility setting restart argument defaults to 0 and
    # ext_service to None
    def __init__(self, rpc, programs, any, url, timeout, status, inbody,
                 email, sendmail, coredir, gcore, eager, retry_time,
                 restart_threshold=0, restart_timespan=0, ext_service=None,
                 restart_string=None, grace_period=None, grace_count=0,
                 capture_mode_stream=None, dry_run=False):
        self.rpc = rpc
        self.programs = programs
        self.any = any
        self.url = url
        self.timeout = timeout
        self.retry_time = retry_time
        self.status = status
        self.inbody = inbody
        self.restart_string = restart_string
        self.email = email
        self.sendmail = sendmail
        self.coredir = coredir
        self.gcore = gcore
        self.eager = eager
        self.stdin = sys.stdin
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        self.counter = {}
        self.error_counter = defaultdict(int)
        self.restart_threshold = restart_threshold
        self.restart_timespan = restart_timespan * 60
        self.ext_service = ext_service
        self.grace_period = grace_period * 60 if grace_period else 0
        self.grace_count = grace_count
        self.dry_run = dry_run
        self.params = {
            'source': 'httpok',
            'response_status': self.status,
            'restart_string': self.restart_string,
            'restart_threshold': self.restart_threshold,
            'restart_timespan': self.restart_timespan,
            'grace_period': self.grace_period,
            'grace_count': self.grace_count,
            'in_body': self.inbody,
        }
        if capture_mode_stream:
            if capture_mode_stream == "stderr":
                self.capture_mode_stream = self.stderr
            elif capture_mode_stream == "stdout":
                self.capture_mode_stream = self.stdout
            else:
                self.capture_mode_stream = None
        else:
            self.capture_mode_stream = None
        self.log = Log(__name__)

    def listProcesses(self, state=None):
        return [x for x in self.rpc.supervisor.getAllProcessInfo()
                   if x['name'] in self.programs and
                      (state is None or x['state'] == state)]

    def runforever(self, test=False):
        parsed = urlparse.urlsplit(self.url)
        scheme = parsed[0].lower()
        hostport = parsed[1]
        self.path = parsed[2]
        query = parsed[3]

        if query:
            self.path += '?' + query
            self.prefix = '&'
        else:
            self.prefix = '?'

        if self.connclass:
            ConnClass = self.connclass
        elif scheme == 'http':
            ConnClass = timeoutconn.TimeoutHTTPConnection
        elif scheme == 'https':
            ConnClass = timeoutconn.TimeoutHTTPSConnection
        else:
            raise ValueError('Bad scheme %s' % scheme)

        while 1:
            # we explicitly use self.stdin, self.stdout, and self.stderr
            # instead of sys.* so we can unit test this code
            headers, payload = childutils.listener.wait(self.stdin, self.stdout)

            if not headers['eventname'].startswith('TICK'):
                # do nothing with non-TICK events
                childutils.listener.ok(self.stdout)
                if test:
                    break
                continue

            self.conn = ConnClass(hostport)
            self.conn.timeout = self.timeout

            try:
                specs = self.listProcesses(ProcessStates.RUNNING)
            except Exception as e:
                self.log.logger.warning('Exception occurred while trying to get '
                    'the list of processes: %s', e)
                traceback.print_exc()
                self.log.logger.warning('Trying to re-establish pipe...')
                self.rpc = childutils.getRPCInterface(os.environ)
                childutils.listener.ok(self.stdout)
                continue
            if self.eager or len(specs) > 0:

                try:
                    for will_retry in range(
                            self.timeout // (self.retry_time or 1) - 1 ,
                            -1, -1):
                        try:
                            params = urllib.urlencode(self.params, True)
                            headers = {'User-Agent': 'httpok'}
                            self.conn.request('GET', self.path + self.prefix + \
                                params, headers=headers)
                            break
                        except socket.error as e:
                            if e.errno == 111 and will_retry:
                                time.sleep(self.retry_time)
                            else:
                                raise

                    res = self.conn.getresponse()
                    body = res.read()
                    self.res_status = res.status
                    msg = 'status contacting %s: %s %s' % (self.url,
                                                           res.status,
                                                           res.reason)
                except Exception as e:
                    body = ''
                    self.res_status = None
                    msg = 'error contacting %s:\n\n %s' % (self.url, e)

                if str(self.res_status) != str(self.status):
                    subject = 'httpok for %s: bad status returned' % self.url
                    self.act(subject, msg)
                elif self.inbody and self.inbody not in body:
                    subject = 'httpok for %s: bad body returned' % self.url
                    self.act(subject, msg)
                elif self.restart_string and isinstance(self.restart_string,
                                                        list):
                    if any(restart_string in body for restart_string in
                           self.restart_string):
                        subject = 'httpok for %s: restart string in body' % \
                                  self.url
                        self.act(subject, msg)
                if [ spec for spec, value in self.counter.items()
                      if value['counter'] > 0]:
                    # Null the counters if timespan is over
                    self.cleanCounters()

            childutils.listener.ok(self.stdout)
            if test:
                break

    def act(self, subject, msg):
        messages = [msg]
        email = True

        logstopper = False

        def write(msg):
            self.log.logger.warning(msg)
            messages.append(msg)

        try:
            specs = self.rpc.supervisor.getAllProcessInfo()
        except Exception as e:
            write('Exception retrieving process info %s, not acting' % e)
            return

        waiting = list(self.programs)

        if self.any:
            write('Trying to restart all affected processes')
            for spec in specs:
                name = spec['name']
                group = spec['group']
                now = spec['now']
                starttime = spec['start']
                if (now - starttime) < self.grace_period:
                    write('Grace period has not been elapsed since %s was '
                          'last restarted' % name)
                    logstopper = True
                    continue
                if not self.errorCounter(spec, write):
                    write('Restart counter for %s is lower than %s, '
                          'not restarting at this time' % (name,
                          self.grace_count))
                    continue
                if self.restartCounter(spec, write):
                    self.restart(spec, write)
                else:
                    email = False
                namespec = make_namespec(group, name)
                if name in waiting:
                    waiting.remove(name)
                if namespec in waiting:
                    waiting.remove(namespec)
        else:
            write('Trying to restart affected processes %s' % self.programs)
            for spec in specs:
                name = spec['name']
                group = spec['group']
                now = spec['now']
                starttime = spec['start']
                namespec = make_namespec(group, name)
                if (name in self.programs) or (namespec in self.programs):
                    if (now - starttime) < self.grace_period:
                        write('Grace period has not been elapsed since %s was '
                              'last restarted' % name)
                        logstopper = True
                        continue
                    if not self.errorCounter(spec, write):
                        write('Restart counter for %s is lower than %s, '
                              'not restarting at this time' % (name,
                              self.grace_count))
                        continue
                    if self.restartCounter(spec, write):
                        self.restart(spec, write)
                    else:
                        email = False
                    if name in waiting:
                        waiting.remove(name)
                    if namespec in waiting:
                        waiting.remove(namespec)

        if not logstopper and waiting:
            write('Programs not restarted because they did not exist: %s' %
                waiting)

        if self.email and email:
            message = '\n'.join(messages)
            self.mail(self.email, subject, message)

    def mail(self, email, subject, msg):
        body =  'To: %s\n' % self.email
        body += 'Subject: %s\n' % subject
        body += '\n'
        body += msg
        with os.popen(self.sendmail, 'w') as m:
            m.write(body)
        self.stderr.write('Mailed:\n\n%s' % body)
        self.mailed = body

    def restart(self, spec, write):
        namespec = make_namespec(spec['group'], spec['name'])
        if self.dry_run:
            write('dry-run mode active, faking %s restart' % namespec)
            return
        if spec['state'] is ProcessStates.RUNNING:
            if self.coredir and self.gcore:
                corename = os.path.join(self.coredir, namespec)
                cmd = self.gcore + ' "%s" %s' % (corename, spec['pid'])
                with os.popen(cmd) as m:
                    write('gcore output for %s:\n\n %s' % (
                        namespec, m.read()))
            write('%s is in RUNNING state, restarting' % namespec)
            # Try to make another GET to send response code message to app
            try:
                # We are working on a copy of params in order to update
                # the response status for this restart instance only
                params_copy = copy.copy(self.params)
                params_copy.update({'response_status': self.res_status})
                params = urllib.urlencode(params_copy, True)
                headers = {'User-Agent': 'httpok'}
                self.conn.request('GET', self.path + self.prefix + params,
                    headers=headers)
            except Exception as e:
                # We don't care whether the GET call succeeds here as we are
                # restarting the application anyway
                write('Exception during GET before restarting %s: %s' % (
                    namespec, e))
            if self.ext_service:
                try:
                    self.ext_service.stopProcess(namespec)
                except Exception as e:
                    write('Failed to stop process %s: %s' % (
                        namespec, e))
                try:
                    self.ext_service.startProcess(namespec)
                except Exception as e:
                    write('Failed to start process %s: %s' % (
                        namespec, e))
                else:
                    write('%s restarted' % namespec)
            else:
                try:
                    self.rpc.supervisor.stopProcess(namespec)
                except xmlrpclib.Fault as e:
                    write('Failed to stop process %s: %s' % (
                        namespec, e))
                except Exception as e:
                    self.log.logger.warning('Exception occurred while trying to '
                        'stop process %s due to %s', namespec, e)
                    return
                try:
                    self.rpc.supervisor.startProcess(namespec)
                except xmlrpclib.Fault as e:
                    write('Failed to start process %s: %s' % (
                        namespec, e))
                except Exception as e:
                    self.log.logger.warning('Exception occurred while trying to '
                        'start process %s due to %s', namespec, e)
                    return
                else:
                    write('%s restarted' % namespec)

            if self.capture_mode_stream:
                childutils.pcomm.send(str({
                    'processname': spec.get('name'),
                    'groupname': spec.get('groupname'),
                    'pid': spec.get('pid'),
                }), self.capture_mode_stream)

            if spec['name'] in self.counter:
                new_spec = self.rpc.supervisor.getProcessInfo(spec['name'])
                self.counter[spec['name']]['last_pid'] = new_spec['pid']
        else:
            write('%s not in RUNNING state, NOT restarting' % namespec)

    def restartCounter(self, spec, write):
        """
        Function to check if number of restarts exceeds the configured
        restart_threshold and last restart time does not exceed
        restart_timespan. It will stop letting self.act() from restarting
        the program unless it is restarted externally, e.g. manually by a human

        :param spec: Spec as returned by RPC
        :type spec: dict struct
        :param write: Stderr write handler and a message container
        :type write: function
        :returns: Boolean result whether to continue or not
        """
        if spec['name'] not in self.counter:
            # Create a new counter and return True
            self.counter[spec['name']] = {}
            self.counter[spec['name']]['counter'] = 1
            self.counter[spec['name']]['last_pid'] = spec['pid']
            self.counter[spec['name']]['restart_time'] = time.time()
            write('%s restart is approved' % spec['name'])
            return True
        elif self.restart_threshold == 0:
            # Continue if we don't limit the number of restarts
            self.counter[spec['name']]['counter'] += 1
            self.counter[spec['name']]['restart_time'] = time.time()
            write('%s in restart loop, attempt: %s' % (spec['name'],
                self.counter[spec['name']]['counter']))
            return True
        else:
            if self.counter[spec['name']]['counter'] < self.restart_threshold:
                self.counter[spec['name']]['counter'] += 1
                write('%s restart attempt: %s' % (spec['name'],
                    self.counter[spec['name']]['counter']))
                return True
            # Do not let httpok restart the program
            else:
                write('Not restarting %s anymore. Restarted %s times' % (
                    spec['name'], self.counter[spec['name']]['counter']))
                return False

    def errorCounter(self, spec, write):
        """
        Function checks the number of errors for the given spec.
        If it does not exceed self.grace_count - ignore the error and increase
        the counter for the spec.

        :param spec: Spec as returned by RPC
        :type spec: dict struct
        :param write: Stderr write handler and a message container
        :type write: function
        :returns: Boolean result whether to ignore the error or not
        """
        if self.grace_count == 0:
            # grace counter is not configured
            return True
        elif self.error_counter[spec['name']] <= self.grace_count:
            write('error count for %s is %s' % (spec['name'],
                self.error_counter[spec['name']]))
            self.error_counter[spec['name']] += 1
            return False
        else:
            write('error count for %s is %s' % (spec['name'],
                self.error_counter[spec['name']]))
            self.error_counter[spec['name']] = 0
            return True


    def cleanCounters(self):
        """
        Function to clean the counter once all monitored programs are
        running properly and successfully respond to GET requests. It won't
        clean the counter if self.restart_timespan hasn't been passed
        """
        for spec in self.counter.keys():
            if ((time.time() - self.counter[spec]['restart_time']) >
                    self.restart_timespan):
                self.counter[spec]['restart_time'] = time.time()
                self.counter[spec]['counter'] = 0


def main(argv=sys.argv):
    import getopt
    short_args="hp:at:c:b:B:s:m:g:d:eEr:n:x:G:C:o:D"
    long_args=[
        "help",
        "program=",
        "any",
        "timeout=",
        "code=",
        "body=",
        "restart-string=",
        "sendmail_program=",
        "email=",
        "gcore=",
        "coredir=",
        "eager",
        "not-eager",
        "restart-threshold=",
        "restart-timespan=",
        "external-service-script=",
        "grace-period=",
        "capture-mode=",
        "grace-count=",
        "dry-run",
        ]
    arguments = argv[1:]
    try:
        opts, args = getopt.getopt(arguments, short_args, long_args)
    except:
        usage()

    if not args:
        usage()
    if len(args) > 1:
        usage()

    programs = []
    any = False
    sendmail = '/usr/sbin/sendmail -t -i'
    gcore = '/usr/bin/gcore -o'
    coredir = None
    eager = True
    email = None
    timeout = 10
    retry_time = 10
    status = '200'
    inbody = None
    restart_string = []
    restart_threshold = 3
    restart_timespan = 60
    external_service_script = None
    grace_period = 0
    capture_mode_stream = None
    grace_count = 0
    dry_run = False

    for option, value in opts:

        if option in ('-h', '--help'):
            usage()

        if option in ('-p', '--program'):
            programs.append(value)

        if option in ('-a', '--any'):
            any = True

        if option in ('-s', '--sendmail_program'):
            sendmail = value

        if option in ('-m', '--email'):
            email = value

        if option in ('-t', '--timeout'):
            timeout = int(value)

        if option in ('-c', '--code'):
            status = value

        if option in ('-b', '--body'):
            inbody = value

        if option in ('-B', '--restart-string'):
            restart_string.append(value)

        if option in ('-g', '--gcore'):
            gcore = value

        if option in ('-d', '--coredir'):
            coredir = value

        if option in ('-e', '--eager'):
            eager = True

        if option in ('-E', '--not-eager'):
            eager = False

        if option in ('-r', '--restart-threshold'):
            try:
                restart_threshold = int(value)
            except ValueError:
                sys.stderr.write('Restart threshold should be a number\n')
                sys.stderr.flush()
                return

        if option in ('-n', '--restart-timespan'):
            try:
                restart_timespan = int(value)
            except ValueError:
                sys.stderr.write('Restart timespan should be a number\n')
                sys.stderr.flush()
                return

        if option in ('-x', '--external-service-script'):
            external_service_script = value

        if option in ('-G', '--grace-period'):
            grace_period = int(value)

        if option in ('-C', '--capture-mode'):
            if value in ['stdout', 'stderr']:
                capture_mode_stream = value.strip()
            else:
                capture_mode_stream = None
                sys.stderr.write('Unable to parse a valid argument.\n')
                sys.stderr.flush()
                return

        if option in ('-o', '--grace-count'):
            grace_count = int(value)

        if option in ('-D', '--dry-run'):
            dry_run = True

    url = arguments[-1]

    try:
        rpc = childutils.getRPCInterface(os.environ)
        if external_service_script:
            # Instantiate an ExternalService class to call the given script
            ext_service = ExternalService(external_service_script)
        else:
            ext_service = None
    except KeyError as e:
        if e.args[0] != 'SUPERVISOR_SERVER_URL':
            raise
        sys.stderr.write('httpok must be run as a supervisor event '
                         'listener\n')
        sys.stderr.flush()
        return
    except OSError as e:
        sys.stderr.write('os error occurred: %s\n' % e)
        sys.stderr.flush()
        return

    prog = HTTPOk(rpc, programs, any, url, timeout, status, inbody, email,
                  sendmail, coredir, gcore, eager, retry_time,
                  restart_threshold, restart_timespan, ext_service,
                  restart_string, grace_period, grace_count,
                  capture_mode_stream, dry_run)
    prog.runforever()

if __name__ == '__main__':
    main()
