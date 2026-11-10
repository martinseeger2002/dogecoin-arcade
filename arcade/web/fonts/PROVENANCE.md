# The arcade's web fonts

Fonts for pages that want them, served by the arcade itself at `/fonts/<file>` (`fonts.css` declares them). Fetched
once from Google Fonts, so no page sends a visitor's address to Google or any other font host. First used by the
MoonBrix explorer (`web/moonbrix_explorer.html`, tbuuol's Syfy-UI look), and open to every page of the arcade.

All three are under the **SIL Open Font License 1.1**: they may be used, shipped and served freely, with their
licence beside them (`OFL-*.txt`), and not sold on their own. They are shipped exactly as Google Fonts served them,
not changed or renamed.

Fetched on 2026-11-10 from `fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Rajdhani:wght@400;500;600;700&family=Share+Tech+Mono`
(the `latin` subset: U+0000-00FF and common punctuation). `tests/test_fonts.py` pins the sums.

| File | Font | From | sha256 |
|---|---|---|---|
| `orbitron.woff2` | Orbitron, variable weight 400-900 | `fonts.gstatic.com/s/orbitron/v35/yMJRMIlzdpvBhQQL_Qq7dy0.woff2` | `c25a9f9da5d9f3db1bf2a01474722dc9b377675b7bbab6d0dfda6902794fd1ed` |
| `rajdhani-400.woff2` | Rajdhani Regular | `fonts.gstatic.com/s/rajdhani/v17/LDIxapCSOBg7S-QT7p4HM-Y.woff2` | `759a9000e47b028799d7a4ca602634a7ac7adf415775df070a335d18d9b66f38` |
| `rajdhani-500.woff2` | Rajdhani Medium | `fonts.gstatic.com/s/rajdhani/v17/LDI2apCSOBg7S-QT7pb0EPOreec.woff2` | `23afdb9b5b89b878fab04d80cc30bf41bb4f3f7e8be88e5f16a7cc7671cdb2dc` |
| `rajdhani-600.woff2` | Rajdhani SemiBold | `fonts.gstatic.com/s/rajdhani/v17/LDI2apCSOBg7S-QT7pbYF_Oreec.woff2` | `433a7007e4747a02a790167a6efa2625855f013970ba49b9b739a5d3db8b2601` |
| `rajdhani-700.woff2` | Rajdhani Bold | `fonts.gstatic.com/s/rajdhani/v17/LDI2apCSOBg7S-QT7pa8FvOreec.woff2` | `5b7e4a6f97163c2636724d4de90304fc895653dcfe64c67a7a22f26331ca5c5f` |
| `share-tech-mono.woff2` | Share Tech Mono | `fonts.gstatic.com/s/sharetechmono/v16/J7aHnp1uDWRBEqV98dVQztYldFcLowEF.woff2` | `41e6b9f297f7d9a2df2aaa274092f76d2f72711a15ca455f7f4f4f92caf16b72` |

Licences: `OFL-orbitron.txt` (The Orbitron Project Authors, Reserved Font Name "Orbitron"), `OFL-rajdhani.txt`
(Indian Type Foundry), `OFL-sharetechmono.txt` (Carrois Type Design, Reserved Font Name "Share"), each from
`github.com/google/fonts/tree/main/ofl/<font>`.
