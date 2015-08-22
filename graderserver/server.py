#!/usr/bin/env python2
# -*- coding: utf-8 -*-

# Copyright (c) 2015 France-IOI, MIT license
#
# http://opensource.org/licenses/MIT

# This script starts a server, fetching jobs from the graderqueue and sending
# them to the taskgrader.
# See https://github.com/France-ioi/graderqueue .


import argparse, json, logging, os, socket, sys, subprocess, threading
import urllib, urllib2, urllib2_ssl
from config import *


def listenWakeup(ev):
    """Listening loop: listen on TCP, set the event ev each time we get a
    wake-up signal."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((CFG_WAKEUP_IP, CFG_WAKEUP_PORT))
    logging.info('Started listening for wake-up signals on udp://%s:%s' % (CFG_WAKEUP_IP, CFG_WAKEUP_PORT))

    while True:
        (data, addr) = sock.recvfrom(1024)
        # TODO :: Replace this by a real authentication
        logging.debug('Wakeup listener received `%s` from %s' % (data, addr))
        if data == 'wakeup':
            logging.info('Received valid wake-up signal.')
            sock.sendto('ok', addr)
            ev.set()
        else:
            logging.info('Received invalid wake-up signal.')
            sock.sendto('no', addr)


if __name__ == '__main__':
    # Read command line options
    argParser = argparse.ArgumentParser(description="Launches an evaluation server for use with the graderQueue.")

    argParser.add_argument('-d', '--debug', help='Shows all the JSON data in and out (implies -v)', action='store_true')
    argParser.add_argument('-D', '--daemon', help='Daemonize the process (incompatible with -v)', action='store_true')
    argParser.add_argument('-l', '--listen', help='Listen on UDP and wait for a wake-up signal', action='store_true')
    argParser.add_argument('-L', '--logfile', help='Write logs into file LOGFILE', action='store', metavar='LOGFILE')
    argParser.add_argument('-s', '--server', help='Server mode; start only if not already started (implies -D)', action='store_true')
    argParser.add_argument('-t', '--test', help='Test communication with the graderqueue (exits after testing)', action='store_true')
    argParser.add_argument('-v', '--verbose', help='Be more verbose', action='store_true')

    args = argParser.parse_args()

    # Some options imply others
    args.daemon = args.daemon or args.server
    args.verbose = args.verbose or args.debug

    # Add configuration from config.py
    if CFG_LOGFILE and not args.logfile:
        args.logfile = CFG_LOGFILE

    # Set logging options
    logLevel = getattr(logging, CFG_LOGLEVEL, logging.CRITICAL)
    if args.debug: logLevel = min(logLevel, logging.DEBUG)
    if args.verbose: logLevel = min(logLevel, logging.INFO)

    logConfig = {'level': logLevel,
        'format': '%(asctime)s - graderserver - %(levelname)s - %(message)s'}
    if args.logfile: logConfig['filename'] = args.logfile
    logging.basicConfig(**logConfig)

    if args.logfile and args.verbose:
        # Also show messages on stderr
        logStderr = logging.StreamHandler()
        logStderr.setFormatter(logging.Formatter('%(asctime)s - graderserver - %(levelname)s - %(message)s'))
        logging.getLogger().addHandler(logStderr)

    # HTTPS layer
    opener = urllib2.build_opener(urllib2_ssl.HTTPSHandler(
            key_file=CFG_SSL_KEY,
            cert_file=CFG_SSL_CERT,
            ca_certs=CFG_SSL_CA,
            checker=CFG_SSL_CHECKER))

    # Test mode: try communicating with the graderqueue
    if args.test:
        print "Testing connection with the graderqueue at URL `%s`..." % CFG_GRADERQUEUE_TEST
        r = opener.open(CFG_GRADERQUEUE_TEST).read()

        logging.debug("Received: %s" % r)

        try:
            jsondata = json.loads(r)
        except:
            print "Error: received invalid JSON data. Test failed."
            sys.exit(1)

        if jsondata['errorcode'] == 0:
            print "Test successful, received answer: (#%d) %s" % (jsondata['errorcode'], jsondata['errormsg'])
            sys.exit(0)
        else:
            print "Test failed, received answer: (#%d) %s" % (jsondata['errorcode'], jsondata['errormsg'])
            sys.exit(1)


    if args.daemon and args.verbose:
        logging.critical("Can't daemonize while verbose mode is enabled.")
        argParser.print_help()
        sys.exit(1)


    if args.server:
        # Launch only if not already started
        try:
            pid = int(open(CFG_SERVER_PIDFILE, 'r').read())
        except:
            pid = 0
        if pid > 0:
            try:
                os.kill(pid, 0)
            except OSError as err:
                if err.errno == 1:
                    print "Server exists as another user. Exiting."
                    sys.exit(1)
            else:
                print "Server already launched. Exiting."
                sys.exit(1)

    if args.daemon:
        # Daemonize
        if os.fork() > 0:
            sys.exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.chdir("/")
        os.setsid()
        os.umask(0)
        if os.fork() > 0:
            sys.exit(0)

    if args.server:
        # Write new PID
        open(CFG_SERVER_PIDFILE, 'w').write(str(os.getpid()))

    if args.listen:
        # Launch a thread to listen on the UDP port
        # The Event allows to tell when a wakeup signal has been received
        wakeupEvent = threading.Event()

        wakeupThread = threading.Thread(target=listenWakeup, kwargs={'ev': wakeupEvent})
        wakeupThread.setDaemon(True)
        wakeupThread.start()

    while(True):
        # Main polling loop
        # Will terminate after a poll without any available job or an error

        # Request data from the graderqueue
        logging.info('Polling the graderqueue at `%s`...' % CFG_GRADERQUEUE_POLL)
        r = opener.open(CFG_GRADERQUEUE_POLL).read()
        try:
            jsondata = json.loads(r)
        except:
            logging.critical('Error: Taskqueue returned non-JSON data.')
            logging.debug('Received: %s' % r)
            sys.exit(1)

        if not jsondata.has_key('errorcode'):
            logging.critical('Error: Taskqueue returned data without errorcode.')
            logging.debug('Received: %s' % r)
            sys.exit(1)

        # Handle various possible errors
        if jsondata['errorcode'] == 1:
            logging.info('Taskqueue has no available job.')

            if args.listen:
                # Wait for a wake-up signal
                wakeupEvent.clear()
                while not wakeupEvent.wait(1):
                    # We use a timeout to keep the main thread responsive to interruptions
                    pass
                logging.info('Received wake-up signal.')
                continue
            elif args.server:
                # Poll again
                logging.info('Waiting 1 second before new poll...')
                time.sleep(1)
                continue
            else:
                # We didn't receive any job, exit
                logging.info("No job available, exiting.")
                break

        elif jsondata['errorcode'] == 2:
            logging.critical('Error: Taskqueue returned an error (%s)' % jsondata['errormsg'])
            sys.exit(1)
        elif jsondata['errorcode'] == 3:
            logging.critical('Error: Authentication failed (%s)' % jsondata['errormsg'])
            sys.exit(1)
        elif jsondata['errorcode'] != 0:
            logging.critical('Error: Taskqueue returned an unknown errorcode (%s): %s' % (jsondata['errorcode'], jsondata['errormsg']))
            sys.exit(1)
        elif not (jsondata.has_key('jobdata') and jsondata.has_key('jobname') and jsondata.has_key('jobid')):
            logging.critical('Error: Taskqueue returned no jobdata.')
            sys.exit(1)

        jobdata = jsondata['jobdata']
        logging.info('Received job `%s` (#%d)' % (jsondata['jobname'], jsondata['jobid']))

        jobdata['rootPath'] = CFG_GRADERQUEUE_ROOT
        if jobdata.has_key('restrictToPaths'):
            jobdata['restrictToPaths'] = map(lambda p: Template(p).safe_substitute(CFG_GRADERQUEUE_VARS), jobdata['restrictToPaths'])
            jobdata['restrictToPaths'].extend(CFG_SERVER_RESTRICT)
        elif CFG_SERVER_RESTRICT:
            jobdata['restrictToPaths'] = CFG_SERVER_RESTRICT

        logging.debug('JSON sent to taskgrader: ```\n%s\n```' %json.dumps(jobdata))
    
        # Send to taskgrader
        proc = subprocess.Popen(['/usr/bin/python2', CFG_TASKGRADER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (procOut, procErr) = proc.communicate(input=json.dumps(jobdata))
        logging.debug('* Output from taskgrader:')
        logging.debug('stdout: ```\n%s\n```' % procOut)
        logging.debug('stderr: ```\n%s\n```' % procErr)

        # Read taskgrader output
        try:
            evalJson = json.loads(procOut)
        except:
            evalJson = None

        if evalJson:
            logging.info("Execution successful.")
            for execution in evalJson['executions']:
                logging.debug(' * Execution %s:' % execution['name'])
                for report in execution['testsReports']:
                    if report.has_key('checker'):
                        # Everything was executed
                        logging.info('Solution executed successfully.')
                        logging.debug(report['checker']['stdout']['data'])
                    elif report.has_key('execution'):
                        # Solution error
                        logging.info('Solution returned an error.')
                        logging.debug(json.dumps(report['execution']))
                    else:
                        # Sanitizer error
                        logging.info('Test rejected by sanitizer.')
                        logging.debug(json.dumps(report['sanitizer']))
            if args.debug:
                logging.debug('* Full report:')
                logging.debug(json.dumps(evalJson))

            # Send back results
            resp = opener.open(CFG_GRADERQUEUE_SEND, data=urllib.urlencode(
                    {'jobid': jsondata['jobid'],
                     'resultdata': json.dumps({'errorcode': 0, 'jobdata': evalJson})})).read()

            logging.info("Sent results.")
        else:
            logging.info("Taskgrader error.")

            resp = opener.open(CFG_GRADERQUEUE_SEND, data=urllib.urlencode(
                    {'jobid': jsondata['jobid'],
                     'resultdata': json.dumps({'errorcode': 2, 'errormsg': "stdout:\n%s\nstderr:\n%s" % (procOut, procErr)})})).read()

        try:
            respjson = json.loads(resp)
            logging.info("Taskqueue response: (%d) %s" % (respjson['errorcode'], respjson['errormsg']))
        except:
            logging.critical("Error: Taskqueue answered results with invalid data (%s)" % resp)
            sys.exit(1)
