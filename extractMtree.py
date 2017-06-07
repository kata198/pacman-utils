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


import copy
import errno
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

__version__ = '0.6.0'
__version_tuple__ = (0, 6, 0)

####################
### Constants
################

global LATEST_FILE_FORMAT
LATEST_FILE_FORMAT = '0.2'

SUPPORTED_DATABASE_VERSIONS = ('0.1', '0.2')

global PROVIDES_DB_LOCATION
PROVIDES_DB_LOCATION = "/var/lib/pacman/.providesDB"


# USE_ARCH - Package arch to use. TODO: Allow others
#   NOTE: If this arch is not found, "any" will be tried
global USE_ARCH
USE_ARCH = "x86_64" 


####################
### Tuneables
################


DEFAULT_SUBPROCESS_BUFSIZE = 1024 * 500 # 500K Bufsize

DEFAULT_SHORT_FETCH_SIZE = 1024 * 200 # Try to fetch first 200K to find MTREE


# MAX_THREADS - Max number of threads
MAX_THREADS = 6

# SHORT_TIMEOUT/LONG_TIMEOUT - Timeouts for short read and full read, in seconds
SHORT_TIMEOUT = 15
LONG_TIMEOUT = ( 60 * 8 )

# Max extra urls added to each thread.
#  Normally, a repo is assigned to a thread, but if any are extra
#  up to this many will be made available to each thread.
global MAX_EXTRA_URLS
MAX_EXTRA_URLS = 3

MAX_REPOS = MAX_THREADS + MAX_EXTRA_URLS


####################
### Definitions
################


if sys.version_info.major == 2:
    ALL_STR_TYPES = ( str, unicode )
    ALL_DECODED_STR_TYPES = ( str, unicode )
else:
    ALL_STR_TYPES = ( str, bytes )
    ALL_DECODED_STR_TYPES = ( str, )

isStrType = lambda arg : issubclass(arg.__class__, ALL_STR_TYPES)
isDecodedStrType = lambda arg : issubclass(arg.__class__, ALL_DECODED_STR_TYPES)



# Try to use shared memory slot, if available
global USE_TEMP_DIR
USE_TEMP_DIR = None
def getUseTempDir():
    '''
        getUseTempDir - Get the directory that should be used for
            short-term temp files.

          We prefer to use /dev/shm (directly in memory), if available.
          Otherwise, fall back to system temp dir ( /tmp )

        @return <str> Temporary directory name to use
    '''
    global USE_TEMP_DIR
    if USE_TEMP_DIR is None:
        if os.path.exists('/dev/shm') and os.access('/dev/shm', os.W_OK):
            USE_TEMP_DIR = '/dev/shm'
        else:
            USE_TEMP_DIR = tempfile.gettmpdir()

    return USE_TEMP_DIR

class FailedToConvertDatabaseException(ValueError):
    '''
        FailedToConvertDatabaseException - Exception raised when we try (but fail)
            to convert provides database from an old format to current format
    '''
    pass

def convertOldDatabase(oldVersion, data):
    '''
        convertOldDatabase - Convert providesDB from an older format to the current format

        @param oldVersion <str> - The old database format version

        @param data <dict> - The old database dict

        @raises - FailedToConvertDatabaseException if failure to convert
    '''
    global LATEST_FILE_FORMAT

    if oldVersion == LATEST_FILE_FORMAT:
        return

    if oldVersion == '0.1':
        for key in list(data.keys()):
            if key == '__vers': # Not in version 0.1, but whatever..
                continue

            oldData = data[key]
            if isStrType(oldData.__class__):
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
    else:
        raise FailedToConvertDatabaseException('Old database version "%s" is not supported for update.' %(oldVersion, ))

    data['__vers'] = LATEST_FILE_FORMAT

    # No return - data modified inline

def writeDatabase(results):
    '''
        writeDatabase - Writes the database to disk

          First, it will try to write to

          @param results <dict> - The dict to write

            MUST BE IN CURRENT DATABASE FORMAT!


         NOTE: Garbage collector runs at the end of this function.

         NOTE: If we fail to write to PROVIDES_DB_LOCATION, we will write to
           a tempfile which will be printed to stderr
    '''
    global PROVIDES_DB_LOCATION

    wroteTo = PROVIDES_DB_LOCATION

    compressed = gzip.compress( json.dumps(results).encode('utf-8') )


    try:
        with open(PROVIDES_DB_LOCATION, 'wb') as f:
            f.write( compressed )
    except Exception as exc:
        tempFile = tempfile.NamedTemporaryFile(mode='wb', delete=False)
        sys.stderr.write('\nFailed to open "%s" for writing ( %s ). Dumping to tempfile:\n%s\n' %(PROVIDES_DB_LOCATION, str(exc), tempFile.name, ))
        tempFile.write( compressed )
        tempFile.close()

        wroteTo = tempFile.name

    # Force this now - it's big!
    del compressed
    gc.collect()

    return wroteTo


def decompressDataSubprocess(data, cmd, bufSize=DEFAULT_SUBPROCESS_BUFSIZE):
    '''
        decompressDataSubprocess - Decompress given compressed #data, using
            a provided decompression command, #cmd

         Likely this will use /dev/shm as an intermediary (if available) @see getUseTempDir

         This is used in lieu of the builtin python modules for speed sake
            (several orders of magnitude faster)

        @param data <bytes> - Compressed data

        @param cmd  list<str> - Decompression command. Will NOT use a whell to launch,
            so first element should be a fully-resolved command, followed by
            args to decompress, read from stdin, write to stdout

        @param bufSize <int> default DEFAULT_SUBPROCESS_BUFSIZE - Number of bytes to use for buffers
        
        @return <bytes> - Decompressed data


        NOTE: If FunctionTimedOut is raised while this method is being called,
            such as if this is called from a func_timeout function or StoppableThread,
            it will clean up and then raise that exception after cleanup.

    '''

    useTempDir = getUseTempDir()

    fte = None
    tempFile = None

    devnull = open(os.devnull, 'w')

    # Short delay to ensure everything is init'd and ready to go
    time.sleep(.002)
    try:


        tempFile = tempfile.NamedTemporaryFile(mode='w+b', buffering=bufSize, dir=useTempDir, prefix='mtree_', delete=True)
        tempFile.write(data)
        tempFile.flush()
        tempFile.seek(0)

        pipe = subprocess.Popen(cmd, shell=False, stdin=tempFile, stdout=subprocess.PIPE, stderr=devnull, close_fds=True, bufsize=bufSize)
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

def decompressZlib(data, bufSize=DEFAULT_SUBPROCESS_BUFSIZE):
    '''
        decompressZlib - Decompress zlib/gz/DEFLATE data using external executable

          @see decompressDataSubprocess
        
        @param data <bytes> - Compressed data

        @param bufSize <int> default DEFAULT_SUBPROCESS_BUFSIZE - Number of bytes to use for buffer size

        @return data <bytes> - Decompressed data

    '''
    # -dnc - Decompress to stdout and don't worry about file
    return decompressDataSubprocess(data, ['/usr/bin/gzip', '-dnc'], bufSize)

def decompressXz(data, bufSize=DEFAULT_SUBPROCESS_BUFSIZE):
    '''
        decompressXz - Decompress lzma/xz data using external executable

          @see decompressDataSubprocess
        
        @param data <bytes> - Compressed data

        @param bufSize <int> default DEFAULT_SUBPROCESS_BUFSIZE - Number of bytes to use for buffer size

        @return data <bytes> - Decompressed data

    '''
    # -dnc - Decompress to stdout and don't worry about file
    # -dc - Decompress to stdout
    return decompressDataSubprocess(data, ['/usr/bin/xz', '-dc'], bufSize)


def getFileSizeFromTarHeader(header):
    '''
        getFileSizeFromTarHeader - Try to get the file size from a file's TAR header
        
          @param header <bytes/str> - The TAR header relating to the file of interest

          @return <int> - The size, in bytes, of the file stored in the TAR archive

          NOTE: This function is not fullproof. There are lots of extensions that
            modify the header and offsets, and this does not try to detect them all.

            It uses the most common GNU Tar extension that system "tar" command generates
          
          NOTE: If an exception is raised, this function should do a full fetch and
            use the "tar" module to support all extensions. This function is intended
            to be used in the "short-read" path.
          
          TODO: Extend to support more files
    '''

    # Expected locations in tar header of size. Very variable because of multiple extensions, etc
    #  These are used in the "short read" path. Upon failure, the full tar will be downloaded
    #  and the "tar" module used (which supports more format versions)
    SIZE_IDX_START = 124
    SIZE_IDX_END = 124 + 12

    trySection = header[SIZE_IDX_START : SIZE_IDX_END]

    # Size is octal
    return int(trySection, 8)

def getFilenamesFromMtree(mtreeContents):
    '''
        getFilenamesFromMtree - Extracts all the "provides" filenames from
          the package's .MTREE file.

        @param mtreeContents <str> - The .MTREE file extracted from archive

        @return list<str> - A list of filenames this package provides.
    '''
    lines = mtreeContents.split('\n')

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

def fetchFromUrl(url, numBytes, isSuperVerbose=False):
    '''
        fetchFromUrl - Fetches #numBytes bytes of data from a given #url

          @param url <str> - Url to fetch

          @param numBytes <None/int> - If None, fetch entire file.
             Otherwise, fetch first N bytes.
          
          @param isSuperVerbose <bool> default False, if True will print curl progress
            This will get messy if numThreads > 1
          
          @return <bytes> - File data

        NOTE: This function uses "curl" to best handle ftp vs http vs https

        NOTE: If "isSuperVerbose" is set to True, the curl progress will be output.
            This will get real ugly when number of threads are > 1
    '''


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
        return fetchFromUrl(url.replace('-x86_64', '-any'), numBytes, isSuperVerbose)

    return urlContents


def refreshPacmanDatabase():
    '''
        refreshPacmanDatabase - Refreshes the pacman package database ( -Sy )

          @return <bool> - True if successful, otherwise False

          NOTE: You must be root to refresh the database.
            You should be root running this script at all though...
    '''
    if os.getuid() != 0:
        sys.stderr.write('WARNING: Cannot refresh pacman database.\n')
        return False
    else:
        ret = subprocess.Popen(['/usr/bin/pacman', '-Sy'], shell=False).wait()
        if ret != 0:
            sys.stderr.write('WARNING: pacman -Sy returned non-zero: %d\n' %(ret,))
            return False

    return True

def getAllPackagesInfo():
    '''
        getAllPackagesInfo - Get the "info" for all packages.
            This includes repo, package name, package version

        @return list< tuple<str(repo), str(name), str(version)> > - The collected info
            for all packages in the repos
    '''
        

    devnull = open(os.devnull, 'w')

    pipe = subprocess.Popen(["/usr/bin/pacman", "-Sl"], shell=False, stdout=subprocess.PIPE, stderr=devnull)

    contents = pipe.stdout.read()

    pipe.wait()

    devnull.close()

    return [tuple(x.split(' ')[0:3]) for x in contents.decode('utf-8').split('\n') if x and ' ' in x]



def getRepoUrls(maxRepos=MAX_REPOS):
    '''
        getRepoUrls - Extract the repo urls from /etc/pacman.d/mirrorlist

          @return list<str> - A list of repos, with "%s" replacing $repo and $arch.
          

          TODO: This replace should happen in another function, in case $repo comes AFTER $arch
            No repos as far as I can tell do this, as they mirror a fixed format, but it IS possible
    '''
    global USE_ARCH

    if not maxRepos:
        gatheredEnough = lambda _repos : False
    else:
        gatheredEnough = lambda _repos : len(_repos) >= maxRepos

    nextLine = True
    repos = []

    repoRE = re.compile('^[ \t]*[sS]erver[ \t]*=[ \t]*(?P<repo_url>[^ \t#]+)[ \t]*([#].*){0,1}$')

    with open('/etc/pacman.d/mirrorlist', 'rt') as f:
        nextLine = f.readline()
        while nextLine != '' and not gatheredEnough(repos):
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
    '''
        shuffleLst - Randomly sort provided list

          @param lst list - List to be randomly sorted

          @return randomly sorted list

        NOTE: #lst is NOT modified
    '''
    if not lst:
        return list()

    lstCopy = lst[:]
    ret = []

    while len(lstCopy) > 1:
        ret.append( lstCopy.pop( random.randint(0, len(lstCopy)-1) ) )

    ret.append( lstCopy.pop() )

    return ret


class RefObj(object):
    '''
        RefObj - An object that holds a reference to another object

           Call to retrieve refrence, i.e.   myObj = myRefObj()
    '''

    def __init__(self, ref):
        '''
            __init__ - Create a refObj

              @param ref <object> - An object to hold reference to
        '''
        self.ref = ref

    def __call__(self):
        '''
            __call__ - Return the reference this object holds

            @return <object> - The object this RefObj is holding
        '''
        return self.ref

#REPO_URL = "http://mirrors.acm.wpi.edu/archlinux/%s/os/x86_64/%s"
#REPO_URLS = [ "http://mirrors.acm.wpi.edu/archlinux/%s/os/x86_64/%s" ]

class RetryWithFullTarException(Exception):
    '''
        RetryWithFullTarException - Exception raised when we failed to use
          a short-read of the tar, to indicate to retry with full fetch
          and tar module
    '''
    pass

def getFileData(filename, decodeWith=None):
    '''
        getFileData - Read and decode a filename

          @param filename <str> - Filename
          @param decodeWith <str/None> - codec to decode results in, or None to leave as bytse

          @return <bytes/str> - File data, either decoded with #decodeWith, or bytes if #decodeWith was None
    '''
    with open(filename, 'rb') as f:
        contents = f.read()

    if decodeWith:
        contents = contents.decode(decodeWith)
    return contents


class RunnerWorker(StoppableThread):
    '''
        RunnerWorker - A StoppableThread set to run a subset of packages.

            @see createThreads
    '''

    def __init__(self, doPackages, resultsRef, failedPackageInfos, repoUrls, shortFetchSize=DEFAULT_SHORT_FETCH_SIZE, timeout=SHORT_TIMEOUT, longTimeout=LONG_TIMEOUT, isVerbose=False, isSuperVerbose=False):
        '''
            __init__ - Create a "RunnerWorker" object

              @see createThreads

              @param doPackages list < tuple < str, str, str > > - A list of package infos this thread should process

              @param resultsRef RefObj < dict > - Reference to the global results 

              @param failedPackageInfos list - Global list where failed package infos should be appended

              @param repoUrls list<str> - A list of urls, ready to be used as a format string (contains two %s, "repo" and "arch").
                First is primary url

              @param shortFetchSize <int> default DEFAULT_SHORT_FETCH_SIZE - Number of bytes to fetch for a "short fetch"

              @param timeout <float> Default SHORT_TIMEOUT , The "short"/standard timeout period per package

              @param longTimeout <float> default LONG_TIMEOUT - The "long"/retry timeout period per package

              @param isVerbose <bool> default False - Whether to be verbose or not

              @param isSuperVerbose <bool> default False - Whether to be "super verbose"

        '''
        StoppableThread.__init__(self)


        self.doPackages = doPackages
        self.resultsRef = resultsRef
        self.failedPackageInfos = failedPackageInfos
        self.repoUrls = repoUrls
        self.timeout = timeout
        self.longTimeout = longTimeout
        self.shortFetchSize = shortFetchSize
        self.isVerbose = isVerbose
        self.isSuperVerbose = isSuperVerbose

    ##############################################
    ######## doOne - Do a single package
    ########################################
    def doOne(self, repoName, packageName, packageVersion, repoUrl, fetchedData=None, useTarMod=False):
        '''
            doOne - Do a single package. This is an internal function.
                Use RunnerWorker.run instead.

                @param repoName <str> - Repo name to use

                @param packageName <str> - Package name to fetch

                @param packageVersion <str> - The package version

                @param repoUrl <str> - Repo url to try

                @param fetchedData <None/bytes> default None - Data that has already been fetched, or None to do fetch

                @param useTarMod <bool> default False - Whether to do a full fetch and use tar module. If False,
                    will be a "short fetch"

                NOTES:
                    
                    * May call itself with useTarMod=True if originally useTarMod=False but short-read failed
        '''
        
        isVerbose = self.isVerbose
        shortFetchSize = self.shortFetchSize
        resultsRef = self.resultsRef

        if isVerbose and useTarMod is True:
            print ( "Using full fetch and tar module for %s - %s" %(repoName, packageName) )
        results = resultsRef()

        if fetchedData is None:
            finalUrl = repoUrl %( repoName, packageName + "-" + packageVersion + "-x86_64.pkg.tar.xz" )
            if isVerbose:
                print ( "Fetching url: " + finalUrl )

            if useTarMod is False:
                maxSize = shortFetchSize
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
            data = decompressXz(tarContents[:shortFetchSize])

            # Sometimes we don't find it, maybe format error, maybe didn't fetch
            #  enough (doTarMod will do a full fetch)
            try:
                # Try an rindex, as some tar's have an extra section which also contains filenames
                mtreeIdx = data.rindex(b'.MTREE')
            except Exception as ex1:
                if isVerbose is True or useTarMod is False:
                    msg = "Could not find .MTREE in %s - %s - %s." %( repoName, packageName, packageVersion ) 
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
                mtreeSize = getFileSizeFromTarHeader(headerStart)
                compressedData = headerStart[512 : 512 + mtreeSize] # 512 is header size. 
            except Exception as ex2:
                # If we failed with the "short fetch", try again with full fetch and tar module
                if useTarMod is False:
                    return self.doOne(repoName, packageName, packageVersion, repoUrl, fetchedData, useTarMod=True)
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

        files = getFilenamesFromMtree(mtreeData)

        results[packageName] = { 'files' : files, 'version' : packageVersion, 'error' : None }
        if isVerbose:
            sys.stdout.write("Got %d files for %s.\n\n" %(len(files), packageName ))

    # END: doOne
    
    ###################################################
    ######## run -
    #########    Run through a list of packages
    #########     on a list of repos
    #############################################
    def run(self):
        '''
            run - Thread main. Runs through a list of packages on a list of repos.

                May be called standalone (i.e. not via thread.start() ) for non-threaded run.

                Uses args from init -
                    doPackages
                    resultsRef
                    failedPackageInfos
                    repoUrls
                    timeout
                    longTimeout
                    isVerbose
        '''
        # repoUrls - First is primary, others may or may not be used

        doPackages = self.doPackages
        resultsRef = self.resultsRef
        failedPackageInfos = self.failedPackageInfos
        repoUrls = self.repoUrls
        timeout = self.timeout
        longTimeout = self.longTimeout
        
        isVerbose = self.isVerbose

        results = resultsRef()

        for repoName, packageName, packageVersion in doPackages:
            startTime = time.time()
            gc.collect()
            endTime = time.time()
            time.sleep(1.5 - (endTime - startTime))
            if isVerbose:
                sys.stdout.write("Processing %s - %s: %s" %(repoName, packageName, isVerbose and '\n' or '') )
                sys.stdout.flush()
            try:
                # Try to run a short fetch with short timeout
                try:
                    func_timeout.func_timeout(timeout, self.doOne, (repoName, packageName, packageVersion, repoUrls[0]))
                except RetryWithFullTarException as retryException1:
                    # If RetryWithFullTarException is raised, we could not parse the tar file,
                    #   so retry with a full read and long timeout
                    if isVerbose:
                        sys.stderr.write( str(retryException1) )

                    func_timeout.func_timeout(longTimeout, self.doOne, (repoName, packageName, packageVersion, repoUrls[0]), kwargs={'useTarMod' : True} )

                except Exception as e:
                    if not isinstance(e, RetryWithFullTarException):
                        raise e

            except func_timeout.FunctionTimedOut as fte:
                # We timed out. Try with any extra repos provided
                try:
                    didIt = False
                    isPackageMarkedFailed = False

                    for nextRepoUrl in repoUrls[1:]:
                        try:
                            try:
                                func_timeout.func_timeout(timeout, self.doOne, (repoName, packageName, packageVersion, nextRepoUrl))
                            except RetryWithFullTarException as retryException1:
                                # Failed short-fetch, try again with full fetch

                                func_timeout.func_timeout(longTimeout, self.doOne, (repoName, packageName, packageVersion, nextRepoUrl), kwargs = {'useTarMod' : True } )

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
                            # Unknown exception, mark as failed and set "error" string
                            isPackageMarkedFailed = True
                            try:
                                exc_info = sys.exc_info()
                                failedPackageInfos.append ( (repoName, packageName, packageVersion) )
                                errStr = 'Error processing %s - %s : < %s >: %s\n\n' %(repoName, packageName, e.__class__.__name__, str(e))
                                sys.stderr.write(errStr)
                                if isVerbose:
                                    traceback.print_exception(*exc_info)
                                results[packageName] = { 'files' : [], 'version' : packageVersion, 'error' : errStr }
                            except:
                                isPackageMarkedFailed = False
                                pass

                    if didIt is False and isPackageMarkedFailed is False:
                        # We failed, and have not already marked as failed.
                        failedPackageInfos.append ( (repoName, packageName, packageVersion) )
                        errStr = 'Error TIMEOUT processing %s - %s : FunctionTimedOut\n\n' %(repoName, packageName )
                        sys.stderr.write(errStr)
                        results[packageName] = { 'files' : [], 'version' : packageVersion, 'error' : errStr }
                except:
                    pass
            except KeyboardInterrupt as ke:
                raise ke
            except Exception as e:
                if isVerbose:
                    exc_info = sys.exc_info()
                    traceback.print_exception(*exc_info)
                try:
                    failedPackageInfos.append ( (repoName, packageName, packageVersion) )
                    errStr = 'Error processing %s - %s : < %s >: %s\n\n' %(repoName, packageName, e.__class__.__name__, str(e))
                    sys.stderr.write(errStr)
                    results[packageName] = { 'files' : [], 'version' : packageVersion, 'error' : errStr }
                except:
                    pass

        #END: def runThroughPackages


class Runner(object):
    
    def __init__(self, numThreads, allPackageInfos, repoUrls, resultsRef, failedPackageInfos, shortFetchSize=DEFAULT_SHORT_FETCH_SIZE, timeout=SHORT_TIMEOUT, longTimeout=LONG_TIMEOUT, isVerbose=False, isSuperVerbose=False):
        '''
            __init__ - Create a Runner. If numThreads > 1, will run as threads. Otherwise,
                        will run inline in current process.

                @param numThreads <int> - Number of threads to start

                @param allPackageInfos list< tuple< str, str, str > > - List of package infos
                    (from getAllPackagesInfo )

                @param repoUrls list<str> - A list of repos to use.
                    Length must be >= numThreads

                @param resultsRef RefObj<dict> - RefObj to the "results" dict

                @param shortFetchSize <int> default DEFAULT_SHORT_FETCH_SIZE - Number of bytes to fetch for a "short fetch"

                @param failedPackageInfos list - A list used to store failed package infos

                @param timeout <float> Default SHORT_TIMEOUT , The "short"/standard timeout period per package

                @param longTimeout <float> default LONG_TIMEOUT - The "long"/retry timeout period per package

                @param isVerbose <bool> default False, if True, will print more verbose output

                @param isSuperVerbose <bool> default False, if True will print super verbose output

                NOTE: Call .run to begin execution
        '''

        self.numThreads = numThreads
        self.allPackageInfos = allPackageInfos
        self.repoUrls = repoUrls
        self.resultsRef = resultsRef
        self.failedPackageInfos = failedPackageInfos
        self.shortFetchSize = shortFetchSize
        self.timeout = timeout
        self.longTimeout = longTimeout
        self.isVerbose = isVerbose
        self.isSuperVerbose = isSuperVerbose

        self.threads = self._createThreads()

    def run(self):
        if self.numThreads > 1:
            self._startThreads()

            didComplete = self._joinThreads()
            if not didComplete:
                sys.exit( errno.EPIPE )
        else:
            try:
                self.threads[0].run()
            except KeyboardInterrupt:
                sys.exit( errno.EPIPE )

    def _createThreads(self):
        '''
            createThreads - Create threads to process package info.

                @param numThreads <int> - Number of threads to start

                @param allPackageInfos list< tuple< str, str, str > > - List of package infos
                    (from getAllPackagesInfo )

                @param repoUrls list<str> - A list of repos to use.
                    Length must be >= numThreads

                @param resultsRef RefObj<dict> - RefObj to the "results" dict

                @param failedPackageInfos list - A list used to store failed package infos


                @return list < StoppableThread > - A list of StoppableThreads set to process
                    a split subset of #allPackageInfos

                NOTES:
                    
                    * For each thread, N, it will use #repoUrls[N] as its "primary" repo.
                        If there are enough repos available, up to #MAX_EXTRA_URLS starting
                          at #repoUrls[ numThreads + 1 ] will be allocated to each thread
                          as "backup" urls

                    * The threads created by this method have not been started.
                        Use "startThreads" to start them.

        '''
        global MAX_EXTRA_URLS


        numThreads = self.numThreads
        allPackageInfos = self.allPackageInfos
        repoUrls = self.repoUrls
        resultsRef = self.resultsRef
        failedPackageInfos = self.failedPackageInfos
        shortFetchSize = self.shortFetchSize

        timeout = self.timeout
        longTimeout = self.longTimeout
        isVerbose = self.isVerbose
        isSuperVerbose = self.isSuperVerbose


        threads = []

        if numThreads > 1:
            numPackages = len(allPackageInfos)

            if numPackages < numThreads:
                if isVerbose:
                    print ( "Less packages than threads! Shrinking number of threads to %d.." %(numThreads, ))
                numThreads = numPackages
            # Split up for threads with primary repo being the Nth repo, and any extra repos not
            #  assigned to a thread get appended as extras. At the bottom we will single-thread
            #  in error mode with all repos and a super-long timeout.
            splitPackages = []
            numPerEach = numPackages // numThreads
            for i in range(numThreads):
                if i == numThreads - 1:
                    splitPackages.append( allPackageInfos[ (numPerEach * i) : ] )
                else:
                    splitPackages.append( allPackageInfos[ (numPerEach * (i)) : (numPerEach * (i+1)) ] )

            if numThreads > 1:
                print ( "Starting %d threads...\n" %(numThreads,))

            for i in range(numThreads):
                packageSet = splitPackages[i]
                if isVerbose:
                    print ( "Thread %d primary repo: %s" %(i, repoUrls[i]) )
                myRepoUrls = [ repoUrls[i] ] + repoUrls[numThreads : numThreads + MAX_EXTRA_URLS]

                thisThread = RunnerWorker(packageSet, resultsRef, failedPackageInfos, myRepoUrls, shortFetchSize=shortFetchSize, timeout=timeout, longTimeout=longTimeout, isVerbose=isVerbose, isSuperVerbose=isSuperVerbose)
                threads.append(thisThread)
            else:
                
                thisThread = RunnerWorker(allPackageInfos, resultsRef, failedPackageInfos, repoUrls, shortFetchSize=shortFetchSize, timeout=timeout, longTimeout=longTimeout, isVerbose=isVerbose, isSuperVerbose=isSuperVerbose)
                threads.append(thisThread)


        return threads

    def _startThreads(self, startOffset=.35):
        '''
            startThreads - Start a list of threads, with a given offset between starts.
                The offset is meant to balance out the network bandwidth requirements,
                 to ensure that we are maxing bandwidth as much as possible.
                With short reads, threads would otherwise all be doing network I/O followed
                  by local I/O at the same time.

              @param threads list<threading.Thread> - Threads to start

              @param startOffset <float> - Offset between thread starts

        '''
        for thisThread in self.threads:
            thisThread.start()
            time.sleep(startOffset)


    def _joinThreads(self):
        '''
            joinThreads - Wait for all threads to complete, 
                            or shut them down gracefully following control+c

              @param threads list<threading.Thread> - Threads to join

              @return <bool> - True if threads were joined successfully,
                                False if KeyboardInterrupt caused them to be
                                stopped
              
              TODO: Handle sigterm as well, same as KeyboardInterrupt
        '''
        threads = self.threads

        try:
            for thread in threads:
                thread.join()

            return True
        except KeyboardInterrupt as ke:
            sys.stderr.write ( "\n\nCAUGHT KEYBOARD INTERRUPT, CLOSING DOWN THREADS...\n\n")
            sys.stderr.flush()
            # Raise keyboard interrupt in each thread to make them terminate
            for thread in threads:
                thread._stopThread(KeyboardInterrupt)

            # Try real quick to cleanup, they are daemon threads so they will be
            #   terminated at end of program forcibly if required
            time.sleep(.1)

            for thread in threads:
                thread.join(.05)

            return False


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
            if not isDecodedStrType(tryAgainMsg):
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
    isVerbose = False
    isSuperVerbose = False

    forceOldUpdate = False

    args = sys.argv[1:]

    if '--help' in args or '-h' in args:
        printUsage()
        sys.exit(0)

    if '--version' in args:
        sys.stderr.write('extractMtree version %s by Timothy Savannah.\nDatabase version: %s\nSupported database versions: %s\n\n' % (
            __version__, LATEST_FILE_FORMAT, ', '.join(SUPPORTED_DATABASE_VERSIONS))
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
            isVerbose = True
            isSuperVerbose = True
            args.remove(arg)



    if len(args) != 0:
        sys.stderr.write('Unknown arguments: %s\n' %(str(args), ))
        sys.exit(1)

    ##############################################
    ######## READ PACKAGE LIST AND OLD DB
    ########################################

    if not convertOnly:
        refreshPacmanDatabase()

#    allPackageInfos = [ ('core', 'binutils', '2.28.0-2') ]
    allPackageInfos = getAllPackagesInfo()

    results = {}
    resultsRef = RefObj(results)


    sys.stdout.write('Read %d total packages.\n' %( len(allPackageInfos), ))

    priorDBContents = None
    try:
        with open(PROVIDES_DB_LOCATION, 'rb') as f:
            priorDBContents = f.read()
    except:
        sys.stderr.write('WARNING: Cannot read old Provides DB at "%s". Will query every package (instead of just updates)\n' %(PROVIDES_DB_LOCATION, ))

    if priorDBContents:
        ####################################################
        ## Figure out which packages actually need update
        ##  and/or convert database format
        #####################################
        priorDBContents = gzip.decompress(priorDBContents)

        try:
            oldResults = json.loads(priorDBContents)
            sys.stdout.write('Read %d records from old database. Trimming non-updates...\n' %(len(oldResults) - 1, ))

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


            # Assmemble new package info list, including only the packages we need to update
            newPackagesInfo = []
            for packageInfo in allPackageInfos:
                pkgName = packageInfo[1]
                pkgVersion = packageInfo[2]

                if pkgName not in oldResults:
                    # New package
                    newPackagesInfo.append(packageInfo)
                    continue

                if oldResults[pkgName]['version'] == pkgVersion:
                    results[pkgName] = oldResults[pkgName]
                else:
                    if not canCompareVersions:
                        newPackagesInfo.append(packageInfo)
                    elif forceOldUpdate:
                        if isVerbose is True:
                            oldVersion = VersionString(oldResults[pkgName]['version'])
                            newVersion = VersionString(pkgVersion)
                            if newVersion < oldVersion:
                                sys.stderr.write('WARNING: Package %s - %s has an older version!  "%s"  < "%s" ! Did primary repo change to an older mirror? Doing anyway, because of --force-old-update\n' %(packageInfo[0], pkgName, str(oldVersion), str(newVersion)))

                        newPackagesInfo.append(packageInfo)
                    else:
                        oldVersion = VersionString(oldResults[pkgName]['version'])
                        newVersion = VersionString(pkgVersion)

                        if newVersion > oldVersion:
                            newPackagesInfo.append(packageInfo)
                        else:
                            sys.stderr.write('WARNING: Package %s - %s has an older version!  "%s"  < "%s" ! Did primary repo change to an older mirror? Skipping... (use --force-old-update to do anyway)\n' %(packageInfo[0], pkgName, str(oldVersion), str(newVersion)))



            allPackageInfos = newPackagesInfo
            sys.stdout.write('\nTrimmed number of updates required to %d\n\n' %(len(allPackageInfos), ))

            del oldResults
            del newPackagesInfo

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


    if 'REPO_URLS' in locals():
        print ( "USING PREDEFINED REPO")
        repoUrls = REPO_URLS
    else:
        repoUrls = getRepoUrls()

    print ( "Using repos from /etc/pacman.d/mirrorlist:\n\t%s\n" %(repoUrls, ))


#    if len(args) != 1:
#        fileData = getFileData(args)
#        doOne('core', 'binutils', '2.28.0-2', results, fileData)
#        sys.exit(0)

    failedPackageInfos = []

    ##############################################
    ######## Start up threads and
    ########   begin processing
    ########################################
    numRepos = len(repoUrls)
    if numRepos == 0:
        sys.stderr.write('No uncommented repos in /etc/pacman.d/mirrorlist !\n\n')
        sys.exit(1)

    if numRepos < MAX_THREADS:
        sys.stdout.write('WARNING: Number of available repos [ %d ] is less than the configured number' %(numRepos, ) +\
            ' of threads [%d].\nRecommended to uncomment more repos. See --help for changing nubmer of threads.\n\n' %(MAX_THREADS, ))

        shrinkThreads = prompt("\nLimit threads to %d and continue? (y/n): " %(numRepos, ), ('y', 'Y', 'n', 'N'))
        if shrinkThreads in ('n', 'N'):
            sys.stderr.write('\nAborting based on user input.\n\n')
            sys.exit(1)

        numThreads = numRepos
    else:
        numThreads = MAX_THREADS

    numPackages = len(allPackageInfos)

    runner = Runner(numThreads, allPackageInfos, repoUrls, resultsRef, failedPackageInfos, isVerbose=isVerbose, isSuperVerbose=isSuperVerbose)
    runner.run()

    del allPackageInfos
    gc.collect()

    ##############################################
    ######## If packages failed, wait a bit,
    ########  refresh database, and try again
    ########################################
    try: # TODO: REMOVE ME 


        if failedPackageInfos:
            sys.stderr.write("Need to retry %d packages. Resting for a minute....\n\n" %(len(failedPackageInfos), ))
            time.sleep(60)

            if numThreads > 1:
                # Shuffle up the order to try different mirrors
                failedPackageInfos = shuffleLst(failedPackageInfos)

            refreshPacmanDatabase()

            # Now give up to 8 minutes for each package to complete. This covers if a PAX_FORMAT on a huge file,
            #   slow mirror, etc.
            newFailedPackageInfos = []

            runner = Runner(numThreads, failedPackageInfos, repoUrls, resultsRef, newFailedPackageInfos, timeout=LONG_TIMEOUT, isVerbose=isVerbose, isSuperVerbose=isSuperVerbose)
            runner.run()

            del failedPackageInfos
            gc.collect()

            ##################################################
            ######## If still failures,
            ########  refresh and check if newer version
            ########  and retry those pkgs with newer version
            ##########################################
            if newFailedPackageInfos:
                sys.stderr.write('After completing, still %d failed packages.\nFailed after retry: %s\n\n' %(len(newFailedPackageInfos), '\n'.join(['\t[%s] %s-%s  \t%s' %(failedP[0], failedP[1], failedP[2], results[failedP[1]]['error'] ) for failedP in newFailedPackageInfos]) ) ) 

                if refreshPacmanDatabase():

#                    oldVersions = {}
#                    for packageInfo in newFailedPackageInfos:
#                        oldVersions[packageInfo[1]] = packageInfo[2]
                    oldVersions = { packageInfo[1] : packageInfo[2] for packageInfo in newFailedPackageInfos }

                    newPackagesInfo = getAllPackagesInfo()

                    # Get a list of any packages that have updated since we started, and retry them
                    updatedPackages = [packageInfo for packageInfo in newPackagesInfo if packageInfo[1] in oldVersions and oldVersions[packageInfo[1]] != packageInfo[2]]


                    # Will try every mirror
                    stillFailedPackageInfos = []

                    runner = Runner(1, updatedPackages, repoUrls, resultsRef, stillFailedPackageInfos, timeout=LONG_TIMEOUT, isVerbose=isVerbose, isSuperVerbose=isSuperVerbose)
                    runner.run()

                    # Append the failed packages we didn't retry
                    stillFailedPackageInfos += [packageInfo for packageInfo in newFailedPackageInfos if packageInfo not in stillFailedPackageInfos]

                    if stillFailedPackageInfos:
                        sys.stderr.write('EVEN after refreshing package database, the following packages are total failures:\n\n%s\n\n' %( str([failedP[1] for failedP in stillFailedPackageInfos]), ))



                    gc.collect()

            # END: if newFailedPackageInfos


        ##############################################
        ######## Write resulting database to file
        ########################################
        results['__vers'] = LATEST_FILE_FORMAT


        writeDatabase(results)

        pass
        #import pdb; pdb.set_trace()
        print ( "\n\nSuccess.\nDatabase size: %d\n" %(len(results) - 1, ))
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
