#!/usr/bin/env python
"""
swiftnbd. common toolset
Copyright (C) 2012 by Juan J. Martinez <jjm@usebox.net>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import logging
from logging.handlers import SysLogHandler
import errno
import os
from time import time
from cStringIO import StringIO
from hashlib import md5
from ConfigParser import RawConfigParser

from swiftclient import client

def getSecrets(container, secrets_file):
    """Read secrets"""
    stat = os.stat(secrets_file)
    if stat.st_mode & 0x004 != 0:
        log = logging.getLogger(__package__)
        log.warning("%s is world readable, please consider changing its permissions to 0600" % secrets_file)

    conf = RawConfigParser(dict(username=None, password=None))
    conf.read(secrets_file)

    if not conf.has_section(container):
        raise ValueError("%s not found in %s" % (container, secrets_file))

    return (conf.get(container, 'username'), conf.get(container, 'password'))

def setLog(debug=False, use_syslog=False):
    """Setup logger"""
    log = logging.getLogger(__package__)

    if use_syslog:
        try:
            handler = SysLogHandler(address="/dev/log", facility='local0')
        except IOError:
            # fallback to UDP
            handler = SysLogHandler(facility='local0')
        handler.setFormatter(logging.Formatter('%(name)s[%(process)d]: %(levelname)s: %(message)s'))
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s: %(name)s: %(levelname)s: %(message)s'))

    log.addHandler(handler)

    if debug:
        log.setLevel(logging.DEBUG)
        log.debug("Verbose log enabled")
    else:
        log.setLevel(logging.INFO)

    return log

_META_PREFIX = "x-container-meta-swiftnbd-"
_META_REQUIRED = ('version', 'blocks', 'block-size')

def setMeta(meta):
    """Convert a metada dict into swift meta headers"""
    return dict(("%s%s" % (_META_PREFIX, key), value) for key, value in meta.iteritems())

def getMeta(hdrs):
    """Convert swift meta headers with swiftndb prefix into a dictionary"""
    data = dict((key[len(_META_PREFIX):], value) for key, value in hdrs.iteritems() if key.lower().startswith(_META_PREFIX))
    for key in _META_REQUIRED:
        if key not in data:
            return dict()
    return data

class SwiftBlockFile(object):
    """
    Manages a block-split file stored in OpenStack Object Storage (swift).

    May raise IOError.
    """
    cache = dict()

    def __init__(self, authurl, username, password, container, block_size, blocks):
        self.container = container
        self.block_size = block_size
        self.blocks = blocks
        self.pos = 0
        self.locked = False
        self.meta = dict()

        self.cli = client.Connection(authurl, username, password)

    def __str__(self):
        return self.container

    def lock(self, client_id):
        """Set the storage as busy"""
        if self.locked:
            return

        try:
            headers, _ = self.cli.get_container(self.container)
        except client.ClientException as ex:
            raise IOError(errno.EACCES, "Storage error: %s" % ex.http_status)

        self.meta = getMeta(headers)

        if self.meta.get('client'):
            raise IOError(errno.EBUSY, "Storage already in use: %s" % self.meta['client'])

        self.meta['client'] = "%s@%i" % (client_id, time())
        hdrs = setMeta(self.meta)
        try:
            self.cli.put_container(self.container, headers=hdrs)
        except client.ClientException as ex:
            raise IOError(errno.EIO, "Failed to lock: %s" % ex.http_status)

        self.locked = True

    def unlock(self):
        """Set the storage as free"""
        if not self.locked:
            return

        self.meta['last'] = self.meta.get('client')
        self.meta['client'] = ''
        hdrs = setMeta(self.meta)
        try:
            self.cli.put_container(self.container, headers=hdrs)
        except client.ClientException as ex:
            raise IOError(errno.EIO, "Failed to unlock: %s" % ex.http_status)

        self.locked = False

    def read(self, size):
        data = ""
        _size = size
        while _size > 0:
            block = self.fetch_block(self.block_num)
            if block == '':
                break

            if _size + self.block_pos >= self.block_size:
                data += block[self.block_pos:]
                part_size = self.block_size - self.block_pos
            else:
                data += block[self.block_pos:self.block_pos+_size]
                part_size = _size

            _size -= part_size
            self.seek(self.pos + part_size)

        return data

    def write(self, data):
        _data = data[:]
        if self.block_pos != 0:
            # block-align the beginning of data
            block = self.fetch_block(self.block_num)
            _data = block[:self.block_pos] + _data
            self.seek(self.pos - self.block_pos)

        reminder = len(_data) % self.block_size
        if reminder != 0:
            # block-align the end of data
            block = self.fetch_block(self.block_num + (len(_data) / self.block_size))
            _data += block[reminder:]

        assert len(_data) % self.block_size == 0, "Data not aligned!"

        offs = 0
        block_num = self.block_num
        while offs < len(_data):
            self.put_block(block_num, _data[offs:offs+self.block_size])
            offs += self.block_size
            block_num += 1

    def tell(self):
        return self.pos

    @property
    def block_pos(self):
        # position in the block
        return self.pos % self.block_size

    @property
    def block_num(self):
        # block number based on the position
        return self.pos / self.block_size

    @property
    def size(self):
        return self.block_size * self.blocks

    def flush(self):
        self.cache = dict()

    def fetch_block(self, block_num):
        if block_num >= self.blocks:
            return ''

        data = self.cache.get(block_num)
        if not data:
            block_name = "disk.part/%.8i" % block_num
            try:
                _, data = self.cli.get_object(self.container, block_name)
            except client.ClientException as ex:
                if ex.http_status != 404:
                    raise IOError(errno.EIO, "Storage error: %s" % ex)
                return '\0' * self.block_size

            self.cache[block_num] = data
        return data

    def put_block(self, block_num, data):
        if block_num >= self.blocks:
            raise IOError(errno.ESPIPE, "Write offset out of bounds")

        block_name = "disk.part/%.8i" % block_num
        try:
            etag = self.cli.put_object(self.container, block_name, StringIO(data))
        except client.ClientException as ex:
            raise IOError("Storage error: %s" % ex, errno=errno.EIO)

        checksum = md5(data).hexdigest()
        etag = etag.lower()
        if etag != checksum:
            raise IOError(errno.EAGAIN, "Block integrity error (block_num=%s)" % block_num)

        self.cache[block_num] = data

    def seek(self, offset):
        if offset < 0 or offset > self.size:
            raise IOError(errno.ESPIPE, "Offset out of bounds")

        self.pos = offset

