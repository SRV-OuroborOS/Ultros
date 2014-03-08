__author__ = 'Gareth Coles'

import system.storage.config as Config
import system.storage.data as Data

import system.storage.formats as Formats

from system.storage.exceptions import NotReadyError

file_formats_map = {
    "config": {
        Formats.JSON: Config.JSONConfig,
        Formats.MEMORY: Config.MemoryConfig,
        Formats.YAML: Config.YamlConfig
    },
    "data": {
        Formats.JSON: Data.JSONData,
        Formats.MEMORY: Data.MemoryData,
        Formats.SQLITE: Data.SqliteData,
        Formats.YAML: Data.YamlData
    }
}


class StorageFile(object):
    _ready = False
    _owner = None

    formats = []
    file_type = "data"

    obj = None

    path = ""
    type_ = ""

    def __init__(self, type_, path, base_path, manager_class):
        if type_ not in self.formats:
            raise TypeError("Type '%s' is unknown or not supported for %s "
                            "files." % (type_, self.file_type))

        self.path = "%s/%s" % (base_path, path)
        self.type_ = type_
        self.manager_class = manager_class

    def load(self):
        self.obj = file_formats_map[self.file_type][self.type_](self.path)

    def get(self):
        if self._ready and self.obj:
            return self.obj
        else:
            raise NotReadyError("This file is not ready for use.")

    def is_ready(self):
        return self._ready

    def make_ready(self, caller):
        if isinstance(caller, self.manager_class):
            self._ready = True
        else:
            raise TypeError("Only the storage manager can make a file ready.")

    def set_owner(self, caller, owner):
        if isinstance(caller, self.manager_class):
            self._owner = owner
        else:
            raise TypeError("Only the storage manager can set the file owner.")

    def get_owner(self):
        return self._owner

    def is_owner(self, candidate):
        return isinstance(candidate, self._owner.__class__)


class DataFile(StorageFile):
    formats = Formats.DATA
    file_type = "data"


class ConfigFile(StorageFile):
    formats = Formats.CONF
    file_type = "config"
