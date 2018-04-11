#!/usr/bin/env python2
import math
import os
import re
import subprocess
import sys
import glob

def commandNew(argv, stdin, stdout, stderr):
    return (argv, stdin, stdout, stderr)

def getRundir(specdir, configname, benchmark):
    maxmtime = 0
    rundir = None
    for d in glob.glob("%s/benchspec/CPU2006/%s/run/run_base_ref_%s.*" % (specdir, benchmark, configname)):
        mtime = os.path.getmtime(d)
        if mtime > maxmtime:
            maxmtime = mtime
            rundir = d
    assert rundir
    return rundir

def commandGetArgv(command):
    return command[0]

def commandGetStdin(command):
    return command[1]

def getCommandFromLine(commands, line):
    stdin = "/dev/null"
    stderr = "/dev/null"
    stdout = "/dev/null"

    parts = line.split(" ")
    index = 0
    while index + 1 < len(parts) and len(parts[index]) > 1 and parts[index].startswith("-"):
        if parts[index] == "-C":
            return
        elif parts[index] == "-e":
            stderr = parts[index + 1]
        elif parts[index] == "-i":
            stdin = parts[index + 1]
        elif parts[index] == "-o":
            stdout = parts[index + 1]
        else:
            break
        index += 2

    if index >= len(parts):
        return

    commands.append(commandNew(parts[index:], stdin, stdout, stderr))

def getCommands(rundir):
    commands = list()
    commandsFilePath = "%s/speccmds.cmd" % (rundir)
    f = open(commandsFilePath, "r")
    for line in f:
        getCommandFromLine(commands, line.strip())
    f.close()
    return commands

def printfile(path, outfile):
    infile = open(path, "r")
    for line in infile:
        outfile.write("%s" % (line))
    infile.close()

def processbenchmark(specdir, configname, benchmark):
    rundir = getRundir(specdir, configname, benchmark)
    sys.stdout.write("run directory: %s\n" % (rundir))
    os.chdir(rundir)

    temppath = "%s/timeoutput.%d.tmp" % (rundir, os.getpid())

    commands = getCommands(rundir)
    for command in commands:
        timeCommand = list()
        timeCommand.insert(0, " ".join(commandGetArgv(command)))
        timeCommand.insert(0, "/usr/bin/time")
        timeCommand.insert(1, "-vo")
        timeCommand.insert(2, temppath)
        timeCommand.insert(3, "/bin/bash")
        timeCommand.insert(4, "-c")
        sys.stdout.write("running command: %s\n" % (" ".join(timeCommand)))
        filestdin = open(commandGetStdin(command), "r")
        filestdout = open("/dev/null", "w")
        exitcode = subprocess.call(timeCommand, stdin=filestdin, stdout=filestdout)
        filestdout.close()
        filestdin.close()
        printfile(temppath, sys.stdout)
        os.remove(temppath)
        sys.stdout.write("command status: %d\n" % (exitcode))

if len(sys.argv) < 4:
    sys.stdout.write("usage:\n")
    sys.stdout.write("  measuremem.py specdir configname benchmark...\n")

print 'hi there!'
specdir=sys.argv[1]
configname=sys.argv[2]
for benchmark in sys.argv[3:]:
    processbenchmark(specdir, configname, benchmark)
