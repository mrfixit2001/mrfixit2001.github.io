# Provider and third-party notices

StreamDial TV does not host, proxy, or restream video. Its adapters request
catalogs, schedules, artwork, manifests, and licenses from provider-operated
services and from delivery networks selected by those services. Availability,
advertising, DRM, geolocation, and service terms remain under each provider's
control.

| Provider | Principal runtime source |
| --- | --- |
| Pluto TV | `boot.pluto.tv`, `service-channels.clusters.pluto.tv`, and Pluto stitcher/CDN URLs |
| Plex Live TV | `plex.tv`, `watch.plex.tv`, `epg.provider.plex.tv`, and Plex-selected CDN URLs |
| Samsung TV Plus | `samsung.com` public US TV Plus lineup, `sis-global.prod.samsungtv.plus`, and Samsung-selected CDN URLs |
| PBS | `station.services.pbs.org`, `help.pbs.org`, `pbs.org`, `livestream.pbskids.org`, and stream/license URLs returned by PBS |
| STIRR | `stirr.com` and STIRR-selected CDN URLs |
| The Roku Channel | `therokuchannel.roku.com`, `content.sr.roku.com`, and Roku-selected CDN/license URLs |
| LG Channels | `api.lgchannels.com` and LG-selected CDN URLs |
| Xumo Play | `valencia-app-mds.xumo.com`, `image.xumo.com`, and Xumo-selected CDN URLs |
| Tubi Live TV | `tubitv.com` and Tubi-selected CDN URLs |
| TCLtv+ | `gateway-prod.ideonow.com`, `tcltv.plus`, and TCL-selected CDN URLs |
| DistroTV | `distro.tv`, `tv.jsrdn.com`, and Distro-selected CDN URLs |
| Whale TV+ | `rlaxx.zeasn.tv`, `watch.whaletvplus.com`, and Whale-selected CDN URLs |
| Free Live Sports TV | `freelivesports.tv`, `epg.unreel.me`, and provider-selected CDN URLs |
| Vizio WatchFree+ | `watchfreeplus-epg-prod.smartcasttv.com` and Vizio-selected CDN URLs |
| Local Now | `localnow.com`, the DSP API host published by Local Now at runtime, and Local Now-selected CDN URLs |
| VIDAA Free TV | `partner.vidaahub.com`, `partner-layout-ui.vidaahub.com`, `partner-detail-ui.vidaahub.com`, and VIDAA-selected stream/license URLs |
| Sling Freestream | `cbd46b77.cdn.cms.movetv.com`, `p-drmwv.movetv.com`, `watch.sling.com`, and Sling-selected manifest/CDN URLs |
| Twitch | `gql.twitch.tv`, `usher.ttvnw.net`, and Twitch CDN URLs |

Samsung's provider-owned public US channel lineup is fetched periodically to
refresh the names, channel numbers, and categories of channels already known to
StreamDial. A bundled Samsung technical playback seed is still required because
Samsung's public consumer lineup does not expose the service identifiers and
manifest paths needed to tune a newly added channel. StreamDial does not use a
community Samsung catalog/resolver to fill that gap.

PBS station records come from `station.services.pbs.org`, and the current
live-capable station roster is derived from PBS's own Live Streaming FAQ on
`help.pbs.org`. The former bundled PBS callsign allow-list has been removed. If
the PBS help roster cannot be parsed, StreamDial falls back to PBS's official
station API rather than to third-party or bundled station metadata.

Bundled provider logos are used solely to identify their respective services.
All provider names, logos, and trademarks belong to their owners. Other custom
menu artwork and the StreamDial TV identity are bundled add-on assets.

`script.module.inputstreamhelper` 0.8.5+ is a required Kodi dependency and
`inputstream.adaptive` is used where the selected provider stream requires it.
No custom repository module, community catalog/playback resolver, GitHub-hosted
runtime feed, or another video add-on is imported or contacted. Development
references are not runtime dependencies.


## Protocol research references

The open-source FastChannels project was consulted as a reference for the
provider protocols used by Vizio WatchFree+, Local Now, VIDAA Free TV, and Sling
Freestream. StreamDial reimplements the required protocol directly with each
provider. FastChannels is not contacted by StreamDial at runtime, is not bundled,
and is not an addon dependency. iptv-org is likewise not a runtime source.
