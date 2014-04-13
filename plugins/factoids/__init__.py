from twisted.internet import defer

from system.command_manager import CommandManager
from system.event_manager import EventManager
from system.plugin import PluginObject
from system.plugin_manager import YamlPluginManagerSingleton
from system.protocols.generic.channel import Channel
from system.storage.formats import DBAPI
from system.storage.manager import StorageManager

from utils import tokens

__author__ = 'Sean'

# Remember kids:
# * Stay in drugs
# * Eat your school
# * Don't do vegetables


class Plugin(PluginObject):

    CHANNEL = "channel"
    PROTOCOL = "protocol"
    GLOBAL = "global"

    PERM_ADD = "factoids.add.%s"
    PERM_SET = "factoids.set.%s"
    PERM_DEL = "factoids.delete.%s"
    PERM_GET = "factoids.get.%s"

    (RES_INVALID_LOCATION,
     RES_INVALID_METHOD,  # _FOR_LOCATION - i.e. CHANNEL in PM
     RES_NO_PERMS,
     RES_MISSING_FACTOID) = xrange(4)

    def setup(self):
        ### Grab important shit
        self.commands = CommandManager()
        self.events = EventManager()
        self.storage = StorageManager()
        self.plugman = YamlPluginManagerSingleton()

        ### Set up database
        self.database = self.storage.get_file(
            self,
            "data",
            DBAPI,
            "sqlite3:data/plugins/factoids.sqlite",
            "data/plugins/factoids.sqlite"
        )
        with self.database as db:
            db.runQuery("CREATE TABLE IF NOT EXISTS factoids ("
                        "factoid_key TEXT, "
                        "location TEXT, "
                        "protocol TEXT, "
                        "channel TEXT, "
                        "factoid_name TEXT, "
                        "info TEXT, "
                        "UNIQUE(factoid_key, location, protocol, channel) "
                        "ON CONFLICT REPLACE)")

        ### Register commands
        # We have multiple possible permissions per command, so we have to do
        # permission handling ourselves
        self.commands.register_command("addfactoid",
                                       self.factoid_add_command,
                                       self,
                                       None)
        self.commands.register_command("setfactoid",
                                       self.factoid_set_command,
                                       self,
                                       None)
        self.commands.register_command("deletefactoid",
                                       self.factoid_delete_command,
                                       self,
                                       None,
                                       ["delfactoid"])
        self.commands.register_command("getfactoid",
                                       self.factoid_get_command,
                                       self,
                                       None)

        ### Register events
        self.events.add_callback("MessageReceived",
                                 self,
                                 self.message_handler,
                                 1)

        self.events.add_callback("Web/ServerStartedEvent",
                                 self,
                                 self.web_routes,
                                 1)

    # region Util functions

    def __check_perm(self, perm, caller, source, protocol):
        self.logger.debug("Checking for perm: '%s'", perm)
        # The second check is a hack to check default group, since there is not
        # currently inheritance
        # TODO: Remove this hack once inheritance has been added
        # Once this hack is not needed, this method can be removed and every
        # call can use the permission handler directly
        allowed = self.commands.perm_handler.check(perm,
                                                   caller,
                                                   source,
                                                   protocol)
        if not allowed:
            allowed = self.commands.perm_handler.check(perm,
                                                       None,
                                                       source,
                                                       protocol)
        return allowed

    def _parse_args(self, raw_args):
        """
        Grabs the location, factoid name, and info from a raw_args string
        """
        pos = raw_args.find(" ")
        if pos < 0:
            raise ValueError("Invalid args")
        location = raw_args[:pos]
        pos2 = raw_args.find(" ", pos + 1)
        if pos2 < 0:
            raise ValueError("Invalid args")
        factoid = raw_args[pos + 1:pos2]
        pos3 = raw_args.find(" ", pos2 + 1)
        info = raw_args[pos2 + 1:]
        if info == "":
            raise ValueError("Invalid args")
        return location, factoid, info

    def valid_location(self, location, source=None):
        """
        Checks if a given location is one of channel, protocol or global, and
        if it's a channel request, that it's in a channel.
        """
        location = location.lower()
        result = location in (self.CHANNEL, self.PROTOCOL, self.GLOBAL)
        if not result:
            raise InvalidLocationError("'%s' is not a valid location" %
                                       location)
        if source is not None:
            if location == self.CHANNEL and not isinstance(source, Channel):
                raise InvalidMethodError("'channel' location can only be used "
                                         "inside a channel")
        return True

    # endregion

    # region API functions to access factoids

    def _add_factoid_interaction(self, txn, factoid_key, location, protocol,
                                 channel, factoid, info):
        """
        Appends a factoid to an existing one if there, otherwise creates it.
        :return: True if already exists, otherwise False
        """
        txn.execute("SELECT * FROM factoids WHERE "
                    "factoid_key = ? AND location = ? AND "
                    "protocol = ? AND channel = ?",
                    (factoid_key, location, protocol, channel))
        results = txn.fetchall()
        if len(results) == 0:
            # Factoid doesn't exist yet, create it
            txn.execute("INSERT INTO factoids VALUES(?, ?, ?, ?, ?, ?)",
                        (
                            factoid_key,
                            location,
                            protocol,
                            channel,
                            factoid,
                            info
                        ))
            return False
        else:
            # Factoid already exists, append
            txn.execute("INSERT INTO factoids VALUES(?, ?, ?, ?, ?, ?)",
                        (
                            results[0][0],
                            results[0][1],
                            results[0][2],
                            results[0][3],
                            results[0][4],
                            results[0][5] + "\n" + info
                        ))
            return True

    def _delete_factoid_interaction(self, txn, factoid_key, location, protocol,
                                    channel):
        """
        Deletes a factoid if it exists, otherwise raises MissingFactoidError
        """

        self.logger.debug("DELETE | Key: %s | Loc: %s | Pro: %s | Cha: %s"
                          % (factoid_key, location, protocol, channel))

        if location == self.CHANNEL:
            txn.execute("DELETE FROM factoids WHERE factoid_key = ? AND "
                        "location = ? AND protocol = ? AND channel = ?",
                        (factoid_key, location, protocol, channel))
        else:
            txn.execute("DELETE FROM factoids WHERE factoid_key = ? AND "
                        "location = ? AND protocol = ?",
                        (factoid_key, location, protocol))
        if txn.rowcount == 0:
            raise MissingFactoidError("Factoid '%s' does not exist" %
                                      factoid_key)

    def _get_factoid_interaction(self, txn, factoid_key, location, protocol,
                                 channel):
        """
        Gets a factoid if it exists, otherwise raises MissingFactoidError
        :return: (factoid_name, [entry, entry, ...])
        """
        self.logger.debug("Getting factoid params: factoid_key = '%s', "
                          "location = '%s', protocol = '%s', channel = '%s'",
                          factoid_key,
                          location,
                          protocol,
                          channel)
        if location is None:
            self.logger.debug("Location is None - getting all factoids with "
                              "key '%s'", factoid_key)
            txn.execute("SELECT location, protocol, channel, factoid_name, "
                        "info FROM factoids WHERE factoid_key = ?",
                        (factoid_key,))
            results = txn.fetchall()
            if len(results) > 0:
                # Check for channel match
                for row in results:
                    if ((row[0] == self.CHANNEL and row[1] == protocol and
                         row[2] == channel)):
                        self.logger.debug("Match found (channel)!")
                        return (row[3], row[4].split("\n"))
                # Check for protocol match
                for row in results:
                    if row[0] == self.PROTOCOL and row[1] == protocol:
                        self.logger.debug("Match found (protocol)!")
                        return (row[3], row[4].split("\n"))
                # Check for global match
                for row in results:
                    if row[0] == self.GLOBAL:
                        self.logger.debug("Match found (global)!")
                        return (row[3], row[4].split("\n"))
        else:
            txn.execute("SELECT location, protocol, channel, factoid_name, "
                        "info FROM factoids WHERE factoid_key = ? AND "
                        "location = ? AND protocol = ? AND channel = ?",
                        (factoid_key, location, protocol, channel))
            results = txn.fetchall()
            if len(results) > 0:
                return (results[0][3], results[0][4].split("\n"))
        raise MissingFactoidError("Factoid '%s' does not exist" % factoid_key)

    def _get_all_factoids_interaction(self, txn):
        """
        Gets all factoids
        :return: (factoid_name, [entry, entry, ...])
        """
        self.logger.debug("Getting all factoids.")
        txn.execute("SELECT location, protocol, channel, factoid_name, "
                    "info FROM factoids")
        results = txn.fetchall()
        return results

    def get_all_factoids(self):
        with self.database as db:
            return db.runInteraction(self._get_all_factoids_interaction)

    def add_factoid(self, caller, source, protocol, location, factoid, info):
        location = location.lower()
        factoid_key = factoid.lower()
        protocol_key = protocol.name.lower()
        channel_key = source.name.lower()
        try:
            valid = location is None or self.valid_location(location, source)
        except Exception as ex:
            return defer.fail(ex)
        if not self.__check_perm(self.PERM_ADD % location,
                                 caller,
                                 source,
                                 protocol):
            return defer.fail(
                NoPermissionError("User does not have required permission"))
        with self.database as db:
            return db.runInteraction(self._add_factoid_interaction,
                                     factoid_key,
                                     location,
                                     protocol_key,
                                     channel_key,
                                     factoid,
                                     info)

    def set_factoid(self, caller, source, protocol, location, factoid, info):
        location = location.lower()
        factoid_key = factoid.lower()
        protocol_key = protocol.name.lower()
        channel_key = source.name.lower()
        try:
            valid = location is None or self.valid_location(location, source)
        except Exception as ex:
            return defer.fail(ex)
        if not self.__check_perm(self.PERM_SET % location,
                                 caller,
                                 source,
                                 protocol):
            return defer.fail(
                NoPermissionError("User does not have required permission"))
        with self.database as db:
            return db.runQuery(
                "INSERT INTO factoids VALUES(?, ?, ?, ?, ?, ?)",
                (
                    factoid_key,
                    location,
                    protocol_key,
                    channel_key,
                    factoid,
                    info
                ))

    def delete_factoid(self, caller, source, protocol, location, factoid):
        location = location.lower()
        factoid_key = factoid.lower()
        protocol_key = protocol.name.lower()
        channel_key = source.name.lower()
        try:
            valid = location is None or self.valid_location(location, source)
        except Exception as ex:
            return defer.fail(ex)
        if not self.__check_perm(self.PERM_DEL % location,
                                 caller,
                                 source,
                                 protocol):
            return defer.fail(
                NoPermissionError("User does not have required permission"))
        with self.database as db:
            return db.runInteraction(self._delete_factoid_interaction,
                                     factoid_key,
                                     location,
                                     protocol_key,
                                     channel_key)

    def get_factoid(self, caller, source, protocol, location, factoid):
        if location is not None:
            location = location.lower()
        factoid_key = factoid.lower()
        protocol_key = protocol.name.lower()
        channel_key = source.name.lower()
        try:
            valid = location is None or self.valid_location(location, source)
        except Exception as ex:
            return defer.fail(ex)
        if not self.__check_perm(self.PERM_GET % location,
                                 caller,
                                 source,
                                 protocol):
            return defer.fail(
                NoPermissionError("User does not have required permission"))
        with self.database as db:
            return db.runInteraction(self._get_factoid_interaction,
                                     factoid_key,
                                     location,
                                     protocol_key,
                                     channel_key)

    # endregion

    # region Command handlers for interacting with factoids

    def _factoid_command_fail(self, caller, failure):
        """
        :type failure: twisted.python.failure.Failure
        """
        if failure.check(InvalidLocationError):
            caller.respond("Invalid location given - possible locations are: "
                           "channel, protocol, global")
        elif failure.check(InvalidMethodError):
            caller.respond("You must do that in a channel")
        elif failure.check(NoPermissionError):
            caller.respond("You don't have permission to do that")
        elif failure.check(MissingFactoidError):
            caller.respond("That factoid doesn't exist")
        else:
            # TODO: We should probably handle this
            failure.raiseException()

    def _factoid_get_command_success(self, source, result, args=None):
        if not args:
            args = []

        for line in result[1]:
            _tokens = tokens.find_tokens(line)
            _numerical = tokens.find_numerical_tokens(line)

            for i, arg in enumerate(args):
                line = line.replace("{%d}" % i, arg)

            for token in _numerical:
                line = line.replace(token, "")

            # TODO: Token handlers
            source.respond("(%s) %s" % (result[0], line))

    def factoid_add_command(self, protocol, caller, source, command, raw_args,
                            parsed_args):
        try:
            location, factoid, info = self._parse_args(raw_args)
        except:
            caller.respond("Usage: %s <location> <factoid> <info>" % command)
            return
        d = self.add_factoid(caller, source, protocol, location, factoid, info)
        d.addCallbacks(
            lambda r: caller.respond("Factoid added"),
            lambda f: self._factoid_command_fail(caller, f)
        )

    def factoid_set_command(self, protocol, caller, source, command, raw_args,
                            parsed_args):
        try:
            location, factoid, info = self._parse_args(raw_args)
        except Exception as ex:
            caller.respond("Usage: %s <location> <factoid> <info>" % command)
            return
        d = self.set_factoid(caller, source, protocol, location, factoid, info)
        d.addCallbacks(
            lambda r: caller.respond("Factoid set"),
            lambda f: self._factoid_command_fail(caller, f)
        )

    def factoid_delete_command(self, protocol, caller, source, command,
                               raw_args, parsed_args):
        args = raw_args.split()  # Quick fix for new command handler signature
        if len(args) != 2:
            caller.respond("Usage: %s <location> <factoid>" % command)
            return
        location = args[0]
        factoid = args[1]
        d = self.delete_factoid(caller, source, protocol, location, factoid)
        d.addCallbacks(
            lambda r: caller.respond("Factoid deleted"),
            lambda f: self._factoid_command_fail(caller, f)
        )

    def factoid_get_command(self, protocol, caller, source, command, raw_args,
                            parsed_args):
        args = raw_args.split()  # Quick fix for new command handler signature
        if len(args) == 1:
            factoid = args[0]
            location = None
        elif len(args) == 2:
            location = args[0]
            factoid = args[1]
        else:
            caller.respond("Usage: %s [location] <factoid>" % command)
            return

        d = self.get_factoid(caller, source, protocol, location, factoid)
        d.addCallbacks(
            lambda r: self._factoid_get_command_success(source, r),
            lambda f: self._factoid_command_fail(caller, f)
        )

    # endregion

    def _print_query(self, result):
        from pprint import pprint
        pprint(result)

    def web_routes(self, event=None):
        self.logger.info("Registering web routes..")

        web = self.plugman.getPluginByName("Web").plugin_object

        web.add_route("/factoids", ["GET", "POST"], self.web_factoids)
        web.add_route("/factoids/", ["GET", "POST"], self.web_factoids)

        web.add_navbar_entry("Factoids", "/factoids/")

    def web_factoids_callback_success(self, result, y, objs):
        web = self.plugman.getPluginByName("Web").plugin_object

        fragment = "<table class=\"table table-striped table-bordered" \
                   " table-sortable\">"
        fragment += "<thead>" \
                    "<tr>" \
                    "<th>Location</th>" \
                    "<th>Protocol</th>" \
                    "<th>Channel</th>" \
                    "<th>Name</th>" \
                    "<th>Content</th>" \
                    "</tr></thead>" \
                    "<tbody>"
        for row in result:
            fragment += "<tr>"

            for column in row:
                fragment += "<td>%s</td>" % column.replace("\n",
                                                           "<br /><br />")
            fragment += "</tr>"

        fragment += "</tbody>" \
                    "</table>"

        y.data = web.wrap_template(fragment, "Factoids", "Factoids", r=objs)

    def web_factoids_callback_fail(self, failure, y, objs):
        web = self.plugman.getPluginByName("Web").plugin_object

        y.data = web.wrap_template("Error: %s" % failure, "Factoids",
                                   "Factoids", r=objs)

    def web_factoids(self):
        web = self.plugman.getPluginByName("Web").plugin_object
        objs = web.get_objects()

        r = web.check_permission(self.PERM_GET % "web", r=objs)

        self.logger.debug("WEB | Checking permissions..")

        if not r:
            self.logger.debug("WEB | User does not have permission. "
                              "Are they logged in?")
            r = web.require_login(r=objs)

            if not r[0]:
                return r[1]

            self.logger.debug("WEB | Yup. They must not have permission.")
            self.logger.debug("WEB | Presenting error..")

            if not r:
                return web.wrap_template("Error: You do not have permission "
                                         "to list the factoids.", "Factoids",
                                         "Factoids")

        self.logger.debug("WEB | User has permission.")

        d = self.get_all_factoids()
        y = web.get_yielder()

        d.addCallbacks(self.web_factoids_callback_success,
                       self.web_factoids_callback_fail,
                       callbackArgs=(y, objs),
                       errbackArgs=(y, objs))

        return y

    def message_handler(self, event):
        """
        Handle ??-style factoid "commands"
        :type event: MessageReceived
        """
        handlers = {
            "??": self._message_handler_get,
            "?<": self._message_handler_get_self,
            "?>": self._message_handler_get_other,
            "??+": self._message_handler_add,
            "??~": self._message_handler_set,
            "??-": self._message_handler_delete,
            "!?+": self._message_handler_add_global,
            "!?~": self._message_handler_set_global,
            "!?-": self._message_handler_delete_global,
            "@?+": self._message_handler_add_protocol,
            "@?~": self._message_handler_set_protocol,
            "@?-": self._message_handler_delete_protocol
        }
        msg = event.message
        command = None
        factoid = ""
        args = ""
        pos = msg.find(" ")
        split = msg.split(" ")
        if pos < 0:
            command = msg
        else:
            command = msg[:pos]
            pos2 = msg.find(" ", pos + 1)
            if pos2 < 0:
                factoid = msg[pos + 1:].strip()
            else:
                factoid = msg[pos + 1:pos2].strip()
                args = msg[pos2 + 1:].strip()
        if command in handlers:
            handlers[command](command, factoid, args, event, split)

    ### Getting "commands"

    def _message_handler_get(self, command, factoid, args, event, split):
        """
        Handle ?? factoid "command"
        :type event: MessageReceived
        """
        if not factoid:
            event.source.respond("Usage: ?? <factoid>")
            return
        d = self.get_factoid(event.source,
                             event.target,
                             event.caller,
                             None,
                             factoid)
        d.addCallbacks(
            lambda r: self._factoid_get_command_success(event.target, r,
                                                        split[2:]),
            lambda f: self._factoid_command_fail(event.source, f)
        )

    def _message_handler_get_self(self, command, factoid, args, event, split):
        """
        Handle ?< factoid "command"
        :type event: MessageReceived
        """
        if not factoid:
            event.source.respond("Usage: ?< <factoid>")
            return
        d = self.get_factoid(event.source,
                             event.target,
                             event.caller,
                             None,
                             factoid)
        d.addCallbacks(
            lambda r: self._factoid_get_command_success(event.source, r,
                                                        split[2:]),
            lambda f: self._factoid_command_fail(event.source, f)
        )

    def _message_handler_get_other(self, command, factoid, args, event, split):
        """
        Handle ?> factoid "command"
        :type event: MessageReceived
        """
        if not len(split) > 2:
            event.source.respond("Usage: ?> <user> <factoid>")
            return

        wanted = split[1]
        factoid = split[2]
        user = event.caller.get_user(wanted)

        if user is None:
            event.source.respond("Unable to find that user.")
            return

        d = self.get_factoid(event.source,
                             event.target,
                             event.caller,
                             None,
                             factoid)
        d.addCallbacks(
            lambda r: self._factoid_get_command_success(user, r, split[3:]),
            lambda f: self._factoid_command_fail(event.source, f)
        )

    ### Channel "commands"

    def _message_handler_add(self, command, factoid, args, event, split):
        """
        Handle ??+ factoid "command"
        :type event: MessageReceived
        """
        if not factoid or not args:
            event.source.respond("Usage: ??+ <factoid> <info>")
            return
        d = self.add_factoid(event.source,
                             event.target,
                             event.caller,
                             self.CHANNEL,
                             factoid,
                             args)
        d.addCallbacks(
            lambda r: event.source.respond("Factoid added"),
            lambda f: self._factoid_command_fail(event.source, f)
        )

    def _message_handler_set(self, command, factoid, args, event, split):
        """
        Handle ??~ factoid "command"
        :type event: MessageReceived
        """
        if not factoid or not args:
            event.source.respond("Usage: ??~ <factoid> <info>")
            return
        d = self.set_factoid(event.source,
                             event.target,
                             event.caller,
                             self.CHANNEL,
                             factoid,
                             args)
        d.addCallbacks(
            lambda r: event.source.respond("Factoid set"),
            lambda f: self._factoid_command_fail(event.source, f)
        )

    def _message_handler_delete(self, command, factoid, args, event, split):
        """
        Handle ??- factoid "command"
        :type event: MessageReceived
        """
        if factoid is None:
            event.source.respond("Usage: ??- <factoid>")
            return
        d = self.delete_factoid(event.source,
                                event.target,
                                event.caller,
                                self.CHANNEL,
                                factoid)
        d.addCallbacks(
            lambda r: event.source.respond("Factoid deleted"),
            lambda f: self._factoid_command_fail(event.source, f)
        )

    ### Global "commands"

    def _message_handler_add_global(self, command, factoid, args, event,
                                    split):
        """
        Handle !?+ factoid "command"
        :type event: MessageReceived
        """
        if not factoid or not args:
            event.source.respond("Usage: !?+ <factoid> <info>")
            return
        d = self.add_factoid(event.source,
                             event.target,
                             event.caller,
                             self.GLOBAL,
                             factoid,
                             args)
        d.addCallbacks(
            lambda r: event.source.respond("Factoid added"),
            lambda f: self._factoid_command_fail(event.source, f)
        )

    def _message_handler_set_global(self, command, factoid, args, event,
                                    split):
        """
        Handle !?~ factoid "command"
        :type event: MessageReceived
        """
        if not factoid or not args:
            event.source.respond("Usage: !?~ <factoid> <info>")
            return
        d = self.set_factoid(event.source,
                             event.target,
                             event.caller,
                             self.GLOBAL,
                             factoid,
                             args)
        d.addCallbacks(
            lambda r: event.source.respond("Factoid set"),
            lambda f: self._factoid_command_fail(event.source, f)
        )

    def _message_handler_delete_global(self, command, factoid, args, event,
                                       split):
        """
        Handle !?- factoid "command"
        :type event: MessageReceived
        """
        if factoid is None:
            event.source.respond("Usage: !?- <factoid>")
            return
        d = self.delete_factoid(event.source,
                                event.target,
                                event.caller,
                                self.GLOBAL,
                                factoid)
        d.addCallbacks(
            lambda r: event.source.respond("Factoid deleted"),
            lambda f: self._factoid_command_fail(event.source, f)
        )

    ### Protocol-specific "commands"

    def _message_handler_add_protocol(self, command, factoid, args, event,
                                      split):
        """
        Handle @?+ factoid "command"
        :type event: MessageReceived
        """
        if not factoid or not args:
            event.source.respond("Usage: @?+ <factoid> <info>")
            return
        d = self.add_factoid(event.source,
                             event.target,
                             event.caller,
                             self.PROTOCOL,
                             factoid,
                             args)
        d.addCallbacks(
            lambda r: event.source.respond("Factoid added"),
            lambda f: self._factoid_command_fail(event.source, f)
        )

    def _message_handler_set_protocol(self, command, factoid, args, event,
                                      split):
        """
        Handle @?~ factoid "command"
        :type event: MessageReceived
        """
        if not factoid or not args:
            event.source.respond("Usage: @?~ <factoid> <info>")
            return
        d = self.set_factoid(event.source,
                             event.target,
                             event.caller,
                             self.PROTOCOL,
                             factoid,
                             args)
        d.addCallbacks(
            lambda r: event.source.respond("Factoid set"),
            lambda f: self._factoid_command_fail(event.source, f)
        )

    def _message_handler_delete_protocol(self, command, factoid, args, event,
                                         split):
        """
        Handle @?- factoid "command"
        :type event: MessageReceived
        """
        if factoid is None:
            event.source.respond("Usage: @?- <factoid>")
            return
        d = self.delete_factoid(event.source,
                                event.target,
                                event.caller,
                                self.PROTOCOL,
                                factoid)
        d.addCallbacks(
            lambda r: event.source.respond("Factoid deleted"),
            lambda f: self._factoid_command_fail(event.source, f)
        )


class FactoidsError(Exception):
    pass


class InvalidLocationError(FactoidsError):
    pass


class InvalidMethodError(FactoidsError):
    pass


class NoPermissionError(FactoidsError):
    pass


class MissingFactoidError(FactoidsError):
    pass
