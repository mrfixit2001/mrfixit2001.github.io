# -*- coding: utf-8 -*-
from .alldebrid import AllDebrid
from .premiumize import Premiumize
from .debridlink import DebridLink
from .linksnappy import LinkSnappy
from .torbox import TorBox


NATIVE_RESOLVERS = {
    'AllDebridResolver': ('ad', AllDebrid),
    'PremiumizeMeResolver': ('pm', Premiumize),
    'DebridLinkResolver': ('dl', DebridLink),
    'LinkSnappyResolver': ('ls', LinkSnappy),
}

NATIVE_ACCOUNTS = {
    'ad': AllDebrid,
    'pm': Premiumize,
    'dl': DebridLink,
    'ls': LinkSnappy,
}

PROVIDER_CODES = {
    'rd': 'RD', 'tb': 'TB', 'ad': 'AD', 'pm': 'PM', 'dl': 'DL', 'ls': 'LS'
}


def client_for_account(provider):
    cls = NATIVE_ACCOUNTS.get(str(provider or '').lower())
    return cls() if cls else None


PLAYBACK_CLEANUP = {
    'tb': (TorBox, 'delete_torrent'),
    'ad': (AllDebrid, 'delete_torrent'),
    'pm': (Premiumize, 'delete_transfer'),
    'dl': (DebridLink, 'delete_torrent'),
    'ls': (LinkSnappy, 'delete_torrent'),
}


def delete_playback_item(provider, item_id):
    """Delete only a transfer/torrent explicitly created for source playback."""
    entry = PLAYBACK_CLEANUP.get(str(provider or '').lower())
    if not entry or item_id in (None, ''):
        return False
    cls, method_name = entry
    client = cls()
    method = getattr(client, method_name)
    method(item_id)
    return True
