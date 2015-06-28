__author__ = 'Gareth Coles'

import os
import re
import socket

from bs4 import BeautifulSoup
from cookielib import LWPCookieJar, LoadError
from kitchen.text.converters import to_unicode, to_bytes
from netaddr import IPAddress
from twisted.web._newclient import ResponseNeverReceived
from txrequests import Session

from plugins.urls.handlers.handler import URLHandler
from utils.misc import str_to_regex_flags


class WebsiteHandler(URLHandler):
    name = "website"

    criteria = {
        "protocol": re.compile(r"http|https", str_to_regex_flags("iu"))
    }

    single_sessions = {}
    group_sessions = {}
    global_session = None

    cookies_base_path = "data/plugins/urls/cookies"

    def __init__(self, plugin):
        super(WebsiteHandler, self).__init__(plugin)

        if not os.path.exists(self.cookies_base_path):
            os.makedirs(self.cookies_base_path)

        if not os.path.exists(self.cookies_base_path + "/domains"):
            os.makedirs(self.cookies_base_path + "/domains")

        if not os.path.exists(self.cookies_base_path + "/groups"):
            os.makedirs(self.cookies_base_path + "/groups")

        self.global_session = Session()
        self.global_session.cookies = self.get_cookie_jar("/global.txt")

    def call(self, url, context):
        # TODO: Channel settings
        # TODO: Decide what to do on missing content-type

        if self.check_blacklist(url, context):
            self.urls_plugin.logger.warn(
                "URL %s is blacklisted, ignoring.." % url.text
            )
            return

        try:
            ip = IPAddress(socket.gethostbyname(url.domain))
        except Exception as e:
            context["event"].target.respond(
                '"{0}" at {1}'.format(e, url.domain)
            )

            self.plugin.logger.warn(str(e))
            return False

        if ip.is_loopback() or ip.is_private() or ip.is_link_local():
            self.plugin.logger.warn("Prevented a port-scan")
            return False

        headers = {}

        if url.domain in context["config"]["spoofing"]:
            user_agent = context["config"]["spoofing"][url.domain]

            if user_agent:
                headers["User-Agent"] = user_agent
        else:
            headers["User-Agent"] = ("Mozilla/5.0 (X11; U; Linux i686; "
                                     "en-US; rv:1.9.0.1) Gecko/20080716"
                                     "15 Fedora/3.0.1-1.fc9-1.fc9 "
                                     "Firefox/3.0.1")

        domain_langs = context.get("config") \
            .get("accept_language", {}) \
            .get("domains", {})

        if url.domain in domain_langs:
            headers["Accept-Language"] = domain_langs.get(url.domain)
        else:
            headers["Accept-Language"] = context.get("config") \
                .get("accept_language", {}) \
                .get("default", "en")

        session = self.get_session(url, context)
        session.get(url.text, headers=headers) \
            .addCallback(self.callback, url, context, session) \
            .addErrback(self.errback, url, context, session)

        return False

    def callback(self, response, url, context, session):
        self.plugin.logger.trace(
            "Headers: {0}", list(response.headers)
        )
        self.plugin.logger.trace("HTTP code: {0}", response.status_code)

        if "content-type" not in response.headers:
            return  # TODO: Decide what to do here

        content_type = response.headers["content-type"].lower()

        if ";" in content_type:
            content_type = content_type.split(";")[0]

        if content_type not in context["config"]["content_types"]:
            self.plugin.logger.debug(
                "Unsupported Content-Type: %s"
                % response.headers["content-type"]
            )
            return  # Not a supported content-type

        soup = BeautifulSoup(response.text)

        if soup.title and soup.title.string:
            title = soup.title.string.strip()
            title = re.sub("\s+", " ", title)
            title = title

            context["event"].target.respond(
                to_unicode('"{0}" at {1}'.format(
                    to_bytes(title), to_bytes(url.domain)
                ))
            )
        else:
            self.plugin.logger.debug("No title")

        session.cookies.save(ignore_discard=True)

    def errback(self, error, url, context, session):
        self.plugin.logger.error("Error parsing URL")

        if isinstance(error.value, ResponseNeverReceived):
            for f in error.value.reasons:
                f.printDetailedTraceback()
                context["event"].target.respond(
                    '"{0}" at {1}'.format(f.getErrorMessage(), url.domain)
                )
        else:
            context["event"].target.respond(
                '"{0}" at {1}'.format(error.getErrorMessage(), url.domain)
            )
            error.printDetailedTraceback()

        session.cookies.save(ignore_discard=True)

    def check_blacklist(self, url, context):
        for entry in context["config"]["blacklist"]:
            r = re.compile(entry, flags=str_to_regex_flags("i"))

            if r.match(url):
                self.urls_plugin.logger.debug(
                    "Matched blacklist regex: %s" % entry
                )
                return True

        return False

    def get_cookie_jar(self, filename):
        cj = LWPCookieJar(self.cookies_base_path + filename)

        try:
            cj.load()
        except LoadError as e:
            self.plugin.logger.exception(
                "Failed to load cookie jar {0}".format(filename)
            )
        except IOError as e:
            self.plugin.logger.debug(
                "Failed to load cookie jar {0}: {1}".format(filename, e)
            )
            pass  # Cookie jar just doesn't exist

        return cj

    def get_session(self, url, context):
        sessions = context["config"]["sessions"]

        if not sessions["enable"]:
            self.urls_plugin.logger.debug("Sessions are disabled.")
            return Session()

        for entry in sessions["never"]:
            if re.match(entry, url.domain, flags=str_to_regex_flags("i")):
                self.urls_plugin.logger.debug(
                    "Domain {0} is blacklisted for sessions.".format(
                        url.domain
                    )
                )
                return Session()

        for entry in sessions["single"]:
            if re.match(entry, url.domain, flags=str_to_regex_flags("i")):
                self.urls_plugin.logger.debug(
                    "Domain {0} has its own session storage.".format(
                        url.domain
                    )
                )

                if entry not in self.single_sessions:
                    self.single_sessions[entry] = Session()
                    self.single_sessions[entry].cookies = self.get_cookie_jar(
                        "/domains/{0}.txt".format(
                            entry
                        )
                    )

                return self.single_sessions[entry]

        for group, entries in sessions["group"].iteritems():
            for entry in entries:
                if re.match(entry, url.domain, flags=str_to_regex_flags("i")):
                    self.urls_plugin.logger.debug(
                        "Domain {0} uses the '{1}' group sessions.".format(
                            url.domain, group
                        )
                    )

                    if group not in self.group_sessions:
                        self.group_sessions[group] = Session()
                        self.group_sessions[group].cookies = (
                            self.get_cookie_jar(
                                "/groups/{0}.txt".format(
                                    group
                                )
                            )
                        )

                    return self.group_sessions[group]

        self.urls_plugin.logger.debug(
            "Domain {0} uses the global session storage.".format(
                url.domain
            )
        )

        return self.global_session

    def teardown(self):
        # Save all our cookie stores
        self.global_session.cookies.save(ignore_discard=True)
        self.global_session.close()

        for session in self.single_sessions.itervalues():
            session.cookies.save(ignore_discard=True)
            session.close()

        for session in self.group_sessions.itervalues():
            session.cookies.save(ignore_discard=True)
            session.close()
