# DogeFeedX page checklist

Compared 2026-11-03 against the public pages on dogefeed.app. Screenshots are
`compare/live-<page>.png` beside `compare/dfx-<page>.png`. The desktop shot is
`compare/dfx-desktop.png`.

Window interiors use dogefeed.app's light page (white, Nunito, Fredoka, orange
`#f5a023`). The menu bar, dock, and window frame stay the dark DogeFeedX skin.
The dock is the only site navigation. No module shows a sign-in button. Nothing
is written to dogefeed.app.

dogefeed.app sends Wallet, Files, Notifications, Messages, Bookmarks, NFTs, and
Settings to its own sign-in wall. Those modules here read this arcade instead.

| Module | Window | Visual | Data | Result |
|---|---|---|---|---|
| Feed (Home) | 640×620 | Light center column, composer, For You tabs, real posts | `/api/feed`, read-only | Pass |
| Profile | 440×620 | Card: banner, wallet switch, local name | Node address list. Empty until a watch-only address is imported | Pass |
| Files | 760×480 | Finder for this wallet | Holdings from this node, with the reading line | Pass |
| Notifications | 480×520 | Filter row, no sign-in | This arcade, per wallet | Pass |
| Messages | 520×460 | Start a DM / New group, no sign-in | This arcade, per wallet | Pass |
| Explore | 560×520 | Hashtag chips, people, collections | Public explore page | Pass |
| Market | 780×560 | Stats, A–Z, collection rows | Public market. This shot: 307 collections, 7.2M items | Pass |
| Inscribe | 640×520 | Note plus Collection, DRC-20, Name | This arcade's create pages. No dogefeed.app write | Pass |
| Wallet | 720×540 | Read-only notice, chain, address, reading line | This node's indexes. Peer line when a peer answers | Pass |
| Launchpad | 900×580 | Launch a collection, live mints, Top Launches | Public launch list (999 Club and the rest) | Pass |
| NFTs | 680×520 | Held by this node's wallet, reading line | Node indexes, names from the public catalog | Pass |
| Arcade | 640×480 | Poker, Much Match, Doge Tycoon, Doge Fleet, Bark Hunt | Public free-chip list, plus games inscribed here | Pass |
| Bookmarks | 480×420 | Empty until this wallet saves one | This browser, per wallet | Pass |
| Meme Library | 720×520 | Local picks and the public count | `/api/memes` (968 on this shot). Nothing uploaded | Pass |
| Leaderboard | 640×520 | 1D / 7D / 30D / ALL, orange amounts | Public board. 7D is the default | Pass |
| Settings | 480×560 | Profile fields and notice toggles | This browser, per wallet | Pass |
| Desktop | — | Shiba mark top-left, 16 dock apps once | Red badges on Notifications, Messages, and Market when the account has a count | Pass |

Not on the wallpaper: the 16 dock apps. Address book, Backup, Exchange, and the
other arcade tools stay, and none of them repeats a dock name.

Home does not repeat the site's left menu or its right rail. The window is the
center column. Profile is the card-sized window, not the full site profile.
