# Provider coverage

Version 1.2.4 contains 13 browsable providers and one additional search-only
provider. “Direct” below means the runtime contacts a provider-operated API,
site, or a CDN URL returned by that provider; it does not mean every channel is
available in every region.

| Provider | Browse | Unified search | Schedule shown | Playback |
| --- | --- | --- | --- | --- |
| Pluto TV | Yes | Yes | Provider guide | Channel-specific HLS |
| Plex Live TV | Yes | Yes | Public Plex grid for visible results | Anonymous HLS tune flow |
| Samsung TV Plus | Yes | Yes | Official Samsung US lineup refreshes known channel metadata; no compatible direct event guide in this build | Direct Samsung HLS |
| PBS | Yes | Yes | Official PBS station/live roster metadata; no compatible event guide | PBS HLS or DASH/Widevine |
| STIRR | Yes | Yes | No compatible event guide located | Session-aware HLS |
| The Roku Channel | Yes | Yes, including recently fetched schedule titles | Provider guide for visible results | Anonymous Roku DASH/Widevine or HLS |
| LG Channels | Yes | Yes | Provider schedule | Direct HLS |
| Xumo Play | Yes | Yes, including bulk-guide program titles | Provider guide for visible results | Per-channel HLS broadcast |
| Tubi Live TV | Yes | Yes | Provider EPG | Direct HLS |
| TCLtv+ | Yes | Yes | Provider schedule | Direct HLS |
| DistroTV | Yes | Yes | Provider EPG where published | Direct HLS |
| Whale TV+ | Yes | Yes | Provider guide | Direct HLS |
| Free Live Sports TV | Yes | Yes | Embedded provider EPG | Direct HLS |
| Twitch | Search only | Yes | Live title and category metadata | Anonymous HLS |

## PBS consolidation

There is exactly one provider key and menu entry for **PBS**. Its channels
include participating local PBS stations plus `PBS KIDS 24/7`, PBS Create, PBS
WORLD, and NHK WORLD-JAPAN. PBS KIDS playback uses PBS's clear national HLS feed and does not require
Widevine; it is not a separate provider or a restream.

## Live validation snapshot

The 2026-08-26 validation found the following fixed catalog counts:

| Provider | Entries |
| --- | ---: |
| Pluto TV | 418 |
| Plex Live TV | 686 |
| Samsung TV Plus | 512 |
| PBS | 148 |
| STIRR | 155 |
| The Roku Channel | 813 |
| LG Channels | 203 |
| Xumo Play | 424 |
| Tubi Live TV | 176 |
| TCLtv+ | 433 |
| DistroTV | 9 |
| Whale TV+ | 138 |
| Free Live Sports TV | 112 |
| **Total fixed listings** | **4,227** |

Provider lists are counted independently, so the total can contain channels
that appear on more than one service. Twitch is dynamic and is not included.

## Reviewed but not enabled

- Local Now: a provider-operated runtime bootstrap/catalog was not reliably
  available from the validation environment.
- NewsON: no current direct implementation was verified for this build.
- VIDAA Free TV: its device-oriented signed/DRM flow was not ported without a
  maintainable, fully tested provider-direct implementation.

Community IPTV lists, custom repository APIs, and third-party catalog or
playback mirrors are deliberately excluded from the runtime.

Requests for additional direct provider coverage are welcome through
**mrfixit2001's GitHub**: https://github.com/mrfixit2001
