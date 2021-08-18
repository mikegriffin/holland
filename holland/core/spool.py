"""
Utilities to manage spool directory
"""

import errno
import itertools
import logging
import os
import shutil
import time
from string import Template
from textwrap import dedent

from holland.core.config import BaseConfig
from holland.core.util.fmt import format_bytes, format_datetime

LOG = logging.getLogger(__name__)


def timestamp_dir(when=None):
    """
    Returns a directory named after the specified time or
    the current time, if no specific time is given
    """
    if when is None:
        when = time.time()
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(when))


class Spool(object):
    """
    A directory spool where backups are saved
    """

    def __init__(self, path=None):
        self.path = path or "/var/spool/holland"

    def find_backup(self, name):
        """
        Find a the specified backup, if it exists. If the backup does
        not exist, returns None

        The backup name must be in <backupset>/<timestamp> format
        """
        try:
            backupset_name, timestamp = name.split("/")
            backupset = self.find_backupset(backupset_name)
            if backupset:
                return backupset.find_backup(timestamp)
        except ValueError as ex:
            LOG.warning("Invalid backup name: %s, Error: %s", name, ex)
        return None

    def add_backup(self, backupset_name):
        """
        Add a new backup to the specified backupset_name, which will also be
        initialized if it does not exist.

        The backup will only exist in memory until its 'flush' method is called
        """
        backupset = self.find_backupset(backupset_name)
        if not backupset:
            backupset = self.add_backupset(backupset_name)
        return backupset.add_backup()

    def find_backupset(self, backupset_name):
        """
        Find an existing backupset.

        If the backupset does not exist None is returned
        """
        path = os.path.join(self.path, backupset_name)
        if not os.path.exists(path):
            return None
        return Backupset(backupset_name, path)

    def add_backupset(self, backupset_name):
        """
        Add a new backupset to this spool.

        If the backupset already exists an IOError is raised
        """
        path = os.path.join(self.path, backupset_name)
        if os.path.exists(path):
            raise IOError("Backupset %s already exists" % backupset_name)
        return Backupset(backupset_name, path)

    def list_backupsets(self, name=None, reverse=False):
        """
        Return a list of backupsets under this spool in lexicographical order.

        If reverse is True, the results will be returned in descending lex
        order, ascending otherwise
        """
        if not os.path.exists(self.path):
            return []

        backupsets = []
        dirs = []
        if name:
            if not os.path.exists(os.path.join(self.path, name)):
                return []
            dirs = [name]
        else:
            dirs = [
                backupset
                for backupset in os.listdir(self.path)
                if os.path.isdir(os.path.join(self.path, backupset))
            ]

        backupsets = [Backupset(d, os.path.join(self.path, d)) for d in dirs]

        backupsets.sort(key=lambda x: x.name)

        if reverse:
            backupsets.reverse()

        return backupsets

    def list_backups(self, backupset_name=None):
        """
        Get list of backups
        """
        for backupset in self.list_backupsets(backupset_name):
            for backup in backupset.list_backups():
                yield backup

    def __iter__(self):
        """
        pass to list_backupsets
        """
        return iter(self.list_backupsets())


class Backupset(object):
    """
    Define backupset
    """

    def __init__(self, name, path):
        """
        A backupset should have a name and a path
        """
        self.name = name
        self.path = path

    def find_backup(self, name):
        """
        Create list of backups
        """
        backups = self.list_backups(name)
        if not backups:
            return None
        return backups[0]

    def add_backup(self):
        """
        Create a new instance for this job
        """
        backup_name = timestamp_dir()
        backup_path = os.path.join(self.path, backup_name)
        backup = Backup(backup_path, self.name, backup_name)
        backup.prepare()
        return backup

    def purge(self, retention_count=0):
        """
        Delete old backup
        """
        if retention_count < 0:
            raise ValueError("Invalid retention count %s" % retention_count)
        for backup in itertools.islice(self.list_backups(reverse=True), retention_count, None):
            backup.purge()
            yield backup

    def list_backups(self, name=None, reverse=False):
        """
        Return list of backups for this backupset in order of their
        creation date.
        """
        if not os.path.exists(self.path):
            return None

        name = (name or "").strip()

        backup_list = []
        if name:
            path = os.path.join(self.path, name)
            ret = []
            if os.path.exists(path):
                ret.append(Backup(path, self.name, name))
            return ret

        dirs = []
        for backup in os.listdir(self.path):
            path = os.path.join(self.path, backup)
            if os.path.isdir(path) and not os.path.islink(path):
                try:
                    time.strptime(backup, "%Y%m%d_%H%M%S")
                except ValueError:
                    continue
                dirs.append(backup)

        backup_list = [Backup(os.path.join(self.path, d), self.name, d) for d in dirs]

        backup_list.sort(key=lambda x: x.name)
        if reverse:
            backup_list.reverse()

        return backup_list

    def update_symlinks(self, enable=True):
        "Update symlinks for newest and oldest backup in the set"
        if not enable:
            return

        backups = self.list_backups()

        oldest_link = os.path.join(self.path, "oldest")
        newest_link = os.path.join(self.path, "newest")
        try:
            os.remove(oldest_link)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        try:
            os.remove(newest_link)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        if not backups:
            return
        oldest_path = backups[0].path
        newest_path = backups[-1].path
        os.symlink(oldest_path, oldest_link)
        os.symlink(newest_path, newest_link)

    def __iter__(self):
        return iter(self.list_backups())

    def __str__(self):
        return "%s [%s]" % (self.name, self.path)

    def __cmp__(self, other):
        _cmp = lambda x, y: (x > y) - (x < y)
        return _cmp(self.name, other.name)


CONFIGSPEC = """
[holland:backup]
plugin                  = string(default="")
start-time              = float(default=0)
stop-time               = float(default=0)
failed-backup           = boolean(default=no)
estimated-size          = float(default=0)
on-disk-size            = float(default=0)
estimated-size-factor   = float(default=1.0)
backups-to-keep         = integer(min=0, default=1)
auto-purge-failures     = boolean(default=yes)
purge-policy            = option(manual, before-backup, after-backup, default='after-backup')
purge-on-demand         = boolean(default=no)
before-backup-command   = string(default=None)
after-backup-command    = string(default=None)
failed-backup-command   = string(default=None)
historic-size           = boolean(default=yes)
historic-size-factor    = float(default=1.5)
historic-estimated-size-factor = float(default=1.1)
create-symlinks     = boolean(default=yes)
""".splitlines()


class Backup(object):
    """
    Representation of a backup instance.
    """

    def __init__(self, path, backupset, name):
        self.path = path
        self.backupset = backupset
        self.name = "/".join((backupset, name))
        # Initialize an empty config
        # This will not be loaded until load_config is called
        config_path = os.path.join(self.path, "backup.conf")
        self.config = BaseConfig({}, file_error=False)
        self.config.filename = config_path
        if os.path.exists(config_path):
            self.load_config()
        else:
            self.validate_config()

    def validate_config(self):
        """
        Validate configuration
        """
        self.config.validate_config(CONFIGSPEC, suppress_warnings=True)

    def load_config(self):
        """
        (Re)Load the config for this backup.
        """
        self.config.reload()
        self.validate_config()

    def purge(self):
        """
        Purge the entire backup directory
        """
        assert os.path.realpath(self.path) != "/"
        try:
            shutil.rmtree(self.path)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise

    def exists(self):
        """
        Check if this backup exists on disk
        """
        return os.path.exists(self.path)

    def prepare(self):
        """
        Prepare this backup on disk.  Ensures the path to this backup is created,
        but does not flush any other backup metadata.
        """
        os.makedirs(self.path)
        LOG.info("Creating backup path %s", self.path)

    def flush(self):
        """
        Flush this backup to disk.  Ensure the path to this backup is created
        and write the backup.conf to the backup directory.
        """
        LOG.debug("Writing out config to %s", self.config.filename)
        self.config.write()

    def _formatted_config(self):
        cfg = dict(self.config["holland:backup"])
        cfg["stop-time"] = format_datetime(cfg["stop-time"])
        cfg["start-time"] = format_datetime(cfg["start-time"])
        cfg["estimated-size"] = format_bytes(cfg["estimated-size"])
        cfg["on-disk-size"] = format_bytes(cfg["on-disk-size"])
        return cfg

    def info(self):
        """
        get plugin info
        """
        tmpl = Template(
            """
        backup-plugin   = ${plugin}
        backup-started  = ${start-time}
        backup-finished = ${stop-time}
        estimated size  = ${estimated-size}
        on-disk size    = ${on-disk-size}
        """
        )
        info_str = tmpl.safe_substitute(self._formatted_config())
        info_str = "\t" + dedent(info_str).lstrip()
        info_str = "\n\t\t".join(info_str.splitlines())
        return info_str

    def __str__(self):
        """
        format plugin info
        """
        return dedent(
            """
        Backup: %s
        start-time:     %s
        stop-time:      %s
        estimated-size: %s
        on-disk-size:   %s
        """
        ).strip() % (
            self.name,
            format_datetime(self.config.lookup("holland:backup.start-time")),
            format_datetime(self.config.lookup("holland:backup.stop-time")),
            format_bytes(self.config.lookup("holland:backup.estimated-size")),
            format_bytes(self.config.lookup("holland:backup.on-disk-size")),
        )

    def __cmp__(self, other):
        _cmp = lambda x, y: (x > y) - (x < y)
        return _cmp(
            self.config["holland:backup"]["start-time"],
            other.config["holland:backup"]["start-time"],
        )

    __repr__ = __str__


SPOOL = Spool()
