# mijwiz — Discord music bot

Plays YouTube and Spotify links (tracks, albums, playlists) or plain search text in a Discord voice channel.

- **YouTube**: streamed with [yt-dlp](https://github.com/yt-dlp/yt-dlp) + FFmpeg.
- **Spotify**: Spotify doesn't allow streaming its audio, so the bot reads each song's artist + title from Spotify's public embed page and plays the best YouTube match. No Spotify API key needed.
- Python 3.12, [discord.py](https://github.com/Rapptz/discord.py) 2.7 (includes DAVE, the voice encryption Discord now requires).

## Setup

1. **Create the bot** at <https://discord.com/developers/applications> → *New Application* → *Bot* → *Reset Token*, copy the token.
   No privileged intents are needed.
2. **Invite it**: *OAuth2* → *URL Generator* → scopes `bot` + `applications.commands`, permissions *View Channels*, *Send Messages*, *Connect*, *Speak*. Or use (replace `CLIENT_ID`):
   `https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=3148800`
3. **Configure**: `cp .env.example .env` and set `DISCORD_TOKEN`.
4. **Run**: `docker compose up -d --build` · logs: `docker compose logs -f` · stop: `docker compose down`

Slash commands are registered on startup; if they don't show up right away, restart your Discord client.

## Commands

| Command | |
|---|---|
| `/play <link or search>` | Join your voice channel and queue a YouTube/Spotify track, album or playlist, or the top YouTube result for the text |
| `/skip` | Skip the current track |
| `/pause` / `/resume` | Pause / resume |
| `/queue` | Show what's playing and up next |
| `/stop` | Clear the queue and leave |

The bot leaves on its own after 5 minutes with an empty queue, or when everyone leaves the channel.

## Notes & troubleshooting

- **YouTube breaks yt-dlp now and then.** The container upgrades yt-dlp every time it starts, so `docker compose restart` is usually the fix.
- **"Sign in to confirm you're not a bot"**: YouTube is blocking your server's IP (common on cloud/VPS hosts, rare at home). Export YouTube cookies in Netscape format (e.g. with the "Get cookies.txt LOCALLY" browser extension, ideally from a throwaway account) to `cookies.txt` next to `docker-compose.yml`, uncomment the `volumes` lines in `docker-compose.yml` and `YTDL_COOKIES` in `.env`, then `docker compose up -d`.
- **Spotify playlists**: Spotify's embed page lists up to ~100 tracks, so longer playlists are cut off at that point. Artist pages, podcasts and private playlists aren't supported. If a `spotify.link/...` short link fails, open it in a browser and use the `open.spotify.com` link instead.
- **YouTube playlists** are capped at the first 500 videos (`MAX_PLAYLIST` in `bot.py`).
- Only YouTube and Spotify links are accepted; anything else that isn't a link is treated as a YouTube search.

## Tests

```sh
docker compose run --rm bot python test_bot.py
```
