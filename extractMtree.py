#!/usr/bin/env python
#
# vim: set ts=4 sw=4 expandtab :

# Copyright (c) 2017 Timothy Savannah - All Rights Reserved
#   This code is licensed under the terms of the APACHE license version 2.0
#
#  extractMtree.py - Extracts the mtree from all available packages,
#    attempting short-reads where possible to limit bandwidth,
#    and assemble a compressed json database for use by whatprovides_upstream


import gzip
import os
import json
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
    from nonblock import bgwrite, bgread, BackgroundIOPriority
except ImportError:
    sys.stderr.write('ERROR: Cannot import python module nonblock ( python-nonblock ) - not installed?\n')
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


LATEST_FILE_FORMAT = '0.2'

PROVIDES_DB_LOCATION = "/var/lib/pacman/.providesDB"

global isVerbose
isVerbose = False

global SUBPROCESS_BUFSIZE
SUBPROCESS_BUFSIZE = 1024 * 500 # 500K Bufsize

global MaxBackgroundIO
MaxBackgroundIO = BackgroundIOPriority(.0009, SUBPROCESS_BUFSIZE, 100, 5)

global SHORT_FETCH_SIZE
SHORT_FETCH_SIZE = 1024 * 200 # Try to fetch first 200K to find MTREE


def decompressDataSubprocess(data, cmd):
    global MaxBackgroundIO
    global SUBPROCESS_BUFSIZE
    
    fte = None

    devnull = open(os.devnull, 'w')
    pipe = subprocess.Popen(cmd, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=devnull, close_fds=True, bufsize=SUBPROCESS_BUFSIZE)

    # Short delay to ensure everything is init'd and ready to go
    time.sleep(.002)
    try:
        # Because these are block compression algorithms, and often contain incomplete data, the output is given after processing
        #  a number of input blocks. At least for my gzip, it seems to stop output after 32K are sitting on the buffer waiting
        #  to be read. So doing a write, then close on stdin, then try to read, depending on timing you may just get that 32K
        #  buffer, after which the remainder will process. This ends up in a messy series of loops and sleeps and whatnot,
        #  and is finnikey, better handled with python-nonblock
        #
        # So we are going to write to the buffer AND read from the buffer at the same time to make sure
        #   everything goes gravytrain on down the line.
        bgwrite(pipe.stdin, data, ioPrio=MaxBackgroundIO, closeWhenFinished=True)
        
        time.sleep(.0005)
        bgReader = bgread(pipe.stdout, blockSizeLimit=SUBPROCESS_BUFSIZE, pollTime=0.0, closeStream=False)

        while not bgReader.isFinished:
            time.sleep(.03)
            pipe.poll()
            
        
#        pipe.stdin.write(data)
#        pipe.stdin.close()
#        
#        time.sleep(.1)
#        result = pipe.stdout.read()
#        nextResult = True
#        # zcat 
#        while nextResult != b'':
#            time.sleep(.05)
#            nextResult = pipe.stdout.read()
#            result += nextResult
#
        pipe.wait()

        result = bgReader.data

    except func_timeout.FunctionTimedOut as _fte:
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

    if fte:
        raise_exception( [fte] )

    return result

def decompressZlib(data):
    # -dnc - Decompress to stdout and don't worry about file
    return decompressDataSubprocess(data, ['/usr/bin/gzip', '-dnc'])

def decompressXz(data):
    # -dc - Decompress to stdout
    return decompressDataSubprocess(data, ['/usr/bin/xz', '-dc'])

SIZE_IDX_START = 124
SIZE_IDX_END = 124 + 12

def getSize(header):
    
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
    
    devnull = open(os.devnull, 'w')

    if numBytes:
        limitBytes = '| head -c %d' %(numBytes, )
    else:
        limitBytes = ''

    if not isVerbose:
        extraArgs = '--silent'
    else:
        extraArgs = ""

    pipe = subprocess.Popen("/usr/bin/curl '-k' %s '%s' %s" %(extraArgs, url, limitBytes), shell=True, stdout=subprocess.PIPE, stderr=devnull)
    
    urlContents = pipe.stdout.read()
    ret = pipe.wait()

    devnull.close()

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


# USE_ARCH - Package arch to use. TODO: Allow others
USE_ARCH = "x86_64" 

def getRepoUrls():
    nextLine = True
    repos = []

    repoRE = re.compile('^[ \t]*Server[ \t]*=[ \t]*(?P<server_url>.+)$')

    with open('/etc/pacman.d/mirrorlist', 'rt') as f:
        nextLine = f.readline()
        while nextLine != '':
            matchObj = repoRE.match(nextLine.strip())
            if matchObj:
                groupDict = matchObj.groupdict()
                if groupDict['server_url']:
                    ret = groupDict['server_url'].replace('$repo', '%s').replace('$arch', USE_ARCH)
                    while ret.endswith('/'):
                        ret = ret[:-1]

                    ret += '/%s'
                    repos.append(ret)

            nextLine = f.readline()

    if not repos:
        raise Exception('Failed to find repo URL from /etc/pacman.d/mirrorlist. Are any not commented?')
    return repos
 

class RefObj(object):
    
    def __init__(self, ref):
        self.ref = ref

    def __call__(self):
        return self.ref

#REPO_URL = "http://mirrors.acm.wpi.edu/archlinux/%s/os/x86_64/%s"
#REPO_URLS = [ "http://mirrors.acm.wpi.edu/archlinux/%s/os/x86_64/%s" ]

MAX_THREADS = 6

SHORT_TIMEOUT = 15
LONG_TIMEOUT = ( 60 * 8 )


class RetryWithFullTarException(Exception):
    pass

def getFileData(filename, decodeWith=None):
    with open(filename, 'rb') as f:
        contents = f.read()

    if decodeWith:
        contents = contents.decode(decodeWith)
    return contents


def createThreads(splitBy, allPackages, repoUrls, resultsRef, failedPackages):
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
            myRepoUrls = [ repoUrls[i] ] + repoUrls[splitBy : splitBy + MAX_EXTRA_URLS]
            thisThread = StoppableThread(target=runThroughPackages, args=(packageSet, resultsRef, failedPackages, myRepoUrls), kwargs={'timeout' : SHORT_TIMEOUT})

            thisThread.start()
            threads.append(thisThread)
            time.sleep(.35) # Offset the threads a bit

    return threads

if __name__ == '__main__':
    # NOTE: This uses a LOT of memory, so we delete and garbage collect
    #  manually (since everything in main thread is in one scope, it will
    #  rarely, if ever, automatically trigger.
    
    args = sys.argv[1:]


    if '-v' in args:
        isVerbose = True
        args.remove('-v')
    if '--verbose' in args:
        isVerbose = True
        args.remove('--verbose')
        

    if len(args) != 0:
        sys.stderr.write('Too many arguments\n')
        sys.exit(1)
 
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
                oldResults.pop('__vers')

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
                    else:
                        oldVersion = VersionString(oldResults[pkgName]['version'])
                        newVersion = VersionString(pkgVersion)

                        if newVersion > oldVersion:
                            newPackages.append(packageInfo)
                        else:
                            sys.stderr.write('WARNING: Package %s - %s has an older version!  "%s"  < "%s" ! Did primary repo change to an older mirror? Skipping...\n' %(packageInfo[0], pkgName, str(oldVersion), str(newVersion)))
                            

            
            allPackages = newPackages
            sys.stdout.write('\nTrimmed number of updates required to %d\n\n' %(len(allPackages), ))
    
            del oldResults
            del newPackages

        except Exception as e:
            sys.stderr.write('Error reading old database:  %s:  %s\n' %( e.__class__.__name__, str(e)))
        finally:
            del priorDBContents
            gc.collect()


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

    def doOne(repoName, packageName, versionInfo, resultsRef, repoUrl, fetchedData=None, useTarMod=False):
        global isVerbose
        global SHORT_FETCH_SIZE

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
                compressedData = headerStart[512 : 512 + mtreeSize]
            except Exception as ex2:
                if useTarMod is False:
                    return doOne(repoName, packageName, versionInfo, resultsRef, repoUrl, fetchedData, useTarMod=True)
                raise ex2

        else:
            # doTarMod is True
            if isVerbose:
                print ( "Fallback..." )
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
            sys.stdout.write("Got %d files.\n\n" %(len(files), ))
    
    # END: doOne


#    if len(args) != 1:
#        fileData = getFileData(args)
#        doOne('core', 'binutils', '2.28.0-2', results, fileData)
#        sys.exit(0)

    failedPackages = []

    def runThroughPackages(allPackages, resultsRef, failedPackages, repoUrls, timeout=SHORT_TIMEOUT, longTimeout=LONG_TIMEOUT):
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

    MAX_EXTRA_URLS = 3
    
    splitBy = len(repoUrls)
    if splitBy > MAX_THREADS:
        splitBy = MAX_THREADS;

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
                thread.stop(ke)

            # Try real quick to cleanup, they are daemon threads so they will be
            #   terminated at end of program forcibly
            for thread in threads:
                thread.join(.05)

            sys.exit(32)

    del allPackages
    gc.collect()

    try: # TODO: REMOVE ME 


        if failedPackages:
            sys.stderr.write("Need to retry %d packages. Resting for a minute....\n\n" %(len(failedPackages), ))
            time.sleep(60)
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
                        thread.stop(ke)

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

        results['__vers'] = LATEST_FILE_FORMAT

        compressed = gzip.compress( json.dumps(results).encode('utf-8') )


        try:
            with open(PROVIDES_DB_LOCATION, 'wb') as f:
                f.write( compressed )
        except PermissionError:
            tempFile = tempfile.NamedTemporaryFile(mode='wb', delete=False)
            sys.stderr.write('Failed to open "%s" for writing. Dumping to tempfile: "%s"\n' %(PROVIDES_DB_LOCATION, tempFile.name, ))
            tempFile.write( compressed )
            tempFile.close()
        pass
        #import pdb; pdb.set_trace()
        print ( "Success.\n")
        print ( str(locals().keys()) )
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
