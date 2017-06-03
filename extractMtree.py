#!/usr/bin/env python
#
# vim: set ts=4 sw=4 expandtab :

# Copyright (c) 2017 Timothy Savannah - All Rights Reserved
#   This code is licensed under the terms of the APACHE license version 2.0
#
#  extractMtree.py - Extracts the mtree from all available packages,
#    attempting short-reads where possible to limit bandwidth,
#    and assemble a compressed json database for use by whatprovides_upstream
#
# IMPORTANT:
#
#  PLEASE - ENSURE THAT YOU HAVE AT LEAST 6 MIRRORS UNCOMMENTED IN
#    /etc/pacman.d/mirrorlist .
#
#  You can run with less, but you will be prompted, unless you specify
#   an alternate number of threads (see --help)
#
#
#  REPO Friendly -
#    The MTREE is at the start of the tar archive, and is almost always found
#     within the first 200K. We try to download just the first 200K of each package first,
#     which will be a total of 1.9G split amongst the mirrors, using 6 mirrors (default),
#     means each mirror will be hit by, on average, just 300MB in order to build one of these files.
#
#   Of course, a few files are in different-than-stard tar formats (Why? Someone is using an alternate
#    than GNU tar and submitting packages, methinks. Or maybe is has something to do with an optional
#    GPG signing that works different on some signed packages than others... either way, for these
#    snall handful of packages, we have to download the full archive package.
#
#   Also, by default, only UPDATES will be processed. The existing database is scanned and imported,
#    and the versions compared against the latest versions. If a mismatch, default (can be slightly expanded, see #usage)
#     is if the version is lower, then download and refresh the list. Can be changed to just "if different" (needed sometimes,
#     like if pkg version was a git commit and then changed to a version, it could be seen as having decreased version)
#
#   This means only the FIRST generation will average 300MB load to repos, and subseqant updates (I generally update
#     once a week which equals about 300 package updates) 
#      yield only 60MB total, ** average 6MB per repo! **
#
#   This means that the repos should get less traffic from this application than from my normal package update traffic
#
#  IMPORTANT NOTE:
#
#    Even though, as mentioned the load added to repos is about the size of a -Syu operation from a signle client for first generation,
#      PLEASE
#      PLEASE
#      PLEASE
#            Don't generate your own providesDB unless you absolutely HAVE to. And if you do, PLEASE download my provided
#            database and update from that.
#            This will shrink your bandwidth requirements to, on average, from 300MB per repo, to 10MB per repo.
#
#   I don't want to have this tool add extra load to the already generous folks who provide
#    the mirrors


# TODO: In major need of a refactor. There's a lot of code, but I really want to
#   keep this as a standalone module.. So maybe just split it into some classes
#   to make it neater.

import copy
import gzip
import os
import json
import random
import re
import subprocess
import sys
import tarfile
import tempfile
import traceback
import time
import gc

from io import BytesIO

try:
    import func_timeout
    from func_timeout.dafunc import raise_exception
    from func_timeout.StoppableThread import StoppableThread
except ImportError:
    sys.stderr.write('ERROR: Cannot import python module func_timeout - not installed?\n')
    sys.exit(1)

try:
    from cmp_version import VersionString

    canCompareVersions = True
except ImportError:
    sys.stderr.write('WARNING: Cannot import python module cmp_version - not installed?\nWARNING: Cannot compare versions. Will assume all != versions are >.\n')
    canCompareVersions = False

try:
    PermissionError
except NameError:
    PermissionError = IOError


# I know... a lot of globals.
#  TODO: Refactor

global LATEST_FILE_FORMAT
LATEST_FILE_FORMAT = '0.2'

global SUPPORTED_DATABASE_VERSIONS
SUPPORTED_DATABASE_VERSIONS = ('0.1', '0.2')

global EXTRACT_MTREE_VERSION
EXTRACT_MTREE_VERSION = '0.3.0'

global PROVIDES_DB_LOCATION
PROVIDES_DB_LOCATION = "/var/lib/pacman/.providesDB"

global isVerbose
isVerbose = False

global isSuperVerbose
isSuperVerbose = False

global SUBPROCESS_BUFSIZE
SUBPROCESS_BUFSIZE = 1024 * 500 # 500K Bufsize

global SHORT_FETCH_SIZE
SHORT_FETCH_SIZE = 1024 * 200 # Try to fetch first 200K to find MTREE

# USE_ARCH - Package arch to use. TODO: Allow others
global USE_ARCH
USE_ARCH = "x86_64" 

global MAX_THREADS
MAX_THREADS = 6

global SHORT_TIMEOUT
SHORT_TIMEOUT = 15
global LONG_TIMEOUT
LONG_TIMEOUT = ( 60 * 8 )

# Max extra urls added to each thread.
#  Normally, a repo is assigned to a thread, but if any are extra
#  up to this many will be made available to each thread.
MAX_EXTRA_URLS = 3

global ALL_STR_TYPES

ALL_STR_TYPES = [ str, bytes ]
try:
    unicode
    ALL_STR_TYPES.append(unicode)
except:
    unicode = str

ALL_STR_TYPES = tuple(ALL_STR_TYPES)

class FailedToConvertDatabaseException(ValueError):
    pass

def convertOldDatabase(oldVersion, data):
    global LATEST_FILE_FORMAT
    global ALL_STR_TYPES

    if oldVersion == '0.1':
        for key in list(data.keys()):
            if key == '__vers': # Not in version 0.1, but whatever..
                continue

            oldData = data[key]
            if issubclass(oldData.__class__, ALL_STR_TYPES):
                # If string, was an error
                newData = { 
                    'files'   : [],   # No files
                    'version' : '',   # Unknown version
                    'error'   : oldData, # Error string
                }
                data[key] = newData
            elif issubclass(oldData.__class__, (list, tuple)): # Should be list, but test tuple too for some reason..
                newData = {
                    'files'    : copy.copy(oldData), # Copy the data ( list of files ). Probably not required to copy, but simplifies GC
                    'version'  : '',                 # Unknown version
                    'error'    : None,               # No error
                }
                data[key] = newData
            else:
                raise FailedToConvertDatabaseException('Failed to convert old data (version %s) to latest version: %s' %(oldVersion, LATEST_FILE_FORMAT))

    data['__vers'] = LATEST_FILE_FORMAT

    # No return - data modified inline

def writeDatabase(results):
    global PROVIDES_DB_LOCATION

    compressed = gzip.compress( json.dumps(results).encode('utf-8') )


    try:
        with open(PROVIDES_DB_LOCATION, 'wb') as f:
            f.write( compressed )
    except PermissionError as permError:
        tempFile = tempfile.NamedTemporaryFile(mode='wb', delete=False)
        sys.stderr.write('Failed to open "%s" for writing ( %s ). Dumping to tempfile: "%s"\n' %(PROVIDES_DB_LOCATION, str(permError), tempFile.name, ))
        tempFile.write( compressed )
        tempFile.close()

    # Force this now - it's big!
    del compressed
    gc.collect()

# Try to use shared memory slot, if available
global USE_TEMP_DIR
if os.path.exists('/dev/shm'):
    USE_TEMP_DIR = '/dev/shm'
else:
    USE_TEMP_DIR = tempfile.gettmpdir()

def decompressDataSubprocess(data, cmd):
    global SUBPROCESS_BUFSIZE
    global USE_TEMP_DIR

    fte = None
    tempFile = None

    devnull = open(os.devnull, 'w')

    # Short delay to ensure everything is init'd and ready to go
    time.sleep(.002)
    try:


        tempFile = tempfile.NamedTemporaryFile(mode='w+b', buffering=SUBPROCESS_BUFSIZE, dir=USE_TEMP_DIR, prefix='mtree_', delete=True)
        tempFile.write(data)
        tempFile.flush()
        tempFile.seek(0)

        pipe = subprocess.Popen(cmd, shell=False, stdin=tempFile, stdout=subprocess.PIPE, stderr=devnull, close_fds=True, bufsize=SUBPROCESS_BUFSIZE)
        time.sleep(.01)

        result = pipe.stdout.read()
        nextResult = True
        while True:
            time.sleep(.005)
            nextResult = pipe.stdout.read()
            if nextResult != b'':
                result += nextResult
            else:
                break

        pipe.wait()

    except func_timeout.FunctionTimedOut as _fte:
        result = None
        fte = _fte
        if pipe.poll() is None:
            try:
                pipe.terminate()
            except:
                pass
            time.sleep(.2)
            if pipe.poll() is None:
                try:
                    pipe.kill()
                except:
                    pass
            pipe.wait()

    devnull.close()
    if tempFile is not None:
        try:
            tempFile.close()
        except:
            pass

    if fte:
        raise_exception( [fte] )

    return result

def decompressZlib(data):
    # -dnc - Decompress to stdout and don't worry about file
    return decompressDataSubprocess(data, ['/usr/bin/gzip', '-dnc'])

def decompressXz(data):
    # -dc - Decompress to stdout
    return decompressDataSubprocess(data, ['/usr/bin/xz', '-dc'])


def getSize(header):

    # Expected locations in tar header of size. Very variable because of multiple extensions, etc
    #  These are used in the "short read" path. Upon failure, the full tar will be downloaded
    #  and the "tar" module used (which supports more format versions)
    SIZE_IDX_START = 124
    SIZE_IDX_END = 124 + 12

    trySection = header[SIZE_IDX_START : SIZE_IDX_END]
    return int(trySection, 8)

    return int(trySection, 8)

def getFiles(dataStr):
    lines = dataStr.split('\n')

    rePat = re.compile('^\.(?P<filename>.+) time')
    ret = []

    for line in lines:
        if not line or line[0] != '.':
            continue
        matchObj = rePat.match(line)
        if not matchObj:
            # Uh oh..
            continue
        ret.append(matchObj.groupdict()['filename'])

    return ret

def fetchFromUrl(url, numBytes):

    global isSuperVerbose

    useStderr = None

    if not isSuperVerbose:
        extraArgs = ['--silent']
        useStderr = open(os.devnull, 'w')
    else:
        extraArgs = []

    pipe = subprocess.Popen(["/usr/bin/curl", '-k'] + extraArgs + [url],  shell=False, stdout=subprocess.PIPE, stderr=useStderr)

    if numBytes:
        urlContents = pipe.stdout.read(numBytes)
        pipe.stdout.close()
    else:
        urlContents = pipe.stdout.read()

    ret = pipe.wait()

    if useStderr is not None:
        useStderr.close()

    if b'404 Not Found' in urlContents and '-x86_64' in url:
        return fetchFromUrl(url.replace('-x86_64', '-any'), numBytes)

    return urlContents

def getAllPackages():

    devnull = open(os.devnull, 'w')

    pipe = subprocess.Popen(["/usr/bin/pacman", "-Sl"], shell=False, stdout=subprocess.PIPE, stderr=devnull)

    contents = pipe.stdout.read()

    pipe.wait()

    devnull.close()

    return [tuple(x.split(' ')[0:3]) for x in contents.decode('utf-8').split('\n') if x and ' ' in x]



def getRepoUrls():
    nextLine = True
    repos = []

    repoRE = re.compile('^[ \t]*[sS]erver[ \t]*=[ \t]*(?P<repo_url>[^ \t#]+)[ \t]*([#].*){0,1}$')

    with open('/etc/pacman.d/mirrorlist', 'rt') as f:
        nextLine = f.readline()
        while nextLine != '':
            matchObj = repoRE.match(nextLine.strip())
            if matchObj:
                groupDict = matchObj.groupdict()
                if groupDict['repo_url']:
                    ret = groupDict['repo_url'].replace('$repo', '%s').replace('$arch', USE_ARCH)
                    while ret.endswith('/'):
                        ret = ret[:-1]

                    ret += '/%s'
                    repos.append(ret)

            nextLine = f.readline()

    if not repos:
        raise Exception('Failed to find repo URL from /etc/pacman.d/mirrorlist. Are any not commented?')
    return repos


def shuffleLst(lst):
    if not lst:
        return list()

    lstCopy = lst[:]
    ret = []

    while len(lstCopy) > 1:
        ret.append( lstCopy.pop( random.randint(0, len(lstCopy)-1) ) )

    ret.append( lstCopy.pop() )

    return ret


class RefObj(object):

    def __init__(self, ref):
        self.ref = ref

    def __call__(self):
        return self.ref

#REPO_URL = "http://mirrors.acm.wpi.edu/archlinux/%s/os/x86_64/%s"
#REPO_URLS = [ "http://mirrors.acm.wpi.edu/archlinux/%s/os/x86_64/%s" ]

class RetryWithFullTarException(Exception):
    pass

def getFileData(filename, decodeWith=None):
    with open(filename, 'rb') as f:
        contents = f.read()

    if decodeWith:
        contents = contents.decode(decodeWith)
    return contents


def createThreads(splitBy, allPackages, repoUrls, resultsRef, failedPackages):
    global SHORT_TIMEOUT
    global MAX_EXTRA_URLS
    global isVerbose

    threads = []
    numPackages = len(allPackages)
    if splitBy > 1:
        # Split up for threads with primary repo being the Nth repo, and any extra repos not
        #  assigned to a thread get appended as extras. At the bottom we will single-thread
        #  in error mode with all repos and a super-long timeout.
        splitPackages = []
        numPerEach = numPackages // splitBy
        for i in range(splitBy):
            if i == splitBy - 1:
                splitPackages.append( allPackages[ (numPerEach * i) : ] )
            else:
                splitPackages.append( allPackages[ (numPerEach * (i)) : (numPerEach * (i+1)) ] )

        print ( "Starting %d threads...\n" %(splitBy,))
        for i in range(splitBy):
            packageSet = splitPackages[i]
            if isVerbose:
                print ( "Thread %d primary repo: %s" %(i, repoUrls[i]) )
            myRepoUrls = [ repoUrls[i] ] + repoUrls[splitBy : splitBy + MAX_EXTRA_URLS]
            thisThread = StoppableThread(target=runThroughPackages, args=(packageSet, resultsRef, failedPackages, myRepoUrls), kwargs={'timeout' : SHORT_TIMEOUT})

            thisThread.start()
            threads.append(thisThread)
            time.sleep(.35) # Offset the threads a bit

    return threads

def prompt(promptMsg, allowedResults=None, tryAgainMsg=None):
    '''
        prompt - Prompt the user for input.
            Optionally, ensure that the input matches a series of "allowed inputs" (e.x. "y" or "n")
             and repeat the prompt message until valid input is provided.

        @param promptMsg <str> - Message to print prior to user input.
            Likely you want this not to contain a newline, line:   'continue? (y/n): '

        @param allowedResults <None/list/tuple/lambda> Default None, 

           If list/tuple:
             if value does not evaluate to False, 
             #promptMsg will be repeated until input is provided which falls into a member of #allowedResults

           If lambda / callable (as __call__ ) - Will be called with the value, return True if allowed, False otherwise.

              MAYBE FUTURE TODO:  If a string is returned, it will be used in lieu of the user's input (like if you want to automatically
                convert to uppercase).

        @param tryAgainMsg <None/str/lambda> Default None, If provided, each time the input frmo user
            does not fall within the #allowedResults, this message will be printed before
            prompting again. 

            IF string: This message is used. If '%s' is contained, every occurance will be replaced with the user's input.
            IF lambda (or anything implementing __call__, i.e. methods, classes, etc:
              Passed the user input. Must return a string to be output. For example: Suggest close field, note a removed field, etc

            This is automatically appended with a newline, generally I find it best to
              prefix with a newline and end with a newline (so final result is 2 end newlines)
              to ensure visibility.

            Example:   tryAgainMsg="\nInvalid response: '%s'\n"

        @return <str> - Input from user
            If #allowedResults was provided, is guarenteed to be a member of that list
    '''

    if not allowedResults:
        checkResult = lambda x : True
    else:
        allowedResultsIsCallable = False
        try:
            allowedResults.__call__
            allowedResultsIsCallable = True
            checkResult = allowedResults
        except:
            pass

        if not allowedResultsIsCallable:

            if not issubclass(allowedResults.__class__, (list, tuple, set)):
                try:    
                    reprVal = repr(allowedResults)
                except Exception as erEx:
                    reprVal = '[ Exception < %s > \"%s\" fetching repr. ]' %( erEx.__class__.__name__, str(erEx) ) 
                raise ValueError('allowedResults argument must be a subclass of list or tuple, or callable ( __call__ ). Got: < %s >  %s' %( allowedResults.__class__.__name__, reprVal ) )

            checkResult = lambda x : x in allowedResults

    # Explicitly set "hasTryAgain", since we allow anything with a __call__, 
    #   something may have a __bool__ which calls a database field or who knows what,
    #   so cache that result and ensure we are just testing a simple value
    if tryAgainMsg:
        tryAgainMsgIsCallable = False
        hasTryAgain = True
        try:
            tryAgainMsg.__call__
            tryAgainMsgIsCallable = True
        except:
            tryAgainMsgIsCallable = False

        if tryAgainMsgIsCallable is False:
            if not issubclass(tryAgainMsg.__class__, (str, unicode)):
                raise ValueError('tryAgainMsg is not False, but is also not a callable (has __call__ method), nor a string. Is a: ' + str(tryAgainMsg.__class__.__name__) )
    else:
        hasTryAgain = False


    sys.stdout.write(promptMsg)
    sys.stdout.flush()

    # Strip the trailing newline from readline()
    result = sys.stdin.readline()[:-1]
    while not checkResult(result):
        thisMsg = ''
        if hasTryAgain is True:

            thisMsg = None
            if tryAgainMsgIsCallable is False:
                # Instead of using a format string, use a replace.
                #  Since only one variable is being subbed, this allows them
                #   to use it multiple times.
                if '%s' in tryAgainMsg:
                    thisMsg = tryAgainMsg.replace('%s', result)
                else:
                    # Strings passed by value, no need to copy
                    thisMsg = tryAgainMsg
            else:
                thisMsg = tryAgainMsg( result )

        print ( thisMsg )

        sys.stdout.write(promptMsg)
        sys.stdout.flush()

        result = sys.stdin.readline()[:-1]

    return result


def printUsage():
    sys.stderr.write('''Usage: extractMtree.py (options)
  Downloads and extracts the file list from the repo.

    Options:

       --single-thread           Use one thread.
       --threads=N               Use N threads (Max at number of repos)
       --convert                 ONLY convert the old database to the new version

       --force-old-update        Force update on different versions, even if older

       -v                        Verbose (lots of extra output, default is very little)
       -vv                       Super Verbose - will show super verbose info
                                  (e.x. progress bars for curl)
                                 This can get VERY messy if threads > 1

      --version                  Print application version, supported database versions, and exit
      --help                     Show this message and exit

''')

if __name__ == '__main__':
    # NOTE: This uses a LOT of memory, so we delete and garbage collect
    #  manually (since everything in main thread is in one scope, it will
    #  rarely, if ever, automatically trigger.



    ################################
    ######## HANDLE ARGUMENTS
    #############################
    convertOnly = False

    forceOldUpdate = False

    args = sys.argv[1:]

    if '--help' in args or '-h' in args:
        printUsage()
        sys.exit(0)

    if '--version' in args:
        sys.stderr.write('extractMtree version %s by Timothy Savannah.\nDatabase version: %s\nSupported database versions: %s\n\n' % (
            EXTRACT_MTREE_VERSION, LATEST_FILE_FORMAT, ', '.join(SUPPORTED_DATABASE_VERSIONS))
        )
        sys.exit(0)


    # setNumThreads - Used to track if multiple thread arguments were provided
    setNumThreads = False

    superVerboseRE = re.compile('^[-][v][v]+$')

    if '--single-thread' in args:
        MAX_THREADS = 1
        setNumThreads = True
        args.remove('--single-thread')

    for arg in args[:]:
        if arg in ('-v', '--verbose'):
            isVerbose = True
            args.remove(arg)
        elif arg.startswith('--threads='):
            try:
                MAX_THREADS = int(arg[ len('--threads=') : ])
            except:
                sys.stderr.write('Number of threads must be a digit! Problem with arg:   "%s"\n\n' %(arg, ))
                sys.exit(1)
            if setNumThreads is True and MAX_THREADS != 1:
                sys.stderr.write('Defined both a > 1 number of threads AND --single-thread. Pick one.\n\n')
                sys.exit(1)
            args.remove(arg)
        elif arg == '--convert':
            convertOnly = True
            args.remove(arg)
        elif arg == '--force-old-update':
            forceOldUpdate = True
            args.remove(arg)
        elif superVerboseRE.match(arg):
            isSuperVerbose = True
            args.remove(arg)



    if len(args) != 0:
        sys.stderr.write('Unknown arguments: %s\n' %(str(args), ))
        sys.exit(1)

    ##############################################
    ######## READ PACKAGE LIST AND OLD DB
    ########################################
#    allPackages = [ ('core', 'binutils', '2.28.0-2') ]
    allPackages = getAllPackages()

    results = {}
    resultsRef = RefObj(results)


    sys.stdout.write('Read %d total packages.\n' %( len(allPackages), ))

    priorDBContents = None
    try:
        with open(PROVIDES_DB_LOCATION, 'rb') as f:
            priorDBContents = f.read()
    except:
        sys.stderr.write('WARNING: Cannot read old Provides DB at "%s". Will query every package (instead of just updates)\n' %(PROVIDES_DB_LOCATION, ))

    if priorDBContents:
        priorDBContents = gzip.decompress(priorDBContents)

        try:
            oldResults = json.loads(priorDBContents)
            sys.stdout.write('Read %d records from old database. Trimming non-updates...\n' %(len(oldResults), ))

            if '__vers' in oldResults:
                oldVersion = oldResults.pop('__vers')
            else:
                # TEMP: Assume for now old version is 0.1 because it did not have a __vers marker.
                #   TODO: In a later version of extractedMtree, remove this assumption
                oldVersion = '0.1'

            if oldVersion not in SUPPORTED_DATABASE_VERSIONS:
                raise FailedToConvertDatabaseException('Unsupported database version: ' + oldVersion)

            # Try to convert old database to new format
            convertOldDatabase(oldVersion, oldResults)

            if convertOnly:
                if oldVersion == LATEST_FILE_FORMAT:
                    sys.stderr.write('No need to update, already at latest version.\n')
                    sys.exit(0)

                try:
                    writeDatabase(oldResults)
                except Exception as e:
                    exc_info = sys.exc_info()
                    sys.stderr.write('Failed to write database.  %s:  %s\n\n' %(e.__class__.__name__, str(e)))
                    traceback.print_exception(*exc_info)
                    sys.stderr.write('\n')
                    sys.exit(4)
                sys.stdout.write('Successfully updated database.\n')
                sys.exit(0)



            newPackages = []
            for packageInfo in allPackages:
                pkgName = packageInfo[1]
                pkgVersion = packageInfo[2]

                if pkgName not in oldResults:
                    # New package
                    newPackages.append(packageInfo)
                    continue

                if oldResults[pkgName]['version'] == pkgVersion:
                    results[pkgName] = oldResults[pkgName]
                else:
                    if not canCompareVersions:
                        newPackages.append(packageInfo)
                    elif forceOldUpdate:
                        if isVerbose is True:
                            oldVersion = VersionString(oldResults[pkgName]['version'])
                            newVersion = VersionString(pkgVersion)
                            if newVersion < oldVersion:
                                sys.stderr.write('WARNING: Package %s - %s has an older version!  "%s"  < "%s" ! Did primary repo change to an older mirror? Doing anyway, because of --force-old-update\n' %(packageInfo[0], pkgName, str(oldVersion), str(newVersion)))

                        newPackages.append(packageInfo)
                    else:
                        oldVersion = VersionString(oldResults[pkgName]['version'])
                        newVersion = VersionString(pkgVersion)

                        if newVersion > oldVersion:
                            newPackages.append(packageInfo)
                        else:
                            sys.stderr.write('WARNING: Package %s - %s has an older version!  "%s"  < "%s" ! Did primary repo change to an older mirror? Skipping... (use --force-old-update to do anyway)\n' %(packageInfo[0], pkgName, str(oldVersion), str(newVersion)))



            allPackages = newPackages
            sys.stdout.write('\nTrimmed number of updates required to %d\n\n' %(len(allPackages), ))

            del oldResults
            del newPackages

        except Exception as e:
            sys.stderr.write('Error reading old database (will perform a full update):  %s:  %s\n' %( e.__class__.__name__, str(e)))
        finally:
            del priorDBContents
            gc.collect()


    if convertOnly: # end if priorDBContents
        sys.stderr.write('Asked to convert old database, but could not read successfully from "%s"\n' %(PROVIDES_DB_LOCATION, ))
        sys.exit(3)



    if not os.access(PROVIDES_DB_LOCATION, os.W_OK):
        sys.stdout.write('Cannot write to "%s". Will create temp file.\n' %((PROVIDES_DB_LOCATION, )) )
        result = False
        while result not in ('y', 'n'):
            sys.stdout.write('Continue? (y/n): ')
            sys.stdout.flush()
            result = sys.stdin.readline().strip().lower()

        if result == 'n':
            sys.exit(2)

    if os.getuid() != 0:
        sys.stderr.write('WARNING: Cannot refresh pacman database.\n')
    else:
        ret = subprocess.Popen(['/usr/bin/pacman', '-Sy'], shell=False).wait()
        if ret != 0:
            sys.stderr.write('WARNING: pacman -Sy returned non-zero: %d\n' %(ret,))


    if 'REPO_URLS' in locals():
        print ( "USING PREDEFINED REPO")
        repoUrls = REPO_URLS
    else:
        repoUrls = getRepoUrls()

    print ( "Using repos from /etc/pacman.d/mirrorlist:\n\t%s\n" %(repoUrls, ))

    ##############################################
    ######## doOne - Do a single package
    ########################################
    def doOne(repoName, packageName, versionInfo, resultsRef, repoUrl, fetchedData=None, useTarMod=False):
        global isVerbose
        global SHORT_FETCH_SIZE

        if isVerbose and useTarMod is True:
            print ( "Using full fetch and tar module for %s - %s" %(repoName, packageName) )
        results = resultsRef()

        if fetchedData is None:
            finalUrl = repoUrl %( repoName, packageName + "-" + versionInfo + "-x86_64.pkg.tar.xz" )
            if isVerbose:
                print ( "Fetching url: " + finalUrl )

            if useTarMod is False:
                maxSize = SHORT_FETCH_SIZE
            else:
                maxSize = None

            tarContents = fetchFromUrl(finalUrl, maxSize)
        else:
            finalUrl = '[cached data]'
            tarContents = fetchedData


        if len(tarContents) == 0:
            msg = 'Unable to fetch %s from: %s\n' %(packageName, finalUrl)
            raise Exception(msg)


        if useTarMod is False:
            data = decompressXz(tarContents[:SHORT_FETCH_SIZE])

            # Sometimes we don't find it, maybe format error, maybe didn't fetch
            #  enough (doTarMod will do a full fetch)
            try:
                mtreeIdx = data.index(b'.MTREE')
            except Exception as ex1:
                if isVerbose is True or useTarMod is False:
                    msg = "Could not find .MTREE in %s - %s - %s." %( repoName, packageName, versionInfo ) 
                if useTarMod is False:
                    msg += ' retrying with full fetch and tar mod.\n\n'
                    raise RetryWithFullTarException(msg)
                else:
                    if isVerbose is True:
                        msg += '\n\n'
                        sys.stderr.write(msg)
                    raise ex1


            headerStart = data[mtreeIdx:]

            try:
                mtreeSize = getSize(headerStart)
                compressedData = headerStart[512 : 512 + mtreeSize] # 512 is header size. 
            except Exception as ex2:
                # If we failed with the "short fetch", try again with full fetch and tar module
                if useTarMod is False:
                    return doOne(repoName, packageName, versionInfo, resultsRef, repoUrl, fetchedData, useTarMod=True)
                raise ex2

        else:
            # doTarMod is True
            data = decompressXz(tarContents)

            bio = BytesIO()
            bio.write(data)
            bio.seek(0)

            tf = tarfile.open(fileobj=bio)

            extractedMtreeFile = tf.extractfile('.MTREE')
            compressedData = extractedMtreeFile.read()
            try:
                extractedMtreeFile.close()
            except:
                pass


        mtreeData = decompressZlib(compressedData).decode('utf-8')

        files = getFiles(mtreeData)

        results[packageName] = { 'files' : files, 'version' : versionInfo, 'error' : None }
        if isVerbose:
            sys.stdout.write("Got %d files for %s.\n\n" %(len(files), packageName ))

    # END: doOne


#    if len(args) != 1:
#        fileData = getFileData(args)
#        doOne('core', 'binutils', '2.28.0-2', results, fileData)
#        sys.exit(0)

    failedPackages = []

    ###################################################
    ######## runThroughPackages -
    #########    Run through a list of packages
    #########     on a list of repos
    #############################################
    def runThroughPackages(allPackages, resultsRef, failedPackages, repoUrls, timeout=SHORT_TIMEOUT, longTimeout=LONG_TIMEOUT):
        # repoUrls - First is primary, others may or may not be used
        global isVerbose

        results = resultsRef()

        for repoName, packageName, versionInfo in allPackages:
            startTime = time.time()
            gc.collect()
            endTime = time.time()
            time.sleep(1.5 - (endTime - startTime))
            if isVerbose:
                sys.stdout.write("Processing %s - %s: %s" %(repoName, packageName, isVerbose and '\n' or '') )
                sys.stdout.flush()
            try:
                try:
                    func_timeout.func_timeout(timeout, doOne, (repoName, packageName, versionInfo, resultsRef, repoUrls[0]))
                except RetryWithFullTarException as retryException1:
                    if isVerbose:
                        sys.stderr.write( str(retryException1) )
                    print ( "\n\n\n1CALLING RETRY!!!\n\n\n" )
                    func_timeout.func_timeout(longTimeout, doOne, (repoName, packageName, versionInfo, resultsRef, repoUrls[0]), kwargs={'useTarMod' : True} )
                    print ( "\n\n\n1AFTER RETRY!!!\n\n\n" )
                except Exception as e:
                    if not isinstance(e, RetryWithFullTarException):
                        raise e

            except func_timeout.FunctionTimedOut as fte:
                try:
                    didIt = False
                    for nextRepoUrl in repoUrls[1:]:
                        try:
                            try:
                                func_timeout.func_timeout(timeout, doOne, (repoName, packageName, versionInfo, resultsRef, nextRepoUrl))
                            except RetryWithFullTarException as retryException1:
                                print ( "\n\n\nCALLING RETRY!!!\n\n\n" )
                                func_timeout.func_timeout(longTimeout, doOne, (repoName, packageName, versionInfo, resultsRef, nextRepoUrl), kwargs = {'useTarMod' : True } )
                                print ( "\n\n\n2AFTER RETRY!!!\n\n\n" )
                            except Exception as e:
                                if not isinstance(e, RetryWithFullTarException):
                                    raise e
                            didIt = True
                            break
                        except func_timeout.FunctionTimedOut:
                            pass
                        except KeyboardInterrupt as ke:
                            raise ke
                        except Exception as e:
                            try:
                                exc_info = sys.exc_info()
                                failedPackages.append ( (repoName, packageName, versionInfo) )
                                errStr = 'Error processing %s - %s : < %s >: %s\n\n' %(repoName, packageName, e.__class__.__name__, str(e))
                                sys.stderr.write(errStr)
                                if isVerbose:
                                    traceback.print_exception(*exc_info)
                                results[packageName] = { 'files' : [], 'version' : versionInfo, 'error' : errStr }
                            except:
                                pass

                    if didIt is False:
                        failedPackages.append ( (repoName, packageName, versionInfo) )
                        errStr = 'Error TIMEOUT processing %s - %s : FunctionTimedOut\n\n' %(repoName, packageName )
                        sys.stderr.write(errStr)
                        results[packageName] = { 'files' : [], 'version' : versionInfo, 'error' : errStr }
                except:
                    pass
            except KeyboardInterrupt as ke:
                raise ke
            except Exception as e:
                if isVerbose:
                    exc_info = sys.exc_info()
                    traceback.print_exception(*exc_info)
                try:
                    failedPackages.append ( (repoName, packageName, versionInfo) )
                    errStr = 'Error processing %s - %s : < %s >: %s\n\n' %(repoName, packageName, e.__class__.__name__, str(e))
                    sys.stderr.write(errStr)
                    results[packageName] = { 'files' : [], 'version' : versionInfo, 'error' : errStr }
                except:
                    pass

        #END: def runThroughPackages


    ##############################################
    ######## Start up threads and
    ########   begin processing
    ########################################
    numRepos = splitBy = len(repoUrls)
    if splitBy == 0:
        sys.stderr.write('No uncommented repos in /etc/pacman.d/mirrorlist !\n\n')
        sys.exit(1)

    if numRepos < MAX_THREADS:
        sys.stdout.write('WARNING: Number of available repos [ %d ] is less than the configured number' %(numRepos, ) +\
            ' of threads [%d].\nRecommended to uncomment more repos. See --help for changing nubmer of threads.\n\n' %(MAX_THREADS, ))

        shrinkThreads = prompt("\nLimit threads to %d and continue? (y/n): " %(numRepos, ), ('y', 'Y', 'n', 'N'))
        if shrinkThreads in ('n', 'N'):
            sys.stderr.write('\nAborting based on user input.\n\n')
            sys.exit(1)

        MAX_THREADS = splitBy
    elif numRepos > MAX_THREADS:
        # They have more repos defined
        splitBy = MAX_THREADS

    numPackages = len(allPackages)

    threads = []
    if splitBy > 1:
        # Split up for threads with primary repo being the Nth repo, and any extra repos not
        #  assigned to a thread get appended as extras. At the bottom we will single-thread
        #  in error mode with all repos and a super-long timeout.
        threads = createThreads(splitBy, allPackages, repoUrls, resultsRef, failedPackages)
    else:
        try:
            runThroughPackages(allPackages, resultsRef, failedPackages, repoUrls, timeout=SHORT_TIMEOUT)
        except KeyboardInterrupt as ke:
            sys.stderr.write ( "\n\nCAUGHT KEYBOARD INTERRUPT, QUITTING...\n\n")
            sys.stderr.flush()
            sys.exit(32)

    if threads:
        try:
            for thread in threads:
                thread.join()
        except KeyboardInterrupt as ke:
            sys.stderr.write ( "\n\nCAUGHT KEYBOARD INTERRUPT, CLOSING DOWN THREADS...\n\n")
            sys.stderr.flush()
            # Raise keyboard interrupt in each thread to make them terminate
            for thread in threads:
                thread._stopThread(ke)

            # Try real quick to cleanup, they are daemon threads so they will be
            #   terminated at end of program forcibly
            for thread in threads:
                thread.join(.05)

            sys.exit(32)

    del allPackages
    gc.collect()

    ##############################################
    ######## If packages failed, wait a bit,
    ########  refresh database, and try again
    ########################################
    try: # TODO: REMOVE ME 


        if failedPackages:
            sys.stderr.write("Need to retry %d packages. Resting for a minute....\n\n" %(len(failedPackages), ))
            time.sleep(60)

            if MAX_THREADS > 1:
                # Shuffle up the order to try different mirrors
                failedPackages = shuffleLst(failedPackages)

            if os.getuid() != 0:
                sys.stderr.write('WARNING: Cannot refresh pacman database.\n')
            else:
                ret = subprocess.Popen(['/usr/bin/pacman', '-Sy'], shell=False).wait()
                if ret != 0:
                    sys.stderr.write('WARNING: pacman -Sy returned non-zero: %d\n' %(ret,))

            # Now give up to 8 minutes for each package to complete. This covers if a PAX_FORMAT on a huge file,
            #   slow mirror, etc.
            newFailedPackages = []

            if splitBy > 1:
                threads = createThreads(splitBy, failedPackages, repoUrls, resultsRef, newFailedPackages)
                try:
                    for thread in threads:
                        thread.join()
                except KeyboardInterrupt as ke:
                    sys.stderr.write ( "\n\nCAUGHT KEYBOARD INTERRUPT, CLOSING DOWN THREADS...\n\n")
                    sys.stderr.flush()
                    # Raise keyboard interrupt in each thread to make them terminate
                    for thread in threads:
                        thread._stopThread(ke)

                    # Try real quick to cleanup, they are daemon threads so they will be
                    #   terminated at end of program forcibly
                    for thread in threads:
                        thread.join(.05)

                    sys.exit(32)
            else:
                try:
                    runThroughPackages( failedPackages, resultsRef, newFailedPackages, repoUrls, timeout=LONG_TIMEOUT)
                except KeyboardInterrupt as ke:
                    pass

            del failedPackages
            gc.collect()

            ##################################################
            ######## If still failures,
            ########  refresh and check if newer version
            ########  and retry those pkgs with newer version
            ##########################################
            if newFailedPackages:
                sys.stderr.write('After completing, still %d failed packages.\nFailed after retry: %s\n\n' %(len(newFailedPackages), '\n'.join(['\t[%s] %s-%s  \t%s' %(failedP[0], failedP[1], failedP[2], results[failedP[1]]['error'] ) for failedP in newFailedPackages]) ) ) 

                if os.getuid() != 0:
                    sys.stderr.write('WARNING: Cannot refresh pacman database.\n')
                else:
                    ret = subprocess.Popen(['/usr/bin/pacman', '-Sy'], shell=False).wait()
                    if ret != 0:
                        sys.stderr.write('WARNING: pacman -Sy returned non-zero: %d\n' %(ret,))


#                    oldVersions = {}
#                    for packageInfo in newFailedPackages:
#                        oldVersions[packageInfo[1]] = packageInfo[2]
                    oldVersions = { packageInfo[1] : packageInfo[2] for packageInfo in newFailedPackages }

                    newPackages = getAllPackages()

                    # Get a list of any packages that have updated since we started, and retry them
                    updatedPackages = [packageInfo for packageInfo in newPackages if packageInfo[1] in oldVersions and oldVersions[packageInfo[1]] != packageInfo[2]]


                    # Will try every mirror
                    stillFailedPackages = []
                    try:
                        runThroughPackages( updatedPackages, resultsRef, stillFailedPackages, repoUrls, timeout=LONG_TIMEOUT)
                    except:
                        pass

                    # Append the failed packages we didn't retry
                    stillFailedPackages += [packageInfo for packageInfo in newFailedPackages if packageInfo not in stillFailedPackages]

                    if stillFailedPackages:
                        sys.stderr.write('EVEN after refreshing package database, the following packages are total failures:\n\n%s\n\n' %( str([failedP[1] for failedP in stillFailedPackages]), ))



                    gc.collect()

            # END: if newFailedPackages


        ##############################################
        ######## Write resulting database to file
        ########################################
        results['__vers'] = LATEST_FILE_FORMAT


        writeDatabase(results)

        pass
        #import pdb; pdb.set_trace()
        print ( "\n\nSuccess.\nDatabase size: %d\n" %(len(results), ))
#        print ( str(locals().keys()) )
        pass
        pass
        pass
        pass
        pass

    except Exception as e:
        #import pdb; pdb.set_trace()
        exc_info = sys.exc_info()
        traceback.print_exception(*exc_info)
        pass
        pass
        pass
        pass
        pass
        pass
        pass

# vim: set ts=4 sw=4 expandtab :
