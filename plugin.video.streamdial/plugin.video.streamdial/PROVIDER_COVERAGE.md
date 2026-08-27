# Provider coverage

Version 1.2.5 contains 17 browsable providers and one additional search-only
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
| Vizio WatchFree+ | Yes | Yes | Provider bulk guide | Direct clear HLS from Vizio catalog |
| Local Now | Yes | Yes | Provider inline EPG | Tune-time HLS from Local Now DSP API |
| VIDAA Free TV | Yes | Yes | Current provider schedule metadata | Provider HLS or DASH/Widevine |
| Sling Freestream | Yes | Yes, including schedules already fetched while browsing | Per-channel provider QVT for visible rows | Anonymous Sling DASH/Widevine or provider-returned HLS |
| Twitch | Search only | Yes | Live title and category metadata | Anonymous HLS |

## PBS consolidation

There is exactly one provider key and menu entry for **PBS**. Its channels
include participating local PBS stations plus `PBS KIDS 24/7`, PBS Create, PBS
WORLD, and NHK WORLD-JAPAN. PBS KIDS playback uses PBS's clear national HLS feed and does not require
Widevine; it is not a separate provider or a restream.

## Newly integrated large FAST providers

- **Vizio WatchFree+** uses Vizio's anonymous WatchFree+ channel/airings API.
  Token-gated catalog rows are deliberately excluded; ordinary rows resolve to
  clear HLS supplied by Vizio.
- **Local Now** bootstraps its current DSP API host and anonymous access token
  from `localnow.com`, then uses Local Now's live EPG and tune-time playback API.
- **VIDAA Free TV** uses the signed production backend used by the Hisense/VIDAA
  Channels application. The adapter supports both clear HLS and provider-issued
  Widevine DASH licenses.
- **Sling Freestream** exposes only rows Sling marks free and linear. No Sling
  account/subscription integration exists in StreamDial; anonymous Freestream
  Widevine requests are constructed directly for Sling's license service.

These adapters use no FastChannels, iptv-org, community M3U, or other third-party
runtime endpoint. Third-party code was used only as reverse-engineering/reference
material while reproducing the provider-direct protocols.

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

- NewsON: no current direct implementation was verified for this build.

Community IPTV lists, custom repository APIs, and third-party catalog or
playback mirrors are deliberately excluded from the runtime.

Requests for additional direct provider coverage are welcome through
**mrfixit2001's GitHub**: https://github.com/mrfixit2001
