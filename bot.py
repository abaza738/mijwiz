import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
from collections import deque
from dataclasses import dataclass
from itertools import islice
from urllib.parse import urlparse

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.utils import escape_markdown

log = logging.getLogger("musicbot")

IDLE_TIMEOUT = 300  # seconds with an empty queue before leaving voice
MAX_PLAYLIST = 500  # tracks taken from one YouTube playlist
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
SPOTIFY_SHORT_HOSTS = {"spotify.link", "spotify.app.link"}
SPOTIFY_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?(?:user/[^/]+/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]{22})"
)
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', re.S)
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# allowed_extractors keeps yt-dlp on YouTube (no generic extractor fetching arbitrary pages).
YTDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "allowed_extractors": ["youtube", "youtube:.*"],
}
ytdl_lock = contextlib.nullcontext()
if os.getenv("YTDL_COOKIES"):
    # yt-dlp writes the cookie file back after every call: use a private writable copy (the mount may be
    # read-only) and one yt-dlp call at a time so concurrent writes can't corrupt it.
    YTDL_OPTS["cookiefile"] = shutil.copy(os.environ["YTDL_COOKIES"], os.path.join(tempfile.gettempdir(), "yt-cookies.txt"))
    ytdl_lock = threading.Lock()
# Queueing only needs titles + video URLs; stream URLs expire, so they're fetched right before playing.
YTDL_FLAT_OPTS = {**YTDL_OPTS, "extract_flat": "in_playlist", "playlist_items": f"1-{MAX_PLAYLIST}"}


@dataclass
class Track:
    title: str
    url: str  # YouTube URL or "ytsearch1:..." query

    async def source(self) -> discord.AudioSource:
        info = await asyncio.to_thread(ytdl_stream_info, self.url)
        headers = "".join(f"{k}: {v}\r\n" for k, v in info.get("http_headers", {}).items())
        # Fresh googlevideo URLs can answer 403 for the first second or two, so retry on 4xx too.
        before = "-reconnect 1 -reconnect_streamed 1 -reconnect_on_http_error 4xx -reconnect_delay_max 10"
        if headers:
            before += f" -headers {shlex.quote(headers)}"
        return discord.FFmpegOpusAudio(info["url"], before_options=before, options="-vn")


def ytdl_tracks(query: str) -> list[Track]:
    with ytdl_lock, yt_dlp.YoutubeDL(YTDL_FLAT_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
    entries = info.get("entries")
    if entries is None:
        return [Track(info.get("title") or query, info.get("webpage_url") or query)]
    # Only videos; channel pages list their tabs ("Videos", "Shorts", ...) as nested playlists.
    return [Track(e.get("title") or e["url"], e["url"]) for e in entries if e and e.get("ie_key") == "Youtube" and e.get("url")]


def ytdl_stream_info(url: str) -> dict:
    with ytdl_lock, yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
    if "entries" in info:  # search query
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise LookupError("no YouTube match found")
        info = entries[0]
    return info


def parse_spotify_embed(kind: str, html: str) -> list[Track]:
    # ponytail: scrapes the public embed page instead of the Web API (which needs Premium + only
    # returns playlists you own since Feb 2026). Embeds list at most ~100 tracks of a playlist.
    m = NEXT_DATA_RE.search(html)
    try:
        entity = json.loads(m[1])["props"]["pageProps"]["state"]["data"]["entity"]
    except (TypeError, KeyError, ValueError):
        raise ValueError("couldn't read that Spotify link (does it exist and is it public?)") from None
    if kind == "track":
        songs = [(entity["name"], ", ".join(a["name"] for a in entity.get("artists", [])))]
    else:
        songs = [(t["title"], t.get("subtitle", "")) for t in entity.get("trackList", [])]
    tracks = []
    for title, artist in songs:
        title, artist = " ".join(title.split()), " ".join(artist.split())  # also drops \xa0
        name = f"{artist} - {title}" if artist else title
        tracks.append(Track(name, f"ytsearch1:{name} audio"))
    return tracks


async def fetch(url: str) -> tuple[str, str]:
    """GET following redirects; returns (final URL, body). Retries, since Spotify sometimes answers 5xx."""
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(headers={"User-Agent": BROWSER_UA}, timeout=timeout) as session:
        for attempt in range(3):
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    return str(resp.url), await resp.text()
            except (aiohttp.ClientError, TimeoutError):
                if attempt == 2:
                    raise
                await asyncio.sleep(1)


async def spotify_tracks(kind: str, spotify_id: str) -> list[Track]:
    _, html = await fetch(f"https://open.spotify.com/embed/{kind}/{spotify_id}")
    return parse_spotify_embed(kind, html)


async def resolve(query: str) -> list[Track]:
    query = query.strip()
    host = (urlparse(query).hostname or "").lower() if "://" in query else ""
    if host in SPOTIFY_SHORT_HOSTS:
        # ponytail: untested against a live short link; looks for the open.spotify.com URL it leads to
        final_url, body = await fetch(query)
        if not (m := SPOTIFY_RE.search(final_url) or SPOTIFY_RE.search(body)):
            raise ValueError("couldn't resolve that Spotify short link, use the open.spotify.com link instead")
        return await spotify_tracks(*m.groups())
    if m := SPOTIFY_RE.search(query):
        return await spotify_tracks(*m.groups())
    if host:
        if host not in YOUTUBE_HOSTS:
            raise ValueError("only YouTube and Spotify links are supported")
    else:
        query = f"ytsearch1:{query}"
    return await asyncio.to_thread(ytdl_tracks, query)


def short_error(e: Exception) -> str:
    return str(e).removeprefix("ERROR: ")[:300] or type(e).__name__


class Player:
    """One per guild: owns the queue and plays it in a background task until stopped or idle."""

    def __init__(self, guild: discord.Guild, channel: discord.abc.Messageable):
        self.guild = guild
        self.channel = channel
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.closing = False  # set once the player is shutting down; it must not be reused after that
        self._wake = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    def add(self, tracks: list[Track]) -> None:
        self.queue.extend(tracks)
        self._wake.set()

    def stop(self) -> None:
        if self.closing:  # already leaving; a second cancel would interrupt the cleanup
            return
        self.closing = True
        self.queue.clear()
        self.task.cancel()

    async def _say(self, text: str) -> None:
        try:
            await self.channel.send(text, suppress_embeds=True)
        except discord.HTTPException:
            log.warning("Can't send messages in channel %s", getattr(self.channel, "id", "?"))

    async def _run(self) -> None:
        try:
            while True:
                if not self.queue:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), IDLE_TIMEOUT)
                    except TimeoutError:
                        return
                    continue

                track = self.current = self.queue.popleft()  # shown by /queue while it loads
                try:
                    source = await track.source()
                except Exception as e:
                    self.current = None
                    log.warning("Failed to load %s: %s", track.url, e)
                    await self._say(f"Skipped **{escape_markdown(track.title)}**: {short_error(e)}")
                    continue

                vc = self.guild.voice_client
                if vc is None:
                    source.cleanup()
                    await self._say("I'm no longer in a voice channel, so I cleared the queue.")
                    return
                done = asyncio.Event()
                loop = asyncio.get_running_loop()

                def after(err: Exception | None, done: asyncio.Event = done) -> None:
                    if err:
                        log.error("Playback error: %s", err)
                    loop.call_soon_threadsafe(done.set)

                try:
                    vc.play(source, after=after)
                except discord.ClientException as e:  # voice dropped while the track was loading
                    source.cleanup()
                    await self._say(f"Lost the voice connection: {e}")
                    return
                await self._say(f"Now playing: **{escape_markdown(track.title)}**")
                await done.wait()
                self.current = None
        except Exception:
            log.exception("Player for guild %s crashed", self.guild.id)
            await self._say("Something went wrong, so I stopped playback.")
        finally:
            self.closing = True
            self.current = None
            if self.guild.voice_client:
                await self.guild.voice_client.disconnect(force=True)
            # Removed only after disconnecting, so /play waits for us instead of reusing the old connection.
            if players.get(self.guild.id) is self:
                del players[self.guild.id]


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await self.tree.sync()


bot = Bot()
players: dict[int, Player] = {}
connect_lock = asyncio.Lock()  # ponytail: global lock, only held while joining voice


@bot.tree.command(description="Play a YouTube/Spotify link (track, album or playlist) or search YouTube")
@app_commands.describe(query="YouTube or Spotify link, or search text")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    voice = interaction.user.voice
    if voice is None or voice.channel is None:
        return await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
    vc = interaction.guild.voice_client
    if vc and vc.channel != voice.channel:
        return await interaction.response.send_message(f"I'm already playing in {vc.channel.mention}.", ephemeral=True)

    await interaction.response.defer(thinking=True)
    try:
        tracks = await resolve(query)
    except Exception as e:
        log.warning("Failed to resolve %r: %s", query, e)
        return await interaction.followup.send(f"Couldn't load that: {short_error(e)}")
    if not tracks:
        return await interaction.followup.send("Nothing found.")

    guild = interaction.guild
    async with connect_lock:
        # Re-checked here: resolving took a while and other /play calls may have joined meanwhile.
        player = players.get(guild.id)
        if player and player.closing:
            await asyncio.wait([player.task])  # let it finish leaving voice before we rejoin
            player = None
        voice = interaction.user.voice
        if voice is None or voice.channel is None:
            return await interaction.followup.send("You left the voice channel.")
        vc = guild.voice_client
        if vc and vc.channel != voice.channel:
            return await interaction.followup.send(f"I'm already playing in {vc.channel.mention}.")
        if vc is None:
            try:
                await voice.channel.connect(self_deaf=True)
            except Exception as e:
                log.warning("Failed to join voice: %s", e)
                return await interaction.followup.send("Couldn't join your voice channel (check my Connect/Speak permissions).")
        if player is None:
            player = players[guild.id] = Player(guild, interaction.channel)
        player.add(tracks)

    if len(tracks) == 1:
        await interaction.followup.send(f"Queued **{escape_markdown(tracks[0].title)}**.")
    else:
        await interaction.followup.send(f"Queued **{len(tracks)}** tracks.")


@bot.tree.command(description="Skip the current track")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        return await interaction.response.send_message("Skipped.")
    await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(description="Pause playback")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        return await interaction.response.send_message("Paused.")
    await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(description="Resume playback")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        return await interaction.response.send_message("Resumed.")
    await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@bot.tree.command(description="Show the queue")
@app_commands.guild_only()
async def queue(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    if player is None or (player.current is None and not player.queue):
        return await interaction.response.send_message("The queue is empty.", ephemeral=True)
    lines = []
    if player.current:
        lines.append(f"Now playing: **{escape_markdown(player.current.title[:100])}**")
    lines += [f"{i}. {escape_markdown(t.title[:100])}" for i, t in enumerate(islice(player.queue, 10), 1)]
    if len(player.queue) > 10:
        lines.append(f"…and {len(player.queue) - 10} more")
    await interaction.response.send_message("\n".join(lines))


async def leave(interaction: discord.Interaction, reply: str) -> None:
    player = players.get(interaction.guild.id)
    if player:
        player.stop()  # its cleanup disconnects
    elif interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect(force=True)
    else:
        return await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
    await interaction.response.send_message(reply)


@bot.tree.command(description="Stop playback, clear the queue and leave the voice channel")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    await leave(interaction, "Stopped.")


@bot.tree.command(description="Leave the voice channel and clear the queue")
@app_commands.guild_only()
async def disconnect(interaction: discord.Interaction):
    await leave(interaction, "Disconnected.")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    player = players.get(member.guild.id)
    if player is None:
        return
    vc = member.guild.voice_client
    if member.id == bot.user.id and after.channel is None:
        player.stop()  # we were disconnected/kicked
    elif vc and vc.channel:
        # Uncached members count as listeners, so a cache miss never makes us leave early.
        listeners = [
            uid for uid in vc.channel.voice_states
            if uid != bot.user.id and not getattr(member.guild.get_member(uid), "bot", False)
        ]
        if not listeners:
            player.stop()  # everyone left


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s) in %d guild(s)", bot.user, bot.user.id, len(bot.guilds))


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN is not set (copy .env.example to .env and fill it in)")
    bot.run(token, root_logger=True)
