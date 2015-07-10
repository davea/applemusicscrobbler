#!/usr/bin/env python
import os
import logging
from configparser import ConfigParser
from pprint import pformat
import time
from math import ceil

import ScriptingBridge
import Foundation
import PyObjCTools.AppHelper
import objc

import pylast

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("scrobbler.py")


ITUNES_PLAYER_STATE_STOPPED = int.from_bytes(b'kPSS', byteorder="big")
ITUNES_PLAYER_STATE_PLAYING = int.from_bytes(b'kPSP', byteorder="big")

# Track must be at least this long to be scrobbled
SCROBBLER_MIN_TRACK_LENGTH = 30
# Scrobble after track halfway point or this many seconds since starting, whichever is first.
SCROBBLER_HALFWAY_THRESHOLD = 240

class Scrobbler(object):
    itunes = None
    lastfm = None
    config = None
    scrobble_timer = None

    def __init__(self):
        self.load_config()
        self.setup_itunes_observer()
        self.setup_lastfm()

    def load_config(self):
        inipath = os.path.expanduser("~/.scrobbler.ini")
        if not os.path.exists(inipath):
            raise Exception("Config file {} is missing.".format(inipath))
        self.config = ConfigParser()
        self.config.read(inipath)

    def setup_itunes_observer(self):
        self.itunes = ScriptingBridge.SBApplication.applicationWithBundleIdentifier_("com.apple.iTunes")
        log.debug("iTunes running: {}".format(self.itunes.isRunning()))
        dnc = Foundation.NSDistributedNotificationCenter.defaultCenter()
        selector = objc.selector(self.receivedNotification_, signature=b"v@:@")
        dnc.addObserver_selector_name_object_(self, selector, "com.apple.iTunes.playerInfo", None)
        log.debug("Added observer")

    def setup_lastfm(self):
        cfg = self.config['lastfm']
        password_hash = pylast.md5(cfg['password'])
        self.lastfm = pylast.LastFMNetwork(api_key=cfg['api_key'], api_secret=cfg['api_secret'], username=cfg['username'], password_hash=password_hash)
        log.debug("Connected to last.fm")

    def receivedNotification_(self, notification):
        log.debug("Got a notification: {}".format(notification.name()))
        userinfo = dict(notification.userInfo())
        # log.debug(pformat(userinfo))
        state = userinfo.get("Player State")
        if state == "Playing":
            should_scrobble = self.update_now_playing(userinfo)
            if should_scrobble:
                self.prepare_to_scrobble(userinfo)
            else:
                log.debug("update_now_playing returned False, so not going to scrobble.")
        elif state in ("Paused", "Stopped"):
            self.cancel_scrobble_timer()
        else:
            log.info("Unrecognised player state: {}".format(state))

    def update_now_playing(self, userinfo):
        kwargs = {
            'artist': userinfo.get("Artist"),
            'album_artist': userinfo.get("Album Artist"),
            'title': userinfo.get("Name"),
            'album': userinfo.get("Album"),
            'track_number': userinfo.get("Track Number"),
            'duration': userinfo.get("Total Time", 0) // 1000 or None,
        }
        # Some things, such as streams, don't have full metadata so we must ignore them
        if not kwargs['artist'] or not kwargs['title']:
            log.debug("Artist or title are missing, so ignoring...")
            return False
        log.debug("Updating now playing with kwargs:\n{}".format(pformat(kwargs)))
        self.lastfm.update_now_playing(**kwargs)
        return True

    def prepare_to_scrobble(self, userinfo):
        log.debug("prepare_to_scrobble")
        self.cancel_scrobble_timer()
        if userinfo.get("PersistentID") is None:
            log.warning("Track being played doesn't have a PersistentID, so can't prepare to scrobble it!")
            return

        # We need to wait a bit for a certain amount of the track to be played before scrobbling it.
        # The delay is half the track's length or SCROBBLER_HALFWAY_THRESHOLD, whichever is sooner.
        track_length = userinfo.get("Total Time", 0) / 1000 # seconds
        if track_length == 0:
            log.debug("Track has zero length, trying to get it from itunes.currentTrack after 5 seconds")
            time.sleep(5)
            track_length = self.itunes.currentTrack().duration()
            log.debug("currentTrack().duration(): {}".format(track_length))
            if not track_length:
                log.debug("Still zero-length, giving up!")
                return
        elif track_length < SCROBBLER_MIN_TRACK_LENGTH:
            log.debug("Track is too short ({}), so not going to scrobble it".format(track_length))
            return
        timeout = min(ceil(track_length/2), SCROBBLER_HALFWAY_THRESHOLD)
        log.debug("Setting up a timer for {} seconds".format(timeout))
        # Set up a timer that calls back after timeout seconds
        self.scrobble_timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            timeout,
            self,
            objc.selector(self.scrobbleTimerFired_, signature=b"v@:@"),
            userinfo,
            False
        )

    def cancel_scrobble_timer(self):
        log.debug("cancel_scrobble_timer")
        if self.scrobble_timer is not None:
            log.debug("Invalidating timer...")
            self.scrobble_timer.invalidate()
            self.scrobble_timer = None
        else:
            log.debug("No timer to invalidate")

    def scrobbleTimerFired_(self, timer):
        log.debug("scrobbleTimerFired_")
        if not timer.isValid():
            log.warning("Received a fire event from an invalid timer, not scrobbling")
            return
        if self.itunes.playerState() != ITUNES_PLAYER_STATE_PLAYING:
            log.debug("iTunes isn't playing, not scrobbling")
            return
        userinfo = timer.userInfo()

        expected_persistent_id = userinfo.get("PersistentID")
        if expected_persistent_id < 0:
            # PyObjC thinks this is a signed long, but actually it's unsigned, so convert it
            expected_persistent_id += 2**64
        expected_persistent_id = "{:016X}".format(expected_persistent_id)
        log.debug("Expected persistent ID of track to be scrobbled: {}".format(expected_persistent_id))

        current_track = self.itunes.currentTrack()
        scrobble_from_current_track = True
        actual_persistent_id = current_track.persistentID()
        if actual_persistent_id is not None and actual_persistent_id != expected_persistent_id:
            log.warning("Track now playing is different to the one that prompted timer, not scrobbling: {} (expected) vs {} (actual)".format(expected_persistent_id, actual_persistent_id))
            return
        elif actual_persistent_id is None:
            log.warning("Track playing has no persistent ID, assuming it's an Apple Music stream and scrobbling based on metadata from original notification")
            scrobble_from_current_track = False
        else:
            # at this point we know the correct track is playing
            log.debug("Correct track is playing, going to scrobble it")
        if scrobble_from_current_track:
            kwargs = {
                'artist': current_track.artist(),
                'title': current_track.name(),
                'album': current_track.album(),
                'album_artist': current_track.albumArtist(),
                'track_number': current_track.trackNumber(),
                'duration': int(current_track.duration()),
            }
        else:
            kwargs = {
                'artist': userinfo.get("Artist"),
                'title': userinfo.get("Name"),
                'album': userinfo.get("Album"),
                'album_artist': userinfo.get("Album Artist"),
                'track_number': userinfo.get("Track Number"),
                'duration': userinfo.get("Total Time", 0) // 1000 or None,
            }
        kwargs['timestamp'] = int(time.time() - self.itunes.playerPosition())
        log.debug("Going to scrobble with kwargs:\n{}".format(pformat(kwargs)))
        self.lastfm.scrobble(**kwargs)
        log.debug("done.")


def main():
    Scrobbler()
    log.debug("Going into event loop...")
    PyObjCTools.AppHelper.runConsoleEventLoop(installInterrupt=True)
    log.debug("exiting...")

if __name__ == '__main__':
    main()