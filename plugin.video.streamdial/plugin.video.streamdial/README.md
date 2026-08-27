# StreamDial TV

StreamDial TV is a self-contained Kodi video add-on by **mrfixit2001** for
searching, browsing, and playing free live television from provider-operated
services. It does not require another video add-on or a custom Kodi repository,
and it does not host, proxy, or restream video.

## Included providers

The browsable provider menu contains:

- Pluto TV
- Plex Live TV
- Samsung TV Plus
- PBS
- STIRR
- The Roku Channel
- LG Channels
- Xumo Play
- Tubi Live TV
- TCLtv+
- DistroTV
- Whale TV+
- Free Live Sports TV
- Vizio WatchFree+
- Local Now
- VIDAA Free TV
- Sling Freestream

Twitch is also included in unified search as a search-only live provider.

**PBS KIDS is not a separate provider.** `PBS KIDS 24/7`, PBS Create, PBS
WORLD, NHK WORLD-JAPAN, and participating local PBS stations are channels
inside the single PBS provider.

A historical validation snapshot for the original provider set found 4,227 fixed
browsable entries. Vizio WatchFree+, Local Now, VIDAA Free TV, and Sling Freestream
are now discovered dynamically from their provider-operated services and are not
represented by that older bundled count snapshot. Runtime catalogs, overlap,
regional availability, and lineups can change.

## Navigation

The root menu is:

1. Live TV Search
2. Browse Providers
3. Browse by Genre
4. Pinned

Provider browsing follows provider → language (when useful) → genre → channel.
Roku language metadata is derived from Roku's own station metadata/tags, so its
English and Spanish lineups are separated by the normal language browser.
Provider names include channel totals, and All Providers shows the combined
total across the enabled browsable services. Provider/language/genre browsing
is derived from one shared 10-minute timestamped catalog. When that catalog must
be rebuilt, StreamDial shows provider-by-provider progress; while it remains
valid, all browse routes reuse the exact same content. A provider that fails the
rebuild is listed with 0 channels instead of borrowing a stale count. With
cross-provider de-duplication enabled, every aggregate count is computed after
de-duplication from that same snapshot; same-provider rows are never collapsed.
Single-language providers skip the redundant language page. Every applicable
scope includes counted All Languages or All Genres entries, and full-scope All
Providers / All Languages / All Genres describe the same channel universe.
Channel results are alphabetical and show provider-published schedule times and
program artwork when available.
The most recently rendered final channel screen (including unified search
results) is timestamped and reused for repeated Kodi directory reloads when
returning from successful playback. This avoids re-running a provider search or
rebuilding a final list just because Kodi reloads the same directory more than
once. Explicit navigation, a newly generated final list, an expired snapshot,
or a resolver failure replaces/invalidates it.

Long-press a provider, language, genre, or channel to pin it. Long-press a
language, genre, or the Pinned menu to use Play Random. Saved searches can be
re-run with a short press or deleted from their context menu.

## Search

Unified keyword search matches channel names and metadata as well as current
provider-published schedule titles that are available without unsafe provider
fan-out. Inline/bulk guides are searched directly; recently fetched per-channel
guides are kept in a short timestamp-bounded schedule index. A provider progress
dialog advances as each enabled provider finishes searching. The optional **De-duplicate matching channels across providers** setting is off by default; when enabled it conservatively collapses matching channel identities only when they come from different providers and never removes duplicate rows that originate within a single provider.

## Playback diagnostics

Detailed diagnostics are opt-in through **General → Debug log diagnostics** and
are off by default. When enabled, catalog, schedule, search, resolve, Widevine,
and HLS/DASH handoff diagnostics apply to every provider. Tokens, cookies,
authorization values, license data, and sensitive URL parameters are redacted.

Every enabled playback handoff writes a correlated provider/manifest block so
failures that happen after Kodi takes control still have an actionable source
record. Widevine-required streams also record InputStream Adaptive/CDM readiness.

## Runtime and availability

The add-on runtime uses Python's standard library, provider-operated online
metadata, and a Samsung technical playback seed. Samsung's current public US
lineup is used to refresh known channel names/numbers/categories, while the seed
retains technical service identifiers/manifest paths that Samsung's public
consumer lineup does not expose. PBS station/live-eligibility metadata is
obtained from PBS-operated online services rather than a bundled callsign list.
`script.module.inputstreamhelper` 0.8.5+ is a required Kodi dependency so
Widevine-capable streams have a supported setup path. `inputstream.adaptive` is
used when needed for DASH/Widevine or adaptive HLS handling; compatible HLS can
still use Kodi's normal playback path.

Vizio WatchFree+, Local Now, VIDAA Free TV, and Sling Freestream are implemented
as isolated provider adapters. Their catalog, tune-time resolution, request
signing, and provider-specific DRM behavior live in their own provider modules;
no existing provider playback path is shared or replaced by these integrations.

All catalogs, schedules, artwork, manifests, and licenses are requested from
the applicable provider or a CDN selected by that provider. Provider terms,
advertising, outages, DRM support, and geolocation determine actual availability.
