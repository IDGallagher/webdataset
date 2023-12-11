import io
import os
import random
import re
import sys
import time
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

import webdataset.gopen as gopen

from . import filters, gopen
from .handlers import reraise_exception
from .tariterators import group_by_keys, tar_file_expander

default_cache_dir = os.environ.get("WDS_CACHE", "./_cache")
default_cache_size = float(os.environ.get("WDS_CACHE_SIZE", "1e18"))

verbose_cache = int(os.environ.get("WDS_VERBOSE_CACHE", "0"))


def get_filetype(fname):
    assert os.system("file . > /dev/null") == 0, "UNIX/Linux file command not available"
    with os.popen("file '%s'" % fname) as f:
        ftype = f.read()
    return ftype


def check_tar_format(fname):
    """Check whether a file is a tar archive."""
    ftype = get_filetype(fname)
    return "tar archive" in ftype or "gzip compressed" in ftype


class LRUCleanup:
    def __init__(
        self,
        cache_dir=None,
        cache_size=int(1e12),
        keyfn=os.path.getctime,
        verbose=False,
        interval=30,
    ):
        self.cache_dir = cache_dir
        self.cache_size = cache_size
        self.keyfn = keyfn
        self.verbose = verbose
        self.interval = interval
        self.last_run = 0

    def set_cache_dir(self, cache_dir):
        self.cache_dir = cache_dir

    def cleanup(self):
        """Performs cleanup of the file cache in cache_dir using an LRU strategy,
        keeping the total size of all remaining files below cache_size."""
        if not os.path.exists(self.cache_dir):
            return
        if self.interval is not None and time.time() - self.last_run < self.interval:
            return
        try:
            total_size = 0
            for dirpath, dirnames, filenames in os.walk(self.cache_dir):
                for filename in filenames:
                    total_size += os.path.getsize(os.path.join(dirpath, filename))
            if total_size <= self.cache_size:
                return
            # sort files by last access time
            files = []
            for dirpath, dirnames, filenames in os.walk(self.cache_dir):
                for filename in filenames:
                    files.append(os.path.join(dirpath, filename))
            files.sort(key=self.keyfn, reverse=True)
            # delete files until we're under the cache size
            while len(files) > 0 and total_size > self.cache_size:
                fname = files.pop()
                total_size -= os.path.getsize(fname)
                if self.verbose:
                    print("# deleting %s" % fname, file=sys.stderr)
                os.remove(fname)
        except (OSError, FileNotFoundError):
            # files may be deleted by other processes between walking the directory and getting their size/deleting them
            pass
        self.last_run = time.time()


def download(url, dest, chunk_size=1024**2, verbose=False):
    """Download a file from `url` to `dest`."""
    temp = dest + f".temp{os.getpid()}"
    with gopen.gopen(url) as stream:
        with open(temp, "wb") as f:
            while True:
                data = stream.read(chunk_size)
                if not data:
                    break
                f.write(data)
    os.rename(temp, dest)


def pipe_cleaner(spec):
    """Guess the actual URL from a "pipe:" specification."""
    if spec.startswith("pipe:"):
        spec = spec[5:]
        words = spec.split(" ")
        for word in words:
            if re.match(r"^(https?|hdfs|gs|ais|s3):", word):
                return word
    return spec


def url_to_cache_name(url, ndir=0):
    """Guess the cache name from a URL."""
    parsed = urlparse(url)
    if parsed.scheme in [
        None,
        "",
        "file",
        "http",
        "https",
        "ftp",
        "ftps",
        "gs",
        "s3",
        "ais",
    ]:
        path = parsed.path
        list_of_directories = path.split("/")
        return "/".join(list_of_directories[-1 - ndir])
    else:
        # don't try to guess, just urlencode the whole thing with "/" and ":"
        # quoted using the urllib.quote function
        quoted = urllib.parse.quote(url, safe="_+{}*,-")
        quoted = quoted[-128:]
        return quoted


class StreamingOpen:
    def __init__(self, verbose=False, handler=reraise_exception):
        self.verbose = verbose
        self.handler = handler

    def get_file(self, url):
        return url

    def open_file(self, url):
        parsed = urlparse(url)
        if parsed.scheme in ["", "file"]:
            return open(parsed.path, "rb")
        else:
            return gopen.gopen(url)

    def __call__(self, urls):
        for url in urls:
            if isinstance(url, dict):
                url = url["url"]
            try:
                stream = self.open_file(self.get_file(url))
                yield dict(url=url, stream=stream)
            except Exception as exn:
                if self.handler(exn):
                    continue
                else:
                    break


class FileCache:
    def __init__(
        self,
        url_to_name: Callable[[str], str] = url_to_cache_name,
        cache_dir: Optional[str] = None,
        verbose: bool = False,
        validator: Callable[[str], bool] = check_tar_format,
        handler: Callable[[Exception], bool] = reraise_exception,
        cache_size: int = -1,
        cache_cleanup_interval: int = 30,
    ):
        self.url_to_name = url_to_name
        self.validator = validator
        self.handler = handler
        if cache_dir is None:
            self.cache_dir = default_cache_dir
        else:
            self.cache_dir = cache_dir
        self.verbose = verbose
        if cache_size > 0:
            self.cleaner = LRUCleanup(
                self.cache_dir,
                self.cache_size,
                verbose=self.verbose,
                interval=cache_cleanup_interval,
            )
        else:
            self.cleaner = None

    def get_file(self, url: str) -> str:
        cache_name = self.url_to_name(url)
        destdir = os.path.join(self.cache_dir, os.path.dirname(cache_name))
        os.makedirs(destdir, exist_ok=True)
        dest = os.path.join(self.cache_dir, cache_name)
        if not os.path.exists(dest):
            if self.verbose:
                print("# downloading %s to %s" % (url, dest), file=sys.stderr)
            if self.cleaner is not None:
                self.cleaner.cleanup()
            download(url, dest, verbose=self.verbose)
            if self.validator:
                if not self.validator(dest):
                    ftype = get_filetype(dest)
                    with open(dest, "rb") as f:
                        data = f.read(200)
                    os.remove(dest)
                    raise ValueError(
                        "%s (%s) is not a tar archive, but a %s, contains %s"
                        % (dest, url, ftype, repr(data))
                    )
        return dest

    def open_file(self, url: str) -> io.IOBase:
        """Open a file, downloading it if necessary.

        If the url refers to a local file, just open it and return the stream
        otherwise, use get_file to download it to the cache and return the stream.
        """
        parsed = urlparse(url)
        if parsed.scheme in ["", "file"]:
            self.last_path = parsed.path
            return open(parsed.path, "rb")
        else:
            self.last_path = self.get_file(url)
            return open(self.last_path, "rb")

    def __call__(self, urls: Iterable[str]) -> Iterable[io.IOBase]:
        for url in urls:
            if isinstance(url, dict):
                url = url["url"]
            for _ in range(10):
                try:
                    stream = self.open_file(self.get_file(url), "rb")
                    yield dict(url=url, stream=stream, local_path=self.last_path)
                except Exception as e:
                    if self.handler(e):
                        continue
                    else:
                        break


def cached_url_opener(
    data,
    handler=reraise_exception,
    cache_size=-1,
    cache_dir=None,
    url_to_name=pipe_cleaner,
    validator=check_tar_format,
    verbose=False,
    always=False,
):
    """Given a stream of url names (packaged in `dict(url=url)`), yield opened streams."""
    raise Exception("obsolete")
    verbose = verbose or verbose_cache
    for sample in data:
        assert isinstance(sample, dict), sample
        assert "url" in sample
        url = sample["url"]
        attempts = 5
        try:
            if not always and os.path.exists(url):
                dest = url
            else:
                dest = get_file_cached(
                    url,
                    cache_size=cache_size,
                    cache_dir=cache_dir,
                    url_to_name=url_to_name,
                    verbose=verbose,
                )
            if verbose:
                print("# opening %s" % dest, file=sys.stderr)
            assert os.path.exists(dest)
            if not validator(dest):
                ftype = get_filetype(dest)
                with open(dest, "rb") as f:
                    data = f.read(200)
                os.remove(dest)
                raise ValueError(
                    "%s (%s) is not a tar archive, but a %s, contains %s"
                    % (dest, url, ftype, repr(data))
                )
            try:
                stream = open(dest, "rb")
                sample.update(stream=stream)
                yield sample
            except FileNotFoundError as exn:
                # dealing with race conditions in lru_cleanup
                attempts -= 1
                if attempts > 0:
                    time.sleep(random.random() * 10)
                    continue
                raise exn
        except Exception as exn:
            exn.args = exn.args + (url,)
            if handler(exn):
                continue
            else:
                break


def cached_tarfile_samples(
    src,
    handler=reraise_exception,
    cache_size=-1,
    cache_dir=None,
    verbose=False,
    url_to_name=pipe_cleaner,
    always=False,
    select_files=None,
    rename_files=None,
):
    raise Exception("obsolete")
    verbose = verbose or int(os.environ.get("GOPEN_VERBOSE", 0))
    streams = cached_url_opener(
        src,
        handler=handler,
        cache_size=cache_size,
        cache_dir=cache_dir,
        verbose=verbose,
        url_to_name=url_to_name,
        always=always,
    )
    files = tar_file_expander(
        streams, handler=handler, select_files=select_files, rename_files=rename_files
    )
    samples = group_by_keys(files, handler=handler)
    return samples


cached_tarfile_to_samples = filters.pipelinefilter(cached_tarfile_samples)
