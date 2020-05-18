#!/usr/bin/python
#
# (C) 2020 Count Count
#
# Distributed under the terms of the MIT license.

from typing import Any, Iterator, Callable, Pattern, Dict
from datetime import datetime
from datetime import timedelta
import os
import re
import time
import signal
import locale
import traceback
import pywikibot
from pywikibot.bot import SingleSiteBot
from pywikibot.diff import PatchManager
from pywikibot.comms.eventstreams import site_rc_listener
from vpncheck import CheckException, VpnCheck
from socket import gaierror, gethostbyname
import errno

TIMEOUT = 600  # We expect at least one rc entry every 10 minutes


class ReadingRecentChangesTimeoutError(Exception):
    pass


def on_timeout(signum: Any, frame: Any) -> None:
    raise ReadingRecentChangesTimeoutError


class Controller(SingleSiteBot):
    def __init__(self) -> None:
        site = pywikibot.Site()
        site.login()
        super(Controller, self).__init__(site=site)
        self.generator = FaultTolerantLiveRCPageGenerator(self.site)
        self.rollbackRegex = re.compile(r"Änderungen von \[\[(?:Special:Contributions|Spezial:Beiträge)/([^|]+)\|.+")
        self.undoRegex = re.compile(r"Änderung [0-9]+ von \[\[Special:Contribs/([^|]+)\|.+")
        self.vmUserTemplateRegex = re.compile(r"{{Benutzer\|([^}]+)}}")
        self.vpnCheck = VpnCheck()
        self.vmPage = pywikibot.Page(self.site, "Wikipedia:Vandalismusmeldung", 4)
        self.lastBlockEventsCheckTime = datetime.utcnow()

    def setup(self) -> None:
        """Setup the bot."""
        if os.name != "nt":
            signal.signal(signal.SIGALRM, on_timeout)  # pylint: disable=E1101
            signal.alarm(TIMEOUT)  # pylint: disable=E1101

    def skip_page(self, page: pywikibot.Page) -> bool:
        """Skip special/media pages"""
        if page.namespace() < 0:
            return True
        elif not page.exists():
            return True
        elif page.isRedirectPage():
            return True
        return super().skip_page(page)

    def treatVmPageChange(self, oldRevision, newRevision) -> None:
        oldText = self.vmPage.getOldVersion(oldRevision)
        newText = self.vmPage.getOldVersion(newRevision)
        oldVersionTemplateInstances = set(re.findall(self.vmUserTemplateRegex, oldText))
        newVersionTemplateInstances = set(re.findall(self.vmUserTemplateRegex, newText))
        newReportedUsers = newVersionTemplateInstances.difference(oldVersionTemplateInstances)
        for username in newReportedUsers:
            username = username.strip()
            pwUser = pywikibot.User(self.site, username)
            if pwUser.isAnonymous():
                checkRes = self.vpnCheck.checkWithIpCheck(username)
                vpnOrProxy = checkRes.score >= 2
                staticIp = not self.isDynamicIp(username)
                removeOneBlock = pwUser.isBlocked(force=True)
                blockCount = self.getBlockCount(pwUser)
                if removeOneBlock:
                    blockCount -= 1
                print(f"VM - Added IP: {username} Static: {staticIp} VPN: {vpnOrProxy} Previous blocks: {blockCount}")

    def getBlockCount(self, pwUser: pywikibot.User) -> int:
        events = self.site.logevents(page=f"User:{pwUser.username}", logtype="block")
        blockCount = 0
        for ev in events:
            if ev.type() == "block" and ev.action() == "block":
                blockCount += 1
        return blockCount

    def isIpV6(self, ip: str) -> bool:
        return ip.find(":") != -1

    def isDynamicIp(self, ip: str) -> bool:
        if self.isIpV6(ip):
            # IPv6 are almost never dynamic
            return False
        elements = ip.split(".")
        elements.reverse()
        checkIp = f"{'.'.join(elements)}.dul.dnsbl.sorbs.net"
        try:
            _ = gethostbyname(checkIp)
            return True
        except gaierror as ex:
            if ex.errno == -errno.ENOENT or ex.errno == 11001:  # 11001 == WSAHOST_NOT_FOUND
                return False
            else:
                print(ex)
                raise

    def treat(self, page: pywikibot.Page) -> None:
        """Process a single Page object from stream."""
        ch = page._rcinfo

        ts = datetime.fromtimestamp(ch["timestamp"])

        if datetime.now() - ts > timedelta(minutes=5):
            pywikibot.warning("Change too old: %s" % (str(datetime.now() - ts)))
            return

        if os.name != "nt":
            signal.alarm(TIMEOUT)  # pylint: disable=E1101

        if ch["type"] == "edit":
            # print(f"Edit on {ch['title']}: {ch['revision']['new']} by {ch['user']}")
            if ch["namespace"] == 4 and ch["title"] == "Wikipedia:Vandalismusmeldung" and not ch["bot"]:
                self.treatVmPageChange(ch["revision"]["old"], ch["revision"]["new"])

            comment = ch["comment"]
            rollbackedUser = None
            searchRes1 = self.rollbackRegex.search(comment)
            if searchRes1:
                rollbackedUser = searchRes1.group(1)
            searchRes2 = self.undoRegex.search(comment)
            if searchRes2:
                rollbackedUser = searchRes2.group(1)
            if rollbackedUser:
                pyUser = pywikibot.User(self.site, rollbackedUser)
                if pyUser.isAnonymous():
                    ip = rollbackedUser
                    try:
                        checkRes = self.vpnCheck.checkWithTeoh(ip)
                        if checkRes.score >= 2:
                            checkRes = self.vpnCheck.checkWithIpCheck(ip)
                    except CheckException as ex:
                        print(f"{ip} could not be checked: {ex}")
                    else:
                        if checkRes.score >= 2:
                            print(f"IP found after rollback: {ip} is a PROXY")

        currentTime = datetime.utcnow()
        if currentTime - self.lastBlockEventsCheckTime >= timedelta(seconds=30):
            events = self.site.logevents(reverse=True, start=self.lastBlockEventsCheckTime, logtype="block")
            for event in events:
                if event.action() == "block":
                    pwUser = pywikibot.User(self.site, event.page().title())
                    if pwUser.isAnonymous() and event.expiry() < currentTime + timedelta(weeks=1):
                        checkRes = self.vpnCheck.checkWithIpCheck(pwUser.username)
                        if checkRes.score >= 2:
                            print(f"Blocked IP {pwUser.username} is a PROXY.")
            self.lastBlockEventsCheckTime = currentTime

    def teardown(self) -> None:
        """Bot has finished due to unknown reason."""
        if self._generator_completed:
            pywikibot.log("Main thread exit - THIS SHOULD NOT HAPPEN")
            time.sleep(10)

    def test(self):
        self.treatVmPageChange(199995606, 199999485)


def FaultTolerantLiveRCPageGenerator(site: pywikibot.site.BaseSite) -> Iterator[pywikibot.Page]:
    for entry in site_rc_listener(site):
        # The title in a log entry may have been suppressed
        if "title" not in entry and entry["type"] == "log":
            continue
        try:
            page = pywikibot.Page(site, entry["title"], entry["namespace"])
        except Exception:
            pywikibot.warning("Exception instantiating page %s: %s" % (entry["title"], traceback.format_exc()))
            continue
        page._rcinfo = entry
        yield page


def main() -> None:
    locale.setlocale(locale.LC_ALL, "de_DE.utf8")
    pywikibot.handle_args()
    Controller().run()
    # Controller().test()


if __name__ == "__main__":
    try:
        main()
    finally:
        pywikibot.stopme()