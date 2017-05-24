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

from io import BytesIO

import func_timeout
from nonblock import bgwrite

try:
    PermissionError
except NameError:
    PermissionError = IOError

PROVIDES_DB_LOCATION = "/var/lib/pacman/.providesDB"

global isVerbose
isVerbose = False

def decompressZlib(data):
    
    devnull = open(os.devnull, 'w')

    pipe = subprocess.Popen(['/usr/bin/zcat'], shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=devnull)
    pipe.stdin.write(data)
    pipe.stdin.close()

    results = pipe.stdout.read()
    pipe.wait()

    devnull.close()

    return results

def decompressXz(data):

    devnull = open(os.devnull, 'w')
    pipe = subprocess.Popen(['/usr/bin/xzcat'], shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=devnull)

    bgwrite(pipe.stdin, data, ioPrio=1, closeWhenFinished=True)

    results = pipe.stdout.read()
    pipe.wait()

    devnull.close()

    return results


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
        

#REPO_URL = "http://mirrors.acm.wpi.edu/archlinux/%s/os/x86_64/%s"
#REPO_URLS = [ "http://mirrors.acm.wpi.edu/archlinux/%s/os/x86_64/%s" ]

def getFileData(filename, decodeWith=None):
    with open(filename, 'rb') as f:
        contents = f.read()

    if decodeWith:
        contents = contents.decode(decodeWith)
    return contents


if __name__ == '__main__':
    
    args = sys.argv[1:]


    if '-v' in args:
        isVerbose = True
        args.remove('-v')
    if '--verbose' in args:
        isVerbose = True
        args.remove('--verbose')
        

    if len(args) == 0:
        allPackages = getAllPackages()
    else:
        sys.stderr.write('Too many arguments\n')
        sys.exit(1)

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

    results = {}

    def doOne(repoName, packageName, versionInfo, results, repoUrl, fetchedData=None, useTarMod=False):
        global isVerbose

        if fetchedData is None:
            finalUrl = repoUrl %( repoName, packageName + "-" + versionInfo + "-x86_64.pkg.tar.xz" )
            if isVerbose:
                print ( "Fetching url: " + finalUrl )

            if useTarMod is False:
                maxSize = 1024 * 80
            else:
                maxSize = None
            tarContents = fetchFromUrl(finalUrl, maxSize)
        else:
            tarContents = fetchedData

        if useTarMod is False:
            data = decompressXz(tarContents[:1024 * 80])

            mtreeIdx = data.index(b'.MTREE')

            headerStart = data[mtreeIdx:]

            try:
                mtreeSize = getSize(headerStart)
                compressedData = headerStart[512 : 512 + mtreeSize]
            except:
                return doOne(repoName, packageName, versionInfo, results, repoUrl, fetchedData, useTarMod=True)
        else:
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

        results[packageName] = files
        if isVerbose:
            sys.stdout.write("Got %d files.\n\n" %(len(files), ))


#    if len(args) != 1:
#        fileData = getFileData(args)
#        doOne('core', 'binutils', '2.28.0-2', results, fileData)
#        sys.exit(0)

    failedPackages = []

    def runThroughPackages(allPackages, results, failedPackages, repoUrls, timeout=5):
        global isVerbose

        for repoName, packageName, versionInfo in allPackages:
            time.sleep(1.5)
            if isVerbose:
                sys.stdout.write("Processing %s - %s: " %(repoName, packageName) )
                sys.stdout.flush()
            try:
                func_timeout.func_timeout(8, doOne, (repoName, packageName, versionInfo, results, repoUrls[0]))
            except func_timeout.FunctionTimedOut as fte:
                try:
                    didIt = False
                    for nextRepoUrl in repoUrls[1:]:
                        try:
                            func_timeout.func_timeout(8, doOne, (repoName, packageName, versionInfo, results, nextRepoUrl))
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
                                results[packageName] = errStr
                            except:
                                pass

                    if didIt is False:
                        failedPackages.append ( (repoName, packageName, versionInfo) )
                        errStr = 'Error TIMEOUT processing %s - %s : FunctionTimedOut\n\n' %(repoName, packageName )
                        sys.stderr.write(errStr)
                        results[packageName] = errStr
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
                    results[packageName] = errStr
                except:
                    pass

        #END: def runThroughPackages

    try:
        runThroughPackages(allPackages, results, failedPackages, repoUrls, timeout=5)
    except KeyboardInterrupt as ke:
        sys.exit(32)

    if failedPackages:
        sys.stderr.write("Need to retry %d packages. Resting for a minute....\n\n" %(len(failedPackages), ))
        time.sleep(60)

        # Now give up to 8 minutes for each package to complete. This covers if a PAX_FORMAT on a huge file,
        #   slow mirror, etc.
        newFailedPackages = []
        try:
            runThroughPackages( failedPackages, results, newFailedPackages, repoUrls, timeout=(60 * 8))
        except KeyboardInterrupt as ke:
            pass

        if newFailedPackages:
            sys.stderr.write('After completing, still %d failed packages.\nFailed after retry: %s\n\n' %(len(newFailedPackages), '\n'.join(['\t[%s] %s-%s  \t%s' %(failedP[0], failedP[1], failedP[2], results[failedP[1]]  ) for failedP in newFailedPackages]) ) )


    compressed = gzip.compress( json.dumps(results).encode('utf-8') )


    try:
        with open(PROVIDES_DB_LOCATION, 'wb') as f:
            f.write( compressed )
    except PermissionError:
        tempFile = tempfile.NamedTemporaryFile(mode='wb', delete=False)
        sys.stderr.write('Failed to open "%s" for writing. Dumping to tempfile: "%s"\n' %(PROVIDES_DB_LOCATION, tempFile.name, ))
        tempFile.write( compressed )
        tempFile.close()

# vim: set ts=4 sw=4 expandtab :
