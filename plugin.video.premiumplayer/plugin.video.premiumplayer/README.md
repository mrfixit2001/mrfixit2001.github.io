# Premium Player

**Premium Player** is a Kodi 20+ / Python 3 video add-on for discovering movie and television metadata, searching compatible third-party sources, resolving playable links through supported premium/debrid services, browsing supported provider accounts, managing pinned items, downloading files to local storage, and optionally providing continuous TV-episode playback.

Premium Player is software only. **No movies, television episodes, streams, torrents, video files, or other media are included with, hosted by, uploaded by, sold by, licensed by, or supplied by Premium Player or by its authors, contributors, or maintainers.**

> **Important:** Simply installing Premium Player does not implicitly grant anyone permission to access, stream, download, copy, distribute, or otherwise use any media. Users are solely responsible for ensuring that every source and service they use, and every item of media they access, is lawful and authorized for their use.

## What Premium Player does

Premium Player provides a user interface and integration layer between Kodi and independently operated third-party services. Depending on the user's configuration, the add-on can:

- Search for movies and TV shows and display metadata and artwork.
- Search compatible third-party source indexes for candidate media sources.
- Apply configured quality, file-size, provider, and cached-only filters.
- Resolve selected sources through ResolveURL and supported premium/debrid services.
- Browse supported Real-Debrid, TorBox, AllDebrid, Premiumize.me, Debrid-Link, and LinkSnappy account/cloud content.
- Play or download files returned by configured third-party services.
- Pin movies, shows, seasons, episodes, sources, account items, and locally downloaded files.
- Provide `Play All Seasons` and `Play All Starting Here` continuous TV playback.
- Display up to 300 current movie releases and newly aired TV episodes from
  broadcast and web/streaming schedules, newest-first and paginated as three
  pages of up to 100 results. TV New Releases maintains one rolling 30-day
  All-Genres provider pool and filters genre views locally; it does not perform
  separate provider crawls for Action, Drama, Thriller, or other genres. Kodi's
  Back action returns to the prior page, so the result list includes only
  forward page navigation.
- Show coordinated, category-specific navigation artwork while leaving media results, files, and torrents visually uncluttered.

Navigation directories are completed without assigning a media content type,
allowing compatible Kodi list views to draw each supplied `ListItem.Icon` in
the actual menu row. Media-result, provider-file, and torrent listings keep
their existing media classifications and do not receive navigation artwork.

Premium Player does not itself operate a streaming service, debrid service, torrent host, torrent swarm, file locker, CDN, media server, or content repository.

On first launch, Premium Player presents a detailed third-party-content, authorized-use, assumption-of-risk, limitation-of-liability, and indemnification notice and requires an explicit `AGREE` or `DISAGREE` choice. Agreement is stored together with the notice version, so a materially revised notice is shown once after an update. `Settings > Maintenance > Reset all settings to defaults` clears every Premium Player setting and causes the notice to appear again on the next launch; ResolveURL account authorizations remain managed separately.

## ResolveURL dependency

Premium Player requires `script.module.resolveurl` version 5.1.206 or newer. ResolveURL manages provider authorization, provider enable/disable state, cached-only options, and resolver-specific settings.

Authoritative ResolveURL locations maintained by Gujal include:

- Source: `https://github.com/Gujal00/ResolveURL`
- Official release feed/zips: `https://github.com/Gujal00/smrzips`
- Gujal Kodi file-manager source: `https://gujal00.github.io/`
- ResolveURL Repository zip: `https://raw.githubusercontent.com/Gujal00/smrzips/master/zips/repository.resolveurl/repository.resolveurl-1.0.0.zip`

If ResolveURL is already installed by another Kodi add-on and meets the required version, Premium Player can use that installation and its existing provider authorizations.

## Provider and account behavior

Enabled and authorized ResolveURL services are shown under `Browse Accounts` when Premium Player can identify them.

Premium Player includes native account/transfer adapters for Real-Debrid, TorBox, AllDebrid, Premiumize.me, Debrid-Link, and LinkSnappy. These adapters do **not** create a second authorization system: they reuse the credentials and provider settings already managed by ResolveURL. They exist because ResolveURL's public resolver interface does not provide a universal API for listing a user's cloud/torrents, browsing exact stored files, deleting provider-side transfers, or validating pinned account items.

Other enabled ResolveURL universal services can still participate in source resolution when supported by ResolveURL. Premium Player does not fabricate an account browser for a service unless it has a provider-specific API path capable of supporting those account operations.

Premium Player does not own, operate, control, or administer any premium/debrid provider. Provider availability, account access, cached content, files, links, APIs, terms of service, billing, privacy practices, and service behavior are controlled solely by the applicable third party.

## Search and playback

Movie and episode searches build an ordered source list using the user's configured filters. The resulting source list is retained temporarily until another search begins so Kodi can return to the same results after playback without performing the search again.

Selecting source N attempts that source first. If automatic fallback is enabled, Premium Player proceeds downward through the current source list until a source successfully starts. The source that actually played is retained as the selected source when possible after playback ends.

For continuous TV playback, each episode performs a new source search using the same configured filters. A naturally completed episode can advance to the next episode after the countdown. A manual Stop terminates continuous playback.

## Pinned items and local downloads

`Pinned` can contain:

- `Movies`
- `TV Shows`
- `My Torrents`
- `Downloaded` when locally downloaded files exist

Provider-backed pinned items are validated against supported provider accounts when possible. Local downloaded items are validated against the local filesystem and are automatically removed from the list when the corresponding file no longer exists.

When source playback or a local download temporarily adds a torrent to TorBox, Premium Player removes that newly-created item after playback, cancellation, or transfer failure. A torrent that was already present in the user's TorBox account is never enrolled in this automatic cleanup lifecycle.

## Cache behavior

Premium Player avoids persistent source-result and metadata caching where practical. Search history and user-created pins are intentionally retained. Temporary source lists are retained only to support navigation and playback behavior and are replaced when a new search begins.

New Releases uses temporary Kodi cache storage for navigation performance. Derived Movie/TV genre result sets are cleared at the shared `New Releases` menu boundary. TV schedule acquisition is cached separately as a rolling 30-day All-Genres provider pool: genre selections filter that pool locally, today's schedule is refreshed frequently, and older daily schedule responses are reused for progressively longer intervals because historical premiere schedules are largely stable. This prevents every genre selection or daily refresh from repeating the full 30-day TVmaze crawl.

Kodi itself may independently cache artwork, textures, metadata, or other resources as part of normal Kodi operation.

---

# Legal Notice, Third-Party Content, and User Responsibility

## No media is provided by Premium Player

**The Premium Player software package contains no playable movies, television programs, copyrighted video catalog, hosted streams, torrent payloads, or media library.**

The authors, contributors, and maintainers of Premium Player **do not provide the media that users play or download through the add-on**. Media, files, URLs, torrent data, streams, cached data, and other content made available during use originate from **independent third-party services, providers, indexes, servers, networks, or user accounts**.

Premium Player may query those third-party systems and may instruct Kodi or another dependency to access a URL or file returned by them. That technical functionality does not make Premium Player, its authors, contributors, or maintainers the provider, publisher, licensor, owner, or distributor of the underlying media.

## No control over third-party content or services

The Premium Player project and its authors, contributors, and maintainers do not control third-party services or the material those services return. They make no representation or warranty concerning the:

- ownership, copyright status, licensing, authorization, or legality of third-party media;
- accuracy, completeness, availability, reliability, or continued availability of third-party results;
- safety, security, privacy, or integrity of third-party websites, APIs, files, streams, torrents, or services;
- terms, policies, billing practices, account restrictions, retention policies, or conduct of any third-party provider; or
- suitability of any third-party source for a particular user's intended use.

The appearance of a source, file, link, title, provider, service, or search result in Premium Player **does not constitute authorization, endorsement, approval, or a representation that access to that material is lawful**.

Likewise, the fact that a torrent or file is cached by a premium/debrid provider does not establish that the user has permission to access, stream, reproduce, or download the underlying work.

## User is solely responsible for use

Premium Player is a general-purpose software tool. **Each user is solely responsible for how they configure and use it.**

By installing or using Premium Player, the user acknowledges that it is the user's responsibility to:

- determine whether the user is legally entitled to access each item of media;
- obtain any subscription, purchase, license, permission, or other authorization required for that media;
- comply with applicable copyright, intellectual-property, communications, computer-misuse, privacy, and other laws and regulations;
- comply with the terms of service, acceptable-use policies, and account rules of every third-party service used with the add-on;
- ensure that downloading, copying, streaming, sharing, or otherwise using content is permitted in the user's jurisdiction; and
- accept all consequences arising from the user's selection of third-party sources and services.

Premium Player is **not intended or authorized for copyright infringement, unauthorized access to protected works, circumvention of access controls, or any other unlawful activity**.

If a user does not know whether a particular use is lawful, the user should not proceed until obtaining appropriate permission or advice from a qualified legal professional in the relevant jurisdiction.

## Copyright

Copyright law can give copyright owners exclusive rights in their works, and unauthorized reproduction or distribution of protected works can create legal liability. The legality of a particular stream, download, copy, or other use depends on the specific work, source, authorization, jurisdiction, and applicable exceptions or limitations.

**Nothing in Premium Player, its source code, documentation, search results, provider integration, or user interface should be interpreted as legal advice or as a determination that any particular media source is authorized.**

Because Premium Player does not host or control third-party media, the project generally cannot remove material from a third-party server, provider, cache, torrent network, or other external service. Complaints concerning media hosted or supplied by a third party should be directed to the party that actually hosts, controls, or supplies that material. Concerns relating to Premium Player's own source code or repository should be directed to the Premium Player project through its normal project contact or issue mechanism.

## Third-party names and trademarks

Kodi, ResolveURL, Real-Debrid, TorBox, AllDebrid, Premiumize, Debrid-Link, LinkSnappy, and other third-party names, service marks, logos, and trademarks belong to their respective owners.

Unless expressly stated otherwise by the applicable third party, Premium Player is an independent project and is **not affiliated with, sponsored by, endorsed by, or operated by** Kodi, the Kodi Foundation, ResolveURL, any premium/debrid provider, any metadata provider, any torrent index, or any media rights holder.

References to third-party products and services are for interoperability and identification purposes only.

## No warranty

Premium Player is distributed under the MIT License and is provided **"AS IS"**, without warranty of any kind, express or implied, including without limitation warranties of merchantability, fitness for a particular purpose, and noninfringement, as stated in `LICENSE.txt`.

Third-party services can change, fail, block access, discontinue APIs, return incorrect information, remove files, suspend accounts, or become unavailable without notice. Premium Player does not guarantee that any search, provider, source, stream, download, account integration, or other feature will remain available or function without interruption or error.

## Limitation of liability

**To the maximum extent permitted by applicable law**, the Premium Player authors, copyright holders, contributors, and maintainers shall not be liable for claims, damages, losses, penalties, costs, account actions, data loss, service interruptions, copyright claims, or other liability arising from or relating to:

- installation or use of Premium Player;
- the user's choice or use of a third-party source or service;
- media accessed, streamed, downloaded, copied, stored, or otherwise used by the user;
- actions or omissions of third-party providers;
- unauthorized, unlawful, or improper use of the software; or
- reliance on search results, metadata, links, availability indicators, cache indicators, or other information presented by the add-on.

Nothing in this notice attempts to exclude or limit liability that cannot lawfully be excluded or limited under applicable law.

The MIT License in `LICENSE.txt` remains the controlling software license. This README is intended to explain the project's operation and intended use and does not expand the warranties or liabilities stated in that license.

## Use only with authorized content

**Use Premium Player only with media and services that you are legally authorized to access.**

Examples may include media you own, media you have purchased or licensed, media made freely available by its rights holder, public-domain material, or material whose use is otherwise permitted by applicable law.

The user—not Premium Player, its authors, contributors, or maintainers—is responsible for determining whether a particular use is authorized.

---

## License

Premium Player is licensed under the MIT License. See [`LICENSE.txt`](LICENSE.txt) for the complete license terms.

Copyright © 2026 Premium Player contributors.
