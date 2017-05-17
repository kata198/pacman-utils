#!/usr/bin/env python
#
# vim: set ts=4 sw=4 expandatb :

# Copyright (c) 2017 Timothy Savannah - All Rights Reserved
#   This code is licensed under the terms of the APACHE license version 2.0
#
#  extractMtree.py - Extracts the mtree from all available packages,
#    attempting short-reads where possible to limit bandwidth,
#    and assemble a compressed json database for use by whatprovides_upstream


import sys
import os
import re
import json
import subprocess
import time
import gzip

import func_timeout
from nonblock import bgwrite


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

    pipe = subprocess.Popen("/usr/bin/curl '-k' '%s' %s" %(url, limitBytes), shell=True, stdout=subprocess.PIPE, stderr=devnull)
    
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


REPO_URL = "http://mirrors.acm.wpi.edu/archlinux/%s/os/x86_64/%s"

def getFileData(filename, decodeWith=None):
    with open(filename, 'rb') as f:
        contents = f.read()

    if decodeWith:
        contents = contents.decode(decodeWith)
    return contents

from io import BytesIO
import tarfile

if __name__ == '__main__':
    
    if len(sys.argv) == 1:
        allPackages = getAllPackages()

    results = {}

    def doOne(repoName, packageName, versionInfo, results, fetchedData=None, useTarMod=False):
        if fetchedData is None:
            finalUrl = REPO_URL %( repoName, packageName + "-" + versionInfo + "-x86_64.pkg.tar.xz" )
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
                return doOne(repoName, packageName, versionInfo, results, fetchedData, useTarMod=True)
        else:
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
        sys.stdout.write("Got %d files.\n\n" %(len(files), ))


    if len(sys.argv) != 1:
        fileData = getFileData(sys.argv[1])
        doOne('core', 'binutils', '2.28.0-2', results, fileData)
        sys.exit(0)

    failedPackages = []

    def runThroughPackages(allPackages, results, failedPackages, timeout=5):

        for repoName, packageName, versionInfo in allPackages:
            time.sleep(1.5)
            sys.stdout.write("Processing %s - %s: " %(repoName, packageName) )
            sys.stdout.flush()
            try:
                func_timeout.func_timeout(5, doOne, (repoName, packageName, versionInfo, results))
            except func_timeout.FunctionTimedOut as fte:
                try:
                    failedPackages.append ( (repoName, packageName, versionInfo) )
                    errStr = 'Error TIMEOUT processing %s - %s : FunctionTimedOut\n\n' %(repoName, packageName )
                    sys.stderr.write(errStr)
                    results[packageName] = errStr
                except:
                    pass
            except KeyboardInterrupt as ke:
                raise ke
            except Exception as e:
                try:
                    failedPackages.append ( (repoName, packageName, versionInfo) )
                    errStr = 'Error processing %s - %s : < %s >: %s\n\n' %(repoName, packageName, e.__class__.__name__, str(e))
                    sys.stderr.write(errStr)
                    results[packageName] = errStr
                except:
                    pass

    try:
        runThroughPackages(allPackages, results, failedPackages, timeout=5)
    except KeyboardInterrupt as ke:
        pass

    if failedPackages:
        sys.stderr.write("Need to retry %d packages. Resting for a minute....\n\n" %(len(failedPackages), ))
        time.sleep(60)

        # Now give up to 8 minutes for each package to complete. This covers if a PAX_FORMAT on a huge file,
        #   slow mirror, etc.
        newFailedPackages = []
        try:
            runThroughPackages( failedPackages, results, newFailedPackages, timeout=(60 * 8))
        except KeyboardInterrupt as ke:
            pass

        if newFailedPackages:
            sys.stderr.write('After completing, still %d failed packages.\nFailed after retry: %s\n\n' %(len(newFailedPackages), '\n'.join(['\t[%s] %s-%s  \t%s' %(failedP[0], failedP[1], failedP[2], results[failedP[1]]  ) for failedP in newFailedPackages]) ) )


    compressed = gzip.compress( json.dumps(results) )

    with open('/var/lib/pacman/.providesDB', 'wb') as f:
        f.write( compressed )

